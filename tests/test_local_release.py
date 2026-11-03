"""Pure #426 safety contracts; no database or network is started by these tests."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app import storage
from app.config import Settings
from app.services.operation_policies import (
    LOCAL_RELEASE_PERIODS, OperationPolicy, local_cleanup_is_due,
    local_release_is_confirmed, validate_policy,
)


class LocalReleasePolicyTests(unittest.TestCase):
    def test_only_fixed_opt_in_periods_and_no_global_environment_gate(self):
        self.assertIsNone(OperationPolicy().local_release_after_days)
        self.assertNotIn("allow_local_delete", {field.name for field in fields(Settings)})
        for period in (None, *LOCAL_RELEASE_PERIODS):
            validate_policy(OperationPolicy(local_release_after_days=period))
        for period in (-1, 0, 30, 91, 181, 366, 731, True, 90.0, "90"):
            with self.subTest(period=period), self.assertRaises(ValueError):
                validate_policy(OperationPolicy(local_release_after_days=period))

    def test_every_period_uses_latest_actual_observation_verification_modification_or_recovery(self):
        old = datetime(2015, 1, 1, tzinfo=timezone.utc)
        fresh = datetime(2026, 11, 3, tzinfo=timezone.utc)
        for days in LOCAL_RELEASE_PERIODS:
            for field in ("verified_at", "content_observed_at", "local_mtime_ns", "recovered_at"):
                args = dict(verified_at=old.isoformat(), content_observed_at=old.isoformat(),
                            local_mtime_ns=int(old.timestamp() * 1e9), recovered_at=old.isoformat())
                args[field] = int(fresh.timestamp() * 1e9) if field == "local_mtime_ns" else fresh.isoformat()
                due = fresh + timedelta(days=days)
                with self.subTest(days=days, field=field):
                    self.assertFalse(local_cleanup_is_due(**args, retention_days=days, now=due - timedelta(microseconds=1)))
                    self.assertTrue(local_cleanup_is_due(**args, retention_days=days, now=due))

    def test_cleanup_preflight_blockage_is_not_an_immediate_notification(self):
        with (patch("app.storage._validate_cloud_vault", side_effect=RuntimeError("cloud unavailable")),
              patch("app.storage._report_cloud_preflight") as report):
            with self.assertRaises(RuntimeError):
                storage.validate_cloud_vault({}, notify=False)
            report.assert_not_called()

    def test_absent_untrusted_or_unconfirmed_sources_never_authorize_release(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {
            "source_root": "/test", "enabled": True, "relocation_state": "ready", "decommission_state": "active",
        }
        for allowed, health in ((False, {}), (True, {"cloud": {"last_error": "unavailable"}}),
                               (True, {"source": {"last_error": "replaced"}}),
                               (True, {"audit": {"last_error": "unavailable"}}), (True, {})):
            with (patch("app.services.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=allowed)),
                  patch("app.services.component_health.load_vault_health", return_value=health)):
                self.assertEqual(local_release_is_confirmed(connection, 1), allowed and not health)


class LocalReleaseWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "old-photo.jpg"
        self.path.write_bytes(b"photo")
        os.utime(self.path, (1, 1))
        self.mtime = self.path.stat().st_mtime_ns
        self.digest = hashlib.sha256(b"photo").hexdigest()
        self.target = dict(local_presence="present", local_file_type="regular", integrity="verified", availability="available",
                           matched_archive_version_id="av", archive_version_id="av", latest_archive_version_id="av",
                           local_sha256=self.digest, version_sha256=self.digest, cloud_only_version_id=None,
                           object_key="photos/old-photo.jpg", provider_version_id="exact", cloud_size=5,
                           local_size=5, local_mtime_ns=self.mtime, vault_file_id="file", job_origin="manual",
                           verified_at="2020-01-01T00:00:00+00:00", content_observed_at="2020-01-01T00:00:00+00:00",
                           local_recovered_at=None)
        self.job = dict(id=1, vault_id=1, source_root=self.tmp.name, path=self.path.name, s3_bucket="test", total_bytes=5)
        self.head = MagicMock(return_value={"ContentLength": 5, "VersionId": "exact"})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("ensure_job_active", "set_job", "set_job_progress", "ensure_job_claim_owned_in_transaction", "record_automatic_cleanup_outcome", "validate_cloud_vault"):
            self.stack.enter_context(patch(f"app.storage.{name}"))
        self.stack.enter_context(patch("app.storage.db"))
        self.catalog = self.stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
        self.catalog.get_job_target.side_effect = lambda _: dict(self.target)
        self.confirmed = self.stack.enter_context(patch("app.storage.local_release_is_confirmed", return_value=True))
        self.stack.enter_context(patch("app.storage.get_policy", return_value=OperationPolicy(local_release_after_days=90)))
        self.stack.enter_context(patch("app.storage.s3_client", return_value=SimpleNamespace(head_object=self.head)))

    def test_explicit_manual_release_works_with_automation_off_including_cold_classes(self):
        for storage_class in ("STANDARD", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"):
            with self.subTest(storage_class=storage_class), patch("app.storage.get_policy", return_value=OperationPolicy()):
                self.path.write_bytes(b"photo")
                os.utime(self.path, ns=(self.mtime, self.mtime))
                self.target["storage_class"] = storage_class
                storage.process_free_space(self.job)
                self.assertFalse(self.path.exists())
                self.head.assert_called_with(Bucket="test", Key="photos/old-photo.jpg", VersionId="exact")

    def test_unconfirmed_or_absent_volume_does_not_even_contact_cloud(self):
        self.confirmed.return_value = False
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            storage.process_free_space(self.job)
        self.assertEqual(self.path.read_bytes(), b"photo")
        self.head.assert_not_called()

    def test_mismatch_unavailable_conflict_and_stale_snapshot_preserve_local_content(self):
        for key, value in (("integrity", "mismatch"), ("integrity", "unverified"), ("availability", "unknown"),
                           ("availability", "missing"), ("local_sha256", "different"),
                           ("cloud_only_version_id", "conflict"), ("latest_archive_version_id", "new")):
            original = self.target[key]
            self.target[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                storage.process_free_space(self.job)
            self.target[key] = original
            self.assertEqual(self.path.read_bytes(), b"photo")

    def test_failed_or_wrong_exact_version_head_never_deletes(self):
        for response in ({}, {"ContentLength": 5}, {"VersionId": "other", "ContentLength": 5},
                         {"VersionId": "exact", "ContentLength": 6},
                         {"VersionId": "exact", "ContentLength": 5, "DeleteMarker": True}):
            self.head.return_value = response
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                storage.process_free_space(self.job)
            self.assertTrue(self.path.exists())
        self.head.side_effect = RuntimeError("cloud unavailable")
        with self.assertRaises(RuntimeError):
            storage.process_free_space(self.job)
        self.assertTrue(self.path.exists())

    def test_changed_bytes_with_same_size_and_mtime_are_restored_not_unlinked(self):
        self.path.write_bytes(b"other")
        os.utime(self.path, ns=(self.mtime, self.mtime))
        with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
            storage.process_free_space(self.job)
        self.assertEqual(self.path.read_bytes(), b"other")

    def test_new_recovery_or_import_blocks_already_queued_automatic_job(self):
        self.target["job_origin"] = "automatic"
        now = datetime.now(timezone.utc).isoformat()
        for key in ("local_recovered_at", "content_observed_at", "verified_at"):
            original = self.target[key]
            self.target[key] = now
            with self.subTest(key=key), self.assertRaises(storage.OperationCancelled):
                storage.process_free_space(self.job)
            self.target[key] = original
            self.assertEqual(self.path.stat().st_mtime_ns, self.mtime)
            self.assertTrue(self.path.exists())

    def test_disabling_or_extending_rule_rechecks_queued_jobs(self):
        self.target["job_origin"] = "automatic"
        self.target["verified_at"] = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        for period in (None, 180, 365, 730):
            with patch("app.storage.get_policy", return_value=OperationPolicy(local_release_after_days=period)):
                with self.subTest(period=period), self.assertRaises(storage.OperationCancelled):
                    storage.process_free_space(self.job)
                self.assertTrue(self.path.exists())

    def test_cloud_identity_change_after_head_restores_claim(self):
        self.catalog.get_job_target.side_effect = [dict(self.target), dict(self.target, provider_version_id="new-placement")]
        with self.assertRaisesRegex(RuntimeError, "protection changed"):
            storage.process_free_space(self.job)
        self.assertEqual(self.path.read_bytes(), b"photo")
        self.assertEqual(self.path.stat().st_mtime_ns, self.mtime)

    def test_disabling_rule_after_claim_restores_claim(self):
        self.target["job_origin"] = "automatic"
        with patch("app.storage.get_policy", side_effect=[OperationPolicy(local_release_after_days=90), OperationPolicy()]):
            with self.assertRaises(storage.OperationCancelled):
                storage.process_free_space(self.job)
        self.assertEqual(self.path.read_bytes(), b"photo")

    def test_lost_confirmation_after_claim_restores_original_mtime_and_bytes(self):
        self.confirmed.side_effect = [True, False]
        with self.assertRaises(RuntimeError):
            storage.process_free_space(self.job)
        self.assertEqual(self.path.read_bytes(), b"photo")
        self.assertEqual(self.path.stat().st_mtime_ns, self.mtime)
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])

