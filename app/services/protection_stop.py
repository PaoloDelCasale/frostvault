"""Stop protecting a folder; local data is never deleted (#428, #594).

Stop Protection moves the Vault to ``stopping`` and, once nothing of it is
still running, to the ``tombstone`` that releases its root. With Delayed Cloud
Deletion the root stays allocated until every Job of that request is terminal,
so another Vault can never inherit a pending deletion's local path.

The choice is not stored twice: it is the Vault's Stop Protection request
(``cloud_purge_requests.stop_protection``). Its cancellation, or the Owner
choosing to keep the cloud content after a failure, retires the request and
the cloud content is kept.
"""
from __future__ import annotations

from typing import Any

from ..database import db
from . import cloud_deletion, source_directories
from .audit_events import record_audit_event
from .bulk_upload_admission import has_pending

_TERMINAL = ("completed", "failed", "cancelled")


def local_work_suspended(vault: dict[str, Any]) -> bool:
    return str(vault.get("lifecycle") or "active") in {"stopping", "tombstone"}


def _stop_group(connection: Any, vault_id: int) -> str | None:
    row = connection.execute(
        """SELECT g.id FROM job_groups g
           JOIN cloud_purge_requests r ON r.group_id = g.id AND r.stop_protection
           WHERE g.vault_id=%s ORDER BY g.created_at DESC, g.id LIMIT 1""",
        (vault_id,)).fetchone()
    return str(row["id"]) if row else None


def _group_jobs(connection: Any, vault_id: int, group_id: str) -> list[dict[str, Any]]:
    return connection.execute(
        """SELECT id, status, run_after AS pending_until FROM jobs
           WHERE vault_id=%s AND group_id=%s ORDER BY id""", (vault_id, group_id)).fetchall()


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def status(connection: Any, vault_id: int) -> dict[str, Any]:
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()
    if not vault:
        raise ValueError("Vault not found")
    group_id = _stop_group(connection, vault_id)
    result = {"vault_id": vault_id, "vault_name": vault["name"],
              "stopped": vault["lifecycle"] in {"stopping", "tombstone"},
              "root_released": bool(vault["root_released_at"]),
              "cloud_disposition": "delete" if group_id else "keep",
              "group_id": group_id, "jobs": [], "cancellable": False, "pending_until": None}
    if group_id:
        jobs = [{"id": int(j["id"]), "status": j["status"], "pending_until": _iso(j["pending_until"])}
                for j in _group_jobs(connection, vault_id, group_id)]
        result.update(jobs=jobs,
                      cancellable=bool(jobs) and all(j["status"] == "pending_delay" for j in jobs),
                      pending_until=max((j["pending_until"] for j in jobs if j["pending_until"]),
                                        default=None))
    return result


def _active_work(connection: Any, vault_id: int) -> bool:
    return connection.execute(
        """SELECT 1 FROM jobs WHERE vault_id=%s
             AND status NOT IN ('completed','failed','cancelled') LIMIT 1""",
        (vault_id,)).fetchone() is not None


def stop_protection(connection: Any, *, vault_id: int, actor: str,
                    cloud_disposition: str, confirmation: str, reason: str) -> dict[str, Any]:
    source_directories.lock_vault_root_allocation(connection)
    vault = cloud_deletion.lock_vault(connection, vault_id)
    if confirmation != vault["name"]:
        raise cloud_deletion.ConfirmationRequired("Type the exact Vault name to confirm")
    if cloud_disposition not in {"keep", "delete"} or not reason.strip():
        raise ValueError("Choose keep or delete and provide a reason")
    if vault["lifecycle"] in {"stopping", "tombstone"}:
        group_id = _stop_group(connection, vault_id)
        existing = "delete" if group_id else "keep"
        if existing == cloud_disposition:
            return status(connection, vault_id)
        if (group_id and cloud_disposition == "keep" and not vault["root_released_at"]
                and not _active_work(connection, vault_id)):
            # Keep the cloud content after a failed deletion: retire the
            # request; completed deletions are not undone.
            connection.execute(
                "UPDATE cloud_purge_requests SET stop_protection=false WHERE group_id=%s",
                (group_id,))
            record_audit_event(connection, event="folder_protection.cloud_retained", actor=actor,
                               vault_id=vault_id, outcome="success", reason=reason.strip(),
                               group_id=group_id, completed_deletions_not_undone=True)
            reconcile_one(connection, vault_id)
            return status(connection, vault_id)
        raise ValueError("Folder protection has already stopped")
    if cloud_disposition == "delete" and vault["lifecycle"] != "active":
        raise ValueError("Reconnect must complete before deleting cloud content")
    if has_pending(connection, vault_id) or _active_work(connection, vault_id):
        raise ValueError("Wait for active Jobs or cancel pending work first")
    at = cloud_deletion.now_iso()
    group_id = None
    if cloud_disposition == "delete":
        # No local-data removal or exception to the Cloud-only gate is possible.
        if cloud_deletion._selected_files(connection, vault_id=vault_id, paths=[],
                                           is_directory=False, whole_vault=True):
            scheduled = cloud_deletion.schedule_cloud_purge(
                connection, vault_id=vault_id, paths=[], is_directory=False, whole_vault=True,
                actor=actor, requested_at=at, confirmation=confirmation,
                reason=reason, origin="stop-protection")
            group_id = scheduled.group_id
    connection.execute("UPDATE vaults SET lifecycle='stopping', paused=false WHERE id=%s",
                       (vault_id,))
    record_audit_event(connection, event="folder_protection.stopped", actor=actor,
                       vault_id=vault_id, outcome="requested", cloud_disposition=cloud_disposition,
                       group_id=group_id, reason=reason.strip(), local_data="retained")
    reconcile_one(connection, vault_id)
    return status(connection, vault_id)


def reconcile_one(connection: Any, vault_id: int) -> None:
    """Release the root of a stopping Vault once nothing of it can still run."""
    source_directories.lock_vault_root_allocation(connection)
    vault = cloud_deletion.lock_vault(connection, vault_id)
    if vault["lifecycle"] != "stopping":
        return
    group_id = _stop_group(connection, vault_id)
    if group_id:
        jobs = _group_jobs(connection, vault_id, group_id)
        # Fail closed until the Owner explicitly chooses keep after a failure.
        if not jobs or any(j["status"] not in {"completed", "cancelled"} for j in jobs):
            return
        if any(j["status"] == "cancelled" for j in jobs):
            connection.execute(
                "UPDATE cloud_purge_requests SET stop_protection=false WHERE group_id=%s",
                (group_id,))
    if _active_work(connection, vault_id):
        return
    connection.execute(
        "UPDATE vaults SET lifecycle='tombstone', root_released_at=now() WHERE id=%s", (vault_id,))
    record_audit_event(connection, event="folder_protection.root_released", actor="system",
                       vault_id=vault_id, outcome="success", local_data="retained")


def cancel_deletion(connection: Any, *, vault_id: int, actor: str) -> dict[str, Any]:
    source_directories.lock_vault_root_allocation(connection)
    cloud_deletion.lock_vault(connection, vault_id)
    group_id = _stop_group(connection, vault_id)
    if not group_id:
        raise ValueError("No pending cloud deletion")
    cloud_deletion.cancel_cloud_deletion(connection, vault_id=vault_id, group_id=group_id,
                                         actor=actor, cancelled_at=cloud_deletion.now_iso())
    reconcile_one(connection, vault_id)
    return status(connection, vault_id)


def reconcile_all() -> None:
    with db() as connection:
        rows = connection.execute("SELECT id FROM vaults WHERE lifecycle='stopping'").fetchall()
    for row in rows:
        with db() as connection:
            reconcile_one(connection, int(row["id"]))
