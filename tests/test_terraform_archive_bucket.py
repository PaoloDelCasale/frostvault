from __future__ import annotations

import json
import unittest
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "archive-bucket"


class ArchiveBucketTerraformTests(unittest.TestCase):
    def test_main_tf_declares_required_bucket_baseline(self) -> None:
        main_tf = (MODULE_ROOT / "main.tf").read_text(encoding="utf-8")
        self.assertIn("aws_s3_bucket_versioning", main_tf)
        self.assertIn('status = "Enabled"', main_tf)
        self.assertIn("aws_s3_bucket_public_access_block", main_tf)
        self.assertIn("BucketOwnerEnforced", main_tf)
        self.assertIn('sse_algorithm = "AES256"', main_tf)
        self.assertIn("abort_incomplete_multipart_upload", main_tf)
        self.assertNotIn("object_lock", main_tf.lower())

    def test_outputs_expose_application_settings(self) -> None:
        outputs_tf = (MODULE_ROOT / "outputs.tf").read_text(encoding="utf-8")
        # #735: VAULT_S3_BUCKET is gone; FrostVault creates every Vault Bucket.
        self.assertNotIn("S3_BUCKET", outputs_tf)
        self.assertIn("AWS_DEFAULT_REGION", outputs_tf)

    def test_operator_policy_covers_archive_operations(self) -> None:
        policy_path = MODULE_ROOT / "iam" / "archive-operator-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        actions = {
            action
            for statement in policy["Statement"]
            for action in statement["Action"]
        }
        self.assertIn("s3:GetBucketPublicAccessBlock", actions)
        self.assertNotIn("s3:PutBucketPublicAccessBlock", actions)
        self.assertIn("s3:PutObject", actions)
        self.assertIn("s3:GetObjectVersion", actions)
        self.assertIn("s3:RestoreObject", actions)
        self.assertIn("s3:PutObjectTagging", actions)
        self.assertIn("s3:PutLifecycleConfiguration", actions)
        self.assertIn("s3:DeleteObjectVersion", actions)
        # Exact upload reconciliation lists versions and incomplete multipart
        # uploads; each is its own IAM action, separate from s3:ListBucket.
        self.assertIn("s3:ListBucketVersions", actions)
        self.assertIn("s3:ListBucketMultipartUploads", actions)

    def test_frostvault_buckets_policy_is_scoped_and_never_bypasses_retention(self) -> None:
        """#734, #735 (ADR-0049): FrostVault creates, configures and uses only frostvault-* buckets."""
        policy_path = MODULE_ROOT / "iam" / "frostvault-buckets-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        allowed: set[str] = set()
        for statement in policy["Statement"]:
            resources = statement["Resource"]
            resources = resources if isinstance(resources, list) else [resources]
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if any(action.startswith("cloudwatch:") for action in actions):
                # #737: CloudWatch actions are not resource-scoped. This one
                # statement reads metrics only, never GetMetricData (billed).
                self.assertEqual((statement["Effect"], actions, resources),
                                 ("Allow", ["cloudwatch:GetMetricStatistics"], ["*"]))
                continue
            self.assertTrue(all(resource.startswith("arn:aws:s3:::frostvault-*")
                                for resource in resources), resources)
            if statement["Effect"] == "Allow":
                allowed.update(actions)
            else:
                self.assertEqual(actions, ["s3:BypassGovernanceRetention"])
        self.assertNotIn("s3:BypassGovernanceRetention", allowed)
        # #739: an empty Vault Bucket is deleted on request, only on frostvault-*.
        self.assertIn("s3:DeleteBucket", allowed)
        self.assertNotIn("cloudwatch:GetMetricData", allowed)
        self.assertFalse(any("*" in action for action in allowed))
        for action in ("s3:CreateBucket", "s3:PutBucketPublicAccessBlock",
                       "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration",
                       "s3:PutLifecycleConfiguration", "s3:PutBucketTagging",
                       "s3:GetBucketObjectLockConfiguration",
                       "s3:PutBucketObjectLockConfiguration",
                       "s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls",
                       "s3:PutObject", "s3:GetObject",
                       # #736: a refused DELETE of a locked version is explained
                       # by its retention, read but never changed or bypassed.
                       "s3:GetObjectRetention"):
            self.assertIn(action, allowed)
        for action in ("s3:PutObjectRetention", "s3:PutObjectLegalHold"):
            self.assertNotIn(action, allowed)
        # #735: everything a Vault does in its Vault Bucket, as on the shared bucket.
        operator = json.loads((MODULE_ROOT / "iam" / "archive-operator-policy.json")
                              .read_text(encoding="utf-8"))
        shared = {action for statement in operator["Statement"] for action in statement["Action"]}
        self.assertLessEqual(shared, allowed)
        for action in ("s3:ListBucketVersions", "s3:ListBucketMultipartUploads",
                       "s3:GetObjectVersion", "s3:DeleteObjectVersion", "s3:DeleteObject",
                       "s3:AbortMultipartUpload", "s3:RestoreObject",
                       "s3:GetLifecycleConfiguration", "s3:PutLifecycleConfiguration"):
            self.assertIn(action, allowed)

    def test_github_oidc_ci_policy_is_prefix_scoped(self) -> None:
        policy_path = MODULE_ROOT / "iam" / "github-oidc-ci-policy.json"
        raw = policy_path.read_text(encoding="utf-8")
        self.assertIn("${archive_ci_prefix}/*", raw)
        self.assertIn("s3:DeleteObjectVersion", raw)
        self.assertIn("s3:GetBucketPublicAccessBlock", raw)
        self.assertNotIn("s3:PutBucketPublicAccessBlock", raw)
        self.assertNotIn("s3:PutLifecycleConfiguration", raw)
        # CI exercises exact upload reconciliation, so it needs both list actions
        # (each separate from s3:ListBucket), scoped to the CI prefix.
        statements = json.loads(raw)["Statement"]
        scoped = next(item for item in statements if item["Sid"] == "ListOnlyCiPrefix")
        self.assertIn("s3:ListBucketVersions", scoped["Action"])
        self.assertIn("s3:ListBucketMultipartUploads", scoped["Action"])
        self.assertIn("s3:prefix", scoped["Condition"]["StringLike"])
        # No unbounded object ARN.
        self.assertNotIn(
            '"Resource": "arn:aws:s3:::${archive_ci_bucket_name}/*"',
            raw,
        )


if __name__ == "__main__":
    unittest.main()
