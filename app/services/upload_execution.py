"""One claimed upload -> one durable outcome, never a replay of provider I/O.

The caller does not orchestrate snapshots, sends, proofs or cleanup. This Module
owns their ordering and the DB-only replay stages. ``worker`` is the existing
storage Adapter (also used by snapshot_cleanup), not a bag of injected callbacks:
its claim/status/source/transport hooks remain the production and test seams.
There is deliberately no import of storage, executor, queue or heartbeat here.
Stage permits are borrowed by the existing claimed foreground threads.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import json
import os
import threading
import time

import psycopg

from . import snapshot_cleanup, upload_pipeline, upload_provenance as provenance
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
class _Attempt:
    job: dict[str, Any]
    source: Path
    client: Any
    intent: dict[str, Any] | None
    target: dict[str, Any] | None
    requested: str
    pipeline: upload_pipeline._Pipeline
    check_active: Callable[[], None]
    snapshot: UploadSnapshot | None = None
    local_digest: str | None = None
    local_stat: os.stat_result | None = None
    version_id: str | None = None
    digest: str = ""
    provider_proof: dict[str, str] | None = None
    verification_started: bool = False
    failure_reason: str | None = None
    verification: dict[str, Any] | None = None
    namespace: snapshot_cleanup.PinnedNamespace | None = None


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
        # The Interface needs the claimed Job, not knowledge of thread-local
        # status helpers. Preserve the existing keeper's loss signal when called
        # by process_job; no new heartbeat/executor is created by this Module.
        with self.worker._job_claim_context(
            job, lost_event=self.worker._claim_lost_event_for(job["id"]),
        ):
            pipeline = upload_pipeline.pipeline_for(self.worker)
            check = _UploadCheckpoint(self.worker, job)
            with upload_pipeline.bind(pipeline, job, check):
                return self._run_claimed(job, pipeline, check)

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
        source = w.safe_local_path(job["source_root"], job["path"])
        local_present = source.is_file()
        intent, target = self._load(job)
        client = None
        catalog_worker = upload_pipeline.catalog_worker(w, job)
        if intent and intent["send_started"]:
            client = w.s3_client()
            snapshot_cleanup.pin_inventory(catalog_worker, job, client)
            w._resume_materialized_upload(job, intent, client)
            # Successful reconciliation established an exact journaled result or
            # definitive no-write. It needs no new-send permit to repair facts.
            pipeline.journal_safe(intent["id"])
            target = self._db(job, lambda catalog: catalog.get_job_target(job["id"]), shared=True)
        linked = target.get("archive_version_id") if target else None
        if job.get("upload_reconcile_only") and not linked:
            raise provenance.UploadBlocked("ambiguous_send")
        if not linked and not local_present:
            raise w.UploadSourceRemoved("The file is no longer available in the source folder")
        if not linked and target and target.get("cloud_only_version_id"):
            raise RuntimeError("Upload held back: the path holds different content than a file kept only in the cloud")
        attempt = _Attempt(job, source, client, intent, target,
            str(intent["requested"] if intent else (target or {}).get("job_upload_verification_requested") or "read_back"),
            pipeline, check)
        try:
            if not linked:
                waiting = self._snapshot(attempt)
                if waiting is not None:
                    return waiting
            # Removed/unstable local-only work must not initialize cloud work.
            # Reconciliation above alone needs a client before the hashing gate.
            if attempt.client is None:
                attempt.client = w.s3_client()
            # Only a freshly inserted, same-attempt namespace can supply the
            # intent inventory. Historical cleanup authority is never rebased.
            attempt.namespace = snapshot_cleanup.pin_upload_namespace(catalog_worker, job, attempt.client)
            if linked:
                self._linked(attempt, str(linked), dict(target))
            else:
                self._reuse_or_send(attempt)
            assert attempt.target is not None
            if attempt.target.get("integrity") != "verified":
                self._verify(attempt)
            self._finish(attempt)
            return UploadOutcome(int(job["id"]), "completed", attempt.version_id)
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
            if attempt.verification_started:
                w._record_verification_failure(attempt.failure_reason or "stream")
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

    def _load(self, job: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        def load(catalog: Any):
            intent = provenance.load_for_job(catalog.connection, job["id"])
            if intent is None:
                intent = provenance.outstanding(catalog.connection, job["vault_file_id"])
                if intent:
                    provenance.bind_for_resume(catalog.connection, job["id"], intent)
            return intent, catalog.get_job_target(job["id"])
        return self._db(job, load, shared=True)

    def _snapshot(self, a: _Attempt) -> UploadOutcome | None:
        w, job = self.worker, a.job
        w.set_job(job["id"], "uploading", message_key="job.hashing_local_file")
        # Admission precedes the very first read/buffer allocation. Snapshot
        # evidence, not payload, survives this scope; its size is format-bounded.
        with a.pipeline.prepare.take(a.check_active, 2 * CHUNK_SIZE):
            a.snapshot, a.local_stat = w.prepare_upload_snapshot(a.source, a.check_active)
        a.local_digest = a.snapshot.plaintext_sha256
        old = (a.target or {}).get("job_upload_snapshot_json")
        if old is not None and old != a.snapshot.as_json():
            raise RuntimeError("Local file changed since fingerprinting")
        w.ensure_job_active(job["id"], "Upload claim was lost")
        return self._automatic_gate(job, after_hash=True)

    def _linked(self, a: _Attempt, version_id: str, target: dict[str, Any]) -> None:
        w = self.worker
        a.target, a.version_id = target, version_id
        a.digest = w._require_linked_upload_digest(target)
        if target.get("integrity") == "mismatch":
            raise RuntimeError("The linked Archive Version already mismatches the Local Copy")
        if target.get("availability") not in {None, "available"}:
            raise RuntimeError("The linked Archive Version is no longer available")
        if not target.get("object_key") or not target.get("provider_version_id"):
            raise RuntimeError("The linked Archive Version cannot be verified safely")
        # Reuse the VERSION's evidence, never today's Local Copy/layout/policy.
        a.snapshot = UploadSnapshot.from_json(target["upload_snapshot_json"]) if target.get("upload_snapshot_json") else None
        a.requested = target.get("upload_verification_requested") or "read_back"
        if a.snapshot is not None and a.snapshot.plaintext_sha256 != a.digest:
            raise RuntimeError("Upload Snapshot digest differs from durable upload digest")
        head = self._head(a)
        if a.snapshot is not None:
            a.provider_proof = self._proof(a, head, source="ordinary_head")

    def _reuse_or_send(self, a: _Attempt) -> None:
        job, w = a.job, self.worker
        assert a.snapshot is not None and a.local_digest is not None
        a.digest = a.local_digest
        provider = provenance.provider_identity(a.client)
        def prepare_digest(catalog: Any):
            catalog.set_upload_snapshot(job["id"], snapshot_json=a.snapshot.as_json())
            file_id = job.get("vault_file_id") or (a.target or {}).get("vault_file_id")
            reusable = catalog.find_reusable_upload_version(
                vault_file_id=str(file_id), plaintext_sha256=a.digest,
                bucket=job["s3_bucket"], provider=provider) if file_id else None
            if reusable is not None and reusable.get("integrity") in {"verified", "unverified"}:
                # Digest, reusable lookup, exact linkage and target are DB-only.
                catalog.link_job_version(job["id"], str(reusable["id"]))
                return catalog.get_job_target(job["id"])
            return None
        reused = self._db(job, prepare_digest, shared=True)
        job["upload_plaintext_sha256"] = a.digest
        if reused is not None:
            self._linked(a, str(reused["archive_version_id"]), reused)
            return
        self._send(a, provider)

    def _refresh_journal_gate(self, pipeline: upload_pipeline._Pipeline) -> None:
        # Recovery facts bypass nonurgent catalog capacity, just like their
        # original appends. No publication/cleanup/Job authority is granted.
        pending = pipeline.journal_probe_pending()
        if not pending:
            return
        try:
            with self.worker.db() as connection:
                connection.execute("SET LOCAL lock_timeout='100ms'")
                connection.execute("SET LOCAL statement_timeout='2s'")
                provenance.probe_receipt_journal(connection)
                for intent_id, _ in pending:
                    intent = connection.execute("SELECT id FROM upload_intents WHERE id=%s", (intent_id,)).fetchone()
                    if intent is None:
                        raise upload_pipeline.UploadJournalHeld("Upload journal intent unavailable; new sends held")
        except psycopg.Error as exc:
            raise upload_pipeline.UploadJournalHeld("Upload receipt journal unavailable; new sends held") from exc
        # Only AFTER the write probe commits. Reachability is not reconciliation:
        # retain send_started/token/snapshot and all durable per-intent fences.
        # Clear only the failures observed before probing, not concurrent late
        # failures. No response payload is retained or declared durably stored.
        pipeline.journal_probe_committed(pending)
        # No S3 observations here: an unrelated cancelled/paused/offline intent
        # must not turn this admission check into unbounded reconciliation I/O.
        # Cancellation still forbids resume, publication and cleanup of its target.

    def _send(self, a: _Attempt, provider: str) -> None:
        w, job = self.worker, a.job
        w.set_job(job["id"], "uploading", message_key="job.plain_upload")
        self._refresh_journal_gate(a.pipeline)
        # Capacity exists BEFORE arming send-started and BEFORE the transport
        # materializes its validated buffers. Waiting cannot manufacture an
        # ambiguous intent. Returning/throwing joins SDK I/O before release.
        with a.pipeline.sending(a.snapshot, a.check_active):
            key, response = self._send_bytes(a, provider)
        # Never retain scarce network capacity while waiting to publish a fact
        # already safely journaled; preparation/other sends may now overlap.
        a.version_id = w._publish_upload_receipt(job, a.intent, response)
        a.target = {"archive_version_id": a.version_id, "object_key": key,
            "provider_version_id": response["VersionId"], "cloud_size": a.snapshot.size,
            "upload_plaintext_sha256": a.digest, "upload_snapshot_json": a.snapshot.as_json(),
            "upload_verification_requested": a.requested,
            "integrity": "unverified", "availability": "available"}
        a.provider_proof = self._proof(a, response, source="upload_response", completion=True)
        head = self._head(a)
        # Always check contradictions, even with a valid response proof/read_back.
        head_proof = self._proof(a, head, source="ordinary_head")
        a.provider_proof = a.provider_proof or head_proof

    def _send_bytes(self, a: _Attempt, provider: str) -> tuple[str, dict[str, Any]]:
        w, job = self.worker, a.job
        key = w.expected_cloud_key(job["path"], job["s3_prefix"])
        fresh = a.namespace.take_for_upload(job, a.client) if a.namespace is not None else None
        if fresh is None:
            pinned = self._db(job, lambda catalog: provenance.target(
                catalog.connection, job, key=key, provider=provider, shared=True), shared=True)
            before = provenance.inventory(a.client, job["s3_bucket"], key)
            if before != provenance.inventory(a.client, job["s3_bucket"], key):
                raise provenance.UploadBlocked("inventory_changed")
        else:
            pinned, before = fresh
        def prepare_send(catalog: Any):
            # Recheck after any catalog-capacity/row-lock wait, before arming
            # an intent. A journal failure elsewhere is not an ambiguous send.
            a.pipeline.require_send_admission()
            intent = provenance.prepare(catalog.connection, job, pinned=pinned,
                snapshot=a.snapshot, requested=a.requested, before=before,
                timestamp=w.now_iso(), shared=True)
            if (intent["snapshot_json"] != a.snapshot.as_json()
                    or intent["inventory_json"] != provenance.canonical(before)):
                raise provenance.UploadBlocked("identity_changed")
            # Same DB-only commit, before any provider send. A crash rolls back
            # both facts or leaves a send-started intent that MUST reconcile.
            provenance.mark_send_started(catalog.connection, job, intent, shared=True)
            return intent
        a.intent = self._db(job, prepare_send, shared=True)
        journal = provenance.ReceiptJournal(w.db, a.intent["id"], w.now_iso)
        # Explicit captures: this callback may run on an SDK thread with NONE
        # of the foreground thread's token/lost/cancel/context-local state.
        pipeline, intent_id = a.pipeline, a.intent["id"]
        journal_lock = threading.Lock()
        def acknowledge(response: dict[str, Any]) -> None:
            with journal_lock:
                fingerprint = "unencodable"  # Never matches a durable fact hash.
                try:
                    _, fingerprint = provenance.receipt_identity(response)
                    journal(response)
                except BaseException:
                    pipeline.journal_failed(intent_id, fingerprint)
                    raise
                else:
                    pipeline.journal_safe(intent_id, fingerprint)
        def multipart(event: str, upload_id: str):
            if event == "created":
                provenance.journal_multipart(w.db, a.intent["id"], upload_id, w.now_iso())
            else:
                provenance.close_multipart(w.db, a.intent["id"], upload_id, w.now_iso())
        transfer_job = {**job, "_upload_snapshot": a.snapshot, "_upload_provider": provider,
            "_upload_metadata": {provenance.TOKEN_KEY: a.intent["token"]},
            "_on_upload_acknowledged": acknowledge, "_on_upload_multipart": multipart}
        # A journal elsewhere may have failed while this DB commit waited.
        # Never send in that case, even if this intent now needs reconciliation.
        w.ensure_job_active(job["id"], "Upload claim was lost")
        pipeline.require_send_admission()
        try:
            response = w.upload_s3_file(transfer_job, source=a.source, key=key)
        except BaseException as exc:
            raw = getattr(exc, "upload_response", None)
            if raw is not None:
                acknowledge(raw)  # DB-only retry of a retained late ack, no PUT.
            no_write = getattr(exc, "upload_no_write_operation", None)
            if raw is None and no_write and w.cloud_preflight_gate.is_readiness_error(exc):
                self._db(job, lambda catalog: provenance.record_no_write(
                    catalog.connection, job, a.intent, no_write, w.now_iso()))
            raise
        acknowledge(response)
        return key, response

    def _head(self, a: _Attempt) -> dict[str, Any]:
        w, job, target = self.worker, a.job, a.target
        head = w._verification_version_head(a.client, job, target)
        token = (head.get("Metadata") or {}).get(provenance.TOKEN_KEY)
        if token:
            with w.db() as connection:
                known = connection.execute("SELECT * FROM upload_intents WHERE archive_version_id=%s AND vault_file_id=%s AND token=%s",
                    (target["archive_version_id"], job["vault_file_id"], token)).fetchone()
            if not known:
                raise provenance.UploadBlocked("ambiguous_receipt")
            scope = json.loads(known["target_json"])
            if (scope["provider"] != provenance.provider_identity(a.client)
                    or scope["bucket"] != job["s3_bucket"] or scope["key"] != target["object_key"]):
                raise provenance.UploadBlocked("identity_changed")
        return head

    def _proof(self, a: _Attempt, proof: dict[str, Any], *, source: str,
               completion: bool = False) -> dict[str, str] | None:
        a.verification_started = True
        try:
            return self.worker.validated_s3_proof(proof,
                version_id=a.target["provider_version_id"], snapshot=a.snapshot,
                source=source, completion=completion)
        except UploadProofMismatch:
            a.failure_reason = "mismatch"
            self._db(a.job, lambda catalog: catalog.mark_version_mismatch(
                a.version_id, plaintext_sha256=a.digest, checked_at=self.worker.now_iso()))
            raise

    def _verify(self, a: _Attempt) -> None:
        w, job, target = self.worker, a.job, a.target
        w.ensure_job_active(job["id"], "Upload stopped")
        w.set_job(job["id"], "verifying", message_key="job.verifying_cloud_copy")
        w.ensure_job_active(job["id"], "Upload claim was lost")
        a.verification_started = True
        fallback = None
        checksum_proven = False
        proof = a.provider_proof
        if a.requested == "s3_checksum" and a.snapshot is not None:
            checksum_proven = proof is not None
            if not checksum_proven:
                try:
                    exact = a.client.head_object(Bucket=job["s3_bucket"], Key=target["object_key"],
                        VersionId=target["provider_version_id"], ChecksumMode="ENABLED")
                    proof = self._proof(a, exact, source="exact_head")
                    checksum_proven = proof is not None
                    if not checksum_proven:
                        fallback = "checksum_missing"
                except Exception as exc:
                    if not unsupported_checksum_api(exc):
                        raise
                    fallback = "checksum_api_unsupported"
        elif a.snapshot is None:
            fallback = "legacy_expected_missing"
        a.verification = dict(method="s3_checksum" if checksum_proven else "read_back",
            fallback_reason=fallback, egress_bytes=0 if checksum_proven else None,
            provider_proof=proof if checksum_proven else None)
        if checksum_proven:
            return  # Metadata and Integrity/local CAS commit together in finish.
        # Persist the visible fallback before incurring full Read-back costs. On
        # success its final egress and Integrity join the local-copy CAS commit.
        self._db(job, lambda catalog: catalog.set_version_verification_method(
            a.version_id, **a.verification), shared=True)
        if fallback:
            w.set_job(job["id"], "verifying", message_key="job.verifying_cloud_fallback")
        source_size = a.local_stat.st_size if a.local_stat is not None else int(job.get("total_bytes") or target.get("cloud_size") or 0)
        digest, size = w._stream_plaintext_for_verification(job, target=target, source_size=source_size)
        a.verification["egress_bytes"] = size
        try:
            # Post-read HEAD remains independent and contradictory evidence blocks.
            final_head = self._head(a)
            if a.snapshot is not None:
                self._proof(a, final_head, source="ordinary_head")
            if target.get("cloud_size") is not None and size != int(target["cloud_size"]):
                a.failure_reason = "truncated"
                raise RuntimeError("Cloud verification stream length did not match the Archive Version")
            if digest != a.digest:
                a.failure_reason = "mismatch"
                w.ensure_job_active(job["id"], "Upload claim was lost")
                self._db(job, lambda catalog: catalog.mark_version_mismatch(
                    a.version_id, plaintext_sha256=a.digest, checked_at=w.now_iso()))
                w._record_upload_reconciliation_block(job, "proof_mismatch")
                raise RuntimeError("Cloud copy digest does not match local file")
        except w.OperationCancelled:
            raise
        except Exception:
            # A fully consumed Read-back cost remains visible even if its final
            # HEAD/digest blocks Integrity. Success folds this write into finish;
            # the exceptional path still records actual egress without new GETs.
            self._db(job, lambda catalog: catalog.set_version_verification_method(
                a.version_id, **a.verification), shared=True)
            raise

    def _finish(self, a: _Attempt) -> None:
        self.worker._complete_verified_upload(a.job, source=a.source,
            version_id=a.version_id, upload_digest=a.digest,
            mark_verified=a.target.get("integrity") != "verified",
            local_digest=a.local_digest, local_stat=a.local_stat,
            verification=a.verification)
