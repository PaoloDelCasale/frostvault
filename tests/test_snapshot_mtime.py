"""#573: preserve the timestamp of the archived bytes, not today's Local Copy."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import storage
from app.services.upload_snapshot import prepare_upload_snapshot, UploadSnapshot, recovery_mtime_ns


class SnapshotMtimeTests(unittest.TestCase):
    def test_legacy_snapshot_remains_byte_compatible_and_has_no_invented_mtime(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "file"
            source.write_bytes(b"old")
            snapshot, _ = prepare_upload_snapshot(source, lambda: None)
            data = json.loads(snapshot.as_json())
            data.pop("mtime_ns", None)
            legacy = json.dumps(data, separators=(",", ":"))
            restored = UploadSnapshot.from_json(legacy)
            self.assertIsNone(restored.mtime_ns)
            self.assertEqual(restored.as_json(), legacy)
            self.assertEqual(restored.file_metadata(), {})
            self.assertIsNone(recovery_mtime_ns(legacy, {}))
            self.assertIsNone(recovery_mtime_ns(None, {"LastModified": "today"}))

    def test_invalid_mtime_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "file"
            source.write_bytes(b"test")
            snapshot, _ = prepare_upload_snapshot(source, lambda: None)
            data = json.loads(snapshot.as_json())
            for invalid in (None, True, 1.5, "123", 2**63, -(2**63)-1):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    UploadSnapshot.from_json(json.dumps({**data, "mtime_ns": invalid}))
            for invalid in ("", "today", "1.5", " 123", "00123", str(2**63), "9" * 100):
                with self.subTest(metadata=invalid), self.assertRaises(ValueError):
                    recovery_mtime_ns(None, {"frostvault-mtime-ns": invalid})

    def test_timestamp_only_mutation_during_hash_never_freezes_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "file"
            source.write_bytes(b"unchanged bytes")
            calls = 0
            def active():
                nonlocal calls
                calls += 1
                if calls == 2:
                    os.utime(source, ns=(123, 123))
            with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                prepare_upload_snapshot(source, active)

    def test_unsent_legacy_admission_keeps_its_original_immutable_json(self):
        from app.services.upload_execution import UploadExecution
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "file"
            source.write_bytes(b"old")
            snapshot, _ = prepare_upload_snapshot(source, lambda: None)
            data = json.loads(snapshot.as_json())
            data.pop("mtime_ns", None)
            legacy = json.dumps(data, separators=(",", ":"))
            worker = MagicMock()
            worker.job_cancelled.return_value = False
            worker._claim_is_lost.return_value = False
            worker.set_job.return_value = True
            worker.OperationCancelled, worker.JobLeaseLost = storage.OperationCancelled, storage.JobLeaseLost
            worker.prepare_upload_snapshot.side_effect = prepare_upload_snapshot
            execution = UploadExecution(worker)
            attempt = SimpleNamespace(job={"id": 1}, pipeline=MagicMock(), source=source,
                check_active=lambda: None, target={"job_upload_snapshot_json": legacy})
            with patch.object(execution, "_automatic_gate", return_value=None):
                execution._snapshot(attempt)
            self.assertEqual(attempt.snapshot.as_json(), legacy)
            worker.set_job.assert_called_once_with(1, 'uploading', message_key='job.hashing_local_file')
            # New admissions freeze the original timestamp as well as bytes.
            attempt.target = {"job_upload_snapshot_json": snapshot.as_json()}
            os.utime(source, ns=(123, 123))
            with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                execution._snapshot(attempt)

    def test_hashing_persists_nanoseconds_in_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "photo"
            source.write_bytes(b"photo")
            original = 1_597_614_120_123_456_789
            os.utime(source, ns=(original, original))
            snapshot, _ = prepare_upload_snapshot(source, lambda: None)
            self.assertEqual(json.loads(snapshot.as_json()).get("mtime_ns"), original)
            self.assertEqual(UploadSnapshot.from_json(snapshot.as_json()).mtime_ns, original)

    def recover(self, *, snapshot=True, metadata=None, expected=1_597_614_120_123_456_789):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            source = Path(root) / "archived"
            source.write_bytes(b"photo")
            os.utime(source, ns=(expected, expected))
            evidence, _ = prepare_upload_snapshot(source, lambda: None)
            destination = Path(root) / "recovered"
            catalog = MagicMock()
            catalog.get_job_target.return_value = {
                "integrity": "verified", "availability": "available", "provider_version_id": "exact",
                "object_key": "vault/photo", "version_sha256": evidence.plaintext_sha256,
                "archive_version_id": "av", "local_presence": "missing", "cloud_size": 5,
                "upload_snapshot_json": evidence.as_json() if snapshot else None,
                # Must never restore the changed/missing Local Copy's timestamp.
                "local_mtime_ns": 1,
            }
            client = MagicMock()
            client.head_object.return_value = {"VersionId": "exact", "Metadata": metadata or {}}
            def download(**kwargs):
                Path(kwargs["Filename"]).write_bytes(b"photo")
                kwargs["Callback"](5)
            client.download_file.side_effect = download
            stack.enter_context(patch.object(storage, "ArchiveCatalog", return_value=catalog))
            for name in ("db", "ensure_job_active", "validate_cloud_vault", "ensure_job_claim_owned_in_transaction",
                         "set_job", "set_job_progress", "_ThrottledByteProgress"):
                stack.enter_context(patch.object(storage, name))
            stack.enter_context(patch.object(storage, "s3_client", return_value=client))
            stack.enter_context(patch.object(storage.local_observation, "remember"))
            storage.process_recover({"id": 1, "vault_id": 2, "source_root": root,
                "path": "recovered", "s3_bucket": "bucket", "total_bytes": 5, "status": "queued"})
            self.assertEqual(destination.read_bytes(), b"photo")
            self.assertEqual(destination.stat().st_mtime_ns, expected)
            observation = catalog.observe_local_copy.call_args.kwargs
            self.assertEqual(observation["mtime_ns"], expected)
            self.assertIsNotNone(observation["recovered_at"])
            client.head_object.assert_called_once_with(Bucket="bucket", Key="vault/photo", VersionId="exact")
            self.assertEqual(client.download_file.call_args.kwargs["ExtraArgs"], {"VersionId": "exact"})

    def test_real_download_and_publish_restore_snapshot_mtime(self):
        self.recover()

    def test_catalogless_version_restores_exact_s3_metadata_mtime(self):
        self.recover(snapshot=False, metadata={"frostvault-mtime-ns": "1597614120123456789"})

    def test_epoch_and_pre_epoch_mtimes_are_not_treated_as_missing(self):
        for original in (0, -1_000_000_000):
            with self.subTest(original=original):
                self.recover(expected=original)

    def test_catalog_snapshot_wins_over_different_provider_metadata(self):
        self.recover(metadata={"frostvault-mtime-ns": "1"})


if __name__ == "__main__":
    unittest.main()
