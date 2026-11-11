"""Managed S3 uploads with a process-wide bandwidth budget and response identity.

The pinned s3transfer release discards PutObject/CompleteMultipartUpload results.
A per-transfer client facade captures them, never inferring identity from HEAD.
CRC32 supports FULL_OBJECT multipart checksums (SHA256 is composite in S3).
"""
from __future__ import annotations

import atexit
import base64
import logging
import io
import hashlib
import os
import stat
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from botocore.client import BaseClient
from botocore.hooks import HierarchicalEmitter
from s3transfer.bandwidth import BandwidthLimiter, BandwidthLimitedStream, LeakyBucket, TimeUtils
from s3transfer.futures import TransferCoordinator
from s3transfer.manager import TransferManager
from s3transfer.subscribers import BaseSubscriber
from s3transfer.upload import InterruptReader
from s3transfer.utils import OSUtils, ReadFileChunk

from .s3_clients import configure_upload_client
from .upload_snapshot import (
    UploadSnapshot, CHUNK_SIZE, MULTIPART_THRESHOLD, validated_snapshot_buffer,
)

_budget_lock = threading.Lock()
_budgets: dict[int, BandwidthLimiter] = {}

# Reserve BEFORE allocating a body or submitting work. Thus both the executor's
# queue and the live buffers are bounded, even with arbitrarily many callers.
# At most 32 buffers <8 MiB (<256 MiB total); no source FD survives validation.
_SMALL_UPLOAD_SLOTS = 32
_small_slots = threading.BoundedSemaphore(_SMALL_UPLOAD_SLOTS)
_small_lock = threading.Lock()
_small_executor: ThreadPoolExecutor | None = None
_inherited_transport: list[Any] = []


def _after_fork() -> None:
    global _budget_lock, _budgets, _small_slots, _small_lock, _small_executor
    _inherited_transport.append((_small_executor, _budgets))
    _budget_lock = threading.Lock()
    _budgets = {}
    _small_slots = threading.BoundedSemaphore(_SMALL_UPLOAD_SLOTS)
    _small_lock = threading.Lock()
    _small_executor = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _small_pool() -> ThreadPoolExecutor:
    global _small_executor
    with _small_lock:
        if _small_executor is None:
            _small_executor = ThreadPoolExecutor(max_workers=_SMALL_UPLOAD_SLOTS,
                                                 thread_name_prefix="s3-small-put")
        return _small_executor


def shutdown_upload_transport() -> None:
    """Drain the process-wide small-PUT executor (after upload callers drain)."""
    global _small_executor
    with _small_lock:
        executor, _small_executor = _small_executor, None
    if executor is not None:
        executor.shutdown(wait=True)


atexit.register(shutdown_upload_transport)


class _BandwidthSchedule:
    """SDK ConsumptionScheduler protocol, without retaining dead stream tokens.

    LeakyBucket serializes these methods. Its default scheduler otherwise keeps
    a cancelled stream's reservation forever, accumulating memory and wait time.
    """
    def __init__(self) -> None:
        self.pending: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def is_scheduled(self, token: Any) -> bool:
        return token in self.pending

    def schedule_consumption(self, amt: int, token: Any, time_to_consume: float) -> float:
        wait = sum(self.pending.values()) + time_to_consume
        self.pending[token] = time_to_consume
        return wait

    def process_scheduled_consumption(self, token: Any) -> None:
        self.pending.pop(token, None)


class _CancellableTime(TimeUtils):
    def __init__(self, coordinator: TransferCoordinator) -> None:
        self.coordinator = coordinator

    def sleep(self, value: float) -> None:
        deadline = time.monotonic() + value
        while True:
            if self.coordinator.exception is not None:
                raise self.coordinator.exception
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.05))


class _SharedLimiter(BandwidthLimiter):
    def __init__(self, rate: int) -> None:
        self.bucket = LeakyBucket(rate, consumption_scheduler=_BandwidthSchedule())
        super().__init__(self.bucket)

    def get_bandwith_limited_stream(self, fileobj: Any, transfer_coordinator: TransferCoordinator,
                                   enabled: bool = True) -> BandwidthLimitedStream:
        # Public SDK extension points only: no mutation of a shared limiter's
        # clock/coordinator and no transfer-specific state on shared clients.
        stream = BandwidthLimitedStream(fileobj, self.bucket, transfer_coordinator,
                                        time_utils=_CancellableTime(transfer_coordinator))
        if not enabled:
            stream.disable_bandwidth_limiting()
        return stream


def _shared_budget(rate: int) -> BandwidthLimiter:
    with _budget_lock:
        if rate not in _budgets:
            _budgets[rate] = _SharedLimiter(rate)
        return _budgets[rate]


class _SnapshotOSUtils(OSUtils):
    def open(self, filename: str, mode: str):
        # A filename can become a pipe/symlink between the caller's lstat and a
        # deferred SDK open. Never follow it or block before cancellation exists.
        if mode != "rb":
            raise ValueError("Upload Snapshot sources are read-only")
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(filename, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("Local file changed since fingerprinting")
            return os.fdopen(fd, mode)
        except BaseException:
            os.close(fd)
            raise


class _UploadClient:
    def __init__(self, client: Any, *, snapshot: UploadSnapshot | None = None,
                 check_active: Callable[[], None] = lambda: None,
                 max_bandwidth: int | None = None,
                 on_acknowledged: Callable[[dict[str, Any]], None] | None = None,
                 write_client: Any = None,
                 on_multipart: Callable[[str, str], None] | None = None) -> None:
        self.client = client
        self.write_client = write_client if write_client is not None else client
        configure_upload_client(client)
        configure_upload_client(self.write_client)
        # TransferManager registers its static stream handlers on meta.events.
        # Keep that registration local: shared SDK clients were configured once
        # at construction, before concurrent requests could start. No response,
        # metadata, callback or event state is ever attached to a shared client.
        self.meta = SimpleNamespace(events=HierarchicalEmitter(),
                                    config=getattr(getattr(client, "meta", None), "config", None))
        if isinstance(self.write_client, BaseClient) and self.write_client.meta.config.retries.get("total_max_attempts") != 1:
            raise RuntimeError("Non-idempotent S3 writes require total_max_attempts=1")
        self.snapshot = snapshot
        self.check_active = check_active
        self.max_bandwidth = max_bandwidth
        self.on_acknowledged = on_acknowledged
        self.on_multipart = on_multipart
        self.response: dict[str, Any] | None = None
        self.pending: dict[str, Any] | None = None
        # The last abort the provider refused: the incomplete parts are still stored.
        self.abort_error: ClientError | None = None

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
                body.signal_transferring()
                return self._acknowledge(self.write_client.put_object(**kwargs))
        if hasattr(kwargs["Body"], "signal_transferring"):
            kwargs["Body"].signal_transferring()
        return self._acknowledge(self.write_client.put_object(**kwargs))

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        if self.snapshot is None:
            if hasattr(kwargs["Body"], "signal_transferring"):
                kwargs["Body"].signal_transferring()
            return self.client.upload_part(**kwargs)
        number = int(kwargs["PartNumber"])
        expected = self.snapshot.part_checksums[number - 1]
        kwargs["ChecksumAlgorithm"] = "SHA256"
        kwargs["ChecksumSHA256"] = expected
        with self._validated_body(kwargs["Body"], number) as body:
            kwargs["Body"] = body
            body.signal_transferring()
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
        result = self.write_client.create_multipart_upload(**kwargs)
        self.pending = {"Bucket": kwargs["Bucket"], "Key": kwargs["Key"], "UploadId": result["UploadId"]}
        if self.on_multipart is not None:
            # Journaled BEFORE s3transfer receives the id, hence before any part:
            # an interrupted upload can later abort exactly this UploadId (#491).
            # If it cannot be journaled the upload must not continue untraceable.
            try:
                self.on_multipart("created", result["UploadId"])
            except BaseException:
                try:
                    self.abort_multipart_upload(**self.pending)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Incomplete multipart abort failed; lifecycle cleanup is required"
                    )
                raise
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
        response = self.write_client.complete_multipart_upload(**kwargs)
        upload_id = kwargs.get("UploadId")
        self.pending = None
        try:
            return self._acknowledge(response)
        finally:
            self._journal_closed(upload_id)

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        try:
            result = self.client.abort_multipart_upload(**kwargs)
        except ClientError as exc:
            # Completion may have succeeded but its response was interrupted.
            # Never delete a completed object to compensate an ambiguous result.
            if exc.response.get("Error", {}).get("Code") != "NoSuchUpload":
                self.abort_error = exc
                raise
            result = {}
        self.pending = None
        self.abort_error = None
        self._journal_closed(kwargs.get("UploadId"))
        return result

    def _journal_closed(self, upload_id: Any) -> None:
        # The provider no longer holds it. A failure here only leaves the row open; it
        # is then closed when reconciliation publishes the version or the intent is
        # settled (or, for an upload that was aborted, aborted again by reconcile).
        # It never fails the upload.
        if self.on_multipart is None or not upload_id:
            return
        try:
            self.on_multipart("closed", str(upload_id))
        except Exception:
            logging.getLogger(__name__).warning("Multipart journal close failed; reconciliation will resolve it")


def abort_refused(error: BaseException | None) -> bool:
    """An AbortMultipartUpload the provider refused for permission (403/AccessDenied)."""
    if not isinstance(error, ClientError):
        return False
    return (error.response.get("Error", {}).get("Code") in {"AccessDenied", "AccessDeniedException"}
            or error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403)


class _Completion(BaseSubscriber):
    def __init__(self) -> None:
        self.event = threading.Event()

    def on_done(self, future: Any, **kwargs: Any) -> None:
        self.event.set()


def _record_failure(failure: BaseException, facade: _UploadClient,
                    snapshot: UploadSnapshot | None) -> None:
    # Call only after every in-flight request has been joined. Preserve the
    # exception object and classification; a late receipt beats absence proof.
    if abort_refused(facade.abort_error):
        failure.upload_abort_refused = True
    if facade.response is not None:
        failure.upload_response = dict(facade.response)
    elif snapshot is not None and isinstance(failure, ClientError):
        from .cloud_preflight_gate import is_readiness_error
        if (failure.operation_name in {'PutObject', 'CreateMultipartUpload', 'UploadPart', 'CompleteMultipartUpload'}
                and is_readiness_error(failure)):
            failure.upload_no_write_operation = failure.operation_name


def _small_put(facade: _UploadClient, *, source: Path, bucket: str, key: str,
               snapshot: UploadSnapshot, metadata: dict[str, str] | None,
               on_progress: Callable[[int], None]) -> dict[str, Any]:
    # The caller thread remains free to observe cancellation/lease loss while a
    # pooled worker is inside a blocking SDK socket. Never return until joined.
    while not _small_slots.acquire(timeout=0.1):
        facade.check_active()
    future = body = buffer = None
    failure = None
    body_owned_by_worker = False
    coordinator = TransferCoordinator()
    try:
        facade.check_active()
        buffer = validated_snapshot_buffer(source, snapshot, facade.check_active)
        stream = InterruptReader(buffer, coordinator)
        if facade.max_bandwidth:
            stream = _shared_budget(facade.max_bandwidth).get_bandwith_limited_stream(stream, coordinator)
        body = ReadFileChunk(stream, snapshot.size, snapshot.size,
                             callbacks=[lambda bytes_transferred: on_progress(bytes_transferred)])
        args = {"Bucket": bucket, "Key": key, "Body": body,
                "ChecksumAlgorithm": "SHA256", "ChecksumSHA256": snapshot.checksum_sha256}
        if metadata is not None:
            args["Metadata"] = dict(metadata)
        done = threading.Event()

        def send() -> dict[str, Any]:
            nonlocal body_owned_by_worker
            body_owned_by_worker = True
            send_failed = False
            try:
                facade.check_active()
                # No check, proof, progress or validation between response and
                # receipt hook, including when cancellation already happened.
                return facade._acknowledge(facade.write_client.put_object(**args))
            except BaseException:
                send_failed = True
                raise
            finally:
                try:
                    # Small bodies may be charged by the shared limiter on close.
                    # Do that in the worker too, keeping lease/cancel checks live.
                    body.close()
                except BaseException:
                    if not send_failed:
                        raise

        future = _small_pool().submit(send)
        future.add_done_callback(lambda _: done.set())
        while not done.wait(0.1):
            facade.check_active()
        response = future.result()
        facade.check_active()
        if not response or not response.get("VersionId") or response["VersionId"] == "null":
            raise RuntimeError("Upload stored without an S3 VersionId; bucket Versioning is required")
        return response
    except BaseException as exc:
        failure = exc
        coordinator.set_exception(exc)
        if future is not None:
            future.cancel()
        raise
    finally:
        try:
            if future is not None:
                try:
                    # Join a running socket BEFORE touching its body. A failure
                    # cannot replace an already observed cancellation/lease loss.
                    future.result()
                except BaseException:
                    if failure is None:
                        raise
            if body is not None and not body_owned_by_worker:
                try:
                    # A queued future cancelled before send never owned its body.
                    body.close()
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                        raise
        finally:
            if buffer is not None:
                buffer.close()
            _small_slots.release()
            if failure is not None:
                _record_failure(failure, facade, snapshot)


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
    write_client: Any = None,
    on_multipart: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Return the upload response or fail closed, aborting incomplete multipart.

    Cancellation is checked by the worker, not only SDK callbacks: a stalled
    socket or a bandwidth-limited stream must not hide a cancelled/lost Job.
    Manager shutdown joins in-flight parts before the explicit abort fallback.
    Completed versions are NEVER deleted here, even after cancellation.
    """
    check_active()
    if max_bandwidth is not None and max_bandwidth <= 0:
        raise ValueError("max_bandwidth must be greater than 0")
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
                           max_bandwidth=max_bandwidth, on_acknowledged=on_acknowledged,
                           write_client=write_client, on_multipart=on_multipart)
    if snapshot is not None and snapshot.size < MULTIPART_THRESHOLD:
        return _small_put(facade, source=source, bucket=bucket, key=key,
                          snapshot=snapshot, metadata=metadata, on_progress=on_progress)
    config = TransferConfig(max_bandwidth=max_bandwidth, preferred_transfer_client="classic")
    if snapshot is not None:
        config.multipart_threshold = MULTIPART_THRESHOLD
        config.multipart_chunksize = snapshot.part_size
        # Hard bounded buffers: at most two parts per upload, no whole-file
        # materialization, plus <=10,000 small independent part digests.
        config.max_request_concurrency = min(config.max_request_concurrency, 2)
    manager = TransferManager(facade, config, osutil=_SnapshotOSUtils() if snapshot is not None else None)
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
        completion = _Completion()
        future = manager.upload(str(source), bucket, key, extra_args=extra_args,
                                subscribers=[_Progress(on_progress), completion])
        while not completion.event.wait(0.1):
            check_active()
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
            if failure is not None:
                _record_failure(failure, facade, snapshot)
