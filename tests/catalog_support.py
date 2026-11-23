"""Shared fixtures for catalog tests on the rebuilt schema (#587)."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from app.catalog import ArchiveCatalog
from tests import catalog_probe


def queue_rename_job(connection: Any, vault_id: int, path: str) -> list[int]:
    """Queue the copy to the renamed file's key, as the scan does after confirming a rename."""
    job_ids, _total, _eligible = ArchiveCatalog(connection).queue_jobs(
        vault_id=vault_id, path=path, action="rename", requested_at=datetime.now(timezone.utc).isoformat(),
        group_id=uuid.uuid4().hex, is_directory=False, origin="automatic",
    )
    return job_ids


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fingerprint(catalog: Any, vault_id: int, path: str, digest: str) -> bool:
    """Publish ``digest`` for the Local Copy currently observed at ``path``."""
    row = catalog_probe.get_file_by_path(catalog, vault_id, path)
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
    row = catalog_probe.get_file_by_path(catalog, vault_id, path)
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
