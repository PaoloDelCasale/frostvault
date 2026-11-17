"""Billed Cost per Vault from the AWS Data Exports CUR 2.0 export (#738, ADR-0051).

The Owner creates, once, in the AWS Billing console, a CUR 2.0 export (gzip
CSV, with resource IDs) delivered to the System Bucket under ``billing/cur/``
(docs/billed-cost.md). At most once a day FrostVault looks for the newest
export there and reads it:

1. it lists ``billing/cur/<export>/metadata/BILLING_PERIOD=YYYY-MM/`` and takes
   the newest ``<export>-Manifest.json`` (the one AWS overwrites with every
   refresh) and the one of the month before, when present;
2. it reads the data files each manifest lists, streaming the gzip CSV with
   the standard library: no file is ever held in memory whole;
3. it keeps only the line items whose ``line_item_resource_id`` is a bucket
   FrostVault knows (name or ``arn:aws:s3:::`` ARN), so cost allocation tags
   are not needed and nothing of the rest of the account is stored;
4. it replaces every stored total, per billing period, bucket and category
   (``CATEGORIES``), in the billing currency, at once.

A Vault Bucket's totals are its Vault's Billed Cost. The System Bucket and a
shared bucket of older Vaults are rows of the installation, never attributed
to one Vault. FrostVault never calls the Cost Explorer API.

A missing export is not a Problem: the status says so and the UI shows the
Cost Estimate and the setup steps. An export that cannot be read is a quiet
status too, retried the next day, never a notification.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from . import system_bucket
from .bucket_provisioning import error_code
from .cost_estimates import estimate_storage_month, list_prices

PREFIX = "billing/cur/"
# A read (or a look that found an error) is due again after a day; while no
# export is there, a look (a few list requests) every six hours finds the
# first delivery sooner. Both are persisted: a restart never looks earlier.
READ_INTERVAL = timedelta(days=1)
ABSENT_INTERVAL = timedelta(hours=6)
# The background loop asks this often whether a look is due (one DB read).
POLL_SECONDS = 15 * 60

CATEGORIES = ("storage", "requests", "transitions", "retrievals", "data_transfer", "other")
ROLES = ("vault", "system", "shared")
STATES = ("unavailable", "checking", "not_configured", "waiting", "ok", "error")

REQUIRED_COLUMNS = (
    "line_item_resource_id", "line_item_unblended_cost", "line_item_currency_code",
    "line_item_usage_type", "line_item_operation",
)
OPTIONAL_COLUMNS = ("line_item_product_code", "line_item_usage_end_date")
# A manifest is a small JSON document; a bigger one is not a manifest.
MANIFEST_MAX_BYTES = 8 * 1024 * 1024
# CUR 2.0 tag and product columns are JSON maps that can be long.
CSV_FIELD_LIMIT = 16 * 1024 * 1024
_ARN_PREFIX = "arn:aws:s3:::"
_PERIOD = re.compile(r"BILLING_PERIOD=(\d{4})-(\d{2})/$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# S3 usage types start with a region code (``EUC1-``, ``EU-``); none of the
# words below is one, so the prefix needs no stripping before matching.
_TRANSFER = re.compile(r"datatransfer|-(in|out)-bytes|-aws-(in|out)-|-cloudfront-")


class BillingExportError(RuntimeError):
    """The export cannot be read; the message is safe to store and show."""


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
class ExportReading:
    """What one look under the prefix found."""

    state: str  # not_configured, waiting or ok
    manifest_key: str | None = None
    exported_at: datetime | None = None
    data_through: datetime | None = None
    currency: str | None = None
    periods: tuple[PeriodTotals, ...] = ()


@dataclass(frozen=True)
class ManifestRef:
    key: str
    period: date
    last_modified: datetime


def categorize(usage_type: str, operation: str, product_code: str = "AmazonS3") -> str:
    """The category of a line item, from its usage type and operation.

    Lifecycle transitions are requests whose operation names the transition
    (``S3-GDATransition``); restores and retrievals name ``Restore`` or
    ``Retrieval``; storage is ``TimedStorage`` and the early-deletion charge of a
    Minimum Storage Duration (``EarlyDelete``); data transfer names the bytes
    moved; the remaining ``Requests-*`` are requests.
    """
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


def _parse_time(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _header_indexes(header: list[str]) -> dict[str, int]:
    names = [name.strip().lstrip("\ufeff").lower() for name in header]
    indexes = {name: index for index, name in enumerate(names)}
    missing = [name for name in REQUIRED_COLUMNS if name not in indexes]
    if missing:
        hint = (" (enable «Include resource IDs» on the export)"
                if "line_item_resource_id" in missing else "")
        raise BillingExportError(
            "The export lacks the column " + ", ".join(missing) + hint)
    return indexes


@dataclass
class _Accumulator:
    roles: Mapping[str, BucketRole]
    currency: str | None = None
    data_through: datetime | None = None
    rows: int = 0


def aggregate_csv(text: Iterable[str], totals: PeriodTotals, state: _Accumulator) -> None:
    """Add the line items of one CSV text stream (header first) to ``totals``.

    Only rows of a bucket in ``state.roles`` count. Rows are read one at a
    time; nothing but the running totals is kept.
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
    product = indexes.get("line_item_product_code")
    usage_end = indexes.get("line_item_usage_end_date")
    width = max(indexes.values()) + 1
    for row in reader:
        if len(row) < width:
            if not any(cell.strip() for cell in row):
                continue
            raise BillingExportError("The export has a row with fewer columns than its header")
        bucket = resource_bucket(row[rid])
        if bucket is None:
            continue
        role = state.roles.get(bucket)
        if role is None:
            continue
        raw = row[cost].strip()
        if not raw:
            continue
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            raise BillingExportError("The export has a cost that is not a number") from None
        if not amount.is_finite():
            raise BillingExportError("The export has a cost that is not a number")
        code = row[currency].strip().upper()
        if not _CURRENCY.fullmatch(code):
            raise BillingExportError("The export has a line item without a currency code")
        if state.currency is None:
            state.currency = code
        elif state.currency != code:
            raise BillingExportError(
                f"The export mixes currencies ({state.currency}, {code})")
        category = categorize(row[usage], row[operation],
                              row[product] if product is not None else "AmazonS3")
        totals.add(bucket, category, amount)
        state.rows += 1
        if usage_end is not None:
            ended = _parse_time(row[usage_end])
            if ended is not None and (state.data_through is None or ended > state.data_through):
                state.data_through = ended


def aggregate_gzip(stream: Any, totals: PeriodTotals, state: _Accumulator) -> None:
    """Stream one ``.csv.gz`` file object into ``totals`` (see ``aggregate_csv``)."""
    previous = csv.field_size_limit()
    if previous < CSV_FIELD_LIMIT:
        csv.field_size_limit(CSV_FIELD_LIMIT)
    try:
        with gzip.GzipFile(fileobj=stream, mode="rb") as unzipped:
            text = io.TextIOWrapper(unzipped, encoding="utf-8", newline="")
            aggregate_csv(text, totals, state)
    except (gzip.BadGzipFile, EOFError, zlib.error, UnicodeDecodeError, csv.Error):
        raise BillingExportError("A data file of the export is not a readable gzip CSV") from None


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
    folders: list[str] = []
    objects: list[dict[str, Any]] = []
    for page in _list(client, bucket, prefix):
        folders.extend(str(item["Prefix"]) for item in page.get("CommonPrefixes") or [])
        objects.extend(page.get("Contents") or [])
    return folders, objects


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = _parse_time(str(value))
    if parsed is None:
        raise BillingExportError("S3 listed a manifest without a modification time")
    return parsed


def find_manifests(client: Any, bucket: str, prefix: str = PREFIX) -> tuple[str, list[ManifestRef]]:
    """The newest export's state and its manifests, newest billing period first.

    One export is expected under the prefix; with several, the one delivered
    last wins. Only the partition-level ``<export>-Manifest.json`` counts: in
    «create new» mode AWS overwrites it with every refresh as well.
    """
    exports, loose = _children(client, bucket, prefix)
    if not exports:
        return ("waiting" if loose else "not_configured"), []
    best: list[ManifestRef] = []
    for export in exports:
        name = export[len(prefix):].rstrip("/")
        periods, _ = _children(client, bucket, f"{export}metadata/")
        manifests: list[ManifestRef] = []
        for period_prefix in periods:
            match = _PERIOD.search(period_prefix)
            if not match:
                continue
            _, objects = _children(client, bucket, period_prefix)
            key = f"{period_prefix}{name}-Manifest.json"
            found = next((item for item in objects if item.get("Key") == key), None)
            if found is not None:
                manifests.append(ManifestRef(
                    key, date(int(match.group(1)), int(match.group(2)), 1),
                    _aware(found.get("LastModified"))))
        manifests.sort(key=lambda ref: ref.period, reverse=True)
        if manifests and (not best or manifests[0].last_modified > best[0].last_modified):
            best = manifests
    if not best:
        return "waiting", []
    newest = best[0]
    previous_period = (newest.period - timedelta(days=1)).replace(day=1)
    previous = [ref for ref in best[1:] if ref.period == previous_period]
    return "ok", [newest, *previous[:1]]


def _manifest_files(document: Any, bucket: str, data_prefix: str) -> list[str]:
    if not isinstance(document, dict):
        raise BillingExportError("The export's Manifest.json is not a JSON object")
    entries: Any = None
    for name in ("dataFiles", "dataFileS3Keys", "reportKeys"):
        if name in document:
            entries = document[name]
            break
    if not isinstance(entries, list):
        raise BillingExportError("The export's Manifest.json lists no data files")
    keys: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            entry = entry.get("key") or entry.get("s3Key") or entry.get("path")
        if not isinstance(entry, str) or not entry.strip():
            raise BillingExportError("The export's Manifest.json has an unreadable data file entry")
        key = entry.strip()
        if key.startswith("s3://"):
            location = key[len("s3://"):]
            named, _, key = location.partition("/")
            if named != bucket:
                raise BillingExportError("The export's Manifest.json names another bucket")
        if not key.startswith(data_prefix) or ".." in key.split("/"):
            raise BillingExportError("The export's Manifest.json names a file outside its folder")
        if not key.endswith(".csv.gz"):
            raise BillingExportError(
                "The export is not gzip CSV: choose «gzip – text/csv» as its file format")
        keys.append(key)
    return keys


def _read_manifest(client: Any, bucket: str, ref: ManifestRef) -> list[str]:
    response = client.get_object(Bucket=bucket, Key=ref.key)
    body = response["Body"]
    try:
        raw = body.read(MANIFEST_MAX_BYTES + 1)
    finally:
        body.close()
    if len(raw) > MANIFEST_MAX_BYTES:
        raise BillingExportError("The export's Manifest.json is too large to be a manifest")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BillingExportError("The export's Manifest.json is not valid JSON") from None
    export_root = ref.key.split("metadata/", 1)[0]
    period = ref.period.strftime("%Y-%m")
    return _manifest_files(document, bucket, f"{export_root}data/BILLING_PERIOD={period}/")


def read_export(client: Any, bucket: str, roles: Mapping[str, BucketRole],
                prefix: str = PREFIX) -> ExportReading:
    """Look under ``prefix`` and, when an export is there, read it."""
    state, manifests = find_manifests(client, bucket, prefix)
    if state != "ok":
        return ExportReading(state)
    accumulator = _Accumulator(roles)
    periods: list[PeriodTotals] = []
    for ref in manifests:
        totals = PeriodTotals(ref.period)
        for key in _read_manifest(client, bucket, ref):
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            try:
                aggregate_gzip(body, totals, accumulator)
            finally:
                body.close()
        periods.append(totals)
    newest = manifests[0]
    through = accumulator.data_through
    if through is not None and through > newest.last_modified:
        through = newest.last_modified
    # A month with no FrostVault line item still has a currency: AWS bills USD
    # unless the account chose another, and then every line names it.
    return ExportReading("ok", manifest_key=newest.key, exported_at=newest.last_modified,
                         data_through=through, currency=accumulator.currency or "USD",
                         periods=tuple(periods))


def safe_error(exc: BaseException) -> str:
    """A message for the status: never a provider message."""
    if isinstance(exc, BillingExportError):
        return str(exc)
    if isinstance(exc, ClientError):
        operation = str(getattr(exc, "operation_name", "") or "")
        action = {"ListObjectsV2": "s3:ListBucket", "GetObject": "s3:GetObject"}.get(
            operation, "the request")
        return f"S3 refused {action} ({error_code(exc)})"
    if isinstance(exc, BotoCoreError):
        return f"S3 could not be reached ({type(exc).__name__})"
    return f"internal error ({type(exc).__name__})"


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
    error: str | None = None


def next_check_at(status: Status | None) -> datetime | None:
    if status is None:
        return None
    absent = status.state in {"not_configured", "waiting"}
    return status.checked_at + (ABSENT_INTERVAL if absent else READ_INTERVAL)


class BilledCostStore(Protocol):
    def load(self) -> Status | None: ...

    def claim(self, previous: datetime | None, now: datetime) -> bool:
        """Start a look at ``now`` unless another process started one since ``previous``."""

    def save_absent(self, state: str, bucket: str) -> None: ...

    def save_error(self, bucket: str, error: str) -> None: ...

    def save_reading(self, bucket: str, reading: ExportReading,
                     roles: Mapping[str, BucketRole], now: datetime) -> None: ...

    def roles(self, system: str) -> dict[str, BucketRole]: ...


def _status(row: Any) -> Status | None:
    if row is None:
        return None
    return Status(**{name: row[name] for name in (
        "state", "checked_at", "bucket", "read_at", "manifest_key", "exported_at",
        "data_through", "currency", "error")})


_STATUS_SQL = ("SELECT state, checked_at, bucket, read_at, manifest_key, exported_at, "
               "data_through, currency, error FROM billing_export_status WHERE id = 1")


def load_status(connection: Any) -> Status | None:
    return _status(connection.execute(_STATUS_SQL).fetchone())


class DatabaseStore:
    """``billing_export_status`` and ``billed_costs``; each method commits on its own."""

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

    def save_absent(self, state: str, bucket: str) -> None:
        # Totals of an export that is no longer there describe nothing.
        with self._database() as connection:
            connection.execute("DELETE FROM billed_costs")
            connection.execute(
                "UPDATE billing_export_status SET state = %s, bucket = %s, error = NULL "
                "WHERE id = 1", (state, bucket))

    def save_error(self, bucket: str, error: str) -> None:
        # The totals of the last good read stay, but are not shown while broken.
        with self._database() as connection:
            connection.execute(
                "UPDATE billing_export_status SET state = 'error', bucket = %s, error = %s "
                "WHERE id = 1", (bucket, error[:500]))

    def save_reading(self, bucket: str, reading: ExportReading,
                     roles: Mapping[str, BucketRole], now: datetime) -> None:
        with self._database() as connection:
            # Vaults deleted while the export was read lose their rows, not the read.
            live = {int(row["id"]) for row in connection.execute("SELECT id FROM vaults").fetchall()}
            connection.execute("DELETE FROM billed_costs")
            for totals in reading.periods:
                for (bucket_name, category), amount in sorted(totals.amounts.items()):
                    role = roles[bucket_name]
                    if role.vault_id is not None and role.vault_id not in live:
                        continue
                    connection.execute(
                        "INSERT INTO billed_costs(billing_period, bucket, role, vault_id, "
                        "category, amount) VALUES (%s, %s, %s, %s, %s, %s)",
                        (totals.period, bucket_name, role.role, role.vault_id, category, amount))
            connection.execute(
                "UPDATE billing_export_status SET state = 'ok', bucket = %s, read_at = %s, "
                "manifest_key = %s, exported_at = %s, data_through = %s, currency = %s, "
                "error = NULL WHERE id = 1",
                (bucket, now, reading.manifest_key, reading.exported_at,
                 reading.data_through, reading.currency))

    def roles(self, system: str) -> dict[str, BucketRole]:
        with self._database() as connection:
            return bucket_roles(connection, system)


def _default_client(region: str) -> Any:
    from ..storage import s3_client

    return s3_client(region=region)


def run_due(*, store: BilledCostStore | None = None,
            location: Callable[[], system_bucket.SystemBucket | None] | None = None,
            client_for: Callable[[str], Any] | None = None,
            now: datetime | None = None) -> bool:
    """Look for and read the export when a look is due; True when one ran.

    Without a ready System Bucket nothing is due. Every outcome is a status,
    never an exception or a Problem: a missing export is ``not_configured`` or
    ``waiting``, a broken one ``error``, retried the next day.
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
    if not store.claim(status.checked_at if status is not None else None, current):
        return False
    try:
        roles = store.roles(bucket.name)
        client = (client_for or _default_client)(bucket.region)
        reading = read_export(client, bucket.name, roles)
    except Exception as exc:  # noqa: BLE001 - every failure is a quiet status
        store.save_error(bucket.name, safe_error(exc))
        return True
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


def _status_fields(status: Status | None, bucket: system_bucket.SystemBucket | None) -> dict[str, Any]:
    state = "unavailable" if bucket is None else (status.state if status else "checking")
    if bucket is not None and status is not None and status.bucket not in (None, bucket.name):
        # The System Bucket changed: what was read describes the old one.
        state = "checking"
    shown = status is not None and state == "ok"
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
        "error": status.error if status is not None and state == "error" else None,
    }


def installation_view(connection: Any, cfg: Any = None) -> dict[str, Any]:
    """The export's status and, when read, every bucket's totals by period."""
    bucket = system_bucket.location(cfg, connection=connection)
    status = load_status(connection)
    view = _status_fields(status, bucket)
    view["periods"] = []
    if view["state"] != "ok":
        return view
    rows = connection.execute(
        "SELECT b.billing_period, b.bucket, b.role, b.vault_id, v.name AS vault_name, "
        "b.category, b.amount FROM billed_costs b LEFT JOIN vaults v ON v.id = b.vault_id "
        "ORDER BY b.billing_period DESC, b.role, lower(v.name), b.bucket"
    ).fetchall()
    periods: dict[date, dict[tuple[str, str], list[Any]]] = {
        period: {} for period in sorted(_periods_read(connection), reverse=True)}
    for row in rows:
        periods.setdefault(row["billing_period"], {}).setdefault(
            (row["role"], row["bucket"]), []).append(row)
    for period, buckets in periods.items():
        vaults, installation = [], []
        total = Decimal(0)
        for (role, name), items in buckets.items():
            amount = sum((Decimal(item["amount"]) for item in items), Decimal(0))
            total += amount
            entry = {"bucket": name, "total": _money(amount), "categories": _categories(items)}
            if role == "vault":
                vaults.append({"vault_id": int(items[0]["vault_id"]),
                               "name": items[0]["vault_name"], **entry})
            else:
                installation.append({"role": role, **entry})
        view["periods"].append({"period": period.strftime("%Y-%m"), "total": _money(total),
                                "vaults": vaults, "installation": installation})
    return view


def vault_view(connection: Any, vault_id: int, cfg: Any = None) -> dict[str, Any]:
    """One Vault's Billed Cost by period, or why there is none to show.

    A Vault in a shared bucket has no Billed Cost of its own: ``shared`` is
    true and its periods are the whole shared bucket's, for every Vault in it.
    """
    vault = connection.execute(
        "SELECT v.id, t.bucket, t.key_layout FROM vaults v "
        "JOIN vault_targets t ON t.id = v.target_id WHERE v.id = %s", (vault_id,),
    ).fetchone()
    if vault is None:
        raise LookupError(vault_id)
    bucket = system_bucket.location(cfg, connection=connection)
    view = _status_fields(load_status(connection), bucket)
    shared = vault["key_layout"] != "bucket_root"
    view.update({"vault_id": int(vault["id"]), "vault_bucket": vault["bucket"],
                 "shared": shared, "periods": [], "estimate": _vault_estimate(connection, vault_id)})
    if view["state"] != "ok":
        return view
    rows = connection.execute(
        "SELECT billing_period, category, amount FROM billed_costs WHERE bucket = %s",
        (vault["bucket"],),
    ).fetchall()
    by_period: dict[date, list[Any]] = {period: [] for period in _periods_read(connection)}
    for row in rows:
        by_period.setdefault(row["billing_period"], []).append(row)
    # A period of the export without a line item of this bucket costs nothing.
    view["periods"] = [{
        "period": period.strftime("%Y-%m"),
        "total": _money(sum((Decimal(item["amount"]) for item in items), Decimal(0))),
        "categories": _categories(items),
    } for period, items in sorted(by_period.items(), reverse=True)]
    return view


def _vault_estimate(connection: Any, vault_id: int) -> dict[str, Any]:
    """This month's Cost Estimate of the Vault, as the Overview computes it."""
    prices = list_prices()
    total = 0.0
    for row in connection.execute(
        "SELECT av.storage_class, sum(av.size) AS bytes FROM archive_versions av "
        "JOIN files f ON f.id = av.file_id WHERE f.vault_id = %s "
        "AND av.availability = 'available' GROUP BY av.storage_class", (vault_id,),
    ).fetchall():
        total += estimate_storage_month(prices, size_bytes=int(row["bytes"] or 0),
                                        storage_class=row["storage_class"]).estimated_cost_eur
    return {"monthly": round(total, 6), "currency": prices.currency,
            "pricing_effective_at": prices.effective_at}


def _periods_read(connection: Any) -> set[date]:
    """The billing periods of the last read: its newest manifest's and any with totals."""
    periods = {row["billing_period"] for row in connection.execute(
        "SELECT DISTINCT billing_period FROM billed_costs").fetchall()}
    status = load_status(connection)
    match = re.search(r"BILLING_PERIOD=(\d{4})-(\d{2})/", (status.manifest_key or "") if status else "")
    if match:
        periods.add(date(int(match.group(1)), int(match.group(2)), 1))
    return periods
