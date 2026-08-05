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
"""

from __future__ import annotations

from glob import glob
from pathlib import Path


def test_no_watchdog_module_imports_a_moderation_mutation_client_method() -> None:
    forbidden = {"kick", "ban", "timeout", "delete_messages", "remove_roles"}
    modules = glob("src/krubit/**/watchdog*.py", recursive=True)
    assert modules, "expected at least one watchdog*.py module to exist and be scanned"
    for module_path in modules:
        source = Path(module_path).read_text(encoding="utf-8")
        for name in forbidden:
            assert f".{name}(" not in source, f"{module_path} calls forbidden {name}()"
