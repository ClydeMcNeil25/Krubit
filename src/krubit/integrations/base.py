"""The connector abstraction every future platform adapter implements.

`Connector` is a structural `Protocol`, not a base class: concrete adapters (Twitch,
YouTube, and so on) implement it without inheriting from anything here. This module
also defines the framework-independent values a connector produces: the resolved
`ConnectorAccount`, one `ConnectorPage` of fetched content, `ConnectorFailure` for
provider errors, and `ConnectorHealth` for capability status. `ConnectorFailure` is
deliberately secret-safe: its `safe_detail` is derived only from a fixed, per-kind
message and never echoes caller-supplied diagnostic text, so a raw provider error
string that happens to contain a token or secret can never reach a log, receipt, or
Discord surface through this type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    CreatorAccount,
    Platform,
    RecognizedAccountUrl,
)
from krubit.domain.models import JSONValue
from krubit.integrations.catalog import ConnectorDescriptor

_MAX_HANDLE_LENGTH = 128
_MAX_URL_LENGTH = 2_048
_MAX_EXTERNAL_ID_LENGTH = 200
_MAX_DETAIL_LENGTH = 200
_MAX_CURSOR_LENGTH = 500


def _require_text(name: str, value: str, *, limit: int) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")


@dataclass(frozen=True, slots=True)
class ConnectorAccount:
    """The stable platform identity a connector resolves from a `RecognizedAccountUrl`."""

    platform: Platform
    external_id: str
    handle: str
    canonical_url: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        _require_text("external_id", self.external_id, limit=_MAX_EXTERNAL_ID_LENGTH)
        _require_text("handle", self.handle, limit=_MAX_HANDLE_LENGTH)
        _require_text("canonical_url", self.canonical_url, limit=_MAX_URL_LENGTH)
        if not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must use https")
        if self.display_name is not None:
            _require_text("display_name", self.display_name, limit=_MAX_HANDLE_LENGTH)


@dataclass(frozen=True, slots=True)
class ConnectorPage:
    """One page of a connector's raw, provider-normalized content payloads.

    `items` intentionally stays JSON-shaped rather than a domain content type: the
    normalized content ledger this feeds is introduced by a later Phase 2 completion
    slice. `next_cursor` is `None` when the connector has no further pages to fetch.
    """

    items: tuple[Mapping[str, JSONValue], ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if type(self.items) is not tuple:
            raise ValueError("items must be a tuple")
        if self.next_cursor is not None:
            _require_text("next_cursor", self.next_cursor, limit=_MAX_CURSOR_LENGTH)


class ConnectorFailureKind(StrEnum):
    """The classes of failure a connector can report."""

    AUTHORIZATION = "authorization"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"


_SAFE_DETAIL_BY_KIND: Mapping[ConnectorFailureKind, str] = {
    ConnectorFailureKind.AUTHORIZATION: "authorization failed or expired",
    ConnectorFailureKind.RATE_LIMITED: "the platform rate-limited this request",
    ConnectorFailureKind.QUOTA_EXCEEDED: "the platform quota was exceeded",
    ConnectorFailureKind.UNAVAILABLE: "the platform was unavailable",
    ConnectorFailureKind.INVALID_RESPONSE: "the platform returned an unexpected response",
    ConnectorFailureKind.TIMEOUT: "the request timed out",
    ConnectorFailureKind.NOT_FOUND: "the requested resource was not found",
}


@dataclass(frozen=True, slots=True)
class ConnectorFailure:
    """A classified connector error.

    `detail` is raw, caller-supplied diagnostic text (for example a provider error
    body) and may contain sensitive values; it is never rendered anywhere Krubit
    surfaces to Discord, logs, or receipts. `safe_detail` is the only rendering meant
    for those surfaces: a fixed, per-`kind` message that never depends on `detail`'s
    contents, so it can never leak a secret regardless of what `detail` holds.
    """

    kind: ConnectorFailureKind
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not ConnectorFailureKind:
            raise ValueError("kind must be a ConnectorFailureKind")
        if self.detail is not None and len(self.detail) > _MAX_DETAIL_LENGTH:
            raise ValueError(f"detail exceeds {_MAX_DETAIL_LENGTH} characters")

    @property
    def safe_detail(self) -> str:
        return _SAFE_DETAIL_BY_KIND[self.kind]

    @classmethod
    def authorization(cls, detail: str | None = None) -> ConnectorFailure:
        return cls(ConnectorFailureKind.AUTHORIZATION, detail)

    @classmethod
    def rate_limited(cls, detail: str | None = None) -> ConnectorFailure:
        return cls(ConnectorFailureKind.RATE_LIMITED, detail)

    @classmethod
    def quota_exceeded(cls, detail: str | None = None) -> ConnectorFailure:
        return cls(ConnectorFailureKind.QUOTA_EXCEEDED, detail)

    @classmethod
    def unavailable(cls, detail: str | None = None) -> ConnectorFailure:
        return cls(ConnectorFailureKind.UNAVAILABLE, detail)

    @classmethod
    def invalid_response(cls, detail: str | None = None) -> ConnectorFailure:
        return cls(ConnectorFailureKind.INVALID_RESPONSE, detail)

    @classmethod
    def timeout(cls, detail: str | None = None) -> ConnectorFailure:
        return cls(ConnectorFailureKind.TIMEOUT, detail)

    @classmethod
    def not_found(cls, detail: str | None = None) -> ConnectorFailure:
        return cls(ConnectorFailureKind.NOT_FOUND, detail)


class UnsupportedConnectorError(RuntimeError):
    """Raised by a connector whose declared capability is permanently `UNSUPPORTED`.

    Distinct from every `ConnectorFailure`-based error: those describe a transient or
    configuration-dependent failure a retry, a fresh token, or an operator action could
    clear. This error means the platform capability itself is not (yet) offered at
    all — no network call was attempted, and a connector raising this must never
    attempt one either, regardless of what credentials or account state it is given.
    """


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """A connector's currently observed state for one capability."""

    capability: Capability
    state: CapabilityState
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.capability) is not Capability:
            raise ValueError("capability must be a Capability")
        if type(self.state) is not CapabilityState:
            raise ValueError("state must be a CapabilityState")
        if self.detail is not None:
            _require_text("detail", self.detail, limit=_MAX_DETAIL_LENGTH)


class Connector(Protocol):
    """The contract every concrete platform adapter implements.

    A structural protocol rather than a base class: adapters satisfy it by
    implementing these members, with no inheritance required. `resolve_account`
    turns a recognized profile URL into a stable platform identity; `fetch_page`
    retrieves one page of that account's content from an optional durable cursor;
    `health` reports the connector's current capability state, optionally scoped to
    one account.
    """

    descriptor: ConnectorDescriptor

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        raise NotImplementedError("implemented by each concrete connector")

    async def fetch_page(
        self, account: CreatorAccount, *, cursor: str | None
    ) -> ConnectorPage:
        raise NotImplementedError("implemented by each concrete connector")

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        raise NotImplementedError("implemented by each concrete connector")
