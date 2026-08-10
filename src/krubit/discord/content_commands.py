"""Unified `/fetch creator`, `/fetch latest`, `/fetch schedule`, and
`/fetch notifications` command surfaces.

`ContentCommandService` is the framework-independent core: every mutating method takes
a plain `ActorContext` (never a `discord.Interaction`) and a `confirm: bool = False`
parameter. The first call — `confirm` omitted — only performs an authority check and,
on success, returns `CommandStatus.CONFIRMATION_REQUIRED` with a rendered preview card;
it never mutates anything. A denied actor gets `CommandStatus.DENIED` immediately,
before any confirmation is ever shown, so an unauthorized member can never even see what
a mutation *would* do. Only a second call with `confirm=True` — which the Discord-layer
confirmation view sends after the actor clicks "Confirm" — performs the actual mutation.

This split is what makes authority and safety properties (denied-before-preview,
preview-has-zero-side-effects, retry validates ownership) directly unit-testable without
any `discord.Interaction` mocking; the thin `app_commands.Group` classes at the bottom of
this module only translate a Discord interaction into an `ActorContext` and render the
returned `CommandResult`.

Every authority check reuses `krubit.services.creator_registry.require_creator_authority`
— the single place "admins may act on any account; the Creator role only on one's own" is
defined — rather than each command re-deriving its own copy of that rule.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

import discord
from discord import app_commands

from krubit.discord.cards import render_card
from krubit.discord.content_cards import (
    ContentGroup,
    ContentGroupMember,
    RenderedCard,
    build_live_card,
    build_social_card,
)
from krubit.discord.content_runtime import ContentRuntime, parse_delivery_id
from krubit.domain.creator_signals import (
    Capability,
    ContentEvent,
    ContentKind,
    ContentState,
    Platform,
    creator_account_id,
)
from krubit.domain.models import Card, CardField, JSONValue
from krubit.integrations.authorize_urls import build_meta_authorize_url, build_tiktok_authorize_url
from krubit.integrations.catalog import CATALOG, recognize_account_url
from krubit.services.creator_analytics import CreatorAnalyticsService
from krubit.services.creator_registry import CreatorAuthorityError, CreatorRegistry
from krubit.services.creator_registry import require_creator_authority as _require_authority
from krubit.services.notification_policy import MentionBudgetState, NotificationPolicy
from krubit.storage.sqlite import CreatorBootstrap, SQLiteStore

if TYPE_CHECKING:
    from krubit.discord.bot import FetchCommands

_CREATOR_ROLE_NAME = "Creator"
_NOTIFICATION_CHANNEL_NAME = "social-notifications"
_PREVIEW_ROUTE_MISSING = "no route is configured for this content kind yet"
_UNLIMITED = MentionBudgetState(limit=None)

# How long an issued authorize link stays valid before it must be
# re-requested. No existing precedent constant for this in the codebase
# (this is the first caller of `issue_oauth_attempt`) -- 10 minutes is
# long enough for a member to click through, short enough that a leaked
# link (e.g. pasted into the wrong channel) has a small blast radius.
_OAUTH_ATTEMPT_TTL = timedelta(minutes=10)


class CommandStatus(StrEnum):
    """The outcome of one `ContentCommandService` call."""

    SUCCEEDED = "succeeded"
    CONFIRMATION_REQUIRED = "confirmation_required"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ActorContext:
    """The plain facts a command needs about the member invoking it.

    Deliberately framework-independent: no `discord.Interaction`/`discord.Member`
    reference, so `ContentCommandService` methods can be called directly from a unit
    test. `has_creator_role` and `is_admin` are resolved by the Discord-layer command
    wrapper before the service is ever called.
    """

    guild_id: int
    member_id: int
    is_admin: bool
    has_creator_role: bool


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of one `ContentCommandService` call, ready for a Discord wrapper to
    render. `card` is a simple fields-and-description summary (rendered via
    `render_card`); `rendered` is a full `RenderedCard` (embed + buttons + mention text)
    used only by `notification_preview`, which never sends or edits anything itself."""

    status: CommandStatus
    card: Card | None = None
    rendered: RenderedCard | None = None
    detail: dict[str, JSONValue] = field(default_factory=dict[str, JSONValue])


class GuildLike(Protocol):
    id: int


async def resolve_creator_bootstrap(
    store: SQLiteStore, guild: discord.Guild, *, now: datetime
) -> CreatorBootstrap | None:
    """Resolve and persist a guild's Creator role and notification channel, once.

    Resolution is by exact Discord name (`Creator` / `#social-notifications`) — never
    guessed, never created implicitly. If a prior resolution is already stored, it is
    reused as-is (a same-named role or channel created later never silently swaps in).
    Returns `None` when the resource is missing or ambiguous (more than one exact-name
    match); callers surface that as a health finding, never as an implicit creation.
    """
    existing = await store.get_creator_bootstrap(guild.id)
    if existing is not None:
        return existing
    matching_roles = [role for role in guild.roles if role.name == _CREATOR_ROLE_NAME]
    matching_channels = [
        channel for channel in guild.text_channels if channel.name == _NOTIFICATION_CHANNEL_NAME
    ]
    if len(matching_roles) != 1 or len(matching_channels) != 1:
        return None
    return await store.save_creator_bootstrap(
        guild_id=guild.id,
        creator_role_id=matching_roles[0].id,
        notification_channel_id=matching_channels[0].id,
        now=now,
    )


def _confirmation(*, title: str, description: str, **fields: str) -> Card:
    return Card(
        kind="fetched",
        title=f"Confirm: {title}",
        description=description,
        fields=tuple(CardField(name, value, True) for name, value in fields.items()),
    )


def _denied(exc: CreatorAuthorityError) -> CommandResult:
    return CommandResult(CommandStatus.DENIED, detail={"reason": str(exc)})


class ContentCommandService:
    """The framework-independent core of every `/fetch creator|latest|schedule|
    notifications` command."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        registry: CreatorRegistry | None = None,
        runtime: ContentRuntime | None = None,
        analytics: CreatorAnalyticsService | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry or CreatorRegistry(store)
        self._runtime = runtime or ContentRuntime(store)
        self._analytics = analytics or CreatorAnalyticsService(store)
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def store(self) -> SQLiteStore:
        return self._store

    # -- creator: enrollment and lifecycle -----------------------------------------

    async def creator_add(
        self, *, actor: ActorContext, owner: ActorContext, url: str, confirm: bool = False
    ) -> CommandResult:
        if actor.guild_id != owner.guild_id:
            return CommandResult(CommandStatus.DENIED, detail={"reason": "cross_guild_owner"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=owner.member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        try:
            recognized = recognize_account_url(url)
        except ValueError as exc:
            return CommandResult(CommandStatus.FAILED, detail={"reason": str(exc)})
        if not confirm:
            card = _confirmation(
                title="Add Creator Account",
                description=f"Add {recognized.canonical_url} for <@{owner.member_id}>?",
                Platform=recognized.platform.value,
                Handle=recognized.handle,
            )
            return CommandResult(
                CommandStatus.CONFIRMATION_REQUIRED,
                card=card,
                detail={"platform": recognized.platform.value, "handle": recognized.handle},
            )
        now = self._now()
        account = await self._registry.add_account(
            guild_id=owner.guild_id,
            actor_member_id=actor.member_id,
            owner_member_id=owner.member_id,
            actor_is_admin=actor.is_admin,
            actor_has_creator_role=actor.has_creator_role,
            recognized=recognized,
            resolved_external_id=recognized.handle,
            now=now,
        )
        card = Card(
            "fetched",
            "Fetched: Creator Account Added",
            f"{account.handle} added, paused until routed and authorized.",
        )
        return CommandResult(
            CommandStatus.SUCCEEDED, card=card, detail={"account_id": account.account_id}
        )

    async def creator_authorize(
        self,
        *,
        actor: ActorContext,
        owner: ActorContext,
        url: str,
        capability: Capability,
        meta_app_id: str | None,
        meta_callback_base_url: str | None,
        tiktok_client_key: str | None,
        tiktok_callback_base_url: str | None,
    ) -> CommandResult:
        if actor.guild_id != owner.guild_id:
            return CommandResult(CommandStatus.DENIED, detail={"reason": "cross_guild_owner"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=owner.member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        if capability not in (Capability.ACCOUNT, Capability.SOCIAL):
            return CommandResult(
                CommandStatus.FAILED,
                detail={"reason": f"{capability.value} capability is not supported yet"},
            )
        try:
            recognized = recognize_account_url(url)
        except ValueError as exc:
            return CommandResult(CommandStatus.FAILED, detail={"reason": str(exc)})

        account_id = creator_account_id(recognized.platform, recognized.handle)
        account = await self._store.get_creator_account(owner.guild_id, account_id)
        if account is None or account.owner_member_id != owner.member_id:
            return CommandResult(
                CommandStatus.FAILED,
                detail={
                    "reason": "creator account not found -- add it first with /fetch creator add"
                },
            )

        now = self._now()
        if recognized.platform in (Platform.INSTAGRAM, Platform.THREADS):
            if meta_app_id is None or meta_callback_base_url is None:
                return CommandResult(
                    CommandStatus.FAILED,
                    detail={"reason": "Meta authorization is not configured yet"},
                )
            redirect_uri = f"{meta_callback_base_url}/callbacks/meta/authorize"
            state = await self._store.issue_oauth_attempt(
                guild_id=owner.guild_id,
                member_id=owner.member_id,
                account_id=account.account_id,
                platform=recognized.platform.value,
                capability=capability.value,
                redirect_uri=redirect_uri,
                now=now,
                ttl=_OAUTH_ATTEMPT_TTL,
            )
            authorize_url = build_meta_authorize_url(
                app_id=meta_app_id,
                redirect_uri=redirect_uri,
                state=state,
                platform=recognized.platform,
                capability=capability,
            )
        elif recognized.platform is Platform.TIKTOK:
            if tiktok_client_key is None or tiktok_callback_base_url is None:
                return CommandResult(
                    CommandStatus.FAILED,
                    detail={"reason": "TikTok authorization is not configured yet"},
                )
            redirect_uri = f"{tiktok_callback_base_url}/callbacks/tiktok/authorize"
            state = await self._store.issue_oauth_attempt(
                guild_id=owner.guild_id,
                member_id=owner.member_id,
                account_id=account.account_id,
                platform=recognized.platform.value,
                capability=capability.value,
                redirect_uri=redirect_uri,
                now=now,
                ttl=_OAUTH_ATTEMPT_TTL,
            )
            authorize_url = build_tiktok_authorize_url(
                client_key=tiktok_client_key,
                redirect_uri=redirect_uri,
                state=state,
                capability=capability,
            )
        else:
            return CommandResult(
                CommandStatus.FAILED,
                detail={
                    "reason": f"{recognized.platform.value} does not support OAuth authorization"
                },
            )

        card = Card(
            "fetched",
            "Fetched: Authorization Link",
            f"Click to authorize {capability.value} access for {account.handle}: "
            f"{authorize_url}\n\nThis link expires in "
            f"{int(_OAUTH_ATTEMPT_TTL.total_seconds() // 60)} minutes and can only be used once.",
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"platform": recognized.platform.value, "capability": capability.value},
        )

    async def _toggle_pause(
        self,
        *,
        actor: ActorContext,
        account_id: str,
        paused: bool,
        confirm: bool,
        verb: str,
        is_removal: bool = False,
    ) -> CommandResult:
        existing = await self._store.get_creator_account(actor.guild_id, account_id)
        if existing is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=existing.owner_member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        if not confirm:
            if is_removal:
                # "Remove" has no hard-delete path in Krubit — it is operationally a
                # pause. The confirmation copy must say so plainly so an operator or
                # creator can never mistake this for their data being deleted.
                card = _confirmation(
                    title="Pause Creator Account (Remove)",
                    description=(
                        f"This does not delete any data. Pause monitoring for "
                        f"{existing.handle}? History and receipts are preserved, and "
                        f"you can resume it later with /fetch creator resume."
                    ),
                    Account=existing.handle,
                )
            else:
                card = _confirmation(
                    title=f"{verb.title()} Creator Account",
                    description=f"{verb.title()} {existing.handle}?",
                    Account=existing.handle,
                )
            return CommandResult(
                CommandStatus.CONFIRMATION_REQUIRED, card=card, detail={"account_id": account_id}
            )
        now = self._now()
        method = self._registry.pause_account if paused else self._registry.resume_account
        updated = await method(
            guild_id=actor.guild_id,
            actor_member_id=actor.member_id,
            account_id=account_id,
            actor_is_admin=actor.is_admin,
            actor_has_creator_role=actor.has_creator_role,
            now=now,
        )
        if is_removal:
            card = Card(
                "fetched",
                "Fetched: Account Paused (Not Deleted)",
                f"{updated.handle}'s monitoring is paused. No data was deleted — "
                f"history and receipts are preserved, and this can be reversed with "
                f"/fetch creator resume.",
            )
        else:
            card = Card("fetched", f"Fetched: Account {verb.title()}d", updated.handle)
        return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"paused": updated.paused})

    async def creator_pause(
        self, *, actor: ActorContext, account_id: str, confirm: bool = False
    ) -> CommandResult:
        return await self._toggle_pause(
            actor=actor, account_id=account_id, paused=True, confirm=confirm, verb="pause"
        )

    async def creator_resume(
        self, *, actor: ActorContext, account_id: str, confirm: bool = False
    ) -> CommandResult:
        return await self._toggle_pause(
            actor=actor, account_id=account_id, paused=False, confirm=confirm, verb="resume"
        )

    async def creator_remove(
        self, *, actor: ActorContext, account_id: str, confirm: bool = False
    ) -> CommandResult:
        """Krubit has no hard-delete path for a creator account — removal pauses it,
        preserving its ledger and receipt history, matching `CreatorRegistry.
        pause_account`'s documented behavior. Both the confirmation and result card
        copy say this plainly (`is_removal=True`) so an operator or creator can never
        mistake a pause for their data actually being deleted."""
        return await self._toggle_pause(
            actor=actor,
            account_id=account_id,
            paused=True,
            confirm=confirm,
            verb="remove",
            is_removal=True,
        )

    async def creator_transfer(
        self,
        *,
        actor: ActorContext,
        account_id: str,
        new_owner_member_id: int,
        confirm: bool = False,
    ) -> CommandResult:
        if not actor.is_admin:
            return CommandResult(
                CommandStatus.DENIED,
                detail={
                    "reason": "transferring a creator account requires administrator authority"
                },
            )
        existing = await self._store.get_creator_account(actor.guild_id, account_id)
        if existing is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        if not confirm:
            card = _confirmation(
                title="Transfer Creator Account",
                description=f"Transfer {existing.handle} to <@{new_owner_member_id}>?",
                Account=existing.handle,
            )
            return CommandResult(
                CommandStatus.CONFIRMATION_REQUIRED, card=card, detail={"account_id": account_id}
            )
        now = self._now()
        updated = await self._registry.transfer_account(
            guild_id=actor.guild_id,
            actor_member_id=actor.member_id,
            account_id=account_id,
            new_owner_member_id=new_owner_member_id,
            actor_is_admin=actor.is_admin,
            now=now,
        )
        card = Card("fetched", "Fetched: Account Transferred", updated.handle)
        return CommandResult(
            CommandStatus.SUCCEEDED, card=card, detail={"new_owner_member_id": new_owner_member_id}
        )

    async def creator_route(
        self,
        *,
        actor: ActorContext,
        account_id: str,
        content_kind: ContentKind,
        channel_id: int,
        mention_role_id: int | None,
        confirm: bool = False,
    ) -> CommandResult:
        existing = await self._store.get_creator_account(actor.guild_id, account_id)
        if existing is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=existing.owner_member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        if not confirm:
            card = _confirmation(
                title="Route Creator Content",
                description=(
                    f"Route {existing.handle}'s {content_kind.value} content "
                    f"to <#{channel_id}>?"
                ),
                Account=existing.handle,
                ContentKind=content_kind.value,
            )
            return CommandResult(
                CommandStatus.CONFIRMATION_REQUIRED, card=card, detail={"account_id": account_id}
            )
        saved = await self._registry.save_route(
            guild_id=actor.guild_id,
            actor_member_id=actor.member_id,
            account_id=account_id,
            actor_is_admin=actor.is_admin,
            actor_has_creator_role=actor.has_creator_role,
            content_kind=content_kind,
            channel_id=channel_id,
            mention_role_id=mention_role_id,
            now=self._now(),
        )
        card = Card(
            "fetched",
            "Fetched: Route Saved",
            f"{existing.handle}'s {content_kind.value} content routes to <#{saved.channel_id}>.",
        )
        return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"account_id": account_id})

    # -- creator: read-only facts ----------------------------------------------------

    async def creator_list(self, *, actor: ActorContext, own_only: bool = False) -> CommandResult:
        accounts = (
            await self._analytics.own_profile(actor.guild_id, actor.member_id)
            if own_only
            else tuple(await self._store.list_creator_accounts(actor.guild_id))
        )
        description = "\n".join(
            f"{acc.handle} ({acc.platform.value}) — {'paused' if acc.paused else 'active'}"
            for acc in accounts
        ) or "No creator accounts registered."
        card = Card("fetched", "Fetched: Creator Accounts", description)
        return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"count": len(accounts)})

    async def creator_show(self, *, actor: ActorContext, account_id: str) -> CommandResult:
        existing = await self._store.get_creator_account(actor.guild_id, account_id)
        if existing is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=existing.owner_member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        report = await self._analytics.account_report(actor.guild_id, account_id)
        if report is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        counts = report.delivery_counts
        card = Card(
            "fetched",
            f"Fetched: {report.account.handle}",
            f"Owner <@{report.account.owner_member_id}> — "
            f"{'paused' if report.account.paused else 'active'}",
            fields=(
                CardField("Platform", report.account.platform.value, True),
                CardField(
                    "Delivered / Pending / Failed / Cancelled",
                    f"{counts.delivered} / {counts.pending} / {counts.failed} / {counts.cancelled}",
                    True,
                ),
                CardField(
                    "Average delivery latency",
                    (
                        f"{report.average_delivery_latency_seconds:.1f}s"
                        if report.average_delivery_latency_seconds is not None
                        else "No delivered content yet"
                    ),
                    True,
                ),
                CardField(
                    "Cursor",
                    report.cursor.updated_at.isoformat() if report.cursor else "No cursor yet",
                    True,
                ),
            ),
        )
        return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"account_id": account_id})

    async def creator_verify(self, *, actor: ActorContext, account_id: str) -> CommandResult:
        existing = await self._store.get_creator_account(actor.guild_id, account_id)
        if existing is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=existing.owner_member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        descriptor = CATALOG[existing.platform]
        # `CATALOG` facts are static and platform-wide — identical for every account on
        # this platform, regardless of that specific account's real authorization
        # state. `existing.paused` is the one genuinely account-specific signal this
        # command can report without a live connector call (no connector instances are
        # wired into this command layer yet). Both the title and the leading field make
        # that distinction explicit so this is never mistaken for a live per-account
        # authorization check.
        fields = (
            CardField(
                "This account's monitoring state",
                "paused" if existing.paused else "active",
                False,
            ),
            *(
                CardField(f"Platform baseline: {fact.capability.value}", fact.state.value, True)
                for fact in descriptor.capabilities
            ),
        )
        card = Card(
            "fetched",
            f"Fetched: {existing.platform.value} Baseline Capability Declaration",
            (
                f"{existing.handle}'s platform-wide baseline capability declaration — "
                "not a live check of this account's current authorization. Krubit does "
                "not yet perform a live per-account verification call."
            ),
            fields=fields,
        )
        return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"account_id": account_id})

    async def creator_template(
        self, *, headline: str, footer: str, accent_color: int
    ) -> CommandResult:
        """Validate proposed template text against `validate_template`'s mention-free,
        allowed-placeholder rules. Never persisted: Krubit has no template storage yet,
        so this is a pure preview of whether the supplied text would be accepted."""
        from krubit.services.notification_policy import NotificationTemplate, validate_template

        try:
            template = validate_template(
                NotificationTemplate(headline=headline, footer=footer, accent_color=accent_color)
            )
        except ValueError as exc:
            return CommandResult(CommandStatus.FAILED, detail={"reason": str(exc)})
        card = Card(
            "fetched",
            "Fetched: Template Preview",
            "This template text is valid.",
            fields=(
                CardField("Headline", template.headline, False),
                CardField("Footer", template.footer, False),
            ),
        )
        return CommandResult(CommandStatus.SUCCEEDED, card=card)

    # -- notifications ----------------------------------------------------------------

    async def notification_status(self, *, actor: ActorContext) -> CommandResult:
        accounts = await self._store.list_creator_accounts(actor.guild_id)
        delivered = pending = failed = cancelled = 0
        for account in accounts:
            counts = await self._analytics.delivery_counts(actor.guild_id, account.account_id)
            delivered += counts.delivered
            pending += counts.pending
            failed += counts.failed
            cancelled += counts.cancelled
        quota = await self._analytics.quota_history(actor.guild_id, limit=5)
        card = Card(
            "fetched",
            "Fetched: Notification Status",
            "Guild-wide delivery facts across every registered creator account.",
            fields=(
                CardField("Accounts", str(len(accounts)), True),
                CardField(
                    "Delivered / Pending / Failed / Cancelled",
                    f"{delivered} / {pending} / {failed} / {cancelled}",
                    True,
                ),
                CardField(
                    "Recent quota outcomes",
                    ", ".join(receipt.outcome for receipt in quota) or "None recorded",
                    False,
                ),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"delivered": delivered, "pending": pending, "failed": failed},
        )

    async def notification_preview(
        self, *, actor: ActorContext, account_id: str, content_kind: ContentKind
    ) -> CommandResult:
        """Render exactly the card a real delivery would use — with zero side effects.

        No Discord send or edit, no mention-budget claim (`NotificationPolicy.
        decide_mention` is pure and never touches storage, unlike `evaluate_and_claim`),
        no role mutation, and no Scheduled Event action: this method never calls
        `ContentRuntime` or `ScheduledEventSynchronizer` at all.

        Gated by the same `_require_authority` rule as every other per-account command
        (`creator_show`, `creator_verify`, `notification_retry`, ...): an admin may
        preview any account, a `Creator`-role member only their own. A preview card can
        include the account's canonical URL and configured mention role, so a denied
        actor must never see it.
        """
        account = await self._store.get_creator_account(actor.guild_id, account_id)
        if account is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=account.owner_member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        routes = await self._store.list_creator_routes(actor.guild_id, account_id)
        route = next((r for r in routes if r.content_kind is content_kind), None)
        if route is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": _PREVIEW_ROUTE_MISSING})
        group = ContentGroup(
            content_kind=content_kind,
            members=(
                ContentGroupMember(
                    platform=account.platform,
                    canonical_url=account.canonical_url,
                    creator_display_name=account.handle,
                    title="Preview content",
                ),
            ),
            mention_role_id=route.mention_role_id,
        )
        preview_event = ContentEvent(
            guild_id=actor.guild_id,
            account_id=account_id,
            platform=account.platform,
            external_id="preview",
            content_kind=content_kind,
            state=ContentState.LIVE if content_kind is ContentKind.LIVE else ContentState.PUBLISHED,
            canonical_url=account.canonical_url,
            title="Preview content",
            published_at=None,
            first_observed_at=self._now(),
            last_observed_at=self._now(),
        )
        policy = NotificationPolicy(
            quiet_hours=None,
            live_everyone_budget=_UNLIMITED,
            social_role_budget=_UNLIMITED,
            social_mention_role_id=route.mention_role_id,
        )
        mention = policy.decide_mention(preview_event)
        rendered = (
            build_live_card(group, mention=mention.kind)
            if content_kind is ContentKind.LIVE
            else build_social_card(group, mention=mention.kind)
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            rendered=rendered,
            detail={"preview": True, "mention": mention.kind.value},
        )

    async def notification_retry(
        self, *, actor: ActorContext, delivery_id: str, guild: GuildLike, confirm: bool = False
    ) -> CommandResult:
        try:
            platform, external_id, transition_seq = parse_delivery_id(delivery_id)
        except ValueError:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "malformed_delivery_id"})
        delivery = await self._store.get_content_delivery_by_seq(
            actor.guild_id, platform, external_id, transition_seq
        )
        if delivery is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "delivery_not_found"})
        account = await self._store.get_creator_account(actor.guild_id, delivery.account_id)
        if account is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=account.owner_member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        if delivery.status != "failed":
            return CommandResult(
                CommandStatus.FAILED,
                detail={"reason": "delivery_not_failed", "status": delivery.status},
            )
        event = await self._store.get_content_event(actor.guild_id, platform, external_id)
        if event is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "event_not_found"})
        routes = await self._store.list_creator_routes(actor.guild_id, account.account_id)
        if not any(route.content_kind is event.content_kind for route in routes):
            return CommandResult(CommandStatus.FAILED, detail={"reason": "route_not_configured"})
        if not confirm:
            card = _confirmation(
                title="Retry Delivery",
                description=f"Retry the failed delivery for {account.handle}?",
                DeliveryId=delivery_id,
            )
            return CommandResult(
                CommandStatus.CONFIRMATION_REQUIRED, card=card, detail={"delivery_id": delivery_id}
            )
        applied = await self._runtime.retry_delivery(guild, delivery_id)  # type: ignore[arg-type]
        status = CommandStatus.SUCCEEDED if applied else CommandStatus.FAILED
        card = Card(
            "fetched",
            "Fetched: Delivery Retried" if applied else "Fetched: Retry Did Not Apply",
            f"delivery_id={delivery_id}",
        )
        return CommandResult(status, card=card, detail={"applied": applied})

    async def notification_retract(
        self, *, actor: ActorContext, delivery_id: str, guild: GuildLike, confirm: bool = False
    ) -> CommandResult:
        try:
            platform, external_id, transition_seq = parse_delivery_id(delivery_id)
        except ValueError:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "malformed_delivery_id"})
        delivery = await self._store.get_content_delivery_by_seq(
            actor.guild_id, platform, external_id, transition_seq
        )
        if delivery is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "delivery_not_found"})
        account = await self._store.get_creator_account(actor.guild_id, delivery.account_id)
        if account is None:
            return CommandResult(CommandStatus.FAILED, detail={"reason": "account_not_found"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=account.owner_member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        if delivery.status == "cancelled":
            return CommandResult(CommandStatus.FAILED, detail={"reason": "already_retracted"})
        if not confirm:
            card = _confirmation(
                title="Retract Delivery",
                description=f"Retract the delivered announcement for {account.handle}?",
                DeliveryId=delivery_id,
            )
            return CommandResult(
                CommandStatus.CONFIRMATION_REQUIRED, card=card, detail={"delivery_id": delivery_id}
            )
        applied = await self._runtime.retract_delivery(guild, delivery_id)  # type: ignore[arg-type]
        status = CommandStatus.SUCCEEDED if applied else CommandStatus.FAILED
        card = Card(
            "fetched",
            "Fetched: Delivery Retracted" if applied else "Fetched: Retract Did Not Apply",
            f"delivery_id={delivery_id}",
        )
        return CommandResult(status, card=card, detail={"applied": applied})

    # -- latest / schedule --------------------------------------------------------

    async def latest(
        self, *, actor: ActorContext, platform: Platform | None = None, limit: int = 5
    ) -> CommandResult:
        accounts = await self._store.list_creator_accounts(actor.guild_id)
        if platform is not None:
            accounts = [account for account in accounts if account.platform is platform]
        lines: list[str] = []
        for account in accounts:
            events = await self._store.list_recent_content_events(
                actor.guild_id, account.account_id, limit=limit
            )
            for event in events:
                lines.append(
                    f"{account.handle} ({account.platform.value}): "
                    f"{event.title or event.content_kind.value} — {event.state.value}"
                )
        description = "\n".join(lines[:limit]) or "No content observed yet."
        card = Card("fetched", "Fetched: Latest Creator Content", description)
        return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"item_count": len(lines)})

    async def schedule_status(self, *, actor: ActorContext) -> CommandResult:
        mappings = await self._store.list_owned_scheduled_event_mappings(actor.guild_id)
        by_status: dict[str, int] = {}
        for mapping in mappings:
            by_status[mapping.discord_status] = by_status.get(mapping.discord_status, 0) + 1
        description = (
            ", ".join(f"{status}: {count}" for status, count in sorted(by_status.items()))
            or "No Krubit-owned Scheduled Events."
        )
        card = Card("fetched", "Fetched: Scheduled Event Status", description)
        return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"count": len(mappings)})


# ---------------------------------------------------------------------------------
# Discord-layer command groups
# ---------------------------------------------------------------------------------


def _content_kind_choice(value: str) -> ContentKind:
    return ContentKind(value)


class _ConfirmView(discord.ui.View):
    """A private, ephemeral Confirm/Cancel view bound to one pending mutation."""

    def __init__(
        self,
        *,
        on_confirm: Callable[[], Awaitable[CommandResult]],
        render: Callable[[CommandResult], discord.Embed],
    ) -> None:
        super().__init__(timeout=60.0)
        self._on_confirm = on_confirm
        self._render = render

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        result = await self._on_confirm()
        await interaction.response.edit_message(embed=self._render(result), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        await interaction.response.edit_message(
            content="Cancelled. Nothing was changed.", embed=None, view=None
        )


async def _actor_context(
    service: ContentCommandService, interaction: discord.Interaction, member_id: int | None = None
) -> ActorContext | None:
    """Resolve an `ActorContext`, including whether the actor holds the guild's
    once-bootstrapped Creator role. Never implicitly creates the role or channel."""
    if interaction.guild_id is None or interaction.guild is None:
        await interaction.response.send_message("This command is server-only.", ephemeral=True)
        return None
    user = interaction.user
    if not isinstance(user, discord.Member):
        await interaction.response.send_message("This command is server-only.", ephemeral=True)
        return None
    subject_id = member_id if member_id is not None else user.id
    bootstrap = await resolve_creator_bootstrap(
        service.store, interaction.guild, now=datetime.now(UTC)
    )
    subject_member = interaction.guild.get_member(subject_id)
    has_creator_role = (
        bootstrap is not None
        and subject_member is not None
        and any(role.id == bootstrap.creator_role_id for role in subject_member.roles)
    )
    return ActorContext(
        guild_id=interaction.guild_id,
        member_id=subject_id,
        is_admin=user.guild_permissions.administrator or user.guild_permissions.manage_guild,
        has_creator_role=has_creator_role,
    )


def _render_or_done(outcome: CommandResult) -> discord.Embed:
    return render_card(outcome.card) if outcome.card else discord.Embed(title="Done")


async def _present_result(
    interaction: discord.Interaction,
    result: CommandResult,
    *,
    confirm: Callable[[], Awaitable[CommandResult]] | None = None,
) -> None:
    """Render one `CommandResult`, wrapping a `CONFIRMATION_REQUIRED` result in a
    private `_ConfirmView` when the caller supplies the bound `confirm=True` callback.
    Shared by `CreatorCommands` and `NotificationCommands` so confirmation rendering
    stays identical across every mutating command."""
    show_confirmation = (
        result.status is CommandStatus.CONFIRMATION_REQUIRED
        and result.card is not None
        and confirm is not None
    )
    if show_confirmation and confirm is not None and result.card is not None:
        view = _ConfirmView(on_confirm=confirm, render=_render_or_done)
        await interaction.followup.send(embed=render_card(result.card), view=view, ephemeral=True)
        return
    if result.card is not None:
        await interaction.followup.send(embed=render_card(result.card), ephemeral=True)
        return
    await interaction.followup.send(content=f"{result.status.value}", ephemeral=True)


class CreatorCommands(app_commands.Group):
    """`/fetch creator` — enrollment, lifecycle, routing, and read-only facts."""

    def __init__(self, parent: FetchCommands, service: ContentCommandService) -> None:
        super().__init__(name="creator", description="Manage guild creator accounts")
        self._parent = parent
        self._service = service

    @app_commands.command(name="add", description="Add a creator profile URL")
    @app_commands.guild_only()
    async def add(
        self, interaction: discord.Interaction, url: str, member: discord.Member | None = None
    ) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        owner = (
            await _actor_context(self._service, interaction, member_id=member.id)
            if member is not None
            else actor
        )
        if owner is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.creator_add(actor=actor, owner=owner, url=url)
        await self._present(interaction, result, confirm=lambda: self._service.creator_add(
            actor=actor, owner=owner, url=url, confirm=True
        ))

    @app_commands.command(
        name="authorize", description="Get an OAuth authorization link for a creator account"
    )
    @app_commands.guild_only()
    async def authorize(
        self,
        interaction: discord.Interaction,
        url: str,
        capability: Literal["account", "social"],
        member: discord.Member | None = None,
    ) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        owner = (
            await _actor_context(self._service, interaction, member_id=member.id)
            if member is not None
            else actor
        )
        if owner is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.creator_authorize(
            actor=actor,
            owner=owner,
            url=url,
            capability=Capability(capability),
            meta_app_id=self._parent._meta_app_id,
            meta_callback_base_url=self._parent._meta_callback_base_url,
            tiktok_client_key=self._parent._tiktok_client_key,
            tiktok_callback_base_url=self._parent._tiktok_callback_base_url,
        )
        await self._present(interaction, result)

    @app_commands.command(name="pause", description="Pause a creator account")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction, account_id: str) -> None:
        await self._toggle(interaction, account_id, self._service.creator_pause)

    @app_commands.command(name="resume", description="Resume a paused creator account")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction, account_id: str) -> None:
        await self._toggle(interaction, account_id, self._service.creator_resume)

    @app_commands.command(name="remove", description="Remove (pause) a creator account")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, account_id: str) -> None:
        await self._toggle(interaction, account_id, self._service.creator_remove)

    async def _toggle(
        self,
        interaction: discord.Interaction,
        account_id: str,
        method: Callable[..., Awaitable[CommandResult]],
    ) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await method(actor=actor, account_id=account_id)
        await self._present(
            interaction,
            result,
            confirm=lambda: method(actor=actor, account_id=account_id, confirm=True),
        )

    @app_commands.command(name="list", description="List creator accounts")
    @app_commands.guild_only()
    async def list_accounts(
        self, interaction: discord.Interaction, mine_only: bool = False
    ) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.creator_list(actor=actor, own_only=mine_only)
        await interaction.followup.send(embed=render_card(result.card), ephemeral=True)  # type: ignore[arg-type]

    @app_commands.command(name="show", description="Show one creator account's facts")
    @app_commands.guild_only()
    async def show(self, interaction: discord.Interaction, account_id: str) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.creator_show(actor=actor, account_id=account_id)
        await self._present(interaction, result)

    @app_commands.command(
        name="verify", description="Show the platform's baseline capability declaration"
    )
    @app_commands.guild_only()
    async def verify(self, interaction: discord.Interaction, account_id: str) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.creator_verify(actor=actor, account_id=account_id)
        await self._present(interaction, result)

    @app_commands.command(name="route", description="Route a creator account's content")
    @app_commands.guild_only()
    async def route(
        self,
        interaction: discord.Interaction,
        account_id: str,
        content_kind: str,
        channel: discord.TextChannel,
        mention_role: discord.Role | None = None,
    ) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        try:
            kind = _content_kind_choice(content_kind)
        except ValueError:
            await interaction.response.send_message("Unrecognized content kind.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        role_id = mention_role.id if mention_role is not None else None
        result = await self._service.creator_route(
            actor=actor,
            account_id=account_id,
            content_kind=kind,
            channel_id=channel.id,
            mention_role_id=role_id,
        )
        await self._present(
            interaction,
            result,
            confirm=lambda: self._service.creator_route(
                actor=actor,
                account_id=account_id,
                content_kind=kind,
                channel_id=channel.id,
                mention_role_id=role_id,
                confirm=True,
            ),
        )

    @app_commands.command(
        name="transfer", description="Transfer a creator account to a new owner"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def transfer(
        self, interaction: discord.Interaction, account_id: str, new_owner: discord.Member
    ) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.creator_transfer(
            actor=actor, account_id=account_id, new_owner_member_id=new_owner.id
        )
        await self._present(
            interaction,
            result,
            confirm=lambda: self._service.creator_transfer(
                actor=actor,
                account_id=account_id,
                new_owner_member_id=new_owner.id,
                confirm=True,
            ),
        )

    @app_commands.command(name="template", description="Validate proposed template text")
    @app_commands.guild_only()
    async def template(
        self,
        interaction: discord.Interaction,
        headline: str,
        footer: str,
        accent_color: int = 0x8B5CF6,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.creator_template(
            headline=headline, footer=footer, accent_color=accent_color
        )
        await self._present(interaction, result)

    async def _present(
        self,
        interaction: discord.Interaction,
        result: CommandResult,
        *,
        confirm: Callable[[], Awaitable[CommandResult]] | None = None,
    ) -> None:
        await _present_result(interaction, result, confirm=confirm)


class NotificationCommands(app_commands.Group):
    """`/fetch notifications` — status, preview, retry, and retract."""

    def __init__(self, parent: FetchCommands, service: ContentCommandService) -> None:
        super().__init__(name="notifications", description="Notification delivery controls")
        self._parent = parent
        self._service = service

    @app_commands.command(name="status", description="Fetch guild-wide notification status")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.notification_status(actor=actor)
        await interaction.followup.send(embed=render_card(result.card), ephemeral=True)  # type: ignore[arg-type]

    @app_commands.command(name="preview", description="Preview a notification card privately")
    @app_commands.guild_only()
    async def preview(
        self, interaction: discord.Interaction, account_id: str, content_kind: str
    ) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None:
            return
        try:
            kind = _content_kind_choice(content_kind)
        except ValueError:
            await interaction.response.send_message("Unrecognized content kind.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.notification_preview(
            actor=actor, account_id=account_id, content_kind=kind
        )
        if result.rendered is not None:
            rendered = result.rendered
            await interaction.followup.send(
                content=rendered.content,
                embed=rendered.embed,
                view=rendered.build_view(),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            content=f"{result.status.value}: {result.detail}", ephemeral=True
        )

    @app_commands.command(name="retry", description="Retry a failed delivery")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def retry(self, interaction: discord.Interaction, delivery_id: str) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None or interaction.guild is None:
            return
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.notification_retry(
            actor=actor, delivery_id=delivery_id, guild=guild
        )
        await self._present(
            interaction,
            result,
            confirm=lambda: self._service.notification_retry(
                actor=actor, delivery_id=delivery_id, guild=guild, confirm=True
            ),
        )

    @app_commands.command(name="retract", description="Retract a delivered announcement")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def retract(self, interaction: discord.Interaction, delivery_id: str) -> None:
        actor = await _actor_context(self._service, interaction)
        if actor is None or interaction.guild is None:
            return
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._service.notification_retract(
            actor=actor, delivery_id=delivery_id, guild=guild
        )
        await self._present(
            interaction,
            result,
            confirm=lambda: self._service.notification_retract(
                actor=actor, delivery_id=delivery_id, guild=guild, confirm=True
            ),
        )

    async def _present(
        self,
        interaction: discord.Interaction,
        result: CommandResult,
        *,
        confirm: Callable[[], Awaitable[CommandResult]] | None = None,
    ) -> None:
        await _present_result(interaction, result, confirm=confirm)
