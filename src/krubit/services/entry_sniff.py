"""Entry Sniff join-time orchestration: signals -> risk band -> durable assessment.

`EntrySniffService.assess_join` is the first service-layer consumer of Task 2's
watchdog storage methods. Per the design doc's Product Decisions, "Entry Sniff runs
exactly once per member join, producing one durable, versioned assessment record" —
this service does exactly that and nothing more: it does not open a watch window
(`krubit.services.watch_window.WatchWindowService`, Task 4, owns that, kept as a
separate service so each service has one responsibility per the plan's file-structure
discipline) and it never mutates a member, role, or message.

## Allow/block-list handling (safety-sensitive — read before changing)

The design doc names the guild's own allow/block lists as an Entry Sniff input: "a
member on an explicit block list should be a strong, clearly-named signal; a member on
an allow list should suppress/override other signals, since staff have explicitly
vouched for them." Both halves are implemented here, not in the pure
`extract_join_signals` (which never touches storage):

- **Block-listed**: a `guild_block_list` signal is appended at the maximum weight (10)
  and maximum confidence (1.0) — an explicit staff block is a certain fact, not an
  inferred pattern, and its effective weight (10.0) alone clears
  `_INCIDENT_THRESHOLD` (6.0) regardless of what else fired.
- **Allow-listed**: `krubit.domain.watchdog.evaluate_risk_band`'s own docstring is
  explicit that `RiskBand.CLEAR` is reserved *exclusively* for the empty-signals case
  — there is no way to reach `CLEAR` through that function with any non-empty signals
  tuple, however weak. Honoring "suppress ... other signals" therefore means this
  service does not call `evaluate_risk_band` at all for an allow-listed member; it
  assigns `RiskBand.CLEAR` directly. The raw signals `extract_join_signals` computed
  are still persisted on the assessment (never discarded) so a later staff review of
  the allow-list decision itself has the same explainable evidence any other
  assessment does — only the *band* reflects the staff override, matching "never a
  black-box score" from the design doc's Evidence Packets section. The explanation
  states the override plainly rather than reusing `evaluate_risk_band`'s generated
  text, which would otherwise misleadingly read "no signals observed."

## Known limitation: `join_velocity`/`join_cluster_similarity` depend on the live
## gateway member cache, not a durable store

`_recent_join_fingerprints` builds `recent_joins` entirely from `discord.Guild.
members` — discord.py's in-memory gateway cache — with no durable fallback. This is
the *only* input to `join_velocity` and `join_cluster_similarity`, two of the three
signals in `watchdog_events.py` allowed to clear `SUSPICIOUS` alone, and they are the
design doc's named raid-detection mechanism ("Raid: join-velocity spike correlated
with join-cluster similarity"). The gap this creates is real and lands at the worst
possible moment: in the seconds-to-tens-of-seconds window immediately after a bot
process reconnects or restarts, `guild.members` is still repopulating from Discord's
member-chunk gateway responses, so a raid landing in exactly that window is undercounted
or missed by these two signals — precisely the highest-value moment for an attacker
to strike, and precisely when this signal pair is weakest. Every other join signal in
`extract_join_signals` is unaffected (they only read the joining member's own fields).
This is an accepted interim scope decision for this task, not an oversight: durable,
cross-restart join-history storage (a new table, or reconstructable data attached to
`entry_sniff_assessments`) is plausibly later Phase 3 work once raid detection proper
is built; today, `entry_sniff_assessments` only stores redacted `signals_json`/
`explanation`, not the raw `created_at`/`avatar` facts needed to reconstruct comparable
`recent_joins` entries from storage. If raid detection during the first minutes after a
restart proves too weak in practice, revisit this before or alongside that raid-
detection work.

## Known scope-narrowing: invite code and `MemberFlags` are not read here

The design doc's Entry Sniff Assessment section names "invite code used where Discord
exposes it" and "`MemberFlags` for Rules Screening state" as candidate inputs.
`extract_join_signals`'s brief-specified signature (`member, recent_joins, now`) omits
an invite parameter entirely, and this module only reads `Member.pending` for Rules
Screening (not the broader `MemberFlags` bitfield) — both are intentional narrowing to
match this task's brief, not omissions. Adding invite-source correlation would require
threading Discord's invite-tracking API/audit-log data through a new parameter, and
`MemberFlags` fields beyond `pending` (e.g. `completed_onboarding`) would be marginal
corroboration on top of what `pending` already captures; either is reasonable future
work, not required by this task.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import discord

from krubit.discord.watchdog_events import JoinFingerprint, extract_join_signals
from krubit.domain.models import JSONValue
from krubit.domain.watchdog import EntrySniffAssessment, RiskBand, RiskSignal, evaluate_risk_band
from krubit.storage.sqlite import SQLiteStore

_JOIN_VELOCITY_WINDOW = timedelta(minutes=5)
_BLOCK_LIST_SIGNAL_WEIGHT = 10
_BLOCK_LIST_SIGNAL_CONFIDENCE = 1.0


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


class EntrySniffService:
    """Own the one-assessment-per-join workflow while storage stays dumb persistence."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def assess_join(self, member: discord.Member, now: datetime) -> EntrySniffAssessment:
        """Extract signals, evaluate the risk band, and durably record one assessment.

        Called exactly once per `on_member_join`; a rejoin after leave calls this again
        and produces an independent assessment keyed on the new `joined_at`, never an
        update to a prior one — matching `EntrySniffAssessment`'s documented identity.
        """
        _require_aware("now", now)
        guild_id = member.guild.id
        joined_at = member.joined_at if member.joined_at is not None else now
        _require_aware("member.joined_at", joined_at)

        recent_joins = self._recent_join_fingerprints(member, now=now)
        base_signals = extract_join_signals(member, recent_joins, now)

        band, explanation, signals = await self._apply_allow_block_lists(
            guild_id, member.id, base_signals
        )

        assessment = EntrySniffAssessment(
            guild_id=guild_id,
            member_id=member.id,
            joined_at=joined_at,
            band=band,
            signals=signals,
            explanation=explanation,
            created_at=now,
        )
        saved = await self._store.save_entry_sniff_assessment(assessment)
        await self._record_receipt(saved, now=now)
        return saved

    @staticmethod
    def _recent_join_fingerprints(
        member: discord.Member, *, now: datetime
    ) -> tuple[JoinFingerprint, ...]:
        """Snapshot other guild members who joined within the join-velocity window.

        Reads only `discord.Guild.members`, the bot's own already-cached member list —
        no new persistence, no network call. A member whose `joined_at` is unknown
        (not yet cached by the gateway) is conservatively excluded rather than guessed.
        """
        fingerprints: list[JoinFingerprint] = []
        for other in member.guild.members:
            if other.id == member.id:
                continue
            other_joined_at = other.joined_at
            if other_joined_at is None:
                continue
            if now - other_joined_at > _JOIN_VELOCITY_WINDOW:
                continue
            fingerprints.append(JoinFingerprint.from_member(other))
        return tuple(fingerprints)

    async def _apply_allow_block_lists(
        self, guild_id: int, member_id: int, base_signals: tuple[RiskSignal, ...]
    ) -> tuple[RiskBand, str, tuple[RiskSignal, ...]]:
        entries = await self._store.list_allow_block_entries(guild_id)
        entry = next((row for row in entries if row.discord_user_id == member_id), None)

        if entry is not None and entry.list_kind == "block":
            block_signal = RiskSignal(
                name="guild_block_list",
                weight=_BLOCK_LIST_SIGNAL_WEIGHT,
                detail=f"member is on this guild's block list: {entry.reason}",
                confidence=_BLOCK_LIST_SIGNAL_CONFIDENCE,
            )
            signals = (*base_signals, block_signal)
            band, explanation = evaluate_risk_band(signals)
            return band, explanation, signals

        if entry is not None and entry.list_kind == "allow":
            if base_signals:
                names = ", ".join(signal.name for signal in base_signals)
                explanation = (
                    f"clear band: member is on this guild's allow list ({entry.reason}); "
                    f"{len(base_signals)} underlying signal(s) suppressed by staff vouch: {names}"
                )
            else:
                explanation = (
                    f"clear band: member is on this guild's allow list ({entry.reason}); "
                    "no signals were observed"
                )
            return RiskBand.CLEAR, explanation, base_signals

        band, explanation = evaluate_risk_band(base_signals)
        return band, explanation, base_signals

    async def _record_receipt(self, assessment: EntrySniffAssessment, *, now: datetime) -> None:
        signal_names: list[JSONValue] = [signal.name for signal in assessment.signals]
        detail: dict[str, JSONValue] = {
            "band": assessment.band.value,
            "signal_names": signal_names,
        }
        await self._store.record_sniff_receipt(
            guild_id=assessment.guild_id,
            receipt_id=f"entry-sniff:{uuid4().hex}",
            member_id=assessment.member_id,
            action="entry_sniff_assessment_recorded",
            detail=detail,
            created_at=now,
        )
