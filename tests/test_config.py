import ast
from pathlib import Path

import pytest

from krubit.config import Settings, SettingsError

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "krubit"
_CONFIG_FILE = _SRC_ROOT / "config.py"

# Fields intentionally exempt from `test_every_settings_field_has_a_non_test_reader`
# below, each with a documented reason -- see Critical #2 of the 2026-08-06 Phase 4
# final-review fix report: a `Settings` field with zero readers advertises a control
# that does not exist, and "documented but not read" is not an adequate fix (that
# exact gap is what let `activity_ledger_excluded_channel_ids` go unwired for two
# task-level reviews in a row). Keep this list small, and give every entry a real
# reason -- a silent exemption defeats the entire point of this test.
_UNREAD_FIELD_EXEMPTIONS: dict[str, str] = {
    "bot_token": (
        "read via the `Settings.require_token()` accessor (called from "
        "`__main__.py`), never by direct `.bot_token` attribute access or a "
        "`bot_token=` keyword -- the only two patterns this test's AST scan "
        "recognizes as a 'read'."
    ),
    "credential_encryption_key": (
        "pre-existing (pre-Phase-4) forward declaration for the not-yet-wired "
        "encrypted-credential-storage path; `Settings."
        "require_credential_encryption_key()` exists but has no production caller "
        "yet. Out of scope for the Phase 4 activity ledger -- flagged here instead "
        "of silently passing."
    ),
    "meta_app_id": "pre-existing forward declaration for the not-yet-wired Meta connector.",
    "meta_app_secret": "pre-existing forward declaration for the not-yet-wired Meta connector.",
    "meta_callback_base_url": (
        "pre-existing forward declaration for the not-yet-wired Meta connector."
    ),
    "tiktok_client_key": (
        "pre-existing forward declaration for the not-yet-wired TikTok connector."
    ),
    "tiktok_client_secret": (
        "pre-existing forward declaration for the not-yet-wired TikTok connector."
    ),
    "tiktok_callback_base_url": (
        "pre-existing forward declaration for the not-yet-wired TikTok connector."
    ),
    "youtube_push_callback_secret": (
        "pre-existing forward declaration for a not-yet-built YouTube "
        "push-notification callback verification path -- distinct from "
        "`youtube_api_key`, which IS read (by `__main__.py`)."
    ),
    "callback_public_base_url": (
        "pre-existing forward declaration for the not-yet-built generic OAuth "
        "callback HTTP server that would host it."
    ),
    "callback_port": (
        "pre-existing forward declaration for the not-yet-built generic OAuth "
        "callback HTTP server that would bind it."
    ),
    "watchdog_zariya_bridge_url": (
        "pre-existing forward declaration for a not-yet-built Watchdog-to-Zariya "
        "escalation bridge integration."
    ),
}


def _identifiers_referenced(tree: ast.AST) -> set[str]:
    """Every attribute name and keyword-argument name referenced anywhere in
    `tree`. Deliberately does NOT match plain identifiers/strings, so a field name
    that only appears inside a comment or a docstring sentence -- exactly how
    `activity_ledger_excluded_channel_ids` looked "documented" before it was
    actually wired -- does not count as a reference here.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            names.add(node.arg)
    return names


def test_every_settings_field_has_a_non_test_reader() -> None:
    """Standing safeguard for the "Settings field parsed but never read" bug class.

    This is the THIRD confirmed instance of this bug class in this codebase's
    history (per the 2026-08-06 Phase 4 whole-branch final review): Phase 2's and
    Phase 3's final reviews each caught one, and Phase 4 had two of its own --
    `activity_ledger_retention_days` was caught and fixed at the task level, but
    `activity_ledger_excluded_channel_ids` was only caught and *documented*, not
    wired, until this final review. This test exists so a fourth instance fails
    CI instead of shipping.

    A field counts as "read" if its name appears as an attribute access
    (`settings.field_name` / `self._settings.field_name`) or a keyword argument
    anywhere under `src/krubit`, outside `config.py` itself -- this deliberately
    does not attempt real control-flow analysis (that would need to prove the read
    is actually reachable at runtime); it only proves *some* production code
    references the field by name in a way a plain grep would not have caught
    reliably (see `_identifiers_referenced`'s docstring for why AST attribute/
    keyword matching is used instead of a text search).
    """
    referenced: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        if path == _CONFIG_FILE or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        referenced |= _identifiers_referenced(tree)

    unread = sorted(
        name
        for name in Settings.__dataclass_fields__
        if name not in referenced and name not in _UNREAD_FIELD_EXEMPTIONS
    )
    assert not unread, (
        f"Settings field(s) with zero non-test readers and no documented "
        f"exemption: {unread}. Either wire the field into production code or add "
        f"a reasoned entry to _UNREAD_FIELD_EXEMPTIONS in this file."
    )

    # Every exemption must name a field that still exists, so a renamed or removed
    # field doesn't leave a stale, meaningless exemption behind.
    stale_exemptions = sorted(set(_UNREAD_FIELD_EXEMPTIONS) - set(Settings.__dataclass_fields__))
    assert not stale_exemptions, f"stale exemption(s) for nonexistent fields: {stale_exemptions}"


def base_env() -> dict[str, str]:
    return {
        "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
        "KRUBIT_DATABASE_PATH": "state/test.db",
    }


def test_watchdog_settings_default_disabled() -> None:
    settings = Settings.from_env(base_env())
    assert settings.watchdog_enabled is False
    assert settings.watchdog_notifications_enabled is False
    assert settings.watchdog_watch_window_hours is None
    assert settings.watchdog_zariya_bridge_url is None


def test_watchdog_settings_parse_when_enabled() -> None:
    settings = Settings.from_env(
        {
            **base_env(),
            "KRUBIT_WATCHDOG_ENABLED": "true",
            "KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED": "true",
            "KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS": "12",
            "KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL": "https://zariya.example/bridge",
        }
    )
    assert settings.watchdog_enabled is True
    assert settings.watchdog_notifications_enabled is True
    assert settings.watchdog_watch_window_hours == 12
    assert settings.watchdog_zariya_bridge_url == "https://zariya.example/bridge"


def test_watchdog_settings_reject_non_positive_watch_window_hours() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS"):
        Settings.from_env({**base_env(), "KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS": "0"})


def test_watchdog_settings_reject_non_https_zariya_bridge_url() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL"):
        Settings.from_env(
            {**base_env(), "KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL": "http://zariya.example/bridge"}
        )


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
    assert settings.health_check_port is None


def test_settings_parses_health_check_port_from_platform_injected_port_var() -> None:
    settings = Settings.from_env({**base_env(), "PORT": "8080"})
    assert settings.health_check_port == 8080


def test_settings_treats_invalid_port_as_absent_rather_than_erroring() -> None:
    # PORT is platform-injected, not operator-set (unlike KRUBIT_CALLBACK_PORT) --
    # a malformed value must never crash startup over something the operator has
    # no config knob to fix. See health_check_port's docstring in config.py.
    assert Settings.from_env({**base_env(), "PORT": "not-a-port"}).health_check_port is None
    assert Settings.from_env({**base_env(), "PORT": "70000"}).health_check_port is None


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


def test_activity_ledger_settings_default_disabled() -> None:
    settings = Settings.from_env(base_env())
    assert settings.activity_ledger_enabled is False
    assert settings.activity_ledger_excluded_channel_ids == ()
    assert settings.activity_ledger_retention_days is None
    assert settings.activity_ledger_inactivity_threshold_days is None


def test_activity_ledger_settings_parse_when_enabled() -> None:
    settings = Settings.from_env(
        {
            **base_env(),
            "KRUBIT_ACTIVITY_LEDGER_ENABLED": "true",
            "KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS": "111,222,333",
            "KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS": "90",
            "KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS": "30",
        }
    )
    assert settings.activity_ledger_enabled is True
    assert settings.activity_ledger_excluded_channel_ids == (111, 222, 333)
    assert settings.activity_ledger_retention_days == 90
    assert settings.activity_ledger_inactivity_threshold_days == 30


def test_activity_ledger_settings_reject_invalid_enabled_value() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_ACTIVITY_LEDGER_ENABLED"):
        Settings.from_env({**base_env(), "KRUBIT_ACTIVITY_LEDGER_ENABLED": "sure"})


def test_activity_ledger_settings_reject_non_numeric_excluded_channel_id() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS"):
        Settings.from_env(
            {**base_env(), "KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS": "111,not-a-id"}
        )


def test_activity_ledger_settings_reject_non_positive_retention_days() -> None:
    with pytest.raises(SettingsError, match="KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS"):
        Settings.from_env({**base_env(), "KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS": "0"})


def test_activity_ledger_settings_reject_non_positive_inactivity_threshold_days() -> None:
    with pytest.raises(
        SettingsError, match="KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS"
    ):
        Settings.from_env(
            {**base_env(), "KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS": "-1"}
        )


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
