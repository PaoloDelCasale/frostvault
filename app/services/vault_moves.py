"""Vault-wide, colder-only storage-class moves along the saved Storage Plan (#595).

A move admits one ``storage-class`` Job per eligible file with
``job_storage_class_changes.colder_only``. The worker copies the file's exact
verified Current through the shared copy engine (``copy_provenance``): frozen
exact source, own token, verification before promotion, exact cleanup of the
superseded version. A move never warms a file and never requests a restore.
"""
from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from .cloud_deletion import lock_vault
from .cost_estimates import estimate_storage_month, list_prices
from .lifecycle_policies import load_vault_plan
from .lifecycle_profiles import ALLOWED_TARGETS
from .s3_copy import S3_SINGLE_COPY_MAX_BYTES, restore_header_state

MINIMUM_DAYS = {"STANDARD_IA": 30, "ONEZONE_IA": 30, "GLACIER_IR": 90,
                "GLACIER": 90, "DEEP_ARCHIVE": 180}


class MoveBlocked(ValueError):
    """The move lacks current, exact, verified authority; preserve every copy."""


def require_ready(vault: dict[str, Any]) -> None:
    if vault["lifecycle"] != "active":
        raise MoveBlocked("Vault must be protected and reconnect must be complete")


def eligibility(row: dict[str, Any], target: str) -> str | None:
    current = (row.get("storage_class") or "STANDARD").upper()
    if row.get("busy"):
        return "pending_work"
    if row.get("cloud_check_failed"):
        return "cloud_check_unavailable"
    if (row.get("integrity") != "verified" or row.get("availability") != "available"
            or not row.get("plaintext_sha256") or row.get("size") is None
            or not row.get("provider_version_id")):
        return "unverified_or_unavailable"
    if target not in ALLOWED_TARGETS.get(current, ()):
        return "already_equal_or_colder"
    if current in {"GLACIER", "DEEP_ARCHIVE"} and row.get("restore_state") != "available":
        return "explicit_restore_required"
    return None


def candidates(connection: Any, vault_id: int) -> list[dict[str, Any]]:
    """Each file's explicit Current; never an older version behind a replacement."""
    return connection.execute("""
        SELECT f.id AS file_id, f.path, av.*,
            f.replacing_version_id IS NOT NULL OR f.held_version_id IS NOT NULL
            OR EXISTS(SELECT 1 FROM jobs j WHERE j.file_id=f.id
                AND j.status NOT IN ('completed','failed','cancelled'))
            OR EXISTS(SELECT 1 FROM upload_attempts a WHERE a.file_id=f.id AND a.settled_at IS NULL)
            OR EXISTS(SELECT 1 FROM copy_attempts c WHERE c.file_id=f.id AND c.settled_at IS NULL)
            AS busy
        FROM files f JOIN archive_versions av ON av.id=f.current_version_id
        WHERE f.vault_id=%s ORDER BY f.path
    """, (vault_id,)).fetchall()


def _namespace(vault: dict[str, Any]) -> str:
    return f"vaults/{vault['uuid']}/"


def refresh_cold_sources(connection: Any, vault: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """Observe an explicit external/recovery restore, never request one.

    Restore availability is temporary and listing does not expose it. A cached
    flag alone must neither admit expired restores nor hide a manually restored
    exact source, including when a Local Copy is already present.
    """
    from .. import storage as worker

    bucket = connection.execute(
        "SELECT bucket FROM vault_targets WHERE id=%s", (vault["target_id"],)).fetchone()["bucket"]
    client = None
    for row in rows:
        if row.get("storage_class") != "GLACIER" or eligibility({**row, "restore_state": "available"}, "DEEP_ARCHIVE"):
            continue
        try:
            if not str(row.get("object_key") or "").startswith(_namespace(vault)):
                raise MoveBlocked("Source key is outside this Vault")
            if client is None:
                client = worker.s3_client()
            head = client.head_object(Bucket=bucket, Key=row["object_key"], VersionId=row["provider_version_id"])
            if (str(head.get("VersionId")) != row["provider_version_id"] or head.get("DeleteMarker")
                    or head.get("ContentLength") != row["size"]):
                raise MoveBlocked("Exact cold source identity changed")
            row["storage_class"] = head.get("StorageClass") or "STANDARD"
            row["restore_state"] = restore_header_state(head.get("Restore"))[0]
        except Exception:
            row["cloud_check_failed"] = True


def estimate(rows: list[dict[str, Any]], target: str) -> dict[str, Any]:
    eligible = [r for r in rows if eligibility(r, target) is None]
    skipped = Counter(reason for r in rows if (reason := eligibility(r, target)))
    prices = list_prices()
    size = sum(int(r["size"]) for r in eligible)
    retrieval = sum(int(r["size"]) / 1024**3 *
                    prices.restore_rates.get(r.get("storage_class"), {}).get("Instant", 0) *
                    (2 if int(r["size"]) > S3_SINGLE_COPY_MAX_BYTES else 1)
                    for r in eligible)
    return {"target_class": target, "eligible_count": len(eligible), "total_bytes": size,
            "skipped": dict(skipped), "minimum_days": MINIMUM_DAYS.get(target, 0),
            "estimated_monthly_eur": estimate_storage_month(prices, size_bytes=size,
                                                            storage_class=target).estimated_cost_eur,
            "estimated_retrieval_eur": round(retrieval, 6),
            "pricing_effective_at": prices.effective_at, "region": prices.region,
            "assumptions": "AWS list prices (USD used 1:1 as EUR), not a bill. Monthly storage and source retrieval only; copy/GET requests, additional destination readback retrieval, minimum billable object sizes, archive metadata, temporary restored storage and early-deletion charges excluded. Exact source eligibility is rechecked by the worker."}


def preview(connection: Any, vault_id: int) -> dict[str, Any]:
    vault = lock_vault(connection, vault_id)
    require_ready(vault)
    plan = load_vault_plan(connection, vault_id)
    rows = candidates(connection, vault_id)
    if any(step.storage_class == "DEEP_ARCHIVE" for step in plan.transitions):
        refresh_cold_sources(connection, vault, rows)
    return {"vault_id": vault_id, "targets": [estimate(rows, step.storage_class)
                        for step in plan.transitions]}


def schedule(connection: Any, *, vault_id: int, target: str, user_id: int,
             acknowledged: bool, timestamp: str) -> dict[str, Any]:
    from ..catalog import ArchiveCatalog

    vault = lock_vault(connection, vault_id)
    require_ready(vault)
    if not acknowledged:
        raise MoveBlocked("Acknowledge minimum-duration and early-deletion charges")
    if target not in {step.storage_class for step in load_vault_plan(connection, vault_id).transitions}:
        raise MoveBlocked("Choose a colder class on this Vault's saved Storage Plan")
    rows = candidates(connection, vault_id)
    if target == "DEEP_ARCHIVE":
        refresh_cold_sources(connection, vault, rows)
    result = estimate(rows, target)
    eligible = [row for row in rows if eligibility(row, target) is None]
    if any(not str(row["object_key"]).startswith(_namespace(vault)) for row in eligible):
        raise MoveBlocked("Source key is outside this Vault")
    group_id = str(uuid.uuid4())
    ids: list[int] = []
    catalog = ArchiveCatalog(connection)
    for row in eligible:
        # Only the files found eligible here: never a warming move.
        admitted, _bytes, _count = catalog.queue_jobs(
            vault_id=vault_id, path=row["path"], action="storage-class", requested_at=timestamp,
            group_id=group_id, is_directory=False, target_storage_class=target, colder_only=True)
        ids.extend(admitted)
    if not ids:
        raise MoveBlocked("No eligible files: already colder, unverified, unrestored or pending work")
    return {**result, "group_id": group_id, "job_ids": ids, "item_count": len(ids)}


def check_source(target: str, head: dict[str, Any], current: dict[str, Any]) -> None:
    """The worker's recheck of the exact source HEAD before a move copies it."""
    observed = (head.get("StorageClass") or "STANDARD").upper()
    if target.upper() not in ALLOWED_TARGETS.get(observed, ()):
        raise MoveBlocked(f"Vault move is colder-only: the Archive Version is already {observed}")
    if observed in {"GLACIER", "DEEP_ARCHIVE"} and restore_header_state(head.get("Restore"))[0] != "available":
        raise MoveBlocked("Vault move never restores: restore the Archive Version explicitly first")
