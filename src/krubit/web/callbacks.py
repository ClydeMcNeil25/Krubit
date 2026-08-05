"""Minimal aiohttp ingress for platform push/webhook callbacks.

`CallbackServer` binds nothing unless both a public HTTPS base URL and a port are
configured, so a deployment with no social platform credentials never opens a
listening socket. Once configured, it enforces an HTTPS public base, a 1 MiB body
limit per request, one HTTP method per registered route, a request timeout, and
redacted error responses: unregistered paths return 404, oversized bodies return
413, and any unhandled handler exception is rendered to the caller as a generic
message that never echoes exception text (which could contain platform payload or
signature contents). The local log record for such an exception is redacted the
same way: only the exception's type name and a correlation id are logged, never
`str(exc)` or a traceback, since either could carry the same sensitive content the
HTTP response withholds.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
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
    ) -> None:
        if public_base_url is not None and not public_base_url.startswith("https://"):
            raise CallbackServerError("callback public base URL must use https")
        if port is not None and not (_MIN_PORT <= port <= _MAX_PORT):
            raise CallbackServerError(f"callback port must be between {_MIN_PORT} and {_MAX_PORT}")
        self._public_base_url = public_base_url
        self._port = port
        self._routes = routes
        self._runner: web.AppRunner | None = None

    @property
    def enabled(self) -> bool:
        """Whether both a public base URL and a port are configured."""
        return self._public_base_url is not None and self._port is not None

    def build_app(self) -> web.Application:
        """Build the aiohttp application, independent of whether it is ever bound."""
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
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._port)
        await site.start()
        self._runner = runner

    async def close(self) -> None:
        """Stop serving and release the bound socket, if any."""
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
