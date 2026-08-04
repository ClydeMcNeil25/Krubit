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
        return cls(
            application_id=int(raw_application_id),
            database_path=Path(raw_path),
            bot_token=raw_token or None,
        )

    def require_token(self) -> str:
        if self.bot_token is None:
            raise SettingsError("DISCORD_KRUBIT_BOT_TOKEN is required to run the Discord bot")
        return self.bot_token
