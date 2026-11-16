"""Vault Deletion: the delayed, cancellable removal of a Vault Tombstone (#717).

ADR-0047. Only a Tombstone can be deleted. The Owner confirms with the exact
Vault name, a reason and a fresh authentication, after a preview from a live
listing of the Vault's prefix ``vaults/{uuid}/``. The same seven-day delay as
Delayed Cloud Deletion follows (``vault_deletions.run_after``); until then the
request can be cancelled.

When the delay has elapsed the worker claims the request and deletes *every*
version and delete marker under that prefix, catalogued or not, and aborts its
unfinished multipart uploads. Nothing outside the prefix is ever named: each
listed key is checked against it before any delete, and one foreign key stops
the run. The Cloud-only gate does not apply and no local file is ever touched:
this module has no filesystem access at all.

Only after a fresh listing proves the prefix empty do the Vault and all its
catalog rows leave the database, in one transaction, with one audit event
``folder_protection.vault_deleted``. A failure (an S3 error or leftovers) keeps
the Tombstone and its catalog, marks the request ``failed`` and opens a
Problem; the Owner may retry it at once, without a new delay, or cancel it.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from .audit_events import record_audit_event
from .cloud_deletion import DELETION_DELAY_SECONDS, ConfirmationRequired, lock_vault
from .cost_estimates import list_prices
from .vault_moves import MINIMUM_DAYS

PROBLEM_EVENT = "vault_deletion_failed"
# A running deletion refreshes its heartbeat at least once per listed page; one
# silent for this long belongs to a worker that stopped and is claimed again.
STALE_RUN_SECONDS = 15 * 60
POLL_SECONDS = 30.0
# S3 DeleteObjects accepts at most 1000 keys per call.
_DELETE_BATCH = 1000
# Early deletion is billed per remaining day of the minimum storage duration,
# at the class's monthly rate (AWS bills a month as 30 days here).
_DAYS_PER_MONTH = 30


class VaultDeletionError(ValueError):
    """The request is not, or no longer, a safe Vault Deletion.

    ``code`` names the Owner-facing message (``api.vault_deletion.<code>``).
    """

    def __init__(self, message: str, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code


class VaultDeletionListingError(VaultDeletionError):
    """The Vault's prefix could not be listed: no preview, no confirmation."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "listing_failed")


class _PrefixViolation(VaultDeletionError):
    """A listing named a key outside the Vault's prefix."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _aware(value: Any) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        raise VaultDeletionError("A timezone-aware time is required")
    return moment


def vault_prefix(vault: dict[str, Any]) -> str:
    """The Vault's own prefix, from its parsed UUID: never empty or partial."""
    parsed = uuid.UUID(str(vault["uuid"]))
    return f"vaults/{parsed}/"


# -- listing -----------------------------------------------------------------


@dataclass
class PrefixInventory:
    prefix: str
    versions: list[dict[str, Any]] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.versions or self.markers or self.uploads)


def _inside(prefix: str, key: Any) -> str:
    text = str(key or "")
    if not prefix.startswith("vaults/") or not text.startswith(prefix) or text == prefix.rstrip("/"):
        raise _PrefixViolation("The listing named a key outside the Vault prefix")
    return text


def _version_pages(client: Any, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
    marker: dict[str, Any] = {}
    while True:
        page = client.list_object_versions(Bucket=bucket, Prefix=prefix, **marker)
        yield page
        if not page.get("IsTruncated"):
            return
        next_marker = {"KeyMarker": page.get("NextKeyMarker") or "",
                       "VersionIdMarker": page.get("NextVersionIdMarker") or ""}
        if not next_marker["KeyMarker"] or next_marker == marker:
            raise VaultDeletionError("The listing did not advance")
        marker = next_marker


def _upload_pages(client: Any, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
    marker: dict[str, Any] = {}
    while True:
        page = client.list_multipart_uploads(Bucket=bucket, Prefix=prefix, **marker)
        yield page
        if not page.get("IsTruncated"):
            return
        next_marker = {"KeyMarker": page.get("NextKeyMarker") or "",
                       "UploadIdMarker": page.get("NextUploadIdMarker") or ""}
        if not next_marker["KeyMarker"] or next_marker == marker:
            raise VaultDeletionError("The listing did not advance")
        marker = next_marker


def _page_items(prefix: str, page: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    versions = [{"key": _inside(prefix, item.get("Key")), "version_id": str(item["VersionId"]),
                 "size": int(item.get("Size") or 0),
                 "storage_class": str(item.get("StorageClass") or "STANDARD").upper(),
                 "last_modified": item.get("LastModified")}
                for item in page.get("Versions") or []]
    markers = [{"key": _inside(prefix, item.get("Key")), "version_id": str(item["VersionId"])}
               for item in page.get("DeleteMarkers") or []]
    return versions, markers


def list_prefix(client: Any, *, bucket: str, prefix: str) -> PrefixInventory:
    """Every version, delete marker and unfinished multipart upload under ``prefix``."""
    inventory = PrefixInventory(prefix)
    for page in _version_pages(client, bucket, prefix):
        versions, markers = _page_items(prefix, page)
        inventory.versions.extend(versions)
        inventory.markers.extend(markers)
    for page in _upload_pages(client, bucket, prefix):
        inventory.uploads.extend({"key": _inside(prefix, item.get("Key")),
                                  "upload_id": str(item["UploadId"])}
                                 for item in page.get("Uploads") or [])
    return inventory


def summarize(inventory: PrefixInventory, *, executes_at: datetime) -> dict[str, Any]:
    """The preview: counts, bytes per storage class and early-deletion cost.

    The cost is estimated at the earliest execution (after the delay), from
    each version's LastModified and the bundled list prices.
    """
    prices = list_prices()
    classes: dict[str, dict[str, Any]] = {}
    for version in inventory.versions:
        name = version["storage_class"]
        entry = classes.setdefault(name, {"storage_class": name, "version_count": 0,
                                          "byte_count": 0, "early_deletion_cost": 0.0,
                                          "early_deletion_version_count": 0,
                                          "minimum_days": MINIMUM_DAYS.get(name, 0)})
        entry["version_count"] += 1
        entry["byte_count"] += version["size"]
        minimum = MINIMUM_DAYS.get(name, 0)
        modified = version.get("last_modified")
        if not minimum or not isinstance(modified, datetime):
            continue
        age_days = (executes_at - _aware(modified)).total_seconds() / 86400
        remaining = max(0.0, minimum - age_days)
        if remaining <= 0:
            continue
        rate = float(prices.storage_rates.get(name, 0.0))
        entry["early_deletion_cost"] += version["size"] / (1024 ** 3) * rate * remaining / _DAYS_PER_MONTH
        entry["early_deletion_version_count"] += 1
    by_class = sorted(classes.values(), key=lambda item: item["storage_class"])
    for entry in by_class:
        entry["early_deletion_cost"] = round(entry["early_deletion_cost"], 6)
    return {
        "prefix": inventory.prefix,
        "object_count": len({v["key"] for v in inventory.versions}
                            | {m["key"] for m in inventory.markers}),
        "version_count": len(inventory.versions),
        "delete_marker_count": len(inventory.markers),
        "multipart_upload_count": len(inventory.uploads),
        "byte_count": sum(v["size"] for v in inventory.versions),
        "by_storage_class": by_class,
        "early_deletion_cost": round(sum(e["early_deletion_cost"] for e in by_class), 6),
        "currency": prices.currency,
        "pricing_effective_at": prices.effective_at,
        "region": prices.region,
        "executes_at": executes_at.isoformat(),
        "delay_seconds": DELETION_DELAY_SECONDS,
    }


# -- consent -----------------------------------------------------------------


def _vault_row(connection: Any, vault_id: int) -> dict[str, Any]:
    row = connection.execute(
        """SELECT v.*, t.bucket AS s3_bucket FROM vaults v
           JOIN vault_targets t ON t.id = v.target_id WHERE v.id=%s""", (vault_id,)).fetchone()
    if row is None:
        raise LookupError("Vault not found")
    return row


def _request_row(connection: Any, vault_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT * FROM vault_deletions WHERE vault_id=%s" + (" FOR UPDATE" if lock else ""),
        (vault_id,)).fetchone()


def require_tombstone(vault: dict[str, Any]) -> None:
    if vault["lifecycle"] != "tombstone" or not vault.get("root_released_at"):
        raise VaultDeletionError("Only a stopped Vault (Tombstone) can be deleted", "not_tombstone")


def deletion_target(connection: Any, vault_id: int) -> dict[str, Any]:
    """What a preview lists: the Tombstone's bucket and its own prefix."""
    vault = _vault_row(connection, vault_id)
    require_tombstone(vault)
    return {"vault_id": vault_id, "name": vault["name"], "bucket": vault["s3_bucket"],
            "prefix": vault_prefix(vault)}


def preview(client: Any, target: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Live preview of a Tombstone's prefix. No listing, no preview."""
    moment = now or datetime.now(timezone.utc)
    try:
        inventory = list_prefix(client, bucket=target["bucket"], prefix=target["prefix"])
    except Exception as exc:
        raise VaultDeletionListingError("The Vault's cloud content could not be listed") from exc
    return summarize(inventory, executes_at=moment + timedelta(seconds=DELETION_DELAY_SECONDS))


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def status(connection: Any, vault_id: int) -> dict[str, Any]:
    vault = _vault_row(connection, vault_id)
    request = _request_row(connection, vault_id)
    result: dict[str, Any] = {
        "vault_id": vault_id, "vault_name": vault["name"], "lifecycle": vault["lifecycle"],
        "deletable": vault["lifecycle"] == "tombstone" and bool(vault["root_released_at"]),
        "local_files": int(connection.execute(
            """SELECT count(*) AS total FROM files
               WHERE vault_id=%s AND local_presence IN ('present','unsupported')""",
            (vault_id,)).fetchone()["total"] or 0),
        "request": None,
    }
    if request:
        result["request"] = {
            "status": request["status"], "requested_at": _iso(request["requested_at"]),
            "pending_until": _iso(request["run_after"]), "reason": request["reason"],
            "attempts": int(request["attempts"]), "failed_at": _iso(request["failed_at"]),
            "failure": request["failure"], "preview": request["preview"],
            "cancellable": request["status"] in {"pending", "failed"},
            "retryable": request["status"] == "failed",
        }
    return result


def request_deletion(connection: Any, *, vault_id: int, actor: str, confirmation: str,
                     reason: str, preview_summary: dict[str, Any],
                     requested_at: str) -> dict[str, Any]:
    """Record the Owner's consent, one per Vault, runnable after the delay."""
    vault = lock_vault(connection, vault_id)
    require_tombstone(vault)
    if confirmation != vault["name"]:
        raise ConfirmationRequired("Type the exact Vault name to confirm")
    if not (reason or "").strip() or len(reason) > 500:
        raise VaultDeletionError("A deletion reason is required", "reason")
    if _request_row(connection, vault_id, lock=True):
        raise VaultDeletionError("A Vault Deletion is already requested", "already_requested")
    if connection.execute(
            """SELECT 1 FROM jobs WHERE vault_id=%s
                 AND status NOT IN ('completed','failed','cancelled') LIMIT 1""",
            (vault_id,)).fetchone():
        raise VaultDeletionError("Wait for active Jobs of this Vault to end", "active_jobs")
    if preview_summary.get("prefix") != vault_prefix(vault):
        raise VaultDeletionError("The preview does not describe this Vault")
    requested = _aware(requested_at)
    pending_until = requested + timedelta(seconds=DELETION_DELAY_SECONDS)
    connection.execute(
        """INSERT INTO vault_deletions(vault_id, status, actor, reason, requested_at,
                                       run_after, preview)
           VALUES (%s, 'pending', %s, %s, %s, %s, %s)""",
        (vault_id, actor, reason.strip(), requested, pending_until, Jsonb(preview_summary)))
    record_audit_event(connection, event="folder_protection.vault_deletion_requested",
                       actor=actor, vault_id=vault_id, outcome="requested",
                       vault_uuid=str(vault["uuid"]), vault_name=vault["name"],
                       reason=reason.strip(), pending_until=pending_until.isoformat(),
                       preview={k: preview_summary.get(k) for k in (
                           "object_count", "version_count", "delete_marker_count",
                           "multipart_upload_count", "byte_count", "early_deletion_cost")},
                       local_data="retained")
    return status(connection, vault_id)


def _close_problem(connection: Any, vault_id: int) -> None:
    from .notifications import set_problem
    set_problem(connection, key=f"vault_deletion:{vault_id}", event=PROBLEM_EVENT,
                vault_id=vault_id, active=False)


def cancel(connection: Any, *, vault_id: int, actor: str) -> dict[str, Any]:
    """Withdraw a pending or failed request; a running one cannot be stopped."""
    vault = lock_vault(connection, vault_id)
    request = _request_row(connection, vault_id, lock=True)
    if request is None:
        raise VaultDeletionError("No Vault Deletion is requested", "not_requested")
    if request["status"] == "running":
        raise VaultDeletionError("The Vault Deletion has started and can no longer be cancelled", "running")
    connection.execute("DELETE FROM vault_deletions WHERE vault_id=%s", (vault_id,))
    record_audit_event(connection, event="folder_protection.vault_deletion_cancelled",
                       actor=actor, vault_id=vault_id, outcome="cancelled",
                       vault_uuid=str(vault["uuid"]), vault_name=vault["name"],
                       previous_status=request["status"],
                       completed_deletions_not_undone=request["status"] == "failed")
    _close_problem(connection, vault_id)
    return status(connection, vault_id)


def retry(connection: Any, *, vault_id: int, actor: str) -> dict[str, Any]:
    """Run a failed request again at once: its delay has already elapsed."""
    vault = lock_vault(connection, vault_id)
    require_tombstone(vault)
    request = _request_row(connection, vault_id, lock=True)
    if request is None or request["status"] != "failed":
        raise VaultDeletionError("Only a failed Vault Deletion can be retried", "not_failed")
    connection.execute(
        """UPDATE vault_deletions SET status='pending', failed_at=NULL, failure=NULL
           WHERE vault_id=%s""", (vault_id,))
    record_audit_event(connection, event="folder_protection.vault_deletion_retried",
                       actor=actor, vault_id=vault_id, outcome="requested",
                       vault_uuid=str(vault["uuid"]), vault_name=vault["name"],
                       attempts=int(request["attempts"]))
    _close_problem(connection, vault_id)
    return status(connection, vault_id)


# -- execution ---------------------------------------------------------------

Database = Callable[[], AbstractContextManager[Any]]


def claim_due(connection: Any, *, now: datetime) -> list[int]:
    """Claim every elapsed pending request and every stale running one."""
    stale = now - timedelta(seconds=STALE_RUN_SECONDS)
    rows = connection.execute(
        """SELECT d.vault_id FROM vault_deletions d
           WHERE (d.status='pending' AND d.run_after <= %s)
              OR (d.status='running' AND d.heartbeat_at < %s)
           ORDER BY d.run_after, d.vault_id FOR UPDATE SKIP LOCKED""",
        (now, stale)).fetchall()
    ids = [int(r["vault_id"]) for r in rows]
    if ids:
        connection.execute(
            """UPDATE vault_deletions SET status='running', heartbeat_at=%s,
                   attempts=attempts + 1, proven_empty_at=NULL
               WHERE vault_id = ANY(%s::bigint[])""", (now, ids))
    return ids


@dataclass
class _Progress:
    versions: int = 0
    markers: int = 0
    uploads: int = 0
    bytes: int = 0


def _heartbeat(database: Database, vault_id: int) -> None:
    with database() as connection:
        row = connection.execute(
            """UPDATE vault_deletions SET heartbeat_at=now()
               WHERE vault_id=%s AND status='running' RETURNING vault_id""",
            (vault_id,)).fetchone()
    if row is None:
        raise VaultDeletionError("The Vault Deletion is no longer running")


def _delete_batch(client: Any, bucket: str, prefix: str, items: list[dict[str, Any]]) -> None:
    for offset in range(0, len(items), _DELETE_BATCH):
        batch = items[offset:offset + _DELETE_BATCH]
        objects = [{"Key": _inside(prefix, item["key"]), "VersionId": item["version_id"]}
                   for item in batch]
        response = client.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        if (response or {}).get("Errors"):
            raise VaultDeletionError("The provider refused to delete some versions")


def _delete_prefix(client: Any, database: Database, *, vault_id: int, bucket: str,
                   prefix: str, progress: _Progress) -> None:
    """Delete everything listed under the prefix, page by page."""
    for page in _version_pages(client, bucket, prefix):
        # Validate the whole page before naming any of it.
        versions, markers = _page_items(prefix, page)
        _delete_batch(client, bucket, prefix, versions + markers)
        progress.versions += len(versions)
        progress.markers += len(markers)
        progress.bytes += sum(v["size"] for v in versions)
        _heartbeat(database, vault_id)
    for page in _upload_pages(client, bucket, prefix):
        uploads = [{"key": _inside(prefix, item.get("Key")), "upload_id": str(item["UploadId"])}
                   for item in page.get("Uploads") or []]
        for upload in uploads:
            client.abort_multipart_upload(Bucket=bucket, Key=upload["key"],
                                          UploadId=upload["upload_id"])
        progress.uploads += len(uploads)
        _heartbeat(database, vault_id)


def _remove_catalog(connection: Any, vault_id: int) -> dict[str, int]:
    """Every catalog row of the Vault, in foreign-key order; audit rows stay."""
    counts = connection.execute(
        """SELECT (SELECT count(*) FROM files WHERE vault_id=%(v)s) AS files,
                  (SELECT count(*) FROM archive_versions av JOIN files f ON f.id=av.file_id
                    WHERE f.vault_id=%(v)s) AS archive_versions,
                  (SELECT count(*) FROM jobs WHERE vault_id=%(v)s) AS jobs""",
        {"v": vault_id}).fetchone()
    files = "SELECT id FROM files WHERE vault_id=%(v)s"
    jobs = "SELECT id FROM jobs WHERE vault_id=%(v)s"
    statements = (
        f"DELETE FROM notifications WHERE vault_id=%(v)s OR job_id IN ({jobs})",
        f"DELETE FROM cleanup_candidates WHERE file_id IN ({files})",
        "DELETE FROM cleanup_batches WHERE vault_id=%(v)s",
        "DELETE FROM upload_remediations WHERE vault_id=%(v)s",
        "DELETE FROM jobs WHERE vault_id=%(v)s",
        "DELETE FROM job_groups WHERE vault_id=%(v)s",
        """UPDATE files SET current_version_id=NULL, replacing_version_id=NULL,
               matched_version_id=NULL, held_version_id=NULL
           WHERE vault_id=%(v)s AND (current_version_id IS NOT NULL
               OR replacing_version_id IS NOT NULL OR matched_version_id IS NOT NULL
               OR held_version_id IS NOT NULL)""",
        f"DELETE FROM archive_versions WHERE file_id IN ({files})",
        f"DELETE FROM upload_attempts WHERE file_id IN ({files})",
        "DELETE FROM upload_snapshots WHERE vault_id=%(v)s",
        "DELETE FROM copy_attempts WHERE vault_id=%(v)s",
        "DELETE FROM files WHERE vault_id=%(v)s",
    )
    for statement in statements:
        connection.execute(statement, {"v": vault_id})
    return {key: int(counts[key] or 0) for key in ("files", "archive_versions", "jobs")}


def _failure_detail(exc: BaseException) -> dict[str, Any]:
    """Safe structured context only: never exception text (URLs, credentials)."""
    detail: dict[str, Any] = {"error_type": type(exc).__name__}
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            detail["error_code"] = str(code)[:100]
    if isinstance(exc, _PrefixViolation):
        detail["reason"] = "outside_prefix"
    elif isinstance(exc, VaultDeletionError):
        detail["reason"] = "provider_refused"
    else:
        detail["reason"] = "provider_error"
    return detail


def _record_failure(database: Database, *, vault_id: int, failure: dict[str, Any],
                    progress: _Progress) -> None:
    from .notifications import set_problem
    failure = {**failure, "deleted_versions": progress.versions,
               "deleted_markers": progress.markers, "aborted_uploads": progress.uploads}
    with database() as connection:
        vault = lock_vault(connection, vault_id)
        request = _request_row(connection, vault_id, lock=True)
        if request is None or request["status"] != "running":
            return
        connection.execute(
            """UPDATE vault_deletions SET status='failed', heartbeat_at=NULL,
                   proven_empty_at=NULL, failed_at=now(), failure=%s
               WHERE vault_id=%s""", (Jsonb(failure), vault_id))
        record_audit_event(connection, event="folder_protection.vault_deletion_failed",
                           actor="system", vault_id=vault_id, outcome="failure",
                           vault_uuid=str(vault["uuid"]), vault_name=vault["name"], **failure)
        set_problem(connection, key=f"vault_deletion:{vault_id}", event=PROBLEM_EVENT,
                    vault_id=vault_id, active=True,
                    params={"vault": vault["name"], "vault_deletion": True,
                            "leftover": int(failure.get("leftover_versions", 0))
                            + int(failure.get("leftover_markers", 0))
                            + int(failure.get("leftover_uploads", 0))})


def execute(database: Database, client_factory: Callable[[], Any], vault_id: int) -> bool:
    """Run one claimed Vault Deletion. True when the Vault left the catalog."""
    progress = _Progress()
    with database() as connection:
        vault = lock_vault(connection, vault_id)
        request = _request_row(connection, vault_id)
        if request is None or request["status"] != "running":
            return False
        if vault["lifecycle"] != "tombstone" or not vault["root_released_at"]:
            # The CHECK and trigger make this unreachable; fail closed anyway.
            raise VaultDeletionError("Only a Vault Tombstone can be deleted")
        bucket, prefix = vault["s3_bucket"], vault_prefix(vault)
    try:
        client = client_factory()
        _delete_prefix(client, database, vault_id=vault_id, bucket=bucket, prefix=prefix,
                       progress=progress)
        leftover = list_prefix(client, bucket=bucket, prefix=prefix)
    except Exception as exc:
        _record_failure(database, vault_id=vault_id, failure=_failure_detail(exc),
                        progress=progress)
        return False
    if not leftover.empty:
        _record_failure(database, vault_id=vault_id, progress=progress, failure={
            "reason": "leftovers", "leftover_versions": len(leftover.versions),
            "leftover_markers": len(leftover.markers),
            "leftover_uploads": len(leftover.uploads)})
        return False
    with database() as connection:
        vault = lock_vault(connection, vault_id)
        request = _request_row(connection, vault_id, lock=True)
        if request is None or request["status"] != "running":
            return False
        connection.execute(
            "UPDATE vault_deletions SET proven_empty_at=now() WHERE vault_id=%s", (vault_id,))
        counts = _remove_catalog(connection, vault_id)
        record_audit_event(connection, event="folder_protection.vault_deleted",
                           actor=request["actor"], vault_id=vault_id, outcome="success",
                           vault_uuid=str(vault["uuid"]), vault_name=vault["name"],
                           prefix=prefix, reason=request["reason"],
                           requested_at=_iso(request["requested_at"]),
                           deleted_versions=progress.versions,
                           deleted_markers=progress.markers,
                           deleted_bytes=progress.bytes,
                           aborted_uploads=progress.uploads,
                           files=counts["files"], archive_versions=counts["archive_versions"],
                           jobs=counts["jobs"], executed_by="system", local_data="retained")
        connection.execute("DELETE FROM vaults WHERE id=%s", (vault_id,))
    return True


def run_due(database: Database, client_factory: Callable[[], Any], *,
            now: datetime | None = None) -> int:
    """Claim and run every due Vault Deletion; returns how many completed."""
    with database() as connection:
        claimed = claim_due(connection, now=now or datetime.now(timezone.utc))
    completed = 0
    for vault_id in claimed:
        try:
            completed += bool(execute(database, client_factory, vault_id))
        except Exception as exc:
            _record_failure(database, vault_id=vault_id, failure=_failure_detail(exc),
                            progress=_Progress())
    return completed
