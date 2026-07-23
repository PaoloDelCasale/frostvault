"""Test-prefix cleanup seam (issue #13).

Seams under test:
- ``app.services.s3_prefix_cleanup.cleanup_prefix_versions`` — deletes every
  object version under a CI/test prefix and reports leftovers so a failed
  cleanup can be surfaced and safely rerun.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from app.services.s3_prefix_cleanup import cleanup_prefix_versions
from app.storage import s3_client


@unittest.skipUnless(
    os.getenv("TEST_S3_ENDPOINT"),
    "Set TEST_S3_ENDPOINT to an S3-compatible endpoint",
)
class S3PrefixCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bucket = os.environ.get("TEST_S3_BUCKET", "archive-ci")
        self.prefix = f"cleanup-test/{uuid.uuid4().hex}"
        env = {
            "AWS_ACCESS_KEY_ID": os.environ["AWS_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": os.environ["AWS_SECRET_ACCESS_KEY"],
            "AWS_ENDPOINT_URL": os.environ["TEST_S3_ENDPOINT"],
            "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        }
        self._env = patch.dict(os.environ, env, clear=False)
        self._env.start()
        self.client = s3_client()
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)
            self.client.put_bucket_versioning(
                Bucket=self.bucket,
                VersioningConfiguration={"Status": "Enabled"},
            )

    def tearDown(self) -> None:
        try:
            cleanup_prefix_versions(self.client, bucket=self.bucket, prefix=self.prefix)
        finally:
            self._env.stop()

    def test_cleanup_removes_all_versions_and_reports_empty_leftovers(self) -> None:
        key = f"{self.prefix}/payload.txt"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=b"one")
        self.client.put_object(Bucket=self.bucket, Key=key, Body=b"two")

        report = cleanup_prefix_versions(
            self.client, bucket=self.bucket, prefix=self.prefix
        )

        self.assertTrue(report.ok)
        self.assertGreaterEqual(report.deleted_versions, 2)
        self.assertEqual(report.leftover_keys, ())

        listed = self.client.list_object_versions(
            Bucket=self.bucket, Prefix=self.prefix
        )
        self.assertFalse(listed.get("Versions"))
        self.assertFalse(listed.get("DeleteMarkers"))

    def test_cleanup_is_idempotent_on_empty_prefix(self) -> None:
        first = cleanup_prefix_versions(
            self.client, bucket=self.bucket, prefix=self.prefix
        )
        second = cleanup_prefix_versions(
            self.client, bucket=self.bucket, prefix=self.prefix
        )
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(first.leftover_keys, ())
        self.assertEqual(second.leftover_keys, ())
        self.assertEqual(second.deleted_versions, 0)
