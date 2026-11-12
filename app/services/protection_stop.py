"""Stop protecting a folder; local data is never deleted (#428).

Historical Vault lifecycle columns remain internal root-occupancy fencing, not
an exposed decommission workflow. Stop+delete owns its root until every Job is
terminal, so another Vault can never inherit a delayed deletion's local path.
"""
from __future__ import annotations

from typing import Any

from ..database import db
from . import cloud_deletion, source_directories
from .audit_events import record_audit_event
from .bulk_upload_admission import has_pending


def local_work_suspended(vault: dict[str, Any]) -> bool:
    return str(vault.get("decommission_state") or "active") != "active"


def status(connection: Any, vault_id: int) -> dict[str, Any]:
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()
    if not vault:
        raise ValueError("Vault not found")
    stop = connection.execute("SELECT * FROM protection_stops WHERE vault_id=%s", (vault_id,)).fetchone()
    result = {"vault_id": vault_id, "vault_name": vault["name"],
              "stopped": vault["decommission_state"] != "active",
              "root_released": bool(vault["root_released_at"]),
              "cloud_disposition": stop["cloud_disposition"] if stop else "keep",
              "group_id": stop["group_id"] if stop else None,
              "jobs": [], "cancellable": False, "pending_until": None}
    if stop and stop["group_id"]:
        jobs = connection.execute("""SELECT id,status,pending_until FROM jobs
            WHERE vault_id=%s AND group_id=%s ORDER BY id""", (vault_id, stop["group_id"])).fetchall()
        result.update(jobs=jobs, cancellable=bool(jobs) and all(j["status"] == "pending_delay" for j in jobs),
                      pending_until=max((j["pending_until"] for j in jobs if j["pending_until"]), default=None))
    return result


def stop_protection(connection: Any, *, vault_id: int, actor: str,
                    cloud_disposition: str, confirmation: str, reason: str) -> dict[str, Any]:
    source_directories.lock_vault_root_allocation(connection)
    vault = cloud_deletion.lock_vault(connection, vault_id)
    if confirmation != vault["name"]:
        raise cloud_deletion.ConfirmationRequired("Type the exact Vault name to confirm")
    if cloud_disposition not in {"keep", "delete"} or not reason.strip():
        raise ValueError("Choose keep or delete and provide a reason")
    if vault["decommission_state"] != "active":
        existing = connection.execute("SELECT * FROM protection_stops WHERE vault_id=%s", (vault_id,)).fetchone()
        if existing and existing["cloud_disposition"] == cloud_disposition:
            return status(connection, vault_id)
        if existing and cloud_disposition == "keep" and not vault["root_released_at"]:
            active = connection.execute("""SELECT 1 FROM jobs WHERE vault_id=%s
                AND status NOT IN ('completed','failed','cancelled') LIMIT 1""", (vault_id,)).fetchone()
            if not active:
                connection.execute("UPDATE protection_stops SET cloud_disposition='keep' WHERE vault_id=%s", (vault_id,))
                record_audit_event(connection, event="folder_protection.cloud_retained", actor=actor,
                                   vault_id=vault_id, outcome="success", reason=reason.strip(),
                                   group_id=existing["group_id"], completed_deletions_not_undone=True)
                reconcile_one(connection, vault_id)
                return status(connection, vault_id)
        raise ValueError("Folder protection has already stopped")
    if cloud_disposition == "delete" and vault["relocation_state"] != "ready":
        raise ValueError("Reconnect must complete before deleting cloud content")
    if has_pending(connection, vault_id) or connection.execute("""SELECT 1 FROM jobs WHERE vault_id=%s
        AND status NOT IN ('completed','failed','cancelled') LIMIT 1""", (vault_id,)).fetchone():
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
    connection.execute("""INSERT INTO protection_stops(vault_id,requested_at,
        cloud_disposition,group_id,reason) VALUES (%s,%s,%s,%s,%s)""",
                       (vault_id, at, cloud_disposition, group_id, reason.strip()))
    connection.execute("UPDATE vaults SET decommission_state='decommissioning' WHERE id=%s", (vault_id,))
    record_audit_event(connection, event="folder_protection.stopped", actor=actor,
                       vault_id=vault_id, outcome="requested", cloud_disposition=cloud_disposition,
                       group_id=group_id, reason=reason.strip(), local_data="retained")
    reconcile_one(connection, vault_id)
    return status(connection, vault_id)


def reconcile_one(connection: Any, vault_id: int) -> None:
    source_directories.lock_vault_root_allocation(connection)
    vault = cloud_deletion.lock_vault(connection, vault_id)
    stop = connection.execute("SELECT * FROM protection_stops WHERE vault_id=%s", (vault_id,)).fetchone()
    if not stop or vault["root_released_at"]:
        return
    if stop["group_id"] and stop["cloud_disposition"] == "delete":
        jobs = connection.execute("SELECT status FROM jobs WHERE vault_id=%s AND group_id=%s",
                                  (vault_id, stop["group_id"])).fetchall()
        # Fail closed until the Owner explicitly chooses keep after a failure.
        if not jobs or any(j["status"] not in {"completed", "cancelled"} for j in jobs):
            return
        if any(j["status"] == "cancelled" for j in jobs):
            connection.execute("UPDATE protection_stops SET cloud_disposition='keep' WHERE vault_id=%s", (vault_id,))
    at = cloud_deletion.now_iso()
    connection.execute("""UPDATE vaults SET decommission_state='decommissioned',
        decommissioned_at=%s,root_released_at=%s WHERE id=%s""", (at, at, vault_id))
    record_audit_event(connection, event="folder_protection.root_released", actor="system",
                       vault_id=vault_id, outcome="success", local_data="retained")


def cancel_deletion(connection: Any, *, vault_id: int, actor: str) -> dict[str, Any]:
    source_directories.lock_vault_root_allocation(connection)
    cloud_deletion.lock_vault(connection, vault_id)
    stop = connection.execute("SELECT * FROM protection_stops WHERE vault_id=%s", (vault_id,)).fetchone()
    if not stop or not stop["group_id"]:
        raise ValueError("No pending cloud deletion")
    cloud_deletion.cancel_cloud_deletion(connection, vault_id=vault_id, group_id=stop["group_id"],
                                         actor=actor, cancelled_at=cloud_deletion.now_iso())
    connection.execute("UPDATE protection_stops SET cloud_disposition='keep' WHERE vault_id=%s", (vault_id,))
    reconcile_one(connection, vault_id)
    return status(connection, vault_id)


def reconcile_all() -> None:
    with db() as connection:
        rows = connection.execute("SELECT vault_id FROM protection_stops").fetchall()
    for row in rows:
        with db() as connection:
            reconcile_one(connection, int(row["vault_id"]))
