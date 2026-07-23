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
        self.assertIn("S3_BUCKET", outputs_tf)
        self.assertIn("VAULT_S3_BUCKET", outputs_tf)
        self.assertIn("AWS_DEFAULT_REGION", outputs_tf)

    def test_operator_policy_covers_archive_operations(self) -> None:
        policy_path = MODULE_ROOT / "iam" / "archive-operator-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        actions = {
            action
            for statement in policy["Statement"]
            for action in statement["Action"]
        }
        self.assertIn("s3:PutObject", actions)
        self.assertIn("s3:GetObjectVersion", actions)
        self.assertIn("s3:RestoreObject", actions)
        self.assertIn("s3:PutObjectTagging", actions)
        self.assertIn("s3:PutLifecycleConfiguration", actions)
        self.assertIn("s3:DeleteObjectVersion", actions)

    def test_github_oidc_ci_policy_is_prefix_scoped(self) -> None:
        policy_path = MODULE_ROOT / "iam" / "github-oidc-ci-policy.json"
        raw = policy_path.read_text(encoding="utf-8")
        self.assertIn("${archive_ci_prefix}/*", raw)
        self.assertIn("s3:DeleteObjectVersion", raw)
        self.assertNotIn("s3:PutLifecycleConfiguration", raw)
        # No unbounded object ARN.
        self.assertNotIn(
            '"Resource": "arn:aws:s3:::${archive_ci_bucket_name}/*"',
            raw,
        )


if __name__ == "__main__":
    unittest.main()
