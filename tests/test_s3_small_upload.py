"""Small Upload Snapshot transport seam: no manager, real bounded stream semantics."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.s3_upload import upload_file
from app.services.upload_snapshot import prepare_upload_snapshot
from app.storage import OperationCancelled


class SmallUploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / "content"
        self.source.write_bytes(b"snapshot A")
        self.snapshot = prepare_upload_snapshot(self.source, lambda: None)[0]

    def upload(self, client, **kwargs):
        return upload_file(client, source=self.source, bucket="bucket", key="key",
                           snapshot=self.snapshot,
                           check_active=kwargs.pop("check_active", lambda: None),
                           on_progress=kwargs.pop("on_progress", lambda _: None), **kwargs)

    def test_small_put_has_no_manager_and_retains_progress_rewind_metadata_and_exact_ack(self):
        progress = []
        acknowledgments = []
        def put(**kwargs):
            self.assertEqual(kwargs["Metadata"], {"opaque": "token"})
            self.assertEqual(kwargs["ChecksumSHA256"], self.snapshot.checksum_sha256)
            body = kwargs["Body"]
            self.assertEqual(body.read(4), b"snap")
            body.seek(0)
            self.assertEqual(body.read(), b"snapshot A")
            return {"VersionId": "exact", "ChecksumSHA256": self.snapshot.checksum_sha256}
        with patch("app.services.s3_upload.TransferManager", side_effect=AssertionError("small PUT needs no manager")):
            response = self.upload(SimpleNamespace(put_object=put), metadata={"opaque": "token"},
                                   on_progress=progress.append, on_acknowledged=acknowledgments.append)
        self.assertEqual(response["VersionId"], "exact")
        self.assertEqual(sum(progress), len(b"snapshot A"))
        self.assertIn(-4, progress)
        self.assertEqual(acknowledgments, [response])

    def test_blocked_socket_does_not_hide_cancel_and_late_receipt_is_joined(self):
        entered, noticed, release, finished = (threading.Event() for _ in range(4))
        cancelled = OperationCancelled("lost claim while socket blocked")
        acknowledgments, errors = [], []
        def put(**kwargs):
            kwargs["Body"].read()
            entered.set()
            if not release.wait(5):
                raise AssertionError("provider was never released")
            return {"VersionId": "late", "ChecksumSHA256": "wrong"}
        def active():
            if entered.is_set():
                noticed.set()
                raise cancelled
        def run():
            try:
                self.upload(SimpleNamespace(put_object=put), check_active=active,
                            on_acknowledged=acknowledgments.append)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(noticed.wait(2), "cancel check hidden by blocked socket")
            self.assertFalse(finished.is_set(), "returned before joining in-flight request")
            self.assertEqual(acknowledgments, [])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [cancelled])
        self.assertEqual(acknowledgments, [{"VersionId": "late", "ChecksumSHA256": "wrong"}])
        self.assertEqual(cancelled.upload_response, acknowledgments[0])
        self.assertFalse(hasattr(cancelled, "upload_no_write_operation"))

    def test_receipt_precedes_missing_version_and_ack_callback_errors(self):
        for response, hook_error in (({}, None), ({"VersionId": "exact"}, RuntimeError("journal unavailable"))):
            ack = Mock(side_effect=hook_error)
            with self.subTest(response=response), self.assertRaises(RuntimeError) as caught:
                self.upload(SimpleNamespace(put_object=lambda **_: dict(response)), on_acknowledged=ack)
            ack.assert_called_once_with(response)
            self.assertEqual(caught.exception.upload_response, response)
            if hook_error is not None:
                self.assertIs(caught.exception, hook_error)

    def test_permission_and_lost_response_are_not_blindly_retried(self):
        from botocore.exceptions import ClientError, ReadTimeoutError
        for error, no_write in ((ClientError({"Error": {"Code": "AccessDenied"},
                                             "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject"), True),
                                (ReadTimeoutError(endpoint_url="http://127.0.0.1:19000"), False)):
            put, ack = Mock(side_effect=error), Mock()
            with self.subTest(no_write=no_write), self.assertRaises(type(error)) as caught:
                self.upload(SimpleNamespace(put_object=put), on_acknowledged=ack)
            self.assertIs(caught.exception, error)
            put.assert_called_once()
            ack.assert_not_called()
            self.assertFalse(hasattr(error, "upload_response"))
            self.assertEqual(getattr(error, "upload_no_write_operation", None), "PutObject" if no_write else None)

    def test_close_bandwidth_error_after_response_keeps_ack_and_cancel_observer_live(self):
        # For a sub-threshold body the SDK limiter charges bytes at close().
        # A low cap deliberately keeps that close blocked after acknowledgment.
        acknowledged, noticed = threading.Event(), threading.Event()
        cancelled = OperationCancelled("cancel during bandwidth accounting")
        ack = []
        def put(**kwargs):
            self.assertEqual(kwargs["Body"].read(), b"snapshot A")
            return {"VersionId": "exact"}
        def received(response):
            ack.append(response)
            acknowledged.set()
        def active():
            if acknowledged.is_set():
                noticed.set()
                raise cancelled
        # First use warms the shared bucket; the second must wait for its cap.
        import time
        provider = SimpleNamespace(put_object=put)
        self.upload(provider, max_bandwidth=2)
        start = time.monotonic()
        with self.assertRaises(OperationCancelled) as caught:
            self.upload(provider, max_bandwidth=2, check_active=active, on_acknowledged=received)
        self.assertLess(time.monotonic() - start, 2, "cancel must interrupt a five-second limiter sleep")
        self.assertIs(caught.exception, cancelled)
        self.assertTrue(noticed.is_set())
        self.assertEqual(cancelled.upload_response, {"VersionId": "exact"})
        self.assertEqual(ack, [{"VersionId": "exact"}])

    def test_concurrent_uploads_share_one_bandwidth_cap(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        # Both sub-threshold bodies are accounted at close. An independent cap
        # per transfer would allow both first consumes without any wait.
        barrier = threading.Barrier(2)
        def put(**kwargs):
            kwargs["Body"].read()
            barrier.wait(timeout=2)
            return {"VersionId": "exact"}
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as callers:
            futures = [callers.submit(self.upload, SimpleNamespace(put_object=put), max_bandwidth=20) for _ in range(2)]
            self.assertEqual([future.result()["VersionId"] for future in futures], ["exact", "exact"])
        self.assertGreaterEqual(time.monotonic() - start, 0.4)

    def test_process_wide_inflight_budget_applies_backpressure_before_buffering_or_send(self):
        from concurrent.futures import ThreadPoolExecutor
        all_entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        entered = 0
        def put(**kwargs):
            nonlocal entered
            with lock:
                entered += 1
                if entered == 32:
                    all_entered.set()
            if not release.wait(5):
                raise AssertionError("provider was never released")
            return {"VersionId": "exact"}
        with ThreadPoolExecutor(max_workers=32) as callers:
            futures = [callers.submit(self.upload, SimpleNamespace(put_object=put)) for _ in range(32)]
            try:
                self.assertTrue(all_entered.wait(3))
                checks = 0
                def cancel_waiter():
                    nonlocal checks
                    checks += 1
                    if checks > 1:
                        raise OperationCancelled("waiting for transport capacity")
                waiting_put = Mock()
                with self.assertRaises(OperationCancelled):
                    self.upload(SimpleNamespace(put_object=waiting_put), check_active=cancel_waiter)
                waiting_put.assert_not_called()
                self.assertEqual(entered, 32)
            finally:
                release.set()
                for future in futures:
                    self.assertEqual(future.result(timeout=5)["VersionId"], "exact")
        # Capacity is returned, including by the cancelled waiting caller.
        self.assertEqual(self.upload(SimpleNamespace(put_object=lambda **_: {"VersionId": "next"}))["VersionId"], "next")

    def test_invalid_bandwidth_never_starts_a_put(self):
        for rate in (0, -1):
            put = Mock()
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.upload(SimpleNamespace(put_object=put), max_bandwidth=rate)
            put.assert_not_called()

    def test_fifo_replacement_between_lstat_and_open_never_blocks_or_sends(self):
        import os
        real_open = os.open
        def race(path, flags, *args, **kwargs):
            if str(path) == str(self.source):
                self.source.unlink()
                os.mkfifo(self.source)
                if not flags & os.O_NONBLOCK:
                    raise AssertionError("blocking open would hang on replacement FIFO")
            return real_open(path, flags, *args, **kwargs)
        for stage in ("prepare", "transport"):
            with self.subTest(stage=stage):
                put = Mock()
                with patch("os.open", side_effect=race), self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                    if stage == "prepare":
                        prepare_upload_snapshot(self.source, lambda: None)
                    else:
                        self.upload(SimpleNamespace(put_object=put))
                put.assert_not_called()
                self.source.unlink()
                self.source.write_bytes(b"snapshot A")

    def test_source_replacement_with_symlink_or_fifo_is_rejected_without_send(self):
        import os
        target = self.source.with_name("other")
        target.write_bytes(b"snapshot A")
        for kind in ("symlink", "fifo"):
            self.source.unlink()
            if kind == "symlink":
                self.source.symlink_to(target)
            else:
                os.mkfifo(self.source)
            put = Mock()
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                self.upload(SimpleNamespace(put_object=put))
            put.assert_not_called()
