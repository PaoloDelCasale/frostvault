"""Pre-write upload facts. Receipts are NOT publication or deletion authority.

The Upload Snapshot and its target are stored once; every send is an Upload
Attempt of it with its own token; receipts are journaled per attempt,
independently of any Job (#588, ADR-0036). No provider I/O in replayable DB
units. An uncertain send can only reconcile; zero token matches prove absence
only together with the complete S3 reads of ADR-0025 (never by themselves), and
a fresh lease cannot quiesce old I/O. This module has no sweep/history timer.
"""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .database_retry import owned_connection, retry_database_unit
from .upload_snapshot import MODES, UploadSnapshot
from .s3_copy import _object_version_entries
from .vault_paths import expected_cloud_key

TOKEN_KEY = "frostvault-upload"
ABSENT_OPERATION = 'ProvenAbsent'
# Jobs whose provider writes run under this target fence: uploads, and the
# server-side copies of rename and storage-class/Vault move (#595).
TARGET_ACTIONS = frozenset({"upload", "rename", "storage-class"})
# Conditions that resolve by themselves: the Job waits queued, nothing is blocked
# or notified, and it restarts once the condition clears.
WAIT_REASONS = frozenset({"vault_suspended"})
# Reasons that, raised for a Job that is itself already terminal, describe that
# Job and not the file: they never leave a durable block or a notification.
TERMINAL_JOB_REASONS = frozenset({"owner_cancelled", "already_resolved"})
# Refusals journaled by the SDK seam, plus the outcome proven by complete S3 reads.
NO_WRITE_OPERATIONS = frozenset({'PutObject','CreateMultipartUpload','UploadPart','CompleteMultipartUpload',ABSENT_OPERATION})
REMEDIES = {
    "upload_journal_unavailable": "The upload receipt journal is unavailable. Restore database write availability; the Job resumes automatically without spending transfer retries or repeating an uncertain send.",
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


# Whether the catalog ever wrote, or may have written, the object key of the
# ``files f`` row's path (one %s: that key). Every holder of the path counts:
# the file itself and the files Path History shows there before it. Any
# Archive Version at the key, any upload attempt of the path and any copy to
# the key say yes; only a key the catalog never sent to says no (ADR-0038).
KEY_WRITTEN_SQL = """(EXISTS (SELECT 1 FROM archive_versions kv WHERE kv.object_key = %s)
    OR EXISTS (SELECT 1 FROM upload_attempts ka JOIN upload_snapshots ks ON ks.id = ka.snapshot_id
               WHERE ka.file_id IN (SELECT f.id UNION ALL
                                    SELECT h.file_id FROM file_path_history h
                                    WHERE h.vault_id = f.vault_id AND h.path = f.path)
                 AND ks.vault_id = f.vault_id AND ks.path = f.path)
    OR EXISTS (SELECT 1 FROM copy_attempts kc
               WHERE kc.file_id IN (SELECT f.id UNION ALL
                                    SELECT h.file_id FROM file_path_history h
                                    WHERE h.vault_id = f.vault_id AND h.path = f.path)
                 AND kc.destination_key = %s))"""


def presumed_empty_inventory() -> dict[str, list[str]]:
    """The pin of a key the catalog never wrote (ADR-0038): nothing, unobserved.

    It is never replacement authority: cleanup deletes only catalogued
    candidates after its own fresh listing, and the post-publication listing
    blocks completion on anything else at the key. A proof of absence compares
    against it like any pin, so an unknown version keeps the send blocked.
    """
    return {"versions": [], "markers": []}


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


def target(connection: Any, job: dict[str, Any], *, key: str, provider: str,
           shared: bool = False) -> dict[str, Any]:
    """Read and fence the live upload target of ``job``; return it as a pin.

    The pin is what an Upload Snapshot freezes once (``upload_snapshots``):
    Vault, file, cloud target, path epoch, Source Volume root and provider
    endpoint. ``key`` is the object key the caller is about to use; it must be
    the Vault namespace key of the current path (``vaults/<uuid>/<path>``).

    Exclusive by default. Opt in to ``shared`` only after auditing the COMPLETE
    DB unit: no Vault touch, aggregate/event/rebuild writer or terminal Job
    transition. SHARE (not KEY SHARE) fences lifecycle/identity and admission's
    NO KEY UPDATE writers. Nested target checks must retain the mode: two SHARE
    holders upgrading to NO KEY UPDATE would deadlock (ADR-0031, #551).
    """
    pinned, _ = _target(connection, job, key=key, provider=provider, shared=shared)
    return pinned


def _target(connection: Any, job: dict[str, Any], *, key: str, provider: str,
            shared: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """``target`` and the locked Vault row it was read from."""
    lock = "SHARE" if shared else "NO KEY UPDATE"
    vault = connection.execute(f"SELECT * FROM vaults WHERE id=%s FOR {lock}", (job["vault_id"],)).fetchone()
    row = connection.execute(
        """SELECT f.path, f.path_since, f.held_version_id, t.bucket, t.region,
                  j.status AS job_status, j.file_id AS job_file_id, j.vault_id AS job_vault_id,
                  j.action AS job_action,
                  EXISTS (SELECT 1 FROM jobs other WHERE other.file_id=f.id AND other.id<>%(job)s
                          AND other.status NOT IN ('failed','cancelled','completed')) AS busy,
                  EXISTS (SELECT 1 FROM copy_attempts c WHERE c.file_id=f.id
                          AND c.settled_at IS NULL) AS copying
           FROM files f
           JOIN vaults v ON v.id=f.vault_id
           JOIN vault_targets t ON t.id=v.target_id
           LEFT JOIN jobs j ON j.id=%(job)s
           WHERE f.id=%(file)s AND f.vault_id=%(vault)s""",
        {"job": job["id"], "file": job["file_id"], "vault": job["vault_id"]}).fetchone()
    if row and row["job_status"] == "cancelled":
        raise UploadBlocked("owner_cancelled")
    if row and row["job_status"] in {"failed", "completed"}:
        raise UploadBlocked("already_resolved")
    if (not vault or not row or row["job_status"] is None or row["job_action"] not in TARGET_ACTIONS
            or str(row["job_file_id"]) != str(job["file_id"]) or row["job_vault_id"] != job["vault_id"]
            or key != expected_cloud_key(row["path"], f"vaults/{vault['uuid']}")):
        raise UploadBlocked("identity_changed")
    if vault["lifecycle"] != "active":
        raise UploadBlocked("vault_suspended")
    if row["busy"] or (row["copying"] and row["job_action"] == "upload"):
        # An unsettled server-side copy (#595) is reconciled by its own
        # purpose before any upload may send to the file.
        raise UploadBlocked("active_operation")
    if row["held_version_id"]:
        raise UploadBlocked("cloud_only_conflict")
    return {"vault_id": vault["id"], "file_id": str(job["file_id"]),
            "bucket": row["bucket"], "region": row["region"], "key": key,
            "provider": provider, "path": row["path"], "path_since": row["path_since"],
            "source_root": vault["source_root"], "root_identity": vault["root_identity"]}, dict(vault)


class _ValidatedTarget:
    """Transaction-local capability, never a caller-controlled bypass flag.

    Only _validated_target can install one. It captures the actual locked row
    validation, one connection and one immutable Job identity. A nested check
    still compares the complete pin, including path epoch/root identity.
    No capability is stored on pooled connections or survives commit/rollback.
    """

    def __init__(self, connection: Any, job: dict[str, Any], pinned: dict[str, Any], shared: bool,
                 vault: dict[str, Any] | None = None):
        self.connection = connection
        self.job = dict(job)
        self.pinned = dict(pinned)
        self.shared = shared
        self.vault = dict(vault or {})

    def matches(self, connection: Any, job: dict[str, Any]) -> bool:
        return connection is self.connection and job == self.job

    def require(self, pinned: dict[str, Any], shared: bool) -> None:
        if self.shared and not shared:
            raise RuntimeError("An audited SHARE upload unit cannot upgrade its Vault lock")
        # provider/key are the caller's namespace labels, checked by target.
        # Identity/lifecycle/ownership checks remain the actual locked facts.
        current = {**self.pinned, "key": pinned["key"], "provider": pinned["provider"]}
        if current != pinned:
            raise UploadBlocked("identity_changed")


_validated: ContextVar[_ValidatedTarget | None] = ContextVar("upload_validated_target", default=None)


@contextmanager
def _validated_target(connection: Any, job: dict[str, Any], *, key: str, provider: str,
                      shared: bool = False):
    pinned, vault = _target(connection, job, key=key, provider=provider, shared=shared)
    token = _validated.set(_ValidatedTarget(connection, job, pinned, shared, vault))
    try:
        yield pinned
    finally:
        _validated.reset(token)


def validated_pin(connection: Any, job: dict[str, Any]) -> dict[str, Any]:
    """The target pin of the claim-fenced unit running on ``connection`` (no re-read)."""
    validated = _validated.get()
    if validated is None or not validated.matches(connection, job):
        raise RuntimeError("validated_pin needs a claim-fenced upload unit")
    return dict(validated.pinned)


def validated_vault(connection: Any, job: dict[str, Any]) -> dict[str, Any]:
    """The Vault row the claim-fenced unit on ``connection`` locked (no re-read).

    Fresh for this unit only: the Source Volume/root check before a provider
    effect compares the filesystem against this row, never against a cached one.
    """
    validated = _validated.get()
    if validated is None or not validated.matches(connection, job):
        raise RuntimeError("validated_vault needs a claim-fenced upload unit")
    return dict(validated.vault)


def require_target(connection: Any, job: dict[str, Any], pinned: dict[str, Any], *,
                   shared: bool = False) -> None:
    validated = _validated.get()
    if validated is not None and validated.matches(connection, job):
        validated.require(pinned, shared)
    elif _target(connection, job, key=pinned["key"], provider=pinned["provider"], shared=shared)[0] != pinned:
        raise UploadBlocked("identity_changed")


# --- Upload proofs (#588): the snapshot and its target once, attempts and
# receipts independent of the Job. Every function below is DB-only; the caller
# owns the claim-fenced unit, except the journal writers, which commit on
# their own connection immediately.

@dataclass(frozen=True)
class AdmittedSnapshot:
    """One admitted Upload Snapshot with the target it was frozen for."""

    id: str
    vault_id: int
    file_id: str
    target: dict[str, Any]
    snapshot: UploadSnapshot
    verification: str
    local_generation: int
    closed: bool


@dataclass(frozen=True)
class UploadAttempt:
    """One send attempt of an admitted snapshot, with its own token."""

    id: str
    token: str
    job_id: int | None
    admitted: AdmittedSnapshot
    inventory: dict[str, list[str]]
    send_started: bool
    outcome: str | None
    no_write_operation: str | None
    landed_version_id: str | None

    @property
    def target(self) -> dict[str, Any]:
        return self.admitted.target

    @property
    def snapshot(self) -> UploadSnapshot:
        return self.admitted.snapshot

    @property
    def settled(self) -> bool:
        return self.outcome is not None

    @property
    def may_send(self) -> bool:
        """Only a never-started, unsettled attempt of an open snapshot may send.

        A committed send-start is no authority by itself: once it is set, the
        attempt can only be reconciled, never sent again.
        """
        return not self.send_started and not self.settled and not self.admitted.closed


_SNAPSHOT_COLUMNS = """s.id AS snapshot_id, s.vault_id, s.file_id, s.path, s.path_since, s.source_root,
    s.root_identity, s.provider, s.local_generation, s.size, s.mtime_ns, s.plaintext_sha256,
    s.part_size, s.part_sha256, s.verification, s.closed_at, v.uuid AS vault_uuid,
    t.bucket, t.region"""
_SNAPSHOT_FROM = """upload_snapshots s JOIN vaults v ON v.id=s.vault_id
    JOIN vault_targets t ON t.id=v.target_id"""
_ATTEMPT_COLUMNS = _SNAPSHOT_COLUMNS + """, a.id AS attempt_id, a.token, a.job_id,
    a.inventory_versions, a.inventory_markers, a.send_started_at, a.outcome,
    a.no_write_operation, a.landed_version_id"""
_ATTEMPT_FROM = "upload_attempts a JOIN " + _SNAPSHOT_FROM + " ON s.id=a.snapshot_id"


def _admitted(row: dict[str, Any]) -> AdmittedSnapshot:
    snapshot = UploadSnapshot.from_record(
        size=row["size"], plaintext_sha256=row["plaintext_sha256"], part_size=row["part_size"],
        part_sha256=row["part_sha256"], mtime_ns=row["mtime_ns"])
    pinned = {"vault_id": row["vault_id"], "file_id": str(row["file_id"]),
              "bucket": row["bucket"], "region": row["region"],
              "key": expected_cloud_key(row["path"], f"vaults/{row['vault_uuid']}"),
              "provider": row["provider"], "path": row["path"], "path_since": row["path_since"],
              "source_root": row["source_root"], "root_identity": row["root_identity"]}
    return AdmittedSnapshot(str(row["snapshot_id"]), row["vault_id"], str(row["file_id"]), pinned,
                            snapshot, row["verification"], row["local_generation"],
                            row["closed_at"] is not None)


def _attempt(row: dict[str, Any]) -> UploadAttempt:
    return UploadAttempt(
        str(row["attempt_id"]), str(row["token"]), row["job_id"], _admitted(row),
        {"versions": list(row["inventory_versions"]), "markers": list(row["inventory_markers"])},
        row["send_started_at"] is not None, row["outcome"], row["no_write_operation"],
        row["landed_version_id"])


def _bound(connection: Any, job_id: int) -> tuple[AdmittedSnapshot, bool] | None:
    row = connection.execute(
        f"SELECT {_SNAPSHOT_COLUMNS}, b.reconcile_only FROM job_uploads b "
        f"JOIN {_SNAPSHOT_FROM} ON s.id=b.snapshot_id WHERE b.job_id=%s", (job_id,)).fetchone()
    return (_admitted(row), row["reconcile_only"]) if row else None


def bound_snapshot(connection: Any, job_id: int) -> AdmittedSnapshot | None:
    """The snapshot a Job uploads or reconciles: its only source of expected proof."""
    bound = _bound(connection, job_id)
    return bound[0] if bound else None


def load_for_job(connection: Any, job_id: int) -> UploadAttempt | None:
    """The attempt of the Job's snapshot that still matters: unsettled or landed.

    There is at most one: one unsettled attempt per file, and landing closes
    the snapshot. A no-write or unsent attempt is history; the next send of the
    snapshot needs a new attempt and token.
    """
    row = connection.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM job_uploads b JOIN {_ATTEMPT_FROM} "
        "ON a.snapshot_id=b.snapshot_id WHERE b.job_id=%s "
        "AND (a.settled_at IS NULL OR a.outcome='landed')", (job_id,)).fetchone()
    return _attempt(row) if row else None


def load_attempt(connection: Any, attempt_id: str) -> UploadAttempt | None:
    row = connection.execute(f"SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM} WHERE a.id=%s",
                             (attempt_id,)).fetchone()
    return _attempt(row) if row else None


_CONTRADICTED = """EXISTS (
    SELECT 1 FROM upload_attempts settled JOIN upload_receipts r ON r.attempt_id=settled.id
    WHERE settled.file_id=%(file)s AND settled.settled_at IS NOT NULL
      AND (settled.outcome<>'landed'
           OR (r.response->>'VersionId') IS DISTINCT FROM settled.landed_version_id))"""


def outstanding(connection: Any, file_id: str) -> UploadAttempt | None:
    """The file's started, unsettled attempt: its provider outcome is unknown.

    A receipt that contradicts a settled attempt of the file (a late receipt of
    a no-write or unsent attempt, or another VersionId than the landed one)
    blocks too, even long after the settlement.
    """
    row = connection.execute(
        f"""SELECT {_CONTRADICTED} AS contradicted, att.*
            FROM (SELECT 1) one LEFT JOIN (
                SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM}
                WHERE a.file_id=%(file)s AND a.settled_at IS NULL
                  AND a.send_started_at IS NOT NULL
            ) att ON true""", {"file": file_id}).fetchone()
    if row["contradicted"]:
        raise UploadBlocked("ambiguous_receipt")
    return _attempt(row) if row["attempt_id"] is not None else None


def _settle_unsent(connection: Any, attempt_id: Any, timestamp: Any) -> bool:
    """Close a never-started attempt: nothing was sent, nothing to reconcile.

    A receipt of an attempt that never started contradicts it: it is never
    ignored, and the attempt stays open (ambiguous_receipt).
    """
    closed = connection.execute(
        """UPDATE upload_attempts a SET settled_at=%s, outcome='unsent'
           WHERE a.id=%s AND a.settled_at IS NULL AND a.send_started_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM upload_receipts r WHERE r.attempt_id=a.id)
           RETURNING a.id""", (timestamp, attempt_id)).fetchone()
    if not closed:
        raise UploadBlocked("ambiguous_receipt")
    close_multiparts(connection, attempt_id, timestamp)
    return True


def _close_abandoned(connection: Any, file_id: str, timestamp: Any) -> None:
    """Close the file's open snapshot left by an attempt that never sent.

    Its Jobs forget it (their next attempt takes a new snapshot). A started,
    unsettled send, a receipt or a late contradiction keeps it: the caller
    must reconcile before hashing a different content.
    """
    row = connection.execute(
        f"""SELECT {_CONTRADICTED} AS contradicted, att.*
            FROM (SELECT 1) one LEFT JOIN (
                SELECT s.id AS snapshot_id,
                       (SELECT a.id FROM upload_attempts a
                        WHERE a.snapshot_id=s.id AND a.settled_at IS NULL) AS open_attempt,
                       EXISTS (SELECT 1 FROM upload_attempts a WHERE a.snapshot_id=s.id
                               AND a.settled_at IS NULL AND a.send_started_at IS NOT NULL) AS sending,
                       EXISTS (SELECT 1 FROM upload_attempts a JOIN upload_receipts r
                               ON r.attempt_id=a.id WHERE a.snapshot_id=s.id) AS received
                FROM upload_snapshots s WHERE s.file_id=%(file)s AND s.closed_at IS NULL
            ) att ON true""", {"file": file_id}).fetchone()
    if row["contradicted"]:
        raise UploadBlocked("ambiguous_receipt")
    if row["snapshot_id"] is None:
        return
    if row["sending"]:
        raise UploadBlocked("ambiguous_send")
    if row["received"]:
        raise UploadBlocked("ambiguous_receipt")
    if row["open_attempt"] is not None:
        _settle_unsent(connection, row["open_attempt"], timestamp)
    _close_snapshot(connection, row["snapshot_id"], timestamp, forget=True)


def _close_snapshot(connection: Any, snapshot_id: Any, timestamp: Any, *, forget: bool) -> None:
    connection.execute("UPDATE upload_snapshots SET closed_at=%s WHERE id=%s AND closed_at IS NULL",
                       (timestamp, snapshot_id))
    if forget:
        connection.execute("UPDATE job_uploads SET snapshot_id=NULL "
                           "WHERE snapshot_id=%s AND NOT reconcile_only", (snapshot_id,))


def _admit(connection: Any, job: dict[str, Any], pinned: dict[str, Any], snapshot: UploadSnapshot,
           requested: str, local_generation: int, timestamp: Any) -> AdmittedSnapshot:
    if requested not in MODES:
        raise ValueError("Unknown Upload Verification Mode")
    _close_abandoned(connection, job["file_id"], timestamp)
    record = snapshot.record()
    row = connection.execute(
        """INSERT INTO upload_snapshots(vault_id, file_id, path, path_since, source_root,
               root_identity, provider, local_generation, size, mtime_ns, plaintext_sha256,
               part_size, part_sha256, verification, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (pinned["vault_id"], pinned["file_id"], pinned["path"], pinned["path_since"],
         pinned["source_root"], pinned["root_identity"], pinned["provider"], local_generation,
         record["size"], record["mtime_ns"], record["plaintext_sha256"], record["part_size"],
         record["part_sha256"], requested, timestamp)).fetchone()
    _link(connection, job["id"], row["id"], requested)
    return AdmittedSnapshot(str(row["id"]), pinned["vault_id"], pinned["file_id"], dict(pinned),
                            snapshot, requested, local_generation, False)


def _open_attempt(connection: Any, job: dict[str, Any], admitted: AdmittedSnapshot,
                  before: dict[str, Any], timestamp: Any) -> UploadAttempt:
    versions, markers = sorted(before["versions"]), sorted(before["markers"])
    current = connection.execute(
        f"""SELECT {_CONTRADICTED} AS contradicted, att.*
            FROM (SELECT 1) one LEFT JOIN (
                SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM}
                WHERE a.file_id=%(file)s AND a.settled_at IS NULL
            ) att ON true""", {"file": admitted.file_id}).fetchone()
    if current["contradicted"]:
        raise UploadBlocked("ambiguous_receipt")
    if current["attempt_id"] is not None:
        existing = _attempt(current)
        if existing.send_started:
            raise UploadBlocked("ambiguous_send")
        if (existing.admitted.id == admitted.id and existing.job_id == job["id"]
                and existing.inventory == {"versions": versions, "markers": markers}):
            return existing
        # A fresh namespace observation is a new attempt with a new token; the
        # unsent one is closed, never re-pinned in place.
        _settle_unsent(connection, existing.id, timestamp)
    row = connection.execute(
        """INSERT INTO upload_attempts(snapshot_id, file_id, job_id, inventory_versions,
               inventory_markers, created_at)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, token""",
        (admitted.id, admitted.file_id, job["id"], versions, markers, timestamp)).fetchone()
    return UploadAttempt(str(row["id"]), str(row["token"]), job["id"], admitted,
                         {"versions": versions, "markers": markers}, False, None, None, None)


def prepare(connection: Any, job: dict[str, Any], *, pinned: dict[str, Any], snapshot: UploadSnapshot,
            requested: str, local_generation: int, before: dict[str, Any], timestamp: Any,
            shared: bool = False) -> UploadAttempt:
    """Admit the Job's snapshot once and open one send attempt on it.

    The snapshot and its target are written once and bound to the Job; a DB
    retry, or a new send after a proven no-write, reuses them. Every attempt
    has its own token and pinned inventory. A different content for the same
    Job needs ``close_unsent_for_job`` first (ADR-0030). Nothing here sends:
    only ``mark_send_started``, committed, admits one send.
    """
    require_target(connection, job, pinned, shared=shared)
    bound = _bound(connection, job["id"])
    if bound is None:
        admitted = _admit(connection, job, pinned, snapshot, requested, local_generation, timestamp)
    else:
        admitted, _ = bound
        if admitted.closed:
            raise UploadBlocked("already_resolved")
        if (admitted.snapshot, admitted.verification) != (snapshot, requested):
            raise RuntimeError("Local file changed since fingerprinting")
        if admitted.target != pinned:
            raise UploadBlocked("identity_changed")
    return _open_attempt(connection, job, admitted, before, timestamp)


def mark_send_started(connection: Any, job: dict[str, Any], attempt: UploadAttempt, *,
                      shared: bool = False) -> UploadAttempt:
    """Commit, before any provider request, that this attempt is sending.

    Succeeds once per attempt: a second call, a settled attempt, a closed
    snapshot or any receipt refuses. The caller sends only after committing.
    """
    require_target(connection, job, attempt.target, shared=shared)
    changed = connection.execute(
        """UPDATE upload_attempts a SET send_started_at=now()
           WHERE a.id=%s AND a.send_started_at IS NULL AND a.settled_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM upload_receipts r WHERE r.attempt_id=a.id)
             AND EXISTS (SELECT 1 FROM upload_snapshots s WHERE s.id=a.snapshot_id
                         AND s.closed_at IS NULL)
           RETURNING a.id""", (attempt.id,)).fetchone()
    if not changed:
        if receipt(connection, attempt.id) is not None:
            raise UploadBlocked("ambiguous_receipt")
        raise UploadBlocked("ambiguous_send")
    return replace(attempt, send_started=True)


def _receipt_fact(response: dict[str, Any]) -> dict[str, Any]:
    # Allowlist facts: never HTTP headers, cookies, credentials or provider errors.
    return {k: response[k] for k in ("VersionId", "ETag", "ChecksumSHA256", "ChecksumType", "ContentLength", "StorageClass") if k in response}


def receipt_identity(response: dict[str, Any]) -> tuple[str, str]:
    """One allowlisted encoding for the journal, positive dedup and failure latch."""
    text = canonical(_receipt_fact(response))
    return text, hashlib.sha256(text.encode()).hexdigest()


class ReceiptJournal:
    """Immediate append-only callback, independent of claim/cancel/publication.

    The transport callback, synchronous return and late-ack fallback share this
    recorder. Only successful return from append_receipt confirms the commit.
    On an unknown commit outcome the retained ack retries DB, never the PUT.
    Holds one committed fact hash, not a receiver batch or pending receipt queue.
    """

    def __init__(self, db: Any, attempt_id: str, clock: Any):
        self._db, self._attempt_id, self._clock = db, attempt_id, clock
        self._committed: str | None = None
        self._lock = threading.Lock()

    def __call__(self, response: dict[str, Any]) -> None:
        fact = _receipt_fact(response)
        _, fingerprint = receipt_identity(fact)
        with self._lock:
            if fingerprint == self._committed:
                return
            append_receipt(self._db, self._attempt_id, fact, self._clock())
            self._committed = fingerprint


@retry_database_unit
def append_receipt(db: Any, attempt_id: str, response: dict[str, Any], timestamp: Any) -> None:
    """Journal a raw receipt on its own connection, committed on return.

    Accepted whatever the attempt's state (cancelled Job, rolled back worker
    unit, settled attempt, collected Job): a late receipt is a fact that may
    contradict the settlement, never one to drop. Idempotent by content.
    """
    text, fingerprint = receipt_identity(response)
    with owned_connection(db()) as connection:
        connection.execute(
            "INSERT INTO upload_receipts(attempt_id, receipt_sha256, response, received_at) "
            "VALUES (%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING",
            (attempt_id, bytes.fromhex(fingerprint), text, timestamp))


def probe_receipt_journal(connection: Any) -> None:
    """Exercise the journal's INSERT permission/read-write path, without a fact.

    The caller must observe COMMIT before reopening send admission. A SELECT is
    insufficient (read-only/failing writes); no fake response or no-write proof
    may be manufactured merely to test availability.
    """
    connection.execute("""INSERT INTO upload_receipts(attempt_id, receipt_sha256, response, received_at)
        SELECT attempt_id, receipt_sha256, response, received_at FROM upload_receipts WHERE false""")


@retry_database_unit
def journal_multipart(db: Any, attempt_id: str, upload_id: str, timestamp: Any) -> None:
    """Record an UploadId this attempt created, BEFORE its first part is sent (#491).

    This row is the only authority to abort a multipart upload later: the provider
    listing alone cannot say whose it is. Own connection, committed on return.
    """
    with owned_connection(db()) as connection:
        connection.execute("INSERT INTO upload_multiparts(attempt_id, upload_id, created_at) "
                           "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (attempt_id, upload_id, timestamp))


@retry_database_unit
def close_multipart(db: Any, attempt_id: str, upload_id: str, timestamp: Any) -> None:
    """The provider no longer holds this UploadId (completed or aborted)."""
    with owned_connection(db()) as connection:
        connection.execute("UPDATE upload_multiparts SET closed_at=%s WHERE attempt_id=%s "
                           "AND upload_id=%s AND closed_at IS NULL", (timestamp, attempt_id, upload_id))


def close_multiparts(connection: Any, attempt_id: Any, timestamp: Any) -> None:
    """DB-only: the attempt no longer owes the provider an abort.

    Called when complete reads proved absence, when reconciliation published the
    version it found (a Complete whose response was lost), and when the attempt is
    settled. A closed row means "no live obligation", not "the provider is clean":
    an orphan the abort never reached is reaped by the bucket lifecycle.
    """
    connection.execute("UPDATE upload_multiparts SET closed_at=%s WHERE attempt_id=%s AND closed_at IS NULL",
                       (timestamp, attempt_id))


def open_multiparts(connection: Any, attempt_id: str) -> list[str]:
    return [r["upload_id"] for r in connection.execute(
        "SELECT upload_id FROM upload_multiparts WHERE attempt_id=%s AND closed_at IS NULL "
        "ORDER BY created_at, upload_id", (attempt_id,)).fetchall()]


def record_no_write(connection: Any, job: dict[str, Any], attempt: UploadAttempt,
                    operation: str, timestamp: Any) -> UploadAttempt:
    """Claim-fenced definitive no-write outcome of a started send.

    Called after the transfer manager joined all I/O and reported an auth
    refusal without a final receipt, or after complete S3 reads proved absence
    (ADR-0025). Zero listing matches alone never reach this seam. The outcome
    settles the attempt: its send-start stays as history and nothing is
    written to the receipt journal. Sending again needs a new attempt (new
    token) on the same snapshot. Caller owns/retries its DB-only unit.
    """
    if operation not in NO_WRITE_OPERATIONS:
        raise UploadBlocked("ambiguous_send")
    require_target(connection, job, attempt.target)
    if receipt(connection, attempt.id) is not None:
        raise UploadBlocked("ambiguous_receipt")
    changed = connection.execute(
        """UPDATE upload_attempts a SET settled_at=%s, outcome='no_write', no_write_operation=%s
           WHERE a.id=%s AND a.send_started_at IS NOT NULL AND a.settled_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM upload_receipts r WHERE r.attempt_id=a.id)
           RETURNING a.id""", (timestamp, operation, attempt.id)).fetchone()
    if not changed:
        raise UploadBlocked("ambiguous_receipt")
    close_multiparts(connection, attempt.id, timestamp)
    return replace(attempt, outcome="no_write", no_write_operation=operation)


def record_landed(connection: Any, job: dict[str, Any], attempt: UploadAttempt, version_id: str,
                  timestamp: Any, *, shared: bool = False) -> UploadAttempt:
    """Settle a started send at the exact VersionId it produced and close its snapshot.

    ``version_id`` comes from the journaled receipt or from exact
    reconciliation; every receipt of the attempt must agree with it. The
    Archive Version is then created with ``upload_attempt_id`` in the same
    unit (``ArchiveCatalog.begin_replacement``); the database refuses any
    other key, size or VersionId for it.
    """
    if not version_id or version_id == "null":
        raise UploadBlocked("ambiguous_receipt")
    require_target(connection, job, attempt.target, shared=shared)
    saved = receipt(connection, attempt.id)
    if saved is not None and str(saved["VersionId"]) != version_id:
        raise UploadBlocked("ambiguous_receipt")
    changed = connection.execute(
        """UPDATE upload_attempts SET settled_at=%s, outcome='landed', landed_version_id=%s
           WHERE id=%s AND send_started_at IS NOT NULL AND settled_at IS NULL RETURNING id""",
        (timestamp, version_id, attempt.id)).fetchone()
    if not changed:
        current = load_attempt(connection, attempt.id)
        if current is None or current.landed_version_id != version_id:
            raise UploadBlocked("ambiguous_receipt")
        return current
    close_multiparts(connection, attempt.id, timestamp)
    _close_snapshot(connection, attempt.admitted.id, timestamp, forget=False)
    return replace(attempt, outcome="landed", landed_version_id=version_id,
                   admitted=replace(attempt.admitted, closed=True))


def close_unsent_for_job(connection: Any, job: dict[str, Any], timestamp: Any) -> bool:
    """Close a Job's stale, never-sent Upload Snapshot after a source change (ADR-0030).

    The snapshot stays immutable; it is closed, never re-pinned. Closable only
    when no attempt of it is sending, landed or has a receipt, and the Job is
    not reconcile-only: its open unsent attempt is settled, its multipart rows
    are closed, and the Job forgets the snapshot. The next attempt
    fingerprints the current Local Copy and admits a new snapshot and token.
    A snapshot whose sends were all proven absent is closable too. Caller owns
    the claim-fenced DB unit. Returns True if closed.
    """
    row = connection.execute(
        """SELECT b.snapshot_id, b.reconcile_only, s.closed_at,
                  (SELECT a.id FROM upload_attempts a
                   WHERE a.snapshot_id=s.id AND a.settled_at IS NULL) AS open_attempt,
                  EXISTS (SELECT 1 FROM upload_attempts a WHERE a.snapshot_id=s.id
                          AND ((a.settled_at IS NULL AND a.send_started_at IS NOT NULL)
                               OR a.outcome='landed')) AS committed,
                  EXISTS (SELECT 1 FROM upload_attempts a JOIN upload_receipts r
                          ON r.attempt_id=a.id WHERE a.snapshot_id=s.id) AS received
           FROM job_uploads b JOIN upload_snapshots s ON s.id=b.snapshot_id
           WHERE b.job_id=%s FOR UPDATE OF b""", (job["id"],)).fetchone()
    if not row or row["reconcile_only"] or row["committed"] or row["received"]:
        return False
    if row["open_attempt"] is not None:
        _settle_unsent(connection, row["open_attempt"], timestamp)
    if row["closed_at"] is None:
        _close_snapshot(connection, row["snapshot_id"], timestamp, forget=True)
    else:
        connection.execute("UPDATE job_uploads SET snapshot_id=NULL WHERE job_id=%s", (job["id"],))
    return True


def _link(connection: Any, job_id: int, snapshot_id: Any, mode: str, *,
          reconcile_only: bool | None = None) -> None:
    """Point the Job's upload detail at a snapshot (the former binding row).

    A Job admitted by queue_jobs already has its detail; a Job created
    elsewhere gets one in the snapshot's mode. A link is set once: a Job never
    switches to another snapshot while one is linked.
    """
    linked = connection.execute(
        """INSERT INTO job_uploads(job_id, verification_mode, reconcile_only, snapshot_id)
           VALUES (%s, %s, COALESCE(%s, false), %s)
           ON CONFLICT (job_id) DO UPDATE SET snapshot_id=EXCLUDED.snapshot_id,
               reconcile_only=COALESCE(%s, job_uploads.reconcile_only)
           WHERE job_uploads.snapshot_id IS NULL OR job_uploads.snapshot_id=EXCLUDED.snapshot_id
           RETURNING job_id""",
        (job_id, mode, reconcile_only, snapshot_id, reconcile_only)).fetchone()
    if linked is None:
        raise UploadBlocked("identity_changed")


def bind_for_resume(connection: Any, job_id: int, attempt: UploadAttempt, *,
                    reconcile_only: bool | None = True) -> None:
    """Bind a Job to the snapshot of an uncertain attempt.

    A resume Job is reconcile-only (the default); the upload worker binds an
    ordinary Job that finds the file's outstanding attempt with
    ``reconcile_only=None``, keeping the Job's own flag, so it may send the
    same snapshot again (a new attempt) after a proven no-write. A cancelled
    original or bound Job is never resurrected.
    """
    cancelled = connection.execute(
        """SELECT 1 FROM jobs j WHERE j.status='cancelled' AND (j.id=%s OR j.id IN (
               SELECT b.job_id FROM job_uploads b WHERE b.snapshot_id=%s)) LIMIT 1""",
        (attempt.job_id, attempt.admitted.id)).fetchone()
    if cancelled:
        raise UploadBlocked("identity_changed")
    _link(connection, job_id, attempt.admitted.id, attempt.admitted.verification,
          reconcile_only=reconcile_only)


def proven_absent(connection: Any, attempt_id: str | None) -> bool:
    """True when this attempt was settled by S3's positive proof of absence."""
    if not attempt_id:
        return False
    return connection.execute("SELECT 1 FROM upload_attempts WHERE id=%s AND outcome='no_write' "
                              "AND no_write_operation=%s", (attempt_id, ABSENT_OPERATION)).fetchone() is not None


def unsent(connection: Any, attempt_id: str | None) -> bool:
    """The attempt carries no provider obligation: never sent, or a settled no-write without receipts."""
    if not attempt_id:
        return False
    return connection.execute(
        """SELECT 1 FROM upload_attempts a WHERE a.id=%s
             AND (a.send_started_at IS NULL OR a.outcome='no_write')
             AND NOT EXISTS (SELECT 1 FROM upload_receipts r WHERE r.attempt_id=a.id)""",
        (attempt_id,)).fetchone() is not None


def receipt(connection: Any, attempt_id: str) -> dict[str, Any] | None:
    """The attempt's journaled receipts combined, or None when there is none.

    Contradictions never resolve by choice: differing facts, a missing or
    ``null`` VersionId, any receipt of an unsent or no-write attempt, or a
    VersionId other than the landed one raise ``ambiguous_receipt``.
    """
    rows = connection.execute(
        "SELECT r.response, a.outcome, a.landed_version_id FROM upload_attempts a "
        "JOIN upload_receipts r ON r.attempt_id=a.id WHERE a.id=%s", (attempt_id,)).fetchall()
    if not rows:
        return None
    if rows[0]["outcome"] in {"unsent", "no_write"}:
        raise UploadBlocked("ambiguous_receipt")
    # Discovery may add fields, but incompatible proofs/identities never overwrite.
    combined: dict[str, Any] = {}
    for row in rows:
        response = row["response"]
        if not isinstance(response, dict):
            raise UploadBlocked("ambiguous_receipt")
        for key, value in response.items():
            if key in combined and combined[key] != value:
                raise UploadBlocked("ambiguous_receipt")
            combined[key] = value
    if not combined.get("VersionId") or combined["VersionId"] == "null":
        raise UploadBlocked("ambiguous_receipt")
    if rows[0]["outcome"] == "landed" and combined["VersionId"] != rows[0]["landed_version_id"]:
        raise UploadBlocked("ambiguous_receipt")
    return combined


def expected_for_version(connection: Any, archive_version_id: str) -> AdmittedSnapshot | None:
    """The frozen expected proof of an uploaded Archive Version, via its attempt."""
    row = connection.execute(
        f"SELECT {_SNAPSHOT_COLUMNS} FROM archive_versions av "
        f"JOIN upload_attempts a ON a.id=av.upload_attempt_id AND a.file_id=av.file_id "
        f"JOIN {_SNAPSHOT_FROM} ON s.id=a.snapshot_id WHERE av.id=%s", (archive_version_id,)).fetchone()
    return _admitted(row) if row else None


def reconcile(client: Any, attempt: UploadAttempt, saved: dict[str, Any] | None,
              check_active: Any, *, owned_multiparts: Any = (),
              on_multipart_aborted: Any = None) -> dict[str, Any]:
    """Decide a started send of ``attempt`` from exact S3 reads only.

    The attempt's own token, its pinned pre-send inventory and the frozen
    snapshot are the whole evidence; earlier attempts' tokens are not its own.
    """
    pinned = attempt.target
    if provider_identity(client) != pinned["provider"]:
        raise UploadBlocked("identity_changed")
    before = attempt.inventory
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
            if (head.get('Metadata') or {}).get(TOKEN_KEY) == attempt.token:
                matches.append((version, head))
        second = inventory(client, pinned['bucket'], pinned['key'])
        if first == second:
            break
        if observation or not _own_write_arrived(client, attempt, saved, before, first, second, check_active):
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
        expected = attempt.snapshot
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


def _own_write_arrived(client: Any, attempt: UploadAttempt, saved: dict[str, Any] | None,
                       before: dict[str, Any], first: dict[str, Any], second: dict[str, Any],
                       check_active: Any) -> bool:
    added = set(second['versions']) - set(first['versions'])
    if (first != before or second['markers'] != before['markers'] or len(added) != 1
            or set(second['versions']) != set(before['versions']) | added):
        return False
    version = next(iter(added))
    if saved and saved['VersionId'] != version:
        raise UploadBlocked('ambiguous_receipt')
    pinned = attempt.target
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
    if (head.get('Metadata') or {}).get(TOKEN_KEY) != attempt.token:
        return False
    try:
        expected = attempt.snapshot
        proof = validated_s3_proof(head, version_id=version, snapshot=expected, source='exact_head')
        if saved:
            validated_s3_proof(saved, version_id=version, snapshot=expected, source='upload_response', completion=True)
    except UploadProofMismatch as exc:
        raise UploadBlocked('proof_mismatch') from exc
    # Token/size/catalog membership alone never attribute an automatic retry.
    return proof is not None


# Resume/Retry projections read the proofs above and the file's explicit
# reconciliation block (#593); remediation episodes are #592's.
_OWNER_CANCELLED_SQL = """SELECT 1 FROM jobs j LEFT JOIN job_uploads u ON u.job_id=j.id
    WHERE j.file_id=%s AND j.action='upload' AND j.status='cancelled'
      AND (coalesce(u.reconcile_only, false)
           OR EXISTS (SELECT 1 FROM upload_attempts a WHERE a.job_id=j.id
                      AND a.send_started_at IS NOT NULL))
    LIMIT 1"""
_ACTIVE_JOB_SQL = ("SELECT 1 FROM jobs WHERE file_id=%s "
                   "AND status NOT IN ('failed','cancelled','completed') LIMIT 1")


def describe(connection: Any, *, vault_id: int, path: str) -> dict[str, Any]:
    """Whether the file has an upload whose provider outcome is unknown.

    Pending when the file carries a reconciliation block, a started attempt
    that never settled, or a receipt contradicting a settled attempt. Resume
    is offered only when nothing else is running on the file and the Owner
    did not cancel its reconciliation.
    """
    empty = {"pending": False, "can_resume": False, "reason": None, "remedy": None}
    row = connection.execute(
        "SELECT id, reconciliation_block FROM files WHERE vault_id=%s AND path=%s",
        (vault_id, path),
    ).fetchone()
    if not row:
        return empty
    file_id = row["id"]
    attempt = connection.execute(
        f"""SELECT {_CONTRADICTED} AS contradicted,
                   EXISTS (SELECT 1 FROM upload_attempts a WHERE a.file_id=%(file)s
                           AND a.settled_at IS NULL AND a.send_started_at IS NOT NULL) AS open_send""",
        {"file": file_id},
    ).fetchone()
    if row["reconciliation_block"] is None and not attempt["open_send"] and not attempt["contradicted"]:
        return empty
    reason = row["reconciliation_block"] or (
        "ambiguous_receipt" if attempt["contradicted"] else "ambiguous_send")
    busy = connection.execute(_ACTIVE_JOB_SQL, (file_id,)).fetchone()
    cancelled = connection.execute(_OWNER_CANCELLED_SQL, (file_id,)).fetchone()
    return {"pending": True, "can_resume": not busy and not cancelled,
            "reason": reason, "remedy": REMEDIES.get(reason, REMEDIES["proof_unavailable"])}


def _unresumable_reason(connection: Any, vault_id: int, path: str, state: dict[str, Any]) -> str:
    """Name why exact resume cannot start; never imply the path/volume changed."""
    if not state["pending"]:
        return "already_resolved"
    file = connection.execute(
        "SELECT id FROM files WHERE vault_id=%s AND path=%s", (vault_id, path),
    ).fetchone()
    if file:
        if connection.execute(_ACTIVE_JOB_SQL, (file["id"],)).fetchone():
            return "active_operation"
        if connection.execute(_OWNER_CANCELLED_SQL, (file["id"],)).fetchone():
            return "owner_cancelled"
    return state["reason"] if state.get("reason") in REMEDIES else "proof_unavailable"


def schedule_resume(connection: Any, *, vault: dict[str, Any], path: str, user_id: int, timestamp: str, remediation_id: str | None = None) -> dict[str, Any]:
    from .upload_remediation import schedule_resume as schedule
    return schedule(connection, vault=vault, path=path, user_id=user_id, timestamp=timestamp, remediation_id=remediation_id)
