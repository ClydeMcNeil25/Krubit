"""Krubit command-line operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict

import aiohttp

from krubit.config import Settings, SettingsError
from krubit.discord.bot import KrubitBot
from krubit.discord.install import install_url
from krubit.integrations.twitch import TwitchHelixClient
from krubit.security.tls import system_ssl_context
from krubit.services.foundation import FoundationService
from krubit.storage.sqlite import SQLiteStore


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
        connector = aiohttp.TCPConnector(ssl=system_ssl_context())
        bot = KrubitBot(settings, FoundationService(store), connector=connector, twitch=twitch)
        await bot.start(settings.require_token())
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        for resource in (bot, store, connector, twitch_session, twitch_connector):
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
