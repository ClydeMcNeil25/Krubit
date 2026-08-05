"""Stable, read-only Discord guild inventory capture."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import discord

from krubit.domain.companion import CoverageIssue
from krubit.domain.creator_signals import CapabilityState
from krubit.domain.models import Card, CardField, JSONValue
from krubit.integrations.base import ConnectorHealth

# Fixed, per-state descriptions — mirrors `ConnectorFailure.safe_detail`. Never derived
# from `ConnectorHealth.detail`: that field is caller-supplied and, unlike
# `ConnectorFailure`, carries no built-in redaction guarantee, so a raw provider error
# string placed there (for example by a connector's exception handler) must never reach
# this rendering.
_SAFE_CAPABILITY_DETAIL: dict[CapabilityState, str] = {
    CapabilityState.READY: "capability is ready",
    CapabilityState.UNCONFIGURED: "capability is not configured",
    CapabilityState.AUTHORIZATION_REQUIRED: "capability requires authorization",
    CapabilityState.APPROVAL_REQUIRED: "capability requires platform approval",
    CapabilityState.DEGRADED: "capability is degraded",
    CapabilityState.QUOTA_LIMITED: "capability is quota limited",
    CapabilityState.UNSUPPORTED: "capability is not supported",
}

_MUTATION_PERMISSIONS = (
    "administrator",
    "ban_members",
    "kick_members",
    "manage_channels",
    "manage_guild",
    "manage_messages",
    "manage_roles",
    "manage_webhooks",
    "moderate_members",
)


@dataclass(frozen=True, slots=True)
class InventoryCapture:
    content: dict[str, JSONValue]
    coverage: tuple[CoverageIssue, ...]


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _id_sort(item: dict[str, JSONValue]) -> int:
    resource_id = item.get("id")
    if not isinstance(resource_id, str):
        raise ValueError("normalized resource id must be a string")
    return int(resource_id)


def _overwrites(channel: discord.abc.GuildChannel) -> list[JSONValue]:
    values: list[JSONValue] = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        values.append(
            {
                "target_id": str(target.id),
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "allow": str(allow.value),
                "deny": str(deny.value),
            }
        )
    return sorted(values, key=lambda item: int(str(item["target_id"])))  # type: ignore[index]


def _channel(channel: discord.abc.GuildChannel) -> dict[str, JSONValue]:
    parent_id = getattr(channel, "category_id", None)
    return {
        "id": str(channel.id),
        "name": channel.name,
        "type": str(channel.type),
        "position": channel.position,
        "parent_id": str(parent_id) if parent_id is not None else None,
        "topic": getattr(channel, "topic", None),
        "nsfw": bool(getattr(channel, "nsfw", False)),
        "overwrites": _overwrites(channel),
    }


def _role(role: discord.Role) -> dict[str, JSONValue]:
    return {
        "id": str(role.id),
        "name": role.name,
        "position": role.position,
        "color": role.color.value,
        "managed": role.managed,
        "permissions": str(role.permissions.value),
    }


def _scheduled_event(event: discord.ScheduledEvent) -> dict[str, JSONValue]:
    return {
        "id": str(event.id),
        "name": event.name,
        "status": str(event.status),
        "channel_id": str(event.channel_id) if event.channel_id is not None else None,
        "start_time": _timestamp(event.start_time),
        "end_time": _timestamp(event.end_time),
    }


async def capture_inventory(
    guild: discord.Guild,
    *,
    required_permissions: discord.Permissions,
    configured_channel_id: int | None,
    live_channel_id: int | None = None,
    streaming_role_id: int | None = None,
    presence_intent: bool | None = None,
    twitch_credentials: bool | None = None,
    twitch_available: bool | None = None,
) -> InventoryCapture:
    """Capture deterministic facts and explicit access limitations for one guild."""

    issues: list[CoverageIssue] = []
    webhooks: list[dict[str, JSONValue]] = []
    automod_rules: list[dict[str, JSONValue]] = []
    try:
        visible_webhooks = await guild.webhooks()
        webhooks = sorted(
            [
                {
                    "id": str(webhook.id),
                    "channel_id": (
                        str(webhook.channel_id) if webhook.channel_id is not None else None
                    ),
                    "name": webhook.name,
                    "type": str(webhook.type),
                }
                for webhook in visible_webhooks
            ],
            key=_id_sort,
        )
    except discord.HTTPException as exc:
        issues.append(CoverageIssue("webhooks", "limited", f"discord_http_{exc.status}"))

    try:
        visible_rules = await guild.fetch_automod_rules()
        automod_rules = sorted(
            [
                {
                    "id": str(rule.id),
                    "name": rule.name,
                    "enabled": rule.enabled,
                    "event_type": str(rule.event_type),
                    "trigger_type": str(rule.trigger.type),
                }
                for rule in visible_rules
            ],
            key=_id_sort,
        )
    except discord.HTTPException as exc:
        issues.append(CoverageIssue("automod_rules", "limited", f"discord_http_{exc.status}"))

    granted = guild.me.guild_permissions
    required_names = sorted(name for name, enabled in required_permissions if enabled)
    missing_names = sorted(name for name in required_names if not getattr(granted, name, False))
    required_values: list[JSONValue] = []
    required_values.extend(required_names)
    missing_values: list[JSONValue] = []
    missing_values.extend(missing_names)
    mutation_values: list[JSONValue] = []
    mutation_values.extend(
        name
        for name in _MUTATION_PERMISSIONS
        if getattr(granted, name, False) and name not in required_names
    )
    configured: dict[str, JSONValue] | None = (
        None
        if configured_channel_id is None
        else {
            "id": str(configured_channel_id),
            "present": guild.get_channel(configured_channel_id) is not None,
        }
    )
    live_signal: dict[str, JSONValue] | None = None
    if any(
        value is not None
        for value in (
            live_channel_id,
            streaming_role_id,
            presence_intent,
            twitch_credentials,
            twitch_available,
        )
    ):
        role = guild.get_role(streaming_role_id) if streaming_role_id is not None else None
        top_role = getattr(guild.me, "top_role", None)
        role_hierarchy: bool | None = None
        if role is not None and top_role is not None:
            role_hierarchy = getattr(role, "position", -1) < getattr(top_role, "position", -1)
        live_signal = {
            "channel": (
                None
                if live_channel_id is None
                else {
                    "id": str(live_channel_id),
                    "present": guild.get_channel(live_channel_id) is not None,
                }
            ),
            "role": (
                None
                if streaming_role_id is None
                else {"id": str(streaming_role_id), "present": role is not None}
            ),
            "presence_intent": presence_intent,
            "twitch_credentials": twitch_credentials,
            "twitch_available": twitch_available,
            "manage_roles": bool(getattr(granted, "manage_roles", False)),
            "role_hierarchy": role_hierarchy,
            "mention_everyone": bool(getattr(granted, "mention_everyone", False)),
        }
    bot_permissions: dict[str, JSONValue] = {
        "granted": str(granted.value),
        "required": required_values,
        "missing_required": missing_values,
        "unexpected_mutation": mutation_values,
    }
    role_values: list[JSONValue] = []
    role_values.extend(sorted((_role(role) for role in guild.roles), key=_id_sort))
    channel_values: list[JSONValue] = []
    channel_values.extend(sorted((_channel(channel) for channel in guild.channels), key=_id_sort))
    event_values: list[JSONValue] = []
    event_values.extend(
        sorted((_scheduled_event(event) for event in guild.scheduled_events), key=_id_sort)
    )
    webhook_values: list[JSONValue] = []
    webhook_values.extend(webhooks)
    automod_values: list[JSONValue] = []
    automod_values.extend(automod_rules)
    content: dict[str, JSONValue] = {
        "guild": {"id": str(guild.id), "name": guild.name},
        "roles": role_values,
        "channels": channel_values,
        "scheduled_events": event_values,
        "webhooks": webhook_values,
        "automod_rules": automod_values,
        "bot_permissions": bot_permissions,
        "configured_channel": configured,
    }
    if live_signal is not None:
        content["live_signal"] = live_signal
    return InventoryCapture(
        content=content,
        coverage=tuple(sorted(issues, key=lambda item: item.section)),
    )


def render_connector_health(health: ConnectorHealth) -> Card:
    """Render one `ConnectorHealth` as a safe, secret-free card.

    Only `capability` and `state` ever reach the returned `Card` — `health.detail` is
    never read here, regardless of what it contains, matching the same never-echo-raw-
    diagnostic-text discipline `ConnectorFailure.safe_detail` already enforces for
    connector errors.
    """
    safe_detail = _SAFE_CAPABILITY_DETAIL[health.state]
    description = f"{health.capability.value}: {health.state.value} — {safe_detail}"
    return Card(
        kind="fetched",
        title="Fetched: Integration Status",
        description=description,
        fields=(
            CardField("Capability", health.capability.value, True),
            CardField("State", health.state.value, True),
        ),
    )
