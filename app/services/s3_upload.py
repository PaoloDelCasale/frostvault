"""Managed S3 uploads with a process-wide bandwidth budget and response identity.

The pinned s3transfer release discards PutObject/CompleteMultipartUpload results.
A per-transfer client facade captures them, never inferring identity from HEAD.
CRC32 supports FULL_OBJECT multipart checksums (SHA256 is composite in S3).
"""
from __future__ import annotations

import base64
import threading
import zlib
from pathlib import Path
from typing import Any, Callable

from boto3.s3.transfer import TransferConfig
from s3transfer.bandwidth import BandwidthLimiter, LeakyBucket
from s3transfer.manager import TransferManager
from s3transfer.subscribers import BaseSubscriber

_budget_lock = threading.Lock()
_budgets: dict[int, BandwidthLimiter] = {}


def _shared_budget(rate: int) -> BandwidthLimiter:
    with _budget_lock:
        if rate not in _budgets:
            _budgets[rate] = BandwidthLimiter(LeakyBucket(rate))
        return _budgets[rate]


class _UploadClient:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.response: dict[str, Any] | None = None
        self.pending: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.response = self.client.put_object(**kwargs)
        return self.response

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        result = self.client.create_multipart_upload(**kwargs)
        self.pending = {"Bucket": kwargs["Bucket"], "Key": kwargs["Key"], "UploadId": result["UploadId"]}
        return result

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.response = self.client.complete_multipart_upload(**kwargs)
        self.pending = None
        return self.response

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        result = self.client.abort_multipart_upload(**kwargs)
        self.pending = None
        return result


class _Progress(BaseSubscriber):
    def __init__(self, callback: Callable[[int], None]) -> None:
        self.callback = callback

    def on_progress(self, future: Any, bytes_transferred: int, **kwargs: Any) -> None:
        self.callback(bytes_transferred)


def upload_file(
    client: Any, *, source: Path, bucket: str, key: str,
    check_active: Callable[[], None], on_progress: Callable[[int], None],
    max_bandwidth: int | None = None,
) -> dict[str, Any]:
    """Return the upload response or fail closed, aborting incomplete multipart.

    Cancellation is checked by the worker, not only SDK callbacks: a stalled
    socket or a bandwidth-limited stream must not hide a cancelled/lost Job.
    Manager shutdown joins in-flight parts before the explicit abort fallback.
    Completed versions are NEVER deleted here, even after cancellation.
    """
    check_active()
    crc = 0
    before = source.stat()
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_active()
            crc = zlib.crc32(chunk, crc)
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise RuntimeError("Local file changed since fingerprinting")
    checksum = base64.b64encode(crc.to_bytes(4, "big")).decode("ascii")
    facade = _UploadClient(client)
    config = TransferConfig(max_bandwidth=max_bandwidth, preferred_transfer_client="classic")
    manager = TransferManager(facade, config)
    if max_bandwidth:
        # One limiter across managers, not one cap per concurrent Job. This
        # narrow private seam is covered by targeted tests against pinned SDK.
        manager._bandwidth_limiter = _shared_budget(max_bandwidth)
    future = None
    failed = True
    try:
        future = manager.upload(
            str(source), bucket, key,
            extra_args={"ChecksumAlgorithm": "CRC32", "ChecksumType": "FULL_OBJECT",
                        "ChecksumCRC32": checksum},
            subscribers=[_Progress(on_progress)],
        )
        waiter = threading.Event()
        while not future.done():
            check_active()
            waiter.wait(0.1)
        future.result()
        check_active()
        response = facade.response
        if not response or not response.get("VersionId") or response["VersionId"] == "null":
            raise RuntimeError("Upload stored without an S3 VersionId; bucket Versioning is required")
        if response.get("ChecksumCRC32") != checksum or response.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT":
            raise RuntimeError("S3 did not confirm the FULL_OBJECT upload checksum")
        failed = False
        return response
    finally:
        if failed and future is not None:
            future.cancel()
        try:
            manager.shutdown()
        finally:
            if facade.pending is not None:
                # Explicit fallback if SDK cleanup failed/interruption raced
                # initiation. Lifecycle also reaps uploads after process death.
                facade.abort_multipart_upload(**facade.pending)
