"""Tests for the `/fetch moderation` command group (Phase 5+6 slice 4).

Two layers, matching this repository's two established conventions:

* the framework-independent `ModerationService` call sequence a command
  handler must perform (mirrors `tests/test_watchdog_commands.py`, which
  drives the service directly against a real on-disk `SQLiteStore`), and
* the Discord-interaction layer, driven through the real
  `app_commands.Command.callback` with the hand-written fake
  interaction/response/member triple established by
  `tests/test_phase_one_commands.py` (this repository does not use
  `discord.Interaction` autospec mocks anywhere).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from krubit.contracts.moderation import (
    ExecuteApprovedActionRequest,
    GetPendingReviewsQuery,
    RecordIncidentRequest,
    RequestHumanApprovalRequest,
    SubmitActionRecommendationRequest,
)
from krubit.discord.bot import FetchCommands
from krubit.discord.moderation_notifier import ModerationNotifier
from krubit.domain.moderation import (
    ActionReceipt,
    ApprovalDecision,
    ModerationCase,
    ModerationStatus,
)
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand
from krubit.services.foundation import FoundationService
from krubit.services.moderation import ModerationService
from krubit.storage.sqlite import SQLiteStore

GUILD_ID = 111
MEMBER_ID = 222
STAFF_ID = 7
NON_STAFF_ID = 42
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeExecutor:
    """Always-succeeding `ActionExecutor` stand-in.

    Records every `(case_id, execution_key)` it is handed so a test can prove
    `approve` passed a *fresh* idempotency key rather than reusing one.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt:
        self.calls.append((case.case_id, execution_key))
        return ActionReceipt(receipt_id="receipt:fake", succeeded=True, detail="ok")


class FailingExecutor:
    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt:
        return ActionReceipt(receipt_id="receipt:fail", succeeded=False, detail="missing perms")


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred: dict[str, bool] | None = None
        self.sent: dict[str, Any] | None = None

    async def defer(self, *, ephemeral: bool, thinking: bool) -> None:
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.sent = {"content": content, "ephemeral": ephemeral}


class _FakeMember:
    def __init__(self, member_id: int, *, can_manage_guild: bool) -> None:
        self.id = member_id
        self.mention = f"<@{member_id}>"
        self.guild_permissions = SimpleNamespace(
            manage_guild=can_manage_guild, administrator=False
        )


class _FakeInteraction:
    def __init__(self, member: _FakeMember) -> None:
        self.guild_id = GUILD_ID
        self.guild = SimpleNamespace(id=GUILD_ID)
        self.user = member
        self.response = _FakeResponse()
        self.edited_embed: discord.Embed | None = None
        self.edited_content: str | None = None

    async def edit_original_response(
        self, *, embed: discord.Embed | None = None, content: str | None = None
    ) -> None:
        if embed is not None:
            self.edited_embed = embed
        if content is not None:
            self.edited_content = content


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    await value.set_guild_enabled(GUILD_ID, True)
    await value.set_guild_enabled(100, True)
    for suffix in ("1", "2"):
        await value.record_incident(
            Incident(
                guild_id=GUILD_ID,
                incident_id=f"incident:{suffix}",
                kind=IncidentKind.MEMBER,
                band=RiskBand.INCIDENT,
                opened_at=NOW,
                evidence_packet_id=f"incident:{suffix}",
                recommended_action="review",
                acknowledged_by=None,
            )
        )
    # Seed incident for guild_id 100 (used by test_full_chain_approve_calls_discord_timeout_and_notifies)
    await value.record_incident(
        Incident(
            guild_id=100,
            incident_id="incident:1",
            kind=IncidentKind.MEMBER,
            band=RiskBand.INCIDENT,
            opened_at=NOW,
            evidence_packet_id="incident:1",
            recommended_action="review",
            acknowledged_by=None,
        )
    )
    try:
        yield value
    finally:
        await value.close()


def _bound_notifier() -> tuple[ModerationNotifier, MagicMock]:
    """A real `ModerationNotifier` over a mock guild/channel, so notification
    assertions run through the production `_send` gate chain rather than a
    hand-rolled fake with a different contract."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    channel.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=True, embed_links=True
    )
    guild = MagicMock()
    guild.get_channel.return_value = channel
    guild.me = MagicMock()
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: guild)
    return notifier, channel


def _commands(
    store: SQLiteStore,
    *,
    executor: object | None = None,
    notifier: ModerationNotifier | None = None,
) -> FetchCommands:
    return FetchCommands(
        FoundationService(store),
        moderation_executor=executor or FakeExecutor(),  # type: ignore[arg-type]
        moderation_notifier=notifier,
    )


def _group(commands: FetchCommands) -> Any:
    return next(command for command in commands.commands if command.name == "moderation")


def _subcommand(group: Any, name: str) -> Any:
    return next(command for command in group.commands if command.name == name)


async def _seed_pending_case(
    service: ModerationService, *, incident_id: str = "incident:1", nonce: str = "a"
) -> str:
    """Drive a case to APPROVAL_REQUIRED through the real service operations."""
    recorded = await service.record_incident(
        GUILD_ID,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": incident_id,
                "guild_id": str(GUILD_ID),
                "member_id": str(MEMBER_ID),
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": f"idem:{nonce}:record",
            }
        ),
    )
    await service.submit_action_recommendation(
        GUILD_ID,
        SubmitActionRecommendationRequest.from_dict(
            {
                "case_id": recorded.case_id,
                "recommended_action": "24h timeout",
                "idempotency_key": f"idem:{nonce}:recommend",
            }
        ),
    )
    await service.request_human_approval(
        GUILD_ID,
        RequestHumanApprovalRequest.from_dict(
            {
                "case_id": recorded.case_id,
                "review_deadline": "2026-01-02T00:00:00+00:00",
                "idempotency_key": f"idem:{nonce}:approval",
            }
        ),
    )
    return recorded.case_id


# -- Service-level call sequence ------------------------------------------------


@pytest.mark.asyncio
async def test_moderation_service_pending_and_approve_flow(store: SQLiteStore) -> None:
    """The exact `ModerationService` call sequence `/fetch moderation approve`
    must perform: pending -> record_human_decision -> execute_approved_action."""
    service = ModerationService(store, FakeExecutor())
    case_id = await _seed_pending_case(service)

    pending = await service.get_pending_reviews(
        GUILD_ID, GetPendingReviewsQuery.from_dict({"guild_id": str(GUILD_ID)})
    )
    assert case_id in pending.cases

    await service.record_human_decision(
        GUILD_ID,
        case_id,
        reviewer_id=STAFF_ID,
        decision=ApprovalDecision.APPROVED,
        timeout_duration=timedelta(hours=24),
    )
    execute_response = await service.execute_approved_action(
        GUILD_ID,
        ExecuteApprovedActionRequest.from_dict(
            {"case_id": case_id, "idempotency_key": "idem:execute"}
        ),
    )
    assert execute_response.status is ModerationStatus.EXECUTED


# -- Interaction-level tests, one per command -----------------------------------


@pytest.mark.asyncio
async def test_pending_lists_cases_awaiting_review(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "pending").callback(group, interaction)

    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert interaction.edited_embed is not None
    assert case_id in [field.value for field in interaction.edited_embed.fields]
    receipts = await store.list_receipts(GUILD_ID)
    assert ("fetch_moderation_pending", "succeeded", STAFF_ID) in [
        (item.action, item.status, item.actor_id) for item in receipts
    ]


@pytest.mark.asyncio
async def test_approve_executes_action_and_notifies_outcome(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = FakeExecutor()
    notifier, channel = _bound_notifier()
    commands = _commands(store, executor=executor, notifier=notifier)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "approve").callback(group, interaction, case_id, 24)

    assert interaction.edited_embed is not None
    stored = await store.get_moderation_case(case_id)
    assert stored is not None
    assert stored.status is ModerationStatus.EXECUTED
    assert stored.reviewer_id == STAFF_ID
    assert stored.reviewer_decision is ApprovalDecision.APPROVED
    assert stored.action_expiration is not None
    # A *fresh* idempotency key per invocation, not a fixed one.
    assert len(executor.calls) == 1
    assert executor.calls[0][0] == case_id
    assert executor.calls[0][1].startswith(f"admin-approve:{case_id}:")
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_reports_execution_failure_without_raising(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier, channel = _bound_notifier()
    commands = _commands(store, executor=FailingExecutor(), notifier=notifier)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "approve").callback(group, interaction, case_id, 1)

    stored = await store.get_moderation_case(case_id)
    assert stored is not None
    assert stored.status is ModerationStatus.EXECUTION_FAILED
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_still_answers_when_execute_approved_action_raises(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`approve` is the one command where an irreversible Discord action
    precedes the response. If anything after `record_human_decision` raises,
    the staff member must still get an answer -- never a deferred ephemeral
    response left spinning on "thinking" forever."""
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    async def boom(*args: object, **kwargs: object) -> object:
        raise ValueError("case status changed concurrently")

    monkeypatch.setattr(
        commands._moderation_service,  # pyright: ignore[reportPrivateUsage]
        "execute_approved_action",
        boom,
    )

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "approve").callback(group, interaction, case_id, 24)

    assert interaction.edited_embed is None
    assert interaction.edited_content is not None
    assert case_id in interaction.edited_content
    assert "case status changed concurrently" in interaction.edited_content
    # The message must not claim the timeout definitely did or did not land.
    assert "may or may not" in interaction.edited_content
    # The decision itself was already recorded before the failure.
    stored = await store.get_moderation_case(case_id)
    assert stored is not None
    assert stored.status is ModerationStatus.APPROVED


@pytest.mark.asyncio
async def test_approve_still_answers_when_notify_outcome_raises(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("notifier exploded past its own guard")

    monkeypatch.setattr(
        commands._moderation_notifier,  # pyright: ignore[reportPrivateUsage]
        "notify_outcome",
        boom,
    )

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "approve").callback(group, interaction, case_id, 24)

    assert interaction.edited_content is not None
    assert "notifier exploded past its own guard" in interaction.edited_content
    # The action itself genuinely executed -- only the announcement failed.
    stored = await store.get_moderation_case(case_id)
    assert stored is not None
    assert stored.status is ModerationStatus.EXECUTED


@pytest.mark.asyncio
async def test_approve_reports_unknown_case_as_plain_text(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "approve").callback(group, interaction, "case:nope", 24)

    assert interaction.edited_embed is None
    assert interaction.edited_content is not None
    assert "case:nope" in interaction.edited_content


@pytest.mark.asyncio
async def test_reject_records_the_rejection(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "reject").callback(group, interaction, case_id, "false positive")

    assert interaction.edited_embed is not None
    stored = await store.get_moderation_case(case_id)
    assert stored is not None
    assert stored.status is ModerationStatus.REJECTED
    assert stored.reviewer_decision is ApprovalDecision.REJECTED
    assert stored.reviewer_id == STAFF_ID
    receipts = await store.list_receipts(GUILD_ID)
    assert ("fetch_moderation_reject", "succeeded", STAFF_ID) in [
        (item.action, item.status, item.actor_id) for item in receipts
    ]


@pytest.mark.asyncio
async def test_reject_with_very_long_reason_answers_cleanly(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reason` is unbounded staff free text. discord.py's embed field-value
    limit is 1024 characters, so a very long reason must still produce a
    clean response -- never a hang (deferred response left spinning) or an
    unhandled `discord.HTTPException` from an oversized embed."""
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    long_reason = "x" * 2000
    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "reject").callback(group, interaction, case_id, long_reason)

    assert interaction.edited_embed is not None
    assert interaction.edited_content is None
    stored = await store.get_moderation_case(case_id)
    assert stored is not None
    assert stored.status is ModerationStatus.REJECTED


@pytest.mark.asyncio
async def test_reject_caps_persisted_card_reason_field_at_1024_chars(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    long_reason = "x" * 2000
    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "reject").callback(group, interaction, case_id, long_reason)

    assert interaction.edited_embed is not None
    reason_field = next(
        field for field in interaction.edited_embed.fields if field.name == "Reason"
    )
    assert len(reason_field.value) == 1024
    assert reason_field.value == long_reason[:1024]


@pytest.mark.asyncio
async def test_reject_still_answers_when_finish_raises(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `test_approve_still_answers_when_notify_outcome_raises` for
    `reject`'s own post-decision guard: a `finish()` failure (embed build,
    receipt write, or response edit) must not leave the deferred ephemeral
    response spinning on "thinking" forever."""
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("finish exploded past its own guard")

    monkeypatch.setattr(commands, "finish", boom)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "reject").callback(group, interaction, case_id, "false positive")

    assert interaction.edited_embed is None
    assert interaction.edited_content is not None
    assert case_id in interaction.edited_content
    assert "finish exploded past its own guard" in interaction.edited_content
    # The decision itself was already recorded before the failure.
    stored = await store.get_moderation_case(case_id)
    assert stored is not None
    assert stored.status is ModerationStatus.REJECTED


@pytest.mark.asyncio
async def test_history_returns_the_members_cases(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    first = await _seed_pending_case(commands._moderation_service, nonce="a")  # pyright: ignore[reportPrivateUsage]
    second = await _seed_pending_case(
        commands._moderation_service,  # pyright: ignore[reportPrivateUsage]
        incident_id="incident:2",
        nonce="b",
    )
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    target = _FakeMember(MEMBER_ID, can_manage_guild=False)
    await _subcommand(group, "history").callback(group, interaction, target)

    assert interaction.edited_embed is not None
    values = [field.value for field in interaction.edited_embed.fields]
    assert first in values
    assert second in values


@pytest.mark.asyncio
async def test_case_reports_one_cases_details(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "case").callback(group, interaction, case_id)

    assert interaction.edited_embed is not None
    values = [field.value for field in interaction.edited_embed.fields]
    assert ModerationStatus.APPROVAL_REQUIRED.value in values
    assert str(MEMBER_ID) in values
    assert "24h timeout" in values


@pytest.mark.asyncio
async def test_case_reports_unknown_case_as_plain_text(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    interaction = _FakeInteraction(_FakeMember(STAFF_ID, can_manage_guild=True))
    await _subcommand(group, "case").callback(group, interaction, "case:nope")

    assert interaction.edited_embed is None
    assert interaction.edited_content is not None
    assert "case:nope" in interaction.edited_content


# -- Staff gating ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_moderation_command_declares_manage_guild_default_permissions(
    store: SQLiteStore,
) -> None:
    group = _group(_commands(store))
    names = {command.name for command in group.commands}
    assert names == {"pending", "approve", "reject", "history", "case"}
    for command in group.commands:
        assert command.default_permissions is not None, command.name
        assert command.default_permissions.manage_guild is True, command.name
        assert command.guild_only is True, command.name


@pytest.mark.asyncio
async def test_non_staff_caller_is_denied_before_any_service_call(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(store)
    group = _group(commands)
    case_id = await _seed_pending_case(commands._moderation_service)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    denied = _FakeInteraction(_FakeMember(NON_STAFF_ID, can_manage_guild=False))
    await _subcommand(group, "approve").callback(group, denied, case_id, 24)

    assert denied.response.sent is not None
    assert denied.response.sent["ephemeral"] is True
    assert denied.response.deferred is None
    assert denied.edited_embed is None
    stored = await store.get_moderation_case(case_id)
    assert stored is not None
    assert stored.status is ModerationStatus.APPROVAL_REQUIRED

    for name, args in (
        ("pending", ()),
        ("reject", (case_id, "nope")),
        ("history", (_FakeMember(MEMBER_ID, can_manage_guild=False),)),
        ("case", (case_id,)),
    ):
        interaction = _FakeInteraction(_FakeMember(NON_STAFF_ID, can_manage_guild=False))
        await _subcommand(group, name).callback(group, interaction, *args)
        assert interaction.response.sent is not None, name
        assert interaction.edited_embed is None, name


@pytest.mark.asyncio
async def test_approve_duration_hours_is_a_bounded_integer_range(store: SQLiteStore) -> None:
    """Discord's own timeout API maxes out at 28 days; the parameter must be a
    typed, bounded int -- never a free-text duration string."""
    approve = _subcommand(_group(_commands(store)), "approve")
    parameter = next(item for item in approve.parameters if item.name == "duration_hours")
    assert parameter.type is discord.AppCommandOptionType.integer
    assert parameter.min_value == 1
    assert parameter.max_value == 672


@pytest.mark.asyncio
async def test_full_chain_approve_calls_discord_timeout_and_notifies(store: SQLiteStore) -> None:
    """record_incident -> submit_action_recommendation -> request_human_approval
    -> record_human_decision (approve) -> execute_approved_action, using the
    REAL DiscordActionExecutor (mocked discord.Client) instead of FakeExecutor,
    proving the full chain reaches member.timeout() and the notifier fires.
    """
    from unittest.mock import AsyncMock, MagicMock

    from krubit.contracts.moderation import RequestHumanApprovalRequest, SubmitActionRecommendationRequest
    from krubit.discord.moderation_executor import DiscordActionExecutor
    from krubit.discord.moderation_notifier import ModerationNotifier
    from krubit.domain.moderation import ApprovalDecision
    from datetime import timedelta

    member = MagicMock()
    member.timeout = AsyncMock()
    guild = MagicMock()
    guild.get_member.return_value = member
    bot = MagicMock()
    bot.get_guild.return_value = guild

    executor = DiscordActionExecutor(bot)
    service = ModerationService(store, executor)

    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:1",
            }
        ),
    )
    await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {"case_id": record_response.case_id, "recommended_action": "24h timeout", "idempotency_key": "idem:2"}
        ),
    )
    await service.request_human_approval(
        100,
        RequestHumanApprovalRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "review_deadline": "2026-01-02T00:00:00+00:00",
                "idempotency_key": "idem:3",
            }
        ),
    )
    case = await service.record_human_decision(
        100, record_response.case_id, reviewer_id=999, decision=ApprovalDecision.APPROVED,
        timeout_duration=timedelta(hours=24),
    )
    execute_response = await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict(
            {"case_id": record_response.case_id, "idempotency_key": "idem:4"}
        ),
    )
    assert execute_response.status.value == "executed"
    member.timeout.assert_awaited_once()

    notify_guild = MagicMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    channel.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=True, embed_links=True
    )
    notify_guild.get_channel.return_value = channel
    notify_guild.me = MagicMock()
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: notify_guild)
    await notifier.notify_outcome(case, succeeded=True)
    channel.send.assert_awaited_once()
