"""Bucket versioning and all four Block Public Access controls fail closed."""
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from app.services.s3_preflight import check_bucket_readiness, preflight_failure_message

FLAGS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")


def ready_client(**overrides):
    return SimpleNamespace(**{
        "head_bucket": Mock(),
        "get_bucket_location": Mock(return_value={"LocationConstraint": "eu-south-1"}),
        "get_bucket_versioning": Mock(return_value={"Status": "Enabled"}),
        "get_public_access_block": Mock(return_value={
            "PublicAccessBlockConfiguration": dict.fromkeys(FLAGS, True),
        }), **overrides,
    })


def check(client):
    return check_bucket_readiness("my-archive-bucket", region="eu-south-1", client=client)


class BucketPreflightTests(unittest.TestCase):
    def test_ready_requires_versioning_and_every_bpa_flag(self):
        client = ready_client()
        result = check(client)
        self.assertTrue(result.ok)
        client.get_public_access_block.assert_called_once_with(Bucket="my-archive-bucket")
        self.assertEqual({c.code: c.status for c in result.checks}, {
            "bucket.versioning": "pass", "bucket.public_access_block": "pass",
        })

    def test_each_flag_disabled_missing_or_non_boolean_fails(self):
        for flag in FLAGS:
            for value in (False, None, "true", 1, "false"):
                configuration = dict.fromkeys(FLAGS, True)
                configuration[flag] = value
                with self.subTest(flag=flag, value=value):
                    result = check(ready_client(get_public_access_block=Mock(return_value={
                        "PublicAccessBlockConfiguration": configuration,
                    })))
                    self.assertFalse(result.ok)
                    self.assertIn("s3:GetBucketPublicAccessBlock", preflight_failure_message(result))
            configuration = dict.fromkeys(FLAGS, True)
            del configuration[flag]
            self.assertFalse(check(ready_client(get_public_access_block=Mock(return_value={
                "PublicAccessBlockConfiguration": configuration,
            }))).ok)

    def test_absent_or_malformed_state_fails(self):
        for response in ({}, None, {"PublicAccessBlockConfiguration": None},
                         {"PublicAccessBlockConfiguration": {}},
                         {"PublicAccessBlockConfiguration": True}):
            with self.subTest(response=response):
                self.assertFalse(check(ready_client(get_public_access_block=Mock(return_value=response))).ok)

    def test_denied_unsupported_missing_configuration_and_network_error_fail(self):
        for code in ("AccessDenied", "NotImplemented", "NoSuchPublicAccessBlockConfiguration"):
            error = ClientError({"Error": {"Code": code, "Message": "provider-secret-fixture"}},
                                "GetPublicAccessBlock")
            result = check(ready_client(get_public_access_block=Mock(side_effect=error)))
            self.assertFalse(result.ok)
            self.assertNotIn("provider-secret-fixture", preflight_failure_message(result))
        self.assertFalse(check(ready_client(get_public_access_block=Mock(side_effect=OSError("network")))).ok)
        client = ready_client()
        del client.get_public_access_block
        self.assertFalse(check(client).ok)

    def test_suspended_or_absent_versioning_still_fails_with_bpa_enabled(self):
        for status in ("Suspended", None):
            result = check(ready_client(get_bucket_versioning=Mock(return_value={"Status": status})))
            self.assertFalse(result.ok)
            self.assertIn("versioning", preflight_failure_message(result).lower())

    def test_denied_versioning_permission_is_actionable(self):
        error = ClientError({"Error": {"Code": "AccessDenied"}}, "GetBucketVersioning")
        result = check(ready_client(get_bucket_versioning=Mock(side_effect=error)))
        self.assertFalse(result.ok)
        self.assertIn("s3:GetBucketVersioning", preflight_failure_message(result))

    def head_denied(self, **overrides):
        # HEAD responses have no body, so botocore reports only the status.
        error = ClientError({"Error": {"Code": "403", "Message": "Forbidden"},
                             "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadBucket")
        return ready_client(head_bucket=Mock(side_effect=error), **overrides)

    @staticmethod
    def provider_error(code, operation="GetBucketLocation", status=403):
        return ClientError({"Error": {"Code": code, "Message": "provider-secret-fixture"},
                            "ResponseMetadata": {"HTTPStatusCode": status}}, operation)

    def test_head_bucket_403_with_rejected_credentials_names_credentials_not_permission(self):
        for code in ("SignatureDoesNotMatch", "InvalidAccessKeyId", "InvalidClientTokenId",
                     "UnrecognizedClientException", "ExpiredToken", "InvalidToken"):
            client = self.head_denied(get_bucket_location=Mock(side_effect=self.provider_error(code)))
            with self.subTest(code=code):
                result = check(client)
                message = preflight_failure_message(result)
                self.assertFalse(result.ok)
                self.assertEqual([c.code for c in result.checks], ["bucket.credentials"])
                self.assertIn(code, message)
                self.assertIn("AWS_SECRET_ACCESS_KEY", message)
                self.assertNotIn("Missing permission", message)
                self.assertNotIn("provider-secret-fixture", message)
                client.get_bucket_versioning.assert_not_called()

    def test_head_bucket_403_with_readable_location_is_a_missing_list_permission(self):
        result = check(self.head_denied())
        message = preflight_failure_message(result)
        self.assertEqual([c.code for c in result.checks], ["bucket.permissions"])
        self.assertIn("s3:ListBucket", message)
        self.assertNotIn("credentials may", message)

    def test_head_bucket_403_with_authenticated_access_denied_is_a_permission_failure(self):
        result = check(self.head_denied(get_bucket_location=Mock(side_effect=self.provider_error("AccessDenied"))))
        message = preflight_failure_message(result)
        self.assertEqual([c.code for c in result.checks], ["bucket.permissions"])
        self.assertIn("s3:ListBucket", message)
        self.assertIn("s3:GetBucketLocation", message)
        self.assertNotIn("provider-secret-fixture", message)

    def test_head_bucket_403_without_a_distinguishing_code_names_both_causes(self):
        for failure in (self.provider_error("403"), self.provider_error(""), OSError("network"),
                        self.provider_error("SlowDown", status=503)):
            client = self.head_denied(get_bucket_location=Mock(side_effect=failure))
            with self.subTest(failure=repr(failure)):
                result = check(client)
                message = preflight_failure_message(result)
                self.assertFalse(result.ok)
                self.assertEqual([c.code for c in result.checks], ["bucket.access"])
                self.assertIn("credentials", message)
                self.assertIn("s3:ListBucket", message)
                self.assertNotIn("provider-secret-fixture", message)

    def test_credential_rejection_after_head_bucket_is_not_reported_as_missing_permission(self):
        for method, action in (("get_bucket_location", "s3:GetBucketLocation"),
                               ("get_bucket_versioning", "s3:GetBucketVersioning")):
            client = ready_client(**{method: Mock(side_effect=self.provider_error("SignatureDoesNotMatch"))})
            with self.subTest(method=method):
                message = preflight_failure_message(check(client))
                self.assertIn("SignatureDoesNotMatch", message)
                self.assertNotIn(action, message)

    def test_head_bucket_non_forbidden_errors_keep_their_existing_report(self):
        error = ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadBucket")
        client = ready_client(head_bucket=Mock(side_effect=error))
        result = check(client)
        self.assertEqual([c.code for c in result.checks], ["bucket.permissions"])
        client.get_bucket_location.assert_not_called()

    def test_wrong_region_fails(self):
        self.assertFalse(check(ready_client(get_bucket_location=Mock(return_value={"LocationConstraint": "us-west-2"}))).ok)

    def test_placeholder_bucket_makes_no_provider_calls(self):
        client = ready_client()
        result = check_bucket_readiness("BUCKET-NAME", region="eu-south-1", client=client)
        self.assertFalse(result.ok)
        client.head_bucket.assert_not_called()
        client.get_public_access_block.assert_not_called()

    def test_failed_bpa_prevents_scans_transfers_recovery_and_cleanup(self):
        from app import storage
        client = ready_client(get_public_access_block=Mock(return_value={
            "PublicAccessBlockConfiguration": dict.fromkeys(FLAGS, False),
        }))
        with tempfile.TemporaryDirectory() as directory:
            job = {"id": 891, "s3_bucket": "my-archive-bucket"}
            with patch("app.storage.cloud_preflight_gate.check", side_effect=lambda _connect, probe: (probe(), {"status": "ready"})[1]), patch("app.storage.s3_client", return_value=client), patch("app.storage.settings") as settings, patch("app.storage.upload_s3_file") as transfer:
                settings.aws_region = "eu-south-1"
                for operation in (storage.process_upload, storage.process_rename, storage.process_recover, storage.process_free_space):
                    with self.subTest(operation=operation.__name__), self.assertRaisesRegex(RuntimeError, "Block Public Access"):
                        operation(job)
                with self.assertRaisesRegex(RuntimeError, "Block Public Access"):
                    storage.scan_cloud(job, "scan")
                transfer.assert_not_called()

    def test_validate_cloud_vault_blocks_bpa_before_any_worker_transfer(self):
        from app.storage import validate_cloud_vault
        for response, succeeds in ((dict.fromkeys(FLAGS, True), True), ({}, False)):
            client = ready_client(get_public_access_block=Mock(return_value={
                "PublicAccessBlockConfiguration": response,
            }))
            with patch("app.storage.cloud_preflight_gate.check", side_effect=lambda _connect, probe: (probe(), {"status": "ready"})[1]), patch("app.storage.s3_client", return_value=client), patch("app.storage.settings") as settings:
                settings.aws_region = "eu-south-1"
                if succeeds:
                    validate_cloud_vault({"s3_bucket": "my-archive-bucket"})
                else:
                    with self.assertRaisesRegex(RuntimeError, "Block Public Access"):
                        validate_cloud_vault({"s3_bucket": "my-archive-bucket"})
