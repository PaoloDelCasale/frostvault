"""Upload failure classification and retry backoff policy."""
from __future__ import annotations


UPLOAD_RETRY_BASE_SECONDS = 2


UPLOAD_RETRY_CAP_SECONDS = 300


UPLOAD_RETRY_MAX_ATTEMPTS = 8


_PERMANENT_UPLOAD_FAILURE_MARKERS = (
    "digest does not match",
    "did not create the verification copy",
    "accessdenied",
    "invalidaccesskeyid",
    "rclone configuration not found",
    "without an s3 versionid",
    "bucket versioning is required",
    "not authorized",
    "access denied",
    "forbidden",
    "signaturedoesnotmatch",
)


_TRANSIENT_UPLOAD_FAILURE_MARKERS = (
    "slowdown",
    "service unavailable",
    "requesttimeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "no route to host",
    "broken pipe",
    "unexpected eof",
    "temporary failure",
    "timeout",
    "throttl",
    "503",
    "500",
    "internal error",
    "econnreset",
    "unavailable",
    "verification stream length",
)


def classify_upload_failure(message: str) -> str:
    """Classify an upload/verify error for retry policy.

    Returns ``source_changed`` when the Local Copy mutated before an Archive
    Version is linked, so the Job can be rescheduled after the Vault stability
    window up to ``UPLOAD_RETRY_MAX_ATTEMPTS``. ``transient`` covers retryable
    transport faults; anything else is ``permanent``.
    """
    lowered = (message or "").lower()
    if "changed since fingerprinting" in lowered:
        return "source_changed"
    if any(marker in lowered for marker in _PERMANENT_UPLOAD_FAILURE_MARKERS):
        return "permanent"
    if any(marker in lowered for marker in _TRANSIENT_UPLOAD_FAILURE_MARKERS):
        return "transient"
    return "permanent"


def upload_retry_delay_seconds(attempt: int) -> int:
    """Exponential backoff delay for the next upload retry attempt."""
    if attempt < 1:
        attempt = 1
    delay = UPLOAD_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    return min(delay, UPLOAD_RETRY_CAP_SECONDS)
