"""Minimal aiohttp ingress for platform push/webhook callbacks.

`CallbackServer` binds nothing unless a port is configured, so a deployment with
no social platform credentials and no health-check port never opens a listening
socket. A public HTTPS base URL is validated when present but is not itself
required to bind -- it is only needed to construct OAuth redirect URIs elsewhere
(see `krubit.web.wiring.build_callback_routes`, which independently gates the
OAuth/webhook routes on both being set). This split lets `build_health_route`
below be served on its own port with no public base URL at all. Once bound, it
enforces an HTTPS public base (when one is configured), a 1 MiB body
limit per request, one HTTP method per registered route, a request timeout, and
redacted error responses: unregistered paths return 404, oversized bodies return
413, and any unhandled handler exception is rendered to the caller as a generic
message that never echoes exception text (which could contain platform payload or
signature contents). The local log record for such an exception is redacted the
same way: only the exception's type name and a correlation id are logged, never
`str(exc)` or a traceback, since either could carry the same sensitive content the
HTTP response withholds.

`build_push_route` is the reusable scaffold every WebSub/PubSubHubbub-style push
platform (starting with YouTube) plugs into: a paired GET (subscription challenge)
and POST (verified notification) route built from a caller-supplied `PushSubscription`.
Verification always runs before ingestion — a GET whose challenge is not recognized
gets 404, not an echoed challenge; a POST whose signature does not verify against the
shared secret gets 403 and `handle_notification` is never called. This module stays
platform-neutral: `PushSubscription`'s callables are supplied by the platform-specific
connector module (for example `krubit.integrations.youtube`), never imported here.

`build_oauth_redirect_route` and `build_signed_webhook_route` extend the same
scaffold to member-facing OAuth (starting with Meta): a single GET authorization
redirect whose query-parameter handling is fully owned by the caller-supplied
`OAuthRedirect`, and a single signed POST webhook (for example a platform's
deauthorization/data-deletion callback) whose signature is verified before
`handle_notification` ever runs, exactly like `build_push_route`'s POST half.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from aiohttp import web

_MAX_BODY_BYTES = 1_048_576  # 1 MiB
_REQUEST_TIMEOUT_SECONDS = 10.0
_MIN_PORT = 1
_MAX_PORT = 65_535

_logger = logging.getLogger(__name__)

RouteHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class CallbackServerError(ValueError):
    """Raised for invalid callback server configuration."""


@dataclass(frozen=True, slots=True)
class CallbackRoute:
    """One registered callback endpoint: an exact path, method, and handler."""

    path: str
    method: str
    handler: RouteHandler

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise CallbackServerError("route path must not be blank")
        if not self.method.strip():
            raise CallbackServerError("route method must not be blank")


def _read_body_then_handle(handler: RouteHandler) -> RouteHandler:
    """Wrap `handler` so the 1 MiB body limit is enforced before it ever runs."""

    async def wrapped(request: web.Request) -> web.StreamResponse:
        await request.read()
        return await handler(request)

    return wrapped


@web.middleware
async def _redacted_errors_middleware(
    request: web.Request, handler: RouteHandler
) -> web.StreamResponse:
    try:
        return await asyncio.wait_for(handler(request), timeout=_REQUEST_TIMEOUT_SECONDS)
    except web.HTTPException:
        raise
    except TimeoutError:
        return web.json_response({"error": "request timed out"}, status=504)
    except Exception as exc:
        correlation_id = uuid.uuid4().hex
        _logger.error(
            "unhandled callback ingress error [correlation_id=%s, kind=%s]",
            correlation_id,
            type(exc).__name__,
        )
        return web.json_response(
            {"error": "internal error", "correlation_id": correlation_id}, status=500
        )


class CallbackServer:
    """Binds an aiohttp server exposing registered platform callback routes."""

    def __init__(
        self,
        *,
        public_base_url: str | None,
        port: int | None,
        routes: tuple[CallbackRoute, ...] = (),
        bind_host: str = "127.0.0.1",
    ) -> None:
        if public_base_url is not None and not public_base_url.startswith("https://"):
            raise CallbackServerError("callback public base URL must use https")
        if port is not None and port != 0 and not (_MIN_PORT <= port <= _MAX_PORT):
            raise CallbackServerError(f"callback port must be between {_MIN_PORT} and {_MAX_PORT}")
        self._public_base_url = public_base_url
        self._port = port
        self._routes = routes
        self._bind_host = bind_host
        self._runner: web.AppRunner | None = None

    @property
    def enabled(self) -> bool:
        """Whether a port is configured -- the only thing binding requires.

        A public base URL is validated when present (see `__init__`) but is not
        required to bind: a health-only server has no public base URL at all.
        """
        return self._port is not None

    def build_app(self) -> web.Application:
        """Build the aiohttp application, independent of whether it is ever bound."""
        # Disable aiohttp's default access logger to prevent secrets in query strings
        # from being logged (e.g., OAuth code/state parameters).
        logging.getLogger("aiohttp.access").disabled = True

        app = web.Application(
            client_max_size=_MAX_BODY_BYTES, middlewares=[_redacted_errors_middleware]
        )
        for route in self._routes:
            app.router.add_route(
                route.method, route.path, _read_body_then_handle(route.handler)
            )
        return app

    async def start(self) -> None:
        """Bind and start serving, or do nothing when not fully configured."""
        if not self.enabled or self._runner is not None:
            return
        app = self.build_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._bind_host, self._port)
        await site.start()
        self._runner = runner

    async def close(self) -> None:
        """Stop serving and release the bound socket, if any."""
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None


def build_health_route(path: str = "/health") -> CallbackRoute:
    """Build a GET route that always answers 200, for a platform's own liveness
    probe (for example Railway's `healthcheckPath`) -- distinct from Railway's
    deployment-status field, which only reflects whether the image built and
    launched, not whether the process inside is still running. Deliberately does
    no I/O and checks no downstream dependency: its only job is proving the
    process is alive and the event loop is responsive enough to answer a request,
    which is exactly the gap a build-only deploy status cannot see.
    """

    async def handle_get(request: web.Request) -> web.StreamResponse:
        return web.Response(status=200, text="ok")

    return CallbackRoute(path=path, method="GET", handler=handle_get)


@dataclass(frozen=True, slots=True)
class PushSubscription:
    """A push ingress endpoint's challenge/signature verification behavior.

    `verify_challenge` answers a GET subscription-confirmation request from the
    request's query parameters, returning the literal challenge text to echo back, or
    `None` to reject an unrecognized or invalid request. `verify_signature` validates a
    POST notification body (and its raw `X-Hub-Signature` header value, `None` if
    absent) against the shared secret established at subscribe time. Both are pure: no
    I/O, so a forged or replayed request is rejected before any ingestion logic runs.
    """

    verify_challenge: Callable[[Mapping[str, str]], str | None]
    verify_signature: Callable[[bytes, str | None], bool]


def build_push_route(
    *,
    path: str,
    subscription: PushSubscription,
    handle_notification: Callable[[bytes], Awaitable[None]],
) -> tuple[CallbackRoute, CallbackRoute]:
    """Build the paired GET (challenge) and POST (verified notification) routes for one
    WebSub/PubSubHubbub-style push endpoint.

    The GET route answers a subscribe/unsubscribe confirmation by echoing back the
    challenge only when `subscription.verify_challenge` recognizes the request;
    anything else is a 404 rather than an oracle for probing valid topics. The POST
    route verifies `subscription.verify_signature` against the raw body BEFORE calling
    `handle_notification` — an unverified body is rejected with 403 and never reaches
    ingestion. Both routes share `path`, matching the single callback URL a platform's
    subscription is configured against.
    """

    async def handle_get(request: web.Request) -> web.StreamResponse:
        query = {key: value for key, value in request.query.items()}
        challenge = subscription.verify_challenge(query)
        if challenge is None:
            return web.Response(status=404)
        return web.Response(text=challenge, status=200)

    async def handle_post(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        signature = request.headers.get("X-Hub-Signature")
        if not subscription.verify_signature(body, signature):
            return web.Response(status=403)
        await handle_notification(body)
        return web.Response(status=204)

    return (
        CallbackRoute(path=path, method="GET", handler=handle_get),
        CallbackRoute(path=path, method="POST", handler=handle_post),
    )


@dataclass(frozen=True, slots=True)
class OAuthRedirect:
    """One OAuth authorization-code redirect's state validation and code exchange.

    `handle_redirect` receives the callback GET's raw query parameters and returns the
    response body text shown to the member who completed the browser redirect. It
    raises `ValueError` for anything that fails state validation (expired, already
    used, tampered, or guild/member-mismatched) or code exchange; the route renders
    that as a generic 400 rather than echoing the raised message, which could carry
    provider diagnostic text.
    """

    handle_redirect: Callable[[Mapping[str, str]], Awaitable[str]]


def build_oauth_redirect_route(*, path: str, redirect: OAuthRedirect) -> CallbackRoute:
    """Build the single GET route for one OAuth authorization-code redirect endpoint."""

    async def handle_get(request: web.Request) -> web.StreamResponse:
        query = {key: value for key, value in request.query.items()}
        try:
            body = await redirect.handle_redirect(query)
        except ValueError:
            return web.Response(status=400, text="authorization request could not be completed")
        return web.Response(status=200, text=body)

    return CallbackRoute(path=path, method="GET", handler=handle_get)


@dataclass(frozen=True, slots=True)
class SignedWebhook:
    """A signed webhook POST's verification and ingestion behavior.

    Mirrors `PushSubscription`'s POST half for platforms (starting with Meta) whose
    webhook is a single signed POST with no GET challenge step. `verify_signature`
    always runs against the raw body before `handle_notification` is ever called.
    """

    verify_signature: Callable[[bytes, str | None], bool]
    handle_notification: Callable[[bytes], Awaitable[None]]


def build_signed_webhook_route(
    *, path: str, header_name: str, webhook: SignedWebhook
) -> CallbackRoute:
    """Build the single POST route for one signed webhook endpoint.

    An unverified body is rejected with 403 and `handle_notification` is never
    called, matching `build_push_route`'s POST verification-before-ingestion order.
    """

    async def handle_post(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        signature = request.headers.get(header_name)
        if not webhook.verify_signature(body, signature):
            return web.Response(status=403)
        await webhook.handle_notification(body)
        return web.Response(status=204)

    return CallbackRoute(path=path, method="POST", handler=handle_post)


@dataclass(frozen=True, slots=True)
class SignedFormRequest:
    """A form-posted signed request's verification and ingestion behavior.

    Mirrors `SignedWebhook` but for platforms (Meta's deauthorization/data-deletion
    callbacks) that sign a single form field's value rather than the raw request
    body — `verify_and_parse` receives that field's raw string value and returns
    the parsed, verified payload, or `None` to reject.
    """

    verify_and_parse: Callable[[str], Mapping[str, object] | None]
    handle_notification: Callable[[Mapping[str, object]], Awaitable[web.StreamResponse]]


def build_signed_form_route(
    *, path: str, field_name: str, webhook: SignedFormRequest
) -> CallbackRoute:
    """Build the single POST route for one form-signed-request endpoint.

    An unverified or missing field is rejected with 403 and `handle_notification`
    is never called, matching every other verify-before-ingest route in this
    module.
    """

    async def handle_post(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        raw_value = form.get(field_name)
        if not isinstance(raw_value, str):
            return web.Response(status=403)
        parsed = webhook.verify_and_parse(raw_value)
        if parsed is None:
            return web.Response(status=403)
        return await webhook.handle_notification(parsed)

    return CallbackRoute(path=path, method="POST", handler=handle_post)
