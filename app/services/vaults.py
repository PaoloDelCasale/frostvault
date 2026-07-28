"""Self-service vault creation (issues #7 and #6).

Authenticated *existing* users can create a vault for themselves; nothing
here provisions a user from identity claims -- the caller must already have
a row in ``users`` (enforced by the ``current_user`` dependency in
``app.main``).

The server is the sole authority over storage identity. Every vault gets an
immutable, randomly generated UUID, and both the S3 namespace
(``vaults/<uuid>/``) and the local directory
(``/sources/managed/<uuid>``) are derived from that UUID alone. Callers
can never choose, and never influence, those namespaces: ``name``/``slug``
stay human-readable labels, not storage identities.

``encryption_mode`` (``plain`` or ``crypt``) is chosen at creation time and
is immutable afterwards. Crypt vaults receive a unique sealed secret pair;
mode changes are a guided migration to a *new* vault, never an in-place
reinterpretation of existing object keys.

Vault row, owner membership, and the local directory are provisioned as one
unit: the directory is created *inside* the database transaction, right
before it commits, so a vault row can never exist without its directory (a
failed ``mkdir`` rolls the transaction back) and a directory is removed if
anything after its creation still fails (e.g. the commit itself). Because
the identity is a freshly generated UUID checked against a unique database
index, concurrent creations cannot collide in the database, the filesystem,
or the S3 prefix.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from typing import Any

from ..config import settings
from ..database import INTEGRITY_ERRORS, db
from .source_layout import ensure_managed_directory, managed_vault_path
from .vault_crypto import encrypt_vault_secrets, generate_crypt_secrets


class VaultCreationError(Exception):
    """Base error for self-service vault creation failures."""


class InvalidVaultName(VaultCreationError):
    """The supplied name/slug could not be turned into a valid label."""


class VaultSlugTaken(VaultCreationError):
    """Another vault already uses the requested slug."""


class VaultProvisioningUnavailable(VaultCreationError):
    """The server has no default S3 bucket/rclone remote configured."""


_SLUG_PATTERN = re.compile(r"[a-z0-9-]+")
_ENCRYPTION_MODES = frozenset({"plain", "crypt"})


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def create_vault_for_user(
    user_id: int,
    name: str,
    slug: str | None = None,
    encryption_mode: str = "plain",
) -> dict[str, Any]:
    """Create a vault owned solely by ``user_id`` and return the new row.

    ``name``, an optional ``slug``, and ``encryption_mode`` are the only
    inputs honoured; any notion of a caller-chosen storage root, S3
    bucket, prefix, rclone remote, or crypt secret is out of scope by
    construction.
    """
    mode = (encryption_mode or "plain").strip().lower()
    if mode not in _ENCRYPTION_MODES:
        raise InvalidVaultName("encryption_mode must be 'plain' or 'crypt'")

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

    # The server -- never the caller -- mints the storage identity. UUIDv4
    # is already collision-resistant; the database's unique index on
    # vaults.uuid additionally guarantees a collision can never persist even
    # under concurrent creation (see migration 0007_vault_ownership).
    vault_uuid = str(uuid.uuid4())
    ensure_managed_directory()
    source_root = str(managed_vault_path(vault_uuid))
    s3_prefix = f"vaults/{vault_uuid}/"

    password_ciphertext = None
    password2_ciphertext = None
    if mode == "crypt":
        sealed = encrypt_vault_secrets(generate_crypt_secrets())
        password_ciphertext = sealed.password_ciphertext
        password2_ciphertext = sealed.password2_ciphertext

    directory_created = False
    try:
        with db() as connection:
            vault = connection.execute(
                """
                INSERT INTO vaults(
                    uuid, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote, encryption_mode,
                    crypt_password_ciphertext, crypt_password2_ciphertext
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
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
            # Create the directory before the transaction commits: if this
            # raises, the `with` block below rolls the inserts back, so no
            # vault row can ever be left pointing at a missing directory.
            os.makedirs(source_root, exist_ok=False)
            directory_created = True
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
        # actually committing: never leave an orphaned directory behind.
        if directory_created:
            shutil.rmtree(source_root, ignore_errors=True)
        raise
    return vault
