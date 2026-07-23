"""Recovery-secret export and custody confirmation for crypt vaults (issue #6)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..branding import PRODUCT_NAME
from ..config import settings
from .rclone_runtime import (
    RUNTIME_REMOTE_NAME,
    _crypt_target,
    _read_base_section,
    _section_as_text,
    build_crypt_config_text,
    secrets_for_vault,
)
from .vault_crypto import (
    CryptSecrets,
    StoredCryptSecrets,
    audit_without_secrets,
    decrypt_vault_secrets,
)
from .vault_governance import notify_owner_of_admin_action, primary_owner


RECOVERY_REMOTE_NAME = RUNTIME_REMOTE_NAME


class RecoveryCustodyRequired(RuntimeError):
    """Uploads to a crypt vault are blocked until recovery custody is confirmed."""

    def __init__(self, message: str = "Recovery secret custody is not confirmed") -> None:
        super().__init__(message)


class RecoveryError(RuntimeError):
    """Recoverable failure while exporting or confirming recovery material."""


def stored_secrets_from_vault(vault: dict[str, Any]) -> StoredCryptSecrets:
    return StoredCryptSecrets(
        password_ciphertext=vault["crypt_password_ciphertext"],
        password2_ciphertext=vault["crypt_password2_ciphertext"],
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def vault_requires_recovery_custody(vault: dict[str, Any]) -> bool:
    return (
        vault.get("encryption_mode") == "crypt"
        and not vault.get("recovery_custody_confirmed_at")
    )


def confirm_recovery_custody(connection: Any, *, vault_id: int) -> dict[str, Any]:
    vault = connection.execute(
        "SELECT * FROM vaults WHERE id=%s", (vault_id,)
    ).fetchone()
    if not vault:
        raise RecoveryError("Vault not found")
    if vault["encryption_mode"] != "crypt":
        raise RecoveryError("Recovery custody applies only to crypt vaults")
    if vault["recovery_custody_confirmed_at"]:
        return vault
    timestamp = now_iso()
    connection.execute(
        "UPDATE vaults SET recovery_custody_confirmed_at=%s WHERE id=%s",
        (timestamp, vault_id),
    )
    updated = connection.execute(
        "SELECT * FROM vaults WHERE id=%s", (vault_id,)
    ).fetchone()
    secrets = secrets_for_vault(updated)
    audit_without_secrets(
        "vault_recovery_custody_confirmed",
        secrets,
        connection=connection,
        vault_id=vault_id,
        confirmed_at=timestamp,
    )
    return updated


def build_recovery_export(vault: dict[str, Any]) -> str:
    """Return a standalone Rclone configuration that can restore the vault."""
    if vault.get("encryption_mode") != "crypt":
        raise RecoveryError("Recovery export is only available for crypt vaults")
    if not settings.archive_master_key.strip():
        raise RecoveryError("ARCHIVE_MASTER_KEY is not configured")
    secrets = secrets_for_vault(vault)
    base_name, section = _read_base_section(str(vault["rclone_remote"]))
    base_text = _section_as_text(base_name, section)
    body = build_crypt_config_text(vault, secrets, base_section_text=base_text)
    header = (
        f"# {PRODUCT_NAME} recovery export\n"
        f"# Vault UUID: {vault['uuid']}\n"
        f"# Vault name: {vault['name']}\n"
        "# Keep this file offline. It reconstructs the crypt remote independently\n"
        "# of the application database and master key.\n"
        f"# Underlying remote target: {_crypt_target(vault)}\n\n"
    )
    return header + body


def export_recovery_secret(
    vault: dict[str, Any],
    *,
    actor_id: int,
    reason: str,
    notify_owner_user_id: int | None = None,
    admin_override: bool = False,
    connection: Any | None = None,
) -> str:
    export = build_recovery_export(vault)
    secrets = secrets_for_vault(vault)
    fields: dict[str, Any] = {
        "vault_id": vault["id"],
        "actor_id": actor_id,
        "reason": reason,
    }
    if notify_owner_user_id is not None:
        fields["notify_user_id"] = notify_owner_user_id
    if admin_override:
        fields["admin_override"] = True
        notify_owner_of_admin_action(
            "vault_recovery_exported",
            vault_id=vault["id"],
            owner_user_id=notify_owner_user_id or 0,
            actor_id=actor_id,
            reason=reason,
        )
    else:
        audit_without_secrets(
            "vault_recovery_exported",
            secrets,
            connection=connection,
            **fields,
        )
    return export


def require_upload_custody(vault: dict[str, Any]) -> None:
    if vault_requires_recovery_custody(vault):
        raise RecoveryCustodyRequired(
            "Confirm recovery-secret custody before uploading to this crypt vault"
        )
