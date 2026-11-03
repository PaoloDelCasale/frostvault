"""Upload failure classification and retry policy (issue #11)."""

from __future__ import annotations

import unittest

from app.storage import (
    UPLOAD_RETRY_MAX_ATTEMPTS,
    classify_upload_failure,
    upload_retry_delay_seconds,
)


class UploadRetryPolicyTests(unittest.TestCase):
    def test_integrity_errors_are_not_retryable(self) -> None:
        for message in (
            "Cloud copy digest does not match local file",
            "S3 did not create the verification copy",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_upload_failure(message), "permanent")

    def test_source_mutation_is_rescheduled(self) -> None:
        self.assertEqual(
            classify_upload_failure("Local file changed since fingerprinting"),
            "source_changed",
        )

    def test_permission_and_configuration_errors_are_not_retryable(self) -> None:
        for message in (
            "AccessDenied: not authorized",
            "AWS credentials are not configured in the .env file",
            "Upload stored without an S3 VersionId; bucket Versioning is required",
            "InvalidAccessKeyId",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_upload_failure(message), "permanent")

    def test_transient_transport_errors_are_retryable(self) -> None:
        for message in (
            "SlowDown: Please reduce your request rate",
            "Service Unavailable",
            "RequestTimeout",
            "connection reset by peer",
            "Temporary failure in name resolution",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_upload_failure(message), "transient")

    def test_sdk_endpoint_and_interrupted_body_failures_keep_job_retries(self) -> None:
        from botocore.exceptions import EndpointConnectionError, IncompleteReadError, ResponseStreamingError
        for error in (EndpointConnectionError(endpoint_url="https://example.invalid"),
                      IncompleteReadError(actual_bytes=3, expected_bytes=10),
                      ResponseStreamingError(error="connection interrupted")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(classify_upload_failure(str(error)), "transient")

    def test_source_mutation_retries_are_bounded(self) -> None:
        self.assertGreaterEqual(UPLOAD_RETRY_MAX_ATTEMPTS, 1)

    def test_retry_delay_grows_exponentially_up_to_a_cap(self) -> None:
        delays = [upload_retry_delay_seconds(attempt) for attempt in range(1, 6)]
        self.assertEqual(delays[0], 2)
        self.assertEqual(delays[1], 4)
        self.assertEqual(delays[2], 8)
        self.assertEqual(delays[3], 16)
        self.assertEqual(delays[4], 32)
        self.assertEqual(upload_retry_delay_seconds(20), 300)


if __name__ == "__main__":
    unittest.main()
