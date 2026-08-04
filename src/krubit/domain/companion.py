"""Framework-independent values for Krubit's operational companion features."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from krubit.domain.models import JSONValue


@dataclass(frozen=True, slots=True)
class CoverageIssue:
    section: str
    status: str
    detail: str

    def __post_init__(self) -> None:
        if not self.section.strip() or not self.status.strip() or not self.detail.strip():
            raise ValueError("coverage fields must not be blank")


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    snapshot_id: str
    guild_id: int
    version: int
    content_hash: str
    content: dict[str, JSONValue]
    coverage: tuple[CoverageIssue, ...]
    captured_at: datetime

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip() or not self.content_hash.strip():
            raise ValueError("snapshot identifiers must not be blank")
        if self.guild_id <= 0 or self.version <= 0:
            raise ValueError("snapshot guild and version must be positive")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("snapshot timestamp must include a timezone")
