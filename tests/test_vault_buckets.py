"""The dedicated Vault Bucket of every new Vault (#735, ADR-0049).

No AWS: ``FakeBucketS3`` answers like S3 for the bucket configuration calls.
"""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import EndpointConnectionError

from app.config import Settings
from app.services import bucket_provisioning, lifecycle_policies, source_layout, vault_buckets
from app.services import vault_deletion
from app.services import vaults as vaults_service
from app.services.lifecycle_profiles import LifecycleProfile, LifecycleTransition, profile_to_json
from app.services.s3_lifecycle_rules import vault_bucket_lifecycle_rules
from app.services.vault_paths import object_prefix
from tests.fake_bucket_s3 import FakeBucketS3, fake_vault_buckets
from tests.owner_fixtures import insert_owner
from tests.test_database import run_alembic

UUID = "2f1c7e36-0000-4000-8000-000000000001"
DEEP = LifecycleProfile(transitions=(LifecycleTransition(days=0, storage_class="DEEP_ARCHIVE"),))


class NamingTests(unittest.TestCase):
    def test_slug_is_lowercase_ascii_and_at_most_forty_characters(self) -> None:
        cases = {
            "Photos": "photos",
            "Foto di Famiglia 2024": "foto-di-famiglia-2024",
            "Città Ñandú": "citta-nandu",
            "  --Docs__Archive!!  ": "docs-archive",
            "日本": "vault",
            "": "vault",
            "System": "system-vault",
            "x" * 60: "x" * 40,
            "a" * 39 + " b": "a" * 39,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(vault_buckets.slug(name), expected)

    def test_bucket_names_are_valid_unique_and_never_the_system_bucket(self) -> None:
        pattern = re.compile(r"^frostvault-[a-z0-9]+(-[a-z0-9]+)*-[a-z0-9]{8}$")
        for name in ("Photos", "x" * 120, "System", "Ünïcødé ✓ name", "日本"):
            with self.subTest(name=name):
                bucket = vault_buckets.new_bucket_name(name)
                self.assertRegex(bucket, pattern)
                self.assertLessEqual(len(bucket), 63)
                self.assertNotRegex(bucket, r"^frostvault-system-[a-z0-9]{8}$")
        self.assertNotEqual(vault_buckets.new_bucket_name("Docs"), vault_buckets.new_bucket_name("Docs"))

    def test_tags_name_the_vault_with_characters_aws_accepts(self) -> None:
        tags = vault_buckets.tags(UUID, "Foto & Video (2024) ✓")
        self.assertEqual(tags, {"frostvault:vault": UUID, "frostvault:name": "Foto _ Video _2024_ _"})
        self.assertEqual(len(vault_buckets.tag_name("y" * 300)), 256)


class LifecycleTests(unittest.TestCase):
    def test_the_lifecycle_is_the_plan_unfiltered_plus_the_multipart_abort(self) -> None:
        rules = vault_bucket_lifecycle_rules(UUID, DEEP)
        self.assertEqual(rules[0], {
            "ID": f"frostvault-plan-{UUID}", "Status": "Enabled", "Filter": {"Prefix": ""},
            "Transitions": [{"Days": 0, "StorageClass": "DEEP_ARCHIVE"}]})
        self.assertEqual(rules[1]["AbortIncompleteMultipartUpload"], {"DaysAfterInitiation": 7})
        self.assertEqual(rules[1]["Filter"], {"Prefix": ""})
        self.assertEqual(len(vault_bucket_lifecycle_rules(UUID, LifecycleProfile())), 1)

    def test_every_lifecycle_keeps_objects_under_128_kib_in_standard(self) -> None:
        s3 = FakeBucketS3()
        spec = vault_buckets.spec(bucket="frostvault-photos-ab12cd34", region="eu-south-1",
                                  vault_uuid=UUID, vault_name="Photos", profile=DEEP,
                                  retention_lock_days=None)
        self.assertEqual(spec.transition_minimum_object_size, "all_storage_classes_128K")
        bucket_provisioning.create_bucket(s3, spec)
        bucket_provisioning.configure_bucket(s3, spec)
        self.assertEqual(s3.buckets[spec.name]["lifecycle_minimum_size"], "all_storage_classes_128K")
        self.assertTrue(bucket_provisioning.verify_bucket(s3, spec).ok)
        s3.buckets[spec.name]["lifecycle_minimum_size"] = "varies_by_storage_class"
        check = bucket_provisioning.lifecycle_check(s3, spec)
        self.assertFalse(check.ok)
        self.assertIn("minimum transition size", check.message)
        self.assertFalse(bucket_provisioning.verify_bucket(s3, spec).ok)

    def test_an_extra_rule_fails_the_exact_lifecycle(self) -> None:
        s3 = FakeBucketS3()
        spec = vault_buckets.spec(bucket="frostvault-photos-ab12cd34", region="eu-south-1",
                                  vault_uuid=UUID, vault_name="Photos", profile=DEEP,
                                  retention_lock_days=None)
        bucket_provisioning.provision_bucket(s3, spec)
        s3.buckets[spec.name]["lifecycle"].append(
            {"ID": "owner-expiry", "Status": "Enabled", "Filter": {"Prefix": ""},
             "NoncurrentVersionExpiration": {"NoncurrentDays": 30}})
        self.assertFalse(bucket_provisioning.lifecycle_check(s3, spec).ok)

    def test_a_retention_lock_is_part_of_the_spec(self) -> None:
        spec = vault_buckets.spec(bucket="frostvault-photos-ab12cd34", region="eu-south-1",
                                  vault_uuid=UUID, vault_name="Photos", profile=DEEP,
                                  retention_lock_days=180)
        self.assertEqual(spec.retention_lock, bucket_provisioning.RetentionLock(days=180))


class KeyLayoutTests(unittest.TestCase):
    def test_object_prefix_follows_the_target(self) -> None:
        self.assertEqual(object_prefix("bucket_root", UUID), "")
        self.assertEqual(object_prefix("vault_prefix", UUID), f"vaults/{UUID}/")
        for layout in (None, "", "other"):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                object_prefix(layout, UUID)

    def test_vault_deletion_lists_a_vault_bucket_whole(self) -> None:
        dedicated = {"uuid": UUID, "s3_key_layout": "bucket_root", "s3_bucket": "frostvault-x-ab12cd34"}
        self.assertEqual(vault_deletion.vault_prefix(dedicated), "")
        with self.assertRaises(vault_deletion.VaultDeletionError):
            vault_deletion.vault_prefix({**dedicated, "s3_bucket": "shared"})
        self.assertEqual(vault_deletion.vault_prefix(
            {"uuid": UUID, "s3_key_layout": "vault_prefix", "s3_bucket": "shared"}), f"vaults/{UUID}/")


class ClientTests(unittest.TestCase):
    def test_no_region_or_credentials_means_unavailable(self) -> None:
        with self.assertRaises(vault_buckets.VaultBucketUnavailable):
            vault_buckets.client_for_region(" ", lambda region: object())

        def refuse(region: str) -> object:
            raise RuntimeError("secret provider detail")

        with self.assertRaises(vault_buckets.VaultBucketUnavailable) as caught:
            vault_buckets.client_for_region("eu-south-1", refuse)
        self.assertNotIn("secret", str(caught.exception))


class VaultBucketProvisioningTests(unittest.TestCase):
    """Creation, failure, retry and restart against a migrated catalog."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        source_layout.ensure_managed_directory()
        with PostgresConnection(str(self.database_path)) as connection:
            insert_owner(connection, display_name="Alice")
        self.settings = replace(Settings(), database_dsn=database_dsn(str(self.database_path)),
                                aws_region="eu-south-1")
        for target in ("app.database.settings", "app.services.vaults.settings"):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.s3 = fake_vault_buckets(self)

    def query(self, statement: str, params: tuple = ()) -> list[dict]:
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute(statement, params).fetchall()

    def open_problems(self) -> list[dict]:
        return self.query("SELECT event, vault_id, message_params FROM notifications "
                          "WHERE problem_status='open' ORDER BY id")

    def test_a_new_vault_is_active_only_with_a_verified_bucket(self) -> None:
        vault = vaults_service.create_vault("Foto 2024")
        bucket = self.s3.buckets[vault["s3_bucket"]]
        self.assertEqual((vault["lifecycle"], vault["s3_prefix"]), ("active", ""))
        self.assertEqual(bucket["region"], "eu-south-1")
        self.assertEqual(bucket["versioning"], "Enabled")
        self.assertEqual(bucket["ownership"]["Rules"], [{"ObjectOwnership": "BucketOwnerEnforced"}])
        self.assertEqual(bucket["sse"]["Rules"][0]["ApplyServerSideEncryptionByDefault"],
                         {"SSEAlgorithm": "AES256"})
        self.assertTrue(all(bucket["bpa"].values()))
        self.assertEqual({tag["Key"]: tag["Value"] for tag in bucket["tags"]},
                         {"frostvault:vault": vault["uuid"], "frostvault:name": "Foto 2024"})
        self.assertEqual([rule["ID"] for rule in bucket["lifecycle"]],
                         ["frostvault-abort-incomplete-multipart"])
        self.assertNotIn("lock", bucket)
        self.assertEqual(self.query("SELECT * FROM vault_bucket_provisioning"), [])
        events = self.query("SELECT event, detail FROM audit_events WHERE event='vault.bucket_ready'")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["bucket"], vault["s3_bucket"])

    def test_a_failure_keeps_the_vault_provisioning_with_a_problem_until_a_retry(self) -> None:
        self.s3.fail["put_bucket_encryption"] = "AccessDenied"
        vault = vaults_service.create_vault("Docs")
        state = vault["bucket"]
        self.assertEqual((vault["lifecycle"], state["state"], state["stage"], state["attempts"]),
                         ("provisioning", "failed", "created", 1))
        self.assertIn("AccessDenied", state["error"])
        self.assertNotIn("provider detail", state["error"])
        problems = self.open_problems()
        self.assertEqual([(row["event"], row["vault_id"]) for row in problems],
                         [("vault_bucket_failed", vault["id"])])
        params = problems[0]["message_params"]
        params = json.loads(params) if isinstance(params, str) else params
        self.assertEqual((params["bucket"], params["vault"]), (vault["s3_bucket"], "Docs"))
        # The same name, the configuration applied again, then verified.
        state = vault_buckets.provision(vault["id"])
        self.assertEqual((state["state"], state["bucket"]), ("ready", vault["s3_bucket"]))
        self.assertEqual(list(self.s3.buckets), [vault["s3_bucket"]])
        self.assertEqual(self.open_problems(), [])
        self.assertEqual(self.query("SELECT lifecycle FROM vaults")[0]["lifecycle"], "active")
        # An active Vault is returned unchanged.
        calls = len(self.s3.calls)
        self.assertEqual(vault_buckets.provision(vault["id"])["state"], "ready")
        self.assertEqual(len(self.s3.calls), calls)

    def test_a_verification_failure_never_activates_the_vault(self) -> None:
        with patch.object(vault_buckets.buckets, "verify_bucket",
                          return_value=bucket_provisioning.BucketVerification(
                              "frostvault-docs-ab12cd34", "eu-south-1",
                              (bucket_provisioning.BucketCheck(
                                  "bucket.versioning", False, "Versioning is Suspended"),))):
            vault = vaults_service.create_vault("Docs")
        self.assertEqual((vault["lifecycle"], vault["bucket"]["stage"]), ("provisioning", "configured"))
        self.assertIn("Versioning is Suspended", vault["bucket"]["error"])

    def test_a_network_failure_is_recorded_without_provider_detail(self) -> None:
        self.s3.create_bucket = lambda **_: (_ for _ in ()).throw(
            EndpointConnectionError(endpoint_url="https://s3.example/account-123"))
        vault = vaults_service.create_vault("Docs")
        self.assertEqual((vault["bucket"]["state"], vault["bucket"]["stage"]), ("failed", "reserved"))
        self.assertNotIn("account-123", vault["bucket"]["error"])

    def test_a_name_another_account_owns_is_replaced_before_activation(self) -> None:
        names = iter(["frostvault-docs-taken000", "frostvault-docs-free0000"])
        self.s3.foreign.add("frostvault-docs-taken000")
        with patch.object(vault_buckets, "new_bucket_name", side_effect=lambda name: next(names)):
            vault = vaults_service.create_vault("Docs")
        self.assertEqual((vault["lifecycle"], vault["s3_bucket"]), ("active", "frostvault-docs-free0000"))
        self.assertEqual(list(self.s3.buckets), ["frostvault-docs-free0000"])
        self.assertNotIn(("head_bucket", "frostvault-docs-taken000"), self.s3.calls)

    def test_three_taken_names_in_a_row_fail_and_keep_the_last_name(self) -> None:
        self.s3.foreign.update({f"frostvault-docs-taken00{i}" for i in range(5)})
        names = iter([f"frostvault-docs-taken00{i}" for i in range(5)])
        with patch.object(vault_buckets, "new_bucket_name", side_effect=lambda name: next(names)):
            vault = vaults_service.create_vault("Docs")
        self.assertEqual(vault["bucket"]["state"], "failed")
        self.assertIn("another AWS account", vault["bucket"]["error"])
        self.assertEqual(self.s3.buckets, {})

    def test_an_interrupted_creation_resumes_on_the_same_name_after_a_restart(self) -> None:
        # The process stopped after CreateBucket, before configuration.
        with patch.object(vault_buckets, "provision", return_value={"state": "creating"}):
            vault = vaults_service.create_vault("Docs")
        self.assertEqual(vault["lifecycle"], "provisioning")
        self.s3.create_bucket(Bucket=vault["s3_bucket"],
                              CreateBucketConfiguration={"LocationConstraint": "eu-south-1"})
        states = vault_buckets.resume_all()
        self.assertEqual([(state["vault_id"], state["state"]) for state in states],
                         [(vault["id"], "ready")])
        self.assertEqual(list(self.s3.buckets), [vault["s3_bucket"]])
        self.assertEqual(self.query("SELECT lifecycle FROM vaults")[0]["lifecycle"], "active")
        self.assertEqual(vault_buckets.resume_all(), [])

    def test_a_provisioning_vault_is_not_operational(self) -> None:
        self.s3.fail["create_bucket"] = "AccessDenied"
        vault = vaults_service.create_vault("Docs")
        with PostgresConnection(str(self.database_path)) as connection:
            inventory = vaults_service.list_vault_inventory(connection)
        self.assertEqual(inventory[0]["bucket_provisioning"]["state"], "failed")
        self.assertTrue(inventory[0]["dedicated_bucket"])
        from app.services import protection_stop
        with PostgresConnection(str(self.database_path)) as connection:
            with self.assertRaises(ValueError):
                protection_stop.stop_protection(
                    connection, vault_id=vault["id"], cloud_disposition="keep", actor="owner",
                    reason="test", confirmation="Docs")

    def test_a_retention_lock_is_applied_at_creation_only(self) -> None:
        """#736 seam: the lock is chosen at creation, then part of the target."""
        vault = vaults_service.create_vault(
            "Docs", retention_lock=bucket_provisioning.RetentionLock(days=90))
        bucket = self.s3.buckets[vault["s3_bucket"]]
        self.assertEqual(bucket["lock"]["Rule"]["DefaultRetention"], {"Mode": "GOVERNANCE", "Days": 90})
        self.assertEqual(self.query("SELECT retention_lock_days FROM vault_targets")[0]["retention_lock_days"], 90)
        self.assertEqual(vault["bucket"]["retention_lock_days"], 90)

    def test_a_storage_plan_change_rewrites_only_the_lifecycle_when_it_differs(self) -> None:
        vault = vaults_service.create_vault("Docs")
        bucket = vault["s3_bucket"]
        with PostgresConnection(str(self.database_path)) as connection:
            self.assertEqual(lifecycle_policies.sync_lifecycle_rules_for_bucket(connection, self.s3, bucket=bucket), 0)
        self.assertEqual(self.s3.mutating_calls().count("put_bucket_lifecycle_configuration"), 1)
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE vaults SET storage_plan=%s WHERE id=%s",
                               (profile_to_json(DEEP), vault["id"]))
            self.assertEqual(lifecycle_policies.sync_lifecycle_rules_for_bucket(connection, self.s3, bucket=bucket), 1)
        rules = self.s3.buckets[bucket]["lifecycle"]
        self.assertEqual([rule["ID"] for rule in rules],
                         [f"frostvault-plan-{vault['uuid']}", "frostvault-abort-incomplete-multipart"])
        mutations = self.s3.mutating_calls()
        self.assertEqual(mutations.count("put_bucket_lifecycle_configuration"), 2)
        # Nothing but the lifecycle is written again.
        self.assertEqual(mutations.count("put_bucket_versioning"), 1)
        self.assertEqual(mutations.count("put_bucket_tagging"), 1)


if __name__ == "__main__":
    unittest.main()
