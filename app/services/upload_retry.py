"""Upload failure classification and retry backoff policy."""
from __future__ import annotations

import psycopg
from botocore.exceptions import (
    BotoCoreError, ClientError, ConnectionError as BotoConnectionError, HTTPClientError, IncompleteReadError,
)
from botocore.parsers import ResponseParserError
from .database_retry import exception_chain, is_retryable_transaction_error


UPLOAD_RETRY_BASE_SECONDS = 2


UPLOAD_RETRY_CAP_SECONDS = 300


UPLOAD_RETRY_MAX_ATTEMPTS = 8


_PERMANENT_UPLOAD_FAILURE_MARKERS = (
    "digest does not match",
    "did not create the verification copy",
    "accessdenied",
    "invalidaccesskeyid",
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
    # SDK endpoint/stream exceptions after botocore's own retries are exhausted.
    "could not connect to the endpoint url",
    "connection was closed before",
    "read, but total bytes expected",
    "error occurred while reading from response stream",
    "network is unreachable",
    "no route to host",
    "broken pipe",
    "unexpected eof",
    "temporary failure",
    "timeout",
    "throttl",
    "internal error",
    "econnreset",
    "unavailable",
    "verification stream length",
)


# Provider answers that say "not now", never "no": throttling, request timeouts
# and server-side faults. Matching is structural (HTTP status / error code), never
# on message text, so a permission or content problem cannot be mistaken for one.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_TRANSIENT_ERROR_CODES = frozenset({
    "SlowDown", "Throttling", "ThrottlingException", "ThrottledException", "TooManyRequestsException",
    "RequestLimitExceeded", "RequestThrottled", "BandwidthLimitExceeded", "RequestTimeout",
    "RequestTimeoutException", "InternalError", "ServiceUnavailable",
})


def is_provider_transient(error: BaseException) -> bool:
    """The provider could not answer right now: retry with backoff, never conclude.

    Covers 408/429/5xx, the 400 ``RequestTimeout``/``SlowDown`` family, transport
    faults (connect/read timeouts, closed or truncated streams) and responses that
    are not a well-formed HTTP/S3 answer (unparsable body, no HTTP status at all).
    Credential and permission refusals are not transient.
    """
    for item in exception_chain(error):
        if isinstance(item, ClientError):
            status = item.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = item.response.get("Error", {}).get("Code")
            if status in _TRANSIENT_HTTP_STATUSES or code in _TRANSIENT_ERROR_CODES:
                return True
        elif isinstance(item, (BotoConnectionError, HTTPClientError, IncompleteReadError, ResponseParserError,
                               ConnectionError, TimeoutError)):
            return True
        elif isinstance(item, BotoCoreError) and "timeout" in type(item).__name__.lower():
            return True
    return False


_CONNECTION_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})


def is_database_connection_error(error: BaseException) -> bool:
    """Structured loss of the PostgreSQL connection, never message heuristics.

    Client-side failures ("connection is bad", refused/unreachable) carry no
    SQLSTATE; server-side shutdown/startup/limits use class 08 and 57P0x/53300.
    Statement timeouts, constraint and transaction-conflict errors do not count.
    """
    for item in exception_chain(error):
        if isinstance(item, psycopg.InterfaceError):
            return True
        if not isinstance(item, psycopg.OperationalError):
            continue
        state = item.sqlstate
        if state is None or state.startswith("08") or state in _CONNECTION_SQLSTATES:
            return True
    return False


def classify_upload_failure(message: str | BaseException) -> str:
    """Classify an upload/verify error for retry policy.

    Returns ``source_changed`` when the Local Copy mutated before an Archive
    Version is linked, so the Job can be rescheduled after the Vault stability
    window up to ``UPLOAD_RETRY_MAX_ATTEMPTS``. ``transient`` covers retryable
    transport faults; anything else is ``permanent``. ``database_transient``
    is separate: only the aborted DB unit can be replayed, never the upload.
    ``database_connection`` is a lost PostgreSQL connection: the Job is retried
    with backoff and its journaled intent decides between reconcile and send.
    """
    from .upload_provenance import UploadBlocked
    if isinstance(message, UploadBlocked):
        # Durable safety blocks are not transport retries inferred from words
        # such as "unavailable" in the reason/remedy. Explicit exact resume only.
        return "permanent"
    if isinstance(message, BaseException):
        if is_retryable_transaction_error(message):
            return "database_transient"
        if is_database_connection_error(message):
            # The send is journaled (#464): a retry can only reconcile, or send
            # when a definitive no-write outcome was proven. It never repeats a
            # PUT blindly, so losing the database is a bounded retry (#484).
            return "database_connection"
        if any(isinstance(error, psycopg.Error) for error in exception_chain(message)):
            # Constraint failures are not S3 faults. Message heuristics must
            # never schedule another provider upload.
            return "permanent"
    lowered = str(message or "").lower()
    if "changed since fingerprinting" in lowered:
        return "source_changed"
    if any(marker in lowered for marker in _PERMANENT_UPLOAD_FAILURE_MARKERS):
        return "permanent"
    if isinstance(message, BaseException) and is_provider_transient(message):
        return "transient"
    if any(marker in lowered for marker in _TRANSIENT_UPLOAD_FAILURE_MARKERS):
        return "transient"
    return "permanent"


def upload_retry_delay_seconds(attempt: int) -> int:
    """Exponential backoff delay for the next upload retry attempt."""
    if attempt < 1:
        attempt = 1
    delay = UPLOAD_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    return min(delay, UPLOAD_RETRY_CAP_SECONDS)
