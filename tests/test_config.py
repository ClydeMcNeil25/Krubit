from pathlib import Path

import pytest

from krubit.config import Settings, SettingsError


def base_env() -> dict[str, str]:
    return {
        "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
        "KRUBIT_DATABASE_PATH": "state/test.db",
    }


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


def test_missing_social_credentials_do_not_prevent_bot_startup() -> None:
    settings = Settings.from_env(base_env())
    assert settings.youtube_api_key is None
    assert settings.x_bearer_token is None
    assert settings.meta_app_secret is None
    assert settings.tiktok_client_secret is None


def test_missing_social_settings_all_default_to_none_or_disabled() -> None:
    settings = Settings.from_env(base_env())
    assert settings.youtube_push_callback_secret is None
    assert settings.meta_app_id is None
    assert settings.meta_callback_base_url is None
    assert settings.tiktok_client_key is None
    assert settings.tiktok_callback_base_url is None
    assert settings.creator_signals_enabled is False
    assert settings.social_delivery_enabled is False
    assert settings.credential_encryption_key is None
    assert settings.callback_public_base_url is None
    assert settings.callback_port is None


def test_settings_parse_configured_social_and_callback_settings() -> None:
    settings = Settings.from_env(
        {
            **base_env(),
            "YOUTUBE_KRUBIT_API_KEY": "youtube-api-key",
            "YOUTUBE_KRUBIT_PUSH_CALLBACK_SECRET": "youtube-push-secret",
            "X_KRUBIT_BEARER_TOKEN": "x-bearer-token",
            "META_KRUBIT_APP_ID": "meta-app-id",
            "META_KRUBIT_APP_SECRET": "meta-app-secret",
            "META_KRUBIT_CALLBACK_BASE_URL": "https://callbacks.example.com/meta",
            "TIKTOK_KRUBIT_CLIENT_KEY": "tiktok-client-key",
            "TIKTOK_KRUBIT_CLIENT_SECRET": "tiktok-client-secret",
            "TIKTOK_KRUBIT_CALLBACK_BASE_URL": "https://callbacks.example.com/tiktok",
            "KRUBIT_CREATOR_SIGNALS_ENABLED": "true",
            "KRUBIT_SOCIAL_DELIVERY_ENABLED": "true",
            "KRUBIT_CREDENTIAL_ENCRYPTION_KEY": "a-long-random-encryption-key",
            "KRUBIT_CALLBACK_PUBLIC_BASE_URL": "https://callbacks.example.com",
            "KRUBIT_CALLBACK_PORT": "8443",
        }
    )
    assert settings.youtube_api_key == "youtube-api-key"
    assert settings.youtube_push_callback_secret == "youtube-push-secret"
    assert settings.x_bearer_token == "x-bearer-token"
    assert settings.meta_app_id == "meta-app-id"
    assert settings.meta_app_secret == "meta-app-secret"
    assert settings.meta_callback_base_url == "https://callbacks.example.com/meta"
    assert settings.tiktok_client_key == "tiktok-client-key"
    assert settings.tiktok_client_secret == "tiktok-client-secret"
    assert settings.tiktok_callback_base_url == "https://callbacks.example.com/tiktok"
    assert settings.creator_signals_enabled is True
    assert settings.social_delivery_enabled is True
    assert settings.credential_encryption_key == "a-long-random-encryption-key"
    assert settings.callback_public_base_url == "https://callbacks.example.com"
    assert settings.callback_port == 8443


def test_settings_rejects_invalid_creator_signals_enabled_value() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_CREATOR_SIGNALS_ENABLED"):
        Settings.from_env({**base_env(), "KRUBIT_CREATOR_SIGNALS_ENABLED": "sure"})


def test_settings_rejects_invalid_social_delivery_enabled_value() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_SOCIAL_DELIVERY_ENABLED"):
        Settings.from_env({**base_env(), "KRUBIT_SOCIAL_DELIVERY_ENABLED": "nope"})


def test_settings_rejects_non_https_callback_public_base_url() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_CALLBACK_PUBLIC_BASE_URL"):
        Settings.from_env(
            {**base_env(), "KRUBIT_CALLBACK_PUBLIC_BASE_URL": "http://callbacks.example.com"}
        )


def test_settings_rejects_invalid_callback_port() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_CALLBACK_PORT"):
        Settings.from_env({**base_env(), "KRUBIT_CALLBACK_PORT": "not-a-port"})

    with pytest.raises(SettingsError, match="KRUBIT_CALLBACK_PORT"):
        Settings.from_env({**base_env(), "KRUBIT_CALLBACK_PORT": "70000"})


def test_require_credential_encryption_key_rejects_missing_key() -> None:
    settings = Settings.from_env(base_env())
    with pytest.raises(SettingsError, match="KRUBIT_CREDENTIAL_ENCRYPTION_KEY"):
        settings.require_credential_encryption_key()


def test_require_credential_encryption_key_returns_configured_key() -> None:
    settings = Settings.from_env(
        {**base_env(), "KRUBIT_CREDENTIAL_ENCRYPTION_KEY": "a-long-random-encryption-key"}
    )
    assert settings.require_credential_encryption_key() == "a-long-random-encryption-key"


def test_settings_repr_never_renders_configured_secret_values() -> None:
    secret_values = {
        "DISCORD_KRUBIT_BOT_TOKEN": "bot-token-value",
        "TWITCH_KRUBIT_CLIENT_SECRET": "twitch-secret-value",
        "YOUTUBE_KRUBIT_API_KEY": "youtube-api-key-value",
        "YOUTUBE_KRUBIT_PUSH_CALLBACK_SECRET": "youtube-push-secret-value",
        "X_KRUBIT_BEARER_TOKEN": "x-bearer-token-value",
        "META_KRUBIT_APP_SECRET": "meta-app-secret-value",
        "TIKTOK_KRUBIT_CLIENT_SECRET": "tiktok-client-secret-value",
        "KRUBIT_CREDENTIAL_ENCRYPTION_KEY": "credential-encryption-key-value",
    }
    non_secret_values = {
        "TWITCH_KRUBIT_CLIENT_ID": "twitch-client-id-visible",
        "META_KRUBIT_APP_ID": "meta-app-id-visible",
    }
    settings = Settings.from_env({**base_env(), **secret_values, **non_secret_values})

    rendered = repr(settings)
    assert rendered == str(settings)
    for secret_value in secret_values.values():
        assert secret_value not in rendered

    # Non-secret identifiers remain visible for diagnostics: repr=False is applied
    # selectively to secrets, not blanket-removed from every field.
    for non_secret_value in non_secret_values.values():
        assert non_secret_value in rendered
