"""The S3 module's error codes, named sets and fork handler (#792).

No network and no database. The clients are covered by ``tests.test_s3_clients``,
the requests the worker sends by ``tests.test_s3_call_sequence``.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from app.services import s3


def client_error(error: dict | None, operation: str = "HeadObject") -> ClientError:
    return ClientError({} if error is None else {"Error": error}, operation)


class ErrorCodeTests(unittest.TestCase):
    def test_the_code_of_a_client_error_and_nothing_of_any_other_error(self) -> None:
        self.assertEqual(s3.error_code(client_error({"Code": "NoSuchKey", "Message": "provider text"})),
                         "NoSuchKey")
        self.assertEqual(s3.error_code(client_error({"Code": 404})), "404")
        for absent in (client_error(None), client_error({}), client_error({"Code": None}),
                       client_error({"Code": ""}), RuntimeError("NoSuchKey"), ConnectionError()):
            with self.subTest(absent=absent):
                self.assertEqual(s3.error_code(absent), "")

    def test_missing_names_only_an_answer_without_a_code(self) -> None:
        self.assertEqual(s3.error_code(client_error({}), missing="ClientError"), "ClientError")
        self.assertEqual(s3.error_code(client_error(None), missing="ClientError"), "ClientError")
        self.assertEqual(s3.error_code(client_error({"Code": ""}), missing="ClientError"), "")
        self.assertEqual(s3.error_code(client_error({"Code": "AccessDenied"}), missing="ClientError"),
                         "AccessDenied")

    def test_a_safe_code_is_kept_only_when_it_looks_like_one(self) -> None:
        self.assertEqual(s3.safe_error_code(client_error({"Code": "AccessDenied"})), "AccessDenied")
        for unsafe in ("", "has spaces", "x" * 65, "<script>", None):
            with self.subTest(unsafe=unsafe):
                self.assertEqual(s3.safe_error_code(client_error({"Code": unsafe})), "ClientError")

    def test_the_named_sets(self) -> None:
        self.assertEqual(s3.GONE_CODES, {"NoSuchKey", "NoSuchVersion", "NotFound", "404"})
        self.assertEqual(s3.ACCESS_DENIED_CODES, {"AccessDenied", "AccessDeniedException"})
        self.assertEqual(s3.CREDENTIAL_CODES, {
            "SignatureDoesNotMatch", "InvalidAccessKeyId", "InvalidClientTokenId",
            "UnrecognizedClientException", "ExpiredToken", "InvalidToken", "TokenRefreshRequired"})
        self.assertEqual(s3.TRANSIENT_CODES, {
            "SlowDown", "Throttling", "ThrottlingException", "ThrottledException",
            "TooManyRequestsException", "RequestLimitExceeded", "RequestThrottled",
            "BandwidthLimitExceeded", "RequestTimeout", "RequestTimeoutException", "InternalError",
            "ServiceUnavailable"})
        self.assertEqual(s3.CHECKSUM_UNSUPPORTED_CODES,
                         {"NotImplemented", "UnsupportedOperation", "XNotImplemented", "501"})

    def test_a_version_is_already_gone_only_by_its_code(self) -> None:
        for code in s3.GONE_CODES:
            self.assertTrue(s3.version_already_gone(client_error({"Code": code})))
        self.assertFalse(s3.version_already_gone(client_error({"Code": "AccessDenied"})))
        self.assertFalse(s3.version_already_gone(KeyError("NoSuchKey")))


class _Head:
    """A provider that refuses the checksum HEAD with ``failure``."""

    def __init__(self, failure: BaseException | None) -> None:
        self.failure, self.requests = failure, []

    def head_object(self, **kwargs):
        self.requests.append(kwargs)
        if "ChecksumMode" in kwargs and self.failure is not None:
            raise self.failure
        return {"VersionId": kwargs["VersionId"]}

    def delete_object(self, **kwargs):
        self.requests.append(kwargs)
        raise self.failure


class ExactVersionTests(unittest.TestCase):
    def test_the_lenient_checksum_head_falls_back_on_any_failure(self) -> None:
        # Copies: even an error that says nothing about the checksum API.
        for failure in (None, client_error({"Code": "NotImplemented"}),
                        client_error({"Code": "AccessDenied"}), RuntimeError("reset")):
            with self.subTest(failure=failure):
                provider = _Head(failure)
                head = s3.head_object_with_checksum(provider, Bucket="b", Key="k", VersionId="v")
                self.assertEqual(head, {"VersionId": "v"})
                plain = {"Bucket": "b", "Key": "k", "VersionId": "v"}
                self.assertEqual(provider.requests, [{**plain, "ChecksumMode": "ENABLED"}]
                                 + ([plain] if failure is not None else []))

    def test_the_strict_policy_names_only_capability_errors(self) -> None:
        # Uploads: the caller sends its own HEAD and asks about the failure.
        self.assertTrue(s3.checksum_mode_unsupported(client_error({"Code": "NotImplemented"})))
        self.assertTrue(s3.checksum_mode_unsupported(
            client_error({"Code": "InvalidArgument", "ArgumentName": "ChecksumMode"})))
        self.assertFalse(s3.checksum_mode_unsupported(client_error({"Code": "InvalidArgument"})))
        self.assertFalse(s3.checksum_mode_unsupported(client_error({"Code": "AccessDenied"})))
        self.assertFalse(s3.checksum_mode_unsupported(client_error({"Code": "NotImplemented"}, "PutObject")))
        self.assertFalse(s3.checksum_mode_unsupported(RuntimeError("reset")))

    def test_deleting_a_version_already_gone_is_done(self) -> None:
        provider = _Head(client_error({"Code": "NoSuchVersion"}, "DeleteObject"))
        s3.delete_version(provider, bucket="b", object_key="k", version_id="v")
        self.assertEqual(provider.requests, [{"Bucket": "b", "Key": "k", "VersionId": "v"}])
        refused = _Head(client_error({"Code": "AccessDenied"}, "DeleteObject"))
        with self.assertRaises(ClientError):
            s3.delete_version(refused, bucket="b", object_key="k", version_id="v")


class ForkHandlerTests(unittest.TestCase):
    def test_one_handler_forgets_the_clients_then_runs_the_registered_resets(self) -> None:
        from app.services import s3_upload
        self.assertIn(s3_upload._reset_transport_after_fork, s3._fork_resets)
        order: list[str] = []
        with patch.object(s3, "_fork_resets", [lambda: order.append(f"first:{len(s3._clients)}"),
                                               lambda: order.append("second")]), \
                patch.object(s3, "_clients", s3._clients.__class__({"key": object()})), \
                patch.multiple(s3, _lock=s3._lock, _live_clients=s3._live_clients,
                               _upload_setup_lock=s3._upload_setup_lock, _upload_ready=s3._upload_ready,
                               _inherited=[]):
            s3._after_fork()
            self.assertEqual(order, ["first:0", "second"])

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX fork safety")
    def test_a_forked_child_starts_with_a_fresh_upload_transport(self) -> None:
        from app.services import s3_upload
        with patch.dict(s3_upload._budgets, {1: object()}):
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    fresh = (s3_upload._budgets == {} and s3_upload._small_executor is None
                             and len(s3._clients) == 0)
                    os.write(write_fd, b"ok" if fresh else b"bad")
                except BaseException:
                    os.write(write_fd, b"error")
                finally:
                    os.close(write_fd)
                    os._exit(0)
            os.close(write_fd)
            try:
                self.assertEqual(os.read(read_fd, 20), b"ok")
            finally:
                os.close(read_fd)
                os.waitpid(pid, 0)
            self.assertIn(1, s3_upload._budgets)


if __name__ == "__main__":
    unittest.main()
