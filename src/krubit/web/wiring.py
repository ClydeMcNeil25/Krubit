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

import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from aiohttp import web

from krubit.config import Settings
from krubit.domain.creator_signals import Platform, RecognizedAccountUrl
from krubit.integrations import meta, tiktok
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import ConnectorAuthorization, SQLiteStore
from krubit.web.callbacks import (
    CallbackRoute,
    OAuthRedirect,
    SignedFormRequest,
    build_oauth_redirect_route,
    build_signed_form_route,
)

# The data_deletion_requests freshness window: a repeat request for the same
# authorization_subject_id within this window reuses the existing confirmation
# code instead of minting a new one and re-running deletion.
_DATA_DELETION_FRESHNESS_WINDOW = timedelta(minutes=5)

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

    # Deauthorization/data-deletion require only meta_app_secret (verifying a
    # signed_request and deleting rows needs no decryption), independent of the
    # vault -- Krubit must be able to honor a legally-required data-deletion
    # request even when the vault is not configured. Opposite gating rule from
    # the OAuth authorization routes above, which do require the vault.
    if settings.meta_app_secret is not None:
        routes.append(_build_meta_deauthorize_route(settings, store))
        routes.extend(_build_meta_data_deletion_routes(settings, store))

    return tuple(routes)


async def _find_meta_connector_authorizations(
    store: SQLiteStore, authorization_subject_id: str
) -> tuple[ConnectorAuthorization, ...]:
    """Find every connector authorization for a Meta user across all Meta platforms.

    `find_connector_authorizations_by_authorization_subject` filters by the
    `creator_accounts.platform` column, which stores individual platform values
    (`instagram`, `facebook_page`, `facebook`, `threads`) -- there is no single
    "meta" platform value in that column. A Meta user can be the authorizing
    subject for any of them, so deauthorization/data-deletion must search each
    in turn rather than a single literal "meta" platform.
    """
    rows: list[ConnectorAuthorization] = []
    for platform in _META_CONNECTOR_BY_PLATFORM:
        rows.extend(
            await store.find_connector_authorizations_by_authorization_subject(
                platform.value, authorization_subject_id
            )
        )
    return tuple(rows)


def _build_meta_deauthorize_route(settings: Settings, store: SQLiteStore) -> CallbackRoute:
    app_secret = settings.meta_app_secret
    assert app_secret is not None

    def verify_and_parse(raw_value: str) -> Mapping[str, object] | None:
        return meta.verify_meta_signed_request(raw_value, app_secret)

    async def handle_notification(payload: Mapping[str, object]) -> web.StreamResponse:
        user_id = str(payload["user_id"])
        rows = await _find_meta_connector_authorizations(store, user_id)
        await store.delete_connector_authorizations(rows, now=datetime.now(UTC))
        return web.Response(status=200)

    webhook = SignedFormRequest(
        verify_and_parse=verify_and_parse, handle_notification=handle_notification
    )
    return build_signed_form_route(
        path="/callbacks/meta/deauthorize", field_name="signed_request", webhook=webhook
    )


def _build_meta_data_deletion_routes(
    settings: Settings, store: SQLiteStore
) -> tuple[CallbackRoute, CallbackRoute]:
    app_secret = settings.meta_app_secret
    assert app_secret is not None
    base_url = settings.callback_public_base_url

    def verify_and_parse(raw_value: str) -> Mapping[str, object] | None:
        return meta.verify_meta_signed_request(raw_value, app_secret)

    async def handle_notification(payload: Mapping[str, object]) -> web.StreamResponse:
        user_id = str(payload["user_id"])
        now = datetime.now(UTC)

        existing = await store.find_recent_data_deletion_request(
            user_id, "meta", since=now - _DATA_DELETION_FRESHNESS_WINDOW
        )
        if existing is not None:
            return web.json_response(
                {
                    "url": (
                        f"{base_url}/callbacks/meta/data-deletion/status"
                        f"?id={existing.confirmation_code}"
                    ),
                    "confirmation_code": existing.confirmation_code,
                }
            )

        rows = await _find_meta_connector_authorizations(store, user_id)
        await store.delete_connector_authorizations(rows, now=now)

        confirmation_code = secrets.token_urlsafe(16)
        await store.save_data_deletion_request(
            confirmation_code=confirmation_code,
            authorization_subject_id=user_id,
            platform="meta",
            requested_at=now,
            rows_deleted=len(rows),
        )
        return web.json_response(
            {
                "url": (
                    f"{base_url}/callbacks/meta/data-deletion/status"
                    f"?id={confirmation_code}"
                ),
                "confirmation_code": confirmation_code,
            }
        )

    webhook = SignedFormRequest(
        verify_and_parse=verify_and_parse, handle_notification=handle_notification
    )
    deletion_route = build_signed_form_route(
        path="/callbacks/meta/data-deletion", field_name="signed_request", webhook=webhook
    )

    async def handle_status(request: web.Request) -> web.StreamResponse:
        confirmation_code = request.query.get("id")
        if not confirmation_code:
            return web.Response(status=404)
        record = await store.get_data_deletion_request(confirmation_code)
        if record is None:
            return web.Response(status=404)
        return web.json_response(
            {"confirmation_code": record.confirmation_code, "status": "complete"}
        )

    status_route = CallbackRoute(
        path="/callbacks/meta/data-deletion/status", method="GET", handler=handle_status
    )
    return deletion_route, status_route


def _build_tiktok_authorize_route(
    settings: Settings, store: SQLiteStore, vault: CredentialVault, oauth_session: object
) -> CallbackRoute:
    async def handle_redirect(query: Mapping[str, str]) -> str:
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
            # Read from the consumed attempt row, not recomputed from current
            # settings -- a mid-flight change to KRUBIT_CALLBACK_PUBLIC_BASE_URL
            # must not break an in-flight authorization.
            redirect_uri=attempt.redirect_uri,
        )
        connector = tiktok.TikTokConnector(oauth_session, grant.access_token)
        identity = await connector.fetch_authorized_identity()

        account = await store.get_creator_account(attempt.guild_id, attempt.account_id)
        # `identity.username is None` (no `user.info.profile` scope granted) is a
        # REJECTION, not a skip -- treating it as "nothing to check" would undo the
        # entire point of `fetch_authorized_identity` sourcing an independently
        # confirmed username instead of trusting an echoed value.
        if (
            account is None
            or identity.username is None
            or identity.username.lower() != account.handle.lower()
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
    async def handle_redirect(query: Mapping[str, str]) -> str:
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

        # Facebook Page / Facebook Profile OAuth authorization is not yet
        # supported: there is no reliable Graph-resolved identity to bind
        # against for either -- `FacebookPageConnector.resolve_account` never
        # performs the `/me/accounts` Page-token exchange needed to resolve a
        # genuine Page identity, and a personal Profile's `/me` has no
        # comparable field at all. Reject before spending a real call against
        # `meta.exchange_authorization_code` on a platform we already know
        # we're rejecting. This is a deliberate, documented gap, not an
        # oversight -- see the design spec's Explicit Exclusions section.
        if platform in (Platform.FACEBOOK_PAGE, Platform.FACEBOOK):
            raise ValueError("authorization request could not be completed")

        assert settings.meta_app_id is not None
        assert settings.meta_app_secret is not None
        grant = await meta.exchange_authorization_code(
            oauth_session,
            platform=platform,
            code=code,
            client_id=settings.meta_app_id,
            client_secret=settings.meta_app_secret,
            # Read from the consumed attempt row, not recomputed from current
            # settings -- a mid-flight change to KRUBIT_CALLBACK_PUBLIC_BASE_URL
            # must not break an in-flight authorization.
            redirect_uri=attempt.redirect_uri,
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

        # `resolved.handle` is not a trustworthy verification signal here:
        # `InstagramConnector`/`ThreadsConnector` (the only connectors that reach
        # this point -- Facebook Page/Profile are rejected above) only sometimes
        # fetch a real `username` from Graph, silently falling back to echoing
        # the input handle when Graph doesn't return one. Verify against
        # `fetch_authorized_identity` instead, which never echoes the input
        # handle -- its `username` is `None`, not a fallback, when Graph did not
        # return one (e.g. missing scope). Treat that as a hard rejection, not a
        # skip.
        assert isinstance(connector, (meta.InstagramConnector, meta.ThreadsConnector))
        identity = await connector.fetch_authorized_identity()
        if identity.username is None or identity.username.lower() != account.handle.lower():
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
