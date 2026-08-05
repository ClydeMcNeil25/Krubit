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
) -> None:
    response = await client.post("/callbacks/boom", data=b"x")
    assert response.status == 500
    body = await response.text()
    assert "secret-token-xyz" not in body


def test_callback_server_rejects_non_https_public_base_url() -> None:
    with pytest.raises(CallbackServerError, match="https"):
        CallbackServer(public_base_url="http://callbacks.example.com", port=8443)


def test_callback_server_rejects_out_of_range_port() -> None:
    with pytest.raises(CallbackServerError, match="port"):
        CallbackServer(public_base_url="https://callbacks.example.com", port=0)


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
