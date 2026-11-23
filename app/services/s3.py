"""The S3 module: clients, credentials, error codes and exact-VersionId operations.

Everything FrostVault needs from the provider that is not a domain decision:

- the shared, process-local, bounded clients and the check that the AWS
  credentials are not the documented placeholders;
- the provider's error code of a ``ClientError`` and the named sets of codes;
- the operations on one exact provider VersionId (listing, HEAD with checksum,
  SHA-256 read-back, delete, "already gone").

Nothing here touches the catalog or a Job row, and nothing here imports
``app.storage`` or a domain service: callers pass ``check_active`` when a long
read must observe cancellation between provider calls.

Client construction is serialized, including Session creation (boto3's default
Session is neither a safe concurrent factory nor a fresh environment view).
Only the public SDK client is shared; never register per-transfer event handlers
or mutate client metadata. Reads/same-MPU parts retry; ambiguous mutators do not.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import os
import re
import threading
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError
from s3transfer.utils import signal_not_transferring, signal_transferring

from ..config import is_placeholder, settings

# -- Clients and credentials -------------------------------------------------------

_DEFAULT_FACTORY = boto3.client
# The seam of the client construction: tests replace it to observe or fake the
# SDK factory. The real SDK client is built on a private Session instead.
client_factory: Callable[..., Any] = _DEFAULT_FACTORY
_MAX_CLIENTS = 12
_lock = threading.Lock()
_clients: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
_live_clients: weakref.WeakSet = weakref.WeakSet()
_upload_setup_lock = threading.Lock()
_upload_ready: weakref.WeakSet = weakref.WeakSet()
# Keep inherited pools alive but never use/close them in the child, even via GC.
_inherited: list[Any] = []


# What other S3 state (the upload transport) resets in a forked child.
_fork_resets: list[Callable[[], None]] = []


def reset_after_fork(reset: Callable[[], None]) -> None:
    """Run ``reset`` in every forked child, after the clients are forgotten.

    The one fork handler of the S3 state: resets run in registration order.
    """
    _fork_resets.append(reset)


def _after_fork() -> None:
    global _lock, _clients, _live_clients, _upload_setup_lock, _upload_ready
    _inherited.append((_clients, tuple(_live_clients), tuple(_upload_ready)))
    _lock = threading.Lock()
    _clients = OrderedDict()
    _live_clients = weakref.WeakSet()
    _upload_setup_lock = threading.Lock()
    _upload_ready = weakref.WeakSet()
    for reset in tuple(_fork_resets):
        reset()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def configure_upload_client(client: Any) -> None:
    """Install stateless SDK stream signals ONCE, never a transfer's callbacks.

    Also supports injected SDK clients at the upload_file seam. Shared clients
    from the factory are already configured before publication to any caller.
    """
    if not isinstance(client, BaseClient):
        return
    with _upload_setup_lock:
        if client not in _upload_ready:
            client.meta.events.register_first("request-created.s3", signal_not_transferring,
                                              unique_id="s3upload-not-transferring")
            client.meta.events.register_last("request-created.s3", signal_transferring,
                                             unique_id="s3upload-transferring")
            _upload_ready.add(client)


def _configuration_key() -> bytes:
    # Hash rather than retaining secret values in inspectable cache keys. Include
    # endpoint/proxy/profile/CA overrides: tests and operators can change these.
    values = sorted((name, value) for name, value in os.environ.items()
                    if name.startswith(("AWS_", "BOTO_")) or name in {
                        "HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                        "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE",
                    })
    for name, default in (("AWS_CONFIG_FILE", "~/.aws/config"),
                          ("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials"),
                          ("BOTO_CONFIG", "~/.boto")):
        path = Path(os.environ.get(name, default)).expanduser()
        try:
            info = path.stat()
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except OSError:
            stamp = None
        values.append((name + ":file", str(stamp)))
    return hashlib.sha256(repr(values).encode()).digest()


def credentials_are_placeholders() -> bool:
    """Whether a static AWS key in the environment is a documented placeholder."""
    return (is_placeholder(os.getenv("AWS_ACCESS_KEY_ID", ""), "REPLACE_ME", "REPLACE-WITH-ACCESS-KEY")
            or is_placeholder(os.getenv("AWS_SECRET_ACCESS_KEY", ""), "REPLACE_ME",
                              "REPLACE-WITH-SECRET-KEY"))


def s3_client(*, preflight: bool = False, non_idempotent: bool = False,
              region: str | None = None) -> Any:
    """The shared client for AWS_DEFAULT_REGION, or for ``region`` (System Bucket).

    Refuses placeholder credentials before any client is built.
    """
    if credentials_are_placeholders():
        raise RuntimeError("AWS credentials are not configured in the .env file")
    return _shared_client(region=region or settings.aws_region, preflight=preflight,
                          non_idempotent=non_idempotent, factory=client_factory)


def _shared_client(*, region: str, preflight: bool = False, non_idempotent: bool = False,
                   factory: Callable[..., Any] = _DEFAULT_FACTORY) -> Any:
    """Return a shared client for this effective configuration and retry policy.

    ``factory`` is ``client_factory``, the override seam of the tests.
    The real SDK uses a new private Session only on cache misses, under the lock.
    Eviction drops cache ownership without closing a client a caller may still be
    using; idle evictees are reclaimed by the SDK/pool GC. Shutdown closes all
    still-live clients after workers have drained, and is also registered at exit.
    """
    with _lock:
        policy = "preflight" if preflight else "write" if non_idempotent else "read"
        key = (factory, region, policy, _configuration_key())
        if key in _clients:
            _clients.move_to_end(key)
            return _clients[key]
        config = (Config(connect_timeout=2, read_timeout=3,
                         retries={"mode": "standard", "total_max_attempts": 1})
                  if preflight else Config(retries={"mode": "standard", "total_max_attempts": 1})
                  if non_idempotent else Config(retries={"mode": "standard", "max_attempts": 4}))
        creator = boto3.session.Session().client if factory is _DEFAULT_FACTORY else factory
        client = creator("s3", region_name=region, config=config)
        configure_upload_client(client)
        _clients[key] = client
        _live_clients.add(client)
        if len(_clients) > _MAX_CLIENTS:
            _clients.popitem(last=False)
        return client


def new_service_client(service: str, *, region: str, config: Config) -> Any:
    """A new private client for another AWS service (CloudWatch, #737).

    Built on its own Session under the same lock as the S3 clients; it is not
    cached, and the caller closes it when done.
    """
    with _lock:
        return boto3.session.Session().client(service, region_name=region, config=config)


def shutdown_s3_clients() -> None:
    """Close this process's clients only; call after all users have drained."""
    with _lock:
        clients = list(_live_clients)
        _clients.clear()
        _live_clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            # Never log provider details or credentials, especially at exit.
            pass


atexit.register(shutdown_s3_clients)


# -- Error codes -------------------------------------------------------------------

# The exact version, or its key, does not exist (any more).
GONE_CODES = frozenset({"NoSuchKey", "NoSuchVersion", "NotFound", "404"})
# The provider refused the action to this identity.
ACCESS_DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException"})
# The request signature or token was rejected: the credentials themselves are
# wrong or expired, not under-privileged.
CREDENTIAL_CODES = frozenset({
    "SignatureDoesNotMatch", "InvalidAccessKeyId", "InvalidClientTokenId",
    "UnrecognizedClientException", "ExpiredToken", "InvalidToken", "TokenRefreshRequired",
})
# Provider answers that say "not now", never "no": throttling, request timeouts
# and server-side faults.
TRANSIENT_CODES = frozenset({
    "SlowDown", "Throttling", "ThrottlingException", "ThrottledException", "TooManyRequestsException",
    "RequestLimitExceeded", "RequestThrottled", "BandwidthLimitExceeded", "RequestTimeout",
    "RequestTimeoutException", "InternalError", "ServiceUnavailable",
})
# The provider does not implement the checksum API at all.
CHECKSUM_UNSUPPORTED_CODES = frozenset({"NotImplemented", "UnsupportedOperation", "XNotImplemented", "501"})
_CHECKSUM_ARGUMENT_CODES = frozenset({"InvalidRequest", "InvalidArgument"})
_CHECKSUM_ARGUMENTS = frozenset({"ChecksumMode", "x-amz-checksum-mode"})
_SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def error_code(exc: BaseException, *, missing: str = "") -> str:
    """The provider's error code of a ``ClientError``; ``""`` for any other error.

    ``missing`` is returned when the answer carries no code at all.
    """
    if not isinstance(exc, ClientError):
        return ""
    error = (exc.response or {}).get("Error") or {}
    if "Code" not in error:
        return missing
    return str(error["Code"] or "")


def safe_error_code(exc: BaseException) -> str:
    """The provider's error code, kept only when it looks like one.

    An S3-compatible endpoint may answer anything: an unexpected value is
    never stored or shown, only ``ClientError``.
    """
    code = error_code(exc)
    return code if _SAFE_ERROR_CODE.fullmatch(code) else "ClientError"


def version_already_gone(exc: BaseException) -> bool:
    """Whether a failed request says the exact version no longer exists."""
    return isinstance(exc, ClientError) and error_code(exc) in GONE_CODES


def checksum_mode_unsupported(exc: BaseException) -> bool:
    """Accept explicit capability errors, never generic malformed requests.

    Some compatible providers identify an unsupported checksum-mode argument
    structurally. Do not infer capability from arbitrary provider messages or
    from InvalidRequest/InvalidArgument alone: that could hide configuration
    errors and incur an unexpected read-back.
    """
    if not isinstance(exc, ClientError) or exc.operation_name != "HeadObject":
        return False
    error = exc.response.get("Error", {})
    code = str(error.get("Code"))
    if code in CHECKSUM_UNSUPPORTED_CODES:
        return True
    return code in _CHECKSUM_ARGUMENT_CODES and error.get("ArgumentName") in _CHECKSUM_ARGUMENTS


# -- One exact provider VersionId --------------------------------------------------

FULL_OBJECT_CHECKSUM_TYPE = "FULL_OBJECT"
OBJECT_HASH_CHUNK_BYTES = 1024 * 1024
ARCHIVE_RESTORE_REQUIRED = frozenset({"GLACIER", "DEEP_ARCHIVE"})


class ProviderResponseError(RuntimeError):
    """The provider answered, but incompletely or not in the documented shape.

    A fault of the answer, never of FrostVault's own code: callers that turn a
    provider failure into a per-file outcome may name it (#628).
    """


def object_version_entries(
    client: Any,
    *,
    bucket: str,
    object_key: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read exact-version postconditions without treating a retry as evidence.

    S3's list API is used rather than a current-key HEAD because a delete marker
    and a noncurrent Archive Version are both meaningful durable outcomes.
    """
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": object_key}
    versions: dict[str, dict[str, Any]] = {}
    markers: dict[str, dict[str, Any]] = {}
    continuations: set[tuple[str, str]] = set()
    while True:
        page = client.list_object_versions(**kwargs)
        if not isinstance(page, dict) or page.get("IsTruncated") not in (True, False):
            raise ProviderResponseError("S3 version listing did not confirm complete pagination")
        for field, entries, other in (("Versions", versions, markers), ("DeleteMarkers", markers, versions)):
            items = page.get(field) or []
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise ProviderResponseError("S3 version listing is malformed")
            for item in items:
                if item.get("Key") != object_key:
                    continue
                version = str(item.get("VersionId") or "")
                if not version or version == "null" or version in entries or version in other:
                    raise ProviderResponseError("S3 version listing has ambiguous exact identities")
                entries[version] = item
        if not page.get("IsTruncated"):
            break
        next_key = page.get("NextKeyMarker")
        next_version = page.get("NextVersionIdMarker")
        if not next_key:
            raise ProviderResponseError("S3 version listing omitted its continuation marker")
        continuation = (str(next_key), str(next_version or ""))
        if continuation in continuations:
            raise ProviderResponseError("S3 version listing repeated its continuation marker")
        continuations.add(continuation)
        kwargs["KeyMarker"] = next_key
        if next_version:
            kwargs["VersionIdMarker"] = next_version
        else:
            kwargs.pop("VersionIdMarker", None)
    return versions, markers


def delete_version(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    version_id: str,
) -> None:
    """Delete one exact version; one already gone is deleted."""
    try:
        client.delete_object(
            Bucket=bucket, Key=object_key, VersionId=version_id
        )
    except Exception as exc:
        if version_already_gone(exc):
            return
        raise


def verification_version_head(
    client: Any,
    job: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    """Reconfirm the exact provider VersionId around a streamed read."""
    object_key = str(target.get("object_key") or "")
    expected_version = str(target.get("provider_version_id") or "")
    if not object_key or not expected_version:
        raise RuntimeError("Upload verification target has no exact S3 VersionId")
    head = client.head_object(Bucket=job["s3_bucket"], Key=object_key,
                              VersionId=expected_version)
    observed_version = head.get("VersionId")
    if not observed_version:
        raise RuntimeError("Upload verification found no S3 VersionId")
    if str(observed_version) != expected_version:
        raise RuntimeError("Archive Version changed during upload verification")
    expected_size = target.get("cloud_size")
    observed_size = head.get("ContentLength")
    if expected_size is not None and observed_size is not None:
        if int(observed_size) != int(expected_size):
            raise RuntimeError("Archive Version size changed during upload verification")
    return head


def head_object_with_checksum(client: Any, **kwargs: Any) -> dict[str, Any]:
    """Read a version with provider checksum fields, or without them on any failure.

    Some S3-compatible providers reject ``ChecksumMode``. Falling back to a
    normal HEAD keeps the operation portable; the caller then obtains an
    equivalent proof by hashing the exact VersionId instead of publishing on
    size/ETag metadata alone.

    This is the lenient policy (copies): every failure of the first HEAD is
    answered by a plain one. The strict policy (uploads) sends its own HEAD
    and asks ``checksum_mode_unsupported`` about the failure.
    """
    try:
        return client.head_object(**kwargs, ChecksumMode="ENABLED")
    except Exception:
        return client.head_object(**kwargs)


def full_object_sha256_checksum(head: dict[str, Any]) -> str | None:
    """The full-object SHA-256 a HEAD exposes, or None (absent or composite)."""
    checksum = head.get("ChecksumSHA256")
    if not checksum:
        return None
    checksum_type = str(head.get("ChecksumType") or FULL_OBJECT_CHECKSUM_TYPE).upper()
    if checksum_type != FULL_OBJECT_CHECKSUM_TYPE:
        return None
    return str(checksum)


def sha256_version(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    version_id: str,
    check_active: Callable[[str], None] | None = None,
) -> str:
    """Hash one exact provider VersionId and return its S3 checksum encoding."""
    response = client.get_object(
        Bucket=bucket,
        Key=object_key,
        VersionId=version_id,
    )
    body = response.get("Body") if isinstance(response, dict) else None
    if body is None:
        raise RuntimeError("S3 did not return a body for integrity verification")
    digest = hashlib.sha256()
    try:
        iter_chunks = getattr(body, "iter_chunks", None)
        if callable(iter_chunks):
            chunks = iter_chunks(chunk_size=OBJECT_HASH_CHUNK_BYTES)
        else:
            read = getattr(body, "read", None)
            if not callable(read):
                raise RuntimeError("S3 returned an unreadable body")

            def read_chunks():
                while True:
                    chunk = read(OBJECT_HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk

            chunks = read_chunks()
        for chunk in chunks:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise RuntimeError("S3 returned a non-binary body")
            digest.update(chunk)
            if check_active is not None:
                check_active("Storage class change stopped")
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return base64.b64encode(digest.digest()).decode("ascii")


def restore_header_state(value: str | None) -> tuple[str, str | None]:
    """``(state, expiry)`` of a HEAD's ``Restore`` header."""
    if not value:
        return "not_requested", None
    if 'ongoing-request="true"' in value:
        return "restoring", None
    match = re.search(r'expiry-date="([^"]+)"', value)
    return "available", match.group(1) if match else None


def storage_class_requires_restore(storage_class: str | None) -> bool:
    return (storage_class or "").upper() in ARCHIVE_RESTORE_REQUIRED
