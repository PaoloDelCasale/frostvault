"""DB-only discovery/admission and durable upload-remediation lifecycle.

Proof/receipts/cleanup belong to #460/#464. This ledger never performs provider
or filesystem I/O, never changes an original failure, and never grants PUT.
"""
from __future__ import annotations

import json
import uuid
from time import monotonic
from datetime import datetime, timedelta, timezone
from typing import Any

from . import upload_provenance as provenance
from .upload_snapshot import UploadSnapshot

POLL_SECONDS = 10
VAULT_BATCH = 10
TARGET_BATCH = 10
TERMINAL = ("completed", "failed", "cancelled")
RESOLUTION_SQL = """EXISTS (
    SELECT 1 FROM upload_remediation_originals o
    JOIN upload_remediations r ON r.id=o.remediation_id
    JOIN upload_remediation_jobs b ON b.remediation_id=r.id AND b.job_id=r.resolved_by_job_id
    JOIN jobs resolving ON resolving.id=b.job_id
    JOIN snapshot_cleanup_inventories cleanup ON cleanup.job_id=r.cleanup_inventory_job_id
    WHERE o.original_job_id=original.id AND r.state='resolved'
      AND resolving.status='completed' AND resolving.upload_reconcile_only
      AND resolving.archive_version_id=r.archive_version_id
      AND cleanup.keeper_id=r.archive_version_id AND cleanup.completed_at IS NOT NULL
)"""


def _invalidate(connection: Any, vault_id: int, stamp: str) -> None:
    from .catalog_events import CatalogEventStore
    # Do not flush aggregates or wait for maintenance. This is only a bounded,
    # durable invalidation; Local Copy protection evidence remains untouched.
    CatalogEventStore(connection).append_event(vault_id=vault_id,domain="catalog",scope="",
        payload={"invalidate":["files"],"reason":"upload_remediation"},created_at=stamp)


def _later(stamp: str) -> str:
    return (datetime.fromisoformat(stamp) + timedelta(seconds=POLL_SECONDS)).isoformat()


def _sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def capture_failure(connection: Any, job_id: int, stamp: str) -> None:
    """Append only originals with materialized facts, never failed remediations."""
    connection.execute("""INSERT INTO upload_remediation_discovery(job_id,vault_id,discovered_at)
        SELECT j.id,j.vault_id,%s FROM jobs j WHERE j.id=%s
          AND j.action='upload' AND j.status='failed' AND NOT j.upload_reconcile_only
          AND NOT EXISTS (SELECT 1 FROM upload_remediation_jobs b WHERE b.job_id=j.id)
          AND (j.archive_version_id IS NOT NULL OR EXISTS (
              SELECT 1 FROM upload_intents i JOIN upload_intent_jobs b ON b.intent_id=i.id
              WHERE b.job_id=j.id AND i.send_started)) ON CONFLICT DO NOTHING""", (stamp, job_id))


def _enroll(connection: Any, original: dict[str, Any], vault: dict[str, Any], stamp: str) -> dict[str, Any] | None:
    if original["upload_reconcile_only"] or original["status"] != "failed":
        return None
    existing = connection.execute("""SELECT r.* FROM upload_remediations r
        JOIN upload_remediation_originals o ON o.remediation_id=r.id WHERE o.original_job_id=%s""", (original["id"],)).fetchone()
    if existing:
        return existing
    intent = provenance.load_for_job(connection, original["id"])
    version = connection.execute("SELECT * FROM archive_versions WHERE id=%s AND vault_file_id=%s AND vault_id=%s",
        (original["archive_version_id"], original["vault_file_id"], vault["id"])).fetchone() if original["archive_version_id"] else None
    if not version and not (intent and intent["send_started"]):
        return None
    path = connection.execute("SELECT path,valid_from FROM file_paths WHERE vault_file_id=%s AND valid_to IS NULL", (original["vault_file_id"],)).fetchone()
    reason = None
    if intent:
        pinned = json.loads(intent["target_json"])
        snapshot_json = intent["snapshot_json"]
        try:
            snapshot = UploadSnapshot.from_json(snapshot_json)
            expected, size = snapshot.plaintext_sha256, snapshot.size
        except (ValueError,TypeError,KeyError,AttributeError):
            expected, size, reason = None, int(original["total_bytes"] or 0), "proof_mismatch"
        mode = intent["requested"]
        key = "intent:" + intent["id"]
        version_id = intent["archive_version_id"]
    else:
        from .vault_paths import expected_cloud_key
        pinned = {"vault_id": vault["id"], "vault_file_id": original["vault_file_id"],
            "bucket": vault["s3_bucket"], "prefix": vault["s3_prefix"], "key": version["object_key"],
            "path": original["path"], "source_root": vault["source_root"],
            "root_identity": vault.get("root_identity"), "root_identity_version": vault.get("root_identity_version"),
            "path_since": path["valid_from"] if path else None}
        expected = original["upload_plaintext_sha256"]
        snapshot_json = version["upload_snapshot_json"] or original["upload_snapshot_json"]
        size, mode = version["size"], version["upload_verification_requested"] or original["upload_verification_requested"] or "read_back"
        key, version_id = "version:" + version["id"], version["id"]
        if version["object_key"] != expected_cloud_key(original["path"], vault["s3_prefix"]):
            reason = "identity_changed"
        if not _sha(expected):
            reason = "expected_evidence_missing"
        if snapshot_json:
            try:
                snapshot = UploadSnapshot.from_json(snapshot_json)
                if snapshot.plaintext_sha256 != expected:
                    reason = "proof_mismatch"
            except (ValueError,TypeError,KeyError,AttributeError):
                reason = "proof_mismatch"
        if version["integrity"] == "verified" and version["plaintext_sha256"] != expected:
            reason = "proof_mismatch"
    if not path or path["path"] != original["path"]:
        reason = "identity_changed"
    if version and (version["availability"] == "purged" or version["integrity"] == "mismatch"):
        reason = "target_superseded" if version["availability"] == "purged" else "proof_mismatch"
    cancelled = connection.execute("""SELECT 1 FROM jobs j WHERE j.vault_file_id=%s AND j.action='upload'
        AND j.status='cancelled' AND (j.archive_version_id=%s OR EXISTS (
            SELECT 1 FROM upload_intent_jobs b WHERE b.job_id=j.id AND b.intent_id=%s)) LIMIT 1""",
        (original["vault_file_id"], version_id, intent["id"] if intent else None)).fetchone()
    state = "cancelled" if cancelled else "blocked" if reason else "pending"
    row = connection.execute("""INSERT INTO upload_remediations(id,target_key,vault_id,vault_file_id,
        intent_id,archive_version_id,target_json,expected_sha256,snapshot_json,requested_mode,size,
        state,phase,reason,due_at,created_at,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (target_key) DO NOTHING RETURNING *""",
        (str(uuid.uuid4()), key, vault["id"], original["vault_file_id"], intent["id"] if intent else None,
         version_id, provenance.canonical(pinned), expected, snapshot_json, mode, size, state,
         "cleanup" if version and version["integrity"] == "verified" else "verification", reason,
         stamp, stamp, stamp)).fetchone()
    if not row:
        row = connection.execute("SELECT * FROM upload_remediations WHERE target_key=%s OR intent_id=%s OR archive_version_id=%s", (key,intent["id"] if intent else None,version_id)).fetchone()
    # An AV identity alone cannot link contradictory expected content to a resolution.
    if row["expected_sha256"] != expected or row["snapshot_json"] != snapshot_json:
        connection.execute("UPDATE upload_remediations SET state='blocked',reason='proof_mismatch',updated_at=%s WHERE id=%s AND state<>'resolved'", (stamp,row["id"]))
        return row
    connection.execute("""INSERT INTO upload_remediation_originals(original_job_id,remediation_id,vault_file_id,vault_id)
        VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (original["id"],row["id"],row["vault_file_id"],row["vault_id"]))
    _invalidate(connection,vault["id"],stamp)
    return row


def discover_file(connection: Any, vault: dict[str, Any], file_id: str, stamp: str) -> None:
    """Targeted indexed history lookup, including originals outside recent50."""
    rows = connection.execute("""SELECT j.* FROM jobs j WHERE j.vault_file_id=%s
        AND j.action='upload' AND j.status='failed' AND NOT j.upload_reconcile_only
        AND NOT EXISTS (SELECT 1 FROM upload_remediation_originals o WHERE o.original_job_id=j.id)
        AND (j.archive_version_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM upload_intents i WHERE i.original_job_id=j.id AND i.send_started))
        ORDER BY j.id LIMIT %s""", (file_id,TARGET_BATCH)).fetchall()
    for original in rows:
        _enroll(connection, original, vault, stamp)


def _suspension(connection: Any, row: dict[str, Any], vault: dict[str, Any], *, automatic: bool) -> str | None:
    pinned = json.loads(row["target_json"])
    if (not vault["enabled"] or vault["decommission_state"] != "active" or vault["root_released_at"]
            or vault["relocation_state"] != "ready"):
        return "vault_suspended"
    if any(vault.get(k) != pinned.get(k) for k in ("source_root","root_identity","root_identity_version")):
        return "identity_changed"
    path = connection.execute("""SELECT fp.path,fp.valid_from,vf.status,lc.cloud_only_version_id
        FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id WHERE vf.id=%s AND vf.vault_id=%s""",
        (row["vault_file_id"],vault["id"])).fetchone()
    if not path or path["status"] != "active" or path["path"] != pinned["path"] or path["valid_from"] != pinned["path_since"]:
        return "identity_changed"
    if path["cloud_only_version_id"]:
        return "cloud_only_conflict"
    if connection.execute("SELECT 1 FROM jobs WHERE vault_file_id=%s AND status NOT IN ('failed','completed','cancelled') LIMIT 1", (row["vault_file_id"],)).fetchone():
        return "active_operation"
    if connection.execute("SELECT 1 FROM cloud_deletion_items WHERE vault_file_id=%s AND status='pending' LIMIT 1", (row["vault_file_id"],)).fetchone():
        return "pending_deletion"
    health = connection.execute("SELECT last_error FROM vault_component_health WHERE vault_id=%s AND component='source'", (vault["id"],)).fetchone()
    if health and health["last_error"]:
        return "source_volume_unavailable"
    if automatic:
        from .operation_policies import get_policy, auto_upload_path_is_included
        policy = get_policy(connection, vault["id"])
        if not policy.auto_upload or not auto_upload_path_is_included(pinned["path"],policy):
            return "protection_paused"
    return None


def admit(connection: Any, row: dict[str, Any], vault: dict[str, Any], *, user_id: int,
          stamp: str, automatic: bool = True, explicit: bool = False,
          group_id: str | None = None) -> dict[str, Any] | None:
    """Caller holds Vault lock. Unique active-file index protects EVERY action."""
    row = connection.execute("SELECT * FROM upload_remediations WHERE id=%s FOR UPDATE", (row["id"],)).fetchone()
    if row["state"] in {"cancelled","resolved","queued","retrying"} or (row["state"] == "blocked" and not explicit):
        return None
    if explicit and row["reason"] in {"proof_mismatch","expected_evidence_missing","target_superseded"}:
        return None
    if row["current_job_id"]:
        current = connection.execute("SELECT status FROM jobs WHERE id=%s",(row["current_job_id"],)).fetchone()
        if current and current["status"] not in TERMINAL:
            return None
    original_cancelled = connection.execute("""SELECT 1 FROM upload_remediation_originals o JOIN jobs j ON j.id=o.original_job_id
        WHERE o.remediation_id=%s AND j.status='cancelled' LIMIT 1""", (row["id"],)).fetchone()
    if original_cancelled:
        connection.execute("UPDATE upload_remediations SET state='cancelled',updated_at=%s WHERE id=%s", (stamp,row["id"]))
        return None
    reason = _suspension(connection,row,vault,automatic=automatic)
    if reason:
        connection.execute("UPDATE upload_remediations SET state='suspended',reason=%s,due_at=%s,updated_at=%s WHERE id=%s", (reason,_later(stamp),stamp,row["id"]))
        return None
    if row["archive_version_id"]:
        version = connection.execute("SELECT integrity,availability FROM archive_versions WHERE id=%s", (row["archive_version_id"],)).fetchone()
        if version["availability"] != "available" or version["integrity"] == "mismatch":
            connection.execute("UPDATE upload_remediations SET state='blocked',reason=%s,updated_at=%s WHERE id=%s", ("proof_mismatch" if version["integrity"] == "mismatch" else "target_unavailable",stamp,row["id"]))
            return None
    path = json.loads(row["target_json"])["path"]
    group = group_id or str(uuid.uuid4())
    job = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,
        requested_by,requested_at,updated_at,group_id,group_path,total_bytes,transferred_bytes,origin,
        upload_reconcile_only,upload_plaintext_sha256,upload_snapshot_json,upload_verification_requested,retry_count)
        VALUES (%s,%s,%s,%s,'upload','queued',%s,%s,%s,%s,%s,%s,0,%s,TRUE,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING RETURNING id""",
        (vault["id"],row["vault_file_id"],row["archive_version_id"],path,user_id,stamp,stamp,group,path,row["size"],
         "automatic" if automatic else "manual",row["expected_sha256"],row["snapshot_json"],row["requested_mode"],row["retry_count"])).fetchone()
    if not job:
        return None
    if row["intent_id"]:
        intent = connection.execute("SELECT * FROM upload_intents WHERE id=%s", (row["intent_id"],)).fetchone()
        provenance.bind_for_resume(connection,job["id"],intent)
    connection.execute("INSERT INTO upload_remediation_jobs(job_id,remediation_id,vault_file_id,vault_id) VALUES (%s,%s,%s,%s)", (job["id"],row["id"],row["vault_file_id"],vault["id"]))
    connection.execute("UPDATE upload_remediations SET state='queued',reason=NULL,current_job_id=%s,updated_at=%s WHERE id=%s", (job["id"],stamp,row["id"]))
    _invalidate(connection,vault["id"],stamp)
    return {"job_id":job["id"],"group_id":group}


def intercept_upload(connection: Any, *, vault_id: int, file_id: str, user_id: int,
                     stamp: str, automatic: bool, group_id: str | None = None) -> tuple[bool, dict[str, Any] | None]:
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()
    discover_file(connection,vault,file_id,stamp)
    row = connection.execute("SELECT * FROM upload_remediations WHERE vault_file_id=%s AND state<>'resolved' ORDER BY created_at,id LIMIT 1", (file_id,)).fetchone()
    if not row:
        return False,None
    return True,admit(connection,row,vault,user_id=user_id,stamp=stamp,automatic=automatic,group_id=group_id)


def schedule_resume(connection: Any, *, vault: dict[str, Any], path: str, user_id: int, timestamp: str, remediation_id: str | None = None) -> dict[str, Any]:
    current = connection.execute("SELECT * FROM vaults WHERE id=%s FOR UPDATE", (vault["id"],)).fetchone()
    file = connection.execute("SELECT vault_file_id FROM file_paths WHERE vault_id=%s AND path=%s AND valid_to IS NULL", (vault["id"],path)).fetchone()
    if not file:
        raise provenance.UploadBlocked("identity_changed")
    discover_file(connection,current,file["vault_file_id"],timestamp)
    rows = connection.execute("SELECT * FROM upload_remediations WHERE vault_file_id=%s AND vault_id=%s AND state<>'resolved' AND (%s::text IS NULL OR id=%s) ORDER BY created_at,id LIMIT 2", (file["vault_file_id"],vault["id"],remediation_id,remediation_id)).fetchall()
    if remediation_id and not rows:
        raise provenance.UploadBlocked("identity_changed")
    if not rows:
        # Preserve #464's explicit claimed cleanup of a scan-discovered block.
        # No unrelated failed Job is resolved by electing a current keeper here.
        result = provenance._schedule_resume_legacy(connection,vault=current,path=path,user_id=user_id,timestamp=timestamp)
        job = connection.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (result["job_id"],)).fetchone()
        intent = provenance.load_for_job(connection,job["id"])
        from .vault_paths import expected_cloud_key
        pinned = json.loads(intent["target_json"]) if intent else provenance.target(connection,job,key=expected_cloud_key(path,current["s3_prefix"]),provider="db-fence")
        episode = connection.execute("""INSERT INTO upload_remediations(id,target_key,vault_id,vault_file_id,intent_id,
            archive_version_id,target_json,expected_sha256,snapshot_json,requested_mode,size,state,due_at,
            created_at,updated_at,current_job_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s) RETURNING id""",
            (str(uuid.uuid4()),"intent:"+intent["id"] if intent else "version:"+job["archive_version_id"],current["id"],job["vault_file_id"],
             intent["id"] if intent else None,job["archive_version_id"],provenance.canonical(pinned),job["upload_plaintext_sha256"],
             job["upload_snapshot_json"],job["upload_verification_requested"],job["total_bytes"],timestamp,timestamp,timestamp,job["id"])).fetchone()
        connection.execute("INSERT INTO upload_remediation_jobs(job_id,remediation_id,vault_file_id,vault_id) VALUES (%s,%s,%s,%s)", (job["id"],episode["id"],job["vault_file_id"],current["id"]))
        return result
    if len(rows) != 1:
        raise provenance.UploadBlocked("ambiguous_receipt")
    result = admit(connection,rows[0],current,user_id=user_id,stamp=timestamp,automatic=False,explicit=True)
    if not result:
        raise provenance.UploadBlocked("identity_changed")
    return result


def require_job(connection: Any, job: dict[str, Any]) -> None:
    row = connection.execute("""SELECT r.* FROM upload_remediations r
        JOIN upload_remediation_jobs b ON b.remediation_id=r.id WHERE b.job_id=%s""", (job["id"],)).fetchone()
    if not row:
        return
    current = connection.execute("SELECT * FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (row["vault_id"],)).fetchone()
    pinned = json.loads(row["target_json"])
    cancelled = connection.execute("""SELECT 1 FROM upload_remediation_originals o
        JOIN jobs j ON j.id=o.original_job_id WHERE o.remediation_id=%s AND j.status='cancelled' LIMIT 1""", (row["id"],)).fetchone()
    if (row["current_job_id"] != job["id"] or row["state"] in {'cancelled','resolved','blocked'} or cancelled
            or not current["upload_reconcile_only"] or current["vault_file_id"] != row["vault_file_id"]
            or current["upload_plaintext_sha256"] != row["expected_sha256"]
            or current["upload_snapshot_json"] != row["snapshot_json"]
            or current["upload_verification_requested"] != row["requested_mode"]
            or (row["archive_version_id"] and current["archive_version_id"] != row["archive_version_id"])
            or any(vault.get(k) != pinned.get(k) for k in ("source_root","root_identity","root_identity_version"))):
        raise provenance.UploadBlocked("identity_changed")


def record_cleanup(connection: Any, job_id: int, inventory_job_id: int) -> None:
    # #464 reuses ORIGINAL intent inventories across attempts. Keep its exact
    # identity, not a second authority journal and not a latest/keeper guess.
    connection.execute("""UPDATE upload_remediations r SET cleanup_inventory_job_id=%s,
        phase='completion' FROM upload_remediation_jobs b
        WHERE b.remediation_id=r.id AND b.job_id=%s AND r.current_job_id=b.job_id""",
        (inventory_job_id,job_id))


def transition(connection: Any, job_id: int, status: str, stamp: str, *, suspension: str | None = None) -> None:
    """Called in the authoritative Job transaction before notifications."""
    row = connection.execute("""SELECT r.* FROM upload_remediations r
        JOIN upload_remediation_jobs b ON b.remediation_id=r.id WHERE b.job_id=%s""", (job_id,)).fetchone()
    if not row:
        if status == "failed":
            capture_failure(connection,job_id,stamp)
        return
    job = connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if row["current_job_id"] != job_id:
        raise RuntimeError("Stale upload remediation attempt")
    phase = row["phase"]
    if job["archive_version_id"]:
        av = connection.execute("SELECT integrity FROM archive_versions WHERE id=%s", (job["archive_version_id"],)).fetchone()
        phase = "cleanup" if av["integrity"] == "verified" else "verification"
    state,reason = row["state"],row["reason"]
    if status == "completed":
        cleanup = connection.execute("SELECT * FROM snapshot_cleanup_inventories WHERE job_id=%s", (row["cleanup_inventory_job_id"],)).fetchone()
        intent = provenance.load_for_job(connection,job_id)
        if (not job["upload_reconcile_only"] or not job["archive_version_id"] or not cleanup
                or cleanup["keeper_id"] != job["archive_version_id"] or not cleanup["completed_at"]
                or (intent and not intent["settled"])):
            raise RuntimeError("Upload remediation completion requires exact cleanup postcondition")
        state,phase,reason = "resolved","completed",None
    elif status == "failed":
        block = connection.execute("SELECT reason FROM snapshot_reconciliation_blocks WHERE vault_file_id=%s", (row["vault_file_id"],)).fetchone()
        state,reason = "blocked",block["reason"] if block else "retry_budget_exhausted" if job["retry_count"] >= 8 else "operation_failed"
    elif status == "cancelled":
        state,reason = "cancelled","owner_cancelled"
    elif status == "retrying":
        state,reason = "retrying","transient"
    elif suspension:
        state,reason = "suspended",suspension
    elif status in {"uploading","verifying"}:
        state,reason = "queued",None
    connection.execute("""UPDATE upload_remediations SET state=%s,phase=%s,reason=%s,retry_count=%s,
        due_at=%s,updated_at=%s,archive_version_id=COALESCE(archive_version_id,%s),
        resolved_by_job_id=CASE WHEN %s THEN %s ELSE resolved_by_job_id END,
        resolved_at=CASE WHEN %s THEN %s ELSE resolved_at END WHERE id=%s""",
        (state,phase,reason,job["retry_count"],job["retry_after"] or stamp,stamp,job["archive_version_id"],
         state=="resolved",job_id,state=="resolved",stamp,row["id"]))
    if (state,phase,reason) != (row["state"],row["phase"],row["reason"]):
        _invalidate(connection,row["vault_id"],stamp)


def retry_vault(connection: Any, vault: dict[str, Any], user_id: int, stamp: str) -> int:
    # Same arbiter, bounded explicit retry. A cancelled target is never rearmed;
    # accumulated retry_count is retained even after a manual correction.
    originals = connection.execute("""SELECT j.* FROM upload_remediation_discovery q
        JOIN jobs j ON j.id=q.job_id WHERE q.vault_id=%s ORDER BY q.job_id LIMIT %s""", (vault["id"],TARGET_BATCH)).fetchall()
    for original in originals:
        _enroll(connection,original,vault,stamp)
        connection.execute("DELETE FROM upload_remediation_discovery WHERE job_id=%s",(original["id"],))
    rows = connection.execute("""SELECT r.* FROM upload_remediations r WHERE vault_id=%s
        AND state IN ('pending','blocked','suspended')
        AND COALESCE(reason,'') NOT IN ('proof_mismatch','expected_evidence_missing','target_superseded')
        AND (current_job_id IS NULL OR EXISTS (SELECT 1 FROM jobs j WHERE j.id=r.current_job_id
             AND j.status IN ('failed','completed','cancelled')))
        ORDER BY updated_at,id LIMIT %s""", (vault["id"],TARGET_BATCH)).fetchall()
    return sum(bool(admit(connection,row,vault,user_id=user_id,stamp=stamp,explicit=True)) for row in rows)


def _fair_vaults(connection: Any, name: str, stamp: str) -> list[dict[str, Any]]:
    cursor = connection.execute("SELECT * FROM upload_remediation_sweep WHERE name=%s FOR UPDATE SKIP LOCKED", (name,)).fetchone()
    if not cursor:
        return []
    predicate = ("EXISTS (SELECT 1 FROM upload_remediation_discovery q WHERE q.vault_id=v.id)" if name == "discovery"
        else "EXISTS (SELECT 1 FROM upload_remediations r WHERE r.vault_id=v.id AND ((r.state IN ('pending','suspended') AND r.due_at<=%s AND (r.current_job_id IS NULL OR EXISTS (SELECT 1 FROM jobs current WHERE current.id=r.current_job_id AND current.status IN ('completed','failed','cancelled')))) OR (r.state IN ('queued','retrying','suspended') AND EXISTS (SELECT 1 FROM jobs j WHERE j.id=r.current_job_id AND j.status='cancelled'))))")
    params = [] if name == "discovery" else [stamp]
    rows = connection.execute(f"SELECT v.* FROM vaults v WHERE {predicate} ORDER BY (v.id>%s) DESC,v.id LIMIT %s", (*params,cursor["last_vault_id"],VAULT_BATCH)).fetchall()
    connection.execute("UPDATE upload_remediation_sweep SET last_tick_at=%s WHERE name=%s", (stamp,name))
    return rows


def sweep(db: Any, stamp: str | None = None) -> int:
    """Bounded round-robin across Vaults; no maintenance/operation worker needed."""
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    count = 0
    for name in ("discovery","admission"):
        deadline = monotonic() + 1.0
        with db() as connection:
            connection.execute("SET LOCAL lock_timeout='100ms'")
            connection.execute("SET LOCAL statement_timeout='2s'")
            vaults = _fair_vaults(connection,name,stamp)
        for candidate in vaults:
            if monotonic() >= deadline:
                break
            with db() as connection:
                connection.execute("UPDATE upload_remediation_sweep SET last_vault_id=%s WHERE name=%s", (candidate["id"],name))
            with db() as connection:
                connection.execute("SET LOCAL lock_timeout='100ms'")
                connection.execute("SET LOCAL statement_timeout='2s'")
                vault = connection.execute("SELECT * FROM vaults WHERE id=%s FOR UPDATE SKIP LOCKED", (candidate["id"],)).fetchone()
                if not vault:
                    continue
                if name == "discovery":
                    rows = connection.execute("SELECT j.* FROM upload_remediation_discovery q JOIN jobs j ON j.id=q.job_id WHERE q.vault_id=%s ORDER BY q.job_id LIMIT %s", (vault["id"],TARGET_BATCH)).fetchall()
                    for original in rows:
                        if monotonic() >= deadline:
                            break
                        _enroll(connection,original,vault,stamp)
                        connection.execute("DELETE FROM upload_remediation_discovery WHERE job_id=%s", (original["id"],))
                else:
                    from ..owner import owner_user_id
                    owner_id = owner_user_id(connection)
                    if owner_id is None:
                        continue
                    rows = connection.execute("""SELECT r.* FROM upload_remediations r WHERE vault_id=%s
                        AND ((r.state IN ('pending','suspended') AND r.due_at<=%s
                              AND (r.current_job_id IS NULL OR EXISTS (SELECT 1 FROM jobs current
                                   WHERE current.id=r.current_job_id AND current.status IN ('completed','failed','cancelled')))) OR
                             (r.state IN ('queued','retrying','suspended') AND EXISTS (
                                SELECT 1 FROM jobs j WHERE j.id=r.current_job_id AND j.status='cancelled')))
                        ORDER BY due_at,id LIMIT %s""", (vault["id"],stamp,TARGET_BATCH)).fetchall()
                    for row in rows:
                        if monotonic() >= deadline:
                            break
                        if row["current_job_id"]:
                            current = connection.execute("SELECT status FROM jobs WHERE id=%s",(row["current_job_id"],)).fetchone()
                            if current and current["status"] == "cancelled":
                                transition(connection,row["current_job_id"],"cancelled",stamp)
                                continue
                        count += bool(admit(connection,row,vault,user_id=owner_id,stamp=stamp))
    return count


def project(connection: Any, row: dict[str, Any]) -> dict[str, Any]:
    state = row["state"]
    return {k:row[k] for k in ("id","state","phase","reason","retry_count","due_at","current_job_id","resolved_by_job_id","resolved_at","archive_version_id")} | {
        "can_resume": state in {"pending","blocked","suspended"} and row["reason"] not in {
            "proof_mismatch","expected_evidence_missing","target_superseded","source_volume_unavailable","vault_suspended"},
        "original_job_ids": [r["original_job_id"] for r in connection.execute(
            "SELECT original_job_id FROM upload_remediation_originals WHERE remediation_id=%s ORDER BY original_job_id LIMIT 50", (row["id"],)).fetchall()],
        "remedy": provenance.REMEDIES.get(row["reason"],"Keep copies. Correct the reported condition and retry exact reconciliation; no fresh upload is authorized."),
    }


def file_projection(connection: Any, vault_id: int, path: str) -> list[dict[str, Any]]:
    rows = connection.execute("""SELECT r.* FROM upload_remediations r JOIN file_paths fp ON fp.vault_file_id=r.vault_file_id
        WHERE r.vault_id=%s AND fp.valid_to IS NULL AND fp.path=%s ORDER BY r.created_at DESC,r.id LIMIT 50""", (vault_id,path)).fetchall()
    return [project(connection,row) for row in rows]


def project_jobs(connection: Any, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not jobs:
        return jobs
    links = connection.execute("""SELECT original.id job_id,r.* FROM jobs original
        LEFT JOIN upload_remediation_originals o ON o.original_job_id=original.id
        LEFT JOIN upload_remediation_jobs b ON b.job_id=original.id
        JOIN upload_remediations r ON r.id=COALESCE(o.remediation_id,b.remediation_id)
        WHERE original.id=ANY(%s)""", ([row["id"] for row in jobs],)).fetchall()
    by_job = {r["job_id"]:project(connection,r) for r in links}
    return [{**row,"remediation":by_job.get(row["id"])} for row in jobs]


def observe(db: Any) -> None:
    from . import metrics
    current = datetime.now(timezone.utc)
    def age(value: str | None) -> float:
        return max(0.0,(current-datetime.fromisoformat(value)).total_seconds()) if value else 0.0
    with db() as connection:
        inbox = connection.execute("SELECT count(*) n,min(discovered_at) oldest FROM upload_remediation_discovery").fetchone()
        due = connection.execute("SELECT count(*) n,min(due_at) oldest FROM upload_remediations WHERE state='pending' AND due_at<=%s", (current.isoformat(),)).fetchone()
        states = connection.execute("SELECT state,count(*) n FROM upload_remediations WHERE state<>'resolved' GROUP BY state").fetchall()
    metrics.set_gauge("upload_remediation_discovery_backlog",inbox["n"])
    metrics.set_gauge("upload_remediation_discovery_oldest_age_seconds",age(inbox["oldest"]))
    metrics.set_gauge("upload_remediation_due_queue",due["n"])
    metrics.set_gauge("upload_remediation_due_oldest_age_seconds",age(due["oldest"]))
    counts = {r["state"]:r["n"] for r in states}
    for state in ("pending","queued","retrying","blocked","suspended","cancelled"):
        metrics.set_gauge("upload_remediation_states",counts.get(state,0),state=state)
    metrics.set_gauge("upload_remediation_last_tick_unixtime",current.timestamp())
