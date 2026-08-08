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

        mock_server.start.assert_awaited_once()
        mock_server.close.assert_awaited_once()


async def test_run_bot_does_not_start_callback_server_when_unconfigured():
    with patch("krubit.__main__.CallbackServer") as mock_server_cls, \
         patch("krubit.__main__.SQLiteStore.open", new_callable=AsyncMock), \
         patch("krubit.__main__.KrubitBot") as mock_bot_cls:
        mock_server = AsyncMock()
        mock_server.enabled = False
        mock_server_cls.return_value = mock_server
        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await _run_bot(_settings(callback_public_base_url=None))

        mock_server_cls.assert_called_once()
        call_kwargs = mock_server_cls.call_args.kwargs
        assert call_kwargs["public_base_url"] is None
