"""Krubit command-line operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict

import aiohttp
import discord

from krubit.config import Settings, SettingsError
from krubit.discord.bot import KrubitBot
from krubit.discord.install import install_url
from krubit.domain.creator_signals import Platform
from krubit.integrations.base import Connector
from krubit.integrations.bluesky import BlueskyConnector
from krubit.integrations.twitch import TwitchHelixClient
from krubit.integrations.x import XConnector
from krubit.integrations.youtube import YouTubeConnector
from krubit.security.credential_vault import CredentialVault
from krubit.security.tls import system_ssl_context
from krubit.services.foundation import FoundationService
from krubit.services.live_signals import migrate_all_twitch_content
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer
from krubit.web.wiring import build_callback_routes

_logger = logging.getLogger(__name__)


def _build_content_connectors(
    settings: Settings, session: aiohttp.ClientSession
) -> dict[Platform, Connector]:
    """Build the statically-credentialed connectors `ConnectorScheduler` can poll.

    Only the platforms with a single bot-wide credential in `Settings` are wired here:
    YouTube (API key), X (bearer token), and Bluesky (no credential at all). Meta and
    TikTok connectors need one access token *per enrolled creator account* resolved
    from `connector_authorizations`/`CredentialVault` at poll time, not one fixed
    bot-wide token — that per-account credential resolution is a distinct feature this
    task does not build, so those platforms are intentionally left unscheduled for now
    rather than wired with a token that cannot be correct for more than one account.

    Callers must gate this on `settings.creator_signals_enabled` themselves (see
    `_run_bot`) — this function does not check the flag, since `KrubitBot` also enforces
    it independently for any caller that constructs it directly (for example, a test).
    """
    connectors: dict[Platform, Connector] = {}
    if settings.youtube_api_key is not None:
        connectors[Platform.YOUTUBE] = YouTubeConnector(session, settings.youtube_api_key)
    if settings.x_bearer_token is not None:
        connectors[Platform.X] = XConnector(session, settings.x_bearer_token)
    connectors[Platform.BLUESKY] = BlueskyConnector(session)
    return connectors


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="krubit", description="Krubit Phase 0 operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="connect Krubit to Discord")
    subparsers.add_parser("init-db", help="initialize the SQLite schema")
    subparsers.add_parser("install-url", help="print the least-privilege Discord install URL")

    enable = subparsers.add_parser("enable-guild", help="enable or disable a Discord guild")
    enable.add_argument("guild_id", type=_positive_int)
    enable.add_argument("--disable", action="store_true")

    status = subparsers.add_parser("status", help="print a guild's foundation status")
    status.add_argument("guild_id", type=_positive_int)

    signal = subparsers.add_parser(
        "emit-test-signal", help="print a schema-valid Zariya foundation signal"
    )
    signal.add_argument("guild_id", type=_positive_int)
    signal.add_argument("--actor-id", type=_positive_int, required=True)
    return parser


async def _database_command(args: argparse.Namespace, settings: Settings) -> int:
    store = await SQLiteStore.open(settings.database_path)
    try:
        await store.initialize()
        service = FoundationService(store)
        if args.command == "init-db":
            print(json.dumps({"database": str(settings.database_path), "initialized": True}))
        elif args.command == "enable-guild":
            enabled = not bool(args.disable)
            await store.set_guild_enabled(int(args.guild_id), enabled)
            print(json.dumps({"guild_id": str(args.guild_id), "enabled": enabled}))
        elif args.command == "status":
            snapshot = await service.status(int(args.guild_id))
            payload = asdict(snapshot)
            payload["guild_id"] = str(snapshot.guild_id)
            print(json.dumps(payload, sort_keys=True))
        elif args.command == "emit-test-signal":
            signal = await service.test_signal(
                int(args.guild_id), int(args.actor_id), can_manage_guild=True
            )
            print(json.dumps(signal.to_dict(), sort_keys=True))
        else:
            raise RuntimeError(f"unsupported database command: {args.command}")
    finally:
        await store.close()
    return 0


async def _run_bot(settings: Settings) -> int:
    twitch_session: aiohttp.ClientSession | None = None
    twitch_connector: aiohttp.TCPConnector | None = None
    connector: aiohttp.TCPConnector | None = None
    content_session: aiohttp.ClientSession | None = None
    content_tcp_connector: aiohttp.TCPConnector | None = None
    oauth_session: aiohttp.ClientSession | None = None
    oauth_tcp_connector: aiohttp.TCPConnector | None = None
    callback_server: CallbackServer | None = None
    store: SQLiteStore | None = None
    bot: KrubitBot | None = None
    twitch = None
    primary_error: BaseException | None = None
    try:
        if settings.live_signals_enabled:
            client_id, client_secret = settings.require_twitch_credentials()
            twitch_connector = aiohttp.TCPConnector(ssl=system_ssl_context())
            twitch_session = aiohttp.ClientSession(connector=twitch_connector)
            twitch = TwitchHelixClient(twitch_session, client_id, client_secret)
        store = await SQLiteStore.open(settings.database_path)
        await store.initialize()
        # Idempotent: safe to run on every boot, links Phase 2A Twitch history into
        # the unified content ledger without ever re-sending anything already
        # delivered. The Phase 2A tables themselves are never mutated by this.
        await migrate_all_twitch_content(store)
        content_tcp_connector = aiohttp.TCPConnector(ssl=system_ssl_context())
        content_session = aiohttp.ClientSession(connector=content_tcp_connector)
        content_connectors = (
            _build_content_connectors(settings, content_session)
            if settings.creator_signals_enabled
            else {}
        )

        vault = (
            CredentialVault.from_env_key(settings.credential_encryption_key)
            if settings.credential_encryption_key is not None
            else None
        )
        oauth_tcp_connector = aiohttp.TCPConnector(ssl=system_ssl_context())
        oauth_session = aiohttp.ClientSession(connector=oauth_tcp_connector)
        callback_routes = build_callback_routes(settings, store, vault, oauth_session)
        callback_server = CallbackServer(
            public_base_url=settings.callback_public_base_url,
            port=settings.callback_port,
            routes=callback_routes,
            bind_host=settings.callback_bind_host,
        )
        await callback_server.start()

        connector = aiohttp.TCPConnector(ssl=system_ssl_context())
        bot = KrubitBot(
            settings,
            FoundationService(store),
            connector=connector,
            twitch=twitch,
            content_connectors=content_connectors,
        )
        try:
            await bot.start(settings.require_token())
        except discord.PrivilegedIntentsRequired:
            # `KrubitBot.__init__` only requests the privileged Message Content
            # intent when `watchdog_enabled`, so this can only fire when the
            # operator turned Watchdog on but has not yet flipped Message Content on
            # in the Discord Developer Portal. Per the Phase 3 design doc, that
            # mismatch must degrade to join-signal-only detection, never crash the
            # whole process (`watchdog_enabled=False` never reaches this branch at
            # all, since it never requests the intent in the first place). Rebuild
            # and reconnect once without the privileged intent rather than letting
            # this exception propagate and take down every other Krubit capability
            # with it.
            if not settings.watchdog_enabled:
                raise
            _logger.warning(
                "KRUBIT_WATCHDOG_ENABLED=true but the privileged Message Content "
                "intent is not enabled for this application in the Discord "
                "Developer Portal (https://discord.com/developers/applications -> "
                "your application -> Bot -> Privileged Gateway Intents). "
                "Reconnecting without it: watch-window message inspection and "
                "spam-wave correlation are unavailable until it is enabled there, "
                "but Entry Sniff join-signal detection, watch-window expiry, "
                "raid/webhook-abuse/permission-risk detection, and every other "
                "Krubit capability continue to run normally."
            )
            await bot.close()
            connector = aiohttp.TCPConnector(ssl=system_ssl_context())
            bot = KrubitBot(
                settings,
                FoundationService(store),
                connector=connector,
                twitch=twitch,
                content_connectors=content_connectors,
                request_message_content_intent=False,
            )
            await bot.start(settings.require_token())
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        for resource in (
            callback_server,
            oauth_session,
            oauth_tcp_connector,
            bot,
            store,
            connector,
            twitch_session,
            twitch_connector,
            content_session,
            content_tcp_connector,
        ):
            if resource is None:
                continue
            try:
                await resource.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error
    return 0


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(environ)
        if args.command == "install-url":
            print(install_url(settings.application_id))
            return 0
        if args.command == "run":
            return asyncio.run(_run_bot(settings))
        return asyncio.run(_database_command(args, settings))
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
