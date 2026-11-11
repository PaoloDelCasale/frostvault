"""Resource accounting checks; claim/locking evidence is in the real-PG suite.

These do not replace Job execution, transport or catalog acceptance tests.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import psycopg

from app.services import upload_pipeline as pipeline
from app.services.shutdown import OperationCancelled, WorkerStopping
from app.services.upload_execution import _UploadCheckpoint
from app.services.upload_snapshot import CHUNK_SIZE, prepare_upload_snapshot


class UploadCapacityTests(unittest.TestCase):
    def snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot"
            path.write_bytes(b"bounded")
            return prepare_upload_snapshot(path, lambda: None)[0]

    def test_counts_are_capped_not_an_increment_of_claimed_workers(self):
        for concurrency in (1, 2, 4, 16):
            with self.subTest(concurrency=concurrency):
                coordinator = pipeline._Pipeline(concurrency)
                self.assertEqual(coordinator.prepare.count, min(concurrency, 2))
                self.assertEqual(coordinator.network.count, min(concurrency, 4))
                self.assertEqual(coordinator.catalog.count, min(concurrency, 4))
                self.assertEqual(coordinator._vault_count, min(concurrency, 2))

    def test_byte_pressure_is_independent_of_count_and_releases_after_fault(self):
        capacity = pipeline._Capacity(4, 10)
        waiting, entered = threading.Event(), threading.Event()
        def wait_for_bytes():
            with capacity.take(waiting.set, 5):
                entered.set()
                raise RuntimeError("network fault")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with capacity.take(lambda: None, 6):
                work = pool.submit(wait_for_bytes)
                self.assertTrue(waiting.wait(2))
                self.assertFalse(entered.is_set(), "Free count does not mean free bytes")
            with self.assertRaisesRegex(RuntimeError, "network fault"):
                work.result(timeout=2)
        with capacity.take(lambda: None, 10):
            self.assertEqual((capacity._count, capacity._bytes), (1, 10))
        self.assertEqual((capacity._count, capacity._bytes, len(capacity._waiting)), (0, 0, 0))

    def test_oversized_layout_has_one_bounded_lane_without_blocking_tiny_sends(self):
        tiny = self.snapshot()
        snapshot = replace(tiny, size=100 * CHUNK_SIZE, part_size=64 * CHUNK_SIZE)
        coordinator = pipeline._Pipeline(4)
        waiting, entered = threading.Event(), threading.Event()
        def another_large():
            with coordinator.sending(snapshot, waiting.set):
                entered.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with coordinator.sending(snapshot, lambda: None):
                self.assertEqual(coordinator._large_buffers._count, 1)
                self.assertEqual(coordinator._large_buffers._bytes, 130 * CHUNK_SIZE)
                work = pool.submit(another_large)
                self.assertTrue(waiting.wait(2))
                self.assertFalse(entered.is_set())
                with coordinator.sending(tiny, lambda: None):
                    self.assertEqual(coordinator.network._count, 2)
                    self.assertEqual(coordinator._buffers._bytes, tiny.size + 2 * CHUNK_SIZE)
            work.result(timeout=2)
        for gate in (coordinator.network, coordinator._buffers, coordinator._large_buffers):
            self.assertEqual((gate._count, gate._bytes), (0, 0))

    def test_wait_and_post_acquisition_faults_release_tickets_and_all_permits(self):
        for failure in (OperationCancelled("cancel"), WorkerStopping("shutdown"), BaseException("crash")):
            with self.subTest(failure=type(failure).__name__):
                coordinator = pipeline._Pipeline(4)
                with self.assertRaises(type(failure)):
                    with coordinator.catalog_unit({"id": 1, "vault_id": 2}, lambda: None):
                        raise failure
                self.assertEqual(coordinator._vaults, {})
                self.assertEqual(coordinator.catalog._count, 0)
                for gate in (coordinator.prepare, coordinator.network, coordinator._buffers, coordinator._large_buffers):
                    # Fail before acquisition AND immediately after acquisition.
                    for checks in ([failure], [None, failure]):
                        with self.assertRaises(type(failure)):
                            with gate.take(Mock(side_effect=checks)):
                                self.fail("Fault must win before entry")
                        self.assertEqual((gate._count, gate._bytes, len(gate._waiting)), (0, 0, 0))

    def test_contended_vault_does_not_occupy_every_global_catalog_slot(self):
        coordinator = pipeline._Pipeline(4)
        waiting = threading.Event()
        def another_in_same_vault():
            with coordinator.catalog_unit({"id": 3, "vault_id": 2}, waiting.set):
                pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            with ExitStack() as held:
                for job_id in (1, 2):
                    held.enter_context(coordinator.catalog_unit({"id": job_id, "vault_id": 2}, lambda: None))
                work = pool.submit(another_in_same_vault)
                self.assertTrue(waiting.wait(2))
                with coordinator.catalog_unit({"id": 4, "vault_id": 9}, lambda: None):
                    self.assertEqual(coordinator.catalog._count, 3)
            work.result(timeout=2)
        self.assertEqual(coordinator._vaults, {})

    def test_journal_fault_is_fail_closed_for_send_not_for_recovery_or_catalog(self):
        coordinator = pipeline._Pipeline(4)
        coordinator.journal_failed("intent-a", "fact-a")
        with self.assertRaises(psycopg.OperationalError):
            with coordinator.sending(self.snapshot(), lambda: None):
                self.fail("A failed journal must stop new sends")
        with coordinator.catalog_unit({"id": 1, "vault_id": 2}, lambda: None):
            pass
        coordinator.journal_safe("another-intent")
        coordinator.journal_safe("intent-a", "another-fact")
        with self.assertRaises(psycopg.OperationalError):
            coordinator.require_send_admission()
        coordinator.journal_safe("intent-a", "fact-a")
        with coordinator.sending(self.snapshot(), lambda: None):
            pass
        self.assertEqual(coordinator.uncertain_journals(), ())
        self.assertEqual((coordinator.network._count, len(coordinator.network._waiting)), (0, 0))

    def test_binding_dies_with_attempt_and_never_gates_another_job(self):
        coordinator = pipeline._Pipeline(4)
        job = {"id": 1, "vault_id": 2}
        with pipeline.bind(coordinator, job, lambda: None):
            with pipeline.catalog_capacity(job):
                self.assertEqual(coordinator.catalog._count, 1)
            with pipeline.catalog_capacity({"id": 3, "vault_id": 2}):
                self.assertEqual(coordinator.catalog._count, 0)
        with pipeline.catalog_capacity(job):
            self.assertEqual(coordinator.catalog._count, 0)

    def test_hash_wait_checkpoints_amortize_remote_checks_not_local_stop_signals(self):
        lost = threading.Event()
        worker = SimpleNamespace(_claim_lost_event_for=lambda job: lost,
            shutdown_state=SimpleNamespace(checkpoint=Mock()), job_cancelled=Mock(return_value=False),
            ensure_job_active=Mock(), OperationCancelled=OperationCancelled, JobLeaseLost=OperationCancelled)
        check = _UploadCheckpoint(worker, {"id": 1})
        with patch("app.services.upload_execution.time.monotonic", side_effect=[10, 10.1, 10.9, 11]):
            for _ in range(4):
                check()
        self.assertEqual(worker.ensure_job_active.call_count, 2)
        worker.job_cancelled.return_value = True
        with self.assertRaises(OperationCancelled):
            check()
        worker.job_cancelled.return_value = False
        lost.set()
        with self.assertRaises(OperationCancelled):
            check()
        self.assertEqual(worker.ensure_job_active.call_count, 2)
