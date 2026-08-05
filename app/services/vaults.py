"""Self-service vault creation (issues #7, #6, and #150).

Authenticated *existing* users can create a vault for themselves; nothing
here provisions a user from identity claims -- the caller must already have
a row in ``users`` (enforced by the ``current_user`` dependency in
``app.main``).

The server is the sole authority over storage identity. Every vault gets an
immutable, randomly generated UUID, and the S3 namespace
(``vaults/<uuid>/``) is derived from that UUID alone. Empty Vaults also
receive ``/sources/managed/<uuid>``; adoption mode stores an authorized
existing directory under a custom Source Volume instead. Callers can never
choose S3 bucket/prefix/remote or crypt secrets: ``name``/``slug`` stay
human-readable labels, and adoption accepts only a volume alias plus
volume-relative path — never an arbitrary filesystem path.

``encryption_mode`` (``plain`` or ``crypt``) is chosen at creation time and
is immutable afterwards. Crypt vaults receive a unique sealed secret pair;
mode changes are a guided migration to a *new* vault, never an in-place
reinterpretation of existing object keys.

Vault row, owner membership, and the local root are provisioned as one
unit. Empty mode creates the managed directory inside the transaction;
adoption binds an existing directory in place and never moves, copies,
chowns, or rewrites adopted content. Because the identity is a freshly
generated UUID checked against a unique database index, concurrent empty
creations cannot collide in the database, the filesystem, or the S3 prefix.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from typing import Any, Mapping

from ..config import settings
from ..database import INTEGRITY_ERRORS, db
from . import source_areas
from .source_layout import ensure_managed_directory, managed_vault_path
from .vault_crypto import encrypt_vault_secrets, generate_crypt_secrets
from .vault_relocation import enroll_vault_root_identity


class VaultCreationError(Exception):
    """Base error for self-service vault creation failures."""


class InvalidVaultName(VaultCreationError):
    """The supplied name/slug could not be turned into a valid label."""


class VaultSlugTaken(VaultCreationError):
    """Another vault already uses the requested slug."""


class VaultProvisioningUnavailable(VaultCreationError):
    """The server has no default S3 bucket/rclone remote configured."""


class VaultAdoptionError(VaultCreationError):
    """Adoption preflight failed; no Vault row or membership was committed."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


_SLUG_PATTERN = re.compile(r"[a-z0-9-]+")
_ENCRYPTION_MODES = frozenset({"plain", "crypt"})
_CREATION_MODES = frozenset({"empty", "adopt"})

# This is deliberately an allowlist rather than a denylist. Admin Vault
# responses must remain safe when new persistence-only columns are added, in
# particular credential material for a storage backend.
_ADMIN_VAULT_PUBLIC_FIELDS = (
    "id",
    "uuid",
    "slug",
    "name",
    "source_root",
    "s3_bucket",
    "s3_prefix",
    "rclone_remote",
    "enabled",
    "encryption_mode",
    "decommission_state",
    "decommissioned_at",
    "root_released_at",
)


def project_admin_vault(
    vault: Mapping[str, Any], *, member_count: int | None = None
) -> dict[str, Any]:
    """Return the stable, non-secret administrative Vault representation.

    The projection intentionally fails closed: any column not named in
    ``_ADMIN_VAULT_PUBLIC_FIELDS`` is absent from the HTTP representation.
    """
    if member_count is None:
        member_count = int(vault["member_count"])
    return {
        **{field: vault[field] for field in _ADMIN_VAULT_PUBLIC_FIELDS},
        "member_count": member_count,
    }


def list_admin_vaults(connection: Any) -> list[dict[str, Any]]:
    """List Vaults through the same explicit public allowlist as creation."""
    rows = connection.execute(
        """
        SELECT v.id, v.uuid, v.slug, v.name, v.source_root, v.s3_bucket,
               v.s3_prefix, v.rclone_remote, v.enabled, v.encryption_mode,
               v.decommission_state, v.decommissioned_at, v.root_released_at,
               COUNT(vm.user_id) AS member_count
        FROM vaults v LEFT JOIN vault_members vm ON vm.vault_id=v.id
        GROUP BY v.id ORDER BY lower(v.name)
        """
    ).fetchall()
    return [project_admin_vault(row) for row in rows]


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def create_vault_for_user(
    user_id: int,
    name: str,
    slug: str | None = None,
    encryption_mode: str = "plain",
    *,
    creation_mode: str = "empty",
    volume_alias: str | None = None,
    relative_path: str | None = None,
    actor_is_admin: bool = False,
) -> dict[str, Any]:
    """Create a vault owned solely by ``user_id`` and return the new row.

    ``creation_mode`` is ``empty`` (default) or ``adopt``. Adoption requires
    ``volume_alias`` and ``relative_path`` under an authorized Source Area
    (or an unassigned path when ``actor_is_admin``). Callers never supply
    S3 identity, crypt secrets, or absolute filesystem paths.
    """
    mode = (encryption_mode or "plain").strip().lower()
    if mode not in _ENCRYPTION_MODES:
        raise InvalidVaultName("encryption_mode must be 'plain' or 'crypt'")

    create_mode = (creation_mode or "empty").strip().lower()
    if create_mode not in _CREATION_MODES:
        raise InvalidVaultName("creation_mode must be 'empty' or 'adopt'")

    default_bucket = settings.vault_s3_bucket.strip()
    if mode == "crypt":
        default_remote = settings.vault_rclone_base_remote.strip()
        if (
            not default_bucket
            or not default_remote
            or not settings.archive_master_key.strip()
        ):
            raise VaultProvisioningUnavailable(
                "Crypt vault creation requires VAULT_S3_BUCKET, "
                "VAULT_RCLONE_BASE_REMOTE, and ARCHIVE_MASTER_KEY"
            )
    else:
        default_remote = settings.vault_rclone_remote.strip()
        if not default_bucket or not default_remote:
            raise VaultProvisioningUnavailable(
                "Self-service vault creation is not configured on this server"
            )

    clean_name = name.strip()
    if not clean_name:
        raise InvalidVaultName("Vault name is required")
    candidate_slug = _slugify(slug) if slug else _slugify(clean_name)
    if not candidate_slug or not _SLUG_PATTERN.fullmatch(candidate_slug):
        raise InvalidVaultName(
            "The slug can contain only lowercase letters, numbers, and hyphens"
        )

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
    # under concurrent creation (see migration 0007_vault_ownership).
    vault_uuid = str(uuid.uuid4())
    s3_prefix = f"vaults/{vault_uuid}/"
    adopting = create_mode == "adopt"
    if not adopting:
        ensure_managed_directory()
        source_root = str(managed_vault_path(vault_uuid))
    else:
        source_root = ""  # resolved under the shared Source Area lock below

    password_ciphertext = None
    password2_ciphertext = None
    if mode == "crypt":
        sealed = encrypt_vault_secrets(generate_crypt_secrets())
        password_ciphertext = sealed.password_ciphertext
        password2_ciphertext = sealed.password2_ciphertext

    directory_created = False
    try:
        with db() as connection:
            if adopting:
                try:
                    adopted = source_areas.resolve_adoption_candidate(
                        connection,
                        owner_user_id=user_id,
                        volume_alias=str(volume_alias),
                        relative_path=str(relative_path),
                        actor_is_admin=actor_is_admin,
                    )
                except source_areas.SourceAreaError as exc:
                    raise VaultAdoptionError(exc.reason, str(exc)) from exc
                source_root = str(adopted)

            vault = connection.execute(
                """
                INSERT INTO vaults(
                    uuid, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote, encryption_mode,
                    crypt_password_ciphertext, crypt_password2_ciphertext
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING
                    id, uuid, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote, enabled, encryption_mode,
                    crypt_password_ciphertext, crypt_password2_ciphertext,
                    recovery_custody_confirmed_at, decommission_state,
                    decommissioned_at, root_released_at
                """,
                (
                    vault_uuid,
                    candidate_slug,
                    clean_name,
                    source_root,
                    default_bucket,
                    s3_prefix,
                    default_remote,
                    mode,
                    password_ciphertext,
                    password2_ciphertext,
                ),
            ).fetchone()
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'owner')",
                (vault["id"], user_id),
            )
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
    except INTEGRITY_ERRORS as exc:
        message = str(exc).lower()
        if "slug" in message:
            raise VaultSlugTaken("Vault slug is already in use") from exc
        # A vaults.uuid collision is astronomically unlikely with UUIDv4,
        # but if the database ever rejects it, surface a retryable error
        # rather than mislabel it as a slug conflict.
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
