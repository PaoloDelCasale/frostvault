"""Vault creation from the UI (issues #7, #6, #150, #412 and #735).

The single owner creates every Vault from the UI; there is no environment
bootstrap Vault and no per-Vault membership.

The server is the sole authority over storage identity. Every vault gets an
immutable, randomly generated UUID and, since #735 (ADR-0049), its own Vault
Bucket that FrostVault creates (``vault_buckets``), with its objects at the
bucket root. Empty Vaults also receive ``/sources/managed/<uuid>``; adoption
mode stores any free existing directory under a custom Source Volume instead.
Callers can never choose S3 bucket/prefix: ``name`` is only a human-readable
label (Vaults have no slug, #593; the bucket name only carries a slug of it),
and adoption accepts only a volume alias plus volume-relative path -- never an
arbitrary filesystem path.

Where a Vault's objects live is data of its Vault Target: the bucket, its
region and its ``key_layout``. A Vault Bucket (``bucket_root``) holds the
Vault at its root; a Vault of an older installation keeps its shared bucket
and its ``vaults/<uuid>/`` prefix (``vault_prefix``). Every key is built from
the target's prefix (``object_prefix``), never from configuration.

Vault row, bucket reservation and local root are provisioned as one unit.
Empty mode creates the managed directory inside the transaction; adoption
binds an existing directory in place and never moves, copies, chowns, or
rewrites adopted content. The Vault is ``provisioning`` until its bucket is
created and verified, then ``active``.
"""
from __future__ import annotations

import os
import shutil
import uuid
from typing import Any, Callable

from ..errors import DomainError
from ..config import settings
from ..database import INTEGRITY_ERRORS, db
from . import source_directories, vault_buckets
from .bucket_provisioning import RetentionLock
from .retention_lock import CHOICES as RETENTION_LOCK_CHOICES
from .vault_paths import KEY_LAYOUT_BUCKET_ROOT, KEY_LAYOUT_VAULT_PREFIX
from .source_layout import ensure_managed_directory, managed_vault_path
from .vault_relocation import enroll_vault_root_identity



# The Vault's object prefix in its target's bucket, for rows aliased ``v``
# (vaults) and ``t`` (vault_targets). ``object_prefix`` is its Python twin.
PREFIX_SQL = (
    "(CASE t.key_layout WHEN 'bucket_root' THEN '' "
    "ELSE 'vaults/' || v.uuid::text || '/' END)"
)
# The cloud location of a Vault row, read from its Vault Target (#593, #735).
TARGET_COLUMNS_SQL = (
    "t.bucket AS s3_bucket, t.region AS s3_region, t.key_layout AS s3_key_layout, "
    "t.retention_lock_days AS s3_retention_lock_days, "
    f"{PREFIX_SQL} AS s3_prefix"
)

# A Vault row with its immutable cloud location. The bucket, region and key
# layout belong to the Vault Target; none is stored on the Vault (#593).
# Callers read ``s3_bucket``/``s3_prefix`` from this projection, never from
# Vault columns or configuration.
VAULT_ROW_SQL = f"""
    SELECT v.*, {TARGET_COLUMNS_SQL}
    FROM vaults v JOIN vault_targets t ON t.id = v.target_id
"""


class VaultCreationError(DomainError):
    """Base error for self-service vault creation failures; ``str()`` is internal."""

    code = "conflict"
    message_key = "ui.vault_create.failed"

    def __init__(self, message: str = "") -> None:
        Exception.__init__(self, message)


class InvalidVaultName(VaultCreationError):
    """The supplied name or creation mode is not valid."""

    code = "invalid"
    message_key = "api.vault_name_invalid"


class VaultProvisioningUnavailable(VaultCreationError):
    """Vault Buckets cannot be created here: no AWS region or credentials."""

    code = "unavailable"
    message_key = "api.vault_provisioning_unavailable"


class VaultAdoptionError(VaultCreationError):
    """Adoption preflight failed; no Vault row was committed."""

    code = "invalid"

    def __init__(self, reason: str, message: str | None = None) -> None:
        Exception.__init__(self, message or reason)
        self.reason = reason
        self.describe(f"api.vault_adoption.{reason}", fallback="api.vault_adoption.failed")


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
    "s3_region",
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
    # A requested Vault Deletion of a Tombstone (#717): pending, running or failed.
    deletions = {int(r["vault_id"]): r["status"] for r in connection.execute(
        "SELECT vault_id, status FROM vault_deletions").fetchall()}
    # A new Vault whose bucket is not ready yet (#735): its creation state.
    provisioning = vault_buckets.provisioning_states(connection)
    items = []
    for row in rows:
        item = {field: row[field] for field in _VAULT_INVENTORY_FIELDS}
        item["uuid"] = str(row["uuid"])
        item["dedicated_bucket"] = row["s3_key_layout"] == KEY_LAYOUT_BUCKET_ROOT
        # The Governance Retention Lock of its Vault Bucket, fixed at creation (#736).
        item["retention_lock_days"] = row["s3_retention_lock_days"]
        released = row["root_released_at"]
        item["root_released_at"] = released.isoformat() if released else None
        item["vault_deletion_status"] = deletions.get(int(row["id"]))
        item["bucket_provisioning"] = provisioning.get(int(row["id"]))
        items.append(item)
    return items


def create_vault(
    name: str,
    *,
    creation_mode: str = "empty",
    volume_alias: str | None = None,
    relative_path: str | None = None,
    retention_lock: RetentionLock | None = None,
    client_for: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Create a Vault and its dedicated Vault Bucket (#735).

    ``creation_mode`` is ``empty`` (default) or ``adopt``. Adoption requires
    ``volume_alias`` and ``relative_path`` naming a free directory inside a
    custom Source Volume. Callers never supply S3 identity or absolute
    filesystem paths. ``retention_lock`` is the optional Governance Retention
    Lock of the new bucket (#736, ADR-0050): 30, 90, 180 or 365 days; it
    cannot change afterwards.

    The Vault, its reserved bucket name and its root are committed first; the
    bucket is then created and verified (``vault_buckets.provision``). The
    returned row carries ``bucket``, the creation state: when it failed, the
    Vault stays ``provisioning`` and the creation resumes later.
    """
    create_mode = (creation_mode or "empty").strip().lower()
    if create_mode not in _CREATION_MODES:
        raise InvalidVaultName("creation_mode must be 'empty' or 'adopt'")

    clean_name = name.strip()
    if not clean_name:
        raise InvalidVaultName("Vault name is required")
    if retention_lock is not None and retention_lock.days not in RETENTION_LOCK_CHOICES:
        raise InvalidVaultName("A Retention Lock lasts 30, 90, 180 or 365 days")

    if create_mode == "adopt":
        if not (volume_alias or "").strip():
            raise InvalidVaultName("Adoption requires volume_alias")
        if relative_path is None:
            raise InvalidVaultName("Adoption requires relative_path")
    elif volume_alias is not None or relative_path is not None:
        raise InvalidVaultName(
            "volume_alias and relative_path are only valid for adoption"
        )

    region = (settings.aws_region or "").strip()
    try:
        # A client first: without a region or credentials nothing is saved.
        client = vault_buckets.client_for_region(region, client_for)
    except vault_buckets.VaultBucketUnavailable as exc:
        raise VaultProvisioningUnavailable(str(exc)) from None

    # The server -- never the caller -- mints the storage identity. UUIDv4
    # is already collision-resistant; the database's unique index on
    # vaults.uuid additionally guarantees a collision can never persist even
    # under concurrent creation (vaults_uuid_uq).
    vault_uuid = str(uuid.uuid4())
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

            # The bucket name is saved before CreateBucket (ADR-0049).
            target_id = vault_buckets.reserve(
                connection, vault_name=clean_name, region=region,
                retention_lock=retention_lock)
            inserted = connection.execute(
                """
                INSERT INTO vaults(uuid, target_id, name, source_root, lifecycle)
                VALUES (%s, %s, %s, %s, 'provisioning')
                RETURNING id
                """,
                (vault_uuid, target_id, clean_name, source_root),
            ).fetchone()
            vault_buckets.start(connection, int(inserted["id"]))
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
    bucket = vault_buckets.provision(int(vault["id"]), client=client)
    with db() as connection:
        row = connection.execute(
            f"{VAULT_ROW_SQL} WHERE v.id=%s", (vault["id"],),
        ).fetchone()
    return {**row, "uuid": str(row["uuid"]), "bucket": bucket}
