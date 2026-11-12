"""Shared fixtures for catalog tests on the rebuilt schema (#587)."""
from __future__ import annotations

import hashlib
from typing import Any


def seed_vault(
    connection: Any,
    *,
    vault_id: int = 2,
    source_root: str = "/source",
    name: str = "Docs",
) -> int:
    """Insert one active Vault (and its S3 target) with an explicit id."""
    target = connection.execute(
        "SELECT id FROM vault_targets WHERE bucket='bucket'"
    ).fetchone()
    if target is None:
        target = connection.execute(
            """
            INSERT INTO vault_targets(provider, bucket, region)
            VALUES ('s3', 'bucket', 'eu-south-1') RETURNING id
            """
        ).fetchone()
    connection.execute(
        """
        INSERT INTO vaults(id, target_id, name, source_root)
        VALUES (%s, %s, %s, %s)
        """,
        (vault_id, target["id"], name, source_root),
    )
    return vault_id


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fingerprint(catalog: Any, vault_id: int, path: str, digest: str) -> bool:
    """Publish ``digest`` for the Local Copy currently observed at ``path``."""
    row = catalog.get_file_by_path(vault_id, path)
    local = row["local_copy"]
    return catalog.publish_local_fingerprint(
        row["id"],
        generation=row["generation"],
        plaintext_sha256=digest,
        size=local["size"],
        mtime_ns=local["mtime_ns"],
    )


def protect(
    catalog: Any,
    vault_id: int,
    path: str,
    digest: str,
    *,
    provider_version_id: str = "v1",
    created_at: str = "2026-07-21T10:01:00+00:00",
    verified_at: str = "2026-07-21T10:02:00+00:00",
) -> str:
    """Upload and verify Current for the fingerprinted Local Copy at ``path``."""
    row = catalog.get_file_by_path(vault_id, path)
    size = row["local_copy"]["size"] if row["local_copy"] else 1
    version_id = catalog.begin_replacement(
        row["id"],
        object_key=f"vaults/x/{path}",
        provider_version_id=provider_version_id,
        size=size or 0,
        storage_class="STANDARD",
        created_at=created_at,
    )
    catalog.promote_replacement(
        row["id"],
        archive_version_id=version_id,
        plaintext_sha256=digest,
        verified_at=verified_at,
        local_generation=row["generation"],
    )
    return version_id
