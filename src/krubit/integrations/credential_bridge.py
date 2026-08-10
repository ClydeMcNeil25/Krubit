"""Bridges per-account OAuth credentials (already issued by `/fetch
creator authorize`) into the `Connector` protocol `ConnectorScheduler`
already polls generically.

`InstagramConnector`/`ThreadsConnector`/`TikTokConnector` (and every
other connector in this codebase) take a fixed access token at
construction -- correct for YouTube/X/Bluesky's single bot-wide
credential, wrong for a platform where each enrolled creator account has
its own separately-authorized token. `CredentialResolvingConnector`
supplies the one thing missing: on every call, it looks up the specific
account's stored authorization, unseals its token, and constructs the
real per-use connector fresh -- stateless, no caching, so a creator's
re-authorization takes effect on the very next poll with no cache
invalidation to reason about.

An account with no valid authorization on file (missing, revoked, or
expired -- no token-refresh exchange is built here, an expired token is
simply an authorization the creator needs to redo) raises the exact same
error shape (`ConnectorFailure.authorization(...)`, wrapped in the
platform's own existing error type) every other authorization failure in
this codebase already produces -- `ConnectorScheduler`'s existing
failure-handling and this codebase's existing `AUTHORIZATION_REQUIRED`
health-state reporting require no changes to understand it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    CreatorAccount,
    Platform,
    RecognizedAccountUrl,
)
from krubit.integrations.base import (
    Connector,
    ConnectorAccount,
    ConnectorFailure,
    ConnectorHealth,
    ConnectorPage,
)
from krubit.integrations.catalog import CATALOG, ConnectorDescriptor
from krubit.security.credential_vault import CredentialVault, CredentialVaultError
from krubit.storage.sqlite import SQLiteStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CredentialResolvingConnector:
    """Satisfies `Connector` by resolving a per-account token from
    storage on every call, rather than a fixed token at construction."""

    def __init__(
        self,
        session: object,
        store: SQLiteStore,
        vault: CredentialVault,
        *,
        platform: Platform,
        inner_connector_factory: Callable[[object, str], Connector],
        error_type: type[Exception],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._vault = vault
        self._platform = platform
        self._inner_connector_factory = inner_connector_factory
        self._error_type = error_type
        self._now = now or _utc_now
        # Satisfies `Connector`'s structural protocol with this instance's own
        # platform's static catalog entry -- the same descriptor the real
        # per-use connector this bridge wraps would report for that platform,
        # since `CredentialResolvingConnector` is a stand-in for it, not a
        # distinct platform of its own.
        self.descriptor: ConnectorDescriptor = CATALOG[platform]

    async def _resolve_inner(self, account: CreatorAccount) -> Connector:
        authorization = await self._store.get_connector_authorization(
            account.guild_id, account.account_id, Capability.SOCIAL.value
        )
        if authorization is None:
            raise self._error_type(
                ConnectorFailure.authorization("no authorization on file for this account")
            )
        if authorization.status != "active":
            raise self._error_type(
                ConnectorFailure.authorization(
                    f"stored authorization status is {authorization.status!r}, not active"
                )
            )
        if authorization.expires_at is not None and authorization.expires_at <= self._now():
            raise self._error_type(
                ConnectorFailure.authorization("stored authorization has expired")
            )
        try:
            grant = self._vault.open_json(authorization.secret_ref)
        except CredentialVaultError as exc:
            raise self._error_type(
                ConnectorFailure.authorization("stored authorization could not be unsealed")
            ) from exc
        access_token = grant.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise self._error_type(
                ConnectorFailure.authorization("stored authorization is malformed")
            )
        return self._inner_connector_factory(self._session, access_token)

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        # Never called by `ConnectorScheduler` -- confirmed by repo-wide search,
        # the only production caller of `.resolve_account(` is the OAuth callback
        # route, which constructs and uses the real per-use connector directly
        # with the token it just obtained, never through this bridge. There is
        # also no real account yet to resolve a stored authorization against at
        # this point (an account must already exist, with a real guild_id and
        # account_id, before any authorization can be looked up for it) --
        # `CreatorAccount.__post_init__` rejects a placeholder `guild_id=0` and
        # empty `account_id`/`external_id`, so this raises directly rather than
        # fighting that validation for genuinely dead code.
        raise NotImplementedError(
            "CredentialResolvingConnector.resolve_account is never called in production: "
            "ConnectorScheduler only calls fetch_page/health on registered connectors, and "
            "the OAuth callback route resolves accounts through the real per-use connector "
            "directly, before any bridge or stored authorization exists."
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        inner = await self._resolve_inner(account)
        return await inner.fetch_page(account, cursor=cursor)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if account is None:
            return ConnectorHealth(
                capability=Capability.SOCIAL,
                state=CapabilityState.UNCONFIGURED,
                detail="no account specified",
            )
        try:
            inner = await self._resolve_inner(account)
        except self._error_type as exc:
            failure = getattr(exc, "failure", None)
            detail = failure.safe_detail if isinstance(failure, ConnectorFailure) else None
            return ConnectorHealth(
                capability=Capability.SOCIAL,
                state=CapabilityState.AUTHORIZATION_REQUIRED,
                detail=detail,
            )
        return await inner.health(account)
