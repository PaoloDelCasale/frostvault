"""#573: preserve the timestamp of the archived bytes, not today's Local Copy."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from app import storage
from app.services.upload_snapshot import prepare_upload_snapshot, UploadSnapshot


class SnapshotMtimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
