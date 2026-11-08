"""Pre-write upload facts. Receipts are NOT publication or deletion authority.

No provider I/O in replayable DB units. An uncertain send can only reconcile;
zero token matches prove absence only together with the complete S3 reads of
ADR-0025 (never by themselves), and a fresh lease cannot quiesce old I/O.
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
# Conditions that resolve by themselves: the Job waits queued, nothing is blocked
# or notified, and it restarts once the condition clears.
WAIT_REASONS = frozenset({"vault_suspended"})
# Reasons that, raised for a Job that is itself already terminal, describe that
# Job and not the file: they never leave a durable block or a notification.
TERMINAL_JOB_REASONS = frozenset({"owner_cancelled", "already_resolved"})
# Refusals journaled by the SDK seam, plus the outcome proven by complete S3 reads.
NO_WRITE_OPERATIONS = frozenset({'PutObject','CreateMultipartUpload','UploadPart','CompleteMultipartUpload',ABSENT_OPERATION})
REMEDIES = {
    "ambiguous_send": "Keep copies and stop external S3 writers. Retry exact reconciliation after the provider outcome is known; the send restarts by itself only when S3 positively proves nothing was written.",
    "read_permission_denied": "S3 refused to read an object version of this file (HeadObject/GetObject), so its upload cannot be confirmed. Keep all copies, grant the archive IAM identity s3:GetObject and s3:GetObjectVersion on the bucket objects, then use Retry failed uploads.",
    "write_permission_denied": "S3 refused to write this file (PutObject or a multipart request) although the bucket itself is reachable. Nothing was stored and the file stays unprotected. Grant the archive IAM identity s3:PutObject and s3:AbortMultipartUpload on the bucket objects, then use Retry failed uploads.",
    "abort_permission_denied": "S3 refused to abort an incomplete multipart upload left by an interrupted upload of this file, so absence cannot be proven. The incomplete parts are still stored (and billed) until aborted. Keep all copies, grant the archive IAM identity s3:AbortMultipartUpload on the bucket objects, then use Retry failed uploads; otherwise the bucket lifecycle reaps them after 7 days.",
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
    "protection_paused": "Automatic protection is paused for this file. Resume protection first: reconciliation will not start an upload while it is paused.",
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
    versions, markers = _guarded_listing(lambda: _object_version_entries(client, bucket=bucket, object_key=key), bucket)
    return {"versions": sorted(versions), "markers": sorted(markers)}


_CREDENTIAL_CODES = frozenset({
    "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "InvalidToken",
    "TokenRefreshRequired", "InvalidClientTokenId", "UnrecognizedClientException",
})
# Which per-file reason names the S3 action the provider refused. The operation is
# the botocore operation name carried by the ClientError, never the message text.
_OPERATION_REASONS = {
    **dict.fromkeys(("ListObjectVersions", "ListMultipartUploads", "ListObjects", "ListObjectsV2"), "list_permission_denied"),
    **dict.fromkeys(("HeadObject", "GetObject", "GetObjectTagging"), "read_permission_denied"),
    **dict.fromkeys(("PutObject", "CreateMultipartUpload", "UploadPart", "CompleteMultipartUpload"), "write_permission_denied"),
    "AbortMultipartUpload": "abort_permission_denied",
    **dict.fromkeys(("DeleteObject", "DeleteObjects"), "cleanup_unavailable"),
}


def _guarded_listing(call: Any, bucket: str) -> Any:
    """Run a provider LIST and name why it failed.

    Anything that is neither a permission refusal nor a transient provider fault is
    an unconfirmed inventory (``inventory_changed``). See ``guarded`` for the
    permission and transient handling shared with every other S3 action.
    """
    return guarded(call, bucket, default="list_permission_denied", unconfirmed="inventory_changed")


def guarded(call: Any, bucket: str, *, default: str, unconfirmed: str | None = None) -> Any:
    """Run a provider call; turn a per-file permission refusal into a named block.

    A refused action (any HTTP 403 that is not a credential error) is a permission
    gap of this file's work, but only when the installation itself is still ready:
    the readiness probe does not exercise these actions, so letting the error reach
    the worker's readiness branch would reopen the gate and re-claim the Job forever
    (#484, #492). A fresh bucket probe tells the two apart: probe fails -> the
    refusal is installation-wide, so it propagates to the readiness gate (the Job
    stays queued); probe passes -> a per-file ``*_permission_denied`` block that
    never touches the gate. Credential, network, throttling and 5xx failures say
    nothing about the file and propagate to be suspended/retried. ``unconfirmed``
    (listings only) names any other failure. The block is raised outside the
    ``except`` so it carries no chained provider error that would be read as readiness.
    """
    reason: str | None
    try:
        return call()
    except UploadBlocked:
        raise
    except Exception as exc:
        reason = permission_denied_reason(exc, bucket, default=default)
        if reason is None and unconfirmed is not None and not _is_provider_unreachable(exc) \
                and not _refusals(exc):
            reason = unconfirmed
        if reason is None:
            raise
    raise UploadBlocked(reason)


def _bucket_still_ready(bucket: str) -> bool:
    """Fresh, bounded readiness probe of the bucket (the gate's own check)."""
    from .. import storage
    from . import s3_preflight
    try:
        return bool(s3_preflight.check_bucket_readiness(
            bucket, region=storage.settings.aws_region, client=storage.s3_client(preflight=True)).ok)
    except Exception:
        return False


def _refusals(error: BaseException) -> list[Any]:
    """Permission refusals in the error chain (HTTP 403 / AccessDenied).

    ``InvalidObjectState`` is also a 403 but describes a cold-storage version that
    needs an explicit restore, not a missing permission: it is never a refusal.
    """
    from .database_retry import exception_chain
    from .cloud_preflight_gate import COLD_STORAGE_CODES
    from botocore.exceptions import ClientError
    return [item for item in exception_chain(error) if isinstance(item, ClientError)
            and item.response.get("Error", {}).get("Code") not in COLD_STORAGE_CODES
            and (item.response.get("Error", {}).get("Code") in {"AccessDenied", "AccessDeniedException"}
                 or item.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403)]


def permission_denied_reason(error: BaseException, bucket: str, *, default: str | None = None) -> str | None:
    """Per-file reason for a refused S3 action, or None when it is not one.

    None means: not a refusal, a credential error, or a fresh bucket probe that
    fails (the refusal is installation-wide) -- the caller then lets the error reach
    the readiness gate unchanged. The reason follows the refused operation; an
    operation not in the table uses ``default`` (None: the gate handles it).
    """
    refusals = _refusals(error)
    if not refusals:
        return None
    if any(item.response.get("Error", {}).get("Code") in _CREDENTIAL_CODES for item in refusals):
        return None
    reason = next((_OPERATION_REASONS[item.operation_name] for item in refusals
                   if item.operation_name in _OPERATION_REASONS), default)
    if reason is None:
        return None
    return reason if _bucket_still_ready(bucket) else None


def _is_provider_unreachable(error: BaseException) -> bool:
    """Credential, network and provider-side failures: retried or suspended, never a changed inventory."""
    from .cloud_preflight_gate import is_readiness_error
    from .upload_retry import is_provider_transient
    return is_readiness_error(error) or is_provider_transient(error)


def target(connection: Any, job: dict[str, Any], *, key: str, provider: str) -> dict[str, Any]:
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s FOR NO KEY UPDATE", (job["vault_id"],)).fetchone()
    from .upload_remediation import require_job
    require_job(connection, job)
    row = connection.execute("SELECT vf.status,fp.path,fp.valid_from FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL WHERE vf.id=%s AND vf.vault_id=%s", (job["vault_file_id"], job["vault_id"])).fetchone()
    current = connection.execute("SELECT status,vault_file_id,path,claim_token FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    if current and current["status"] == "cancelled":
        raise UploadBlocked("owner_cancelled")
    if current and current["status"] in {"failed", "completed"}:
        raise UploadBlocked("already_resolved")
    if (not vault or not row or row["status"] != "active" or not current
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
    # An intent whose send never started (prepared only, or closed by a proven
    # absence) has no provider obligation: it never blocks or binds another Job.
    rows = connection.execute("SELECT * FROM upload_intents WHERE vault_file_id=%s AND NOT settled AND send_started ORDER BY created_at,id LIMIT 2", (file_id,)).fetchall()
    if len(rows) > 1:
        raise UploadBlocked("ambiguous_send")
    return rows[0] if rows else None


def _settle_unstarted(connection: Any, file_id: str, *, keep_intent_id: str | None = None) -> None:
    """Close intents that never sent: nothing was written, nothing to reconcile.

    ``send_started`` is committed before any request and is cleared only by a
    journaled definitive no-write outcome, so an unsettled, unlinked intent with
    it unset cannot have produced an object. Receipts are never ignored.
    """
    rows = connection.execute("SELECT id FROM upload_intents WHERE vault_file_id=%s AND NOT settled AND NOT send_started AND archive_version_id IS NULL AND id IS DISTINCT FROM %s", (file_id, keep_intent_id)).fetchall()
    for row in rows:
        if receipt(connection, row["id"]) is not None:
            raise UploadBlocked("ambiguous_receipt")
        connection.execute("UPDATE upload_intents SET settled=TRUE,blocked_reason=NULL WHERE id=%s AND NOT send_started", (row["id"],))
        from datetime import datetime, timezone
        close_multiparts(connection, row["id"], datetime.now(timezone.utc).isoformat())


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
        current = (canonical(pinned), snapshot.as_json(), requested, canonical(before))
        if (existing["send_started"] or existing["settled"] or existing["archive_version_id"]
                or current == (existing["target_json"], existing["snapshot_json"], existing["requested"], existing["inventory_json"])):
            return existing
        # Prepared (or re-armed after a proven absence) but never sent, and the
        # content or cloud inventory moved on: re-pin it in place with a new
        # token rather than failing every later attempt with a false identity change.
        if receipt(connection, existing["id"]) is not None:
            raise UploadBlocked("ambiguous_receipt")
        refreshed = connection.execute("UPDATE upload_intents SET token=%s,target_json=%s,snapshot_json=%s,requested=%s,inventory_json=%s,created_at=%s,blocked_reason=NULL WHERE id=%s AND NOT send_started AND NOT settled AND archive_version_id IS NULL RETURNING *", (str(uuid.uuid4()), *current, timestamp, existing["id"])).fetchone()
        if not refreshed:
            raise UploadBlocked("ambiguous_send")
        _settle_unstarted(connection, job["vault_file_id"], keep_intent_id=refreshed["id"])
        return refreshed
    _settle_unstarted(connection, job["vault_file_id"])
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


@retry_database_unit
def journal_multipart(db: Any, intent_id: str, upload_id: str, timestamp: str) -> None:
    """Record an UploadId this intent created, BEFORE its first part is sent (#491).

    This row is the only authority to abort a multipart upload later: the provider
    listing alone cannot say whose it is. Own connection, committed on return.
    """
    with owned_connection(db()) as connection:
        connection.execute("INSERT INTO upload_multiparts(intent_id,upload_id,created_at) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (intent_id, upload_id, timestamp))


@retry_database_unit
def close_multipart(db: Any, intent_id: str, upload_id: str, timestamp: str) -> None:
    """The provider no longer holds this UploadId (completed or aborted)."""
    with owned_connection(db()) as connection:
        connection.execute("UPDATE upload_multiparts SET state='closed',closed_at=%s WHERE intent_id=%s AND upload_id=%s AND state='open'", (timestamp, intent_id, upload_id))


def close_multiparts(connection: Any, intent_id: str, timestamp: str) -> None:
    """DB-only: the intent no longer owes the provider an abort.

    Called when complete reads proved absence, when reconciliation published the
    version it found (a Complete whose response was lost), and when the intent is
    settled. A closed row means "no live obligation", not "the provider is clean":
    an orphan the abort never reached is reaped by the bucket lifecycle.
    """
    connection.execute("UPDATE upload_multiparts SET state='closed',closed_at=%s WHERE intent_id=%s AND state='open'", (timestamp, intent_id))


def open_multiparts(connection: Any, intent_id: str) -> list[str]:
    return [r["upload_id"] for r in connection.execute("SELECT upload_id FROM upload_multiparts WHERE intent_id=%s AND state='open' ORDER BY created_at,upload_id", (intent_id,)).fetchall()]


def record_no_write(connection: Any, job: dict[str, Any], intent: dict[str, Any],
                    operation: str, timestamp: str) -> None:
    """Claim-fenced definitive refusal; retain the immutable outcome as a fact.

    Called after the transfer manager joined all I/O and reported an auth
    refusal without a final receipt, or after complete S3 reads proved absence
    (ADR-0025). Zero listing matches alone never reach this seam.
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


def proven_absent(connection: Any, intent_id: str | None) -> bool:
    """True when this intent was closed by a journaled positive proof of absence."""
    if not intent_id:
        return False
    return connection.execute("SELECT 1 FROM upload_receipts WHERE intent_id=%s AND response_json=%s LIMIT 1",
                              (intent_id, canonical({'NoWriteOperation': ABSENT_OPERATION}))).fetchone() is not None


def unsent(connection: Any, intent_id: str | None) -> bool:
    """The intent carries no provider obligation: its send never (or no longer) started."""
    if not intent_id:
        return False
    return connection.execute("SELECT 1 FROM upload_intents WHERE id=%s AND NOT send_started AND archive_version_id IS NULL",
                              (intent_id,)).fetchone() is not None


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
              check_active: Any, *, owned_multiparts: Any = (),
              on_multipart_aborted: Any = None) -> dict[str, Any]:
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
            _prove_absent(client, pinned, before, check_active, owned_multiparts, on_multipart_aborted)
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
        page = _guarded_listing(lambda: lister(**kwargs), bucket)
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


def _abort_owned_multiparts(client: Any, pinned: dict[str, Any], found: list[str],
                            owned: Any, check_active: Any, on_aborted: Any) -> None:
    """Abort exactly the UploadIds this intent journaled; never any other.

    An incomplete multipart for the key whose UploadId this intent did not journal
    (another writer, an older release, an unjournaled crash window) is untraceable:
    it is left alone and keeps the proof of absence closed (#491, ADR-0029).
    """
    from botocore.exceptions import ClientError
    for upload_id in found:
        if upload_id not in owned:
            continue
        check_active()
        try:
            guarded(lambda: client.abort_multipart_upload(Bucket=pinned["bucket"], Key=pinned["key"], UploadId=upload_id),
                    pinned["bucket"], default="abort_permission_denied")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchUpload":
                raise
        if on_aborted is not None:
            on_aborted(upload_id)


def _prove_absent(client: Any, pinned: dict[str, Any], before: dict[str, Any],
                  check_active: Any, owned_multiparts: Any = (),
                  on_multipart_aborted: Any = None) -> None:
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
    found = _incomplete_uploads(client, pinned["bucket"], pinned["key"])
    owned = frozenset(owned_multiparts)
    if found and owned.intersection(found):
        _abort_owned_multiparts(client, pinned, found, owned, check_active, on_multipart_aborted)
        check_active()
        # Whatever the exact abort did not remove is not provably this intent's.
        found = _incomplete_uploads(client, pinned["bucket"], pinned["key"])
    if found:
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


def _only_void_episodes(connection: Any, file_id: str) -> bool:
    rows = connection.execute("SELECT intent_id FROM upload_remediations WHERE vault_file_id=%s AND state NOT IN ('resolved','cancelled') LIMIT 8", (file_id,)).fetchall()
    return bool(rows) and all(proven_absent(connection, r["intent_id"]) and unsent(connection, r["intent_id"]) for r in rows)


def describe(connection: Any, *, vault_id: int, path: str) -> dict[str, Any]:
    row = connection.execute("SELECT vf.id FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL WHERE vf.vault_id=%s AND fp.path=%s", (vault_id, path)).fetchone()
    empty = {"pending": False, "can_resume": False, "reason": None, "remedy": None}
    if not row:
        return empty
    file_id = row["id"]
    block = connection.execute("SELECT reason FROM snapshot_reconciliation_blocks WHERE vault_file_id=%s", (file_id,)).fetchone()
    intents = connection.execute("SELECT id,blocked_reason,send_started FROM upload_intents WHERE vault_file_id=%s AND NOT settled AND send_started ORDER BY created_at LIMIT 2", (file_id,)).fetchall()
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
    # An episode whose re-armed send never started has nothing left to reconcile:
    # offering Resume would only end in a 409 (the ordinary upload retries it).
    void_only = not intents and _only_void_episodes(connection, file_id)
    return {"pending": True, "can_resume": not busy and not cancelled and not legacy_reason and len(intents) < 2 and not void_only,
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
    except ValueError:
        raise UploadBlocked("vault_suspended") from None
    state = describe(connection, vault_id=vault["id"], path=path)
    if not state["pending"] or not state["can_resume"]:
        raise UploadBlocked(_unresumable_reason(connection, vault["id"], path, state))
    row = connection.execute("SELECT vf.id,lc.cloud_only_version_id FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id WHERE vf.vault_id=%s AND fp.path=%s AND vf.status='active'", (vault["id"], path)).fetchone()
    if not row:
        raise UploadBlocked("identity_changed")
    if row["cloud_only_version_id"]:
        raise UploadBlocked("cloud_only_conflict")
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
