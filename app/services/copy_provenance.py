"""Server-side copies of an exact Archive Version (#595, ADR-0035).

A rename copies the file's Current to the key of its new path; a storage-class
change or Vault move copies it in place to another class. Both produce the
file's replacement in progress and never touch Current until the copy is
verified; the superseded version is then deleted by the exact cleanup (#591).

Interface: ``run(worker, job, purpose=..., storage_class=...)`` from a claimed
rename or storage-class Job. It returns the verified keeper (the new Current),
or None when Current already is what the Job asked for.

Authority and proofs (``copy_attempts``):

- the attempt freezes, in one transaction before any provider request, the
  exact source (key and VersionId of the verified Current, never latest or a
  HEAD of the key), the destination key and class, the expected digest and
  size, the path epoch, the Source Volume root, the provider endpoint and the
  destination inventory; scope is checked by the schema (same file, same
  Vault namespace, a Job of the same purpose);
- ``send_started_at`` commits before CopyObject/CreateMultipartUpload: a DB
  retry, a crash or a takeover never sends the copy again. A started attempt
  is reconciled from S3 only: its journaled receipt or its own token in the
  destination, at an exact VersionId; nothing found while the inventory is
  unchanged and no unowned multipart is open proves it absent; anything else
  stays blocked with every copy kept;
- receipts and multipart UploadIds are journaled immediately on their own
  connection, independent of the Job, its cancellation and its claim;
- the landed version is recorded as soon as it is known (a claim-fenced unit),
  then made the replacement in progress only under the full target fence;
  publication (verification and promotion) and the exact cleanup are separate
  fenced units. The Local Copy is matched to the new Current only through a
  digest of the real local bytes (a rename hashes them again), never through
  the remote digest.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from . import upload_provenance as provenance
from .database_retry import owned_connection, retry_database_unit
from .s3_copy import (
    COPY_TOKEN_KEY, S3_SINGLE_COPY_MAX_BYTES, _copy_storage_class_version,
    _head_object_with_checksum, _multipart_source_checksum, _sha256_s3_version,
    _full_object_sha256_checksum, restore_header_state, storage_class_requires_restore,
)
from .upload_provenance import UploadBlocked

COPY_ACTIONS = frozenset({"rename", "storage-class"})
_STOPPED = {"rename": "Rename stopped", "storage-class": "Storage class change stopped"}
_MAX_ROUNDS = 4


class CopyNeedsRestore(RuntimeError):
    """The exact source is in an archive class and not readable yet."""

    def __init__(self, storage_class: str, restore_state: str, expiry: str | None) -> None:
        super().__init__(f"The source Archive Version ({storage_class}) must be restored first")
        self.storage_class, self.restore_state, self.expiry = storage_class, restore_state, expiry


@dataclass(frozen=True)
class Attempt:
    id: str
    purpose: str
    job_id: int | None
    source_version_id: str | None
    source_key: str
    source_provider_version_id: str
    destination_key: str
    storage_class: str
    size: int
    plaintext_sha256: bytes
    token: str
    inventory: dict[str, list[str]]
    composite_checksum: str | None
    send_started: bool
    bucket: str

    @property
    def expected_checksum(self) -> str:
        return base64.b64encode(self.plaintext_sha256).decode("ascii")


def _attempt(row: dict[str, Any], bucket: str) -> Attempt:
    return Attempt(
        id=str(row["id"]), purpose=row["purpose"], job_id=row["job_id"],
        source_version_id=str(row["source_archive_version_id"]) if row.get("source_archive_version_id") else None,
        source_key=row["source_key"], source_provider_version_id=row["source_provider_version_id"],
        destination_key=row["destination_key"], storage_class=row["storage_class"],
        size=int(row["size"]), plaintext_sha256=bytes(row["plaintext_sha256"]),
        token=str(row["token"]),
        inventory={"versions": sorted(row["inventory_versions"]),
                   "markers": sorted(row["inventory_markers"])},
        composite_checksum=row["composite_checksum"],
        send_started=row["send_started_at"] is not None, bucket=bucket)


_ATTEMPT_SQL = """SELECT c.*, src.id AS source_archive_version_id
    FROM copy_attempts c
    LEFT JOIN archive_versions src ON src.file_id = c.file_id
        AND src.object_key = c.source_key
        AND src.provider_version_id = c.source_provider_version_id"""


def _state(connection: Any, job: dict[str, Any]) -> dict[str, Any]:
    """The file's live copy state, read inside a claim- and target-fenced unit."""
    file_id = job["file_id"]
    row = connection.execute(
        """SELECT f.generation, f.local_presence, f.local_file_type, f.local_size,
                  f.local_mtime_ns, f.local_sha256, f.matched_version_id,
                  f.current_version_id, f.replacing_version_id,
                  EXISTS (SELECT 1 FROM upload_attempts a WHERE a.file_id = f.id
                          AND a.settled_at IS NULL) AS upload_open
           FROM files f WHERE f.id=%s""", (file_id,)).fetchone()
    versions = {
        str(v["id"]): dict(v) for v in connection.execute(
            "SELECT * FROM archive_versions WHERE id = ANY(%s::uuid[])",
            ([str(x) for x in (row["current_version_id"], row["replacing_version_id"]) if x],),
        ).fetchall()
    }
    attempt = connection.execute(
        _ATTEMPT_SQL + " WHERE c.file_id=%s AND c.settled_at IS NULL", (file_id,)).fetchone()
    return {
        "file": dict(row), "pin": provenance.validated_pin(connection, job),
        "current": versions.get(str(row["current_version_id"])) if row["current_version_id"] else None,
        "replacing": versions.get(str(row["replacing_version_id"])) if row["replacing_version_id"] else None,
        "attempt": dict(attempt) if attempt else None,
    }


# -- journals on their own connection (independent of claim and cancellation) --

def _receipt_fact(response: dict[str, Any]) -> dict[str, Any]:
    fact = {k: response[k] for k in ("VersionId", "ETag", "ChecksumSHA256", "ChecksumType")
            if response.get(k) is not None}
    result = response.get("CopyObjectResult") or {}
    for k in ("ETag", "ChecksumSHA256"):
        if result.get(k) is not None:
            fact.setdefault(k, result[k])
    return fact


@retry_database_unit
def append_receipt(db: Any, attempt_id: str, response: dict[str, Any]) -> None:
    text = json.dumps(_receipt_fact(response), sort_keys=True, separators=(",", ":"))
    with owned_connection(db()) as connection:
        connection.execute(
            "INSERT INTO copy_receipts(attempt_id, receipt_sha256, response) "
            "VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
            (attempt_id, hashlib.sha256(text.encode()).digest(), text))


@retry_database_unit
def journal_multipart(db: Any, attempt_id: str, upload_id: str) -> None:
    with owned_connection(db()) as connection:
        connection.execute(
            "INSERT INTO copy_multiparts(attempt_id, upload_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING", (attempt_id, upload_id))


@retry_database_unit
def _close_multiparts(db: Any, attempt_id: str, upload_ids: list[str] | None = None) -> None:
    with owned_connection(db()) as connection:
        connection.execute(
            """UPDATE copy_multiparts SET closed_at=now()
               WHERE attempt_id=%s AND closed_at IS NULL
                 AND (%s::text[] IS NULL OR upload_id = ANY(%s::text[]))""",
            (attempt_id, upload_ids, upload_ids))


def _receipt_versions(connection: Any, attempt_id: str) -> set[str]:
    rows = connection.execute(
        "SELECT response->>'VersionId' AS version FROM copy_receipts WHERE attempt_id=%s",
        (attempt_id,)).fetchall()
    return {row["version"] for row in rows if row["version"]}


# -- the copy --------------------------------------------------------------

def _source_head(client: Any, bucket: str, current: dict[str, Any]) -> dict[str, Any]:
    head = _head_object_with_checksum(
        client, Bucket=bucket, Key=current["object_key"], VersionId=current["provider_version_id"])
    if (str(head.get("VersionId") or "") != str(current["provider_version_id"])
            or head.get("DeleteMarker")
            or head.get("ContentLength") is None or int(head["ContentLength"]) != int(current["size"])):
        raise UploadBlocked("identity_changed")
    return head


def _goal(purpose: str, pin: dict[str, Any], current: dict[str, Any], head_class: str,
          storage_class: str | None) -> tuple[str, str]:
    """Destination key and class of the copy the Job asks for."""
    if purpose == "rename":
        return pin["key"], head_class
    if current["object_key"] != pin["key"]:
        # The Current still lives at a previous path's key: its rename first.
        raise UploadBlocked("identity_changed")
    return current["object_key"], (storage_class or "").upper()


def _open(w: Any, job: dict[str, Any], *, purpose: str, destination_key: str,
          storage_class: str, current: dict[str, Any], inventory: dict[str, list[str]],
          provider: str) -> None:
    def open_attempt(catalog: Any) -> None:
        connection = catalog.connection
        state = _state(connection, job)
        pin = state["pin"]
        live = state["current"]
        if (state["attempt"] is not None or state["replacing"] is not None
                or live is None or str(live["id"]) != str(current["id"])):
            raise UploadBlocked("target_superseded")
        connection.execute(
            """INSERT INTO copy_attempts(
                   vault_id, file_id, job_id, purpose, source_key, source_provider_version_id,
                   destination_key, storage_class, size, plaintext_sha256, path, path_since,
                   source_root, root_identity, provider, inventory_versions, inventory_markers)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (job["vault_id"], job["file_id"], job["id"], purpose, live["object_key"],
             live["provider_version_id"], destination_key, storage_class, live["size"],
             bytes(live["plaintext_sha256"]), pin["path"], pin["path_since"],
             pin["source_root"], pin["root_identity"], provider,
             inventory["versions"], inventory["markers"]))

    w._upload_catalog_unit(job, open_attempt)


def _load(w: Any, job: dict[str, Any], bucket: str) -> Attempt:
    def load(catalog: Any) -> Attempt:
        row = catalog.connection.execute(
            _ATTEMPT_SQL + " WHERE c.file_id=%s AND c.settled_at IS NULL", (job["file_id"],)).fetchone()
        if row is None:
            raise UploadBlocked("target_superseded")
        return _attempt(dict(row), bucket)

    return w._upload_catalog_unit(job, load, shared=True)


def _adopt(w: Any, job: dict[str, Any], attempt_id: str) -> None:
    """A later Job of the same purpose takes over an attempt (crash, expiry, takeover)."""
    def adopt(catalog: Any) -> None:
        catalog.connection.execute(
            "UPDATE copy_attempts SET job_id=%s WHERE id=%s AND job_id IS DISTINCT FROM %s",
            (job["id"], attempt_id, job["id"]))

    w._upload_catalog_unit(job, adopt)


def _settle(w: Any, job: dict[str, Any], attempt_id: str, *, outcome: str,
            operation: str | None = None) -> None:
    def settle(catalog: Any) -> None:
        catalog.connection.execute(
            """UPDATE copy_attempts SET settled_at=now(), outcome=%s, no_write_operation=%s
               WHERE id=%s AND settled_at IS NULL""",
            (outcome, operation, attempt_id))

    w._upload_catalog_unit(job, settle)


def _mark_send_started(w: Any, job: dict[str, Any], attempt: Attempt, composite: str | None) -> None:
    def start(catalog: Any) -> None:
        state = _state(catalog.connection, job)
        live = state["current"]
        if (live is None or live["object_key"] != attempt.source_key
                or live["provider_version_id"] != attempt.source_provider_version_id
                or state["replacing"] is not None):
            raise UploadBlocked("target_superseded")
        started = catalog.connection.execute(
            """UPDATE copy_attempts SET send_started_at=now(), composite_checksum=%s
               WHERE id=%s AND send_started_at IS NULL AND settled_at IS NULL""",
            (composite, attempt.id))
        if getattr(started, "rowcount", 1) != 1:
            raise UploadBlocked("ambiguous_send")

    w._upload_catalog_unit(job, start)


def _send(w: Any, job: dict[str, Any], attempt: Attempt, client: Any, head: dict[str, Any],
          active: Callable[..., None]) -> str | None:
    composite = None
    if attempt.size > S3_SINGLE_COPY_MAX_BYTES:
        # The independent composite proof is read from the exact source once,
        # before the send, and frozen with the send start.
        composite = _multipart_source_checksum(
            client, bucket=attempt.bucket, object_key=attempt.source_key,
            version_id=attempt.source_provider_version_id, size=attempt.size,
            expected_sha256=attempt.expected_checksum, check_active=lambda *_: active())
    active()
    _mark_send_started(w, job, attempt, composite)
    return _copy_storage_class_version(
        client, bucket=attempt.bucket, object_key=attempt.destination_key,
        source_key=attempt.source_key, source_version_id=attempt.source_provider_version_id,
        target_class=attempt.storage_class, source_size=attempt.size, source_head=head,
        check_active=lambda *_: active(), intent_token=attempt.token,
        on_upload_id=lambda upload_id: journal_multipart(w.db, attempt.id, upload_id),
        on_response=lambda response: append_receipt(w.db, attempt.id, response))


def _exact_destination(client: Any, attempt: Attempt, version: str) -> dict[str, Any]:
    head = _head_object_with_checksum(
        client, Bucket=attempt.bucket, Key=attempt.destination_key, VersionId=version)
    if str(head.get("VersionId") or "") != version or head.get("DeleteMarker"):
        raise UploadBlocked("ambiguous_receipt")
    return head


def _owns(head: dict[str, Any], attempt: Attempt) -> bool:
    return (head.get("Metadata") or {}).get(COPY_TOKEN_KEY) == attempt.token


def reconcile(w: Any, job: dict[str, Any], attempt: Attempt, client: Any,
              active: Callable[..., None]) -> str | None:
    """Decide a started copy from S3 only; never send it again.

    Returns the landed VersionId, or None once absence is proven (the attempt
    is then settled ``no_write`` and a new attempt may copy). Raises
    ``UploadBlocked`` whenever the outcome cannot be attributed exactly.
    """
    with w.db() as connection:
        receipted = _receipt_versions(connection, attempt.id)
        owned = [r["upload_id"] for r in connection.execute(
            "SELECT upload_id FROM copy_multiparts WHERE attempt_id=%s AND closed_at IS NULL",
            (attempt.id,)).fetchall()]
    if len(receipted) > 1:
        raise UploadBlocked("ambiguous_receipt")
    if receipted:
        version = next(iter(receipted))
        active()
        if not _owns(_exact_destination(client, attempt, version), attempt):
            raise UploadBlocked("ambiguous_receipt")
        return version
    first = provenance.inventory(client, attempt.bucket, attempt.destination_key)
    second = provenance.inventory(client, attempt.bucket, attempt.destination_key)
    if first != second:
        raise UploadBlocked("inventory_changed")
    matches = []
    for version in sorted(set(first["versions"]) - set(attempt.inventory["versions"])):
        active()
        if _owns(_exact_destination(client, attempt, version), attempt):
            matches.append(version)
    if len(matches) > 1:
        raise UploadBlocked("ambiguous_receipt")
    if matches:
        return matches[0]
    if first != attempt.inventory:
        raise UploadBlocked("ambiguous_send")
    pinned = {"bucket": attempt.bucket, "key": attempt.destination_key}
    found = provenance._incomplete_uploads(client, attempt.bucket, attempt.destination_key)
    if found and set(owned).intersection(found):
        provenance._abort_owned_multiparts(
            client, pinned, found, frozenset(owned), active,
            lambda upload_id: _close_multiparts(w.db, attempt.id, [upload_id]))
        found = provenance._incomplete_uploads(client, attempt.bucket, attempt.destination_key)
    if found:
        raise UploadBlocked("ambiguous_send")
    active()
    if provenance.inventory(client, attempt.bucket, attempt.destination_key) != attempt.inventory:
        raise UploadBlocked("inventory_changed")
    _close_multiparts(w.db, attempt.id)
    _settle(w, job, attempt.id, outcome="no_write", operation=provenance.ABSENT_OPERATION)
    return None


def _record_landed(w: Any, job: dict[str, Any], attempt: Attempt, version: str) -> None:
    """Record the exact landed version, then make it the replacement in progress.

    The settlement and the version row only need the claim: they are facts
    that must survive a later identity change (the version is then a
    superseded write with open cleanup). The replacement needs the full fence.
    """
    @retry_database_unit
    def record() -> None:
        with owned_connection(w.db()) as connection:
            w.ensure_job_claim_owned_in_transaction(connection, job, "Copy claim was lost")
            connection.execute(
                """UPDATE copy_attempts SET settled_at=now(), outcome='landed', landed_version_id=%s
                   WHERE id=%s AND settled_at IS NULL""", (version, attempt.id))
            connection.execute(
                """INSERT INTO archive_versions(file_id, object_key, provider_version_id, size,
                       storage_class, availability, availability_checked_at, copy_attempt_id)
                   SELECT file_id, destination_key, landed_version_id, size, storage_class,
                          'available', now(), id
                   FROM copy_attempts WHERE id=%s AND outcome='landed'
                   ON CONFLICT (copy_attempt_id) DO NOTHING""", (attempt.id,))
            connection.execute(
                "UPDATE copy_multiparts SET closed_at=now() WHERE attempt_id=%s AND closed_at IS NULL",
                (attempt.id,))

    record()

    def replace(catalog: Any) -> None:
        connection = catalog.connection
        moved = connection.execute(
            """UPDATE files f SET replacing_version_id = av.id
               FROM archive_versions av, copy_attempts c, archive_versions cur
               WHERE f.id=%s AND av.copy_attempt_id = c.id AND c.id=%s
                 AND cur.id = f.current_version_id AND f.replacing_version_id IS NULL
                 AND cur.object_key = c.source_key
                 AND cur.provider_version_id = c.source_provider_version_id
                 AND f.path_since = c.path_since
               RETURNING f.vault_id, f.path""", (job["file_id"], attempt.id)).fetchone()
        if moved is None:
            raise UploadBlocked("target_superseded")
        catalog._mark_path_aggregates_dirty(int(moved["vault_id"]), moved["path"])

    w._upload_catalog_unit(job, replace)


def _local_generation(w: Any, job: dict[str, Any], state: dict[str, Any], expected: bytes,
                      purpose: str) -> int | None:
    """The Local Copy observation the new Current may be matched to.

    Only a digest of real local bytes: a rename hashes the file at its new
    path again (published by compare-and-swap on the generation read before
    hashing); a class change reuses the fingerprint of an unchanged
    observation. The remote digest never stands in for the Local Copy.
    """
    row = state["file"]
    if row["local_presence"] != "present" or row["local_file_type"] != "regular":
        return None
    generation = int(row["generation"])
    if purpose != "rename":
        return generation
    from .local_files import hash_stable_regular_file
    from .vault_paths import safe_local_path
    try:
        w._require_upload_source_identity(job)
        digest, info = hash_stable_regular_file(safe_local_path(job["source_root"], job["path"]))
    except (OSError, RuntimeError, ValueError):
        return None
    if (info.st_size, info.st_mtime_ns) != (row["local_size"], row["local_mtime_ns"]):
        return None
    if bytes.fromhex(digest) != expected:
        return None

    def publish(catalog: Any) -> bool:
        return catalog.publish_local_fingerprint(
            job["file_id"], generation=generation, plaintext_sha256=digest,
            size=info.st_size, mtime_ns=info.st_mtime_ns)

    return generation if w._upload_catalog_unit(job, publish) else None


def _verify_and_promote(w: Any, job: dict[str, Any], version: dict[str, Any],
                        state: dict[str, Any], client: Any, bucket: str,
                        active: Callable[..., None]) -> str:
    """Prove the copied version against the frozen expectation, then make it Current."""
    attempt_row = None
    with w.db() as connection:
        attempt_row = connection.execute(
            _ATTEMPT_SQL + " WHERE c.id=%s", (version["copy_attempt_id"],)).fetchone()
    attempt = _attempt(dict(attempt_row), bucket)
    active()
    head = _exact_destination(client, attempt, str(version["provider_version_id"]))
    observed_class = (head.get("StorageClass") or "STANDARD").upper()
    checksum = _full_object_sha256_checksum(head)
    if attempt.composite_checksum and head.get("ChecksumType") == "COMPOSITE":
        checksum = (attempt.expected_checksum
                    if head.get("ChecksumSHA256") == attempt.composite_checksum else None)
    if checksum is None and not storage_class_requires_restore(observed_class):
        checksum = _sha256_s3_version(
            client, bucket=bucket, object_key=attempt.destination_key,
            version_id=str(version["provider_version_id"]), check_active=lambda *_: active())
    proven = (_owns(head, attempt) and observed_class == attempt.storage_class.upper()
              and head.get("ContentLength") is not None
              and int(head["ContentLength"]) == attempt.size
              and checksum == attempt.expected_checksum)
    if not proven:
        if checksum is None:
            raise UploadBlocked("proof_unavailable")

        def mismatch(catalog: Any) -> None:
            catalog.abandon_replacement(
                job["file_id"], archive_version_id=str(version["id"]), mismatch=True,
                checked_at=w.now_iso())

        w._upload_catalog_unit(job, mismatch)
        raise UploadBlocked("proof_mismatch")
    generation = _local_generation(w, job, state, attempt.plaintext_sha256, attempt.purpose)
    current = state["current"] or {}
    verified_at = current.get("verified_at") if attempt.purpose == "storage-class" else None
    active()

    def promote(catalog: Any) -> None:
        catalog.promote_replacement(
            job["file_id"], archive_version_id=str(version["id"]),
            plaintext_sha256=attempt.plaintext_sha256,
            verified_at=verified_at or w.now_iso(), local_generation=generation)
        catalog.connection.execute(
            """UPDATE archive_versions SET verification_method='s3_checksum'
               WHERE id=%s AND verification_method IS NULL""", (str(version["id"]),))
        catalog.link_job_version(int(job["id"]), str(version["id"]))

    w._upload_catalog_unit(job, promote)
    return str(version["id"])


def run(w: Any, job: dict[str, Any], *, purpose: str, storage_class: str | None = None,
        check_source: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
        client: Any = None) -> str | None:
    """Copy the file's Current as ``purpose`` asks; return the new verified Current.

    ``check_source(head, current)`` runs on the exact source HEAD before a new
    attempt is opened (never on a resumed one): it may refuse the copy.
    """
    if purpose not in COPY_ACTIONS:
        raise ValueError(f"Unsupported copy purpose: {purpose}")
    active = lambda *_: w.ensure_job_active(job["id"], _STOPPED[purpose])
    active()
    client = client or w.s3_client()
    provider = provenance.provider_identity(client)
    bucket = job["s3_bucket"]
    keeper: str | None = None
    cleanup_keeper: str | None = None
    for _round in range(_MAX_ROUNDS):
        state = w._upload_catalog_unit(job, lambda c: _state(c.connection, job), shared=True)
        if state["file"]["upload_open"]:
            raise UploadBlocked("active_operation")
        replacing = state["replacing"]
        if replacing is not None:
            if replacing.get("copy_attempt_id") is None:
                raise UploadBlocked("active_operation")
            keeper = _verify_and_promote(w, job, replacing, state, client, bucket, active)
            break
        row = state["attempt"]
        if row is not None:
            if row["purpose"] != purpose:
                raise UploadBlocked("active_operation")
            if row["job_id"] != job["id"]:
                _adopt(w, job, str(row["id"]))
            attempt = _attempt(row, bucket)
            if not attempt.send_started:
                _settle(w, job, attempt.id, outcome="unsent")
                continue
            version = reconcile(w, job, attempt, client, active)
            if version is not None:
                _record_landed(w, job, attempt, version)
            continue
        current = state["current"]
        if current is None or current["integrity"] != "verified" or current["availability"] != "available":
            raise UploadBlocked("keeper_missing")
        active()
        head = _source_head(client, bucket, current)
        head_class = (head.get("StorageClass") or "STANDARD").upper()
        destination_key, target_class = _goal(purpose, state["pin"], current, head_class, storage_class)
        if destination_key == current["object_key"] and target_class == head_class:
            # Current already is what the Job asked for; a crash after its
            # promotion may still have left the superseded version to remove.
            cleanup_keeper = str(current["id"])
            break
        if check_source is not None:
            check_source(head, current)
        if storage_class_requires_restore(head_class):
            restore, expiry = restore_header_state(head.get("Restore"))
            if restore != "available":
                raise CopyNeedsRestore(head_class, restore, expiry)
        inventory = provenance.inventory(client, bucket, destination_key)
        _open(w, job, purpose=purpose, destination_key=destination_key,
              storage_class=target_class, current=current, inventory=inventory, provider=provider)
        attempt = _load(w, job, bucket)
        try:
            version = _send(w, job, attempt, client, head, active)
        except (w.OperationCancelled, w.JobLeaseLost, w.WorkerStopping, UploadBlocked):
            raise
        except Exception:
            # Outcome unknown: the next round reconciles from S3, never resends.
            continue
        if version:
            if not _owns(_exact_destination(client, attempt, str(version)), attempt):
                raise UploadBlocked("ambiguous_receipt")
            _record_landed(w, job, attempt, str(version))
    else:
        raise UploadBlocked("ambiguous_send")
    # The superseded copy is removed only now, by exact VersionId; without a
    # new keeper an open cleanup of Current is finished (or nothing is done).
    w.cleanup_superseded_upload(job, keeper_id=keeper or cleanup_keeper, client=client)
    return keeper
