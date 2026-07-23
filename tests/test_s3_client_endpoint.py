"""S3 client endpoint seam (issue #13).

Seams under test:
- ``app.storage.s3_client`` — public factory for the S3 API boundary.
  When ``AWS_ENDPOINT_URL`` is set, the client must talk to that
  S3-compatible endpoint (MinIO/LocalStack) instead of public AWS.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from botocore.exceptions import ClientError

from app.storage import s3_client


@unittest.skipUnless(
    os.getenv("TEST_S3_ENDPOINT")
    and os.getenv("TEST_S3_ENDPOINT", "").lower() != "aws",
    "Set TEST_S3_ENDPOINT to an S3-compatible endpoint (e.g. http://127.0.0.1:9000)",
)
class S3ClientEndpointTests(unittest.TestCase):
    def test_s3_client_can_create_and_head_bucket_on_compatible_endpoint(self) -> None:
        """The client must use AWS_ENDPOINT_URL so CI can target MinIO without AWS."""
        bucket = f"archive-ci-{uuid.uuid4().hex[:12]}"
        env = {
            "AWS_ACCESS_KEY_ID": os.environ["AWS_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": os.environ["AWS_SECRET_ACCESS_KEY"],
            "AWS_ENDPOINT_URL": os.environ["TEST_S3_ENDPOINT"],
            "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        }
        with patch.dict(os.environ, env, clear=False):
            # Re-import settings region is read at call time via settings.aws_region
            client = s3_client()
            client.create_bucket(Bucket=bucket)
            try:
                client.head_bucket(Bucket=bucket)
            finally:
                client.delete_bucket(Bucket=bucket)
