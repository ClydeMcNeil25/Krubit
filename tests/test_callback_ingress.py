import logging
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krubit.web.callbacks import CallbackRoute, CallbackServer, CallbackServerError


async def _youtube_handler(request: web.Request) -> web.StreamResponse:
    return web.json_response({"received": True})


async def _boom_handler(request: web.Request) -> web.StreamResponse:
    raise RuntimeError("provider secret-token-xyz leaked in a stack trace")


def _build_server() -> CallbackServer:
    return CallbackServer(
        public_base_url="https://callbacks.example.com",
        port=8443,
        routes=(
            CallbackRoute(path="/callbacks/youtube", method="POST", handler=_youtube_handler),
            CallbackRoute(path="/callbacks/boom", method="POST", handler=_boom_handler),
        ),
    )


@pytest.fixture
async def client() -> AsyncIterator[TestClient[web.Request, web.Application]]:
    server = _build_server()
    app = server.build_app()
    test_client = TestClient[web.Request, web.Application](TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


async def test_callback_ingress_rejects_oversized_or_unregistered_requests(
    client: TestClient[web.Request, web.Application],
) -> None:
    assert (await client.post("/callbacks/unknown", data=b"x")).status == 404
    assert (await client.post("/callbacks/youtube", data=b"x" * 1_048_577)).status == 413


async def test_callback_ingress_accepts_registered_route_within_body_limit(
    client: TestClient[web.Request, web.Application],
) -> None:
    response = await client.post("/callbacks/youtube", data=b"x" * 1_048_576)
    assert response.status == 200
    assert (await response.json()) == {"received": True}


async def test_callback_ingress_rejects_wrong_method_on_a_registered_route(
    client: TestClient[web.Request, web.Application],
) -> None:
    response = await client.get("/callbacks/youtube")
    assert response.status == 405


async def test_callback_ingress_redacts_unhandled_handler_errors(
    client: TestClient[web.Request, web.Application],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        response = await client.post("/callbacks/boom", data=b"x")
    assert response.status == 500
    body = await response.text()
    assert "secret-token-xyz" not in body
    assert "secret-token-xyz" not in caplog.text


async def test_callback_ingress_error_response_carries_a_correlation_id_not_the_exception(
    client: TestClient[web.Request, web.Application],
) -> None:
    response = await client.post("/callbacks/boom", data=b"x")
    payload = await response.json()
    assert payload["error"] == "internal error"
    assert isinstance(payload["correlation_id"], str) and payload["correlation_id"]
    assert "RuntimeError" not in payload["correlation_id"]


def test_callback_server_rejects_non_https_public_base_url() -> None:
    with pytest.raises(CallbackServerError, match="https"):
        CallbackServer(public_base_url="http://callbacks.example.com", port=8443)


def test_callback_server_rejects_out_of_range_port() -> None:
    with pytest.raises(CallbackServerError, match="port"):
        CallbackServer(public_base_url="https://callbacks.example.com", port=65536)


def test_callback_server_disabled_without_base_url_and_port() -> None:
    assert CallbackServer(public_base_url=None, port=None).enabled is False
    assert (
        CallbackServer(public_base_url="https://callbacks.example.com", port=None).enabled
        is False
    )
    assert CallbackServer(public_base_url=None, port=8443).enabled is False


async def test_callback_server_start_is_a_no_op_when_not_fully_configured() -> None:
    server = CallbackServer(public_base_url=None, port=None)
    await server.start()
    assert server.enabled is False
    await server.close()


async def test_callback_server_defaults_to_loopback_bind_host() -> None:
    server = CallbackServer(public_base_url="https://example.test", port=0)
    assert server._bind_host == "127.0.0.1"


async def test_callback_server_accepts_explicit_bind_host() -> None:
    server = CallbackServer(public_base_url="https://example.test", port=0, bind_host="0.0.0.0")
    assert server._bind_host == "0.0.0.0"


async def test_callback_server_second_start_is_a_noop() -> None:
    server = CallbackServer(public_base_url="https://example.test", port=0)
    await server.start()
    runner_after_first_start = server._runner
    await server.start()
    assert server._runner is runner_after_first_start
    await server.close()


async def test_callback_server_access_log_is_silent_for_query_string_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handle(request: web.Request) -> web.Response:
        return web.Response(status=200)

    server = CallbackServer(
        public_base_url="https://example.test",
        port=0,
        routes=(CallbackRoute(path="/cb", method="GET", handler=handle),),
    )
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        with caplog.at_level(logging.INFO, logger="aiohttp.access"):
            await client.get("/cb?code=super-secret-code&state=super-secret-state")
    access_records = [r for r in caplog.records if r.name == "aiohttp.access"]
    assert access_records == []
