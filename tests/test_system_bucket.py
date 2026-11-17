"""The System Bucket for metadata backups (#734, ADR-0049).

Seam: ``app.services.system_bucket`` with an injected store and S3 client.
``MemoryStore`` stands in for the ``system_bucket`` row; ``DatabaseStoreTests``
runs the real store against a disposable PostgreSQL database.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import psycopg

from app.config import Settings
from app.services import system_bucket
from app.services.system_bucket import Reservation
from tests.fake_bucket_s3 import FakeBucketS3


class MemoryStore:
    def __init__(self):
        self.row: dict | None = None
        self.events: list[str] = []

    def load(self):
        return None if self.row is None else Reservation(**self.row)

    def reserve(self, name, region):
        self.events.append(f"reserve {name}")
        if self.row is None:
            self.row = {"name": name, "region": region, "ready": False}
        return self.load()

    def rename(self, old, new):
        self.events.append(f"rename {new}")
        if self.row["name"] != old or self.row["ready"]:
            raise system_bucket.ReservationChanged("lost race")
        self.row["name"] = new
        return self.load()

    def mark_ready(self, name):
        self.events.append(f"ready {name}")
        if self.row["name"] != name:
            raise system_bucket.ReservationChanged("lost race")
        self.row["ready"] = True


def settings(**changes):
    return replace(Settings(), **{"aws_region": "eu-south-1", "metadata_backup_bucket": "",
                                  "vault_s3_bucket": "", **changes})


class CreatedSystemBucketTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeBucketS3()
        self.store = MemoryStore()
        self.regions: list[str] = []

    def creations(self):
        return [bucket for operation, bucket in self.s3.calls if operation == "create_bucket"]

    def ensure(self, cfg=None):
        def client_for(region):
            self.regions.append(region)
            return self.s3
        return system_bucket.ensure(cfg or settings(), store=self.store, client_for=client_for)

    def test_creates_one_bucket_with_the_system_configuration(self):
        bucket = self.ensure()
        self.assertRegex(bucket.name, r"^frostvault-system-[a-z0-9]{8}$")
        self.assertEqual((bucket.region, bucket.origin, bucket.ready), ("eu-south-1", "created", True))
        stored = self.s3.buckets[bucket.name]
        self.assertEqual(stored["tags"], [{"Key": "frostvault:role", "Value": "system"}])
        self.assertEqual(stored["lifecycle"], [system_bucket.ABORT_RULE])
        self.assertNotIn("lock", stored)
        self.assertEqual(stored["ownership"], {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
        self.assertEqual(self.creations(), [bucket.name])
        # The name was saved before the bucket existed, and only then marked ready.
        self.assertEqual(self.store.events, [f"reserve {bucket.name}", f"ready {bucket.name}"])

    def test_later_calls_only_verify(self):
        first = self.ensure()
        calls = len(self.s3.mutating_calls())
        self.assertEqual(self.ensure(), first)
        self.assertEqual(len(self.s3.mutating_calls()), calls)

    def test_an_interrupted_creation_resumes_on_the_saved_name(self):
        self.s3.fail["put_bucket_tagging"] = "SlowDown"
        with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
            self.ensure()
        name = self.store.row["name"]
        self.assertIn("s3:PutBucketTagging (SlowDown)", str(caught.exception))
        self.assertIn("retries with the same name", str(caught.exception))
        self.assertFalse(self.store.row["ready"])
        self.assertEqual(self.ensure().name, name)
        self.assertEqual(list(self.s3.buckets), [name])

    def test_a_name_owned_by_another_account_is_replaced_before_creation(self):
        with patch.object(system_bucket.buckets, "random_bucket_name",
                          side_effect=["frostvault-system-taken000", "frostvault-system-free0000"]):
            self.s3.foreign.add("frostvault-system-taken000")
            bucket = self.ensure()
        self.assertEqual(bucket.name, "frostvault-system-free0000")
        self.assertEqual(list(self.s3.buckets), ["frostvault-system-free0000"])
        self.assertNotIn("head_bucket", [operation for operation, _ in self.s3.calls])

    def test_every_generated_name_taken_stops_after_three_attempts(self):
        names = [f"frostvault-system-taken00{index}" for index in range(4)]
        self.s3.foreign.update(names[:3])
        with patch.object(system_bucket.buckets, "random_bucket_name", side_effect=names):
            with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
                self.ensure()
        self.assertIn("Every generated System Bucket name belongs to another AWS account",
                      str(caught.exception))
        self.assertEqual(self.creations(), names[:3])
        self.assertEqual(self.s3.buckets, {})
        # The next attempt starts from a fresh saved name, never a taken one.
        self.assertEqual(self.store.row, {"name": names[3], "region": "eu-south-1", "ready": False})

    def test_create_bucket_access_denied_names_the_permission_and_keeps_the_name(self):
        self.s3.fail["create_bucket"] = "AccessDenied"
        with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
            self.ensure()
        name = self.store.row["name"]
        self.assertEqual(str(caught.exception),
                         f"Creating System Bucket {name} did not finish: S3 refused "
                         "s3:CreateBucket (AccessDenied). FrostVault retries with the same name")
        self.assertNotIn("provider detail", str(caught.exception))
        self.assertEqual(self.store.events, [f"reserve {name}"])
        self.assertFalse(self.store.row["ready"])

    def test_a_denied_head_bucket_never_renames_our_own_bucket(self):
        """An interrupted creation of our bucket resumes on BucketAlreadyOwnedByYou."""
        self.s3.fail["put_bucket_versioning"] = "InternalError"
        with self.assertRaises(system_bucket.SystemBucketUnavailable):
            self.ensure()
        name = self.store.row["name"]
        self.s3.fail["head_bucket"] = "403"
        self.assertEqual(self.ensure().name, name)
        self.assertEqual(self.creations(), [name, name])
        self.assertNotIn("head_bucket", [operation for operation, _ in self.s3.calls])
        self.assertFalse(any(event.startswith("rename") for event in self.store.events))

    def test_a_denied_read_of_a_ready_bucket_is_an_error_not_a_rename(self):
        bucket = self.ensure()
        self.s3.fail["get_bucket_location"] = "AccessDenied"
        with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
            self.ensure()
        self.assertIn("S3 refused s3:GetBucketLocation (AccessDenied)", str(caught.exception))
        self.assertEqual(self.store.row["name"], bucket.name)
        self.assertEqual(self.creations(), [bucket.name])

    def test_a_ready_bucket_deleted_afterwards_is_refused_not_recreated(self):
        bucket = self.ensure()
        del self.s3.buckets[bucket.name]
        with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
            self.ensure()
        self.assertEqual(str(caught.exception),
                         f"System Bucket {bucket.name} failed verification: The bucket does not exist")
        self.assertEqual(self.creations(), [bucket.name])
        self.assertEqual(self.store.row["name"], bucket.name)

    def test_owner_additions_are_accepted_frostvault_essentials_are_not(self):
        bucket = self.ensure()
        stored = self.s3.buckets[bucket.name]
        stored["lifecycle"].append({"ID": "owner-noncurrent", "Status": "Enabled", "Filter": {},
                                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30}})
        stored["tags"] = [{"Key": "cost-center", "Value": "home"}]
        self.assertEqual(self.ensure(), bucket)
        for change, message in (
            (lambda: stored.update(lifecycle=stored["lifecycle"][1:]),
             "FrostVault's lifecycle rules are missing or changed"),
            (lambda: stored.update(lock={"ObjectLockEnabled": "Enabled"}),
             "Object Lock is enabled on a bucket that must not have it"),
            (lambda: stored.update(ownership={"Rules": [{"ObjectOwnership": "ObjectWriter"}]}),
             "Object Ownership is not BucketOwnerEnforced"),
        ):
            saved = {key: value for key, value in stored.items()}
            with self.subTest(message=message):
                change()
                with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
                    self.ensure()
                self.assertIn(message, str(caught.exception))
            stored.clear()
            stored.update(saved)

    def test_verifying_a_ready_bucket_does_not_take_the_creation_lock(self):
        bucket = self.ensure()
        with patch.object(system_bucket, "_ensure_lock") as lock:
            lock.__enter__.side_effect = AssertionError("creation lock taken")
            self.assertEqual(self.ensure(), bucket)

    def test_the_shared_vault_bucket_never_becomes_the_system_bucket(self):
        bucket = self.ensure(settings(vault_s3_bucket="shared-bucket"))
        self.assertRegex(bucket.name, r"^frostvault-system-[a-z0-9]{8}$")
        self.assertNotIn("shared-bucket", [name for _, name in self.s3.calls])

    def test_a_lost_race_never_reports_ready(self):
        self.s3.fail["put_bucket_tagging"] = "SlowDown"
        with self.assertRaises(system_bucket.SystemBucketUnavailable):
            self.ensure()
        # Another process renamed the saved name meanwhile.
        self.store.row["name"] = "frostvault-system-other000"
        with self.assertRaises(system_bucket.ReservationChanged):
            system_bucket._finish_creation(
                self.store, self.s3, Reservation(self.creations()[0], "eu-south-1", False))
        self.assertFalse(self.store.row["ready"])

    def test_the_bucket_keeps_its_region_when_aws_default_region_changes(self):
        bucket = self.ensure()
        self.regions.clear()
        self.assertEqual(self.ensure(settings(aws_region="eu-west-1")), bucket)
        self.assertEqual(self.regions, ["eu-south-1"])

    def test_drift_of_the_created_bucket_is_refused_not_repaired(self):
        bucket = self.ensure()
        self.s3.buckets[bucket.name]["versioning"] = "Suspended"
        with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
            self.ensure()
        self.assertIn(f"System Bucket {bucket.name} failed verification", str(caught.exception))
        self.assertIn("Versioning is suspended", str(caught.exception))
        self.assertEqual(self.s3.buckets[bucket.name]["versioning"], "Suspended")

    def test_location_names_the_saved_bucket_without_s3(self):
        self.assertIsNone(system_bucket.location(settings(), store=self.store))
        self.store.reserve("frostvault-system-abcd1234", "eu-south-1")
        self.assertEqual(system_bucket.location(settings(), store=self.store),
                         system_bucket.SystemBucket("frostvault-system-abcd1234", "eu-south-1",
                                                    "created", ready=False))
        self.assertEqual(self.s3.calls, [])


class ConfiguredSystemBucketTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeBucketS3()
        self.s3.create_bucket(Bucket="my-backups",
                              CreateBucketConfiguration={"LocationConstraint": "eu-south-1"})
        self.s3.put_bucket_versioning(Bucket="my-backups", VersioningConfiguration={"Status": "Enabled"})
        self.s3.put_public_access_block(Bucket="my-backups", PublicAccessBlockConfiguration={
            flag: True for flag in ("BlockPublicAcls", "IgnorePublicAcls",
                                    "BlockPublicPolicy", "RestrictPublicBuckets")})
        self.s3.put_bucket_encryption(Bucket="my-backups", ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        self.s3.calls.clear()
        self.store = MemoryStore()
        self.cfg = settings(metadata_backup_bucket="my-backups")

    def ensure(self):
        return system_bucket.ensure(self.cfg, store=self.store, client_for=lambda region: self.s3)

    def test_a_verified_operator_bucket_is_used_and_never_changed(self):
        self.assertEqual(self.ensure(), system_bucket.SystemBucket("my-backups", "eu-south-1", "configured"))
        self.assertEqual(self.s3.mutating_calls(), [])
        self.assertIsNone(self.store.row)

    def test_each_missing_setting_is_refused_clearly(self):
        stored = self.s3.buckets["my-backups"]
        del stored["sse"]
        stored["versioning"] = "Suspended"
        stored["bpa"]["BlockPublicPolicy"] = False
        with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
            self.ensure()
        message = str(caught.exception)
        self.assertTrue(message.startswith("METADATA_BACKUP_BUCKET refused: "), message)
        for part in ("Versioning is suspended", "Block Public Access is not fully enabled",
                     "not SSE-S3", "unset it to let FrostVault create its own"):
            self.assertIn(part, message)
        self.assertEqual(self.s3.mutating_calls(), [])

    def test_another_region_is_refused(self):
        self.s3.buckets["my-backups"]["region"] = "us-west-2"
        with self.assertRaises(system_bucket.SystemBucketUnavailable) as caught:
            self.ensure()
        self.assertIn("The bucket is in us-west-2, not in eu-south-1", str(caught.exception))

    def test_location_is_the_configured_bucket_even_with_a_created_one(self):
        self.store.reserve("frostvault-system-abcd1234", "eu-south-1")
        self.assertEqual(system_bucket.location(self.cfg, store=self.store),
                         system_bucket.SystemBucket("my-backups", "eu-south-1", "configured"))


class OffHostRequirementTests(unittest.TestCase):
    def test_placeholders_mean_no_configured_bucket(self):
        for value in ("", "  ", "example-bucket", "REPLACE-WITH-BUCKET-NAME"):
            self.assertIsNone(system_bucket.configured_bucket(settings(metadata_backup_bucket=value)))

    def test_configured_bucket_or_credentials_require_off_host_copies(self):
        none = lambda: False  # noqa: E731
        self.assertFalse(system_bucket.off_host_required(settings(vault_s3_bucket=""),
                                                         credentials=none))
        self.assertTrue(system_bucket.off_host_required(
            settings(metadata_backup_bucket="my-backups", vault_s3_bucket=""), credentials=none))
        self.assertTrue(system_bucket.off_host_required(settings(vault_s3_bucket=""),
                                                        credentials=lambda: True))

    def test_the_shared_vault_bucket_still_requires_off_host_copies_until_735(self):
        """An upgrade with VAULT_S3_BUCKET never silently goes local-only."""
        none = lambda: False  # noqa: E731
        self.assertTrue(system_bucket.off_host_required(
            settings(vault_s3_bucket="shared-bucket"), credentials=none))
        for value in ("", "example-bucket", "BUCKET-NAME", "REPLACE-WITH-BUCKET-NAME"):
            with self.subTest(value=value):
                self.assertFalse(system_bucket.off_host_required(
                    settings(vault_s3_bucket=value), credentials=none))

    def test_credentials_from_the_environment_without_probing_aws(self):
        cases = [
            ({"AWS_ACCESS_KEY_ID": "AKIATEST", "AWS_SECRET_ACCESS_KEY": "secret"}, True),
            ({"AWS_ACCESS_KEY_ID": "REPLACE-WITH-ACCESS-KEY",
              "AWS_SECRET_ACCESS_KEY": "REPLACE-WITH-SECRET-KEY"}, False),
            ({"AWS_ACCESS_KEY_ID": "AKIATEST"}, False),
            ({"AWS_PROFILE": "frostvault"}, True),
            ({}, False),
        ]
        names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
                 "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                 "AWS_CONTAINER_CREDENTIALS_FULL_URI")
        for environment, expected in cases:
            with self.subTest(environment=sorted(environment)), \
                    patch.dict("os.environ", {name: "" for name in names}), \
                    patch("boto3.session.Session", side_effect=AssertionError("no probe")):
                for name, value in environment.items():
                    __import__("os").environ[name] = value
                self.assertIs(system_bucket.aws_credentials_configured(), expected)


class MaintenanceTests(unittest.TestCase):
    """``app.storage.ensure_system_bucket_once``: created ahead of the first backup."""

    def run_once(self, *, off_host=True, configured=None, saved=None, ensure_error=None,
                 interval=86400):
        from app import storage

        runtime = replace(Settings(), metadata_backup_interval_seconds=interval)
        with patch.object(storage, "_runtime_settings", return_value=runtime), \
                patch.object(storage.metadata_backup_service, "off_host_backup_configured",
                          return_value=off_host), \
                patch.object(system_bucket, "configured_bucket", return_value=configured), \
                patch.object(system_bucket.DatabaseStore, "load", return_value=saved), \
                patch.object(system_bucket, "ensure", side_effect=ensure_error) as ensure, \
                patch.object(storage, "_report_worker_error") as reported, \
                patch.object(storage, "_note_worker_ok") as healthy:
            storage.ensure_system_bucket_once()
        return ensure, reported, healthy

    def test_creates_only_when_off_host_and_not_ready(self):
        ensure, _, healthy = self.run_once()
        ensure.assert_called_once_with()
        healthy.assert_called_once_with("system_bucket")
        pending = Reservation("frostvault-system-abcd1234", "eu-south-1", False)
        self.run_once(saved=pending)[0].assert_called_once_with()
        for kwargs in ({"off_host": False}, {"configured": "my-backups"}, {"interval": 0},
                       {"saved": Reservation("frostvault-system-abcd1234", "eu-south-1", True)}):
            with self.subTest(**{key: str(value) for key, value in kwargs.items()}):
                self.run_once(**kwargs)[0].assert_not_called()

    def test_a_failure_is_a_worker_problem(self):
        error = system_bucket.SystemBucketUnavailable("Creating System Bucket did not finish")
        _, reported, healthy = self.run_once(ensure_error=error)
        reported.assert_called_once_with("system_bucket", error)
        healthy.assert_not_called()


class DatabaseStoreTests(unittest.TestCase):
    """The ``system_bucket`` row of migration 0008 (PostgreSQL)."""

    def setUp(self):
        from tests.postgres import PostgresConnection
        from tests.test_database import run_alembic

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "system-bucket.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.store = system_bucket.DatabaseStore(lambda: PostgresConnection(str(self.path)))
        self.connect = lambda: PostgresConnection(str(self.path))

    def test_reserve_rename_and_ready(self):
        self.assertIsNone(self.store.load())
        first = self.store.reserve("frostvault-system-aaaa1111", "eu-south-1")
        # A concurrent start keeps the first name.
        self.assertEqual(self.store.reserve("frostvault-system-bbbb2222", "eu-west-1"), first)
        renamed = self.store.rename("frostvault-system-aaaa1111", "frostvault-system-cccc3333")
        self.assertEqual(renamed, Reservation("frostvault-system-cccc3333", "eu-south-1", False))
        self.store.mark_ready("frostvault-system-cccc3333")
        self.assertTrue(self.store.load().ready)
        # A ready bucket is never renamed.
        with self.assertRaises(system_bucket.ReservationChanged):
            self.store.rename("frostvault-system-cccc3333", "frostvault-system-dddd4444")
        self.assertEqual(self.store.load().name, "frostvault-system-cccc3333")
        with self.connect() as connection:
            self.assertEqual(system_bucket.location(settings(), connection=connection),
                             system_bucket.SystemBucket("frostvault-system-cccc3333", "eu-south-1",
                                                        "created"))

    def test_lost_races_are_reported_not_applied(self):
        self.store.reserve("frostvault-system-aaaa1111", "eu-south-1")
        with self.assertRaises(system_bucket.ReservationChanged):
            self.store.rename("frostvault-system-zzzz0000", "frostvault-system-bbbb2222")
        with self.assertRaises(system_bucket.ReservationChanged):
            self.store.mark_ready("frostvault-system-zzzz0000")
        self.assertEqual(self.store.load(),
                         Reservation("frostvault-system-aaaa1111", "eu-south-1", False))
        self.store.mark_ready("frostvault-system-aaaa1111")
        # A second process completing the same name is not a lost race.
        self.store.mark_ready("frostvault-system-aaaa1111")
        with self.assertRaises(system_bucket.ReservationChanged):
            self.store.rename("frostvault-system-aaaa1111", "frostvault-system-bbbb2222")
        self.assertEqual(self.store.load(),
                         Reservation("frostvault-system-aaaa1111", "eu-south-1", True))

    def test_constraints(self):
        for name, region in (("backups", "eu-south-1"), ("frostvault-system-ABCD1234", "eu-south-1"),
                             ("frostvault-system-abcd1234", "")):
            with self.subTest(name=name, region=region), self.assertRaises(psycopg.errors.CheckViolation):
                self.store.reserve(name, region)
        with self.connect() as connection, self.assertRaises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO system_bucket(id, name, region, reserved_at) "
                "VALUES (2, 'frostvault-system-abcd1234', 'eu-south-1', now())")


if __name__ == "__main__":
    unittest.main()
