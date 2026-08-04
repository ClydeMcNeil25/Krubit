import json
from pathlib import Path

import pytest

from krubit.__main__ import main
from krubit.config import Settings
from krubit.discord.bot import KrubitBot
from krubit.services.foundation import FoundationService
from krubit.storage.sqlite import SQLiteStore


def environment(database_path: Path) -> dict[str, str]:
    return {
        "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
        "KRUBIT_DATABASE_PATH": str(database_path),
    }


def test_cli_initializes_enables_and_reports_guild_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "krubit.db"
    env = environment(database_path)

    assert main(["init-db"], env) == 0
    assert main(["enable-guild", "111"], env) == 0
    assert main(["status", "111"], env) == 0

    output = capsys.readouterr().out.strip().splitlines()
    status = json.loads(output[-1])
    assert status == {
        "database_healthy": True,
        "enabled": True,
        "event_count": 0,
        "guild_id": "111",
        "receipt_count": 1,
    }


def test_cli_prints_install_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["install-url"], environment(tmp_path / "krubit.db")) == 0

    output = capsys.readouterr().out.strip()
    assert output.startswith(
        "https://discord.com/oauth2/authorize?client_id=123456789012345678"
    )


def test_cli_emits_schema_valid_signal_without_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "krubit.db"
    env = environment(database_path)
    main(["init-db"], env)
    main(["enable-guild", "111"], env)

    assert main(["emit-test-signal", "111", "--actor-id", "7"], env) == 0

    output = capsys.readouterr().out.strip().splitlines()
    signal = json.loads(output[-1])
    assert signal["schema_version"] == "krubit.zariya-signal.v1"
    assert signal["guild_id"] == "111"
    assert signal["action_request"] is None


@pytest.mark.asyncio
async def test_bot_registers_only_phase_zero_fetch_commands(tmp_path: Path) -> None:
    settings = Settings.from_env(environment(tmp_path / "krubit.db"))
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(settings, FoundationService(store))

    try:
        fetch = next(command for command in bot.tree.get_commands() if command.name == "fetch")

        assert {command.name for command in fetch.commands} == {"status", "test-card"}  # type: ignore[attr-defined]
    finally:
        await bot.close()
        await store.close()
