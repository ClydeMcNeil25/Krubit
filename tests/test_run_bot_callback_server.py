from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from krubit.__main__ import _run_bot  # pyright: ignore[reportPrivateUsage]
from krubit.config import Settings

pytestmark = pytest.mark.asyncio


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        application_id=1,
        bot_token="t",
        database_path=Path("unused.db"),
        creator_signals_enabled=True,
        callback_public_base_url="https://example.test",
        callback_port=8080,
        credential_encryption_key="a" * 32,
        tiktok_client_key="ck",
        tiktok_client_secret="cs",
        meta_app_id=None,
        meta_app_secret=None,
        live_signals_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def test_run_bot_starts_and_closes_callback_server_once():
    with patch("krubit.__main__.CallbackServer") as mock_server_cls, \
         patch("krubit.__main__.SQLiteStore.open", new_callable=AsyncMock), \
         patch("krubit.__main__.KrubitBot") as mock_bot_cls:
        mock_server = AsyncMock()
        mock_server_cls.return_value = mock_server
        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await _run_bot(_settings())

        # _run_bot constructs two CallbackServer instances (the OAuth callback
        # server and the health-check server); both share this mock instance
        # since mock_server_cls.return_value is fixed, so start/close each land
        # twice here -- see test_run_bot_starts_and_closes_health_server_on_its_own_port
        # for a case that distinguishes the two by their constructor kwargs.
        assert mock_server.start.await_count == 2
        assert mock_server.close.await_count == 2


async def test_run_bot_does_not_start_callback_server_when_unconfigured():
    with patch("krubit.__main__.CallbackServer") as mock_server_cls, \
         patch("krubit.__main__.SQLiteStore.open", new_callable=AsyncMock), \
         patch("krubit.__main__.KrubitBot") as mock_bot_cls:
        mock_server = AsyncMock()
        mock_server.enabled = False
        mock_server_cls.return_value = mock_server
        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await _run_bot(
            _settings(callback_public_base_url=None, callback_port=None, health_check_port=None)
        )

        callback_call = next(
            call for call in mock_server_cls.call_args_list
            if call.kwargs["public_base_url"] is None and call.kwargs["port"] is None
        )
        assert callback_call.kwargs["public_base_url"] is None


async def test_run_bot_starts_and_closes_health_server_on_its_own_port():
    with patch("krubit.__main__.CallbackServer") as mock_server_cls, \
         patch("krubit.__main__.SQLiteStore.open", new_callable=AsyncMock), \
         patch("krubit.__main__.KrubitBot") as mock_bot_cls:
        mock_server = AsyncMock()
        mock_server_cls.return_value = mock_server
        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await _run_bot(_settings(callback_port=9999, health_check_port=8080))

        health_call = next(
            call for call in mock_server_cls.call_args_list
            if call.kwargs["port"] == 8080
        )
        assert health_call.kwargs["public_base_url"] is None
        assert health_call.kwargs["bind_host"] == "0.0.0.0"
        assert len(health_call.kwargs["routes"]) == 1
