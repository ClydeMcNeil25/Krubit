"""Staff-only `/fetch sniff`, `/fetch sniff-report`, `/fetch incident`, `/fetch
evidence`, and `/fetch watchlist` command surfaces.

`WatchdogCommandService` is the framework-independent core, matching `Content
CommandService`'s (`krubit.discord.content_commands`) own division of labor: every
method takes a plain `WatchdogActorContext` (never a `discord.Interaction`) so
authority and safety properties are directly unit-testable without any
`discord.Interaction` mocking. `CommandStatus`/`CommandResult` are reused from
`content_commands` rather than redefined here, so a test can assert
`result.status is CommandStatus.DENIED` regardless of which command module produced
the result.

Every method checks `actor.is_staff` as its first statement, before any storage
query — a non-staff actor never causes a single read against `SQLiteStore`, matching
`live_commands.py`/`content_commands.py`'s existing "denied before any query or
preview" convention (a Phase 2 review specifically found and fixed a command that
skipped this ordering).

## Read-only surface (safety-sensitive)

None of these five commands mutate anything — Phase 3 Watchdog carries zero
autonomous moderation authority (see `krubit.domain.watchdog`'s module docstring),
and this command surface is purely evidentiary. `sniff` and `sniff_report` read
`entry_sniff_assessments`/`watch_windows`; `incident` and `evidence` read `incidents`
plus the `sniff_receipts` row an incident-recording detector wrote alongside it;
`watchlist` reads `watch_windows` and `guild_allow_block_lists`.

## Why `incident`/`evidence` reconstruct signals instead of reading a stored packet

No Task 1-7 storage table persists a full `EvidencePacket` (its `signals`/
`message_links`/`event_ids`) — `Incident.evidence_packet_id` is only an opaque
identifier (see `krubit.services.raid_detection._default_evidence_builder` and its
siblings in `webhook_and_permission_risk.py`); wiring a real evidence-packet store is
explicitly out of scope for Tasks 1-7 (see `krubit.services.incident_evidence`'s
module docstring: "this task does not change those detectors"). The one durable
trace of *which* signals contributed is the `incident_recorded` `SniffReceipt` every
incident-producing detector writes (`receipt_id=f"incident:{incident.incident_id}"`,
`detail["signal_names"]`) — already redacted by `record_sniff_receipt` before it ever
reached storage. `_reconstruct_signals` reads that receipt and rebuilds one
placeholder `RiskSignal` per named signal, with a fixed, code-authored `detail`
string (never a pass-through of raw stored data) so `RiskSignal.__post_init__`'s
unconditional redaction has nothing surprising to catch. Those placeholders are then
handed to `krubit.services.incident_evidence.build_evidence_packet` — the same
Task 6 builder every detector was written against — rather than this module rolling
its own evidence-assembly or redaction path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from krubit.discord.content_commands import CommandResult, CommandStatus
from krubit.domain.models import Card, CardField
from krubit.domain.watchdog import EvidencePacket, Incident, RiskBand, RiskSignal
from krubit.services.incident_evidence import build_evidence_packet

if TYPE_CHECKING:
    from krubit.storage.sqlite import SQLiteStore

_SNIFF_REPORT_WINDOW = timedelta(hours=24)
_INCIDENT_RECEIPT_SCAN_LIMIT = 500
_PLACEHOLDER_SIGNAL_WEIGHT = 1
_PLACEHOLDER_SIGNAL_CONFIDENCE = 1.0
_HIGH_BANDS = (RiskBand.SUSPICIOUS, RiskBand.INCIDENT)

# `_reconstruct_signals` below rebuilds one `RiskSignal` per recovered signal *name*
# only -- the detector's real weight, confidence, detail text, message links, and
# event IDs are not durably stored anywhere Task 1-7 wrote to (see the module
# docstring's "Why incident/evidence reconstruct signals" section). Neither
# `incident()` nor `evidence()` may ever render `_PLACEHOLDER_SIGNAL_WEIGHT`/
# `_PLACEHOLDER_SIGNAL_CONFIDENCE`/an empty `message_links`/`event_ids` as if they
# were the detector's genuine output -- a uniform, hardcoded confidence is exactly
# the "opaque risk score with no explanation" the design doc prohibits. Both
# commands surface this notice instead of any of those fabricated fields.
_RECONSTRUCTION_NOTICE = (
    "Note: full evidence detail (per-signal weight, confidence, detail text, "
    "message links, and event IDs) was not persisted for this incident -- only "
    "the contributing signal names are recoverable. Nothing below is a genuine "
    "detector-computed confidence or message-link count."
)


@dataclass(frozen=True, slots=True)
class WatchdogActorContext:
    """The plain facts a watchdog command needs about the member invoking it (or,
    for `sniff`'s `target`, the member being looked up). Deliberately framework-
    independent, matching `content_commands.ActorContext`. `is_staff` is resolved by
    the Discord-layer command wrapper (Manage Guild, matching every other staff-only
    `/fetch` command's gate) before this service is ever called."""

    guild_id: int
    member_id: int
    is_staff: bool = False


def _denied() -> CommandResult:
    return CommandResult(
        CommandStatus.DENIED, detail={"reason": "staff authority required"}
    )


class WatchdogCommandService:
    """The framework-independent core of every `/fetch sniff|sniff-report|incident|
    evidence|watchlist` command."""

    def __init__(
        self, store: SQLiteStore, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))

    # -- sniff: one member's current/most recent assessment --------------------

    async def sniff(
        self, *, actor: WatchdogActorContext, target: WatchdogActorContext
    ) -> CommandResult:
        if not actor.is_staff:
            return _denied()
        assessment = await self._store.get_entry_sniff_assessment(
            actor.guild_id, target.member_id
        )
        if assessment is None:
            return CommandResult(
                CommandStatus.FAILED, detail={"reason": "no_assessment_found"}
            )
        card = Card(
            kind="fetched",
            title=f"Fetched: Entry Sniff <@{target.member_id}>",
            description=assessment.explanation,
            fields=(
                CardField("Band", assessment.band.value, True),
                CardField("Joined", assessment.joined_at.isoformat(), True),
                CardField("Assessed", assessment.created_at.isoformat(), True),
                CardField(
                    "Contributing signals",
                    ", ".join(signal.name for signal in assessment.signals) or "None",
                    False,
                ),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"band": assessment.band.value, "member_id": target.member_id},
        )

    # -- sniff-report: guild-wide open windows + recent high-band assessments ---

    async def sniff_report(self, *, actor: WatchdogActorContext) -> CommandResult:
        """Surface open watch windows AND recent `SUSPICIOUS`/`INCIDENT` assessments.

        Real-time INCIDENT notification for a lone member's join assessment does not
        exist yet (only sweep-cycle detectors notify staff proactively — see
        `WatchdogRuntime.on_member_join`'s docstring), which makes this the primary
        way staff discover a risky join that never escalated into a guild-scoped
        incident. Recent high-band assessments are therefore listed prominently and
        first, not buried after the open-watch-window list, and are never limited to
        currently-open windows: a member whose window already expired is still worth
        surfacing here within the lookback window.
        """
        if not actor.is_staff:
            return _denied()
        now = self._now()
        windows = await self._store.list_open_watch_windows(actor.guild_id)
        recent = await self._store.list_recent_entry_sniff_assessments(
            actor.guild_id, since=now - _SNIFF_REPORT_WINDOW, until=now
        )
        high_band = tuple(a for a in recent if a.band in _HIGH_BANDS)
        high_band_sorted = tuple(
            sorted(
                high_band,
                key=lambda a: (a.band is not RiskBand.INCIDENT, a.joined_at),
            )
        )
        high_band_lines = "\n".join(
            f"<@{a.member_id}> — **{a.band.value}** (joined {a.joined_at.isoformat()})"
            for a in high_band_sorted
        ) or "None in the lookback window."
        window_lines = "\n".join(
            f"<@{w.member_id}> — **{w.band.value}** (expires {w.expires_at.isoformat()})"
            for w in windows
        ) or "None currently open."
        lookback_hours = int(_SNIFF_REPORT_WINDOW.total_seconds() // 3600)
        card = Card(
            kind="fetched",
            title="Fetched: Watchdog Sniff Report",
            description=(
                f"Recent high-band joins (last {lookback_hours}h):\n"
                f"{high_band_lines}\n\n"
                f"Open watch windows:\n{window_lines}"
            ),
            fields=(
                CardField("High-band joins", str(len(high_band_sorted)), True),
                CardField("Open watch windows", str(len(windows)), True),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={
                "high_band_count": len(high_band_sorted),
                "open_watch_window_count": len(windows),
            },
        )

    # -- incident / evidence: shared reconstruction ------------------------------

    async def _reconstruct_signals(self, incident: Incident) -> tuple[RiskSignal, ...]:
        """Rebuild the signals that contributed to `incident` from its
        `incident_recorded` `SniffReceipt`. See the module docstring's "Why
        incident/evidence reconstruct signals" section."""
        receipts = await self._store.list_sniff_receipts(
            incident.guild_id, member_id=None, limit=_INCIDENT_RECEIPT_SCAN_LIMIT
        )
        receipt_id = f"incident:{incident.incident_id}"
        receipt = next((r for r in receipts if r.receipt_id == receipt_id), None)
        names: list[str] = []
        if receipt is not None:
            raw_names = receipt.detail.get("signal_names")
            if isinstance(raw_names, list):
                names = [str(name) for name in raw_names if isinstance(name, str)]
        if not names:
            names = [f"{incident.kind.value}_incident"]
        return tuple(
            RiskSignal(
                name=name,
                weight=_PLACEHOLDER_SIGNAL_WEIGHT,
                detail=f"contributing signal recorded for incident {incident.incident_id}",
                confidence=_PLACEHOLDER_SIGNAL_CONFIDENCE,
            )
            for name in names
        )

    async def _evidence_packet_for(self, incident: Incident) -> EvidencePacket:
        signals = await self._reconstruct_signals(incident)
        return build_evidence_packet(incident, signals, ())

    async def _get_incident_or_fail(
        self, actor: WatchdogActorContext, incident_id: str
    ) -> Incident | CommandResult:
        incident = await self._store.get_incident(actor.guild_id, incident_id)
        if incident is None:
            return CommandResult(
                CommandStatus.FAILED, detail={"reason": "incident_not_found"}
            )
        return incident

    async def incident(
        self, *, actor: WatchdogActorContext, incident_id: str
    ) -> CommandResult:
        if not actor.is_staff:
            return _denied()
        found = await self._get_incident_or_fail(actor, incident_id)
        if isinstance(found, CommandResult):
            return found
        record = found
        packet = await self._evidence_packet_for(record)
        card = Card(
            kind="fetched",
            title=f"Fetched: Incident {record.incident_id}",
            description=(
                f"Kind: **{record.kind.value}**\n"
                f"Band: **{record.band.value}**\n"
                f"Opened: {record.opened_at.isoformat()}\n\n"
                f"{_RECONSTRUCTION_NOTICE}"
            ),
            fields=(
                CardField("Recommended action", record.recommended_action, False),
                CardField(
                    "Acknowledged by",
                    f"<@{record.acknowledged_by}>"
                    if record.acknowledged_by is not None
                    else "Not yet acknowledged",
                    True,
                ),
                CardField(
                    "Contributing signals (names only)",
                    ", ".join(signal.name for signal in packet.signals),
                    False,
                ),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"incident_id": record.incident_id, "kind": record.kind.value},
        )

    async def evidence(
        self, *, actor: WatchdogActorContext, incident_id: str
    ) -> CommandResult:
        """Render `incident_id`'s raw, redacted evidence export.

        Deliberately does NOT dump `EvidencePacket.to_storage_dict()` wholesale:
        that representation includes `_reconstruct_signals`'s placeholder
        `weight`/`confidence`/`detail`, and an always-empty `message_links`/
        `event_ids` (see the module-level `_RECONSTRUCTION_NOTICE` docstring for
        why those are never the detector's genuine output). Only the genuinely
        recoverable fields — `incident_id`, `guild_id`, `created_at`, and the
        recovered signal *names* — are rendered, each still passed through the
        same redacted `EvidencePacket`/`RiskSignal` construction path
        (`build_evidence_packet`) `incident()` uses, so this is still "reuse the
        Task 6 builder's redaction," just a narrower render of its output.
        """
        if not actor.is_staff:
            return _denied()
        found = await self._get_incident_or_fail(actor, incident_id)
        if isinstance(found, CommandResult):
            return found
        record = found
        packet = await self._evidence_packet_for(record)
        signal_names = ", ".join(signal.name for signal in packet.signals)
        lines = [
            _RECONSTRUCTION_NOTICE,
            "",
            f"incident_id: {packet.incident_id}",
            f"guild_id: {packet.guild_id}",
            f"created_at: {packet.created_at.isoformat()}",
            f"signal_names: {signal_names}",
            "message_links: not recoverable (not persisted for this incident)",
            "event_ids: not recoverable (not persisted for this incident)",
        ]
        card = Card(
            kind="fetched",
            title=f"Fetched: Evidence Export {record.incident_id}",
            description="\n".join(lines),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"incident_id": record.incident_id},
        )

    # -- watchlist: open windows + allow/block lists ----------------------------

    async def watchlist(self, *, actor: WatchdogActorContext) -> CommandResult:
        if not actor.is_staff:
            return _denied()
        windows = await self._store.list_open_watch_windows(actor.guild_id)
        allow_entries = await self._store.list_allow_block_entries(actor.guild_id, "allow")
        block_entries = await self._store.list_allow_block_entries(actor.guild_id, "block")
        window_lines = "\n".join(
            f"<@{w.member_id}> — **{w.band.value}** (expires {w.expires_at.isoformat()})"
            for w in windows
        ) or "None currently open."
        allow_lines = "\n".join(
            f"<@{e.discord_user_id}> — {e.reason}" for e in allow_entries
        ) or "None configured."
        block_lines = "\n".join(
            f"<@{e.discord_user_id}> — {e.reason}" for e in block_entries
        ) or "None configured."
        card = Card(
            kind="fetched",
            title="Fetched: Watchdog Watchlist",
            description=(
                f"Open watch windows:\n{window_lines}\n\n"
                f"Allow list:\n{allow_lines}\n\n"
                f"Block list:\n{block_lines}"
            ),
            fields=(
                CardField("Open watch windows", str(len(windows)), True),
                CardField("Allow list entries", str(len(allow_entries)), True),
                CardField("Block list entries", str(len(block_entries)), True),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={
                "open_watch_window_count": len(windows),
                "allow_count": len(allow_entries),
                "block_count": len(block_entries),
            },
        )
