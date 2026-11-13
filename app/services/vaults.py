"""Vault creation from the UI (issues #7, #6, #150, and #412).

The single owner creates every Vault from the UI; there is no environment
bootstrap Vault and no per-Vault membership.

The server is the sole authority over storage identity. Every vault gets an
immutable, randomly generated UUID, and the S3 namespace
(``vaults/<uuid>/``) is derived from that UUID alone. Empty Vaults also
receive ``/sources/managed/<uuid>``; adoption mode stores any free existing
directory under a custom Source Volume instead. Callers can never
choose S3 bucket/prefix: ``name`` is only a
human-readable label (Vaults have no slug, #593), and adoption accepts only a volume alias plus
volume-relative path — never an arbitrary filesystem path.

Objects use readable keys under the immutable ``vaults/<uuid>/`` namespace.

Vault row and local root are provisioned as one
unit. Empty mode creates the managed directory inside the transaction;
adoption binds an existing directory in place and never moves, copies,
chowns, or rewrites adopted content. Because the identity is a freshly
generated UUID checked against a unique database index, concurrent empty
creations cannot collide in the database, the filesystem, or the S3 prefix.
"""
from __future__ import annotations

import os
import shutil
import uuid
from typing import Any

from ..config import settings
from ..database import INTEGRITY_ERRORS, db
from . import source_directories
from .source_layout import ensure_managed_directory, managed_vault_path
from .vault_relocation import enroll_vault_root_identity


# A Vault row with its immutable cloud location. The bucket belongs to the
# Vault Target and the prefix is derived from the Vault UUID alone; neither is
# stored on the Vault (#593). Callers read ``s3_bucket``/``s3_prefix`` from
# this projection, never from Vault columns.
VAULT_ROW_SQL = """
    SELECT v.*, t.bucket AS s3_bucket, t.region AS s3_region,
           'vaults/' || v.uuid::text || '/' AS s3_prefix
    FROM vaults v JOIN vault_targets t ON t.id = v.target_id
"""


def vault_prefix(vault_uuid: Any) -> str:
    """The Vault's S3 namespace, ``vaults/<uuid>/``."""
    return f"vaults/{vault_uuid}/"


class VaultCreationError(Exception):
    """Base error for self-service vault creation failures."""


class InvalidVaultName(VaultCreationError):
    """The supplied name or creation mode is not valid."""


class VaultProvisioningUnavailable(VaultCreationError):
    """The server has no default S3 bucket configured."""


class VaultAdoptionError(VaultCreationError):
    """Adoption preflight failed; no Vault row was committed."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


_CREATION_MODES = frozenset({"empty", "adopt"})

# This is deliberately an allowlist rather than a denylist. Vault inventory
# responses must remain safe when new persistence-only columns are added, in
# particular credential material for a storage backend.
_VAULT_INVENTORY_FIELDS = (
    "id",
    "uuid",
    "name",
    "source_root",
    "s3_bucket",
    "s3_prefix",
    "lifecycle",
    "paused",
    "root_released_at",
)


def list_vault_inventory(connection: Any) -> list[dict[str, Any]]:
    """List every Vault, tombstones included, through an explicit allowlist."""
    rows = connection.execute(
        f"{VAULT_ROW_SQL} ORDER BY lower(v.name), v.id"
    ).fetchall()
    items = []
    for row in rows:
        item = {field: row[field] for field in _VAULT_INVENTORY_FIELDS}
        item["uuid"] = str(row["uuid"])
        released = row["root_released_at"]
        item["root_released_at"] = released.isoformat() if released else None
        items.append(item)
    return items


def _vault_target_id(connection: Any, bucket: str, region: str) -> int:
    """The immutable Vault Target for ``bucket``/``region``, created once."""
    connection.execute(
        """
        INSERT INTO vault_targets(provider, bucket, region) VALUES ('s3', %s, %s)
        ON CONFLICT (provider, bucket, region) DO NOTHING
        """,
        (bucket, region),
    )
    return int(connection.execute(
        "SELECT id FROM vault_targets WHERE provider='s3' AND bucket=%s AND region=%s",
        (bucket, region),
    ).fetchone()["id"])


def create_vault(
    name: str,
    *,
    creation_mode: str = "empty",
    volume_alias: str | None = None,
    relative_path: str | None = None,
) -> dict[str, Any]:
    """Create a Vault with an immutable plaintext cloud namespace.

    ``creation_mode`` is ``empty`` (default) or ``adopt``. Adoption requires
    ``volume_alias`` and ``relative_path`` naming a free directory inside a
    custom Source Volume. Callers never supply S3 identity or absolute
    filesystem paths.
    """
    create_mode = (creation_mode or "empty").strip().lower()
    if create_mode not in _CREATION_MODES:
        raise InvalidVaultName("creation_mode must be 'empty' or 'adopt'")

    default_bucket = settings.vault_s3_bucket.strip()
    if not default_bucket:
        raise VaultProvisioningUnavailable(
            "Vault creation is not configured on this server"
        )

    clean_name = name.strip()
    if not clean_name:
        raise InvalidVaultName("Vault name is required")

    if create_mode == "adopt":
        if not (volume_alias or "").strip():
            raise InvalidVaultName("Adoption requires volume_alias")
        if relative_path is None:
            raise InvalidVaultName("Adoption requires relative_path")
    elif volume_alias is not None or relative_path is not None:
        raise InvalidVaultName(
            "volume_alias and relative_path are only valid for adoption"
        )

    # The server -- never the caller -- mints the storage identity. UUIDv4
    # is already collision-resistant; the database's unique index on
    # vaults.uuid additionally guarantees a collision can never persist even
    # under concurrent creation (vaults_uuid_uq).
    vault_uuid = str(uuid.uuid4())
    region = (settings.aws_region or "").strip()
    if not region:
        raise VaultProvisioningUnavailable(
            "Vault creation is not configured on this server"
        )
    adopting = create_mode == "adopt"
    if not adopting:
        ensure_managed_directory()
        source_root = str(managed_vault_path(vault_uuid))
    else:
        source_root = ""  # resolved under the shared Vault-root lock below

    directory_created = False
    try:
        with db() as connection:
            if adopting:
                try:
                    adopted = source_directories.resolve_adoption_candidate(
                        connection,
                        volume_alias=str(volume_alias),
                        relative_path=str(relative_path),
                    )
                except source_directories.SourceDirectoryError as exc:
                    raise VaultAdoptionError(exc.reason, str(exc)) from exc
                source_root = str(adopted)

            target_id = _vault_target_id(connection, default_bucket, region)
            inserted = connection.execute(
                """
                INSERT INTO vaults(uuid, target_id, name, source_root)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (vault_uuid, target_id, clean_name, source_root),
            ).fetchone()
            row = connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s", (inserted["id"],),
            ).fetchone()
            vault = {**row, "uuid": str(row["uuid"])}
            if not adopting:
                # Create the directory before the transaction commits: if this
                # raises, the `with` block below rolls the inserts back, so no
                # vault row can ever be left pointing at a missing directory.
                os.makedirs(source_root, exist_ok=False)
                directory_created = True
            # Enrol the real directory identity while creation still owns the
            # transaction. Relocation can later prove a rename by inode rather
            # than accepting a content lookalike or generic rebind.
            enroll_vault_root_identity(connection, int(vault["id"]), source_root)
            # Return the row as enrolled: a first scan checks the identity.
            row = connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s", (inserted["id"],),
            ).fetchone()
            vault = {**row, "uuid": str(row["uuid"])}
    except INTEGRITY_ERRORS as exc:
        # A vaults.uuid collision is astronomically unlikely with UUIDv4; a
        # concurrent allocation of the same root is refused by its index.
        raise VaultCreationError(
            "Could not provision a unique vault identity; please retry"
        ) from exc
    except BaseException:
        # Covers the directory already existing (rare, e.g. a stale mount)
        # and any failure between a successful mkdir and the transaction
        # actually committing: never leave an orphaned *managed* directory.
        # Adoption never deletes existing content.
        if directory_created:
            shutil.rmtree(source_root, ignore_errors=True)
        raise
    return vault
