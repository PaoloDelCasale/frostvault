"""Create, configure and verify FrostVault-owned buckets (#734, ADR-0049).

Seam: ``app.services.bucket_provisioning`` with an injected client. The unit
tests use ``tests.fake_bucket_s3``; ``ProvisioningAgainstS3CompatibleServer``
runs the same contract against the disposable S3-compatible server of PR CI
(``TEST_S3_ENDPOINT``), never against AWS.
"""
from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from app.services import bucket_provisioning as buckets
from tests.fake_bucket_s3 import FakeBucketS3, client_error

ABORT = {"ID": "frostvault-abort-incomplete-multipart", "Status": "Enabled",
         "Filter": {"Prefix": ""}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}
PLAN = {"ID": "frostvault-plan", "Status": "Enabled", "Filter": {},
        "Transitions": [{"Days": 30, "StorageClass": "DEEP_ARCHIVE"}]}


def spec(name="frostvault-docs-abcd1234", *, region="eu-south-1", lock=None, rules=(ABORT,)):
    return buckets.BucketSpec(name=name, region=region,
                              tags={"frostvault:vault": "uuid-1", "frostvault:name": "Docs"},
                              lifecycle_rules=rules, retention_lock=lock)


class BucketNameTests(unittest.TestCase):
    def test_random_suffix_is_eight_lowercase_alphanumerics(self):
        names = {buckets.random_bucket_name("frostvault-system") for _ in range(50)}
        self.assertGreater(len(names), 45)
        for name in names:
            self.assertRegex(name, r"^frostvault-system-[a-z0-9]{8}$")

    def test_names_stay_inside_the_iam_scope(self):
        with self.assertRaises(ValueError):
            buckets.random_bucket_name("backups")
        with self.assertRaises(ValueError):
            buckets.random_bucket_name("frostvault-" + "x" * 60)

    def test_retention_lock_is_governance_only(self):
        with self.assertRaises(ValueError):
            buckets.RetentionLock(days=30, mode="COMPLIANCE")
        with self.assertRaises(ValueError):
            buckets.RetentionLock(days=0)


class ProvisionBucketTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeBucketS3()

    def test_creates_configures_and_verifies_every_setting(self):
        verification = buckets.provision_bucket(self.s3, spec(rules=(PLAN, ABORT)))
        self.assertTrue(verification.ok, verification.failure_message())
        self.assertEqual({check.code for check in verification.checks}, {
            "bucket.region", "bucket.versioning", "bucket.public_access_block",
            "bucket.encryption", "bucket.lifecycle", "bucket.tags", "bucket.retention_lock"})
        stored = self.s3.buckets["frostvault-docs-abcd1234"]
        self.assertEqual(stored["region"], "eu-south-1")
        self.assertEqual(stored["versioning"], "Enabled")
        self.assertTrue(all(stored["bpa"].values()) and len(stored["bpa"]) == 4)
        self.assertEqual(stored["sse"]["Rules"][0]["ApplyServerSideEncryptionByDefault"],
                         {"SSEAlgorithm": "AES256"})
        self.assertNotIn("lock", stored)

    def test_us_east_1_is_created_without_a_location_constraint(self):
        buckets.provision_bucket(self.s3, spec(region="us-east-1"))
        self.assertIsNone(self.s3.buckets["frostvault-docs-abcd1234"]["region"])

    def test_a_second_run_resumes_an_interrupted_one(self):
        self.s3.fail["put_bucket_encryption"] = "InternalError"
        with self.assertRaises(buckets.BucketProvisioningError) as caught:
            buckets.provision_bucket(self.s3, spec())
        self.assertIn("s3:PutEncryptionConfiguration (InternalError)", str(caught.exception))
        self.assertNotIn("provider detail", str(caught.exception))
        self.assertTrue(buckets.provision_bucket(self.s3, spec()).ok)
        self.assertEqual(self.s3.mutating_calls().count("create_bucket"), 2)

    def test_a_foreign_name_is_reported_for_a_new_name(self):
        self.s3.foreign.add("frostvault-docs-abcd1234")
        with self.assertRaises(buckets.BucketNameUnavailable):
            buckets.provision_bucket(self.s3, spec())

    def test_repeated_creation_answered_as_already_exists_by_the_owner_is_resumed(self):
        buckets.create_bucket(self.s3, spec())
        with patch.object(self.s3, "create_bucket",
                          side_effect=client_error("BucketAlreadyExists", "CreateBucket")):
            buckets.create_bucket(self.s3, spec())

    def test_other_tags_are_kept(self):
        buckets.create_bucket(self.s3, spec())
        self.s3.put_bucket_tagging(Bucket="frostvault-docs-abcd1234",
                                   Tagging={"TagSet": [{"Key": "cost-center", "Value": "home"}]})
        buckets.provision_bucket(self.s3, spec())
        tags = {tag["Key"]: tag["Value"] for tag in self.s3.buckets["frostvault-docs-abcd1234"]["tags"]}
        self.assertEqual(tags, {"cost-center": "home", "frostvault:vault": "uuid-1",
                                "frostvault:name": "Docs"})

    def test_governance_retention_lock_is_enabled_at_creation(self):
        lock = buckets.RetentionLock(days=180)
        self.assertTrue(buckets.provision_bucket(self.s3, spec(lock=lock)).ok)
        self.assertIn(("create_bucket", "frostvault-docs-abcd1234"), self.s3.calls)
        stored = self.s3.buckets["frostvault-docs-abcd1234"]["lock"]
        self.assertEqual(stored["Rule"]["DefaultRetention"], {"Mode": "GOVERNANCE", "Days": 180})

    def test_no_lifecycle_rules_leave_none(self):
        buckets.create_bucket(self.s3, spec())
        self.s3.put_bucket_lifecycle_configuration(
            Bucket="frostvault-docs-abcd1234", LifecycleConfiguration={"Rules": [PLAN]})
        self.assertTrue(buckets.provision_bucket(self.s3, spec(rules=())).ok)
        self.assertNotIn("lifecycle", self.s3.buckets["frostvault-docs-abcd1234"])


class VerifyBucketTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeBucketS3()
        buckets.provision_bucket(self.s3, spec())
        self.stored = self.s3.buckets["frostvault-docs-abcd1234"]

    def failures(self, verification):
        return {check.code: check.message for check in verification.checks if not check.ok}

    def test_drift_of_each_setting_is_named(self):
        self.stored["versioning"] = "Suspended"
        self.stored["bpa"]["RestrictPublicBuckets"] = False
        self.stored["sse"] = {"Rules": [{"ApplyServerSideEncryptionByDefault": {
            "SSEAlgorithm": "aws:kms"}}]}
        failures = self.failures(buckets.verify_baseline(self.s3, "frostvault-docs-abcd1234",
                                                         "eu-south-1"))
        self.assertEqual(failures, {
            "bucket.versioning": "Versioning is suspended, not enabled",
            "bucket.public_access_block": "Block Public Access is not fully enabled (all four settings)",
            "bucket.encryption": "Default encryption is not SSE-S3 (AES256)",
        })

    def test_wrong_region_and_missing_bucket(self):
        self.assertEqual(self.failures(buckets.verify_baseline(
            self.s3, "frostvault-docs-abcd1234", "eu-west-1")),
            {"bucket.region": "The bucket is in eu-south-1, not in eu-west-1"})
        self.assertEqual(self.failures(buckets.verify_baseline(self.s3, "frostvault-nope", "eu-south-1")),
                         {"bucket.exists": "The bucket does not exist"})

    def test_absent_configuration_fails_and_denied_reads_name_the_permission(self):
        for key in ("bpa", "sse"):
            del self.stored[key]
        self.s3.fail["get_bucket_versioning"] = "AccessDenied"
        failures = self.failures(buckets.verify_baseline(self.s3, "frostvault-docs-abcd1234",
                                                         "eu-south-1"))
        self.assertEqual(failures["bucket.versioning"], "S3 refused s3:GetBucketVersioning (AccessDenied)")
        self.assertIn("bucket.public_access_block", failures)
        self.assertIn("bucket.encryption", failures)

    def test_lifecycle_tags_and_unexpected_object_lock(self):
        self.stored["lifecycle"].append({**PLAN, "ID": "someone-else"})
        self.stored["tags"] = []
        self.stored["lock"] = {"ObjectLockEnabled": "Enabled"}
        failures = self.failures(buckets.verify_bucket(self.s3, spec()))
        self.assertEqual(set(failures), {"bucket.lifecycle", "bucket.tags", "bucket.retention_lock"})

    def test_lifecycle_comparison_ignores_how_s3_spells_an_unfiltered_rule(self):
        self.stored["lifecycle"] = [{**ABORT, "Filter": {}}]
        self.assertTrue(buckets.verify_bucket(self.s3, spec()).ok)
        self.stored["lifecycle"] = [{key: value for key, value in ABORT.items() if key != "Filter"}
                                    | {"Prefix": ""}]
        self.assertTrue(buckets.verify_bucket(self.s3, spec()).ok)
        self.stored["lifecycle"] = [{**ABORT, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]
        self.assertFalse(buckets.verify_bucket(self.s3, spec()).ok)

    def test_retention_lock_must_match_mode_and_days(self):
        locked = spec("frostvault-locked-abcd1234", lock=buckets.RetentionLock(days=90))
        buckets.provision_bucket(self.s3, locked)
        stored = self.s3.buckets["frostvault-locked-abcd1234"]
        stored["lock"]["Rule"]["DefaultRetention"] = {"Mode": "GOVERNANCE", "Days": 90, "Years": 0}
        self.assertTrue(buckets.verify_bucket(self.s3, locked).ok)
        stored["lock"]["Rule"]["DefaultRetention"]["Days"] = 30
        self.assertFalse(buckets.verify_bucket(self.s3, locked).ok)

    def test_verification_never_changes_the_bucket(self):
        before = len(self.s3.mutating_calls())
        buckets.verify_bucket(self.s3, spec())
        buckets.verify_baseline(self.s3, "frostvault-docs-abcd1234", "eu-south-1")
        self.assertEqual(len(self.s3.mutating_calls()), before)


def _s3_compatible_endpoint() -> str | None:
    endpoint = os.getenv("TEST_S3_ENDPOINT", "").strip()
    # Never create buckets in a real AWS account.
    if not endpoint or endpoint.lower() == "aws" or not re.match(r"^https?://(127\.0\.0\.1|localhost)[:/]", endpoint):
        return None
    return endpoint


@unittest.skipUnless(_s3_compatible_endpoint(), "needs the disposable S3-compatible server of PR CI")
class ProvisioningAgainstS3CompatibleServer(unittest.TestCase):
    def setUp(self):
        import boto3

        self.client = boto3.client(
            "s3", endpoint_url=_s3_compatible_endpoint(),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        self.region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

    def test_system_and_locked_buckets_verify_and_resume(self):
        for lock in (None, buckets.RetentionLock(days=30)):
            with self.subTest(lock=lock):
                bucket = spec(buckets.random_bucket_name("frostvault-ci"), region=self.region,
                              lock=lock, rules=(ABORT,))
                self.assertTrue(buckets.provision_bucket(self.client, bucket).ok)
                # A second run (a resumed creation) is idempotent.
                verification = buckets.provision_bucket(self.client, bucket)
                self.assertTrue(verification.ok, verification.failure_message())
                self.client.put_bucket_versioning(Bucket=bucket.name,
                                                  VersioningConfiguration={"Status": "Suspended"})
                self.assertFalse(buckets.verify_bucket(self.client, bucket).ok)


if __name__ == "__main__":
    unittest.main()
