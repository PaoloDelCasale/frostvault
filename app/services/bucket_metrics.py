"""Daily bytes and object counts of each Vault Bucket from CloudWatch (#737, ADR-0051).

Once a day, off the request path, the background loop reads the S3 storage
metrics of every dedicated Vault Bucket (``key_layout`` ``bucket_root``) in
the bucket's own region:

- ``BucketSizeBytes`` for each StorageType in ``STORAGE_TYPES``, one
  CloudWatch ``GetMetricStatistics`` call per StorageType;
- ``NumberOfObjects`` (StorageType ``AllStorageTypes``), one more call.

Only ``GetMetricStatistics`` is used: it is in CloudWatch's free API tier,
while ``GetMetricData`` is always billed (ADR-0051). Each call asks for the
daily data points of the last ``HISTORY_DAYS`` UTC days (today included), so a
missed day fills in at the next read; every recorded read, a failed one too,
prunes the rows of older days, so the catalog keeps the same days a read asks
for.

S3 publishes these metrics once a day, for the day before, so the newest data
point is one or two days old. A new bucket has none for its first day or two
(``no_data``).

A Vault on a shared bucket (``vault_prefix``, an older Vault) is never read:
its bucket's metrics cover other Vaults too. Without
``cloudwatch:GetMetricStatistics`` the read is recorded as ``access_denied``
and the Vault page shows a note; it is never a Problem. Any other AWS error is
recorded by its code (never its message). An unexpected error is recorded as
``failed`` with its exception type and escapes ``run_due`` after every Vault
was tried, so the worker reports it.

``vault_bucket_metric_reads`` keeps every Vault's last attempt, so a restart
never reads a Vault before its retry cadence: ``OK_INTERVAL`` after an ``ok``
read, ``RETRY_INTERVAL`` after any other outcome.

A pass stops early, leaving the remaining Vaults due and unrecorded, on the
first connection-level error (the network or the CloudWatch endpoint is down,
so the other Vaults would only wait on their timeouts), on shutdown (even in
the middle of a Vault, which is then not saved), and once it has run
``PASS_BUDGET_SECONDS``. The budget is checked between Vaults only, so a Vault
whose read started always ends with a recorded attempt and every pass makes
progress.
"""
from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from botocore.exceptions import (
    BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError,
)
from psycopg.types.json import Jsonb

from .cost_estimates import estimate_storage_month, list_prices
from .storage_classes import normalize_storage_class
from .vault_paths import KEY_LAYOUT_BUCKET_ROOT

NAMESPACE = "AWS/S3"
SIZE_METRIC = "BucketSizeBytes"
COUNT_METRIC = "NumberOfObjects"
COUNT_STORAGE_TYPE = "AllStorageTypes"
PERIOD_SECONDS = 86_400
POLL_SECONDS = 300.0
# The retry cadence: a Vault is read again a day after an ``ok`` read, six
# hours after any other outcome (no data yet, no permission, an error).
OK_INTERVAL = timedelta(hours=24)
RETRY_INTERVAL = timedelta(hours=6)
# Wall-clock budget of one pass; the Vaults left wait for the next poll.
PASS_BUDGET_SECONDS = 120.0
# UTC days kept per Vault, today included, and asked for at every read.
HISTORY_DAYS = 30
# Errors that mean the endpoint is unreachable, not that one Vault failed.
CONNECTION_ERRORS = (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)
OUTCOMES = ("ok", "no_data", "access_denied", "failed")
_ACCESS_DENIED = frozenset({
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
    "AuthorizationError", "NotAuthorized",
})
_logger = logging.getLogger(__name__)

Database = Callable[[], AbstractContextManager[Any]]


@dataclass(frozen=True)
class StorageType:
    """What a CloudWatch StorageType value counts, in FrostVault's terms.

    ``storage_class``: the class of the objects it belongs to. ``kind``:
    ``data`` (the objects' bytes), ``object_overhead`` (metadata AWS adds to
    every archived object), ``size_overhead`` (what AWS bills on top of an
    object smaller than the class's 128 KiB minimum) or ``staging`` (parts of
    an unfinished multipart upload). ``billed_as``: the class whose price AWS
    applies to those bytes.
    """

    storage_class: str
    kind: str
    billed_as: str


def _data(storage_class: str) -> StorageType:
    return StorageType(storage_class, "data", storage_class)


# Every BucketSizeBytes StorageType of a general purpose bucket (AWS docs,
# "Amazon S3 daily storage metrics for buckets in CloudWatch").
STORAGE_TYPES: dict[str, StorageType] = {
    "StandardStorage": _data("STANDARD"),
    "IntelligentTieringFAStorage": _data("INTELLIGENT_TIERING"),
    "IntelligentTieringIAStorage": _data("INTELLIGENT_TIERING"),
    "IntelligentTieringAIAStorage": _data("INTELLIGENT_TIERING"),
    "IntelligentTieringAAStorage": _data("INTELLIGENT_TIERING"),
    "IntelligentTieringDAAStorage": _data("INTELLIGENT_TIERING"),
    # The 32 KB index of an object in the Archive Access tier is billed at
    # Glacier Flexible Retrieval rates, Deep Archive Access at Deep Archive.
    "IntAAObjectOverhead": StorageType("INTELLIGENT_TIERING", "object_overhead", "GLACIER"),
    "IntAAS3ObjectOverhead": StorageType("INTELLIGENT_TIERING", "object_overhead", "STANDARD"),
    "IntDAAObjectOverhead": StorageType("INTELLIGENT_TIERING", "object_overhead", "DEEP_ARCHIVE"),
    "IntDAAS3ObjectOverhead": StorageType("INTELLIGENT_TIERING", "object_overhead", "STANDARD"),
    "StandardIAStorage": _data("STANDARD_IA"),
    # Listed by AWS among the Standard-IA StorageTypes but not described:
    # assumed to be per-object metadata billed at Standard-IA rates.
    "StandardIAObjectOverhead": StorageType("STANDARD_IA", "object_overhead", "STANDARD_IA"),
    "StandardIASizeOverhead": StorageType("STANDARD_IA", "size_overhead", "STANDARD_IA"),
    "OneZoneIAStorage": _data("ONEZONE_IA"),
    "OneZoneIASizeOverhead": StorageType("ONEZONE_IA", "size_overhead", "ONEZONE_IA"),
    "ReducedRedundancyStorage": _data("REDUCED_REDUNDANCY"),
    "GlacierInstantRetrievalStorage": _data("GLACIER_IR"),
    "GlacierIRSizeOverhead": StorageType("GLACIER_IR", "size_overhead", "GLACIER_IR"),
    "GlacierStorage": _data("GLACIER"),
    "GlacierObjectOverhead": StorageType("GLACIER", "object_overhead", "GLACIER"),
    "GlacierS3ObjectOverhead": StorageType("GLACIER", "object_overhead", "STANDARD"),
    "GlacierStagingStorage": StorageType("GLACIER", "staging", "STANDARD"),
    "DeepArchiveStorage": _data("DEEP_ARCHIVE"),
    "DeepArchiveObjectOverhead": StorageType("DEEP_ARCHIVE", "object_overhead", "DEEP_ARCHIVE"),
    "DeepArchiveS3ObjectOverhead": StorageType("DEEP_ARCHIVE", "object_overhead", "STANDARD"),
    "DeepArchiveStagingStorage": StorageType("DEEP_ARCHIVE", "staging", "STANDARD"),
}
# The order classes are shown in: warmest first.
CLASS_ORDER = ("STANDARD", "INTELLIGENT_TIERING", "STANDARD_IA", "ONEZONE_IA",
               "REDUCED_REDUNDANCY", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE")


class CloudWatchUnavailable(RuntimeError):
    """No usable AWS credentials here (placeholders): nothing is read."""


class PassStopped(RuntimeError):
    """Shutdown stopped a read before its end: nothing saved."""


@dataclass
class MetricDay:
    storage_bytes: dict[str, int]
    object_count: int | None = None


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _default_client(region: str) -> Any:
    import os

    from botocore.config import Config

    from ..config import is_placeholder
    from .s3_clients import new_service_client

    if (is_placeholder(os.getenv("AWS_ACCESS_KEY_ID", ""), "REPLACE_ME", "REPLACE-WITH-ACCESS-KEY")
            or is_placeholder(os.getenv("AWS_SECRET_ACCESS_KEY", ""), "REPLACE_ME",
                              "REPLACE-WITH-SECRET-KEY")):
        raise CloudWatchUnavailable("AWS credentials are not configured")
    return new_service_client(
        "cloudwatch", region=region,
        config=Config(connect_timeout=3, read_timeout=10,
                      retries={"mode": "standard", "total_max_attempts": 2}))


def _datapoints(client: Any, *, bucket: str, metric: str, storage_type: str,
                start: datetime, end: datetime) -> list[tuple[date, int]]:
    response = client.get_metric_statistics(
        Namespace=NAMESPACE, MetricName=metric,
        Dimensions=[{"Name": "BucketName", "Value": bucket},
                    {"Name": "StorageType", "Value": storage_type}],
        StartTime=start, EndTime=end, Period=PERIOD_SECONDS, Statistics=["Average"])
    points = []
    for point in response.get("Datapoints") or ():
        value = point.get("Average")
        stamp = point.get("Timestamp")
        if value is None or stamp is None:
            continue
        points.append((_utc(stamp).astimezone(timezone.utc).date(), max(0, int(round(float(value))))))
    return points


def first_kept_day(now: datetime) -> date:
    """The oldest UTC day read and kept: ``HISTORY_DAYS`` days, today included."""
    return _utc(now).astimezone(timezone.utc).date() - timedelta(days=HISTORY_DAYS - 1)


def next_read_after(attempted_at: datetime, outcome: str) -> datetime:
    """When a Vault is due again after an attempt with ``outcome``."""
    return _utc(attempted_at) + (OK_INTERVAL if outcome == "ok" else RETRY_INTERVAL)


def read_bucket(client: Any, bucket: str, *, now: datetime,
                stop: Callable[[], bool] | None = None) -> dict[date, MetricDay]:
    """The daily data points of ``bucket`` over the last ``HISTORY_DAYS`` days.

    One ``GetMetricStatistics`` per StorageType and one for the object count;
    a failing call raises, so a read is saved whole or not at all. ``stop`` is
    checked before every call and raises ``PassStopped``.
    """
    end = _utc(now)
    start = datetime.combine(first_kept_day(end), time(), tzinfo=timezone.utc)
    days: dict[date, MetricDay] = {}
    requests = [(SIZE_METRIC, storage_type) for storage_type in STORAGE_TYPES]
    requests.append((COUNT_METRIC, COUNT_STORAGE_TYPE))
    for metric, storage_type in requests:
        if stop is not None and stop():
            raise PassStopped("the pass stopped before this read ended")
        for day, value in _datapoints(client, bucket=bucket, metric=metric,
                                      storage_type=storage_type, start=start, end=end):
            entry = days.setdefault(day, MetricDay({}))
            if metric == COUNT_METRIC:
                entry.object_count = value
            else:
                entry.storage_bytes[storage_type] = value
    return days


def classify_error(exc: BaseException) -> tuple[str, str]:
    """``(outcome, code)`` of a failed read: the AWS code, never the message."""
    if isinstance(exc, ClientError):
        code = str((exc.response.get("Error") or {}).get("Code") or "ClientError")[:100]
        return ("access_denied" if code in _ACCESS_DENIED else "failed"), code
    return "failed", type(exc).__name__[:100]


def due_vaults(connection: Any, *, now: datetime) -> list[dict[str, Any]]:
    """Every Vault with its own Vault Bucket whose retry cadence has passed.

    Never read first, then the oldest attempt first.
    """
    moment = _utc(now)
    return [dict(row) for row in connection.execute(
        """SELECT v.id, t.bucket, t.region
           FROM vaults v JOIN vault_targets t ON t.id = v.target_id
           LEFT JOIN vault_bucket_metric_reads r ON r.vault_id = v.id
           WHERE t.key_layout = %s AND v.lifecycle <> 'provisioning'
             AND (r.attempted_at IS NULL
                  OR (r.outcome = 'ok' AND r.attempted_at <= %s)
                  OR (r.outcome <> 'ok' AND r.attempted_at <= %s))
           ORDER BY r.attempted_at ASC NULLS FIRST, v.id""",
        (KEY_LAYOUT_BUCKET_ROOT, moment - OK_INTERVAL, moment - RETRY_INTERVAL)).fetchall()]


def _record_attempt(connection: Any, vault_id: int, *, now: datetime, outcome: str,
                    code: str | None) -> None:
    """The Vault's last attempt, and the pruning of days older than a read asks for."""
    connection.execute(
        "DELETE FROM vault_bucket_metric_days WHERE vault_id = %s AND day < %s",
        (vault_id, first_kept_day(now)))
    connection.execute(
        """INSERT INTO vault_bucket_metric_reads(vault_id, attempted_at, outcome, error_code)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (vault_id) DO UPDATE SET attempted_at = EXCLUDED.attempted_at,
               outcome = EXCLUDED.outcome, error_code = EXCLUDED.error_code""",
        (vault_id, now, outcome, code))


def record_read(connection: Any, vault_id: int, days: dict[date, MetricDay], *,
                now: datetime) -> str:
    """Save one read's data points and prune older days; returns the outcome."""
    oldest = first_kept_day(now)
    kept = {day: values for day, values in days.items() if day >= oldest}
    for day, values in sorted(kept.items()):
        connection.execute(
            """INSERT INTO vault_bucket_metric_days(vault_id, day, storage_bytes, object_count, read_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (vault_id, day) DO UPDATE SET storage_bytes = EXCLUDED.storage_bytes,
                   object_count = EXCLUDED.object_count, read_at = EXCLUDED.read_at""",
            (vault_id, day, Jsonb(values.storage_bytes), values.object_count, now))
    outcome = "ok" if kept else "no_data"
    _record_attempt(connection, vault_id, now=now, outcome=outcome, code=None)
    return outcome


def record_failure(connection: Any, vault_id: int, exc: BaseException, *, now: datetime) -> str:
    outcome, code = classify_error(exc)
    _record_attempt(connection, vault_id, now=now, outcome=outcome, code=code)
    return outcome


def _record_unexpected(database: Database, vault_id: int, exc: BaseException, *,
                       now: datetime) -> None:
    """Record an unexpected error as ``failed`` with its type, in its own transaction.

    So the Vault waits for its retry cadence like any failed read. When even
    that fails (the catalog is unavailable) nothing more is done: the original
    error is the one reported.
    """
    try:
        with database() as connection:
            _record_attempt(connection, vault_id, now=now, outcome="failed",
                            code=type(exc).__name__[:100])
    except Exception:  # noqa: BLE001 - the original error is reported instead
        _logger.warning("CloudWatch read of Vault %s could not be recorded", vault_id)


def run_due(database: Database, *, client_for: Callable[[str], Any] | None = None,
            now: datetime | None = None, should_stop: Callable[[], bool] | None = None,
            budget_seconds: float = PASS_BUDGET_SECONDS,
            clock: Callable[[], float] = _time.monotonic) -> int:
    """Read every due Vault Bucket, each saved in its own transaction.

    Returns how many Vaults were read (whatever the outcome). AWS errors are
    recorded on their Vault; an unexpected error is recorded as ``failed``
    and raised once every Vault was tried. A connection-level error stops the
    pass after recording its Vault; shutdown (``should_stop``, also checked
    between the calls of a read) or the ``budget_seconds`` of the pass
    (checked between Vaults) stop it too. The Vaults left are not recorded,
    so they stay due. Without usable credentials nothing is read or
    recorded.
    """
    moment = _utc(now or datetime.now(timezone.utc)).replace(microsecond=0)
    started = clock()

    def stopping() -> bool:
        return should_stop is not None and should_stop()

    with database() as connection:
        vaults = due_vaults(connection, now=moment)
    if not vaults:
        return 0
    factory = client_for or _default_client
    clients: dict[str, Any] = {}
    read = 0
    failure: Exception | None = None
    try:
        for vault in vaults:
            if stopping() or clock() - started >= budget_seconds:
                _logger.info("CloudWatch sizes pass stopped: %s Vault(s) left for later",
                             len(vaults) - read)
                break
            vault_id = int(vault["id"])
            try:
                region = str(vault["region"])
                if region not in clients:
                    clients[region] = factory(region)
                try:
                    days = read_bucket(clients[region], str(vault["bucket"]), now=moment, stop=stopping)
                except (ClientError, BotoCoreError) as exc:
                    with database() as connection:
                        outcome = record_failure(connection, vault_id, exc, now=moment)
                    read += 1
                    _logger.info("CloudWatch sizes of Vault %s not read: %s", vault_id, outcome)
                    if isinstance(exc, CONNECTION_ERRORS):
                        _logger.info("CloudWatch unreachable (%s): pass stopped", type(exc).__name__)
                        break
                else:
                    with database() as connection:
                        record_read(connection, vault_id, days, now=moment)
                    read += 1
            except (CloudWatchUnavailable, PassStopped):
                break
            except Exception as exc:  # noqa: BLE001 - reported once below
                _record_unexpected(database, vault_id, exc, now=moment)
                read += 1
                failure = failure or exc
    finally:
        if client_for is None:
            for client in clients.values():
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - never log provider details
                    pass
    if failure is not None:
        raise failure
    return read


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return _utc(value).astimezone(timezone.utc).isoformat()


def summarize_day(storage_bytes: dict[str, Any]) -> dict[str, Any]:
    """One day's bytes by FrostVault class: the objects' bytes and the overhead apart."""
    classes: dict[str, dict[str, Any]] = {}
    for storage_type, raw in sorted(storage_bytes.items()):
        meaning = STORAGE_TYPES.get(storage_type)
        value = int(raw or 0)
        if meaning is None or value <= 0:
            continue
        entry = classes.setdefault(meaning.storage_class, {
            "storage_class": meaning.storage_class, "bytes": 0, "overhead_bytes": 0, "overhead": []})
        if meaning.kind == "data":
            entry["bytes"] += value
        else:
            entry["overhead_bytes"] += value
            entry["overhead"].append({"storage_type": storage_type, "kind": meaning.kind,
                                      "billed_as": meaning.billed_as, "bytes": value})
    ordered = sorted(classes.values(), key=lambda entry: (
        CLASS_ORDER.index(entry["storage_class"]) if entry["storage_class"] in CLASS_ORDER
        else len(CLASS_ORDER), entry["storage_class"]))
    return {"bytes": sum(entry["bytes"] for entry in ordered),
            "overhead_bytes": sum(entry["overhead_bytes"] for entry in ordered),
            "by_storage_class": ordered}


def catalog_estimate(connection: Any, vault_id: int) -> dict[str, Any]:
    """The catalog's own view: versions not known missing, by class, and a Cost Estimate.

    Like the Minimum Storage Duration (``minimum_storage``), a version whose
    availability is still ``unknown`` counts: it is billed until proven gone.
    """
    prices = list_prices()
    rows = connection.execute(
        """SELECT av.storage_class, count(*) AS versions, COALESCE(sum(av.size), 0) AS bytes
           FROM archive_versions av JOIN files f ON f.id = av.file_id
           WHERE f.vault_id = %s AND av.availability <> 'missing'
           GROUP BY av.storage_class""", (vault_id,)).fetchall()
    classes: dict[str, dict[str, Any]] = {}
    for row in rows:
        storage_class = normalize_storage_class(row["storage_class"])
        entry = classes.setdefault(storage_class, {"storage_class": storage_class, "versions": 0,
                                                   "bytes": 0})
        entry["versions"] += int(row["versions"])
        entry["bytes"] += int(row["bytes"] or 0)
    for entry in classes.values():
        entry["monthly_cost"] = round(estimate_storage_month(
            prices, size_bytes=entry["bytes"], storage_class=entry["storage_class"]
        ).estimated_cost_eur, 6)
    ordered = sorted(classes.values(), key=lambda entry: (
        CLASS_ORDER.index(entry["storage_class"]) if entry["storage_class"] in CLASS_ORDER
        else len(CLASS_ORDER), entry["storage_class"]))
    return {
        "versions": sum(entry["versions"] for entry in ordered),
        "bytes": sum(entry["bytes"] for entry in ordered),
        "monthly_cost": round(sum(entry["monthly_cost"] for entry in ordered), 6),
        "currency": prices.currency, "region": prices.region,
        "pricing_effective_at": prices.effective_at,
        "by_storage_class": ordered,
    }


def measured_sizes(connection: Any, vault: dict[str, Any]) -> dict[str, Any]:
    """The CloudWatch sizes saved for a Vault, and why there are none."""
    if vault["key_layout"] != KEY_LAYOUT_BUCKET_ROOT:
        return {"status": "shared_bucket"}
    if vault["lifecycle"] == "provisioning":
        return {"status": "provisioning"}
    vault_id = int(vault["id"])
    attempt = connection.execute(
        "SELECT attempted_at, outcome, error_code FROM vault_bucket_metric_reads WHERE vault_id = %s",
        (vault_id,)).fetchone()
    rows = connection.execute(
        """SELECT day, storage_bytes, object_count, read_at FROM vault_bucket_metric_days
           WHERE vault_id = %s ORDER BY day DESC LIMIT %s""", (vault_id, HISTORY_DAYS)).fetchall()
    last_read = None if attempt is None else {
        "attempted_at": _iso(attempt["attempted_at"]), "outcome": attempt["outcome"],
        "error_code": attempt["error_code"]}
    next_read = None if attempt is None else _iso(
        next_read_after(attempt["attempted_at"], attempt["outcome"]))
    history = []
    for row in rows:
        day = summarize_day(row["storage_bytes"] or {})
        history.append({"day": row["day"].isoformat(), "bytes": day["bytes"],
                        "overhead_bytes": day["overhead_bytes"], "object_count": row["object_count"]})
    result: dict[str, Any] = {"last_read": last_read, "next_read_after": next_read,
                              "history": history}
    if not rows:
        result["status"] = "pending" if attempt is None or attempt["outcome"] == "ok" else attempt["outcome"]
        return result
    latest = rows[0]
    return {**result, "status": "available", "day": latest["day"].isoformat(),
            "read_at": _iso(latest["read_at"]), "object_count": latest["object_count"],
            **summarize_day(latest["storage_bytes"] or {})}


def vault_usage(connection: Any, vault_id: int) -> dict[str, Any]:
    """A Vault's catalog estimate next to its measured CloudWatch sizes.

    Reads only the catalog: no AWS request on the request path.
    """
    vault = connection.execute(
        """SELECT v.id, v.lifecycle, t.bucket, t.region, t.key_layout
           FROM vaults v JOIN vault_targets t ON t.id = v.target_id WHERE v.id = %s""",
        (vault_id,)).fetchone()
    if vault is None:
        raise LookupError("Vault not found")
    return {
        "vault_id": int(vault["id"]), "bucket": vault["bucket"], "region": vault["region"],
        "dedicated_bucket": vault["key_layout"] == KEY_LAYOUT_BUCKET_ROOT,
        "estimate": catalog_estimate(connection, vault_id),
        "measured": measured_sizes(connection, dict(vault)),
    }
