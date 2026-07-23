from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from app.services.s3_preflight import check_bucket_readiness


class BucketVersioningPreflightTests(unittest.TestCase):
    def test_enabled_versioning_passes(self) -> None:
        client = SimpleNamespace(
            head_bucket=lambda **_: None,
            get_bucket_location=lambda **_: {"LocationConstraint": "eu-south-1"},
            get_bucket_versioning=lambda **_: {"Status": "Enabled"},
        )
        result = check_bucket_readiness(
            "my-archive-bucket",
            region="eu-south-1",
            client=client,
        )
        self.assertTrue(result.ok)
        versioning = next(c for c in result.checks if c.code == "bucket.versioning")
        self.assertEqual(versioning.status, "pass")


class BucketVersioningFailureTests(unittest.TestCase):
    def _client(self, *, versioning_status: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            head_bucket=lambda **_: None,
            get_bucket_location=lambda **_: {"LocationConstraint": "eu-south-1"},
            get_bucket_versioning=lambda **_: {"Status": versioning_status},
        )

    def test_suspended_versioning_fails_with_remediation(self) -> None:
        result = check_bucket_readiness(
            "my-archive-bucket",
            region="eu-south-1",
            client=self._client(versioning_status="Suspended"),
        )
        self.assertFalse(result.ok)
        versioning = next(c for c in result.checks if c.code == "bucket.versioning")
        self.assertEqual(versioning.status, "fail")
        self.assertIn("suspended", versioning.message.lower())
        self.assertIn("versioning", (versioning.remediation or "").lower())

    def test_missing_versioning_status_fails(self) -> None:
        result = check_bucket_readiness(
            "my-archive-bucket",
            region="eu-south-1",
            client=self._client(versioning_status=None),
        )
        self.assertFalse(result.ok)
        versioning = next(c for c in result.checks if c.code == "bucket.versioning")
        self.assertEqual(versioning.status, "fail")
        self.assertIn("disabled", versioning.message.lower())


class BucketConfigurationPreflightTests(unittest.TestCase):
    def test_placeholder_bucket_is_rejected_without_aws_calls(self) -> None:
        client = SimpleNamespace(
            head_bucket=Mock(),
            get_bucket_location=Mock(),
            get_bucket_versioning=Mock(),
        )
        result = check_bucket_readiness(
            "BUCKET-NAME",
            region="eu-south-1",
            client=client,
        )
        self.assertFalse(result.ok)
        configured = next(c for c in result.checks if c.code == "bucket.configured")
        self.assertEqual(configured.status, "fail")
        client.head_bucket.assert_not_called()
        client.get_bucket_versioning.assert_not_called()

    def test_denied_versioning_permission_reports_actionable_failure(self) -> None:
        def deny_versioning(**_kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied",
                        "Message": "Access Denied",
                    }
                },
                "GetBucketVersioning",
            )

        client = SimpleNamespace(
            head_bucket=lambda **_: None,
            get_bucket_location=lambda **_: {"LocationConstraint": "eu-south-1"},
            get_bucket_versioning=deny_versioning,
        )
        result = check_bucket_readiness(
            "my-archive-bucket",
            region="eu-south-1",
            client=client,
        )
        self.assertFalse(result.ok)
        permissions = next(c for c in result.checks if c.code == "bucket.permissions")
        self.assertEqual(permissions.status, "fail")
        self.assertIn("s3:GetBucketVersioning", permissions.message)


class ValidateCloudVaultIntegrationTests(unittest.TestCase):
    def _ready_client(self, *, versioning_status: str = "Enabled") -> SimpleNamespace:
        return SimpleNamespace(
            head_bucket=lambda **_: None,
            get_bucket_location=lambda **_: {"LocationConstraint": "eu-south-1"},
            get_bucket_versioning=lambda **_: {"Status": versioning_status},
        )

    def test_validate_cloud_vault_passes_when_bucket_is_ready(self) -> None:
        from app.storage import validate_cloud_vault

        with patch("app.storage.s3_client", return_value=self._ready_client()):
            with patch("app.storage.settings") as mock_settings:
                mock_settings.aws_region = "eu-south-1"
                validate_cloud_vault({"s3_bucket": "my-archive-bucket"})

    def test_validate_cloud_vault_blocks_when_versioning_disabled(self) -> None:
        from app.storage import validate_cloud_vault

        with patch("app.storage.s3_client", return_value=self._ready_client(versioning_status="Suspended")):
            with patch("app.storage.settings") as mock_settings:
                mock_settings.aws_region = "eu-south-1"
                with self.assertRaises(RuntimeError) as error:
                    validate_cloud_vault({"s3_bucket": "my-archive-bucket"})
        self.assertIn("versioning", str(error.exception).lower())
