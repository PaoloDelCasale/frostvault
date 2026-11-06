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
from urllib.parse import urlsplit, urlunsplit

from .database_retry import owned_connection, retry_database_unit
from .upload_snapshot import UploadSnapshot
from .s3_copy import _object_version_entries

TOKEN_KEY = "frostvault-upload"
ABSENT_OPERATION = 'ProvenAbsent'
# Refusals journaled by the SDK seam, plus the outcome proven by complete S3 reads.
NO_WRITE_OPERATIONS = frozenset({'PutObject','CreateMultipartUpload','UploadPart','CompleteMultipartUpload',ABSENT_OPERATION})
REMEDIES = {
    "ambiguous_send": "Keep copies and stop external S3 writers. Retry exact reconciliation after the provider outcome is known; zero matches is not proof of no write.",
    "list_permission_denied": "S3 refused to list object versions or incomplete multipart uploads for this file, so reconciliation cannot see what was sent. Keep all copies, grant the archive IAM identity s3:ListBucketVersions and s3:ListBucketMultipartUploads on the bucket, then use Retry failed uploads.",
    "ambiguous_receipt": "Keep all copies. Inspect exact versions with authorized S3 tools; do not choose latest or delete by token.",
    "inventory_changed": "Keep all copies. The exact cloud inventory changed or could not be confirmed; this does not by itself identify an external writer. Check listing permissions and any other S3 activity, then use Retry failed uploads to retry exact reconciliation when the namespace is stable.",
    "cleanup_unavailable": "The verified keeper is retained; cloud cleanup could not finish. Check provider permissions/connectivity and retry exact reconciliation. Completed/in-flight deletes cannot be undone.",
    "identity_changed": "Restore the expected Source Volume/Vault path and resolve incompatible operations before retrying reconciliation.",
    "proof_mismatch": "Contradictory independent checksum/identity evidence blocks publication and deletion. Keep copies and investigate the exact version; never downgrade to a permissive proof.",
    "proof_unavailable": "Make the exact version readable (explicit restore for cold storage), check permissions and retry reconciliation. No automatic restore or deletion.",
    "orphan_different": "The verified keeper is retained. Recover/export the different exact version using authorized S3 tools or the Recovery Kit; ask for assistance reconciling it. No arbitrary removal action.",
    "keeper_missing": "Restore access to the exact verified keeper before cleanup; keep every other copy.",
    "markers_present": "Keep delete markers. Replacement cleanup has no consent to remove newly observed markers; reconcile explicitly with authorized S3 tools.",
    "active_operation": "Another operation on this file is still running or waiting to retry. Wait for it to finish; nothing changed in the file, path or Source Volume.",
    "source_volume_unavailable": "The Source Volume is not available right now. It resumes by itself once the volume is back; the file, path and Vault were not changed.",
    "vault_suspended": "The Vault is disabled, being relocated or decommissioned. Resume it to continue; the file, path and Vault root were not changed.",
    "pending_deletion": "A cloud deletion for this file is pending. Cancel or complete it before reconciling.",
    "protection_paused": "Automatic protection is paused for this file. Resume protection or reconcile manually.",
    "cloud_only_conflict": "The path holds different content than a file kept only in the cloud. Choose which copy to keep before reconciling.",
    "already_resolved": "Nothing to reconcile: this upload has already been resolved.",
    "expected_evidence_missing": "The independent expected proof for this upload is missing, so exact reconciliation cannot start. Keep all copies and ask for assistance.",
    "target_superseded": "A newer verified version already protects this file; the older target is retained and cannot be reconciled.",
    "target_unavailable": "The exact version to reconcile is not available. Keep all copies and check the exact version with authorized S3 tools.",
    "owner_cancelled": "This reconciliation was cancelled and is never resurrected automatically. Keep copies and start a new upload when the file changes.",
}


class UploadBlocked(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        self.remedy = REMEDIES.get(reason, REMEDIES["proof_unavailable"])
        super().__init__(f"Upload reconciliation blocked ({reason}). {self.remedy}")


class ProvenAbsent(UploadBlocked):
    """Positive S3 proof that no send of this intent landed or can still complete.

    Subclasses UploadBlocked so a caller that does not handle the proof keeps the
    fail-closed ``ambiguous_send`` block; only the worker seam acts on it.
    """

    def __init__(self) -> None:
        super().__init__("ambiguous_send")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def provider_identity(client: Any) -> str:
    # Namespace identity only, never credentials. Fakes explicitly identify themselves.
    meta = getattr(client, "meta", None)
    raw = getattr(meta, "endpoint_url", "s3-compatible-test")
    if not isinstance(raw, str):
        raw = "s3-compatible-test"
    parsed = urlsplit(raw)
    endpoint = urlunsplit((parsed.scheme, (parsed.hostname or "") + (f":{parsed.port}" if parsed.port else ""), parsed.path, "", "")) if parsed.scheme else raw
    region = getattr(getattr(meta, "config", None), "region_name", None)
    return canonical({"endpoint": endpoint, "region": region if isinstance(region, str) else None})


def inventory(client: Any, bucket: str, key: str) -> dict[str, list[str]]:
    versions, markers = _guarded_listing(lambda: _object_version_entries(client, bucket=bucket, object_key=key))
    return {"versions": sorted(versions), "markers": sorted(markers)}


_LIST_DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException"})


def _guarded_listing(call: Any) -> Any:
    """Run a provider LIST and name why it failed.

    A refused listing is a permission gap of this file's reconciliation, never an
    installation readiness failure: the readiness probe does not exercise it, so
    letting the error reach the worker's readiness branch would reopen the gate
    and re-claim the Job forever (#484). Credential, network and 5xx failures say
    nothing about the inventory and propagate to be suspended/retried; anything
    else is an unconfirmed inventory. The block is raised outside the ``except``
    so it carries no chained provider error that would be read as readiness.
    """
    reason: str | None
    try:
        return call()
    except UploadBlocked:
        raise
    except Exception as exc:
        reason = _listing_block_reason(exc)
        if reason is None:
            raise
    raise UploadBlocked(reason)


def _listing_block_reason(error: BaseException) -> str | None:
    from .database_retry import exception_chain
    from botocore.exceptions import ClientError
    if any(isinstance(item, ClientError) and item.response.get("Error", {}).get("Code") in _LIST_DENIED_CODES
           for item in exception_chain(error)):
        return "list_permission_denied"
    return None if _is_provider_unreachable(error) else "inventory_changed"


def _is_provider_unreachable(error: BaseException) -> bool:
    """Credential, network and provider-side failures: retried or suspended, never a changed inventory."""
    from .cloud_preflight_gate import is_readiness_error
    from .database_retry import exception_chain
    from botocore.exceptions import BotoCoreError, ClientError, ConnectionError as BotoConnectionError
    return is_readiness_error(error) or any(
        isinstance(item, (BotoConnectionError, ConnectionError, TimeoutError))
        or (isinstance(item, BotoCoreError) and "timeout" in type(item).__name__.lower())
        or (isinstance(item, ClientError)
            and item.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) in range(500, 600))
        for item in exception_chain(error))


def target(connection: Any, job: dict[str, Any], *, key: str, provider: str) -> dict[str, Any]:
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s FOR NO KEY UPDATE", (job["vault_id"],)).fetchone()
    from .upload_remediation import require_job
    require_job(connection, job)
    row = connection.execute("SELECT vf.status,fp.path,fp.valid_from FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL WHERE vf.id=%s AND vf.vault_id=%s", (job["vault_file_id"], job["vault_id"])).fetchone()
    current = connection.execute("SELECT status,vault_file_id,path,claim_token FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    if (not vault or not row or row["status"] != "active" or not current
            or current["status"] in {"cancelled", "failed", "completed"}
            or current["vault_file_id"] != job["vault_file_id"] or current["path"] != job["path"]
            or row["path"] != job["path"] or vault["source_root"] != job["source_root"]
            or vault["s3_bucket"] != job["s3_bucket"] or vault["s3_prefix"] != job["s3_prefix"]
            ):
        raise UploadBlocked("identity_changed")
    if (not vault["enabled"] or vault["decommission_state"] != "active"
            or vault["relocation_state"] != "ready" or vault.get("root_released_at")):
        raise UploadBlocked("vault_suspended")
    incompatible = connection.execute("SELECT 1 FROM jobs WHERE vault_file_id=%s AND id<>%s AND status NOT IN ('failed','cancelled','completed') LIMIT 1", (job["vault_file_id"], job["id"])).fetchone()
    conflict = connection.execute("SELECT cloud_only_version_id FROM local_copies WHERE vault_file_id=%s", (job["vault_file_id"],)).fetchone()
    if incompatible:
        raise UploadBlocked("active_operation")
    if conflict and conflict["cloud_only_version_id"]:
        raise UploadBlocked("cloud_only_conflict")
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
    if receipt(connection, intent['id']) is not None:
        raise UploadBlocked('ambiguous_receipt')
    changed = connection.execute("UPDATE upload_intents SET send_started=TRUE WHERE id=%s AND NOT send_started RETURNING id", (intent["id"],)).fetchone()
    if not changed:
        raise UploadBlocked("ambiguous_send")
    from datetime import datetime, timezone
    from .upload_remediation_discovery import capture
    capture(connection, datetime.now(timezone.utc).isoformat(), intent_id=intent["id"])


@retry_database_unit
def append_receipt(db: Any, intent_id: str, response: dict[str, Any], timestamp: str) -> None:
    # Allowlist facts: never HTTP headers, cookies, credentials or provider errors.
    allowed = {k: response[k] for k in ("VersionId", "ETag", "ChecksumSHA256", "ChecksumType", "ContentLength", "StorageClass") if k in response}
    text = canonical(allowed)
    with owned_connection(db()) as connection:
        connection.execute("INSERT INTO upload_receipts(intent_id,receipt_hash,response_json,received_at) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (intent_id, hashlib.sha256(text.encode()).hexdigest(), text, timestamp))


def record_no_write(connection: Any, job: dict[str, Any], intent: dict[str, Any],
                    operation: str, timestamp: str) -> None:
    """Claim-fenced definitive refusal; retain the immutable outcome as a fact.

    Called ONLY after the transfer manager joined all I/O and reported an auth
    refusal without a final receipt. Zero listing matches never reach this seam.
    Caller owns/retries its entire DB-only publication transaction.
    """
    if operation not in NO_WRITE_OPERATIONS:
        raise UploadBlocked('ambiguous_send')
    require_target(connection,job,json.loads(intent['target_json']))
    if receipt(connection,intent['id']) is not None:
        raise UploadBlocked('ambiguous_receipt')
    changed = connection.execute("UPDATE upload_intents SET send_started=FALSE WHERE id=%s AND send_started AND NOT settled AND archive_version_id IS NULL RETURNING id",(intent['id'],)).fetchone()
    if not changed:
        raise UploadBlocked('ambiguous_receipt')
    text = canonical({'NoWriteOperation':operation})
    connection.execute("INSERT INTO upload_receipts(intent_id,receipt_hash,response_json,received_at) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (intent['id'],hashlib.sha256(text.encode()).hexdigest(),text,timestamp))


def receipt(connection: Any, intent_id: str) -> dict[str, Any] | None:
    rows = connection.execute("SELECT response_json FROM upload_receipts WHERE intent_id=%s", (intent_id,)).fetchall()
    if not rows:
        return None
    responses = [json.loads(r["response_json"]) for r in rows]
    # Explicit, allowlisted no-write facts are history, never a VersionId/proof.
    # Do not ignore malformed responses or arbitrary dictionaries without IDs.
    responses = [r for r in responses if not (set(r) == {'NoWriteOperation'}
        and r['NoWriteOperation'] in NO_WRITE_OPERATIONS)]
    if not responses:
        return None
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
    # A crashed worker's request can complete between the two complete reads.
    # Re-observe ONCE only for that exact, independently proven app write. Never
    # rebase the pinned inventory, adopt unknown deltas or retry external churn.
    for observation in range(2):
        first = inventory(client, pinned['bucket'], pinned['key'])
        matches = []
        for version in first['versions']:
            check_active()
            head = client.head_object(Bucket=pinned['bucket'], Key=pinned['key'], VersionId=version)
            if str(head.get('VersionId') or '') != version or head.get('DeleteMarker'):
                raise UploadBlocked('ambiguous_receipt')
            if (head.get('Metadata') or {}).get(TOKEN_KEY) == intent['token']:
                matches.append((version, head))
        second = inventory(client, pinned['bucket'], pinned['key'])
        if first == second:
            break
        if observation or not _own_write_arrived(client, intent, saved, before, first, second, check_active):
            raise UploadBlocked('inventory_changed')
    if len(matches) > 1:
        raise UploadBlocked("ambiguous_receipt")
    if not matches:
        if saved is None and first == before:
            _prove_absent(client, pinned, before, check_active)
        raise UploadBlocked("ambiguous_send")
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


def _incomplete_uploads(client: Any, bucket: str, key: str) -> list[str]:
    """Exact-key incomplete multipart uploads; unconfirmed completeness fails closed."""
    lister = getattr(client, "list_multipart_uploads", None)
    if lister is None:
        raise UploadBlocked("ambiguous_send")
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": key}
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    while True:
        page = _guarded_listing(lambda: lister(**kwargs))
        if not isinstance(page, dict) or page.get("IsTruncated") not in (True, False) or "IsTruncated" not in page:
            raise UploadBlocked("ambiguous_send")
        found += [str(item.get("UploadId") or "?") for item in page.get("Uploads") or [] if item.get("Key") == key]
        if not page["IsTruncated"]:
            return found
        marker = (str(page.get("NextKeyMarker") or ""), str(page.get("NextUploadIdMarker") or ""))
        if not marker[0] or marker in seen:
            raise UploadBlocked("ambiguous_send")
        seen.add(marker)
        kwargs["KeyMarker"], kwargs["UploadIdMarker"] = marker[0], marker[1]
        if not marker[1]:
            kwargs.pop("UploadIdMarker")


def _prove_absent(client: Any, pinned: dict[str, Any], before: dict[str, Any],
                  check_active: Any) -> None:
    """Raise ProvenAbsent only when S3 positively shows that nothing landed.

    Callers already read two equal complete listings that equal the pinned
    pre-send inventory and found no token carrier. Here: no incomplete multipart
    upload exists for the exact key, and a later complete listing is still the
    pinned inventory (so a request finishing meanwhile would be seen). Any read
    failure, unsupported API, unconfirmed completeness or delta stays blocked.
    The sender is gone: reconcile runs only for a Job that is the file's sole
    active operation, and an intent receipt (even partial) is never ignored.
    """
    check_active()
    if _incomplete_uploads(client, pinned["bucket"], pinned["key"]):
        raise UploadBlocked("ambiguous_send")
    check_active()
    if inventory(client, pinned["bucket"], pinned["key"]) != before:
        raise UploadBlocked("inventory_changed")
    raise ProvenAbsent()


def _own_write_arrived(client: Any, intent: dict[str, Any], saved: dict[str, Any] | None,
                       before: dict[str, Any], first: dict[str, Any], second: dict[str, Any],
                       check_active: Any) -> bool:
    added = set(second['versions']) - set(first['versions'])
    if (first != before or second['markers'] != before['markers'] or len(added) != 1
            or set(second['versions']) != set(before['versions']) | added):
        return False
    version = next(iter(added))
    if saved and saved['VersionId'] != version:
        raise UploadBlocked('ambiguous_receipt')
    pinned = json.loads(intent['target_json'])
    check_active()
    from .upload_snapshot import validated_s3_proof, UploadProofMismatch, unsupported_checksum_api
    try:
        head = client.head_object(Bucket=pinned['bucket'], Key=pinned['key'], VersionId=version, ChecksumMode='ENABLED')
    except Exception as exc:
        if unsupported_checksum_api(exc):
            return False  # Capability absence is not independent attribution.
        raise
    if str(head.get('VersionId') or '') != version or head.get('DeleteMarker'):
        raise UploadBlocked('ambiguous_receipt')
    if (head.get('Metadata') or {}).get(TOKEN_KEY) != intent['token']:
        return False
    try:
        expected = UploadSnapshot.from_json(intent['snapshot_json'])
        proof = validated_s3_proof(head, version_id=version, snapshot=expected, source='exact_head')
        if saved:
            validated_s3_proof(saved, version_id=version, snapshot=expected, source='upload_response', completion=True)
    except UploadProofMismatch as exc:
        raise UploadBlocked('proof_mismatch') from exc
    # Token/size/catalog membership alone never attribute an automatic retry.
    return proof is not None


def _legacy_materialized(connection: Any, file_id: str) -> dict[str, Any] | None:
    """Manual exact resume of failed linked pre-0064 uploads, without backfill."""
    rows = connection.execute("""SELECT av.*,j.upload_plaintext_sha256 AS expected_sha,
        j.upload_snapshot_json AS job_snapshot
        FROM archive_versions av JOIN jobs j ON j.archive_version_id=av.id
        JOIN file_paths fp ON fp.vault_file_id=av.vault_file_id AND fp.valid_to IS NULL
        WHERE av.vault_file_id=%s AND av.availability='available' AND av.integrity='unverified'
          AND j.action='upload' AND j.status='failed' AND j.vault_file_id=av.vault_file_id
          AND j.vault_id=av.vault_id AND j.path=fp.path
          AND NOT EXISTS (SELECT 1 FROM jobs cancelled WHERE cancelled.archive_version_id=av.id AND cancelled.status='cancelled' AND cancelled.action='upload')
        ORDER BY j.id""", (file_id,)).fetchall()
    if not rows:
        return None
    if len({r["id"] for r in rows}) != 1:
        raise UploadBlocked("ambiguous_receipt")
    for row in rows:
        if row.get("upload_snapshot_json"):
            try:
                snapshot = UploadSnapshot.from_json(row["upload_snapshot_json"])
                if row["job_snapshot"] and UploadSnapshot.from_json(row["job_snapshot"]).plaintext_sha256 != snapshot.plaintext_sha256:
                    raise UploadBlocked("proof_mismatch")
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise UploadBlocked("proof_mismatch") from exc
            if row["expected_sha"] != snapshot.plaintext_sha256:
                raise UploadBlocked("proof_mismatch")
        elif not (isinstance(row["expected_sha"],str) and len(row["expected_sha"]) == 64 and all(c in "0123456789abcdef" for c in row["expected_sha"])):
            raise UploadBlocked("proof_unavailable")
    if len({r["expected_sha"] for r in rows}) != 1:
        raise UploadBlocked("proof_mismatch")
    return rows[0]


def describe(connection: Any, *, vault_id: int, path: str) -> dict[str, Any]:
    row = connection.execute("SELECT vf.id FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL WHERE vf.vault_id=%s AND fp.path=%s", (vault_id, path)).fetchone()
    empty = {"pending": False, "can_resume": False, "reason": None, "remedy": None}
    if not row:
        return empty
    file_id = row["id"]
    block = connection.execute("SELECT reason FROM snapshot_reconciliation_blocks WHERE vault_file_id=%s", (file_id,)).fetchone()
    intents = connection.execute("SELECT id,blocked_reason,send_started FROM upload_intents WHERE vault_file_id=%s AND NOT settled ORDER BY created_at LIMIT 2", (file_id,)).fetchall()
    legacy, legacy_reason = None, None
    if not intents:
        try:
            legacy = _legacy_materialized(connection,file_id)
        except UploadBlocked as exc:
            legacy_reason = exc.reason
    if not block and not intents and not legacy and not legacy_reason:
        return empty
    reason = (block["reason"] if block else intents[0]["blocked_reason"] if intents else legacy_reason or "proof_unavailable") or "ambiguous_send"
    busy = connection.execute("SELECT 1 FROM jobs WHERE vault_file_id=%s AND status NOT IN ('failed','cancelled','completed') LIMIT 1", (file_id,)).fetchone()
    cancelled = connection.execute("SELECT 1 FROM jobs j LEFT JOIN upload_intent_jobs b ON b.job_id=j.id WHERE j.vault_file_id=%s AND j.status='cancelled' AND (j.upload_reconcile_only OR b.intent_id IS NOT NULL) LIMIT 1", (file_id,)).fetchone()
    return {"pending": True, "can_resume": not busy and not cancelled and not legacy_reason and len(intents) < 2 and (not intents or intents[0]["send_started"]),
            "reason": reason, "remedy": REMEDIES.get(reason, REMEDIES["proof_unavailable"])}


def _unresumable_reason(connection: Any, vault_id: int, path: str, state: dict[str, Any]) -> str:
    """Name why exact resume cannot start; never imply the path/volume changed."""
    if not state["pending"]:
        return "already_resolved"
    file = connection.execute("SELECT vault_file_id FROM file_paths WHERE vault_id=%s AND path=%s AND valid_to IS NULL", (vault_id, path)).fetchone()
    if file:
        fid = file["vault_file_id"]
        if connection.execute("SELECT 1 FROM jobs WHERE vault_file_id=%s AND status NOT IN ('failed','cancelled','completed') LIMIT 1", (fid,)).fetchone():
            return "active_operation"
        if connection.execute("SELECT 1 FROM jobs j LEFT JOIN upload_intent_jobs b ON b.job_id=j.id WHERE j.vault_file_id=%s AND j.status='cancelled' AND (j.upload_reconcile_only OR b.intent_id IS NOT NULL) LIMIT 1", (fid,)).fetchone():
            return "owner_cancelled"
        if len(connection.execute("SELECT 1 FROM upload_intents WHERE vault_file_id=%s AND NOT settled LIMIT 2", (fid,)).fetchall()) > 1:
            return "ambiguous_send"
    return state["reason"] if state.get("reason") in REMEDIES else "proof_unavailable"


def schedule_resume(connection: Any, *, vault: dict[str, Any], path: str, user_id: int, timestamp: str, remediation_id: str | None = None) -> dict[str, Any]:
    from .upload_remediation import schedule_resume as schedule
    return schedule(connection, vault=vault, path=path, user_id=user_id, timestamp=timestamp, remediation_id=remediation_id)


def _schedule_resume_legacy(connection: Any, *, vault: dict[str, Any], path: str, user_id: int, timestamp: str) -> dict[str, Any]:
    from .cloud_deletion import lock_vault
    from .vault_moves import require_ready
    current = lock_vault(connection, vault["id"])
    try:
        require_ready(current)
    except ValueError as exc:
        raise UploadBlocked("identity_changed") from exc
    state = describe(connection, vault_id=vault["id"], path=path)
    if not state["pending"] or not state["can_resume"]:
        raise UploadBlocked(_unresumable_reason(connection, vault["id"], path, state))
    row = connection.execute("SELECT vf.id,lc.cloud_only_version_id FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id WHERE vf.vault_id=%s AND fp.path=%s AND vf.status='active'", (vault["id"], path)).fetchone()
    if not row or row["cloud_only_version_id"]:
        raise UploadBlocked("identity_changed")
    intent = outstanding(connection, row["id"])
    linked = _legacy_materialized(connection,row["id"]) if not intent else None
    if not intent and not linked:
        linked = connection.execute("SELECT * FROM archive_versions WHERE vault_file_id=%s AND availability='available' AND integrity='verified' ORDER BY version_number DESC LIMIT 1", (row["id"],)).fetchone()
    if not intent and not linked:
        raise UploadBlocked("proof_unavailable")
    job = connection.execute("INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,requested_by,requested_at,updated_at,group_id,group_path,total_bytes,transferred_bytes,origin,upload_reconcile_only,upload_plaintext_sha256,upload_snapshot_json,upload_verification_requested) VALUES (%s,%s,%s,%s,'upload','queued',%s,%s,%s,%s,%s,%s,0,'manual',TRUE,%s,%s,%s) RETURNING id,group_id", (vault["id"], row["id"], intent["archive_version_id"] if intent else linked["id"], path, user_id, timestamp, timestamp, str(uuid.uuid4()), path, UploadSnapshot.from_json(intent["snapshot_json"]).size if intent else linked["size"], UploadSnapshot.from_json(intent["snapshot_json"]).plaintext_sha256 if intent else linked.get("expected_sha") or linked["plaintext_sha256"], intent["snapshot_json"] if intent else linked["upload_snapshot_json"], intent["requested"] if intent else linked["upload_verification_requested"] or "read_back")).fetchone()
    if intent:
        bind_for_resume(connection, job["id"], intent)
    return {"job_id": job["id"], "group_id": job["group_id"]}
