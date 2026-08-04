from pathlib import Path

import pytest

from krubit.config import Settings, SettingsError


def test_settings_load_without_requiring_token() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
            "KRUBIT_DATABASE_PATH": "state/test.db",
        }
    )

    assert settings.application_id == 123456789012345678
    assert settings.database_path == Path("state/test.db")
    assert settings.bot_token is None


def test_settings_reject_non_numeric_application_id() -> None:
    with pytest.raises(SettingsError, match="numeric Discord snowflake"):
        Settings.from_env({"DISCORD_KRUBIT_APPLICATION_ID": "not-a-snowflake"})


def test_require_token_rejects_missing_token() -> None:
    settings = Settings.from_env({"DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678"})

    with pytest.raises(SettingsError, match="DISCORD_KRUBIT_BOT_TOKEN"):
        settings.require_token()


def test_require_token_returns_configured_token() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
            "DISCORD_KRUBIT_BOT_TOKEN": "environment-only-token",
        }
    )

    assert settings.require_token() == "environment-only-token"
