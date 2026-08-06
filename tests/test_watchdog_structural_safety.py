"""The load-bearing structural proof for the Phase 3 Completion Gate.

The design doc's Completion Gate requires that "Krubit cannot execute an unapproved
moderation action" be verified *structurally*, not just by behavioral test coverage
(`docs/superpowers/specs/2026-08-05-phase-3-watchdog-design.md`, "Testing and
Rollout": "the absence of that capability should be structurally enforced ... not
just behaviorally untested"). This test is that structural proof: it scans the
source text of every Watchdog module for a call to a Discord moderation-mutation
client method. It does not care whether such a call would ever actually execute --
even a call that is unreachable, commented out as code (not prose), or behind a
dead branch is forbidden, because the guarantee this test exists to make is about
what the codebase is *capable* of calling, not what today's control flow happens to
reach.

If this test ever fails, STOP. Do not add documentation, do not adjust the
assertion, do not narrow the `forbidden` set -- remove the offending call. This is
the single most important check in the whole Phase 3 Watchdog effort.

## What "every Watchdog module" actually means (read before trusting the glob alone)

A first version of this test scanned only `glob("src/krubit/**/watchdog*.py")`. That
glob filters by *filename*, not by "is this Watchdog code" -- it matched exactly five
files (`discord/watchdog_commands.py`, `discord/watchdog_events.py`,
`discord/watchdog_runtime.py`, `domain/watchdog.py`, `storage/watchdog_rows.py`) and
silently missed every Task 3-6 Watchdog *service* module, none of which happen to have
"watchdog" in their filename: `services/entry_sniff.py` (Entry Sniff join
assessment), `services/watch_window.py` (the module that reads live message content
during a watch window), `services/raid_detection.py` (`RaidDetector`,
`SpamWaveDetector`), `services/webhook_and_permission_risk.py` (`WebhookAbuseDetector`,
`PermissionRiskDetector`), and `services/incident_evidence.py` (evidence-packet
construction and AutoMod correlation). Those five are exactly the modules with the
most direct access to `discord.Member`/`discord.Guild`-shaped data, and so are exactly
where a future change would most plausibly introduce a moderation-mutation call --
leaving them out of "the single most important check in the whole Phase 3 Watchdog
effort" defeated the point of having it.

This test now scans the union of the filename glob (so a future `watchdog*.py` module
is covered automatically) and an explicit, maintained list of every non-`watchdog*`-
named module that implements Watchdog detection/evidence/runtime logic. **The explicit
list is not self-maintaining**: a future task that adds a new Watchdog service module
without a `watchdog`-prefixed filename must add it to `_EXPLICIT_WATCHDOG_MODULES`
below, or this test will silently fail to cover it, exactly as it silently failed to
cover the five modules above. `test_explicit_watchdog_modules_still_exist` guards
against the list going stale by asserting every entry is still a real file.
"""

from __future__ import annotations

from glob import glob
from pathlib import Path

# Every Task 3-6 Watchdog service module whose filename does not start with
# "watchdog" -- see the module docstring's "What 'every Watchdog module' actually
# means" section for why each one belongs here.
_EXPLICIT_WATCHDOG_MODULES = (
    "src/krubit/services/entry_sniff.py",
    "src/krubit/services/watch_window.py",
    "src/krubit/services/raid_detection.py",
    "src/krubit/services/webhook_and_permission_risk.py",
    "src/krubit/services/incident_evidence.py",
)


def _watchdog_modules() -> list[str]:
    matches = set(glob("src/krubit/**/watchdog*.py", recursive=True))
    matches.update(_EXPLICIT_WATCHDOG_MODULES)
    return sorted(matches)


def test_explicit_watchdog_modules_still_exist() -> None:
    """Guard `_EXPLICIT_WATCHDOG_MODULES` against silently going stale on a rename."""
    for module_path in _EXPLICIT_WATCHDOG_MODULES:
        assert Path(module_path).is_file(), (
            f"{module_path} no longer exists -- update _EXPLICIT_WATCHDOG_MODULES in "
            "this test file (do not just delete the entry; confirm where the module's "
            "logic moved to and add its new path instead)"
        )


def test_no_watchdog_module_imports_a_moderation_mutation_client_method() -> None:
    # Widened per the final whole-branch review (Critical #2): the original five-name
    # set under-covered the design doc's own stated scope. `add_roles` is the most
    # plausible future footgun (auto-assigning a quarantine/unverified role to a
    # SUSPICIOUS member) and was not checked at all; `.timeout(` alone does not cover
    # discord.py's canonical `member.edit(timed_out_until=...)` timeout API, nor
    # `channel.edit(...)`/`channel.set_permissions(...)` channel mutation; `message.
    # delete()`/`.purge(` (the common message-deletion forms) were not covered either,
    # only bulk `delete_messages`. `unban` closes the matching gap on the ban side.
    # Confirmed via grep across all currently-scanned modules that none contains any
    # of the newly added names, so this widening passes today at zero cost.
    forbidden = {
        "kick",
        "ban",
        "unban",
        "timeout",
        "delete_messages",
        "remove_roles",
        "add_roles",
        "edit",
        "delete",
        "purge",
        "set_permissions",
    }
    modules = _watchdog_modules()
    assert modules, "expected at least one Watchdog module to exist and be scanned"
    for module_path in modules:
        source = Path(module_path).read_text(encoding="utf-8")
        for name in forbidden:
            assert f".{name}(" not in source, f"{module_path} calls forbidden {name}()"
