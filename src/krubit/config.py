"""Environment-only runtime configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class SettingsError(ValueError):
    """Raised when Krubit's runtime configuration is invalid."""


_BOOL_VALUES = {"true", "false", "1", "0"}


def _parse_bool(name: str, raw: str) -> bool:
    if raw not in _BOOL_VALUES:
        raise SettingsError(f"{name} must be one of true, false, 1, or 0")
    return raw in {"true", "1"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment-derived runtime configuration.

    Secret-bearing fields are marked `repr=False` so `repr(settings)`/`str(settings)`
    (and anything that stringifies this object — a debug log, an uncaught-exception
    frame dump) never renders a plaintext secret. Non-secret identifiers (client IDs,
    app IDs, callback base URLs) are intentionally left visible for diagnostics.
    """

    application_id: int
    database_path: Path
    bot_token: str | None = field(default=None, repr=False)
    staff_channel_id: int | None = None
    twitch_client_id: str | None = None
    twitch_client_secret: str | None = field(default=None, repr=False)
    live_signals_enabled: bool = False
    youtube_api_key: str | None = field(default=None, repr=False)
    youtube_push_callback_secret: str | None = field(default=None, repr=False)
    x_bearer_token: str | None = field(default=None, repr=False)
    meta_app_id: str | None = None
    meta_app_secret: str | None = field(default=None, repr=False)
    meta_callback_base_url: str | None = None
    tiktok_client_key: str | None = None
    tiktok_client_secret: str | None = field(default=None, repr=False)
    tiktok_callback_base_url: str | None = None
    creator_signals_enabled: bool = False
    social_delivery_enabled: bool = False
    credential_encryption_key: str | None = field(default=None, repr=False)
    callback_public_base_url: str | None = None
    callback_port: int | None = None
    watchdog_enabled: bool = False
    watchdog_notifications_enabled: bool = False
    watchdog_watch_window_hours: int | None = None
    watchdog_zariya_bridge_url: str | None = None
    activity_ledger_enabled: bool = False
    activity_ledger_excluded_channel_ids: tuple[int, ...] = ()
    # Consumed by `krubit.discord.activity_runtime.ActivityRuntime.sweep_cycle`: when
    # set, seeds a guild's default `RetentionPolicy` the first time a sweep finds none
    # configured (never overwrites a staff-configured policy -- see that method's
    # docstring).
    activity_ledger_retention_days: int | None = None
    # NOT YET CONSUMED anywhere in this codebase. This is a query-time parameter for
    # `krubit.services.activity_views.inactive_view`/`returning_member_view`
    # (`inactivity_threshold: timedelta`), which only a later task's `/fetch
    # inactive`/community-pulse-style command surface (Task 8 in the Phase 4 plan)
    # will actually read and pass through. Parsed and validated here so operators can
    # configure it ahead of that surface landing, but setting it currently has no
    # observable effect -- unlike `activity_ledger_retention_days` above, there is no
    # sensible "seed a default row" action for a per-query threshold, so there is
    # nothing for this task to wire it into.
    activity_ledger_inactivity_threshold_days: int | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environ is None else environ
        raw_application_id = values.get("DISCORD_KRUBIT_APPLICATION_ID", "").strip()
        if not raw_application_id.isdigit() or int(raw_application_id) <= 0:
            raise SettingsError(
                "DISCORD_KRUBIT_APPLICATION_ID must be a positive numeric Discord snowflake"
            )
        raw_path = values.get("KRUBIT_DATABASE_PATH", "data/krubit.db").strip()
        if not raw_path:
            raise SettingsError("KRUBIT_DATABASE_PATH must not be blank")
        raw_token = values.get("DISCORD_KRUBIT_BOT_TOKEN", "").strip()
        raw_staff_channel = values.get("KRUBIT_STAFF_CHANNEL_ID", "").strip()
        raw_twitch_client_id = values.get("TWITCH_KRUBIT_CLIENT_ID", "").strip()
        raw_twitch_client_secret = values.get("TWITCH_KRUBIT_CLIENT_SECRET", "").strip()
        raw_live_signals_enabled = values.get("KRUBIT_LIVE_SIGNALS_ENABLED", "false").strip()
        if raw_staff_channel and (
            not raw_staff_channel.isdigit() or int(raw_staff_channel) <= 0
        ):
            raise SettingsError(
                "KRUBIT_STAFF_CHANNEL_ID must be a positive numeric Discord snowflake"
            )
        live_signals_enabled = _parse_bool("KRUBIT_LIVE_SIGNALS_ENABLED", raw_live_signals_enabled)

        raw_youtube_api_key = values.get("YOUTUBE_KRUBIT_API_KEY", "").strip()
        raw_youtube_push_callback_secret = values.get(
            "YOUTUBE_KRUBIT_PUSH_CALLBACK_SECRET", ""
        ).strip()
        raw_x_bearer_token = values.get("X_KRUBIT_BEARER_TOKEN", "").strip()
        raw_meta_app_id = values.get("META_KRUBIT_APP_ID", "").strip()
        raw_meta_app_secret = values.get("META_KRUBIT_APP_SECRET", "").strip()
        raw_meta_callback_base_url = values.get("META_KRUBIT_CALLBACK_BASE_URL", "").strip()
        raw_tiktok_client_key = values.get("TIKTOK_KRUBIT_CLIENT_KEY", "").strip()
        raw_tiktok_client_secret = values.get("TIKTOK_KRUBIT_CLIENT_SECRET", "").strip()
        raw_tiktok_callback_base_url = values.get("TIKTOK_KRUBIT_CALLBACK_BASE_URL", "").strip()
        raw_creator_signals_enabled = values.get(
            "KRUBIT_CREATOR_SIGNALS_ENABLED", "false"
        ).strip()
        raw_social_delivery_enabled = values.get(
            "KRUBIT_SOCIAL_DELIVERY_ENABLED", "false"
        ).strip()
        raw_credential_encryption_key = values.get("KRUBIT_CREDENTIAL_ENCRYPTION_KEY", "").strip()
        raw_callback_public_base_url = values.get("KRUBIT_CALLBACK_PUBLIC_BASE_URL", "").strip()
        raw_callback_port = values.get("KRUBIT_CALLBACK_PORT", "").strip()
        raw_watchdog_enabled = values.get("KRUBIT_WATCHDOG_ENABLED", "false").strip()
        raw_watchdog_notifications_enabled = values.get(
            "KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED", "false"
        ).strip()
        raw_watchdog_watch_window_hours = values.get(
            "KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS", ""
        ).strip()
        raw_watchdog_zariya_bridge_url = values.get(
            "KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL", ""
        ).strip()
        raw_activity_ledger_enabled = values.get(
            "KRUBIT_ACTIVITY_LEDGER_ENABLED", "false"
        ).strip()
        raw_activity_ledger_excluded_channel_ids = values.get(
            "KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS", ""
        ).strip()
        raw_activity_ledger_retention_days = values.get(
            "KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS", ""
        ).strip()
        raw_activity_ledger_inactivity_threshold_days = values.get(
            "KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS", ""
        ).strip()

        creator_signals_enabled = _parse_bool(
            "KRUBIT_CREATOR_SIGNALS_ENABLED", raw_creator_signals_enabled
        )
        social_delivery_enabled = _parse_bool(
            "KRUBIT_SOCIAL_DELIVERY_ENABLED", raw_social_delivery_enabled
        )
        if raw_callback_public_base_url and not raw_callback_public_base_url.startswith(
            "https://"
        ):
            raise SettingsError("KRUBIT_CALLBACK_PUBLIC_BASE_URL must use https")
        callback_port: int | None = None
        if raw_callback_port:
            if not raw_callback_port.isdigit() or not (1 <= int(raw_callback_port) <= 65_535):
                raise SettingsError(
                    "KRUBIT_CALLBACK_PORT must be a port number between 1 and 65535"
                )
            callback_port = int(raw_callback_port)

        watchdog_enabled = _parse_bool("KRUBIT_WATCHDOG_ENABLED", raw_watchdog_enabled)
        watchdog_notifications_enabled = _parse_bool(
            "KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED", raw_watchdog_notifications_enabled
        )
        watchdog_watch_window_hours: int | None = None
        if raw_watchdog_watch_window_hours:
            if (
                not raw_watchdog_watch_window_hours.isdigit()
                or int(raw_watchdog_watch_window_hours) <= 0
            ):
                raise SettingsError(
                    "KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS must be a positive integer"
                )
            watchdog_watch_window_hours = int(raw_watchdog_watch_window_hours)
        if raw_watchdog_zariya_bridge_url and not raw_watchdog_zariya_bridge_url.startswith(
            "https://"
        ):
            raise SettingsError("KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL must use https")

        activity_ledger_enabled = _parse_bool(
            "KRUBIT_ACTIVITY_LEDGER_ENABLED", raw_activity_ledger_enabled
        )
        activity_ledger_excluded_channel_ids: tuple[int, ...] = ()
        if raw_activity_ledger_excluded_channel_ids:
            raw_ids = [
                part.strip()
                for part in raw_activity_ledger_excluded_channel_ids.split(",")
                if part.strip()
            ]
            if not raw_ids or any(
                not raw_id.isdigit() or int(raw_id) <= 0 for raw_id in raw_ids
            ):
                raise SettingsError(
                    "KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS must be a comma-separated "
                    "list of positive numeric Discord snowflakes"
                )
            activity_ledger_excluded_channel_ids = tuple(int(raw_id) for raw_id in raw_ids)
        activity_ledger_retention_days: int | None = None
        if raw_activity_ledger_retention_days:
            if (
                not raw_activity_ledger_retention_days.isdigit()
                or int(raw_activity_ledger_retention_days) <= 0
            ):
                raise SettingsError(
                    "KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS must be a positive integer"
                )
            activity_ledger_retention_days = int(raw_activity_ledger_retention_days)
        activity_ledger_inactivity_threshold_days: int | None = None
        if raw_activity_ledger_inactivity_threshold_days:
            if (
                not raw_activity_ledger_inactivity_threshold_days.isdigit()
                or int(raw_activity_ledger_inactivity_threshold_days) <= 0
            ):
                raise SettingsError(
                    "KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS must be a positive integer"
                )
            activity_ledger_inactivity_threshold_days = int(
                raw_activity_ledger_inactivity_threshold_days
            )

        return cls(
            application_id=int(raw_application_id),
            database_path=Path(raw_path),
            bot_token=raw_token or None,
            staff_channel_id=int(raw_staff_channel) if raw_staff_channel else None,
            twitch_client_id=raw_twitch_client_id or None,
            twitch_client_secret=raw_twitch_client_secret or None,
            live_signals_enabled=live_signals_enabled,
            youtube_api_key=raw_youtube_api_key or None,
            youtube_push_callback_secret=raw_youtube_push_callback_secret or None,
            x_bearer_token=raw_x_bearer_token or None,
            meta_app_id=raw_meta_app_id or None,
            meta_app_secret=raw_meta_app_secret or None,
            meta_callback_base_url=raw_meta_callback_base_url or None,
            tiktok_client_key=raw_tiktok_client_key or None,
            tiktok_client_secret=raw_tiktok_client_secret or None,
            tiktok_callback_base_url=raw_tiktok_callback_base_url or None,
            creator_signals_enabled=creator_signals_enabled,
            social_delivery_enabled=social_delivery_enabled,
            credential_encryption_key=raw_credential_encryption_key or None,
            callback_public_base_url=raw_callback_public_base_url or None,
            callback_port=callback_port,
            watchdog_enabled=watchdog_enabled,
            watchdog_notifications_enabled=watchdog_notifications_enabled,
            watchdog_watch_window_hours=watchdog_watch_window_hours,
            watchdog_zariya_bridge_url=raw_watchdog_zariya_bridge_url or None,
            activity_ledger_enabled=activity_ledger_enabled,
            activity_ledger_excluded_channel_ids=activity_ledger_excluded_channel_ids,
            activity_ledger_retention_days=activity_ledger_retention_days,
            activity_ledger_inactivity_threshold_days=activity_ledger_inactivity_threshold_days,
        )

    def require_token(self) -> str:
        if self.bot_token is None:
            raise SettingsError("DISCORD_KRUBIT_BOT_TOKEN is required to run the Discord bot")
        return self.bot_token

    def require_twitch_credentials(self) -> tuple[str, str]:
        if self.twitch_client_id is None or self.twitch_client_secret is None:
            raise SettingsError(
                "TWITCH_KRUBIT_CLIENT_ID and TWITCH_KRUBIT_CLIENT_SECRET are required "
                "when KRUBIT_LIVE_SIGNALS_ENABLED=true"
            )
        return self.twitch_client_id, self.twitch_client_secret

    def require_credential_encryption_key(self) -> str:
        if self.credential_encryption_key is None:
            raise SettingsError(
                "KRUBIT_CREDENTIAL_ENCRYPTION_KEY is required to store or read creator "
                "OAuth grants"
            )
        return self.credential_encryption_key
