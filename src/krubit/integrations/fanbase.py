"""Fanbase: a permanently dormant capability adapter.

`catalog.py` already declares `Platform.FANBASE`'s `Capability.SOCIAL` and
`Capability.LIVE` as `CapabilityState.UNSUPPORTED` — Fanbase offers no public or
partner API Krubit can read from today. `FanbaseConnector` exists only so Fanbase
satisfies `krubit.integrations.base.Connector` structurally alongside every other
platform, not because it does any real work.

Unlike every other connector in this package, `FanbaseConnector` never holds an
`aiohttp`-like session, an access token, or any other connector-DI machinery: there
is nothing for it to call. `resolve_account` recognizes the account's URL-derived
metadata (matching `Capability.ACCOUNT`'s `READY` state — "URL recognition" needs no
network access), but `fetch_page` always raises `UnsupportedConnectorError` before
attempting anything, and `health` always reports the same static `UNSUPPORTED`
state regardless of any account passed to it. No code path here ever performs a
network request, now or if this module changes in the future — that is the entire
point of keeping it this small.
"""

from __future__ import annotations

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    CreatorAccount,
    Platform,
    RecognizedAccountUrl,
)
from krubit.integrations.base import (
    ConnectorAccount,
    ConnectorHealth,
    ConnectorPage,
    UnsupportedConnectorError,
)
from krubit.integrations.catalog import CATALOG

_UNSUPPORTED_DETAIL = "Pending official API or partner access"


class FanbaseConnector:
    """Recognizes Fanbase account metadata but never makes a network request.

    Satisfies `krubit.integrations.base.Connector` structurally. Takes no
    constructor arguments — there is no session or credential to inject.
    """

    descriptor = CATALOG[Platform.FANBASE]

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        """Build the stable identity from the recognized URL alone; no network call."""
        return ConnectorAccount(
            platform=Platform.FANBASE,
            external_id=recognized.handle,
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        raise UnsupportedConnectorError(_UNSUPPORTED_DETAIL)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        return ConnectorHealth(
            capability=Capability.SOCIAL,
            state=CapabilityState.UNSUPPORTED,
            detail=_UNSUPPORTED_DETAIL,
        )
