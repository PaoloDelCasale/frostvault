"""Pure, database-free kit and custody fingerprint contracts."""
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from cryptography.fernet import Fernet

from app.config import Settings
from app.services import recovery_kit
from app.services.audit_events import redact_sensitive, REDACTED


class RecoveryKitTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(Settings(), archive_master_key=Fernet.generate_key().decode(),
                              vault_s3_bucket="test-bucket", aws_region="eu-south-1")

    def test_kit_is_independent_of_catalog_and_excludes_all_other_credentials(self):
        kit = json.loads(recovery_kit.build_recovery_kit(self.config))
        self.assertEqual(set(kit), {"format", "fingerprint", "ARCHIVE_MASTER_KEY", "bucket", "region", "instructions"})
        self.assertTrue(kit["ARCHIVE_MASTER_KEY"] == self.config.archive_master_key)
        self.assertIn("s3api list-object-versions", kit["instructions"])
        self.assertIn("s3api restore-object", kit["instructions"])
        self.assertIn("pg_restore --exit-on-error", kit["instructions"])
        self.assertNotIn("rclone", kit["instructions"])
        self.assertNotIn("sync ", kit["instructions"])

    def test_fingerprint_changes_with_master_key_not_catalog_or_aws_credentials(self):
        current = recovery_kit.kit_fingerprint(self.config)
        changed = replace(self.config, archive_master_key=Fernet.generate_key().decode())
        self.assertNotEqual(current, recovery_kit.kit_fingerprint(changed))
        self.assertEqual(current, recovery_kit.kit_fingerprint(self.config))
        self.assertEqual(len(current), 24)

    def test_invalid_key_fails_without_exposing_material(self):
        with self.assertRaises(recovery_kit.RecoveryKitUnavailable) as caught:
            recovery_kit.build_recovery_kit(replace(self.config, archive_master_key="invalid-test-material"))
        self.assertNotIn("invalid-test-material", str(caught.exception))

    def test_stale_custody_is_rejected_before_any_database_write(self):
        connection = Mock()
        with self.assertRaises(ValueError):
            recovery_kit.acknowledge_custody(connection, "0" * 24, self.config)
        connection.execute.assert_not_called()

    def test_key_fields_are_redacted_defensively(self):
        result = redact_sensitive({"ARCHIVE_MASTER_KEY": self.config.archive_master_key, "nested": {"master_key": "value"}})
        self.assertEqual(result, {"ARCHIVE_MASTER_KEY": REDACTED, "nested": {"master_key": REDACTED}})

    def test_manual_restore_rejects_an_undated_manifest_before_mutating(self):
        connection = Mock()
        with self.assertRaises(ValueError): recovery_kit.record_metadata_restore(connection, "2026-11-02T10:00:00")
        connection.execute.assert_not_called()


if __name__ == "__main__": unittest.main()
