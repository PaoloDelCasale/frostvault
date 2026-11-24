"""Manual Vault hold. No filesystem/provider operations or queue rewrites."""
from __future__ import annotations

import logging
from typing import Any

from ..database import transaction
from ..errors import Conflict, NotFound
from ..i18n import DEFAULT_LOCALE, translate
from . import notifications, vault_locks

_logger = logging.getLogger(__name__)


class VaultNotFound(NotFound, ValueError):
    """The Vault to pause or resume does not exist."""


class NotOperational(Conflict, ValueError):
    """Only an operational Vault is paused or resumed."""


def set_paused(connection: Any, *, vault_id: int, paused: bool,
               locale: str = DEFAULT_LOCALE) -> dict[str, Any]:
    # FOR UPDATE conflicts with shared worker claims, without making every claim
    # or held progress step exclusive (ADR-0031/0033). Always Vault before Jobs.
    vault = vault_locks.lock_vault(
        connection, vault_id, vault_locks.UPDATE, columns="id, name, paused, lifecycle",
    )
    if vault is None:
        raise VaultNotFound("api.vault_not_found")
    # Stopping or tombstoned Vaults are not operational; a Vault waiting for
    # its reconnect scan still is (the schema allows a pause only there).
    if vault["lifecycle"] not in {"active", "reconnect_required"}:
        raise NotOperational("api.vault_pause.not_operational")
    key = "vault_pause.paused_notice" if paused else "vault_pause.resumed_notice"
    if bool(vault["paused"]) != paused:
        connection.execute("UPDATE vaults SET paused=%s WHERE id=%s", (paused, vault_id))
        # Optional delivery failure must not roll back the Owner's hold.
        try:
            with transaction(connection):
                channels = ("in_app", "email") if notifications.email_configured() else ("in_app",)
                notifications.enqueue_notification(
                    connection, vault_id=vault_id,
                    event="vault_paused" if paused else "vault_resumed", title="",
                    title_key="vault_pause.paused" if paused else "vault_pause.resumed",
                    body_key=key, message_params={"name": vault["name"]}, channels=channels,
                )
        except Exception:
            _logger.warning("Vault pause notification failed; hold transition remains committed")
    return {"id": vault_id, "paused": paused, "message_key": key,
            "message": translate(key, locale=locale, name=vault["name"])}
