"""Durable DB-only production of manual bulk uploads (#550).

An admission is not a transfer Job. Every bounded unit locks Vault -> request,
then commits its cursor and Jobs together. No in-memory continuation is needed.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .operation_policies import get_policy

BATCH_SIZE = 100
VAULT_BATCH = 8
POLL_SECONDS = 0.25
_IDENTITY_KEYS = ("source_root", "root_identity", "root_identity_version",
                  "reconnect_volume_fingerprint", "target_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(vault: Any) -> str:
    return json.dumps({key: vault.get(key) for key in _IDENTITY_KEYS}, sort_keys=True, default=str)


def _ready(vault: Any) -> bool:
    # paused deliberately does not participate: admission is not a claim.
    return vault["lifecycle"] == "active"


def _scope(path: str) -> tuple[str, list[Any]]:
    return ("starts_with(f.path, %s)", [path + "/"]) if path else ("TRUE", [])


def public_status(row: Any) -> dict[str, Any]:
    return {"group_id": str(row["group_id"])} | {key: row[key] for key in (
        "vault_id", "path", "status", "reason", "requested_at", "updated_at",
        "origin", "verification_mode", "selected_count", "processed_count", "admitted_count", "total_bytes")}


def get(connection: Any, *, vault_id: int, group_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM bulk_upload_admissions WHERE vault_id=%s AND group_id=%s",
                             (vault_id, group_id)).fetchone()
    return public_status(row) if row else None


def create(connection: Any, *, vault_id: int, path: str, user_id: int,
           request_id: str | None = None) -> dict[str, Any]:
    """One short admission transaction, no per-file reads/remediation/inserts.

    Callers normalize the directory path. A caller-supplied UUID is the stable
    idempotency key and group identifier, including for terminal replays.
    """
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s FOR NO KEY UPDATE", (vault_id,)).fetchone()
    if not vault:
        raise LookupError("Vault not found")
    group_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
    existing = connection.execute("SELECT * FROM bulk_upload_admissions WHERE group_id=%s", (group_id,)).fetchone()
    if existing:
        # One Owner (#586): the scope alone identifies a replay.
        if existing["vault_id"] != vault_id or existing["path"] != path:
            raise ValueError("Upload request identifier already used for a different scope")
        return public_status(existing)
    if not _ready(vault):
        raise ValueError("Vault is not ready for upload admission")
    scope, params = _scope(path)
    stamp = _now()
    # Acceptance snapshots File identities, not upload eligibility: the Files
    # of the scope that exist at requested_at (later ones are not admitted).
    # The bounded queue_jobs arbiter below checks every eligibility condition.
    selection = connection.execute(f"""
        SELECT count(*) AS selected FROM files f
        WHERE f.vault_id=%s AND f.created_at<=%s AND {scope}
    """, [vault_id, stamp, *params]).fetchone()
    row = connection.execute("""
        INSERT INTO bulk_upload_admissions(group_id,vault_id,requested_at,updated_at,
            path,verification_mode,vault_identity_json,selected_count,status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *
    """, (group_id, vault_id, stamp, stamp, path,
          get_policy(connection, vault_id).effective_upload_verification(), _identity(vault),
          selection["selected"], "pending" if selection["selected"] else "completed")).fetchone()
    if row is None:
        # Same key concurrently submitted under a different Vault lock.
        raise ValueError("Upload request identifier already used for a different scope")
    return public_status(row)


def has_pending(connection: Any, vault_id: int) -> bool:
    return connection.execute("SELECT 1 FROM bulk_upload_admissions WHERE vault_id=%s AND status='pending' LIMIT 1",
                              (vault_id,)).fetchone() is not None


def cancel(connection: Any, *, vault_id: int, group_id: str | None = None, path: str = "") -> int:
    """Caller holds the Vault lock, like Job cancellation.

    Persist subtree exclusions even if the cursor has not reached that folder.
    Whole-group/whole-Vault cancellation stops the producer before any Job exists.
    The return value counts stopped admissions (not transfer Jobs).
    """
    if group_id is not None or not path:
        return connection.execute("""UPDATE bulk_upload_admissions
            SET status='cancelled',reason='owner_cancelled',updated_at=%s
            WHERE vault_id=%s AND status='pending' AND (%s::text IS NULL OR group_id=%s)
            RETURNING group_id""", (_now(), vault_id, group_id, group_id)).rowcount
    rows = connection.execute("""SELECT group_id,path FROM bulk_upload_admissions
        WHERE vault_id=%s AND status='pending'
          AND (path='' OR path=%s OR starts_with(path,%s) OR starts_with(%s,path || '/'))""",
        (vault_id, path, path + "/", path)).fetchall()
    stopped = 0
    for row in rows:
        if row["path"] == path or row["path"].startswith(path + "/"):
            stopped += cancel(connection, vault_id=vault_id, group_id=row["group_id"])
        else:
            connection.execute("INSERT INTO bulk_upload_exclusions(group_id,path) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                               (row["group_id"], path))
    return stopped


def advance(connection: Any, *, vault_id: int, group_id: str, batch_size: int = BATCH_SIZE) -> bool:
    """One bounded transaction; its owner must commit before calling again.

    Skip a busy Vault instead of holding a request row while waiting for it.
    No eligibility predicates enter cursor pagination: becoming protected, active,
    cancelled or excluded cannot shift later candidates out of the window.
    """
    if not 1 <= batch_size <= BATCH_SIZE:
        raise ValueError("Invalid admission batch size")
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s FOR NO KEY UPDATE SKIP LOCKED", (vault_id,)).fetchone()
    if not vault:
        return False
    request = connection.execute("SELECT * FROM bulk_upload_admissions WHERE vault_id=%s AND group_id=%s FOR UPDATE",
                                 (vault_id, group_id)).fetchone()
    if not request or request["status"] != "pending":
        return False
    stamp = _now()
    reason = "vault_suspended" if not _ready(vault) else "identity_changed" if _identity(vault) != request["vault_identity_json"] else None
    if reason:
        connection.execute("UPDATE bulk_upload_admissions SET status='invalidated',reason=%s,updated_at=%s WHERE group_id=%s",
                           (reason, stamp, group_id))
        return True
    scope, params = _scope(request["path"])
    rows = connection.execute(f"""
        SELECT f.id, f.path, NOT EXISTS (
            SELECT 1 FROM bulk_upload_exclusions e WHERE e.group_id=%s
              AND (f.path=e.path OR starts_with(f.path,e.path || '/'))
        ) AS included
        FROM files f
        WHERE f.vault_id=%s AND f.created_at<=%s
          AND (%s::uuid IS NULL OR f.id>%s::uuid) AND {scope}
        ORDER BY f.id LIMIT %s
    """, [group_id, vault_id, request["requested_at"], request["cursor"], request["cursor"],
          *params, batch_size]).fetchall()
    file_ids = [str(row["id"]) for row in rows if row["included"]]
    job_ids, total_bytes = [], 0
    if file_ids:
        from ..catalog import ArchiveCatalog
        job_ids, total_bytes, _ = ArchiveCatalog(connection).queue_jobs(
            vault_id=vault_id, path=request["path"], action="upload",
            requested_at=stamp, group_id=group_id, is_directory=True,
            whole_vault=not request["path"], origin=request["origin"], admission_file_ids=file_ids,
            admission_verification_mode=request["verification_mode"])
    completed = len(rows) < batch_size
    # Files deleted since acceptance are skipped. At exhaustion all initially
    # selected Files have been considered, including those no longer present.
    connection.execute("""UPDATE bulk_upload_admissions SET cursor=%s,
        processed_count=CASE WHEN %s THEN selected_count ELSE LEAST(selected_count,processed_count+%s) END,
        admitted_count=admitted_count+%s,total_bytes=total_bytes+%s,status=%s,updated_at=%s
        WHERE group_id=%s""",
        (rows[-1]["id"] if rows else request["cursor"], completed, len(rows), len(job_ids), total_bytes,
         "completed" if completed else "pending", stamp, group_id))
    return True


def tick(connection_factory: Callable[..., Any]) -> int:
    """At most one request per Vault per tick; every advance commits separately."""
    with connection_factory() as connection:
        requests = connection.execute("""WITH heads AS (
            SELECT DISTINCT ON (vault_id) group_id
            FROM bulk_upload_admissions WHERE status='pending'
            ORDER BY vault_id,requested_at,group_id
        ) SELECT a.group_id,a.vault_id
          FROM bulk_upload_admissions a JOIN heads h ON h.group_id=a.group_id
          ORDER BY a.last_attempted_at NULLS FIRST,a.updated_at,a.vault_id
          LIMIT %s FOR UPDATE OF a SKIP LOCKED""", (VAULT_BATCH,)).fetchall()
        if requests:
            # Commit the rotation BEFORE the Vault-first batch transactions.
            # A skipped lock, rollback, or restart must not pin the oldest eight
            # requests forever. This is attempt accounting, never progress.
            connection.execute("""UPDATE bulk_upload_admissions SET last_attempted_at=%s
                WHERE group_id=ANY(%s::uuid[])""",
                (_now(), [row["group_id"] for row in requests]))
    advanced = 0
    failure = None
    for row in requests:
        try:
            with connection_factory() as connection:
                advanced += int(advance(connection, vault_id=row["vault_id"], group_id=row["group_id"]))
        except Exception as exc:
            # Roll back only this batch and still serve other Vaults. The loop
            # reports the failure and the persisted cursor retries next tick.
            failure = exc
    if failure is not None:
        raise failure
    return advanced
