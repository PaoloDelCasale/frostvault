"""Upload protocol's DB-only internal seams; real Job/provider cases live in
 test_upload_provenance_worker (mandatory disposable PostgreSQL16 in CI).
"""
import unittest
from unittest.mock import MagicMock, patch

import psycopg

from app.services import upload_provenance as provenance


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
