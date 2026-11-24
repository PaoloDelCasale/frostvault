"""The Vault row of a cloud-content decision, locked ``FOR UPDATE`` (#858).

Cloud Deletion, the cleanups, Stop Protection, Vault Moves and Vault Deletion
decide on a Vault's cloud content holding its row in ``vault_locks.UPDATE``,
read through ``VAULT_ROW_SQL`` (the row with its cloud location). A missing
Vault raises ``VaultNotFound``, a ``CloudDeletionError``.

The lock and its errors live apart from ``cloud_deletion`` so that a service
can take it without importing Cloud Deletion. ``cloud_deletion`` imports them
back: they are the same objects, so every ``except cloud_deletion.VaultNotFound``
and every error handler behaves as before.
"""
from __future__ import annotations

from typing import Any

from ..errors import DomainError
from . import vault_locks
from .vaults import VAULT_ROW_SQL


class CloudDeletionError(DomainError, ValueError):
    """The request no longer proves safe, deliberate deletion authority."""

    code = "conflict"


class VaultNotFound(CloudDeletionError):
    """The Vault whose cloud content is asked about does not exist."""

    code = "not_found"


def lock_vault(connection: Any, vault_id: int) -> dict[str, Any]:
    row = vault_locks.lock_vault_view(
        connection, vault_id, vault_locks.UPDATE, view_sql=VAULT_ROW_SQL,
    )
    if row is None:
        raise VaultNotFound("api.vault_not_found")
    return row
