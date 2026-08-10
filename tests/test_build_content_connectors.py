"""Unit test for krubit.__main__._build_content_connectors's Instagram/
Threads/TikTok wiring."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest

from krubit.__main__ import _build_content_connectors
from krubit.config import Settings
from krubit.domain.creator_signals import Platform
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore


@pytest.mark.asyncio
async def test_build_content_connectors_includes_credential_bridge_platforms_when_vault_present(
    tmp_path: Path,
) -> None:
    settings = Settings(application_id=123, database_path=tmp_path / "krubit.db")
    store = await SQLiteStore.open(settings.database_path)
    await store.initialize()
    vault = CredentialVault.from_env_key("test-key")
    async with aiohttp.ClientSession() as session:
        try:
            connectors = _build_content_connectors(settings, session, store, vault)
        finally:
            await store.close()

    assert Platform.INSTAGRAM in connectors
    assert Platform.THREADS in connectors
    assert Platform.TIKTOK in connectors


@pytest.mark.asyncio
async def test_build_content_connectors_omits_credential_bridge_platforms_without_a_vault(
    tmp_path: Path,
) -> None:
    settings = Settings(application_id=123, database_path=tmp_path / "krubit.db")
    store = await SQLiteStore.open(settings.database_path)
    await store.initialize()
    async with aiohttp.ClientSession() as session:
        try:
            connectors = _build_content_connectors(settings, session, store, vault=None)
        finally:
            await store.close()

    assert Platform.INSTAGRAM not in connectors
    assert Platform.THREADS not in connectors
    assert Platform.TIKTOK not in connectors
