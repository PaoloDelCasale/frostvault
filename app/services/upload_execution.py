"""One claimed upload -> one durable outcome, never a replay of provider I/O.

The caller does not orchestrate snapshots, sends, proofs or cleanup. This Module
owns their ordering on the rebuilt model (#590, ADR-0035/0036):

1. the pre-hash observation: the Local Copy generation read (or recorded)
   before hashing is the only one any later publication compares against;
2. the Upload Snapshot and its target, written once by ``prepare``; each send
   is an Upload Attempt with its own token, committed as send-started before
   the first provider request;
3. the receipt, journaled immediately and independently of the Job;
4. materialization: the landed attempt and its Archive Version (the
   replacement in progress) in one DB unit;
5. verification of that exact VersionId against the attempt's snapshot, the
   only expected proof (never a historical object, never today's file);
6. publication: Current, the Local Copy match and the completion, each in a
   claim-fenced DB unit after verification.

A receipt or a materialized version alone never makes a version Current nor
completes the Job. Every DB unit is replayed alone on a serialization failure
(``retry_database_unit``); provider and filesystem effects are never inside one.

``worker`` is the existing storage Adapter, not a bag of injected callbacks: its
claim/status/source/transport hooks remain the production and test seams.
There is deliberately no import of storage, executor, queue or heartbeat here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import os
import stat
import threading
import time

import psycopg

from . import upload_pipeline, upload_provenance as provenance
from .database_retry import owned_connection
from .upload_snapshot import CHUNK_SIZE, UploadSnapshot, UploadProofMismatch, unsupported_checksum_api


@dataclass(frozen=True)
class UploadOutcome:
    job_id: int
    status: str
    archive_version_id: str | None = None


class _UploadCheckpoint:
    """Only hashing/permit waits amortize renewals; effects still fence durably.

    Remote cancellation can take at most one second to stop these effect-free
    loops (plus a DB call's own duration). Same-process cancel, shutdown and the
    keeper's loss signal are checked every turn. Send/publication retain their
    unconditional durable claim checks; this changes no other Job/heartbeat.
    """

    def __init__(self, worker: Any, job: dict[str, Any]):
        self.worker, self.job = worker, job
        self.lost = worker._claim_lost_event_for(job["id"])
        self.next_remote = 0.0

    def __call__(self) -> None:
        w, job_id = self.worker, self.job["id"]
        w.shutdown_state.checkpoint()
        if w.job_cancelled(job_id):
            raise w.OperationCancelled("Upload stopped")
        if self.lost is not None and self.lost.is_set():
            raise w.JobLeaseLost("Upload claim was lost")
        now = time.monotonic()
        if now >= self.next_remote:
            w.ensure_job_active(job_id, "Upload claim was lost")
            self.next_remote = now + 1.0


@dataclass
class _Version:
    """The exact Archive Version this run verifies and publishes."""

    id: str
    object_key: str
    provider_version_id: str
    size: int
    integrity: str
    expected: provenance.AdmittedSnapshot
    token: str

    def target(self) -> dict[str, Any]:
        return {"archive_version_id": self.id, "object_key": self.object_key,
                "provider_version_id": self.provider_version_id, "cloud_size": self.size}


@dataclass
class _Run:
    job: dict[str, Any]
    source: Path
    pipeline: upload_pipeline._Pipeline
    check_active: Callable[[], None]
    mode: str = "read_back"
    reconcile_only: bool = False
    client: Any = None
    attempt: provenance.UploadAttempt | None = None
    version: _Version | None = None
    # Pre-hash observation: kept unchanged for the whole run.
    generation: int | None = None
    observed: os.stat_result | None = None
    snapshot: UploadSnapshot | None = None
    response: dict[str, Any] | None = None
    provider_proof: dict[str, str] | None = None
    verification: dict[str, Any] | None = None
    verification_started: bool = False
    failure_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class UploadExecution:
    """Interface: run(claimed Job), durable completed/queued/cancelled or error.

    Errors retain the ordinary caller's retry/block handling. A response is
    journaled immediately and independently of that claim; all publication is
    fenced. Each attempt keeps immutable expected evidence and bounded state,
    never buffers a batch of receipts or repeats a send inside a DB retry.
    """

    def __init__(self, worker: Any):
        self.worker = worker

    def run(self, job: dict[str, Any]) -> UploadOutcome:
        with self.worker._job_claim_context(
            job, lost_event=self.worker._claim_lost_event_for(job["id"]),
        ):
            pipeline = upload_pipeline.pipeline_for(self.worker)
            check = _UploadCheckpoint(self.worker, job)
            with upload_pipeline.bind(pipeline, job, check):
                return self._run_claimed(job, pipeline, check)

    # -- orchestration -------------------------------------------------------

    def _run_claimed(self, job: dict[str, Any], pipeline: upload_pipeline._Pipeline,
                     check: Callable[[], None]) -> UploadOutcome:
        w = self.worker
        w.ensure_job_active(job["id"], "Upload stopped")
        waiting = self._automatic_gate(job)
        if waiting is not None:
            return waiting
        w.validate_cloud_vault(job)
        w._require_upload_source_identity(job)
        w._clear_source_volume_problem(job)
        run = _Run(job, w.safe_local_path(job["source_root"], job["path"]), pipeline, check)
        try:
            self._load(run)
            if run.attempt is not None and run.attempt.send_started and not run.attempt.settled:
                self._reconcile(run)
            if run.version is None and run.reconcile_only:
                raise provenance.UploadBlocked("ambiguous_send")
            present = self._observe(run)
            if run.version is None and not present:
                raise w.UploadSourceRemoved("The file is no longer available in the source folder")
            if present:
                waiting = self._hash(run)
                if waiting is not None:
                    return waiting
            if run.version is None:
                if self._unchanged(run):
                    return UploadOutcome(int(job["id"]), "completed", None)
                self._send(run)
            assert run.version is not None
            if run.version.integrity != "verified":
                self._prove(run)
                self._verify(run)
            self._publish(run)
            w.cleanup_superseded_upload(job, keeper_id=run.version.id)
            self._complete(run)
            return UploadOutcome(int(job["id"]), "completed", run.version.id)
        except w.OperationCancelled:
            raise
        except provenance.UploadBlocked as exc:
            w._record_upload_reconciliation_block(job, exc.reason)
            raise
        except UploadProofMismatch:
            w._record_upload_reconciliation_block(job, "proof_mismatch")
            w._record_verification_failure("mismatch")
            raise
        except Exception:
            if run.verification_started:
                w._record_verification_failure(run.failure_reason or "stream")
            raise

    def _db(self, job: dict[str, Any], mutation: Any, *, shared: bool = False) -> Any:
        return self.worker._upload_catalog_unit(job, mutation, shared=shared)

    def _automatic_gate(self, job: dict[str, Any], *, after_hash: bool = False) -> UploadOutcome | None:
        w = self.worker
        if job.get("origin") != "automatic":
            return None
        state = w._automatic_upload_state(job)
        if state == w.UPLOAD_SOURCE_REMOVED:
            if not w._close_removed_upload(job):
                raise w.JobLeaseLost("Upload claim was lost")
            return UploadOutcome(int(job["id"]), "cancelled")
        if state != w.UPLOAD_READY:
            message = "Automatic upload waits for stable content" if after_hash else "Automatic upload waits for enabled protection and stable content"
            if not w.set_job(job["id"], "queued", message):
                raise w.JobLeaseLost("Upload claim was lost")
            return UploadOutcome(int(job["id"]), "queued")
        return None

    # -- what this Job has to do --------------------------------------------

    def _load(self, run: _Run) -> None:
        """Bind the Job to the file's outstanding attempt and find its version.

        An ordinary Job adopts the file's started, unsettled attempt (it keeps
        its own reconcile_only); a version a previous Job materialized but did
        not publish (the replacement in progress) is verified by this Job from
        its own attempt's snapshot, independently of the original Job.
        """
        job = run.job

        def load(catalog: Any) -> tuple[Any, Any, Any]:
            connection = catalog.connection
            detail = connection.execute(
                "SELECT verification_mode, reconcile_only FROM job_uploads WHERE job_id=%s",
                (job["id"],)).fetchone()
            attempt = provenance.load_for_job(connection, job["id"])
            if attempt is None:
                attempt = provenance.outstanding(connection, job["file_id"])
                if attempt is not None:
                    provenance.bind_for_resume(connection, job["id"], attempt, reconcile_only=None)
            version = _linked_version(connection, job, attempt)
            if version is not None:
                catalog.link_job_version(job["id"], version.id)
            return detail, attempt, version

        detail, run.attempt, run.version = self._db(job, load, shared=True)
        if detail is not None:
            run.mode, run.reconcile_only = detail["verification_mode"], bool(detail["reconcile_only"])
        bound = run.attempt.admitted if run.attempt is not None else None
        if bound is not None and not bound.closed:
            run.mode = bound.verification

    def _reconcile(self, run: _Run) -> None:
        """Decide a started, unsettled send from exact S3 reads, never a new PUT."""
        w, job, attempt = self.worker, run.job, run.attempt
        assert attempt is not None
        run.client = run.client or w.s3_client()

        def inspect(catalog: Any) -> tuple[Any, list[str]]:
            connection = catalog.connection
            return (provenance.receipt(connection, attempt.id),
                    provenance.open_multiparts(connection, attempt.id))

        saved, owned = self._db(job, inspect, shared=True)
        if saved is not None:
            try:
                w.validated_s3_proof(saved, version_id=saved["VersionId"], snapshot=attempt.snapshot,
                                     source="upload_response", completion=True)
            except UploadProofMismatch as exc:
                raise provenance.UploadBlocked("proof_mismatch") from exc
        try:
            response = provenance.reconcile(
                run.client, attempt, saved,
                lambda: w.ensure_job_active(job["id"], "Upload reconciliation stopped"),
                owned_multiparts=owned,
                on_multipart_aborted=lambda upload_id: provenance.close_multipart(
                    w.db, attempt.id, upload_id, w.now_iso()))
        except provenance.ProvenAbsent:
            # Complete S3 reads prove the send never landed (ADR-0025): settle
            # the attempt as a no-write; the next send is a new attempt (new
            # token) of the same snapshot (ADR-0036). A reconcile-only Job
            # never starts an upload while automatic protection is paused.
            if run.reconcile_only and w._upload_protection_paused(job):
                raise provenance.UploadBlocked("protection_paused") from None
            self._db(job, lambda catalog: _rearm(catalog, job, attempt, w.now_iso()))
            w._clear_upload_reconciliation_problem(job)
            run.attempt, run.reconcile_only = None, False
            return
        # Reconciliation established the exact result; repairing facts needs
        # no new-send permit.
        run.pipeline.journal_safe(attempt.id)
        self._materialize(run, attempt, response)

    def _materialize(self, run: _Run, attempt: provenance.UploadAttempt,
                     response: dict[str, Any]) -> None:
        """Settle the attempt at its exact VersionId and record the replacement.

        Idempotent: a DB retry, or a later Job, finds the same attempt and
        version. The version is born unverified; nothing here changes Current.
        """
        w, job = self.worker, run.job
        version_id = str(response.get("VersionId") or "")
        if not version_id or version_id == "null" or version_id in attempt.inventory["versions"]:
            raise provenance.UploadBlocked("ambiguous_receipt")

        def publish(catalog: Any) -> str:
            connection = catalog.connection
            stamp = w.now_iso()
            existing = connection.execute(
                "SELECT id FROM archive_versions WHERE upload_attempt_id=%s", (attempt.id,)).fetchone()
            provenance.record_landed(connection, job, attempt, version_id, stamp)
            if existing is not None:
                archive_version_id = str(existing["id"])
            else:
                archive_version_id = catalog.begin_replacement(
                    job["file_id"], object_key=attempt.target["key"],
                    provider_version_id=version_id, size=attempt.snapshot.size,
                    storage_class=response.get("StorageClass") or "STANDARD",
                    created_at=stamp, upload_attempt_id=attempt.id)
            catalog.link_job_version(job["id"], archive_version_id)
            return archive_version_id

        archive_version_id = self._db(job, publish)
        run.version = _Version(archive_version_id, attempt.target["key"], version_id,
                               attempt.snapshot.size, "unverified", attempt.admitted, attempt.token)

    # -- the Local Copy ------------------------------------------------------

    def _observe(self, run: _Run) -> bool:
        """Read (or record) the observation the hash is taken from.

        Its generation is the only token the final publication compares
        against: a newer observation, a rename or another stat always wins.
        """
        w, job = self.worker, run.job
        try:
            observed = run.source.stat(follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return False
        if not stat.S_ISREG(observed.st_mode):
            return False

        def current(connection: Any) -> Any:
            return connection.execute(
                "SELECT generation, local_presence, local_file_type, local_size, local_mtime_ns "
                "FROM files WHERE id=%s", (job["file_id"],)).fetchone()

        def same(row: Any) -> bool:
            return bool(row and row["local_presence"] == "present"
                        and row["local_file_type"] == "regular"
                        and (row["local_size"], row["local_mtime_ns"])
                        == (observed.st_size, observed.st_mtime_ns))

        row = self._db(job, lambda catalog: current(catalog.connection), shared=True)
        if not same(row):
            def observe(catalog: Any) -> Any:
                catalog.observe_local_copy(
                    vault_id=int(job["vault_id"]), path=job["path"], file_type="regular",
                    size=observed.st_size, mtime_ns=observed.st_mtime_ns, observed_at=w.now_iso())
                return current(catalog.connection)
            row = self._db(job, observe)
        run.generation, run.observed = int(row["generation"]), observed
        return True

    def _hash(self, run: _Run) -> UploadOutcome | None:
        w, job = self.worker, run.job
        w.set_job(job["id"], "uploading", message_key="job.hashing_local_file")
        # Admission precedes the very first read/buffer allocation.
        with run.pipeline.prepare.take(run.check_active, 2 * CHUNK_SIZE):
            snapshot, hashed = w.prepare_upload_snapshot(run.source, run.check_active)
        observed = run.observed
        assert observed is not None
        if (hashed.st_size, hashed.st_mtime_ns) != (observed.st_size, observed.st_mtime_ns):
            raise RuntimeError("Local file changed since fingerprinting")
        run.snapshot = snapshot
        w.ensure_job_active(job["id"], "Upload claim was lost")
        if run.version is None:
            return self._automatic_gate(job, after_hash=True)
        return None

    def _unchanged(self, run: _Run) -> bool:
        """The hashed content is already Current: publish the match, send nothing.

        Only Current counts, never an older version with the same digest.
        """
        w, job, snapshot, observed = self.worker, run.job, run.snapshot, run.observed
        assert snapshot is not None and observed is not None
        digest = bytes.fromhex(snapshot.plaintext_sha256)

        def finish(catalog: Any) -> bool:
            connection = catalog.connection
            current = connection.execute(
                """SELECT 1 FROM files f JOIN archive_versions cur ON cur.id=f.current_version_id
                   WHERE f.id=%s AND f.replacing_version_id IS NULL AND f.held_version_id IS NULL
                     AND cur.integrity='verified' AND cur.availability='available'
                     AND cur.plaintext_sha256=%s FOR UPDATE OF f""",
                (job["file_id"], digest)).fetchone()
            if current is None:
                return False
            if not catalog.publish_local_fingerprint(
                    job["file_id"], generation=run.generation, plaintext_sha256=digest,
                    size=observed.st_size, mtime_ns=observed.st_mtime_ns, match_current=True):
                raise RuntimeError("Local file changed since fingerprinting")
            catalog.clear_reconciliation_block(job["file_id"])
            if not w._set_job_in_transaction(connection, job["id"], "completed",
                                             message_key="job.upload_verified",
                                             transferred_bytes=0):
                raise w.JobLeaseLost("Upload completion claim was lost")
            return True

        return bool(self._db(job, finish))

    # -- the send --------------------------------------------------------------

    def _refresh_journal_gate(self, pipeline: upload_pipeline._Pipeline) -> None:
        # Recovery facts bypass nonurgent catalog capacity, just like their
        # original appends. No publication/cleanup/Job authority is granted.
        pending = pipeline.journal_probe_pending()
        if not pending:
            return
        try:
            with owned_connection(self.worker.db()) as connection:
                connection.execute("SET LOCAL lock_timeout='100ms'")
                connection.execute("SET LOCAL statement_timeout='2s'")
                provenance.probe_receipt_journal(connection)
                for attempt_id, _ in pending:
                    attempt = connection.execute("SELECT id FROM upload_attempts WHERE id=%s",
                                                 (attempt_id,)).fetchone()
                    if attempt is None:
                        raise upload_pipeline.UploadJournalHeld("Upload journal attempt unavailable; new sends held")
        except psycopg.Error as exc:
            raise upload_pipeline.UploadJournalHeld("Upload receipt journal unavailable; new sends held") from exc
        # Only AFTER the write probe commits. Reachability is not reconciliation:
        # retain send-started/token/snapshot and all durable per-attempt fences.
        pipeline.journal_probe_committed(pending)

    def _send(self, run: _Run) -> None:
        w, job = self.worker, run.job
        run.client = run.client or w.s3_client()
        provider = provenance.provider_identity(run.client)
        w.set_job(job["id"], "uploading", message_key="job.plain_upload")
        self._refresh_journal_gate(run.pipeline)
        # Capacity exists BEFORE arming send-started and BEFORE the transport
        # materializes its validated buffers.
        assert run.snapshot is not None
        with run.pipeline.sending(run.snapshot, run.check_active):
            attempt, response = self._send_bytes(run, provider)
        run.response = response
        self._materialize(run, attempt, response)

    def _send_bytes(self, run: _Run, provider: str) -> tuple[provenance.UploadAttempt, dict[str, Any]]:
        w, job, client = self.worker, run.job, run.client
        key = w.expected_cloud_key(job["path"], f"vaults/{job['vault_uuid']}")
        pinned = self._db(job, lambda catalog: provenance.target(
            catalog.connection, job, key=key, provider=provider, shared=True), shared=True)
        before = provenance.inventory(client, pinned["bucket"], key)
        if before != provenance.inventory(client, pinned["bucket"], key):
            raise provenance.UploadBlocked("inventory_changed")

        def prepare_send(catalog: Any) -> provenance.UploadAttempt:
            # Recheck after any catalog-capacity/row-lock wait, before arming.
            run.pipeline.require_send_admission()
            attempt = provenance.prepare(
                catalog.connection, job, pinned=pinned, snapshot=run.snapshot,
                requested=run.mode, local_generation=run.generation, before=before,
                timestamp=w.now_iso(), shared=True)
            # Same DB-only commit, before any provider send. A crash rolls back
            # both or leaves a send-started attempt that MUST reconcile.
            return provenance.mark_send_started(catalog.connection, job, attempt, shared=True)

        attempt = self._db(job, prepare_send, shared=True)
        run.attempt = attempt
        journal = provenance.ReceiptJournal(w.db, attempt.id, w.now_iso)
        # Explicit captures: this callback may run on an SDK thread with NONE
        # of the foreground thread's token/lost/cancel/context-local state.
        pipeline, attempt_id = run.pipeline, attempt.id
        journal_lock = threading.Lock()

        def acknowledge(response: dict[str, Any]) -> None:
            with journal_lock:
                fingerprint = "unencodable"  # Never matches a durable fact hash.
                try:
                    _, fingerprint = provenance.receipt_identity(response)
                    journal(response)
                except BaseException:
                    pipeline.journal_failed(attempt_id, fingerprint)
                    raise
                else:
                    pipeline.journal_safe(attempt_id, fingerprint)

        def multipart(event: str, upload_id: str) -> None:
            if event == "created":
                provenance.journal_multipart(w.db, attempt_id, upload_id, w.now_iso())
            else:
                provenance.close_multipart(w.db, attempt_id, upload_id, w.now_iso())

        transfer_job = {**job, "_upload_snapshot": run.snapshot, "_upload_provider": provider,
            "_upload_metadata": {provenance.TOKEN_KEY: attempt.token, **run.snapshot.file_metadata()},
            "_on_upload_acknowledged": acknowledge, "_on_upload_multipart": multipart}
        # A journal elsewhere may have failed while this DB commit waited.
        w.ensure_job_active(job["id"], "Upload claim was lost")
        pipeline.require_send_admission()
        try:
            response = w.upload_s3_file(transfer_job, source=run.source, key=key)
        except BaseException as exc:
            raw = getattr(exc, "upload_response", None)
            if raw is not None:
                acknowledge(raw)  # DB-only retry of a retained late ack, no PUT.
            no_write = getattr(exc, "upload_no_write_operation", None)
            if raw is None and no_write and w.cloud_preflight_gate.is_readiness_error(exc):
                self._db(job, lambda catalog: provenance.record_no_write(
                    catalog.connection, job, attempt, no_write, w.now_iso()))
            raise
        acknowledge(response)
        return attempt, response

    # -- verification ----------------------------------------------------------

    def _head(self, run: _Run) -> dict[str, Any]:
        """HEAD the exact VersionId; it must carry its own attempt's token."""
        w, version = self.worker, run.version
        assert version is not None
        head = w._verification_version_head(run.client, run.job, version.target())
        token = (head.get("Metadata") or {}).get(provenance.TOKEN_KEY)
        if token != version.token:
            raise provenance.UploadBlocked("ambiguous_receipt")
        return head

    def _proof(self, run: _Run, proof: dict[str, Any], *, source: str,
               completion: bool = False) -> dict[str, str] | None:
        version = run.version
        assert version is not None
        run.verification_started = True
        try:
            return self.worker.validated_s3_proof(
                proof, version_id=version.provider_version_id, snapshot=version.expected.snapshot,
                source=source, completion=completion)
        except UploadProofMismatch:
            run.failure_reason = "mismatch"
            self._mismatch(run, None)
            raise

    def _prove(self, run: _Run) -> None:
        run.client = run.client or self.worker.s3_client()
        proof = None
        if run.response is not None:
            proof = self._proof(run, run.response, source="upload_response", completion=True)
        # Always check contradictions, even with a valid response proof.
        head_proof = self._proof(run, self._head(run), source="ordinary_head")
        run.provider_proof = proof or head_proof

    def _verify(self, run: _Run) -> None:
        """Prove the version's digest in its snapshot's Upload Verification Mode."""
        w, job, version = self.worker, run.job, run.version
        assert version is not None
        expected = version.expected
        w.ensure_job_active(job["id"], "Upload stopped")
        w.set_job(job["id"], "verifying", message_key="job.verifying_cloud_copy")
        w.ensure_job_active(job["id"], "Upload claim was lost")
        run.verification_started = True
        fallback = None
        proof = run.provider_proof
        if expected.verification == "s3_checksum" and proof is None:
            try:
                exact = run.client.head_object(
                    Bucket=job["s3_bucket"], Key=version.object_key,
                    VersionId=version.provider_version_id, ChecksumMode="ENABLED")
                proof = self._proof(run, exact, source="exact_head")
                if proof is None:
                    fallback = "checksum_missing"
            except UploadProofMismatch:
                raise
            except Exception as exc:
                if not unsupported_checksum_api(exc):
                    raise
                fallback = "checksum_api_unsupported"
        if expected.verification == "s3_checksum" and proof is not None:
            run.verification = {"method": "s3_checksum", "fallback": None, "egress_bytes": 0}
            return  # Integrity commits with Current in _publish.
        run.verification = {"method": "read_back", "fallback": fallback, "egress_bytes": None}
        # Persist the visible method before incurring the full read-back cost.
        self._db(job, lambda catalog: catalog.record_verification_evidence(
            version.id, **run.verification), shared=True)
        if fallback:
            w.set_job(job["id"], "verifying", message_key="job.verifying_cloud_fallback")
        digest, size = w._stream_plaintext_for_verification(
            job, target=version.target(), source_size=expected.snapshot.size)
        run.verification["egress_bytes"] = size
        try:
            # The post-read HEAD is independent; contradictory evidence blocks.
            self._proof(run, self._head(run), source="ordinary_head")
            if size != expected.snapshot.size:
                run.failure_reason = "truncated"
                raise RuntimeError("Cloud verification stream length did not match the Archive Version")
            if digest != expected.snapshot.plaintext_sha256:
                run.failure_reason = "mismatch"
                w.ensure_job_active(job["id"], "Upload claim was lost")
                self._mismatch(run, digest)
                w._record_upload_reconciliation_block(job, "proof_mismatch")
                raise RuntimeError("Cloud copy digest does not match local file")
        except w.OperationCancelled:
            raise
        except Exception:
            # A consumed read-back stays visible even when Integrity is blocked.
            self._db(job, lambda catalog: catalog.record_verification_evidence(
                version.id, **run.verification), shared=True)
            raise

    def _mismatch(self, run: _Run, digest: str | None) -> None:
        """Record the proven mismatch and end the replacement; Current stays."""
        w, job, version = self.worker, run.job, run.version
        assert version is not None

        def mark(catalog: Any) -> None:
            row = catalog.connection.execute(
                "SELECT replacing_version_id FROM files WHERE id=%s FOR UPDATE",
                (job["file_id"],)).fetchone()
            if row is not None and str(row["replacing_version_id"]) == version.id:
                catalog.abandon_replacement(
                    job["file_id"], archive_version_id=version.id, mismatch=True,
                    mismatch_sha256=digest, checked_at=w.now_iso())
            catalog.block_reconciliation(job["file_id"], reason="proof_mismatch", at=w.now_iso())

        self._db(job, mark)

    # -- publication -----------------------------------------------------------

    def _publish(self, run: _Run) -> None:
        """Make the verified version Current and publish the Local Copy match.

        One claim-fenced unit: the digest is published only on the pre-hash
        generation, so a newer observation keeps its own state (the version
        still becomes Current, unmatched). A lost claim rolls the whole unit
        back; the receipt, the landed attempt and the version stay for the
        next Job to verify again without sending.
        """
        w, job, version = self.worker, run.job, run.version
        assert version is not None
        expected = version.expected.snapshot
        verified_at = w.now_iso()

        def publish(catalog: Any) -> None:
            connection = catalog.connection
            if run.verification is not None:
                catalog.record_verification_evidence(version.id, **run.verification)
            row = connection.execute(
                "SELECT current_version_id, replacing_version_id FROM files WHERE id=%s FOR UPDATE",
                (job["file_id"],)).fetchone()
            if run.snapshot is not None and run.generation is not None and run.observed is not None:
                catalog.publish_local_fingerprint(
                    job["file_id"], generation=run.generation,
                    plaintext_sha256=run.snapshot.plaintext_sha256, size=run.observed.st_size,
                    mtime_ns=run.observed.st_mtime_ns, match_current=True)
            if str(row["replacing_version_id"]) == version.id:
                catalog.promote_replacement(
                    job["file_id"], archive_version_id=version.id,
                    plaintext_sha256=expected.plaintext_sha256, verified_at=verified_at,
                    local_generation=run.generation)
            elif str(row["current_version_id"]) != version.id:
                raise provenance.UploadBlocked("target_superseded")
            catalog.clear_reconciliation_block(job["file_id"])

        self._db(job, publish)
        version.integrity = "verified"

    def _complete(self, run: _Run) -> None:
        w, job = self.worker, run.job
        assert run.version is not None
        progress = int(run.version.expected.snapshot.size)

        def finish(catalog: Any) -> None:
            if not w._set_job_in_transaction(
                    catalog.connection, job["id"], "completed",
                    message_key="job.upload_verified", transferred_bytes=progress):
                raise w.JobLeaseLost("Upload completion claim was lost")

        self._db(job, finish)


def _linked_version(connection: Any, job: dict[str, Any],
                    attempt: provenance.UploadAttempt | None) -> _Version | None:
    """The version this Job must verify/publish, with its expected proof.

    In order: the version its landed attempt produced, the version linked to
    the Job, then the file's replacement in progress left by an earlier Job.
    A version without an upload attempt has no expected proof and is never
    elected: it blocks (``expected_evidence_missing``).
    """
    row = None
    if attempt is not None and attempt.outcome == "landed":
        row = connection.execute(
            "SELECT id FROM archive_versions WHERE upload_attempt_id=%s", (attempt.id,)).fetchone()
        if row is None:
            raise provenance.UploadBlocked("ambiguous_receipt")
    if row is None:
        row = connection.execute(
            """SELECT COALESCE(j.archive_version_id, f.replacing_version_id) AS id
               FROM jobs j JOIN files f ON f.id=j.file_id WHERE j.id=%s""",
            (job["id"],)).fetchone()
        if row is None or row["id"] is None:
            return None
    version = connection.execute(
        """SELECT av.id, av.object_key, av.provider_version_id, av.size, av.integrity,
                  av.availability, a.token,
                  f.current_version_id, f.replacing_version_id
           FROM archive_versions av JOIN files f ON f.id=av.file_id
           LEFT JOIN upload_attempts a ON a.id=av.upload_attempt_id
           WHERE av.id=%s AND av.file_id=%s""", (row["id"], job["file_id"])).fetchone()
    if version is None:
        raise provenance.UploadBlocked("target_unavailable")
    expected = provenance.expected_for_version(connection, str(version["id"]))
    if expected is None or version["token"] is None:
        raise provenance.UploadBlocked("expected_evidence_missing")
    if version["integrity"] == "mismatch":
        raise provenance.UploadBlocked("proof_mismatch")
    if version["availability"] == "missing":
        raise provenance.UploadBlocked("target_unavailable")
    vid = str(version["id"])
    if vid not in {str(version["current_version_id"]), str(version["replacing_version_id"])}:
        raise provenance.UploadBlocked("target_superseded")
    return _Version(vid, version["object_key"], version["provider_version_id"], int(version["size"]),
                    version["integrity"], expected, str(version["token"]))


def _rearm(catalog: Any, job: dict[str, Any], attempt: provenance.UploadAttempt, stamp: Any) -> None:
    """Claim-fenced DB-only unit after a proof of absence; no provider I/O."""
    connection = catalog.connection
    provenance.record_no_write(connection, job, attempt, provenance.ABSENT_OPERATION, stamp)
    # The Job is now this file's ordinary upload of the same snapshot.
    connection.execute("UPDATE job_uploads SET reconcile_only=false WHERE job_id=%s", (job["id"],))
    catalog.clear_reconciliation_block(job["file_id"])
