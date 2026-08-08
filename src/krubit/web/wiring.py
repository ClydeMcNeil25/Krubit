"""Assembles Krubit's callback-server routes from settings, storage, and each
platform's existing connector code.

`build_callback_routes` is the single production call site that turns
`Settings`/`SQLiteStore`/`CredentialVault` into the route set `CallbackServer`
serves. Every route it registers is gated independently per the design spec's
capability-specific gating rule
(`docs/superpowers/specs/2026-08-07-phase-2-callback-server-design.md`, Component
7): OAuth authorization routes require the vault (sealing a grant needs it);
Meta's deauthorization/data-deletion routes require only `meta_app_secret`
(verifying and deleting needs no decryption).
"""

from __future__ import annotations

from datetime import UTC, datetime

from krubit.config import Settings
from krubit.domain.creator_signals import Platform, RecognizedAccountUrl
from krubit.integrations import meta, tiktok
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackRoute, OAuthRedirect, build_oauth_redirect_route

# Which Meta connector class resolves the authorizing account for a given
# platform. Keyed by `Platform` (the oauth_attempts row's `platform` column),
# NOT by `Capability` (`account`/`social`/`live` — a single, platform-independent
# enum shared across every connector) which is a separate column on the same row.
_META_CONNECTOR_BY_PLATFORM = {
    Platform.INSTAGRAM: meta.InstagramConnector,
    Platform.FACEBOOK_PAGE: meta.FacebookPageConnector,
    Platform.FACEBOOK: meta.FacebookProfileConnector,
    Platform.THREADS: meta.ThreadsConnector,
}


def build_callback_routes(
    settings: Settings,
    store: SQLiteStore,
    vault: CredentialVault | None,
    oauth_session: object,
) -> tuple[CallbackRoute, ...]:
    if not settings.creator_signals_enabled:
        return ()
    if settings.callback_public_base_url is None or settings.callback_port is None:
        return ()

    routes: list[CallbackRoute] = []

    if (
        settings.tiktok_client_key is not None
        and settings.tiktok_client_secret is not None
        and vault is not None
    ):
        routes.append(
            _build_tiktok_authorize_route(settings, store, vault, oauth_session)
        )

    if (
        settings.meta_app_id is not None
        and settings.meta_app_secret is not None
        and vault is not None
    ):
        routes.append(
            _build_meta_authorize_route(settings, store, vault, oauth_session)
        )

    return tuple(routes)


def _build_tiktok_authorize_route(
    settings: Settings, store: SQLiteStore, vault: CredentialVault, oauth_session: object
) -> CallbackRoute:
    redirect_uri = f"{settings.callback_public_base_url}/callbacks/tiktok/authorize"

    async def handle_redirect(query: object) -> str:
        code = query.get("code")
        state = query.get("state")
        if not code or not state:
            raise ValueError("authorization redirect is missing required parameters")

        attempt = await store.consume_oauth_attempt(state, now=datetime.now(UTC))
        if attempt is None:
            raise ValueError("authorization request could not be completed")

        assert settings.tiktok_client_key is not None
        assert settings.tiktok_client_secret is not None
        grant = await tiktok.exchange_authorization_code(
            oauth_session,
            code=code,
            client_key=settings.tiktok_client_key,
            client_secret=settings.tiktok_client_secret,
            redirect_uri=redirect_uri,
        )
        connector = tiktok.TikTokConnector(oauth_session, grant.access_token)
        identity = await connector.fetch_authorized_identity()

        account = await store.get_creator_account(attempt.guild_id, attempt.account_id)
        if account is None or (
            identity.username is not None
            and identity.username.lower() != account.handle.lower()
        ):
            raise ValueError("authorization request could not be completed")

        secret_ref = vault.seal_json(
            {
                "access_token": grant.access_token,
                "refresh_token": grant.refresh_token,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            }
        )
        await store.save_connector_authorization(
            guild_id=attempt.guild_id,
            account_id=attempt.account_id,
            capability=attempt.capability,
            secret_ref=secret_ref,
            provider_resource_id=identity.open_id,
            authorization_subject_id=identity.open_id,
            status="active",
            expires_at=grant.expires_at,
            now=datetime.now(UTC),
        )
        return "Authorization complete. You may close this window."

    redirect = OAuthRedirect(handle_redirect=handle_redirect)
    return build_oauth_redirect_route(path="/callbacks/tiktok/authorize", redirect=redirect)


def _build_meta_authorize_route(
    settings: Settings, store: SQLiteStore, vault: CredentialVault, oauth_session: object
) -> CallbackRoute:
    redirect_uri = f"{settings.callback_public_base_url}/callbacks/meta/authorize"

    async def handle_redirect(query: object) -> str:
        code = query.get("code")
        state = query.get("state")
        if not code or not state:
            raise ValueError("authorization redirect is missing required parameters")

        attempt = await store.consume_oauth_attempt(state, now=datetime.now(UTC))
        if attempt is None:
            raise ValueError("authorization request could not be completed")

        try:
            platform = Platform(attempt.platform)
        except ValueError as exc:
            raise ValueError("authorization request could not be completed") from exc

        resolver_class = _META_CONNECTOR_BY_PLATFORM.get(platform)
        if resolver_class is None:
            raise ValueError("authorization request could not be completed")

        assert settings.meta_app_id is not None
        assert settings.meta_app_secret is not None
        grant = await meta.exchange_authorization_code(
            oauth_session,
            platform=platform,
            code=code,
            client_id=settings.meta_app_id,
            client_secret=settings.meta_app_secret,
            redirect_uri=redirect_uri,
        )

        account = await store.get_creator_account(attempt.guild_id, attempt.account_id)
        if account is None:
            raise ValueError("authorization request could not be completed")

        connector = resolver_class(oauth_session, grant.access_token)
        resolved = await connector.resolve_account(
            RecognizedAccountUrl(
                platform=platform, handle=account.handle, canonical_url=account.canonical_url
            )
        )
        if resolved.handle.lower() != account.handle.lower():
            raise ValueError("authorization request could not be completed")

        authorization_subject_id = await meta.fetch_authorizing_user_id(
            oauth_session, grant.access_token
        )

        secret_ref = vault.seal_json(
            {
                "access_token": grant.access_token,
                "refresh_token": grant.refresh_token,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            }
        )
        await store.save_connector_authorization(
            guild_id=attempt.guild_id,
            account_id=attempt.account_id,
            capability=attempt.capability,
            secret_ref=secret_ref,
            provider_resource_id=resolved.external_id,
            authorization_subject_id=authorization_subject_id,
            status="active",
            expires_at=grant.expires_at,
            now=datetime.now(UTC),
        )
        return "Authorization complete. You may close this window."

    redirect = OAuthRedirect(handle_redirect=handle_redirect)
    return build_oauth_redirect_route(path="/callbacks/meta/authorize", redirect=redirect)
