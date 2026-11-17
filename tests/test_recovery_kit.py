"""Pure, database-free kit and custody fingerprint contracts."""
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from app.config import Settings
from app.services import recovery_kit
from app.services.system_bucket import SystemBucket
from app.services.audit_events import redact_sensitive, REDACTED


class BackupRunsConnection:
    """Answers the kit's reads: backups by bucket, no custody or restore rows."""

    def __init__(self, backup_buckets: list[str]):
        self.backup_buckets = backup_buckets

    def execute(self, sql, params=()):
        result = Mock()
        if "FROM system_bucket" in sql:
            result.fetchone.return_value = None
        elif "FROM metadata_backup_runs" in sql:
            self.assert_succeeded_only(sql)
            result.fetchone.return_value = {"?column?": 1} if params[0] in self.backup_buckets else None
        elif "COUNT(*)" in sql:
            result.fetchone.return_value = {"n": 0}
        else:
            result.fetchone.return_value = None
        return result

    @staticmethod
    def assert_succeeded_only(sql):
        assert "status IN ('succeeded', 'verified')" in sql, sql


class RecoveryKitTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(Settings(), archive_master_key=Fernet.generate_key().decode(),
                              vault_s3_bucket="shared-bucket", metadata_backup_bucket="",
                              aws_region="eu-south-1")
        self.bucket = SystemBucket("frostvault-system-abcd1234", "eu-south-1", "created")

    def fingerprint(self, config=None, bucket=None):
        return recovery_kit.kit_fingerprint(config or self.config, bucket=bucket or self.bucket)

    def test_kit_is_independent_of_catalog_and_excludes_all_other_credentials(self):
        kit = json.loads(recovery_kit.build_recovery_kit(self.config, bucket=self.bucket))
        self.assertEqual(set(kit), {"format", "fingerprint", "ARCHIVE_MASTER_KEY", "system_bucket", "region", "instructions"})
        self.assertEqual((kit["format"], kit["system_bucket"], kit["region"]),
                         ("frostvault-recovery-kit-v2", "frostvault-system-abcd1234", "eu-south-1"))
        self.assertNotIn("shared-bucket", json.dumps(kit))
        self.assertTrue(kit["ARCHIVE_MASTER_KEY"] == self.config.archive_master_key)
        self.assertIn("s3api list-object-versions", kit["instructions"])
        self.assertIn("s3api restore-object", kit["instructions"])
        self.assertIn("pg_restore --exit-on-error", kit["instructions"])
        self.assertNotIn("rclone", kit["instructions"])
        self.assertNotIn("sync ", kit["instructions"])

    def test_fingerprint_changes_with_master_key_not_catalog_or_aws_credentials(self):
        current = self.fingerprint()
        changed = replace(self.config, archive_master_key=Fernet.generate_key().decode())
        self.assertNotEqual(current, self.fingerprint(changed))
        self.assertEqual(current, self.fingerprint())
        self.assertEqual(current, self.fingerprint(replace(self.config, vault_s3_bucket="other")))
        self.assertEqual(len(current), 24)

    def test_fingerprint_changes_with_the_system_bucket_and_its_region(self):
        """#734: a new System Bucket asks for custody again."""
        current = self.fingerprint()
        for bucket in (SystemBucket("frostvault-system-zzzz9999", "eu-south-1", "created"),
                       SystemBucket("frostvault-system-abcd1234", "eu-west-1", "created")):
            self.assertNotEqual(current, self.fingerprint(bucket=bucket))

    def test_kit_is_unavailable_until_the_system_bucket_is_ready(self):
        pending = SystemBucket("frostvault-system-abcd1234", "eu-south-1", "created", ready=False)
        for bucket in (None, pending):
            with self.subTest(bucket=bucket), self.assertRaises(recovery_kit.RecoveryKitUnavailable) as caught:
                recovery_kit.build_recovery_kit(self.config, bucket=bucket)
            self.assertIn("System Bucket", str(caught.exception))

    def test_the_configured_bucket_is_named_without_a_database(self):
        configured = replace(self.config, metadata_backup_bucket="my-backups")
        self.assertEqual(recovery_kit.current_bucket(None, configured),
                         SystemBucket("my-backups", "eu-south-1", "configured"))

    def test_key_normalization_matches_metadata_backup_key_handling(self):
        padded = replace(self.config, archive_master_key=" \n" + self.config.archive_master_key + "\n ")
        self.assertEqual(self.fingerprint(padded), self.fingerprint())
        self.assertTrue(json.loads(recovery_kit.build_recovery_kit(padded, bucket=self.bucket))["ARCHIVE_MASTER_KEY"] == self.config.archive_master_key)

    def test_invalid_key_fails_without_exposing_material(self):
        with self.assertRaises(recovery_kit.RecoveryKitUnavailable) as caught:
            recovery_kit.build_recovery_kit(replace(self.config, archive_master_key="invalid-test-material"),
                                            bucket=self.bucket)
        self.assertNotIn("invalid-test-material", str(caught.exception))

    def test_stale_custody_is_rejected_before_any_database_write(self):
        connection = Mock()
        configured = replace(self.config, metadata_backup_bucket="my-backups")
        with self.assertRaises(ValueError):
            recovery_kit.acknowledge_custody(connection, "0" * 24, configured)
        statements = [call.args[0].lstrip().split()[0] for call in connection.execute.call_args_list]
        self.assertEqual(set(statements), {"SELECT"})

    def test_the_kit_waits_for_a_backup_in_the_current_system_bucket(self):
        """S4: never offer a kit naming a bucket that holds no backup yet."""
        configured = replace(self.config, metadata_backup_bucket="my-backups")
        backups: list[str] = []
        connection = BackupRunsConnection(backups)
        status = recovery_kit.kit_status(connection, configured)
        self.assertEqual((status["configured"], status["fingerprint"], status["awaiting_backup"]),
                         (False, None, True))
        self.assertEqual(status["system_bucket"]["name"], "my-backups")
        self.assertIsNone(recovery_kit.kit_bucket(connection, configured))
        with self.assertRaises(recovery_kit.RecoveryKitUnavailable) as caught:
            recovery_kit.build_recovery_kit(configured, bucket=None)
        self.assertIn("keep the previous kit", str(caught.exception))
        self.assertIn("the bucket in VAULT_S3_BUCKET", str(caught.exception))
        # A backup in another bucket (the shared one, or a previous System Bucket) does not count.
        backups.append("shared-bucket")
        self.assertTrue(recovery_kit.kit_status(connection, configured)["awaiting_backup"])
        backups.append("my-backups")
        status = recovery_kit.kit_status(connection, configured)
        self.assertEqual((status["configured"], status["awaiting_backup"]), (True, False))
        self.assertEqual(status["fingerprint"], self.fingerprint(
            configured, SystemBucket("my-backups", "eu-south-1", "configured")))

    def test_no_system_bucket_is_not_awaiting_a_backup(self):
        status = recovery_kit.kit_status(BackupRunsConnection([]), self.config)
        self.assertEqual((status["configured"], status["system_bucket"], status["awaiting_backup"]),
                         (False, None, False))

    def test_the_kit_instructions_point_to_the_previous_kit_for_older_backups(self):
        self.assertIn("the bucket in VAULT_S3_BUCKET", recovery_kit.INSTRUCTIONS)

    def test_key_fields_are_redacted_defensively(self):
        result = redact_sensitive({"ARCHIVE_MASTER_KEY": self.config.archive_master_key, "nested": {"master_key": "value"}})
        self.assertEqual(result, {"ARCHIVE_MASTER_KEY": REDACTED, "nested": {"master_key": REDACTED}})

    def test_reconnect_boundary_rejects_nested_same_device_binds_without_traversal(self):
        from app.services.source_identity import validate_root_mount_boundary, MountIdentityError
        base = "10 1 8:1 /data /sources/photos rw - ext4 /dev/test rw\n"
        nested = base + "11 10 8:1 /other /sources/photos/root/child rw - ext4 /dev/test rw\n"
        with self.assertRaises(MountIdentityError):
            validate_root_mount_boundary("/sources/photos", "/sources/photos/root", text=nested)
        with self.assertRaises(MountIdentityError):
            validate_root_mount_boundary("/sources/photos", "/sources/photos/root/child/album", text=nested)
        validate_root_mount_boundary("/sources/photos", "/sources/photos/sibling", text=nested)
        with self.assertRaises(MountIdentityError):
            validate_root_mount_boundary("/sources/photos", "/outside", text=base)

    def test_watcher_identity_changes_even_when_reconnect_keeps_the_path(self):
        from app.storage import _watch_root_signature
        before = {"source_root": "/sources/photos/root", "root_identity": "inode-a", "reconnect_volume_fingerprint": "mount-a"}
        self.assertNotEqual(_watch_root_signature(before), _watch_root_signature({**before, "root_identity": "inode-b"}))
        self.assertNotEqual(_watch_root_signature(before), _watch_root_signature({**before, "reconnect_volume_fingerprint": "mount-b"}))

    def test_manual_restore_cancels_every_open_job_after_suspending_vaults(self):
        connection = Mock()
        # The schema guard itself is covered on PostgreSQL by tests.test_database;
        # the behaviour on the rebuilt schema by tests.test_vault_reconnect.
        with patch.object(recovery_kit, "require_current_schema"):
            recovery_kit.record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")
        statements = [" ".join(call.args[0].split()) for call in connection.execute.call_args_list]
        vaults = next(i for i, sql in enumerate(statements) if sql.startswith("UPDATE vaults"))
        jobs = [sql for sql in statements if sql.startswith("UPDATE jobs")]
        self.assertEqual(len(jobs), 1)
        # Every nonterminal Job, including a pending cloud-purge consent.
        self.assertIn("status='cancelled'", jobs[0])
        self.assertIn("WHERE status NOT IN ('completed','failed','cancelled')", jobs[0])
        self.assertIn("lifecycle='reconnect_required'", statements[vaults])
        self.assertLess(vaults, statements.index(jobs[0]))
        self.assertFalse(any("cloud_deletion_items" in sql for sql in statements))

    def test_manual_restore_rejects_an_undated_manifest_before_mutating(self):
        connection = Mock()
        with self.assertRaises(ValueError): recovery_kit.record_metadata_restore(connection, "2026-11-02T10:00:00")
        connection.execute.assert_not_called()


if __name__ == "__main__": unittest.main()
