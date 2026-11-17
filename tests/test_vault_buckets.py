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
            "System": "vault-system",
            "SYSTEM photos": "vault-system-photos",
            "system-" + "x" * 50: "vault-system-" + "x" * 27,
            "system " + "y" * 33: "vault-system-" + "y" * 27,
            "Systems": "systems",
            "My System": "my-system",
            "x" * 60: "x" * 40,
            "a" * 39 + " b": "a" * 39,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(vault_buckets.slug(name), expected)

    def test_bucket_names_are_valid_unique_and_never_the_system_bucket(self) -> None:
        pattern = re.compile(r"^frostvault-[a-z0-9]+(-[a-z0-9]+)*-[a-z0-9]{8}$")
        for name in ("Photos", "x" * 120, "System", "System Photos", "system-" + "z" * 80,
                     "Ünïcødé ✓ name", "日本"):
            with self.subTest(name=name):
                bucket = vault_buckets.new_bucket_name(name)
                self.assertRegex(bucket, pattern)
                self.assertLessEqual(len(bucket), 63)
                self.assertLessEqual(len(vault_buckets.slug(name)), 40)
                # Only the System Bucket is frostvault-system-*.
                self.assertFalse(bucket.startswith("frostvault-system-"))
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


class _Rows:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows


class _Catalog:
    """The two catalog reads of ``sync_lifecycle_rules_for_bucket``, answered in order."""

    def __init__(self, *answers: list[dict]):
        self.answers = list(answers)

    def execute(self, statement: str, params: tuple = ()) -> _Rows:
        return _Rows(self.answers.pop(0))


class SharedBucketLifecycleTests(unittest.TestCase):
    """T1: a shared bucket of older Vaults keeps the rules FrostVault does not own."""

    def test_foreign_rules_stay_and_the_minimum_size_is_sent(self) -> None:
        s3 = FakeBucketS3()
        s3.create_bucket(Bucket="shared", CreateBucketConfiguration={"LocationConstraint": "eu-south-1"})
        foreign = {"ID": "owner-expiry", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
                   "Expiration": {"Days": 30}}
        s3.buckets["shared"]["lifecycle"] = [foreign, {
            "ID": f"frostvault-plan-{UUID}", "Status": "Enabled",
            "Filter": {"Prefix": f"vaults/{UUID}/"}, "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]}]
        s3.buckets["shared"]["lifecycle_minimum_size"] = "varies_by_storage_class"
        catalog = _Catalog([{"key_layout": "vault_prefix"}], [
            {"uuid": UUID, "s3_prefix": f"vaults/{UUID}/", "storage_plan_json": profile_to_json(DEEP)}])
        with patch.object(s3, "put_bucket_lifecycle_configuration",
                          wraps=s3.put_bucket_lifecycle_configuration) as put:
            self.assertEqual(
                lifecycle_policies.sync_lifecycle_rules_for_bucket(catalog, s3, bucket="shared"), 1)
        self.assertEqual(put.call_args.kwargs["TransitionDefaultMinimumObjectSize"],
                         "all_storage_classes_128K")
        rules = s3.buckets["shared"]["lifecycle"]
        self.assertEqual([rule["ID"] for rule in rules],
                         ["owner-expiry", f"frostvault-plan-{UUID}", "frostvault-abort-incomplete-multipart"])
        self.assertEqual(rules[0], foreign)
        self.assertEqual(rules[1]["Filter"], {"Prefix": f"vaults/{UUID}/"})
        self.assertEqual(rules[1]["Transitions"], [{"Days": 0, "StorageClass": "DEEP_ARCHIVE"}])
        self.assertEqual(rules[2]["Filter"], {"Prefix": "vaults/"})
        self.assertEqual(s3.buckets["shared"]["lifecycle_minimum_size"], "all_storage_classes_128K")

    def test_targets_of_one_bucket_with_two_layouts_are_refused(self) -> None:
        s3 = FakeBucketS3()
        catalog = _Catalog([{"key_layout": "vault_prefix"}, {"key_layout": "bucket_root"}])
        with self.assertRaisesRegex(ValueError, "one key layout"):
            lifecycle_policies.sync_lifecycle_rules_for_bucket(catalog, s3, bucket="frostvault-x-ab12cd34")
        self.assertEqual(s3.mutating_calls(), [])


class RemovedSettingsTests(unittest.TestCase):
    """S8: VAULT_S3_BUCKET / S3_BUCKET are ignored, with one warning naming them only."""

    def test_one_warning_names_the_settings_never_their_values(self) -> None:
        from app.config import warn_removed_settings

        with self.assertLogs("app.config", level="WARNING") as logs:
            message = warn_removed_settings({"VAULT_S3_BUCKET": "secret-bucket-name",
                                             "S3_BUCKET": "other-secret", "AWS_DEFAULT_REGION": "x"})
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertEqual(message, "VAULT_S3_BUCKET and S3_BUCKET ignored since 0.15.0; "
                                  "each Vault uses its own bucket")
        self.assertNotIn("secret", logs.output[0])
        with self.assertLogs("app.config", level="WARNING") as logs:
            warn_removed_settings({"S3_BUCKET": "b"})
        self.assertEqual(logs.records[0].getMessage(),
                         "S3_BUCKET ignored since 0.15.0; each Vault uses its own bucket")

    def test_nothing_is_logged_without_them(self) -> None:
        from app.config import warn_removed_settings

        with self.assertNoLogs("app.config", level="WARNING"):
            self.assertIsNone(warn_removed_settings({"VAULT_S3_BUCKET": " ", "AWS_DEFAULT_REGION": "x"}))

    def test_the_application_start_warns(self) -> None:
        import inspect

        import app.main as main

        self.assertIn("warn_removed_settings()", inspect.getsource(main.lifespan))


class FirstScanTests(unittest.TestCase):
    """S4: a Vault the loop activates is scanned on its own thread, not the maintenance pool."""

    def test_an_activated_vault_is_submitted_to_its_scan_thread(self) -> None:
        from app import storage

        states = [{"vault_id": 7, "state": "ready"}, {"vault_id": 8, "state": "settling"}]
        with patch.object(vault_buckets, "resume_all", return_value=states), \
                patch.object(storage, "_note_worker_ok"), \
                patch.object(storage, "scan_vault") as scan, \
                patch.object(storage, "_submit_watcher_scan") as submit:
            storage.ensure_vault_buckets_once()
        scan.assert_not_called()
        submit.assert_called_once_with({"id": 7})

    def test_a_vault_error_is_reported_and_the_loop_is_not_marked_ok(self) -> None:
        from app import storage

        def resume(*, on_error):
            on_error(7, RuntimeError("catalog down"))
            return [{"vault_id": 8, "state": "ready"}]

        with patch.object(vault_buckets, "resume_all", side_effect=resume), \
                patch.object(storage, "_note_worker_ok") as ok, \
                patch.object(storage, "_report_worker_error") as report, \
                patch.object(storage, "_submit_watcher_scan") as submit:
            storage.ensure_vault_buckets_once()
        ok.assert_not_called()
        self.assertEqual((report.call_args.args[0], report.call_args.args[2]), ("vault_buckets", 7))
        submit.assert_called_once_with({"id": 8})


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

    def test_an_unexpected_error_is_recorded_by_its_type_with_a_problem(self) -> None:
        """S1: never the exception message, always a Problem."""
        with patch.object(vault_buckets.buckets, "configure_bucket",
                          side_effect=KeyError("https://s3.example/account-123 secret")):
            vault = vaults_service.create_vault("Docs")
        state = vault["bucket"]
        self.assertEqual((vault["lifecycle"], state["state"], state["error"]),
                         ("provisioning", "failed", "internal error (KeyError)"))
        problems = self.open_problems()
        self.assertEqual([row["event"] for row in problems], ["vault_bucket_failed"])
        params = problems[0]["message_params"]
        params = json.loads(params) if isinstance(params, str) else params
        self.assertEqual(params["error"], "internal error (KeyError)")
        self.assertNotIn("account-123", json.dumps(params))
        stored = self.query("SELECT last_error FROM vault_bucket_provisioning")[0]["last_error"]
        self.assertEqual(stored, "internal error (KeyError)")

    def test_one_vault_failing_never_stops_the_others(self) -> None:
        """S1: resume_all runs each Vault on its own."""
        self.s3.fail["create_bucket"] = "AccessDenied"
        first = vaults_service.create_vault("First")
        self.s3.fail["create_bucket"] = "AccessDenied"
        second = vaults_service.create_vault("Second")
        self.assertEqual({first["lifecycle"], second["lifecycle"]}, {"provisioning"})
        real = vault_buckets.provision
        errors: list[tuple[int, str]] = []

        def provision(vault_id: int, **kwargs):
            if vault_id == first["id"]:
                raise RuntimeError("catalog unavailable")
            return real(vault_id, **kwargs)

        with patch.object(vault_buckets, "RETRY_SECONDS", 0), \
                patch.object(vault_buckets, "provision", side_effect=provision):
            states = vault_buckets.resume_all(
                on_error=lambda vault_id, exc: errors.append((vault_id, type(exc).__name__)))
        self.assertEqual(errors, [(first["id"], "RuntimeError")])
        self.assertEqual([(state["vault_id"], state["state"]) for state in states],
                         [(second["id"], "ready")])

    def test_a_failure_is_retried_only_after_the_retry_delay(self) -> None:
        self.s3.fail["create_bucket"] = "AccessDenied"
        vault = vaults_service.create_vault("Docs")
        calls = len(self.s3.calls)
        self.assertEqual(vault_buckets.resume_all(), [])
        self.assertEqual(len(self.s3.calls), calls)
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE vault_bucket_provisioning "
                               "SET last_attempt_at = last_attempt_at - interval '16 minutes'")
        self.assertEqual([state["state"] for state in vault_buckets.resume_all()], ["ready"])

    def test_sdk_errors_name_the_stage_they_happened_in(self) -> None:
        """N6: creation, configuration or verification."""
        unreachable = EndpointConnectionError(endpoint_url="https://s3.example/account-123")

        def fail(*_args, **_kwargs):
            raise unreachable

        self.s3.put_bucket_versioning = fail
        vault = vaults_service.create_vault("Docs")
        self.assertEqual(vault["bucket"]["error"],
                         "S3 could not be reached during the bucket configuration "
                         "(EndpointConnectionError)")
        del self.s3.put_bucket_versioning
        self.s3.get_bucket_ownership_controls = fail
        state = vault_buckets.provision(vault["id"])
        self.assertEqual(state["error"], "S3 could not be reached during the bucket verification "
                                         "(EndpointConnectionError)")
        self.assertEqual(state["stage"], "configured")

    def test_a_verified_vault_waits_for_versioning_to_settle(self) -> None:
        """N1: active 15 minutes after the verification, from the background loop."""
        with patch.object(vault_buckets, "VERSIONING_SETTLE_SECONDS", 15 * 60):
            vault = vaults_service.create_vault("Docs")
            state = vault["bucket"]
            self.assertEqual((vault["lifecycle"], state["state"], state["stage"], state["error"]),
                             ("provisioning", "settling", "verified", None))
            self.assertTrue(850 <= state["active_in_seconds"] <= 900, state["active_in_seconds"])
            self.assertIsNotNone(state["active_at"])
            self.assertEqual(self.open_problems(), [])
            calls = len(self.s3.calls)
            # Nothing is due: no S3 request, no activation; a retry neither.
            self.assertEqual(vault_buckets.resume_all(), [])
            self.assertEqual(vault_buckets.provision(vault["id"])["state"], "settling")
            self.assertEqual(len(self.s3.calls), calls)
            with PostgresConnection(str(self.database_path)) as connection:
                connection.execute("UPDATE vault_bucket_provisioning "
                                   "SET verified_at = verified_at - interval '15 minutes'")
            states = vault_buckets.resume_all()
        self.assertEqual([(state["vault_id"], state["state"]) for state in states],
                         [(vault["id"], "ready")])
        self.assertEqual(len(self.s3.calls), calls)
        self.assertEqual(self.query("SELECT lifecycle FROM vaults")[0]["lifecycle"], "active")
        events = self.query("SELECT detail FROM audit_events WHERE event='vault.bucket_ready'")
        self.assertIsNotNone(events[0]["detail"]["verified_at"])

    def test_a_failure_after_an_earlier_one_clears_when_verified(self) -> None:
        self.s3.fail["put_bucket_encryption"] = "AccessDenied"
        vault = vaults_service.create_vault("Docs")
        with patch.object(vault_buckets, "VERSIONING_SETTLE_SECONDS", 15 * 60):
            state = vault_buckets.provision(vault["id"])
        self.assertEqual((state["state"], state["error"], state["attempts"]), ("settling", None, 1))
        self.assertEqual(self.open_problems(), [])

    def test_a_provisioning_vault_gets_no_scan_and_no_job(self) -> None:
        """S5: no local scan and no Job admission before the bucket is ready."""
        from app import storage
        from app.services.job_admission import lock_vault

        self.s3.fail["create_bucket"] = "AccessDenied"
        vault = vaults_service.create_vault("Docs")
        with patch.object(storage, "_verified_local_scan") as local, \
                patch.object(storage, "scan_cloud") as cloud:
            self.assertEqual(storage.scan_vault({"id": vault["id"]}), {"suspended": 1})
        local.assert_not_called()
        cloud.assert_not_called()
        self.assertEqual(storage.queue_vault_auto_uploads({"id": vault["id"]}), 0)
        with PostgresConnection(str(self.database_path)) as connection:
            with self.assertRaisesRegex(LookupError, "vault_quiesced"):
                lock_vault(connection, vault["id"])
            jobs = connection.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]
        self.assertEqual(jobs, 0)

    def test_the_owner_cancels_a_creation_and_the_bucket_is_named(self) -> None:
        """S7: the Vault leaves the catalog; a created bucket stays, named."""
        self.s3.fail["put_bucket_versioning"] = "AccessDenied"
        vault = vaults_service.create_vault("Docs")
        root = Path(vault["source_root"])
        self.assertTrue(root.is_dir())
        outcome = vault_buckets.cancel_creation(vault["id"])
        self.assertEqual(outcome, {"vault_id": vault["id"], "vault_name": "Docs",
                                   "bucket": vault["s3_bucket"], "region": "eu-south-1",
                                   "bucket_left": "created"})
        self.assertEqual(self.query("SELECT * FROM vaults"), [])
        self.assertEqual(self.query("SELECT * FROM vault_bucket_provisioning"), [])
        self.assertEqual(self.query("SELECT * FROM notifications WHERE vault_id IS NOT NULL"), [])
        self.assertEqual([row["bucket"] for row in self.query("SELECT bucket FROM vault_targets")],
                         [vault["s3_bucket"]])
        self.assertFalse(root.exists())
        self.assertIn(vault["s3_bucket"], self.s3.buckets)
        self.assertNotIn("delete_bucket", [name for name, _ in self.s3.calls])
        events = self.query("SELECT actor, vault_id, outcome, detail FROM audit_events "
                            "WHERE event='vault.creation_cancelled'")
        self.assertEqual((events[0]["actor"], events[0]["vault_id"], events[0]["outcome"]),
                         ("owner", vault["id"], "success"))
        detail = events[0]["detail"]
        self.assertEqual((detail["bucket"], detail["region"], detail["bucket_left"], detail["stage"]),
                         (vault["s3_bucket"], "eu-south-1", "created", "created"))
        # The released root can hold a new Vault; the cancelled one is gone.
        with self.assertRaises(LookupError):
            vault_buckets.cancel_creation(vault["id"])
        self.assertEqual(vault_buckets.resume_all(), [])

    def test_a_name_only_reserved_may_have_reached_aws(self) -> None:
        self.s3.create_bucket = lambda **_: (_ for _ in ()).throw(
            EndpointConnectionError(endpoint_url="https://s3.example"))
        vault = vaults_service.create_vault("Docs")
        root = Path(vault["source_root"])
        (root / "kept.txt").write_text("the Owner's file")
        outcome = vault_buckets.cancel_creation(vault["id"], actor="emergency")
        self.assertEqual(outcome["bucket_left"], "possible")
        # Never removes content: the folder stays with the Owner's file.
        self.assertEqual((root / "kept.txt").read_text(), "the Owner's file")
        detail = self.query("SELECT actor, detail FROM audit_events "
                            "WHERE event='vault.creation_cancelled'")[0]
        self.assertEqual((detail["actor"], detail["detail"]["bucket_left"]), ("emergency", "possible"))

    def test_only_a_provisioning_vault_idle_now_can_be_cancelled(self) -> None:
        active = vaults_service.create_vault("Photos")
        with self.assertRaises(vault_buckets.CancelRefused) as refused:
            vault_buckets.cancel_creation(active["id"])
        self.assertEqual(refused.exception.code, "not_provisioning")
        self.s3.fail["create_bucket"] = "AccessDenied"
        pending = vaults_service.create_vault("Docs")
        lock = vault_buckets._vault_lock(pending["id"])
        with lock, self.assertRaises(vault_buckets.CancelRefused) as busy:
            vault_buckets.cancel_creation(pending["id"])
        self.assertEqual(busy.exception.code, "busy")
        self.assertEqual(len(self.query("SELECT id FROM vaults")), 2)

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



class VaultBucketEndToEndTests(unittest.TestCase):
    """T1: upload, cloud scan and audit of a Vault Bucket Vault, keys at the root.

    A real catalog row (the CI database) with a ``bucket_root`` target and the
    in-memory versioned provider; the readiness gate is not under test here.
    """

    BUCKET = "frostvault-docs-ab12cd34"

    def setUp(self) -> None:
        import uuid as uuid_module
        from types import SimpleNamespace

        from app import storage
        from app.catalog import ArchiveCatalog
        from app.services.vault_relocation import enroll_vault_root_identity
        from tests.upload_provenance_provider import VersionedProvider

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source = root / "source"
        (self.source / "photos").mkdir(parents=True)
        self.file = self.source / "photos" / "report.txt"
        self.payload = b"vault bucket content"
        self.file.write_bytes(self.payload)
        self.db_path = root / "catalog"
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.provider = VersionedProvider()
        with PostgresConnection(str(self.db_path)) as connection:
            insert_owner(connection)
            target = connection.execute(
                "INSERT INTO vault_targets(provider, bucket, region, key_layout) "
                "VALUES ('s3', %s, 'eu-south-1', 'bucket_root') RETURNING id", (self.BUCKET,)).fetchone()
            connection.execute(
                "INSERT INTO vaults(id, target_id, name, source_root) VALUES (2, %s, 'Docs', %s)",
                (target["id"], str(self.source)))
            enroll_vault_root_identity(connection, 2, str(self.source))
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path="photos/report.txt", file_type="regular", size=len(self.payload),
                mtime_ns=self.file.stat().st_mtime_ns, observed_at=storage.now_iso())
        self.enterContext(patch("app.database.settings",
                                SimpleNamespace(database_dsn=database_dsn(str(self.db_path)))))
        self.enterContext(patch("app.storage.validate_cloud_vault"))
        self.enterContext(patch("app.storage.s3_client", return_value=self.provider))
        self.enterContext(patch("app.storage.upload_file", side_effect=self.provider.upload))
        self.uuid_module = uuid_module

    def vault(self) -> dict:
        with PostgresConnection(str(self.db_path)) as connection:
            return connection.execute(f"{vaults_service.VAULT_ROW_SQL} WHERE v.id=2").fetchone()

    def test_upload_scan_and_audit_use_the_bucket_root(self) -> None:
        from app import storage
        from app.catalog import ArchiveCatalog
        from app.main import queue_jobs
        from app.services.catalog_audit import audit_vault_catalog
        from tests.upload_support import claimed_job_row

        vault = self.vault()
        self.assertEqual((vault["s3_bucket"], vault["s3_prefix"]), (self.BUCKET, ""))
        self.assertEqual(queue_jobs("photos/report.txt", "upload", 2, 1)["item_count"], 1)
        with PostgresConnection(str(self.db_path)) as connection:
            job_id = connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
            claimed = ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=str(self.uuid_module.uuid4()),
                claimed_at=storage.now_iso(), claim_expires_at=storage._claim_expiry_at(),
                now=storage.now_iso(), restore_due_before=storage.now_iso())
            job = claimed_job_row(connection, job_id, claimed)
        storage.process_job(job)
        with PostgresConnection(str(self.db_path)) as connection:
            done = connection.execute("SELECT status, message FROM jobs WHERE id=%s", (job_id,)).fetchone()
            self.assertEqual(done["status"], "completed", done["message"])
            current = ArchiveCatalog(connection).get_file_by_path(2, "photos/report.txt")["current_version"]
        # The key is the relative path: no vaults/<uuid>/ prefix in a Vault Bucket.
        self.assertEqual(current["object_key"], "photos/report.txt")
        self.assertEqual([key for key, _ in self.provider.objects], ["photos/report.txt"])

        self.assertEqual(storage.scan_cloud(vault, storage.now_iso()), 1)
        with PostgresConnection(str(self.db_path)) as connection:
            report = audit_vault_catalog(connection, vault, self.provider)
        self.assertEqual(
            {key: report[key] for key in ("catalog_versions", "cloud_versions", "missing_in_cloud",
                                          "missing_in_catalog", "healthy")},
            {"catalog_versions": 1, "cloud_versions": 1, "missing_in_cloud": 0,
             "missing_in_catalog": 0, "healthy": 1})

        # The upload identity trigger refuses the legacy vaults/<uuid>/ key on a
        # Vault Bucket Vault: the same landed attempt, only the prefixed key.
        import psycopg

        with PostgresConnection(str(self.db_path)) as connection:
            with self.assertRaisesRegex(psycopg.errors.RestrictViolation, "exact landed identity"):
                connection.execute(
                    """INSERT INTO archive_versions
                       SELECT (jsonb_populate_record(NULL::archive_versions,
                               to_jsonb(av) || jsonb_build_object(
                                   'object_key', 'vaults/' || v.uuid::text || '/' || av.object_key))).*
                       FROM archive_versions av JOIN files f ON f.id = av.file_id
                       JOIN vaults v ON v.id = f.vault_id
                       WHERE av.id = %s""", (current["id"],))


if __name__ == "__main__":
    unittest.main()
