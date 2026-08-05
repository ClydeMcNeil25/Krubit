"""Authority-enforcing creator registry service.

`CreatorRegistry` is the only place that decides who may add, pause, resume, or
transfer a creator account. It composes catalog capability facts with guild-scoped
persistence (`SQLiteStore`) and always records a redacted audit receipt after a
successful state change. It performs no I/O beyond `SQLiteStore` and never renders
Discord surfaces or touches secrets.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from krubit.domain.creator_signals import (
    Capability,
    CreatorAccount,
    Platform,
    RecognizedAccountUrl,
    creator_account_id,
)
from krubit.domain.models import JSONValue
from krubit.integrations.catalog import CATALOG
from krubit.storage.sqlite import SQLiteStore


class CreatorAuthorityError(PermissionError):
    """Raised when the acting member lacks authority for a creator registry action."""


def _require_authority(
    *,
    actor_member_id: int,
    owner_member_id: int,
    actor_is_admin: bool,
    actor_has_creator_role: bool,
) -> None:
    """Enforce: admins may act on any account; the Creator role only on one's own.

    Mirrors the design spec's Authority section: "Administrators may add, remove,
    pause, resume, transfer, route, or template any guild creator account. Members
    with the configured Creator role may manage only accounts owned by their own
    Discord member identity."
    """
    if actor_is_admin:
        return
    if actor_member_id != owner_member_id:
        raise CreatorAuthorityError(
            "managing another member's creator account requires administrator authority"
        )
    if not actor_has_creator_role:
        raise CreatorAuthorityError(
            "self-service creator management requires the configured Creator role"
        )


class CreatorRegistry:
    """Guild-scoped creator account authority and lifecycle."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def add_account(
        self,
        *,
        guild_id: int,
        actor_member_id: int,
        owner_member_id: int,
        actor_is_admin: bool,
        actor_has_creator_role: bool,
        recognized: RecognizedAccountUrl,
        resolved_external_id: str,
        now: datetime,
    ) -> CreatorAccount:
        """Register a resolved platform account, paused until baseline/authorization complete.

        Requires `actor_is_admin` when `owner_member_id` differs from the actor (managing
        another member's profile) and `actor_has_creator_role` for self-service.
        """
        _require_authority(
            actor_member_id=actor_member_id,
            owner_member_id=owner_member_id,
            actor_is_admin=actor_is_admin,
            actor_has_creator_role=actor_has_creator_role,
        )
        account = CreatorAccount(
            guild_id=guild_id,
            account_id=creator_account_id(recognized.platform, resolved_external_id),
            owner_member_id=owner_member_id,
            platform=recognized.platform,
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
            external_id=resolved_external_id,
            paused=True,
            created_at=now,
            updated_at=now,
        )
        saved = await self._store.save_creator_account(account)
        account_capability_state = CATALOG[recognized.platform].capability(Capability.ACCOUNT).state
        await self._record_receipt(
            guild_id=guild_id,
            account_id=saved.account_id,
            action="add_account",
            actor_member_id=actor_member_id,
            detail={
                "platform": recognized.platform.value,
                "owner_member_id": owner_member_id,
                "paused": True,
                "account_capability_state": account_capability_state.value,
            },
            now=now,
        )
        return saved

    async def pause_account(
        self,
        *,
        guild_id: int,
        actor_member_id: int,
        account_id: str,
        actor_is_admin: bool,
        actor_has_creator_role: bool,
        now: datetime,
    ) -> CreatorAccount:
        """Pause an account, stopping new monitoring while preserving its history."""
        return await self._set_paused(
            guild_id=guild_id,
            actor_member_id=actor_member_id,
            account_id=account_id,
            actor_is_admin=actor_is_admin,
            actor_has_creator_role=actor_has_creator_role,
            paused=True,
            action="pause_account",
            now=now,
        )

    async def resume_account(
        self,
        *,
        guild_id: int,
        actor_member_id: int,
        account_id: str,
        actor_is_admin: bool,
        actor_has_creator_role: bool,
        now: datetime,
    ) -> CreatorAccount:
        """Resume a paused account."""
        return await self._set_paused(
            guild_id=guild_id,
            actor_member_id=actor_member_id,
            account_id=account_id,
            actor_is_admin=actor_is_admin,
            actor_has_creator_role=actor_has_creator_role,
            paused=False,
            action="resume_account",
            now=now,
        )

    async def transfer_account(
        self,
        *,
        guild_id: int,
        actor_member_id: int,
        account_id: str,
        new_owner_member_id: int,
        actor_is_admin: bool,
        now: datetime,
    ) -> CreatorAccount:
        """Reassign an account's owner.

        Per the design spec, "Staff may transfer ownership through an explicit audited
        action" — transfer is always administrator-only, with no self-service path.
        """
        if not actor_is_admin:
            raise CreatorAuthorityError(
                "transferring a creator account requires administrator authority"
            )
        existing = await self._existing_account(guild_id, account_id)
        saved = await self._store.transfer_creator_account(
            guild_id, account_id, new_owner_member_id, now
        )
        await self._record_receipt(
            guild_id=guild_id,
            account_id=account_id,
            action="transfer_account",
            actor_member_id=actor_member_id,
            detail={
                "previous_owner_member_id": existing.owner_member_id,
                "new_owner_member_id": new_owner_member_id,
            },
            now=now,
        )
        return saved

    async def record_oauth_authorization(
        self,
        *,
        guild_id: int,
        actor_member_id: int,
        account_id: str,
        platform: Platform,
        capability: Capability,
        expires_at: datetime | None,
        now: datetime,
    ) -> CreatorAccount:
        """Unpause an account once a member completes its Meta OAuth authorization.

        Called only after the caller has already validated and single-use-consumed the
        signed OAuth state and exchanged the authorization code for a token — this
        method never receives the token itself (sealed or otherwise), only the fact that
        `capability` is now authorized for `account_id` and, if the provider supplied
        one, when that authorization expires. The audit receipt it records reflects the
        same: capability and expiry only, never a credential reference.
        """
        existing = await self._existing_account(guild_id, account_id)
        if existing.platform is not platform:
            raise ValueError(
                f"account {account_id!r} is registered for {existing.platform.value}, "
                f"not {platform.value}"
            )
        updated = replace(existing, paused=False, updated_at=now)
        saved = await self._store.save_creator_account(updated)
        await self._record_receipt(
            guild_id=guild_id,
            account_id=account_id,
            action="authorize_connector",
            actor_member_id=actor_member_id,
            detail={
                "capability": capability.value,
                "expires_at": expires_at.isoformat() if expires_at is not None else None,
            },
            now=now,
        )
        return saved

    async def _set_paused(
        self,
        *,
        guild_id: int,
        actor_member_id: int,
        account_id: str,
        actor_is_admin: bool,
        actor_has_creator_role: bool,
        paused: bool,
        action: str,
        now: datetime,
    ) -> CreatorAccount:
        existing = await self._existing_account(guild_id, account_id)
        _require_authority(
            actor_member_id=actor_member_id,
            owner_member_id=existing.owner_member_id,
            actor_is_admin=actor_is_admin,
            actor_has_creator_role=actor_has_creator_role,
        )
        updated = replace(existing, paused=paused, updated_at=now)
        saved = await self._store.save_creator_account(updated)
        await self._record_receipt(
            guild_id=guild_id,
            account_id=account_id,
            action=action,
            actor_member_id=actor_member_id,
            detail={"paused": paused},
            now=now,
        )
        return saved

    async def _existing_account(self, guild_id: int, account_id: str) -> CreatorAccount:
        existing = await self._store.get_creator_account(guild_id, account_id)
        if existing is None:
            raise ValueError(f"creator account {account_id!r} was not found in this guild")
        return existing

    async def _record_receipt(
        self,
        *,
        guild_id: int,
        account_id: str,
        action: str,
        actor_member_id: int,
        detail: dict[str, JSONValue],
        now: datetime,
    ) -> None:
        await self._store.record_creator_registry_receipt(
            guild_id=guild_id,
            receipt_id=f"creator-registry:{uuid4().hex}",
            account_id=account_id,
            action=action,
            actor_member_id=actor_member_id,
            detail=detail,
            created_at=now,
        )
