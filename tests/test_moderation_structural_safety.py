"""The load-bearing structural proof for slice 4's real Discord-mutation code.

Mirrors `tests/test_watchdog_structural_safety.py`'s exact approach for the
Phase 3 Watchdog Completion Gate, applied to this slice's own boundary: the
roadmap's Phase 5 boundary states "Kick and permanent ban remain unavailable
to autonomous execution in this phase regardless of configuration" (see
`krubit.discord.moderation_executor`'s module docstring). This test is that
structural proof: it scans the source text of every moderation-mutation-
capable module for a call to a forbidden Discord moderation-mutation client
method. It does not care whether such a call would ever actually execute --
even a call that is unreachable, commented out as code (not prose), or behind
a dead branch is forbidden, because the guarantee this test exists to make is
about what the codebase is *capable* of calling, not what today's control
flow happens to reach.

If this test ever fails, STOP. Do not add documentation, do not adjust the
assertion, do not narrow the `forbidden` set -- remove the offending call.
"""

from __future__ import annotations

from pathlib import Path

_MODERATION_EXECUTOR = Path("src/krubit/discord/moderation_executor.py")
_MODERATION_NOTIFIER = Path("src/krubit/discord/moderation_notifier.py")

_MODULES = (_MODERATION_EXECUTOR, _MODERATION_NOTIFIER)

# Mirrors `test_watchdog_structural_safety.py`'s forbidden set. `timeout` is
# deliberately NOT in this set -- it is the one expected mutation this slice's
# `DiscordActionExecutor` performs, and is proven present (not absent) by
# `test_moderation_executor_calls_timeout` below instead.
_FORBIDDEN = {
    "kick",
    "ban",
    "unban",
    "delete_messages",
    "purge",
    "set_permissions",
    "add_roles",
    "remove_roles",
}


def test_moderation_modules_still_exist() -> None:
    """Guard the explicit module list against silently going stale on a rename."""
    for module_path in _MODULES:
        assert module_path.is_file(), (
            f"{module_path} no longer exists -- update the module list in this "
            "test file (do not just delete the entry; confirm where the module's "
            "logic moved to and add its new path instead)"
        )


def test_no_moderation_module_calls_a_forbidden_mutation_method() -> None:
    for module_path in _MODULES:
        source = module_path.read_text(encoding="utf-8")
        for name in _FORBIDDEN:
            assert f".{name}(" not in source, f"{module_path} calls forbidden {name}()"


def test_moderation_executor_calls_timeout() -> None:
    """Positive control: proves the scan above isn't passing vacuously by
    scanning a module that calls nothing at all. `.timeout(` is the ONE
    expected Discord-mutation call this slice performs (member timeout, via
    `DiscordActionExecutor.execute`) -- if this assertion ever fails, that
    call was removed and this test would otherwise fail loudly for the wrong
    reason (or, worse, this file would need to be trusted without an
    executable proof it is scanning real, non-empty source).
    """
    source = _MODERATION_EXECUTOR.read_text(encoding="utf-8")
    assert ".timeout(" in source, (
        f"{_MODERATION_EXECUTOR} no longer calls .timeout( -- the structural "
        "scan above would pass vacuously without this positive control"
    )
