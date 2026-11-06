"""Managed S3 uploads with a process-wide bandwidth budget and response identity.

The pinned s3transfer release discards PutObject/CompleteMultipartUpload results.
A per-transfer client facade captures them, never inferring identity from HEAD.
CRC32 supports FULL_OBJECT multipart checksums (SHA256 is composite in S3).
"""
from __future__ import annotations

import base64
import logging
import io
import hashlib
import threading
import zlib
from pathlib import Path
from typing import Any, Callable

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from s3transfer.bandwidth import BandwidthLimiter, LeakyBucket
from s3transfer.manager import TransferManager
from s3transfer.subscribers import BaseSubscriber
from s3transfer.upload import InterruptReader
from s3transfer.utils import ReadFileChunk

from .upload_snapshot import UploadSnapshot, CHUNK_SIZE, MULTIPART_THRESHOLD

_budget_lock = threading.Lock()
_budgets: dict[int, BandwidthLimiter] = {}


def _shared_budget(rate: int) -> BandwidthLimiter:
    with _budget_lock:
        if rate not in _budgets:
            _budgets[rate] = BandwidthLimiter(LeakyBucket(rate))
        return _budgets[rate]


class _UploadClient:
    def __init__(self, client: Any, *, snapshot: UploadSnapshot | None = None,
                 check_active: Callable[[], None] = lambda: None,
                 max_bandwidth: int | None = None,
                 on_acknowledged: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.client = client
        self.snapshot = snapshot
        self.check_active = check_active
        self.max_bandwidth = max_bandwidth
        self.on_acknowledged = on_acknowledged
        self.response: dict[str, Any] | None = None
        self.pending: dict[str, Any] | None = None

    def _acknowledge(self, response: dict[str, Any]) -> dict[str, Any]:
        # Raw response identity is available before cancellation/proof/HEAD or
        # any worker callback can throw. #464 can journal it via this hook.
        self.response = response
        if self.on_acknowledged is not None:
            self.on_acknowledged(dict(response))
        return response

    def _validated_body(self, body: Any, number: int) -> ReadFileChunk:
        snapshot = self.snapshot
        assert snapshot is not None
        length = snapshot.part_length(number)
        if len(body) != length:
            raise RuntimeError("Local file changed since fingerprinting")
        # Pinned classic filename bodies are ReadFileChunk -> [bandwidth] ->
        # InterruptReader. Preserve cancellation, shared budget, progress and
        # retry seek behavior, but send only an independently validated buffer.
        body.signal_not_transferring()
        digest = hashlib.sha256()
        buffer = io.BytesIO()
        try:
            left = length
            while left:
                self.check_active()
                chunk = body.read(min(CHUNK_SIZE, left))
                if not chunk:
                    raise RuntimeError("Local file changed since fingerprinting")
                buffer.write(chunk)
                digest.update(chunk)
                left -= len(chunk)
            if base64.b64encode(digest.digest()).decode("ascii") != snapshot.part_checksums[number - 1]:
                raise RuntimeError("Local file changed since fingerprinting")
            self.check_active()
            buffer.seek(0)
            original = body._fileobj
            if self.max_bandwidth:
                original = original._fileobj
            coordinator = original._transfer_coordinator
            wrapped = InterruptReader(buffer, coordinator)
            if self.max_bandwidth:
                wrapped = _shared_budget(self.max_bandwidth).get_bandwith_limited_stream(
                    wrapped, coordinator, enabled=False,
                )
            return ReadFileChunk(wrapped, length, length, callbacks=body._callbacks,
                                 enable_callbacks=False, close_callbacks=body._close_callbacks)
        except BaseException:
            buffer.close()
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.snapshot is not None:
            kwargs["ChecksumAlgorithm"] = "SHA256"
            kwargs["ChecksumSHA256"] = self.snapshot.checksum_sha256
            with self._validated_body(kwargs["Body"], 1) as body:
                kwargs["Body"] = body
                return self._acknowledge(self.client.put_object(**kwargs))
        return self._acknowledge(self.client.put_object(**kwargs))

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        if self.snapshot is None:
            return self.client.upload_part(**kwargs)
        number = int(kwargs["PartNumber"])
        expected = self.snapshot.part_checksums[number - 1]
        kwargs["ChecksumAlgorithm"] = "SHA256"
        kwargs["ChecksumSHA256"] = expected
        with self._validated_body(kwargs["Body"], number) as body:
            kwargs["Body"] = body
            result = self.client.upload_part(**kwargs)
        if result.get("ChecksumSHA256") is not None and result["ChecksumSHA256"] != expected:
            raise RuntimeError("S3 upload part checksum mismatch")
        # Completion carries OUR independently computed checksums, never an
        # expected checksum reconstructed from provider responses.
        return {**result, "ChecksumSHA256": expected}

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        if self.snapshot is not None:
            kwargs["ChecksumAlgorithm"] = "SHA256"
            kwargs["ChecksumType"] = "COMPOSITE"
        result = self.client.create_multipart_upload(**kwargs)
        self.pending = {"Bucket": kwargs["Bucket"], "Key": kwargs["Key"], "UploadId": result["UploadId"]}
        return result

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        if self.snapshot is not None:
            kwargs["ChecksumType"] = "COMPOSITE"
            kwargs["ChecksumSHA256"] = self.snapshot.checksum_sha256
            kwargs["MpuObjectSize"] = self.snapshot.size
            parts = kwargs["MultipartUpload"]["Parts"]
            if [part["PartNumber"] for part in parts] != list(range(1, len(self.snapshot.part_checksums) + 1)):
                raise RuntimeError("Upload Snapshot completion layout mismatch")
            for part, checksum in zip(parts, self.snapshot.part_checksums):
                part["ChecksumSHA256"] = checksum
        response = self.client.complete_multipart_upload(**kwargs)
        self.pending = None
        return self._acknowledge(response)

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        try:
            result = self.client.abort_multipart_upload(**kwargs)
        except ClientError as exc:
            # Completion may have succeeded but its response was interrupted.
            # Never delete a completed object to compensate an ambiguous result.
            if exc.response.get("Error", {}).get("Code") != "NoSuchUpload":
                raise
            result = {}
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
    snapshot: UploadSnapshot | None = None,
    metadata: dict[str, str] | None = None,
    on_acknowledged: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Return the upload response or fail closed, aborting incomplete multipart.

    Cancellation is checked by the worker, not only SDK callbacks: a stalled
    socket or a bandwidth-limited stream must not hide a cancelled/lost Job.
    Manager shutdown joins in-flight parts before the explicit abort fallback.
    Completed versions are NEVER deleted here, even after cancellation.
    """
    check_active()
    checksum = None
    if snapshot is None:
        # Shared rename/copy/recovery callers retain the pre-existing CRC32
        # transport contract and their independent full verification behavior.
        crc = 0
        before = source.stat()
        with source.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                check_active()
                crc = zlib.crc32(chunk, crc)
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise RuntimeError("Local file changed since fingerprinting")
        checksum = base64.b64encode(crc.to_bytes(4, "big")).decode("ascii")
    else:
        UploadSnapshot.from_json(snapshot.as_json())
        if source.stat(follow_symlinks=False).st_size != snapshot.size:
            raise RuntimeError("Local file changed since fingerprinting")
    facade = _UploadClient(client, snapshot=snapshot, check_active=check_active,
                           max_bandwidth=max_bandwidth, on_acknowledged=on_acknowledged)
    config = TransferConfig(max_bandwidth=max_bandwidth, preferred_transfer_client="classic")
    if snapshot is not None:
        config.multipart_threshold = MULTIPART_THRESHOLD
        config.multipart_chunksize = snapshot.part_size
        # Hard bounded buffers: at most two parts per upload, no whole-file
        # materialization, plus <=10,000 small independent part digests.
        config.max_request_concurrency = min(config.max_request_concurrency, 2)
    manager = TransferManager(facade, config)
    if max_bandwidth:
        # One limiter across managers, not one cap per concurrent Job. This
        # narrow private seam is covered by targeted tests against pinned SDK.
        manager._bandwidth_limiter = _shared_budget(max_bandwidth)
    future = None
    failed = True
    failure: BaseException | None = None
    try:
        extra_args = ({"ChecksumAlgorithm": "SHA256", "ChecksumType": snapshot.checksum_type}
                      if snapshot is not None else
                      {"ChecksumAlgorithm": "CRC32", "ChecksumType": "FULL_OBJECT", "ChecksumCRC32": checksum})
        # Do not pass ChecksumSHA256 to manager.upload: pinned s3transfer forces
        # FULL_OBJECT for explicit checksums. The facade sets PUT/part/completion
        # fields at their distinct API boundaries instead.
        if metadata is not None:
            extra_args["Metadata"] = dict(metadata)
        future = manager.upload(str(source), bucket, key, extra_args=extra_args,
                                subscribers=[_Progress(on_progress)])
        waiter = threading.Event()
        while not future.done():
            check_active()
            waiter.wait(0.1)
        future.result()
        check_active()
        response = facade.response
        if not response or not response.get("VersionId") or response["VersionId"] == "null":
            raise RuntimeError("Upload stored without an S3 VersionId; bucket Versioning is required")
        if snapshot is not None:
            # Evidence is evaluated by the worker only AFTER catalog linkage.
            failed = False
            return response
        proof = response
        if not proof.get("ChecksumCRC32"):
            # Some compatible servers omit checksums in completion responses.
            # Read ONLY the response VersionId's independently computed checksum;
            # never discover identity from HEAD or accept size/ETag as integrity.
            proof = client.head_object(Bucket=bucket, Key=key,
                                       VersionId=response["VersionId"], ChecksumMode="ENABLED")
            if str(proof.get("VersionId") or "") != str(response["VersionId"]):
                raise RuntimeError("S3 checksum proof returned a different VersionId")
            if proof.get("ChecksumType") != "FULL_OBJECT":
                raise RuntimeError("S3 did not confirm the FULL_OBJECT upload checksum")
        if proof.get("ChecksumCRC32") != checksum or proof.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT":
            raise RuntimeError("S3 did not confirm the FULL_OBJECT upload checksum")
        failed = False
        return response
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            if failed and future is not None:
                future.cancel()
            try:
                manager.shutdown()
            finally:
                if facade.pending is not None:
                    # Explicit fallback if SDK cleanup failed/interruption raced
                    # initiation. Lifecycle also reaps uploads after process death.
                    try:
                        facade.abort_multipart_upload(**facade.pending)
                    except Exception:
                        # Never log provider messages, keys or credentials.
                        logging.getLogger(__name__).warning(
                            "Incomplete multipart abort failed; lifecycle cleanup is required"
                        )
        except BaseException as cleanup_error:
            if failure is None:
                failure = cleanup_error
                raise
            # A cleanup error must not replace the original classification.
            logging.getLogger(__name__).warning(
                "Upload manager shutdown failed; original transfer error retained"
            )
        finally:
            # shutdown joins in-flight requests: their acknowledgments can arrive
            # AFTER cancellation was raised. Attach the final receipt only now,
            # keeping the original exception object and retry classification.
            if failure is not None and facade.response is not None:
                failure.upload_response = dict(facade.response)
