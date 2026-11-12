"""Upload protocol's DB-only internal seams; real Job/provider cases live in
 test_upload_provenance_worker (mandatory disposable PostgreSQL16 in CI).
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import psycopg

from app.services import upload_provenance as provenance


class StatusCheckpointTests(unittest.TestCase):
    def setUp(self):
        from app import storage
        from app.services.upload_execution import UploadExecution
        self.worker = MagicMock()
        self.worker.OperationCancelled = storage.OperationCancelled
        self.worker.JobLeaseLost = storage.JobLeaseLost
        self.worker.job_cancelled.return_value = False
        self.worker._claim_is_lost.return_value = False
        self.worker.set_job.return_value = True
        self.execution = UploadExecution(self.worker)

    def test_committed_status_cas_is_the_durable_checkpoint(self):
        self.execution._status_checkpoint({'id': 42}, 'verifying', 'job.verifying_cloud_copy')
        self.worker.shutdown_state.checkpoint.assert_called_once()
        self.worker.set_job.assert_called_once_with(42, 'verifying', message_key='job.verifying_cloud_copy')
        self.worker.ensure_job_active.assert_not_called()

    def test_failed_status_cas_stops_before_provider_work(self):
        self.worker.set_job.return_value = False
        with self.assertRaises(self.worker.JobLeaseLost):
            self.execution._status_checkpoint({'id': 42}, 'verifying', 'job.verifying_cloud_copy')
        self.worker.ensure_job_active.assert_not_called()

    def test_local_cancel_or_keeper_loss_prevents_the_status_write(self):
        for cancelled, lost, error in ((True, False, self.worker.OperationCancelled),
                                       (False, True, self.worker.JobLeaseLost)):
            with self.subTest(cancelled=cancelled, lost=lost):
                self.worker.job_cancelled.return_value = cancelled
                self.worker._claim_is_lost.return_value = lost
                with self.assertRaises(error):
                    self.execution._status_checkpoint({'id': 42}, 'verifying', 'job.verifying_cloud_copy')
                self.worker.set_job.assert_not_called()

    def test_shutdown_prevents_the_status_write(self):
        self.worker.shutdown_state.checkpoint.side_effect = self.worker.OperationCancelled('shutdown')
        with self.assertRaises(self.worker.OperationCancelled):
            self.execution._status_checkpoint({'id': 42}, 'verifying', 'job.verifying_cloud_copy')
        self.worker.set_job.assert_not_called()


class ReceiptJournalTests(unittest.TestCase):
    def test_callback_then_return_appends_the_same_committed_fact_once(self):
        journal = provenance.ReceiptJournal(MagicMock(), "intent", lambda: "now")
        with patch.object(provenance, "append_receipt") as append:
            journal({"VersionId": "exact", "ETag": "etag", "ResponseMetadata": {"ignored": True}})
            journal({"ETag": "etag", "VersionId": "exact"})
            append.assert_called_once()
            self.assertEqual(append.call_args.args[2], {"ETag": "etag", "VersionId": "exact"})

    def test_ambiguous_commit_is_retried_on_late_ack_without_claim_or_put(self):
        journal = provenance.ReceiptJournal(MagicMock(), "intent", lambda: "now")
        with patch.object(provenance, "append_receipt", side_effect=[psycopg.OperationalError("commit uncertain"), None]) as append:
            with self.assertRaises(psycopg.OperationalError):
                journal({"VersionId": "exact"})
            journal({"VersionId": "exact"})
            journal({"VersionId": "exact"})
            self.assertEqual(append.call_count, 2)

    def test_a_different_fact_is_never_deduplicated_by_version_id_alone(self):
        journal = provenance.ReceiptJournal(MagicMock(), "intent", lambda: "now")
        with patch.object(provenance, "append_receipt") as append:
            journal({"VersionId": "exact", "ChecksumSHA256": "a"})
            journal({"VersionId": "exact", "ChecksumSHA256": "b"})
            self.assertEqual(append.call_count, 2)


class JournalAdmissionTests(unittest.TestCase):
    def setUp(self):
        from app import storage
        from app.services.upload_execution import UploadExecution
        from app.services.upload_pipeline import _Pipeline
        from app.services.upload_snapshot import prepare_upload_snapshot
        from tests.upload_provenance_provider import VersionedProvider

        provider = VersionedProvider()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "file"
            source.write_bytes(b"frozen content")
            snapshot, _ = prepare_upload_snapshot(source, lambda: None)
        provider.add("old/file", b"frozen content", {provenance.TOKEN_KEY: "intent-token"})
        intent = {"id": "intent", "send_started": True, "cancelled": True,
            "target_json": json.dumps({"provider": provenance.provider_identity(provider),
                "bucket": "bucket", "key": "old/file"}),
            "inventory_json": json.dumps({"versions": [], "markers": []}),
            "snapshot_json": snapshot.as_json(), "token": "intent-token"}
        worker = MagicMock()
        worker.OperationCancelled, worker.JobLeaseLost = storage.OperationCancelled, storage.JobLeaseLost
        connection = worker.db.return_value.__enter__.return_value
        def query(sql, *args):
            result = MagicMock()
            result.fetchone.return_value = None if "upload_receipts" in sql else intent
            result.fetchall.return_value = []
            return result
        connection.execute.side_effect = query
        worker.s3_client.return_value = provider
        coordinator = _Pipeline(4)
        coordinator.journal_failed("intent", "lost-fact")
        self.worker, self.provider, self.coordinator = worker, provider, coordinator
        self.execution = UploadExecution(worker)

    def test_reachable_journal_does_not_wait_for_reconciliation_of_an_active_intent(self):
        query = self.worker.db.return_value.__enter__.return_value.execute.side_effect
        def active_intent(sql, *args):
            result = query(sql, *args)
            if "FROM upload_intents" in sql:
                result.fetchone.return_value = {**result.fetchone.return_value, "cancelled": False}
            return result
        self.worker.db.return_value.__enter__.return_value.execute.side_effect = active_intent
        self.execution._refresh_journal_gate(self.coordinator)
        self.coordinator.require_send_admission()
        self.assertEqual(self.provider.puts, 0)
        self.worker._resume_materialized_upload.assert_not_called()

    def test_cancelled_intent_cannot_force_provider_reads_on_other_sends(self):
        self.worker.s3_client.side_effect = AssertionError("Admission must be DB-only, even for cancelled intents")
        with patch.object(provenance, "append_receipt") as append:
            self.execution._refresh_journal_gate(self.coordinator)
            self.coordinator.require_send_admission()
            append.assert_not_called()
        self.worker._publish_upload_receipt.assert_not_called()
        self.worker._resume_materialized_upload.assert_not_called()
        self.assertEqual((self.provider.puts, self.provider.deletes, self.provider.aborted), (0, 0, []))

    def test_failed_probe_or_commit_keeps_send_admission_closed(self):
        from app.services.upload_pipeline import UploadJournalHeld
        for stage in ("probe", "commit"):
            with self.subTest(stage=stage):
                context = self.worker.db.return_value
                context.__exit__.side_effect = psycopg.OperationalError("unknown commit") if stage == "commit" else None
                with patch.object(provenance, "probe_receipt_journal",
                                  side_effect=psycopg.OperationalError("write unavailable") if stage == "probe" else None):
                    with self.assertRaises(UploadJournalHeld):
                        self.execution._refresh_journal_gate(self.coordinator)
                with self.assertRaises(UploadJournalHeld):
                    self.coordinator.require_send_admission()
        self.worker.s3_client.assert_not_called()

    def test_same_fact_failing_again_during_probe_keeps_the_new_hold(self):
        with patch.object(provenance, "probe_receipt_journal", side_effect=lambda connection:
                          self.coordinator.journal_failed("intent", "lost-fact")):
            self.execution._refresh_journal_gate(self.coordinator)
        with self.assertRaises(psycopg.OperationalError):
            self.coordinator.require_send_admission()

    def test_another_failed_late_fact_during_probe_is_not_cleared(self):
        with patch.object(provenance, "probe_receipt_journal", side_effect=lambda connection:
                          self.coordinator.journal_failed("intent", "different-late-fact")):
            self.execution._refresh_journal_gate(self.coordinator)
        self.assertEqual(self.coordinator.uncertain_journals(), (("intent", "different-late-fact"),))
        with self.assertRaises(psycopg.OperationalError):
            self.coordinator.require_send_admission()


class TargetCapabilityTests(unittest.TestCase):
    def test_target_is_reused_only_inside_the_same_connection_job_and_scope(self):
        job = {"id": 1, "vault_id": 2, "vault_file_id": "file", "path": "file"}
        pinned = {"vault_id": 2, "vault_file_id": "file", "path": "file", "key": "key", "provider": "provider"}
        connection = MagicMock()
        with patch.object(provenance, "target", return_value=pinned) as target:
            with provenance._validated_target(connection, job, key="key", provider="provider", shared=True):
                provenance.require_target(connection, job, pinned, shared=True)
                provenance.require_target(connection, job, pinned, shared=True)
                self.assertEqual(target.call_count, 1)
                provenance.require_target(MagicMock(), job, pinned, shared=True)
                self.assertEqual(target.call_count, 2)
                provenance.require_target(connection, {**job, "id": 3}, pinned, shared=True)
                self.assertEqual(target.call_count, 3)
                with self.assertRaises(provenance.UploadBlocked):
                    provenance.require_target(connection, job, {**pinned, "path": "changed"}, shared=True)
            provenance.require_target(connection, job, pinned, shared=True)
            self.assertEqual(target.call_count, 4, "No capability survives the DB unit")

    def test_share_capability_rejects_an_unaudited_exclusive_nested_check(self):
        job = {"id": 1}
        pinned = {"key": "key", "provider": "provider"}
        connection = MagicMock()
        with patch.object(provenance, "target", return_value=pinned):
            with provenance._validated_target(connection, job, key="key", provider="provider", shared=True):
                with self.assertRaisesRegex(RuntimeError, "upgrade"):
                    provenance.require_target(connection, job, pinned)

    def test_context_is_revoked_on_rollback(self):
        connection, job = MagicMock(), {"id": 1}
        pinned = {"key": "key", "provider": "provider"}
        with patch.object(provenance, "target", return_value=pinned) as target:
            with self.assertRaises(RuntimeError):
                with provenance._validated_target(connection, job, **pinned):
                    raise RuntimeError("rollback")
            provenance.require_target(connection, job, pinned)
            self.assertEqual(target.call_count, 2)
