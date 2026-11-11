"""Bounded stages through real admission/claim/Job execution and PG16 catalog.

No mocked catalog/claim/locks. The provider seam deliberately holds stages and
injects faults; every assertion of authority observes committed PostgreSQL facts.
TEST_POSTGRES_URL is mandatory, as for the other upload integrity suites.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import psycopg

from app import database, storage
from app.catalog import ArchiveCatalog
from app.main import queue_jobs
from app.services import upload_pipeline as pipeline, upload_provenance as provenance
from app.services.vault_pause import set_paused
from app.services.vault_relocation import enroll_vault_root_identity
from tests import test_upload_provenance_worker as worker_tests
from tests.postgres import PostgresConnection


class UploadPipelinePostgresTests(unittest.TestCase):
    def setUp(self):
        self.worker = worker_tests.UploadProvenanceWorkerTests(methodName="runTest")
        self.addCleanup(self.worker.doCleanups)
        self.worker.setUp()
        self.pipeline = pipeline._Pipeline(4, pipeline._Limits(network_count=1))
        self.enterContext(patch.object(pipeline, "pipeline_for", return_value=self.pipeline))
        self.enterContext(patch.dict(pipeline._registry, {database.settings.database_dsn: self.pipeline}))
        self.addCleanup(storage.shutdown_state.reset)

    def queue(self, path):
        if path == "report.txt":
            return self.worker.queue()
        source = self.worker.source / path
        source.write_bytes(worker_tests.A)
        with PostgresConnection(self.worker.db_path) as connection:
            ArchiveCatalog(connection).observe_local_copy(vault_id=2, path=path,
                file_type="regular", size=source.stat().st_size, mtime_ns=source.stat().st_mtime_ns,
                observed_at=storage.now_iso())
        queued = queue_jobs(path, "upload", 2, 1)
        self.assertEqual(queued["item_count"], 1)
        with PostgresConnection(self.worker.db_path) as connection:
            return connection.execute("SELECT id FROM jobs WHERE path=%s ORDER BY id DESC", (path,)).fetchone()["id"]

    def queue_other_vault(self):
        source = self.worker.root / "other-vault"
        source.mkdir()
        file = source / "other.txt"
        file.write_bytes(worker_tests.A)
        with PostgresConnection(self.worker.db_path) as connection:
            connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (3,'other','Other',%s,'bucket','other')", (str(source),))
            enroll_vault_root_identity(connection, 3, str(source))
            ArchiveCatalog(connection).observe_local_copy(vault_id=3, path="other.txt", file_type="regular",
                size=file.stat().st_size, mtime_ns=file.stat().st_mtime_ns, observed_at=storage.now_iso())
        queue_jobs("other.txt", "upload", 3, 1)
        with PostgresConnection(self.worker.db_path) as connection:
            return connection.execute("SELECT id FROM jobs WHERE vault_id=3").fetchone()["id"]

    def claim(self, job_id):
        with PostgresConnection(self.worker.db_path) as connection:
            claimed = ArchiveCatalog(connection).claim_job(job_id=job_id,
                claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso())
            if claimed is None:
                return None
            return connection.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job_id,)).fetchone()

    def wait_for(self, predicate, message="stage did not become observable"):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if predicate():
                return
            threading.Event().wait(0.02)
        self.fail(message)

    def snapshot_saved(self, job_id):
        return self.worker.row(job_id)["upload_snapshot_json"] is not None

    def assert_no_send_fact(self, job_id):
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertIsNone(provenance.load_for_job(connection, job_id))
        self.assertIsNone(self.worker.row(job_id)["archive_version_id"])

    def assert_empty(self):
        for capacity in (self.pipeline.prepare, self.pipeline.network, self.pipeline.catalog,
                         self.pipeline._buffers, self.pipeline._large_buffers):
            self.assertEqual((capacity._count, capacity._bytes, len(capacity._waiting)), (0, 0, 0))
        self.assertEqual(self.pipeline._vaults, {})

    def held_send(self):
        started, release = threading.Event(), threading.Event()
        def hook(stage, **kwargs):
            if stage == "before_put" and kwargs["key"] == "docs/report.txt":
                started.set()
                if not release.wait(15):
                    raise RuntimeError("test did not release provider")
        self.worker.provider.hook = hook
        self.addCleanup(release.set)
        return started, release

    def test_hash_network_and_publication_overlap_without_preclaim_or_early_completion(self):
        first, second, unclaimed = [self.queue(path) for path in ("report.txt", "second.txt", "third.txt")]
        # Exactly two existing worker slots; the third queued Job is never
        # privately claimed to fill a new pipeline queue.
        first_job, second_job = self.claim(first), self.claim(second)
        started, release = self.held_send()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(storage.process_job, first_job)
            self.assertTrue(started.wait(10))
            b = pool.submit(storage.process_job, second_job)
            try:
                self.wait_for(lambda: self.snapshot_saved(second), "Hashing must overlap another Job's held send")
                self.assert_no_send_fact(second)
                self.assertEqual(self.worker.provider.puts, 0)
                self.assertNotEqual(self.worker.row(first)["status"], "completed")
                with PostgresConnection(self.worker.db_path) as connection:
                    set_paused(connection, vault_id=2, paused=True, user_id=1)
                self.assertIsNone(self.claim(unclaimed), "Pause holds NEW claims, not leased stages")
                self.assertIsNone(self.worker.row(unclaimed)["claim_token"])
                with PostgresConnection(self.worker.db_path) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) n FROM jobs WHERE claim_token IS NOT NULL").fetchone()["n"], 2)
            finally:
                release.set()
            a.result(timeout=15)
            b.result(timeout=15)
        self.worker.assert_keeper(first)
        self.worker.assert_keeper(second)
        self.assertEqual(self.worker.row(unclaimed)["status"], "queued")
        self.assertEqual(self.worker.provider.puts, 2)
        self.assert_empty()

    def test_byte_backpressure_precedes_send_started_even_with_free_network_count(self):
        # Each tiny transfer reserves its payload plus bounded read chunks.
        # Enough bytes for one, even though four network count permits exist.
        self.pipeline = pipeline._Pipeline(4, pipeline._Limits(
            network_count=4, network_bytes=2 * pipeline.CHUNK_SIZE + len(worker_tests.A)))
        with patch.object(pipeline, "pipeline_for", return_value=self.pipeline):
            first, second = self.queue("report.txt"), self.queue("second.txt")
            started, release = self.held_send()
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(self.worker.run_job, first)
                self.assertTrue(started.wait(10))
                b = pool.submit(self.worker.run_job, second)
                try:
                    self.wait_for(lambda: self.snapshot_saved(second))
                    self.assert_no_send_fact(second)
                    self.assertEqual(self.pipeline.network._count, 1)
                    self.assertEqual(self.worker.provider.puts, 0)
                finally:
                    release.set()
                a.result(timeout=15)
                b.result(timeout=15)
            self.worker.assert_keeper(first)
            self.worker.assert_keeper(second)
            self.assert_empty()

    def test_cancel_lease_loss_and_shutdown_while_waiting_cannot_arm_or_publish(self):
        for stop in ("cancel", "lease", "shutdown"):
            with self.subTest(stop=stop):
                first, second = self.queue("report.txt"), self.queue(f"{stop}.txt")
                started, release = self.held_send()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(self.worker.run_job, first)
                    self.assertTrue(started.wait(10))
                    b = pool.submit(self.worker.run_job, second)
                    try:
                        self.wait_for(lambda: self.snapshot_saved(second))
                        if stop == "cancel":
                            self.assertTrue(storage.set_job(second, "cancelled"))
                        elif stop == "lease":
                            with PostgresConnection(self.worker.db_path) as connection:
                                connection.execute("UPDATE jobs SET claim_token='new-owner' WHERE id=%s", (second,))
                        else:
                            storage.shutdown_state.request()
                        b.result(timeout=10)  # Does not need first's permit.
                        self.assert_no_send_fact(second)
                    finally:
                        release.set()
                    a.result(timeout=15)
                self.assertIsNone(self.worker.row(second)["archive_version_id"])
                self.assert_empty()
                storage.shutdown_state.reset()
                # report.txt was already completed in earlier subtests; queue a
                # different content generation, with the old keeper retained.
                self.worker.file.write_bytes(self.worker.file.read_bytes() + b"next")
                with PostgresConnection(self.worker.db_path) as connection:
                    ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="report.txt", file_type="regular",
                        size=self.worker.file.stat().st_size, mtime_ns=self.worker.file.stat().st_mtime_ns,
                        observed_at=storage.now_iso())
                    # No stale claimed Job may block admission of the next
                    # subtest; these are fixture-only cancelled lost leases.
                    connection.execute("UPDATE jobs SET status='cancelled',claim_token=NULL,claim_expires_at=NULL WHERE status NOT IN ('completed','cancelled','failed')")

    def test_receipt_bypasses_saturated_catalog_and_cancel_wins_publication(self):
        job = self.queue("report.txt")
        accepted, allow_ack, acknowledged = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(allow_ack.set)
        def hook(stage, **kwargs):
            if stage == "accepted":
                accepted.set()
                if not allow_ack.wait(15):
                    raise RuntimeError("test did not release ack")
            if stage == "acknowledged":
                acknowledged.set()
        self.worker.provider.hook = hook
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.worker.run_job, job)
            self.assertTrue(accepted.wait(10))
            with ExitStack() as saturated:
                for _ in range(self.pipeline.catalog.count):
                    saturated.enter_context(self.pipeline.catalog.take(lambda: None))
                try:
                    allow_ack.set()
                    self.assertTrue(acknowledged.wait(10), "Journal cannot queue behind catalog capacity")
                    with PostgresConnection(self.worker.db_path) as connection:
                        intent = provenance.load_for_job(connection, job)
                        self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
                    self.assertIsNone(self.worker.row(job)["archive_version_id"])
                    self.assertTrue(storage.set_job(job, "cancelled"), "No Vault lock while awaiting a permit")
                finally:
                    allow_ack.set()
            work.result(timeout=15)
        self.assertEqual(self.worker.row(job)["status"], "cancelled")
        self.assertIsNone(self.worker.row(job)["archive_version_id"])
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertEqual(self.worker.provider.deletes, 0)
        self.assert_empty()

    def test_reachable_journal_with_suspended_uncertain_intent_does_not_fail_other_vault(self):
        first, other = self.queue("report.txt"), self.queue_other_vault()
        with patch.object(provenance, "append_receipt", side_effect=psycopg.OperationalError("journal unavailable")):
            self.worker.run_job(first)
        with PostgresConnection(self.worker.db_path) as connection:
            intent = provenance.load_for_job(connection, first)
            self.assertTrue(intent["send_started"])
            self.assertIsNone(provenance.receipt(connection, intent["id"]))
            connection.execute("UPDATE vaults SET enabled=FALSE WHERE id=2")
        self.assertTrue(pipeline.journal_status()["held"])
        self.worker.run_job(other)
        self.assertFalse(pipeline.journal_status()["held"])
        self.worker.assert_keeper(other)
        self.assertEqual(self.worker.row(other)["retry_count"], 0)
        self.assertEqual(self.worker.provider.puts, 2)
        # Reachability is not evidence that the first provider send never landed.
        with PostgresConnection(self.worker.db_path) as connection:
            after = provenance.load_for_job(connection, first)
            for field in ("token", "target_json", "snapshot_json", "send_started", "settled", "archive_version_id"):
                self.assertEqual(after[field], intent[field], field)
            self.assertIsNone(provenance.receipt(connection, intent["id"]), "Availability is not a recovered receipt")
            connection.execute("UPDATE vaults SET enabled=TRUE WHERE id=2")
            connection.execute("UPDATE jobs SET retry_after=NULL WHERE id=%s", (first,))
        self.worker.run_job(first)
        self.worker.assert_keeper(first)
        self.assertEqual(self.worker.provider.puts, 2, "Same durable intent must reconcile, never send twice")
        self.assert_empty()

    def test_unavailable_journal_holds_without_spending_retries_then_recovers(self):
        first, second = self.queue("report.txt"), self.queue("second.txt")
        with patch.object(provenance, "append_receipt", side_effect=psycopg.OperationalError("journal unavailable")):
            self.worker.run_job(first)
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertTrue(self.pipeline.uncertain_journals())
        from app.services import upload_remediation
        with PostgresConnection(self.worker.db_path) as connection:
            connection.execute("UPDATE jobs SET retry_count=7 WHERE id=%s", (second,))
        # Inject a real journal write outage while the rest of the catalog can
        # still park Jobs. More waits than the transfer budget must not fail it.
        probe = provenance.probe_receipt_journal
        def read_only_journal(connection):
            connection.execute("SET TRANSACTION READ ONLY")
            probe(connection)  # Actual PostgreSQL read-only failure, not a mock verdict.
        with patch.object(provenance, "probe_receipt_journal", side_effect=read_only_journal):
            for _ in range(10):
                self.assertFalse(self.worker.run_job(second))
                held = self.worker.row(second)
                self.assertEqual((held["status"], held["retry_count"]), ("retrying", 7))
                self.assertEqual(held["message_key"], "job.upload_journal_waiting")
                self.assertIsNotNone(held["retry_after"])
                self.assertIsNone(held["claim_token"])
                self.assert_no_send_fact(second)
                with PostgresConnection(self.worker.db_path) as connection:
                    connection.execute("UPDATE jobs SET retry_after=NULL WHERE id=%s", (second,))
            upload_remediation.observe(storage.db)
            with PostgresConnection(self.worker.db_path) as connection:
                self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE event='upload_journal_unavailable'").fetchone()["problem_status"], "open")
                self.assertEqual(connection.execute("SELECT count(*) n FROM notifications WHERE event='job_failed' AND job_id=%s", (second,)).fetchone()["n"], 0)
        self.assertEqual(self.worker.provider.puts, 1)
        self.assert_empty()
        with PostgresConnection(self.worker.db_path) as connection:
            connection.execute("UPDATE jobs SET retry_after=NULL WHERE id IN (%s,%s)", (first, second))
        # Reconciliation bypasses send admission: it locates the first write by
        # its token, journals it and completes without another PUT.
        self.worker.run_job(first)
        self.worker.assert_keeper(first)
        self.assertEqual(self.pipeline.uncertain_journals(), ())
        self.worker.run_job(second)
        self.worker.assert_keeper(second)
        self.assertEqual(self.worker.provider.puts, 2)
        self.assertEqual(self.worker.row(second)["retry_count"], 7)
        upload_remediation.observe(storage.db)
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE event='upload_journal_unavailable'").fetchone()["problem_status"], "resolved")
        self.assert_empty()

    def test_cancelled_uncertain_intent_needs_no_provider_reads_and_never_resurrects(self):
        first, other = self.queue("report.txt"), self.queue_other_vault()
        with patch.object(provenance, "append_receipt", side_effect=psycopg.OperationalError("journal unavailable")):
            self.worker.run_job(first)
            self.assertTrue(storage.set_job(first, "cancelled"))
            with patch.object(provenance, "probe_receipt_journal", side_effect=psycopg.OperationalError("still unavailable")):
                self.worker.run_job(other)
            self.assert_no_send_fact(other)
            self.assertEqual(self.worker.provider.puts, 1)
            self.assertTrue(self.pipeline.uncertain_journals())
        with PostgresConnection(self.worker.db_path) as connection:
            before = provenance.load_for_job(connection, first)
            self.assertIsNone(provenance.receipt(connection, before["id"]))
            connection.execute("UPDATE jobs SET retry_after=NULL WHERE id=%s", (other,))
        # Only the journal fault is gone. Do not query the cancelled namespace:
        # a suspended source or unavailable provider there must not hold others.
        def no_cancelled_reads(stage, **kwargs):
            if kwargs.get("key") == "docs/report.txt":
                raise AssertionError("Unrelated admission cannot reconcile the cancelled intent")
        self.worker.provider.hook = no_cancelled_reads
        self.worker.run_job(other)
        self.worker.assert_keeper(other)
        self.assertEqual(self.worker.row(first)["status"], "cancelled")
        self.assertIsNone(self.worker.row(first)["archive_version_id"])
        with PostgresConnection(self.worker.db_path) as connection:
            after = provenance.load_for_job(connection, first)
            receipt = provenance.receipt(connection, before["id"])
            self.assertIsNone(receipt, "A reachability probe does not invent a lost receipt")
            for field in ("token", "target_json", "snapshot_json", "send_started", "settled", "archive_version_id"):
                self.assertEqual(after[field], before[field], field)
            self.assertEqual(connection.execute("SELECT count(*) n FROM archive_versions WHERE vault_id=2").fetchone()["n"], 0)
            self.assertIsNone(connection.execute("SELECT matched_archive_version_id FROM local_copies WHERE vault_file_id=%s", (self.worker.file_id,)).fetchone()["matched_archive_version_id"])
        self.assertEqual((self.worker.provider.puts, self.worker.provider.deletes, self.worker.provider.aborted), (2, 0, []))
        self.assertEqual(self.pipeline.uncertain_journals(), ())
        self.assert_empty()

    def test_a_preexisting_different_fact_keeps_its_intent_ambiguous_not_other_sends(self):
        first, second = self.queue("report.txt"), self.queue("second.txt")
        transport = storage.upload_s3_file
        append = provenance.append_receipt
        def changed_return(transfer_job, **kwargs):
            raw = transport(transfer_job, **kwargs)  # Normal committed callback.
            return {**raw, "ETag": "a-different-observed-fact"}
        def fail_changed(db, intent_id, raw, stamp):
            if raw.get("ETag") == "a-different-observed-fact":
                raise psycopg.OperationalError("second fact not committed")
            return append(db, intent_id, raw, stamp)
        with patch.object(storage, "upload_s3_file", side_effect=changed_return), \
                patch.object(provenance, "append_receipt", side_effect=fail_changed):
            self.worker.run_job(first)
        with PostgresConnection(self.worker.db_path) as connection:
            intent = provenance.load_for_job(connection, first)
            self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
        self.worker.run_job(second)
        self.worker.assert_keeper(second)
        self.assertEqual(self.worker.provider.puts, 2)
        self.assertEqual(self.pipeline.uncertain_journals(), ())
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertTrue(provenance.load_for_job(connection, first)["send_started"])
            connection.execute("UPDATE jobs SET retry_after=NULL WHERE id=%s", (first,))
        self.worker.run_job(first)
        self.assertEqual(self.worker.provider.puts, 2, "Conflicting facts cannot authorize another PUT")
        self.assert_empty()

    def test_unencodable_ack_is_local_when_the_journal_is_writable(self):
        first, second = self.queue("report.txt"), self.queue("second.txt")
        transport = storage.upload_s3_file
        def malformed_return(transfer_job, **kwargs):
            raw = transport(transfer_job, **kwargs)  # Normal callback committed.
            return {**raw, "ChecksumSHA256": b"not-a-json-provider-field"}
        with patch.object(storage, "upload_s3_file", side_effect=malformed_return):
            self.worker.run_job(first)
        self.worker.run_job(second)
        self.worker.assert_keeper(second)
        self.assertEqual(self.worker.provider.puts, 2)
        self.assertEqual(self.pipeline.uncertain_journals(), ())
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertTrue(provenance.load_for_job(connection, first)["send_started"])
            connection.execute("UPDATE jobs SET retry_after=NULL WHERE id=%s", (first,))
        self.worker.run_job(first)
        self.assertEqual(self.worker.provider.puts, 2)
        self.assert_empty()

    def test_a_durably_committed_unknown_outcome_can_reopen_admission_without_repeating_put(self):
        first, second = self.queue("report.txt"), self.queue("second.txt")
        append = provenance.append_receipt
        def uncertain(*args, **kwargs):
            append(*args, **kwargs)
            raise psycopg.OperationalError("lost commit confirmation")
        with patch.object(provenance, "append_receipt", side_effect=uncertain):
            self.worker.run_job(first)
        self.assertTrue(self.pipeline.uncertain_journals())
        self.worker.run_job(second)
        self.worker.assert_keeper(second)
        self.assertEqual(self.pipeline.uncertain_journals(), ())
        self.assertEqual(self.worker.provider.puts, 2)
        self.assertIsNone(self.worker.row(first)["archive_version_id"])
        self.assert_empty()

    def test_prepare_and_prewrite_faults_return_capacity_for_the_next_job(self):
        for fault in ("hash", "prewrite"):
            with self.subTest(fault=fault):
                failed, next_job = self.queue(f"failed-{fault}.txt"), self.queue(f"next-{fault}.txt")
                if fault == "hash":
                    injection = patch.object(storage, "prepare_upload_snapshot", side_effect=RuntimeError("hash fault"))
                else:
                    injection = patch.object(provenance, "mark_send_started", side_effect=RuntimeError("prewrite fault"))
                before = self.worker.provider.puts
                with injection:
                    self.worker.run_job(failed)
                self.assertEqual(self.worker.provider.puts, before)
                self.assert_no_send_fact(failed)
                self.assert_empty()
                self.worker.run_job(next_job)
                self.worker.assert_keeper(next_job)
                self.assert_empty()

    def test_proof_and_cleanup_faults_never_complete_or_leak_catalog_capacity(self):
        for fault in ("proof", "cleanup"):
            with self.subTest(fault=fault):
                failed, next_job = self.queue(f"failed-{fault}.txt"), self.queue(f"next-{fault}.txt")
                verified = ArchiveCatalog.mark_version_verified
                def abort_proof(catalog, *args, **kwargs):
                    verified(catalog, *args, **kwargs)
                    raise RuntimeError("proof publication aborted")
                acknowledged = False
                def fail_cleanup(stage, **kwargs):
                    nonlocal acknowledged
                    if stage == "acknowledged":
                        acknowledged = True
                    if stage == "list" and acknowledged:
                        raise RuntimeError("cleanup inventory unavailable")
                if fault == "proof":
                    with patch.object(ArchiveCatalog, "mark_version_verified", abort_proof):
                        self.worker.run_job(failed)
                else:
                    self.worker.provider.hook = fail_cleanup
                    try:
                        self.worker.run_job(failed)
                    finally:
                        self.worker.provider.hook = lambda *args, **kwargs: None
                self.assertNotEqual(self.worker.row(failed)["status"], "completed")
                self.assert_empty()
                with PostgresConnection(self.worker.db_path) as connection:
                    intent = provenance.load_for_job(connection, failed)
                    self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
                    version = connection.execute("SELECT integrity FROM archive_versions WHERE id=%s", (intent["archive_version_id"],)).fetchone()
                    self.assertEqual(version["integrity"], "unverified" if fault == "proof" else "verified")
                self.worker.run_job(next_job)
                self.worker.assert_keeper(next_job)
                self.assert_empty()

    def test_slow_vault_catalog_does_not_hold_network_or_another_vault(self):
        other = self.queue_other_vault()
        first = self.queue("report.txt")
        accepted, allow_ack, acknowledged = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(allow_ack.set)
        def hook(stage, **kwargs):
            if stage == "accepted" and not accepted.is_set():
                accepted.set()
                if not allow_ack.wait(15):
                    raise RuntimeError("test did not release ack")
            if stage == "acknowledged":
                acknowledged.set()
        self.worker.provider.hook = hook
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.worker.run_job, first)
            self.assertTrue(accepted.wait(10))
            with ExitStack() as slow_vault:
                for _ in range(self.pipeline._vault_count):
                    slow_vault.enter_context(self.pipeline.catalog_unit({"id": first, "vault_id": 2}, lambda: None))
                try:
                    allow_ack.set()
                    self.assertTrue(acknowledged.wait(10))
                    b = pool.submit(self.worker.run_job, other)
                    b.result(timeout=15)
                    self.worker.assert_keeper(other)
                    self.assertIsNone(self.worker.row(first)["archive_version_id"])
                    self.assertFalse(a.done(), "First publication still awaits its own Vault capacity")
                finally:
                    allow_ack.set()
            a.result(timeout=15)
        self.worker.assert_keeper(first)
        self.assert_empty()

    def test_preparation_capacity_is_taken_before_materialization_and_released_on_loss(self):
        job = self.queue("report.txt")
        prepare = storage.prepare_upload_snapshot
        with patch.object(storage, "prepare_upload_snapshot", wraps=prepare) as hashing:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with ExitStack() as full:
                    for _ in range(self.pipeline.prepare.count):
                        full.enter_context(self.pipeline.prepare.take(lambda: None, 2 * pipeline.CHUNK_SIZE))
                    work = pool.submit(self.worker.run_job, job)
                    self.wait_for(lambda: self.worker.row(job)["message_key"] == "job.hashing_local_file")
                    hashing.assert_not_called()
                    with PostgresConnection(self.worker.db_path) as connection:
                        connection.execute("UPDATE jobs SET claim_token='new-owner' WHERE id=%s", (job,))
                    work.result(timeout=10)
                    hashing.assert_not_called()
                    self.assert_no_send_fact(job)
        self.assertEqual(self.worker.provider.puts, 0)
        self.assert_empty()

    def test_sdk_thread_late_ack_after_cancel_is_journaled_without_ambient_claim(self):
        job = self.queue("report.txt")
        sent, response_ready = threading.Event(), threading.Event()
        self.addCleanup(response_ready.set)
        def transport(transfer_job, *, source, key):
            self.worker.provider.puts += 1
            version = self.worker.provider.add(key, source.read_bytes(), transfer_job["_upload_metadata"])
            raw = self.worker.provider.head_object(Bucket=transfer_job["s3_bucket"], Key=key, VersionId=version)
            sent.set()
            if not response_ready.wait(15):
                raise RuntimeError("test did not release late response")
            def sdk_ack():
                self.assertIsNone(storage._claim_token_for(job), "SDK thread has no publication authority")
                transfer_job["_on_upload_acknowledged"](raw)
            with ThreadPoolExecutor(max_workers=1) as sdk:
                sdk.submit(sdk_ack).result(timeout=10)
            storage.ensure_job_active(job, "cancelled before response")
            return raw
        with patch.object(storage, "upload_s3_file", side_effect=transport), ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.worker.run_job, job)
            self.assertTrue(sent.wait(10))
            try:
                self.assertTrue(storage.set_job(job, "cancelled"))
            finally:
                response_ready.set()
            work.result(timeout=15)
        with PostgresConnection(self.worker.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
        self.assertEqual(self.worker.row(job)["status"], "cancelled")
        self.assertIsNone(self.worker.row(job)["archive_version_id"])
        self.assertEqual(self.worker.provider.puts, 1)
        self.assert_empty()

    def test_interrupted_stack_after_ack_keeps_receipt_and_reconciles_without_put(self):
        class Interrupted(BaseException):
            pass
        def hook(stage, **kwargs):
            if stage == "acknowledged":
                raise Interrupted("process stops before publication")
        self.worker.provider.hook = hook
        job = self.queue("report.txt")
        with self.assertRaises(Interrupted):
            self.worker.run_job(job)
        self.assert_empty()
        with PostgresConnection(self.worker.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
            self.assertTrue(intent["send_started"])
            self.assertIsNone(self.worker.row(job)["archive_version_id"])
            # Fixture models a dead worker's abandoned lease becoming runnable.
            connection.execute("UPDATE jobs SET status='queued',claim_token=NULL,claim_expires_at=NULL WHERE id=%s", (job,))
        self.worker.provider.hook = lambda *args, **kwargs: None
        self.worker.run_job(job)
        self.worker.assert_keeper(job)
        self.assertEqual(self.worker.provider.puts, 1)
        self.assert_empty()

    def test_deferred_commit_abort_returns_permit_before_replaying_only_database_work(self):
        job = self.queue("report.txt")
        with PostgresConnection(self.worker.db_path) as connection:
            connection.execute("CREATE SEQUENCE pipeline_commit_attempt")
            connection.execute("""CREATE FUNCTION pipeline_abort_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF nextval('pipeline_commit_attempt')=1 THEN
                    RAISE EXCEPTION 'deferred publication abort' USING ERRCODE='40001';
                END IF; RETURN NEW; END $$""")
            connection.execute("""CREATE CONSTRAINT TRIGGER pipeline_commit_abort AFTER INSERT ON archive_versions
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pipeline_abort_commit()""")
        self.worker.run_job(job)
        self.worker.assert_keeper(job)
        self.assertEqual(self.worker.provider.puts, 1)
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM archive_versions").fetchone()["n"], 1)
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_receipts").fetchone()["n"], 1)
            self.assertGreaterEqual(connection.execute("SELECT last_value FROM pipeline_commit_attempt").fetchone()["last_value"], 2)
        self.assert_empty()

    def test_transaction_abort_after_ack_replays_only_catalog_and_returns_permits(self):
        job = self.queue("report.txt")
        original = ArchiveCatalog.record_archive_version
        attempts = []
        def abort_first(catalog, *args, **kwargs):
            result = original(catalog, *args, **kwargs)
            attempts.append(1)
            if len(attempts) == 1:
                catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'publication abort' USING ERRCODE='40001'; END $$")
            return result
        with patch.object(ArchiveCatalog, "record_archive_version", abort_first):
            self.worker.run_job(job)
        self.assertEqual(len(attempts), 2)
        self.worker.assert_keeper(job)
        self.assertEqual(self.worker.provider.puts, 1)
        self.assert_empty()
