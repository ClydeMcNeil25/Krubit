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
    assert settings.staff_channel_id is None


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


def test_settings_parse_optional_staff_channel_id() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
            "KRUBIT_STAFF_CHANNEL_ID": "987654321098765432",
        }
    )

    assert settings.staff_channel_id == 987654321098765432


def test_settings_reject_invalid_staff_channel_id() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_STAFF_CHANNEL_ID"):
        Settings.from_env(
            {
                "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
                "KRUBIT_STAFF_CHANNEL_ID": "staff-chat",
            }
        )


def test_phase_two_settings_parse_twitch_and_default_disabled() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
            "TWITCH_KRUBIT_CLIENT_ID": "client-id",
            "TWITCH_KRUBIT_CLIENT_SECRET": "client-secret",
        }
    )

    assert settings.require_twitch_credentials() == ("client-id", "client-secret")
    assert settings.live_signals_enabled is False


def test_settings_rejects_invalid_live_signals_enabled_value() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_LIVE_SIGNALS_ENABLED"):
        Settings.from_env(
            {
                "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
                "KRUBIT_LIVE_SIGNALS_ENABLED": "yes",
            }
        )
