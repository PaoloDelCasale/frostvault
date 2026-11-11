"""Manual Vault hold. No filesystem/provider operations or queue rewrites."""
from __future__ import annotations

import logging
from typing import Any

from ..database import transaction
from ..i18n import DEFAULT_LOCALE, translate
from . import notifications

_logger = logging.getLogger(__name__)


class VaultNotFound(ValueError):
    pass


def set_paused(connection: Any, *, vault_id: int, paused: bool, user_id: int,
               locale: str = DEFAULT_LOCALE) -> dict[str, Any]:
    # FOR UPDATE conflicts with shared worker claims, without making every claim
    # or held progress step exclusive (ADR-0031/0033). Always Vault before Jobs.
    vault = connection.execute(
        "SELECT id, name, paused, enabled, decommission_state FROM vaults WHERE id=%s FOR UPDATE",
        (vault_id,),
    ).fetchone()
    if vault is None:
        raise VaultNotFound("Vault not found")
    if not vault["enabled"] or vault["decommission_state"] != "active":
        raise ValueError("Only an operational Vault can be paused or resumed")
    key = "vault_pause.paused_notice" if paused else "vault_pause.resumed_notice"
    if bool(vault["paused"]) != paused:
        connection.execute("UPDATE vaults SET paused=%s WHERE id=%s", (paused, vault_id))
        # Optional delivery failure must not roll back the Owner's hold.
        try:
            with transaction(connection):
                channels = ("in_app", "email") if notifications._smtp_config() is not None else ("in_app",)
                notifications.enqueue_notification(
                    connection, user_id=user_id, vault_id=vault_id,
                    event="vault_paused" if paused else "vault_resumed", title="",
                    title_key="vault_pause.paused" if paused else "vault_pause.resumed",
                    body_key=key, message_params={"name": vault["name"]}, channels=channels,
                )
        except Exception:
            _logger.warning("Vault pause notification failed; hold transition remains committed")
    return {"id": vault_id, "paused": paused, "message_key": key,
            "message": translate(key, locale=locale, name=vault["name"])}
