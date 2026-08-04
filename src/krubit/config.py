"""Environment-only runtime configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class SettingsError(ValueError):
    """Raised when Krubit's runtime configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    application_id: int
    database_path: Path
    bot_token: str | None = None
    staff_channel_id: int | None = None
    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None
    live_signals_enabled: bool = False

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
        if raw_live_signals_enabled not in {"true", "false", "1", "0"}:
            raise SettingsError(
                "KRUBIT_LIVE_SIGNALS_ENABLED must be one of true, false, 1, or 0"
            )
        return cls(
            application_id=int(raw_application_id),
            database_path=Path(raw_path),
            bot_token=raw_token or None,
            staff_channel_id=int(raw_staff_channel) if raw_staff_channel else None,
            twitch_client_id=raw_twitch_client_id or None,
            twitch_client_secret=raw_twitch_client_secret or None,
            live_signals_enabled=raw_live_signals_enabled in {"true", "1"},
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
