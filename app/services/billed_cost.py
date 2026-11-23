"""Billed Cost per Vault from the AWS Data Exports CUR 2.0 export (#738, ADR-0051).

The Owner creates, once, in the AWS Billing console, a CUR 2.0 export (gzip
CSV, with resource IDs) delivered to the System Bucket under ``billing/cur/``
(docs/billed-cost.md). At most once a day FrostVault looks for the newest
export there and reads it:

1. it lists ``billing/cur/<export>/metadata/``, keeps the two newest
   ``BILLING_PERIOD=YYYY-MM/`` folders and in each takes the newest object whose
   name ends in ``Manifest.json`` (``<export>-Manifest.json`` or
   ``Manifest.json``). AWS does not document the manifest's schema, so it is
   parsed tolerantly: when no data file list is recognised, the gzip CSV files
   under ``data/BILLING_PERIOD=YYYY-MM/`` are listed instead (in «create new»
   mode only the newest ``<timestamp>-<execution-id>/`` folder);
2. a period whose manifest (key, ETag, LastModified) and known buckets are
   unchanged since the last read keeps its totals and is not read again;
3. the data files of the other periods are streamed with the standard
   library: no file is ever held in memory whole;
4. only line items whose ``line_item_resource_id`` is a bucket FrostVault knows
   (name or ``arn:aws:s3:::`` ARN) are kept, so cost allocation tags are not
   needed and nothing of the rest of the account is stored. The amount is
   ``line_item_net_unblended_cost`` when the export has it and the cell is not
   empty, else ``line_item_unblended_cost``. Credit, refund and discount line
   items of a known bucket are their own category (``credits``);
5. the totals per billing period, bucket and category (``CATEGORIES``), in the
   billing currency, replace the stored ones at once.

A Vault Bucket's totals are its Vault's Billed Cost. The System Bucket and a
shared bucket of older Vaults are rows of the installation, never attributed
to one Vault. FrostVault never calls the Cost Explorer API.

A pass uses its own S3 client (short timeouts, two attempts), stops at the
first connection error, and runs within ``PASS_BUDGET_SECONDS``, checked
between files and every ``CHECK_EVERY_ROWS`` rows. Shutdown (``should_stop``)
stops it the same way. A stopped or over-budget pass records nothing: the
previous totals and status stay.

A missing export is not a Problem: the status says so and the UI shows the
setup steps. An export that cannot be read is a quiet status too, stored as an
error code (never a provider message): a transient error (S3 unreachable,
throttled, a 5xx) is retried after ``TRANSIENT_RETRY``, any other after
``READ_INTERVAL``.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import re
import threading
import time as _time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol

from botocore.exceptions import (
    BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError,
    NoCredentialsError, PartialCredentialsError, ReadTimeoutError,
)

from . import system_bucket
from . import s3

PREFIX = "billing/cur/"
# A read, or a deterministic error, makes the next look due a day later; while
# no export is there, or after a transient error, six hours later. Both are
# persisted: a restart never looks earlier.
READ_INTERVAL = timedelta(days=1)
ABSENT_INTERVAL = timedelta(hours=6)
TRANSIENT_RETRY = timedelta(hours=6)
# The background loop asks this often whether a look is due (one DB read).
POLL_SECONDS = 15 * 60
# Wall-clock budget of one pass, checked between files and every N rows.
PASS_BUDGET_SECONDS = 300.0
CHECK_EVERY_ROWS = 5_000
# Only the newest billing periods are listed: the current month and the one before.
PERIODS_LISTED = 2
# The data are «not up to date» when the export AWS delivered is older than
# this, or the current month is missing after its first STALE_GRACE_DAYS days.
STALE_AFTER = timedelta(days=3)
STALE_GRACE_DAYS = 3

CATEGORIES = ("storage", "requests", "transitions", "retrievals", "data_transfer", "other", "credits")
STATES = ("unavailable", "checking", "not_configured", "waiting", "ok", "error")
# Error codes stored in the status and translated by the UI. The first three
# are transient: retried after TRANSIENT_RETRY, the others after READ_INTERVAL.
TRANSIENT_ERRORS = ("s3_unreachable", "s3_throttled", "s3_server_error")
ERROR_CODES = TRANSIENT_ERRORS + (
    "credentials", "access_denied", "s3_refused", "parquet", "missing_column",
    "not_gzip_csv", "short_row", "cost_not_number", "currency_missing", "mixed_currencies",
    "manifest_invalid", "manifest_too_large", "manifest_foreign_bucket",
    "manifest_outside_folder", "no_data_files", "internal",
)

REQUIRED_COLUMNS = (
    "line_item_resource_id", "line_item_unblended_cost", "line_item_currency_code",
    "line_item_usage_type", "line_item_operation",
)
# A manifest is a small JSON document; a bigger one is not a manifest.
MANIFEST_MAX_BYTES = 8 * 1024 * 1024
# CUR 2.0 tag and product columns are JSON maps that can be long.
CSV_FIELD_LIMIT = 16 * 1024 * 1024
# Keys under which a manifest may list its data files (the schema is not documented).
MANIFEST_FILE_LISTS = ("dataFiles", "dataFileS3Keys", "reportKeys", "files")
MANIFEST_ENTRY_KEYS = ("key", "s3Key", "path", "s3Uri", "uri")
CONNECTION_ERRORS = (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)
_THROTTLING = frozenset({
    "Throttling", "ThrottlingException", "SlowDown", "RequestLimitExceeded",
    "TooManyRequestsException", "RequestThrottled", "RequestTimeout",
})
_ACCESS_DENIED = frozenset({"AccessDenied", "AllAccessDisabled", "InvalidAccessKeyId",
                            "SignatureDoesNotMatch", "AccountProblem"})
_ARN_PREFIX = "arn:aws:s3:::"
_PERIOD = re.compile(r"BILLING_PERIOD=(\d{4})-(\d{2})/$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# S3 usage types start with a region code (``EUC1-``, ``EU-``); none of the
# words below is one, so the prefix needs no stripping before matching.
_TRANSFER = re.compile(r"datatransfer|-(in|out)-bytes|-aws-(in|out)-|-cloudfront-")
_logger = logging.getLogger(__name__)


class BillingExportError(RuntimeError):
    """The export cannot be read: ``code`` is one of ``ERROR_CODES``.

    ``detail`` is safe to store and show: an S3 action and AWS error code, or
    column or currency names, never a provider message.
    """

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class PassStopped(RuntimeError):
    """Shutdown stopped the pass before its end: nothing is recorded."""


class PassOverBudget(RuntimeError):
    """The pass ran longer than its budget: nothing is recorded."""


@dataclass(frozen=True)
class BucketRole:
    role: str
    vault_id: int | None = None


@dataclass
class PeriodTotals:
    """The totals of one billing period: ``(bucket, category) -> amount``."""

    period: date
    amounts: dict[tuple[str, str], Decimal] = field(default_factory=dict)

    def add(self, bucket: str, category: str, amount: Decimal) -> None:
        key = (bucket, category)
        self.amounts[key] = self.amounts.get(key, Decimal(0)) + amount


@dataclass(frozen=True)
class ManifestRef:
    key: str
    period: date
    last_modified: datetime
    etag: str = ""


@dataclass(frozen=True)
class PeriodMark:
    """What a period's stored totals were read from (``billed_cost_periods``)."""

    period: date
    manifest_key: str
    manifest_etag: str
    manifest_modified: datetime
    roles_digest: str
    currency: str | None = None
    data_through: datetime | None = None

    def matches(self, ref: ManifestRef, digest: str) -> bool:
        return (self.manifest_key, self.manifest_etag, self.manifest_modified, self.roles_digest) == (
            ref.key, ref.etag, ref.last_modified, digest)


@dataclass(frozen=True)
class PeriodReading:
    """One period of a read: ``totals`` is None when its stored totals are kept."""

    mark: PeriodMark
    totals: PeriodTotals | None


@dataclass(frozen=True)
class ExportReading:
    """What one look under the prefix found."""

    state: str  # not_configured, waiting or ok
    manifest_key: str | None = None
    exported_at: datetime | None = None
    data_through: datetime | None = None
    currency: str | None = None
    periods: tuple[PeriodReading, ...] = ()


def categorize(usage_type: str, operation: str, product_code: str = "AmazonS3",
               line_item_type: str = "") -> str:
    """The category of a line item, from its type, usage type and operation.

    Credits, refunds and discounts are ``credits``. Lifecycle transitions are
    requests whose operation names the transition (``S3-GDATransition``);
    restores and retrievals name ``Restore`` or ``Retrieval``; storage is
    ``TimedStorage`` and the early-deletion charge of a Minimum Storage
    Duration (``EarlyDelete``); data transfer names the bytes moved; the
    remaining ``Requests-*`` are requests.
    """
    kind = (line_item_type or "").strip().lower()
    if kind in {"credit", "refund"} or kind.endswith("discount"):
        return "credits"
    usage = (usage_type or "").lower()
    op = (operation or "").lower()
    product = (product_code or "AmazonS3").strip().lower()
    if "transition" in op or "transition" in usage or "lifecycle" in op:
        return "transitions"
    if "restore" in op or "restore" in usage or "retrieval" in usage or "retrieval" in op:
        return "retrievals"
    if "timedstorage" in usage or "earlydelete" in usage or "storage-bytehrs" in usage:
        return "storage"
    if _TRANSFER.search(usage) or product == "awsdatatransfer":
        return "data_transfer"
    if "requests" in usage:
        return "requests"
    return "other"


def resource_bucket(resource_id: str) -> str | None:
    """The bucket a ``line_item_resource_id`` names: a bucket name or an S3 ARN."""
    value = (resource_id or "").strip()
    if value.startswith(_ARN_PREFIX):
        value = value[len(_ARN_PREFIX):].split("/", 1)[0]
    elif value.startswith("arn:"):
        return None
    return value or None


def roles_digest(roles: Mapping[str, BucketRole]) -> str:
    """A fingerprint of the known buckets: a change makes every period read again."""
    text = "\n".join(f"{name}\t{role.role}\t{role.vault_id}" for name, role in sorted(roles.items()))
    return hashlib.sha256(text.encode()).hexdigest()


def _parse_time(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _header_indexes(header: list[str]) -> dict[str, int]:
    names = [name.strip().lstrip("\ufeff").lower() for name in header]
    indexes = {name: index for index, name in enumerate(names)}
    missing = [name for name in REQUIRED_COLUMNS if name not in indexes]
    if missing:
        raise BillingExportError("missing_column", ", ".join(missing))
    return indexes


@dataclass
class _Accumulator:
    roles: Mapping[str, BucketRole]
    checkpoint: Callable[[], None] | None = None
    currency: str | None = None
    data_through: datetime | None = None
    rows: int = 0
    kept: int = 0


def _cell(row: list[str], index: int | None) -> str:
    return row[index].strip() if index is not None else ""


def aggregate_csv(text: Iterable[str], totals: PeriodTotals, state: _Accumulator) -> None:
    """Add the line items of one CSV text stream (header first) to ``totals``.

    Only rows of a bucket in ``state.roles`` add an amount; the currency and
    the newest usage end come from every row. Rows are read one at a time;
    nothing but the running totals is kept. ``state.checkpoint`` runs every
    ``CHECK_EVERY_ROWS`` rows.
    """
    reader = csv.reader(text)
    try:
        header = next(reader)
    except StopIteration:
        return  # An empty chunk: AWS empties extra chunks of a smaller refresh.
    indexes = _header_indexes(header)
    rid, cost = indexes["line_item_resource_id"], indexes["line_item_unblended_cost"]
    currency, usage = indexes["line_item_currency_code"], indexes["line_item_usage_type"]
    operation = indexes["line_item_operation"]
    net = indexes.get("line_item_net_unblended_cost")
    product = indexes.get("line_item_product_code")
    usage_end = indexes.get("line_item_usage_end_date")
    kind = indexes.get("line_item_line_item_type")
    width = max(indexes.values()) + 1
    for row in reader:
        state.rows += 1
        if state.checkpoint is not None and state.rows % CHECK_EVERY_ROWS == 0:
            state.checkpoint()
        if len(row) < width:
            if not any(cell.strip() for cell in row):
                continue
            raise BillingExportError("short_row")
        code = row[currency].strip().upper()
        if _CURRENCY.fullmatch(code):
            if state.currency is None:
                state.currency = code
            elif state.currency != code:
                raise BillingExportError("mixed_currencies", f"{state.currency}, {code}")
        if usage_end is not None:
            ended = _parse_time(row[usage_end])
            if ended is not None and (state.data_through is None or ended > state.data_through):
                state.data_through = ended
        bucket = resource_bucket(row[rid])
        if bucket is None or bucket not in state.roles:
            continue
        raw = _cell(row, net) or row[cost].strip()
        if not raw:
            continue
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            raise BillingExportError("cost_not_number") from None
        if not amount.is_finite():
            raise BillingExportError("cost_not_number")
        if not _CURRENCY.fullmatch(code):
            raise BillingExportError("currency_missing")
        category = categorize(row[usage], row[operation],
                              row[product] if product is not None else "AmazonS3", _cell(row, kind))
        totals.add(bucket, category, amount)
        state.kept += 1


# ``csv.field_size_limit`` is process-wide: it is raised only while a file is
# read, under this lock, and restored after it.
_CSV_LIMIT_LOCK = threading.Lock()


@contextmanager
def _csv_field_limit(limit: int) -> Iterator[None]:
    with _CSV_LIMIT_LOCK:
        previous = csv.field_size_limit()
        if previous < limit:
            csv.field_size_limit(limit)
        try:
            yield
        finally:
            csv.field_size_limit(previous)


def aggregate_gzip(stream: Any, totals: PeriodTotals, state: _Accumulator) -> None:
    """Stream one ``.csv.gz`` file object into ``totals`` (see ``aggregate_csv``)."""
    try:
        with _csv_field_limit(CSV_FIELD_LIMIT), gzip.GzipFile(fileobj=stream, mode="rb") as unzipped:
            text = io.TextIOWrapper(unzipped, encoding="utf-8", newline="")
            aggregate_csv(text, totals, state)
    except (gzip.BadGzipFile, EOFError, zlib.error, UnicodeDecodeError, csv.Error):
        raise BillingExportError("not_gzip_csv") from None


# -- S3 ------------------------------------------------------------------


def _list(client: Any, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
    """Every page of ``ListObjectsV2`` with ``Delimiter='/'``."""
    token: str | None = None
    while True:
        request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/"}
        if token:
            request["ContinuationToken"] = token
        page = client.list_objects_v2(**request)
        yield page
        token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
        if not token:
            return


def _children(client: Any, bucket: str, prefix: str) -> tuple[list[str], list[dict[str, Any]]]:
    """The direct subfolders and objects of ``prefix``."""
    folders: list[str] = []
    objects: list[dict[str, Any]] = []
    for page in _list(client, bucket, prefix):
        folders.extend(str(item["Prefix"]) for item in page.get("CommonPrefixes") or [])
        objects.extend(item for item in page.get("Contents") or []
                       if "/" not in str(item.get("Key", ""))[len(prefix):])
    return folders, objects


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = _parse_time(str(value))
    if parsed is None:
        raise BillingExportError("manifest_invalid")
    return parsed


def _newest_manifest(objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    found = [item for item in objects if str(item.get("Key", "")).endswith("Manifest.json")]
    return max(found, key=lambda item: (_aware(item.get("LastModified")), str(item["Key"])),
               default=None)


def _period_manifest(client: Any, bucket: str, folder: str, period: date) -> ManifestRef | None:
    """The newest ``…Manifest.json`` directly in ``folder``, else in its newest subfolder."""
    subfolders, objects = _children(client, bucket, folder)
    found = _newest_manifest(objects)
    if found is None and subfolders:
        # «Create new» without a manifest at the partition level: the newest
        # ``<timestamp>-<execution-id>/`` folder holds the latest one.
        _, objects = _children(client, bucket, max(subfolders))
        found = _newest_manifest(objects)
    if found is None:
        return None
    return ManifestRef(str(found["Key"]), period, _aware(found.get("LastModified")),
                       str(found.get("ETag") or "").strip('"'))


def find_manifests(client: Any, bucket: str, prefix: str = PREFIX) -> tuple[str, list[ManifestRef]]:
    """The newest export's state and its manifests, newest billing period first.

    One export is expected under the prefix; with several, the one delivered
    last wins. Only the ``PERIODS_LISTED`` newest billing period folders of an
    export are listed.
    """
    exports, loose = _children(client, bucket, prefix)
    if not exports:
        return ("waiting" if loose else "not_configured"), []
    best: list[ManifestRef] = []
    for export in exports:
        folders, _ = _children(client, bucket, f"{export}metadata/")
        dated: list[tuple[date, str]] = []
        for folder in folders:
            match = _PERIOD.search(folder)
            if match:
                dated.append((date(int(match.group(1)), int(match.group(2)), 1), folder))
        manifests: list[ManifestRef] = []
        for period, folder in sorted(dated, reverse=True)[:PERIODS_LISTED]:
            ref = _period_manifest(client, bucket, folder, period)
            if ref is not None:
                manifests.append(ref)
        if manifests and (not best or manifests[0].last_modified > best[0].last_modified):
            best = manifests
    if not best:
        return "waiting", []
    newest = best[0]
    previous_period = (newest.period - timedelta(days=1)).replace(day=1)
    previous = [ref for ref in best[1:] if ref.period == previous_period]
    return "ok", [newest, *previous[:1]]


def _data_key(entry: Any, bucket: str) -> str | None:
    """The key a manifest entry names, or None when it is not recognisable."""
    if isinstance(entry, dict):
        entry = next((entry[name] for name in MANIFEST_ENTRY_KEYS
                      if isinstance(entry.get(name), str)), None)
    if not isinstance(entry, str) or not entry.strip():
        return None
    key = entry.strip()
    if key.startswith("s3://"):
        named, _, key = key[len("s3://"):].partition("/")
        if named != bucket:
            raise BillingExportError("manifest_foreign_bucket")
    return key


def _checked_files(keys: list[str], data_prefix: str) -> list[str]:
    for key in keys:
        if not key.startswith(data_prefix) or ".." in key.split("/"):
            raise BillingExportError("manifest_outside_folder")
        if not key.endswith(".csv.gz"):
            raise BillingExportError("parquet" if key.endswith(".parquet") else "not_gzip_csv")
    return keys


def manifest_files(document: Any, bucket: str, data_prefix: str) -> list[str] | None:
    """The data files a manifest lists, or None when it has no recognisable list.

    AWS does not document the manifest's schema: a list under one of
    ``MANIFEST_FILE_LISTS`` of keys, ``s3://`` URIs or objects naming one is
    accepted. A file in another bucket or outside the period's data folder is
    refused, and so is a format other than gzip CSV.
    """
    if not isinstance(document, dict):
        return None
    entries = next((document[name] for name in MANIFEST_FILE_LISTS
                    if isinstance(document.get(name), list)), None)
    if not entries:
        return None
    keys = [_data_key(entry, bucket) for entry in entries]
    if any(key is None for key in keys):
        return None
    return _checked_files([key for key in keys if key is not None], data_prefix)


def listed_files(client: Any, bucket: str, data_prefix: str) -> list[str]:
    """The gzip CSV files under ``data_prefix``: in «create new» mode, only
    those of its newest ``<timestamp>-<execution-id>/`` folder."""
    folders, objects = _children(client, bucket, data_prefix)
    if folders:
        _, objects = _children(client, bucket, max(folders))
    keys = sorted(str(item["Key"]) for item in objects)
    files = [key for key in keys if key.endswith(".csv.gz")]
    if not files:
        raise BillingExportError("parquet" if any(key.endswith(".parquet") for key in keys)
                                 else "no_data_files")
    return files


def _read_manifest(client: Any, bucket: str, ref: ManifestRef) -> list[str]:
    response = client.get_object(Bucket=bucket, Key=ref.key)
    body = response["Body"]
    try:
        raw = body.read(MANIFEST_MAX_BYTES + 1)
    finally:
        body.close()
    if len(raw) > MANIFEST_MAX_BYTES:
        raise BillingExportError("manifest_too_large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BillingExportError("manifest_invalid") from None
    export_root = ref.key.split("metadata/", 1)[0]
    data_prefix = f"{export_root}data/BILLING_PERIOD={ref.period.strftime('%Y-%m')}/"
    files = manifest_files(document, bucket, data_prefix)
    return files if files is not None else listed_files(client, bucket, data_prefix)


def read_export(client: Any, bucket: str, roles: Mapping[str, BucketRole],
                prefix: str = PREFIX, *, known: Mapping[date, PeriodMark] | None = None,
                checkpoint: Callable[[], None] | None = None) -> ExportReading:
    """Look under ``prefix`` and, when an export is there, read what changed.

    A period in ``known`` whose manifest and known buckets are unchanged is
    not read: its ``PeriodReading.totals`` is None and its stored totals stay.
    ``checkpoint`` runs between files and every ``CHECK_EVERY_ROWS`` rows; it
    raises to stop the read.
    """
    def check() -> None:
        if checkpoint is not None:
            checkpoint()

    state, manifests = find_manifests(client, bucket, prefix)
    if state != "ok":
        return ExportReading(state)
    digest = roles_digest(roles)
    periods: list[PeriodReading] = []
    for ref in manifests:
        check()
        mark = (known or {}).get(ref.period)
        if mark is not None and mark.matches(ref, digest):
            periods.append(PeriodReading(mark, None))
            continue
        totals = PeriodTotals(ref.period)
        accumulator = _Accumulator(roles, checkpoint)
        for key in _read_manifest(client, bucket, ref):
            check()
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            try:
                aggregate_gzip(body, totals, accumulator)
            finally:
                body.close()
        through = accumulator.data_through
        if through is not None and through > ref.last_modified:
            through = ref.last_modified
        periods.append(PeriodReading(PeriodMark(
            ref.period, ref.key, ref.etag, ref.last_modified, digest,
            accumulator.currency, through), totals))
    check()
    currencies = {reading.mark.currency for reading in periods if reading.mark.currency}
    if len(currencies) > 1:
        raise BillingExportError("mixed_currencies", ", ".join(sorted(currencies)))
    newest = manifests[0]
    # A month without any line item still has a currency: AWS bills USD unless
    # the account chose another, and then every line names it.
    return ExportReading("ok", manifest_key=newest.key, exported_at=newest.last_modified,
                         data_through=periods[0].mark.data_through,
                         currency=next(iter(currencies), "USD"), periods=tuple(periods))


def classify_error(exc: BaseException) -> tuple[str, str | None]:
    """``(code, detail)`` of a failed look: never a provider message."""
    if isinstance(exc, BillingExportError):
        return exc.code, exc.detail
    if isinstance(exc, ClientError):
        operation = str(getattr(exc, "operation_name", "") or "")
        action = {"ListObjectsV2": "s3:ListBucket", "GetObject": "s3:GetObject"}.get(operation, "S3")
        code = s3.safe_error_code(exc)
        status = int((exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)
        if code in _THROTTLING or status == 429:
            return "s3_throttled", code
        if status >= 500 or code in {"InternalError", "ServiceUnavailable"}:
            return "s3_server_error", code
        if code in _ACCESS_DENIED or status == 403:
            return "access_denied", f"{action} ({code})"
        return "s3_refused", f"{action} ({code})"
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return "credentials", None
    if isinstance(exc, CONNECTION_ERRORS):
        # The pass already stopped at this first one: nothing else was asked.
        return "s3_unreachable", type(exc).__name__[:100]
    if isinstance(exc, BotoCoreError):
        return "s3_unreachable", type(exc).__name__[:100]
    if isinstance(exc, RuntimeError) and "credentials are not configured" in str(exc):
        # ``s3.s3_client`` with placeholder credentials.
        return "credentials", None
    return "internal", type(exc).__name__[:100]


# -- catalog ---------------------------------------------------------------


def bucket_roles(connection: Any, system: str | None) -> dict[str, BucketRole]:
    """Every bucket FrostVault knows: Vault Buckets, shared buckets, the System Bucket."""
    roles: dict[str, BucketRole] = {}
    if system:
        roles[system] = BucketRole("system")
    for row in connection.execute(
        "SELECT t.bucket, t.key_layout, v.id AS vault_id "
        "FROM vault_targets t JOIN vaults v ON v.target_id = t.id"
    ).fetchall():
        name = str(row["bucket"])
        if row["key_layout"] == "bucket_root":
            roles[name] = BucketRole("vault", int(row["vault_id"]))
        else:
            roles[name] = BucketRole("shared")
    return roles


@dataclass(frozen=True)
class Status:
    state: str
    checked_at: datetime
    bucket: str | None = None
    read_at: datetime | None = None
    manifest_key: str | None = None
    exported_at: datetime | None = None
    data_through: datetime | None = None
    currency: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


def next_check_at(status: Status | None) -> datetime | None:
    if status is None:
        return None
    soon = (status.state in {"not_configured", "waiting"}
            or (status.state == "error" and status.error_code in TRANSIENT_ERRORS))
    return status.checked_at + (ABSENT_INTERVAL if soon else READ_INTERVAL)


class BilledCostStore(Protocol):
    def load(self) -> Status | None: ...

    def claim(self, previous: datetime | None, now: datetime) -> bool:
        """Start a look at ``now`` unless another process started one since ``previous``."""

    def release(self, claimed: datetime, previous: datetime | None) -> None:
        """Undo a claim at ``claimed`` (shutdown): the look is due as before it."""

    def marks(self) -> dict[date, PeriodMark]: ...

    def save_absent(self, state: str, bucket: str) -> None: ...

    def save_error(self, bucket: str, code: str, detail: str | None) -> None: ...

    def save_reading(self, bucket: str, reading: ExportReading,
                     roles: Mapping[str, BucketRole], now: datetime) -> None: ...

    def roles(self, system: str) -> dict[str, BucketRole]: ...


_STATUS_COLUMNS = ("state", "checked_at", "bucket", "read_at", "manifest_key", "exported_at",
                   "data_through", "currency", "error_code", "error_detail")
_STATUS_SQL = f"SELECT {', '.join(_STATUS_COLUMNS)} FROM billing_export_status WHERE id = 1"


def load_status(connection: Any) -> Status | None:
    row = connection.execute(_STATUS_SQL).fetchone()
    return None if row is None else Status(**{name: row[name] for name in _STATUS_COLUMNS})


def load_marks(connection: Any) -> dict[date, PeriodMark]:
    return {row["billing_period"]: PeriodMark(
        row["billing_period"], row["manifest_key"], row["manifest_etag"],
        row["manifest_modified"], row["roles_digest"], row["currency"], row["data_through"],
    ) for row in connection.execute(
        "SELECT billing_period, manifest_key, manifest_etag, manifest_modified, roles_digest, "
        "currency, data_through FROM billed_cost_periods").fetchall()}


class DatabaseStore:
    """``billing_export_status``, ``billed_cost_periods`` and ``billed_costs``;
    each method commits on its own."""

    def __init__(self, database: Callable[[], Any] | None = None):
        if database is None:
            from ..database import db as database
        self._database = database

    def load(self) -> Status | None:
        with self._database() as connection:
            return load_status(connection)

    def claim(self, previous: datetime | None, now: datetime) -> bool:
        with self._database() as connection:
            if previous is None:
                return connection.execute(
                    "INSERT INTO billing_export_status(id, state, checked_at) "
                    "VALUES (1, 'checking', %s) ON CONFLICT (id) DO NOTHING", (now,),
                ).rowcount == 1
            return connection.execute(
                "UPDATE billing_export_status SET checked_at = %s "
                "WHERE id = 1 AND checked_at = %s", (now, previous),
            ).rowcount == 1

    def release(self, claimed: datetime, previous: datetime | None) -> None:
        with self._database() as connection:
            if previous is None:
                connection.execute(
                    "DELETE FROM billing_export_status WHERE id = 1 AND checked_at = %s "
                    "AND state = 'checking'", (claimed,))
            else:
                connection.execute(
                    "UPDATE billing_export_status SET checked_at = %s "
                    "WHERE id = 1 AND checked_at = %s", (previous, claimed))

    def marks(self) -> dict[date, PeriodMark]:
        with self._database() as connection:
            return load_marks(connection)

    def save_absent(self, state: str, bucket: str) -> None:
        # Totals of an export that is no longer there describe nothing.
        with self._database() as connection:
            connection.execute("DELETE FROM billed_cost_periods")
            connection.execute(
                "UPDATE billing_export_status SET state = %s, bucket = %s, error_code = NULL, "
                "error_detail = NULL WHERE id = 1", (state, bucket))

    def save_error(self, bucket: str, code: str, detail: str | None) -> None:
        # The totals of the last good read stay, but are not shown while broken.
        with self._database() as connection:
            connection.execute(
                "UPDATE billing_export_status SET state = 'error', bucket = %s, error_code = %s, "
                "error_detail = %s WHERE id = 1",
                (bucket, code, detail[:200] if detail else None))

    def save_reading(self, bucket: str, reading: ExportReading,
                     roles: Mapping[str, BucketRole], now: datetime) -> None:
        with self._database() as connection:
            # Vaults deleted while the export was read lose their rows, not the read.
            live = {int(row["id"]) for row in connection.execute("SELECT id FROM vaults").fetchall()}
            kept = [period.mark.period for period in reading.periods]
            # Periods no longer listed go, with their totals (ON DELETE CASCADE).
            connection.execute(
                "DELETE FROM billed_cost_periods WHERE NOT (billing_period = ANY(%s))", (kept,))
            for period in reading.periods:
                if period.totals is None:
                    continue  # Unchanged since the last read: its totals stay.
                mark = period.mark
                connection.execute("DELETE FROM billed_cost_periods WHERE billing_period = %s",
                                   (mark.period,))
                connection.execute(
                    "INSERT INTO billed_cost_periods(billing_period, manifest_key, manifest_etag, "
                    "manifest_modified, roles_digest, currency, data_through, read_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (mark.period, mark.manifest_key, mark.manifest_etag, mark.manifest_modified,
                     mark.roles_digest, mark.currency, mark.data_through, now))
                for (bucket_name, category), amount in sorted(period.totals.amounts.items()):
                    role = roles[bucket_name]
                    if role.vault_id is not None and role.vault_id not in live:
                        continue
                    connection.execute(
                        "INSERT INTO billed_costs(billing_period, bucket, role, vault_id, "
                        "category, amount) VALUES (%s, %s, %s, %s, %s, %s)",
                        (mark.period, bucket_name, role.role, role.vault_id, category, amount))
            connection.execute(
                "UPDATE billing_export_status SET state = 'ok', bucket = %s, read_at = %s, "
                "manifest_key = %s, exported_at = %s, data_through = %s, currency = %s, "
                "error_code = NULL, error_detail = NULL WHERE id = 1",
                (bucket, now, reading.manifest_key, reading.exported_at,
                 reading.data_through, reading.currency))

    def roles(self, system: str) -> dict[str, BucketRole]:
        with self._database() as connection:
            return bucket_roles(connection, system)


def _default_client(region: str) -> Any:
    """A private S3 client for this pass: 3 s to connect, 10 s per read, 2 attempts."""
    from botocore.config import Config

    if s3.credentials_are_placeholders():
        raise BillingExportError("credentials")
    return s3.new_service_client(
        "s3", region=region,
        config=Config(connect_timeout=3, read_timeout=10,
                      retries={"mode": "standard", "total_max_attempts": 2}))


def run_due(*, store: BilledCostStore | None = None,
            location: Callable[[], system_bucket.SystemBucket | None] | None = None,
            client_for: Callable[[str], Any] | None = None,
            now: datetime | None = None, should_stop: Callable[[], bool] | None = None,
            budget_seconds: float = PASS_BUDGET_SECONDS,
            clock: Callable[[], float] = _time.monotonic) -> bool:
    """Look for and read the export when a look is due; True when one ran.

    Without a ready System Bucket nothing is due. Every outcome of a look is a
    status, never an exception or a Problem: a missing export is
    ``not_configured`` or ``waiting``, a broken one ``error``. Shutdown
    (``should_stop``) and ``budget_seconds`` stop the pass without recording
    anything: on shutdown the look is due again as before it, over budget the
    next look waits for the usual interval.
    """
    store = store or DatabaseStore()
    bucket = (location or system_bucket.location)()
    if bucket is None or not bucket.ready:
        return False
    current = now or datetime.now(timezone.utc)
    status = store.load()
    due = next_check_at(status)
    # Another System Bucket (METADATA_BACKUP_BUCKET set or changed): what was
    # read describes the old one, so look in the new one at once.
    moved = status is not None and status.bucket not in (None, bucket.name)
    if due is not None and current < due and not moved:
        return False
    previous = status.checked_at if status is not None else None
    if not store.claim(previous, current):
        return False
    started = clock()

    def checkpoint() -> None:
        if should_stop is not None and should_stop():
            raise PassStopped("shutdown")
        if clock() - started >= budget_seconds:
            raise PassOverBudget(f"over {budget_seconds:.0f} s")

    client = None
    try:
        roles = store.roles(bucket.name)
        known = {} if moved else store.marks()
        client = (client_for or _default_client)(bucket.region)
        reading = read_export(client, bucket.name, roles, known=known, checkpoint=checkpoint)
        checkpoint()
    except PassStopped:
        _logger.info("Billing export read stopped by shutdown: nothing recorded")
        store.release(current, previous)
        return True
    except PassOverBudget:
        _logger.info("Billing export read over its %.0f s budget: nothing recorded", budget_seconds)
        return True
    except Exception as exc:  # noqa: BLE001 - every failure is a quiet status
        code, detail = classify_error(exc)
        store.save_error(bucket.name, code, detail)
        return True
    finally:
        if client is not None and client_for is None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - never log provider details
                pass
    if reading.state == "ok":
        store.save_reading(bucket.name, reading, roles, current)
    else:
        store.save_absent(reading.state, bucket.name)
    return True


# -- views -----------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _money(value: Decimal) -> float:
    return float(round(value, 6))


def _categories(rows: Iterable[Any]) -> dict[str, float]:
    sums = {name: Decimal(0) for name in CATEGORIES}
    for row in rows:
        sums[row["category"]] += Decimal(row["amount"])
    return {name: _money(amount) for name, amount in sums.items()}


def _periods_read(connection: Any) -> list[date]:
    """The billing periods of the stored read, newest first."""
    return [row["billing_period"] for row in connection.execute(
        "SELECT billing_period FROM billed_cost_periods ORDER BY billing_period DESC").fetchall()]


def is_stale(status: Status | None, periods: Iterable[date], now: datetime) -> bool:
    """«Not up to date»: AWS delivered the export more than ``STALE_AFTER`` ago,
    or the current month is missing after its first ``STALE_GRACE_DAYS`` days."""
    if status is None or status.exported_at is None:
        return False
    moment = now.astimezone(timezone.utc)
    if moment - status.exported_at > STALE_AFTER:
        return True
    return moment.day > STALE_GRACE_DAYS and moment.date().replace(day=1) not in set(periods)


def _status_fields(connection: Any, status: Status | None,
                   bucket: system_bucket.SystemBucket | None, now: datetime | None) -> dict[str, Any]:
    state = "unavailable" if bucket is None else (status.state if status else "checking")
    if bucket is not None and status is not None and status.bucket not in (None, bucket.name):
        # The System Bucket changed: what was read describes the old one.
        state = "checking"
    shown = status is not None and state == "ok"
    periods = _periods_read(connection) if shown else []
    errored = status is not None and state == "error"
    return {
        "state": state,
        "bucket": bucket.name if bucket is not None else None,
        "prefix": PREFIX,
        "checked_at": _iso(status.checked_at) if status else None,
        "next_check_at": _iso(next_check_at(status)) if status and bucket else None,
        "read_at": _iso(status.read_at) if shown else None,
        "exported_at": _iso(status.exported_at) if shown else None,
        "data_through": _iso(status.data_through) if shown else None,
        "currency": status.currency if shown else None,
        "stale": shown and is_stale(status, periods, now or datetime.now(timezone.utc)),
        "error_code": status.error_code if errored else None,
        "error_detail": status.error_detail if errored else None,
    }, periods


def installation_view(connection: Any, cfg: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
    """The export's status and, when read, every bucket's totals by period.

    ``vaults_total`` is the sum of the Vault rows only (FrostVault's Vault
    Buckets), ``total`` adds the installation rows.
    """
    bucket = system_bucket.location(cfg, connection=connection)
    view, periods_read = _status_fields(connection, load_status(connection), bucket, now)
    view["periods"] = []
    if view["state"] != "ok":
        return view
    rows = connection.execute(
        "SELECT b.billing_period, b.bucket, b.role, b.vault_id, v.name AS vault_name, "
        "b.category, b.amount FROM billed_costs b LEFT JOIN vaults v ON v.id = b.vault_id "
        "ORDER BY b.billing_period DESC, b.role, lower(v.name), b.bucket"
    ).fetchall()
    periods: dict[date, dict[tuple[str, str], list[Any]]] = {period: {} for period in periods_read}
    for row in rows:
        periods.setdefault(row["billing_period"], {}).setdefault(
            (row["role"], row["bucket"]), []).append(row)
    for period, buckets in sorted(periods.items(), reverse=True):
        vaults, installation = [], []
        total = vaults_total = Decimal(0)
        for (role, name), items in buckets.items():
            amount = sum((Decimal(item["amount"]) for item in items), Decimal(0))
            total += amount
            entry = {"bucket": name, "total": _money(amount), "categories": _categories(items)}
            if role == "vault":
                vaults_total += amount
                vaults.append({"vault_id": int(items[0]["vault_id"]),
                               "name": items[0]["vault_name"], **entry})
            else:
                installation.append({"role": role, **entry})
        view["periods"].append({"period": period.strftime("%Y-%m"), "total": _money(total),
                                "vaults_total": _money(vaults_total),
                                "vaults": vaults, "installation": installation})
    return view


def vault_view(connection: Any, vault_id: int, cfg: Any = None, *,
               now: datetime | None = None) -> dict[str, Any]:
    """One Vault's Billed Cost by period, or why there is none to show.

    A Vault in a shared bucket has no Billed Cost of its own: ``shared`` is
    true and ``periods`` stays empty; the bucket is an installation row of
    the System page.
    """
    vault = connection.execute(
        "SELECT v.id, t.bucket, t.key_layout FROM vaults v "
        "JOIN vault_targets t ON t.id = v.target_id WHERE v.id = %s", (vault_id,),
    ).fetchone()
    if vault is None:
        raise LookupError(vault_id)
    bucket = system_bucket.location(cfg, connection=connection)
    view, periods_read = _status_fields(connection, load_status(connection), bucket, now)
    shared = vault["key_layout"] != "bucket_root"
    view.update({"vault_id": int(vault["id"]), "vault_bucket": vault["bucket"],
                 "shared": shared, "periods": []})
    if view["state"] != "ok" or shared:
        return view
    rows = connection.execute(
        "SELECT billing_period, category, amount FROM billed_costs WHERE vault_id = %s",
        (vault_id,),
    ).fetchall()
    by_period: dict[date, list[Any]] = {period: [] for period in periods_read}
    for row in rows:
        by_period.setdefault(row["billing_period"], []).append(row)
    # A period of the export without a line item of this bucket costs nothing.
    view["periods"] = [{
        "period": period.strftime("%Y-%m"),
        "total": _money(sum((Decimal(item["amount"]) for item in items), Decimal(0))),
        "categories": _categories(items),
    } for period, items in sorted(by_period.items(), reverse=True)]
    return view
