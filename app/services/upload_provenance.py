"""Pre-write upload facts. Receipts are NOT publication or deletion authority.

No provider I/O in replayable DB units. An uncertain send can only reconcile;
zero token matches never prove absence, and a fresh lease cannot quiesce old I/O.
#459 consumes load_for_job/bind_for_resume; this module has no sweep/history timer.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .database_retry import retry_database_unit
from .upload_snapshot import UploadSnapshot
from .s3_copy import _object_version_entries

TOKEN_KEY = "frostvault-upload"
REMEDIES = {
    "ambiguous_send": "Keep copies and stop external S3 writers. Retry exact reconciliation after the provider outcome is known; zero matches is not proof of no write.",
    "ambiguous_receipt": "Keep all copies. Inspect exact versions with authorized S3 tools; do not choose latest or delete by token.",
    "inventory_changed": "Keep copies and stop external S3 writers. Retry reconciliation only after the namespace is quiescent.",
    "identity_changed": "Restore the expected Source Volume/Vault path and resolve incompatible operations before retrying reconciliation.",
    "proof_mismatch": "Contradictory independent checksum/identity evidence blocks publication and deletion. Keep copies and investigate the exact version; never downgrade to a permissive proof.",
    "proof_unavailable": "Make the exact version readable (explicit restore for cold storage), check permissions and retry reconciliation. No automatic restore or deletion.",
    "orphan_different": "The verified keeper is retained. Recover/export the different exact version using authorized S3 tools or the Recovery Kit; ask for assistance reconciling it. No arbitrary removal action.",
    "keeper_missing": "Restore access to the exact verified keeper before cleanup; keep every other copy.",
    "markers_present": "Keep delete markers. Replacement cleanup has no consent to remove newly observed markers; reconcile explicitly with authorized S3 tools.",
}


class UploadBlocked(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        self.remedy = REMEDIES[reason]
        super().__init__(f"Upload reconciliation blocked ({reason}). {self.remedy}")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def provider_identity(client: Any) -> str:
    # Namespace identity only, never credentials. Fakes explicitly identify themselves.
    meta = getattr(client, "meta", None)
    return canonical({"endpoint": getattr(meta, "endpoint_url", "s3-compatible-test"),
                      "region": getattr(getattr(meta, "config", None), "region_name", None)})


def inventory(client: Any, bucket: str, key: str) -> dict[str, list[str]]:
    versions, markers = _object_version_entries(client, bucket=bucket, object_key=key)
    return {"versions": sorted(versions), "markers": sorted(markers)}


def target(connection: Any, job: dict[str, Any], *, key: str, provider: str) -> dict[str, Any]:
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s FOR UPDATE", (job["vault_id"],)).fetchone()
    row = connection.execute("SELECT vf.status,fp.path,fp.valid_from FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL WHERE vf.id=%s AND vf.vault_id=%s", (job["vault_file_id"], job["vault_id"])).fetchone()
    current = connection.execute("SELECT status,vault_file_id,path,claim_token FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    if (not vault or not row or row["status"] != "active" or not current
            or current["status"] in {"cancelled", "failed", "completed"}
            or current["vault_file_id"] != job["vault_file_id"] or current["path"] != job["path"]
            or row["path"] != job["path"] or vault["source_root"] != job["source_root"]
            or vault["s3_bucket"] != job["s3_bucket"] or vault["s3_prefix"] != job["s3_prefix"]
            or not vault["enabled"] or vault["decommission_state"] != "active"
            or vault["relocation_state"] != "ready" or vault.get("root_released_at")):
        raise UploadBlocked("identity_changed")
    incompatible = connection.execute("SELECT 1 FROM jobs WHERE vault_file_id=%s AND id<>%s AND status NOT IN ('failed','cancelled','completed') LIMIT 1", (job["vault_file_id"], job["id"])).fetchone()
    conflict = connection.execute("SELECT cloud_only_version_id FROM local_copies WHERE vault_file_id=%s", (job["vault_file_id"],)).fetchone()
    if incompatible or (conflict and conflict["cloud_only_version_id"]):
        raise UploadBlocked("identity_changed")
    return {"vault_id": job["vault_id"], "vault_file_id": job["vault_file_id"],
            "bucket": vault["s3_bucket"], "prefix": vault["s3_prefix"], "key": key,
            "provider": provider, "path": row["path"], "path_since": row["valid_from"],
            "source_root": vault["source_root"], "root_identity": vault.get("root_identity"),
            "root_identity_version": vault.get("root_identity_version")}


def require_target(connection: Any, job: dict[str, Any], pinned: dict[str, Any]) -> None:
    if target(connection, job, key=pinned["key"], provider=pinned["provider"]) != pinned:
        raise UploadBlocked("identity_changed")


def load_for_job(connection: Any, job_id: int) -> dict[str, Any] | None:
    return connection.execute("SELECT i.* FROM upload_intents i JOIN upload_intent_jobs j ON j.intent_id=i.id WHERE j.job_id=%s", (job_id,)).fetchone()


def outstanding(connection: Any, file_id: str) -> dict[str, Any] | None:
    rows = connection.execute("SELECT * FROM upload_intents WHERE vault_file_id=%s AND NOT settled ORDER BY created_at,id LIMIT 2", (file_id,)).fetchall()
    if len(rows) > 1:
        raise UploadBlocked("ambiguous_send")
    return rows[0] if rows else None


def bind_for_resume(connection: Any, job_id: int, intent: dict[str, Any]) -> None:
    original = connection.execute("SELECT status FROM jobs WHERE id=%s", (intent["original_job_id"],)).fetchone()
    cancelled = connection.execute("SELECT 1 FROM upload_intent_jobs b JOIN jobs j ON j.id=b.job_id WHERE b.intent_id=%s AND j.status='cancelled' LIMIT 1", (intent["id"],)).fetchone()
    if not original or original["status"] == "cancelled" or cancelled:
        raise UploadBlocked("identity_changed")
    connection.execute("INSERT INTO upload_intent_jobs(job_id,intent_id) VALUES (%s,%s) ON CONFLICT (job_id) DO NOTHING", (job_id, intent["id"]))


def prepare(connection: Any, job: dict[str, Any], *, pinned: dict[str, Any], snapshot: UploadSnapshot,
            requested: str, before: dict[str, Any], timestamp: str) -> dict[str, Any]:
    require_target(connection, job, pinned)
    existing = load_for_job(connection, job["id"])
    if existing:
        return existing
    previous = outstanding(connection, job["vault_file_id"])
    if previous:
        # Caller MUST reconcile before hashing a different generation.
        raise UploadBlocked("ambiguous_send")
    row = connection.execute("INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,target_json,snapshot_json,requested,inventory_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *", (str(uuid.uuid4()), str(uuid.uuid4()), job["vault_id"], job["vault_file_id"], job["id"], canonical(pinned), snapshot.as_json(), requested, canonical(before), timestamp)).fetchone()
    connection.execute("INSERT INTO upload_intent_jobs(job_id,intent_id) VALUES (%s,%s)", (job["id"], row["id"]))
    return row


def mark_send_started(connection: Any, job: dict[str, Any], intent: dict[str, Any]) -> None:
    require_target(connection, job, json.loads(intent["target_json"]))
    changed = connection.execute("UPDATE upload_intents SET send_started=TRUE WHERE id=%s AND NOT send_started RETURNING id", (intent["id"],)).fetchone()
    if not changed:
        raise UploadBlocked("ambiguous_send")


@retry_database_unit
def append_receipt(db: Any, intent_id: str, response: dict[str, Any], timestamp: str) -> None:
    # Allowlist facts: never HTTP headers, cookies, credentials or provider errors.
    allowed = {k: response[k] for k in ("VersionId", "ETag", "ChecksumSHA256", "ChecksumType", "ContentLength", "StorageClass") if k in response}
    text = canonical(allowed)
    with db() as connection:
        connection.execute("INSERT INTO upload_receipts(intent_id,receipt_hash,response_json,received_at) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (intent_id, hashlib.sha256(text.encode()).hexdigest(), text, timestamp))


def receipt(connection: Any, intent_id: str) -> dict[str, Any] | None:
    rows = connection.execute("SELECT response_json FROM upload_receipts WHERE intent_id=%s", (intent_id,)).fetchall()
    if not rows:
        return None
    responses = [json.loads(r["response_json"]) for r in rows]
    # Discovery may add fields, but incompatible proofs/identities never overwrite.
    combined: dict[str, Any] = {}
    for response in responses:
        for key, value in response.items():
            if key in combined and combined[key] != value:
                raise UploadBlocked("ambiguous_receipt")
            combined[key] = value
    if not combined.get("VersionId") or combined["VersionId"] == "null":
        raise UploadBlocked("ambiguous_receipt")
    return combined


def reconcile(client: Any, intent: dict[str, Any], saved: dict[str, Any] | None,
              check_active: Any) -> dict[str, Any]:
    pinned = json.loads(intent["target_json"])
    if provider_identity(client) != pinned["provider"]:
        raise UploadBlocked("identity_changed")
    before = json.loads(intent["inventory_json"])
    first = inventory(client, pinned["bucket"], pinned["key"])
    matches = []
    for version in first["versions"]:
        check_active()
        head = client.head_object(Bucket=pinned["bucket"], Key=pinned["key"], VersionId=version)
        if str(head.get("VersionId") or "") != version or head.get("DeleteMarker"):
            raise UploadBlocked("ambiguous_receipt")
        if (head.get("Metadata") or {}).get(TOKEN_KEY) == intent["token"]:
            matches.append((version, head))
    if first != inventory(client, pinned["bucket"], pinned["key"]):
        raise UploadBlocked("inventory_changed")
    if len(matches) != 1:
        raise UploadBlocked("ambiguous_send" if not matches else "ambiguous_receipt")
    version, head = matches[0]
    if (version in before["versions"] or first["markers"] != before["markers"]
            or set(first["versions"]) != set(before["versions"]) | {version}):
        raise UploadBlocked("inventory_changed")
    if saved and str(saved["VersionId"]) != version:
        raise UploadBlocked("ambiguous_receipt")
    from .upload_snapshot import validated_s3_proof, UploadProofMismatch
    try:
        expected = UploadSnapshot.from_json(intent["snapshot_json"])
        validated_s3_proof(head, version_id=version, snapshot=expected, source="exact_head")
        if saved:
            validated_s3_proof(saved, version_id=version, snapshot=expected, source="upload_response", completion=True)
    except UploadProofMismatch as exc:
        raise UploadBlocked("proof_mismatch") from exc
    # Retain raw response proof if present; worker validates BOTH head and receipt
    # against the independent frozen baseline, never trusting this token as proof.
    return {**head, **(saved or {}), "VersionId": version}


def describe(connection: Any, *, vault_id: int, path: str) -> dict[str, Any]:
    row = connection.execute("SELECT vf.id FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL WHERE vf.vault_id=%s AND fp.path=%s", (vault_id, path)).fetchone()
    empty = {"pending": False, "can_resume": False, "reason": None, "remedy": None}
    if not row:
        return empty
    file_id = row["id"]
    block = connection.execute("SELECT reason FROM snapshot_reconciliation_blocks WHERE vault_file_id=%s", (file_id,)).fetchone()
    intents = connection.execute("SELECT id,blocked_reason FROM upload_intents WHERE vault_file_id=%s AND NOT settled ORDER BY created_at LIMIT 2", (file_id,)).fetchall()
    if not block and not intents:
        return empty
    reason = (block["reason"] if block else intents[0]["blocked_reason"]) or "ambiguous_send"
    busy = connection.execute("SELECT 1 FROM jobs WHERE vault_file_id=%s AND status NOT IN ('failed','cancelled','completed') LIMIT 1", (file_id,)).fetchone()
    cancelled = connection.execute("SELECT 1 FROM jobs j LEFT JOIN upload_intent_jobs b ON b.job_id=j.id WHERE j.vault_file_id=%s AND j.status='cancelled' AND (j.upload_reconcile_only OR b.intent_id IS NOT NULL) LIMIT 1", (file_id,)).fetchone()
    return {"pending": True, "can_resume": not busy and not cancelled and len(intents) < 2,
            "reason": reason, "remedy": REMEDIES.get(reason, REMEDIES["proof_unavailable"])}


def schedule_resume(connection: Any, *, vault: dict[str, Any], path: str, user_id: int, timestamp: str) -> dict[str, Any]:
    from .cloud_deletion import lock_vault
    from .vault_moves import require_ready
    current = lock_vault(connection, vault["id"])
    require_ready(current)
    state = describe(connection, vault_id=vault["id"], path=path)
    if not state["pending"] or not state["can_resume"]:
        raise UploadBlocked("identity_changed")
    row = connection.execute("SELECT vf.id,lc.cloud_only_version_id FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id WHERE vf.vault_id=%s AND fp.path=%s AND vf.status='active'", (vault["id"], path)).fetchone()
    if not row or row["cloud_only_version_id"]:
        raise UploadBlocked("identity_changed")
    intent = outstanding(connection, row["id"])
    linked = connection.execute("SELECT * FROM archive_versions WHERE vault_file_id=%s AND availability='available' AND integrity='verified' ORDER BY version_number DESC LIMIT 1", (row["id"],)).fetchone() if not intent else None
    if not intent and not linked:
        raise UploadBlocked("proof_unavailable")
    job = connection.execute("INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,requested_by,requested_at,updated_at,group_id,group_path,total_bytes,transferred_bytes,origin,upload_reconcile_only,upload_plaintext_sha256,upload_snapshot_json,upload_verification_requested) VALUES (%s,%s,%s,%s,'upload','queued',%s,%s,%s,%s,%s,%s,0,'manual',TRUE,%s,%s,%s) RETURNING id,group_id", (vault["id"], row["id"], intent["archive_version_id"] if intent else linked["id"], path, user_id, timestamp, timestamp, str(uuid.uuid4()), path, UploadSnapshot.from_json(intent["snapshot_json"]).size if intent else linked["size"], UploadSnapshot.from_json(intent["snapshot_json"]).plaintext_sha256 if intent else linked["plaintext_sha256"], intent["snapshot_json"] if intent else linked["upload_snapshot_json"], intent["requested"] if intent else linked["upload_verification_requested"])).fetchone()
    if intent:
        bind_for_resume(connection, job["id"], intent)
    return {"job_id": job["id"], "group_id": job["group_id"]}
