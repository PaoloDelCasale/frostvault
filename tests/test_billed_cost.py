"""Billed Cost from the AWS Data Exports CUR 2.0 export (#738, ADR-0051).

Seam: ``app.services.billed_cost`` with an in-memory S3 (``FakeExportS3``) and
an in-memory store. ``tests/fixtures/billing/cur2-2026-11.csv`` is a small
synthetic CUR 2.0 file with the real column names; it is gzipped like AWS
delivers it. ``DatabaseStoreTests`` runs the store and the views against a
disposable PostgreSQL database (migration 0012).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError

from app.config import Settings
from app.services import billed_cost as bc
from app.services import system_bucket
from app.services.system_bucket import SystemBucket
from tests.fake_bucket_s3 import FakeBucketS3
from tests.fixtures import FrostVaultTestCase, seed_vault

FIXTURE = Path(__file__).with_name("fixtures") / "billing" / "cur2-2026-11.csv"
SYSTEM = "frostvault-system-k3x9q0ab"
VAULT_1 = "frostvault-foto-ab12cd34"
VAULT_2 = "frostvault-documenti-x9y8z7w6"
SHARED = "paolo-archive"
ROLES = {
    SYSTEM: bc.BucketRole("system"),
    VAULT_1: bc.BucketRole("vault", 1),
    VAULT_2: bc.BucketRole("vault", 2),
    SHARED: bc.BucketRole("shared"),
}
EXPORT = "billing/cur/frostvault/"
DELIVERED = datetime(2026, 11, 17, 6, 12, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def gz(text: str | bytes) -> bytes:
    return gzip.compress(text.encode() if isinstance(text, str) else text)


def fixture_csv() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class RecordingBody(io.BytesIO):
    """A response body that records every read size: ``-1``/None reads all."""

    def __init__(self, data: bytes, reads: list):
        super().__init__(data)
        self._reads = reads

    def read(self, size=-1):  # noqa: D401 - BytesIO signature
        self._reads.append(size)
        return super().read(size)


class FakeExportS3:
    """``ListObjectsV2`` with a delimiter and ``GetObject`` over a dict of keys."""

    def __init__(self, objects: dict[str, tuple[bytes, datetime]] | None = None, *, page=2):
        self.objects = dict(objects or {})
        self.page = page
        self.calls: list[tuple[str, str]] = []
        self.reads: list = []
        self.fail: dict[str, Exception | str] = {}
        self.on_get = None

    def put(self, key: str, data: bytes, modified: datetime = DELIVERED) -> None:
        self.objects[key] = (data, modified)

    def _raise(self, operation: str, name: str) -> None:
        failure = self.fail.get(operation)
        if isinstance(failure, Exception):
            raise failure
        if failure:
            raise ClientError({"Error": {"Code": failure, "Message": "secret detail"}}, name)

    def list_objects_v2(self, Bucket, Prefix, Delimiter, ContinuationToken=None):
        self.calls.append(("list_objects_v2", Prefix))
        self._raise("list_objects_v2", "ListObjectsV2")
        entries: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for key in sorted(self.objects):
            if not key.startswith(Prefix):
                continue
            rest = key[len(Prefix):]
            if Delimiter in rest:
                folder = Prefix + rest.split(Delimiter, 1)[0] + Delimiter
                if folder not in seen:
                    seen.add(folder)
                    entries.append(("prefix", {"Prefix": folder}))
            else:
                data, modified = self.objects[key]
                entries.append(("object", {"Key": key, "LastModified": modified, "Size": len(data),
                                           "ETag": '"' + hashlib.md5(data).hexdigest() + '"'}))
        start = int(ContinuationToken or 0)
        chunk = entries[start:start + self.page]
        page = {"CommonPrefixes": [item for kind, item in chunk if kind == "prefix"],
                "Contents": [item for kind, item in chunk if kind == "object"],
                "IsTruncated": start + self.page < len(entries)}
        if page["IsTruncated"]:
            page["NextContinuationToken"] = str(start + self.page)
        return page

    def get_object(self, Bucket, Key):
        self.calls.append(("get_object", Key))
        self._raise("get_object", "GetObject")
        if self.on_get is not None:
            self.on_get(Key)
        return {"Body": RecordingBody(self.objects[Key][0], self.reads)}

    def got(self) -> list[str]:
        return [key for name, key in self.calls if name == "get_object"]


def manifest(period: str, files: list[str], **extra) -> bytes:
    return json.dumps({"executionId": "synthetic", "exportName": "frostvault",
                       "billingPeriod": {"start": f"{period}-01T00:00:00.000Z"},
                       "dataFiles": files, **extra}).encode()


def delivered_export(s3: FakeExportS3, period: str = "2026-11", text: str | None = None,
                     modified: datetime = DELIVERED, chunks: int = 1,
                     manifest_name: str = "frostvault-Manifest.json") -> None:
    data = f"{EXPORT}data/BILLING_PERIOD={period}/"
    keys = [f"{data}frostvault-{index:05d}.csv.gz" for index in range(1, chunks + 1)]
    for index, key in enumerate(keys):
        s3.put(key, gz(text if text is not None else fixture_csv()) if index == 0 else b"", modified)
    s3.put(f"{EXPORT}metadata/BILLING_PERIOD={period}/{manifest_name}",
           manifest(period, [f"s3://{SYSTEM}/{key}" for key in keys]), modified)


HEADER = ["line_item_resource_id", "line_item_unblended_cost", "line_item_currency_code",
          "line_item_usage_type", "line_item_operation", "line_item_product_code",
          "line_item_usage_end_date"]


def csv_text(rows: list[dict[str, str]], header: list[str] = HEADER) -> str:
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(row.get(name, "") for name in header))
    return "\n".join(lines) + "\n"


def storage_row(bucket: str = VAULT_1, cost: str = "1", **extra) -> dict[str, str]:
    return {"line_item_resource_id": bucket, "line_item_unblended_cost": cost,
            "line_item_currency_code": "USD", "line_item_usage_type": "EUS1-TimedStorage-ByteHrs",
            "line_item_operation": "StandardStorage", **extra}


class CategoryTests(unittest.TestCase):
    def test_s3_usage_types_and_operations(self):
        cases = {
            ("EUS1-TimedStorage-ByteHrs", "StandardStorage"): "storage",
            ("EUS1-TimedStorage-GDA-ByteHrs", "DeepArchiveStorage"): "storage",
            ("EUS1-EarlyDelete-GDA", "DeleteObject"): "storage",
            ("EUS1-Requests-Tier1", "PutObject"): "requests",
            ("EUS1-Requests-Tier2", "GetObject"): "requests",
            ("EUS1-Requests-GDA-Tier1", "S3-GDATransition"): "transitions",
            ("EUS1-Requests-SIA-Tier1", "S3-SIATransition"): "transitions",
            ("EUS1-Requests-GDA-Tier3", "RestoreObject"): "retrievals",
            ("EUS1-Retrieval-SIA", "GetObject"): "retrievals",
            ("EUS1-DataTransfer-Out-Bytes", "GetObject"): "data_transfer",
            ("EUS1-USE1-AWS-Out-Bytes", "GetObject"): "data_transfer",
            ("EUS1-Monitoring-Automation-INT", "IntelligentTieringMonitoring"): "other",
            ("", ""): "other",
        }
        for (usage, operation), expected in cases.items():
            with self.subTest(usage=usage, operation=operation):
                self.assertEqual(bc.categorize(usage, operation), expected)
        self.assertEqual(bc.categorize("EUS1-Something", "Op", "AWSDataTransfer"), "data_transfer")

    def test_credits_refunds_and_discounts_are_their_own_category(self):
        for kind in ("Credit", "Refund", "Discount", "BundledDiscount", "EdpDiscount"):
            with self.subTest(kind):
                self.assertEqual(bc.categorize("EUS1-TimedStorage-ByteHrs", "StandardStorage",
                                               "AmazonS3", kind), "credits")
        for kind in ("Usage", "DiscountedUsage", ""):
            with self.subTest(kind):
                self.assertEqual(bc.categorize("EUS1-TimedStorage-ByteHrs", "StandardStorage",
                                               "AmazonS3", kind), "storage")

    def test_resource_ids_name_a_bucket_or_an_s3_arn(self):
        self.assertEqual(bc.resource_bucket(VAULT_1), VAULT_1)
        self.assertEqual(bc.resource_bucket(f"arn:aws:s3:::{VAULT_2}"), VAULT_2)
        self.assertEqual(bc.resource_bucket(f"arn:aws:s3:::{VAULT_2}/some/key"), VAULT_2)
        self.assertIsNone(bc.resource_bucket("arn:aws:ec2:eu-south-1:1:instance/i-1"))
        self.assertIsNone(bc.resource_bucket("  "))


class AggregationTests(unittest.TestCase):
    def aggregate(self, text: str, roles=ROLES):
        totals = bc.PeriodTotals(date(2026, 11, 1))
        state = bc._Accumulator(roles)
        reads: list = []
        bc.aggregate_gzip(RecordingBody(gz(text), reads), totals, state)
        return totals, state, reads

    def test_the_synthetic_cur2_file_is_attributed_by_bucket(self):
        totals, state, reads = self.aggregate(fixture_csv())
        amounts = totals.amounts
        self.assertEqual(amounts[(VAULT_1, "storage")], Decimal("2.15"))
        # The Credit line item of the Vault Bucket is shown apart.
        self.assertEqual(amounts[(VAULT_1, "credits")], Decimal("-0.1"))
        self.assertEqual(amounts[(VAULT_1, "requests")], Decimal("0.11"))
        self.assertEqual(amounts[(VAULT_1, "transitions")], Decimal("0.3"))
        self.assertEqual(amounts[(VAULT_1, "retrievals")], Decimal("0.07"))
        self.assertEqual(amounts[(VAULT_1, "data_transfer")], Decimal("0.09"))
        # A resource ID can also be the bucket ARN.
        self.assertEqual(amounts[(VAULT_2, "storage")], Decimal("2"))
        self.assertEqual(amounts[(SYSTEM, "requests")], Decimal("0.005"))
        self.assertEqual(amounts[(SHARED, "storage")], Decimal("3"))
        # Another bucket of the account, EC2 and taxes are never kept.
        self.assertEqual({bucket for bucket, _ in amounts}, {VAULT_1, VAULT_2, SYSTEM, SHARED})
        self.assertEqual(state.currency, "USD")
        self.assertEqual(state.data_through, datetime(2026, 11, 17, tzinfo=timezone.utc))
        # Streamed: every read of the response body is bounded.
        self.assertTrue(reads)
        self.assertTrue(all(isinstance(size, int) and size > 0 for size in reads), reads)

    def test_the_net_cost_wins_when_the_export_has_it(self):
        header = HEADER + ["line_item_net_unblended_cost", "line_item_line_item_type"]
        text = csv_text([
            storage_row(cost="1.00", line_item_net_unblended_cost="0.80", line_item_line_item_type="Usage"),
            storage_row(cost="0.50", line_item_line_item_type="Usage"),  # empty net: unblended
            storage_row(cost="-0.20", line_item_net_unblended_cost="-0.20", line_item_line_item_type="Refund"),
        ], header)
        totals, _, _ = self.aggregate(text)
        self.assertEqual(totals.amounts, {(VAULT_1, "storage"): Decimal("1.30"),
                                          (VAULT_1, "credits"): Decimal("-0.20")})

    def test_currency_and_data_date_come_from_every_row(self):
        text = csv_text([
            storage_row("someone-elses-bucket", line_item_currency_code="EUR",
                        line_item_usage_end_date="2026-11-16T00:00:00Z"),
        ])
        totals, state, _ = self.aggregate(text)
        self.assertEqual(totals.amounts, {})
        self.assertEqual((state.currency, state.data_through),
                         ("EUR", datetime(2026, 11, 16, tzinfo=timezone.utc)))

    def test_a_large_file_is_streamed_not_loaded(self):
        row = {"line_item_resource_id": VAULT_1, "line_item_unblended_cost": "0.0000010000",
               "line_item_currency_code": "USD", "line_item_usage_type": "EUS1-Requests-Tier1",
               "line_item_operation": "PutObject"}
        text = csv_text([row] * 200_000)
        self.assertGreater(len(text), 10 * 1024 * 1024)
        totals, state, reads = self.aggregate(text)
        self.assertEqual(state.kept, 200_000)
        self.assertEqual(totals.amounts[(VAULT_1, "requests")], Decimal("0.2"))
        self.assertLess(max(reads), 1024 * 1024)

    def test_the_csv_field_limit_is_restored(self):
        import csv

        before = csv.field_size_limit()
        self.aggregate(fixture_csv())
        self.assertEqual(csv.field_size_limit(), before)
        with self.assertRaises(bc.BillingExportError):
            self.aggregate(csv_text([storage_row(cost="abc")]))
        self.assertEqual(csv.field_size_limit(), before)

    def test_the_checkpoint_runs_every_n_rows(self):
        calls: list[int] = []
        state = bc._Accumulator(ROLES, lambda: calls.append(state.rows))
        text = csv_text([storage_row()] * (bc.CHECK_EVERY_ROWS * 2 + 1))
        bc.aggregate_gzip(io.BytesIO(gz(text)), bc.PeriodTotals(date(2026, 11, 1)), state)
        self.assertEqual(calls, [bc.CHECK_EVERY_ROWS, bc.CHECK_EVERY_ROWS * 2])

    def test_an_empty_chunk_adds_nothing(self):
        totals = bc.PeriodTotals(date(2026, 11, 1))
        bc.aggregate_gzip(io.BytesIO(b""), totals, bc._Accumulator(ROLES))
        bc.aggregate_gzip(io.BytesIO(gz("")), totals, bc._Accumulator(ROLES))
        self.assertEqual(totals.amounts, {})

    def test_unreadable_exports_are_error_codes(self):
        base = storage_row()
        cases = {
            "without resource IDs": (csv_text([base], [name for name in HEADER if name != "line_item_resource_id"]),
                                     ("missing_column", "line_item_resource_id")),
            "a cost that is text": (csv_text([{**base, "line_item_unblended_cost": "abc"}]), ("cost_not_number", None)),
            "mixed currencies": (csv_text([base, {**base, "line_item_currency_code": "EUR"}]), ("mixed_currencies", "USD, EUR")),
            "a short row": (csv_text([base]) + f"{VAULT_1},1\n", ("short_row", None)),
            "no currency on a known bucket": (csv_text([{**base, "line_item_currency_code": ""}]), ("currency_missing", None)),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name), self.assertRaises(bc.BillingExportError) as raised:
                self.aggregate(text)
            self.assertEqual((raised.exception.code, raised.exception.detail), expected)
        with self.assertRaises(bc.BillingExportError) as raised:
            bc.aggregate_gzip(io.BytesIO(b"not gzip at all"), bc.PeriodTotals(date(2026, 11, 1)),
                              bc._Accumulator(ROLES))
        self.assertEqual(raised.exception.code, "not_gzip_csv")


def totals_of(reading: bc.ExportReading) -> list[date]:
    return [period.mark.period for period in reading.periods]


class ReadExportTests(unittest.TestCase):
    def test_nothing_under_the_prefix_is_not_configured(self):
        self.assertEqual(bc.read_export(FakeExportS3(), SYSTEM, ROLES).state, "not_configured")
        # Backups elsewhere in the System Bucket are not an export.
        s3 = FakeExportS3()
        s3.put("system/backups/backup-1.fvb", b"x")
        self.assertEqual(bc.read_export(s3, SYSTEM, ROLES).state, "not_configured")

    def test_an_export_folder_without_a_manifest_is_waiting(self):
        s3 = FakeExportS3()
        s3.put("billing/cur/aws-programmatic-access-test-object", b"")
        self.assertEqual(bc.read_export(s3, SYSTEM, ROLES).state, "waiting")
        s3.put(f"{EXPORT}data/BILLING_PERIOD=2026-11/frostvault-00001.csv.gz", gz(fixture_csv()))
        self.assertEqual(bc.read_export(s3, SYSTEM, ROLES).state, "waiting")

    def test_the_newest_manifest_and_the_month_before_are_read(self):
        s3 = FakeExportS3()
        delivered_export(s3, "2026-10", modified=DELIVERED - timedelta(days=16))
        delivered_export(s3, "2026-11", chunks=2)
        # An older month is not even listed; a manifest of one execution is not read.
        delivered_export(s3, "2026-08", modified=DELIVERED - timedelta(days=90))
        s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/20261117T061200Z-abc/frostvault-Manifest.json",
               b"{not read}")
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual(reading.state, "ok")
        self.assertEqual(totals_of(reading), [date(2026, 11, 1), date(2026, 10, 1)])
        self.assertEqual(reading.manifest_key,
                         f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json")
        self.assertEqual((reading.exported_at, reading.currency), (DELIVERED, "USD"))
        self.assertEqual(reading.data_through, datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertEqual(reading.periods[0].totals.amounts[(VAULT_1, "storage")], Decimal("2.15"))
        self.assertFalse(any("2026-08" in key or "20261117T" in key for key in s3.got()), s3.got())
        listed = [prefix for name, prefix in s3.calls if name == "list_objects_v2"]
        self.assertFalse(any("2026-08" in prefix for prefix in listed), listed)

    def test_a_gap_month_is_not_called_previous(self):
        s3 = FakeExportS3()
        delivered_export(s3, "2026-09", modified=DELIVERED - timedelta(days=40))
        delivered_export(s3, "2026-11")
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual(totals_of(reading), [date(2026, 11, 1)])

    def test_the_export_delivered_last_wins(self):
        s3 = FakeExportS3()
        delivered_export(s3, "2026-11", modified=DELIVERED - timedelta(days=30))
        other = "billing/cur/newer/"
        s3.put(f"{other}data/BILLING_PERIOD=2026-11/newer-00001.csv.gz",
               gz(csv_text([storage_row(VAULT_2, "4", line_item_currency_code="EUR")])))
        s3.put(f"{other}metadata/BILLING_PERIOD=2026-11/newer-Manifest.json",
               manifest("2026-11", [f"{other}data/BILLING_PERIOD=2026-11/newer-00001.csv.gz"]))
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual(reading.currency, "EUR")
        self.assertEqual(reading.periods[0].totals.amounts, {(VAULT_2, "storage"): Decimal("4")})

    def test_a_manifest_named_manifest_json_or_export_manifest_json(self):
        for name in ("frostvault-Manifest.json", "Manifest.json"):
            with self.subTest(name):
                s3 = FakeExportS3()
                delivered_export(s3, manifest_name=name)
                reading = bc.read_export(s3, SYSTEM, ROLES)
                self.assertEqual(reading.manifest_key,
                                 f"{EXPORT}metadata/BILLING_PERIOD=2026-11/{name}")
                self.assertEqual(reading.periods[0].totals.amounts[(VAULT_1, "storage")], Decimal("2.15"))

    def test_manifest_entries_may_be_keys_or_objects(self):
        s3 = FakeExportS3()
        delivered_export(s3)
        key = f"{EXPORT}data/BILLING_PERIOD=2026-11/frostvault-00001.csv.gz"
        for files in ([key], [{"key": key}], [{"s3Uri": f"s3://{SYSTEM}/{key}"}], [f"s3://{SYSTEM}/{key}"]):
            with self.subTest(files=files):
                s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json",
                       manifest("2026-11", files))
                self.assertEqual(bc.read_export(s3, SYSTEM, ROLES).state, "ok")

    def test_without_a_recognisable_file_list_the_data_folder_is_listed(self):
        for name, body in {
            "no list": json.dumps({"exportName": "frostvault", "somethingElse": []}).encode(),
            "unknown entries": json.dumps({"dataFiles": [{"location": "x"}]}).encode(),
            "not an object": b"[]",
        }.items():
            with self.subTest(name):
                s3 = FakeExportS3()
                delivered_export(s3, chunks=2)
                s3.put(f"{EXPORT}data/BILLING_PERIOD=2026-11/readme.txt", b"not data")
                s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json", body)
                reading = bc.read_export(s3, SYSTEM, ROLES)
                self.assertEqual(reading.periods[0].totals.amounts[(VAULT_1, "storage")], Decimal("2.15"))
                self.assertEqual([key for key in s3.got() if "/data/" in key],
                                 [f"{EXPORT}data/BILLING_PERIOD=2026-11/frostvault-00001.csv.gz",
                                  f"{EXPORT}data/BILLING_PERIOD=2026-11/frostvault-00002.csv.gz"])

    def test_create_new_versioning_reads_only_the_newest_execution(self):
        s3 = FakeExportS3()
        data = f"{EXPORT}data/BILLING_PERIOD=2026-11/"
        s3.put(f"{data}20261116T061200Z-aaa/frostvault-00001.csv.gz", gz(csv_text([storage_row(cost="1")])))
        s3.put(f"{data}20261117T061200Z-bbb/frostvault-00001.csv.gz", gz(csv_text([storage_row(cost="2")])))
        s3.put(f"{data}20261117T061200Z-bbb/frostvault-00002.csv.gz", gz(csv_text([storage_row(cost="3")])))
        # The manifest is only in the newest execution's folder and lists no file.
        s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/20261116T061200Z-aaa/frostvault-Manifest.json",
               b"{}", DELIVERED - timedelta(days=1))
        s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/20261117T061200Z-bbb/frostvault-Manifest.json",
               b"{}")
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual(reading.manifest_key,
                         f"{EXPORT}metadata/BILLING_PERIOD=2026-11/20261117T061200Z-bbb/frostvault-Manifest.json")
        self.assertEqual(reading.periods[0].totals.amounts, {(VAULT_1, "storage"): Decimal("5")})
        self.assertFalse(any("-aaa/" in key for key in s3.got()))

    def test_create_new_files_listed_by_the_partition_manifest(self):
        s3 = FakeExportS3()
        key = f"{EXPORT}data/BILLING_PERIOD=2026-11/20261117T061200Z-bbb/frostvault-00001.csv.gz"
        s3.put(key, gz(csv_text([storage_row(cost="2")])))
        s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json",
               manifest("2026-11", [f"s3://{SYSTEM}/{key}"]))
        self.assertEqual(bc.read_export(s3, SYSTEM, ROLES).periods[0].totals.amounts,
                         {(VAULT_1, "storage"): Decimal("2")})

    def test_a_manifest_that_points_elsewhere_or_at_parquet_is_refused(self):
        cases = {
            "another bucket": ([f"s3://someone-else/{EXPORT}data/BILLING_PERIOD=2026-11/x.csv.gz"], "manifest_foreign_bucket"),
            "outside its folder": (["system/backups/backup.fvb.csv.gz"], "manifest_outside_folder"),
            "parquet": ([f"{EXPORT}data/BILLING_PERIOD=2026-11/frostvault-00001.snappy.parquet"], "parquet"),
            "invalid json": (None, "manifest_invalid"),
        }
        for name, (files, code) in cases.items():
            with self.subTest(name):
                s3 = FakeExportS3()
                body = b"[1" if files is None else manifest("2026-11", files)
                s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json", body)
                with self.assertRaises(bc.BillingExportError) as raised:
                    bc.read_export(s3, SYSTEM, ROLES)
                self.assertEqual(raised.exception.code, code)
                self.assertFalse([key for key in s3.got() if "Manifest" not in key])

    def test_a_listed_parquet_export_or_an_empty_data_folder_is_refused(self):
        for name, files, code in (("parquet", {"frostvault-00001.snappy.parquet": b"x"}, "parquet"),
                                  ("empty", {}, "no_data_files")):
            with self.subTest(name):
                s3 = FakeExportS3()
                for file, data in files.items():
                    s3.put(f"{EXPORT}data/BILLING_PERIOD=2026-11/{file}", data)
                s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json", b"{}")
                with self.assertRaises(bc.BillingExportError) as raised:
                    bc.read_export(s3, SYSTEM, ROLES)
                self.assertEqual(raised.exception.code, code)

    def test_a_month_without_frostvault_lines_is_zero_in_usd(self):
        s3 = FakeExportS3()
        delivered_export(s3, text=csv_text([]))
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual((reading.state, reading.currency, reading.periods[0].totals.amounts),
                         ("ok", "USD", {}))

    def test_an_unchanged_period_is_not_read_again(self):
        s3 = FakeExportS3()
        delivered_export(s3, "2026-10", modified=DELIVERED - timedelta(days=16))
        delivered_export(s3, "2026-11")
        first = bc.read_export(s3, SYSTEM, ROLES)
        known = {period.mark.period: period.mark for period in first.periods}
        s3.calls.clear()
        # AWS refreshed only November.
        delivered_export(s3, "2026-11", text=csv_text([storage_row(cost="7")]),
                         modified=DELIVERED + timedelta(days=1))
        second = bc.read_export(s3, SYSTEM, ROLES, known=known)
        self.assertEqual(second.periods[0].totals.amounts, {(VAULT_1, "storage"): Decimal("7")})
        self.assertIsNone(second.periods[1].totals)
        self.assertEqual(second.periods[1].mark, known[date(2026, 10, 1)])
        self.assertFalse(any("2026-10" in key for key in s3.got()), s3.got())
        # Nothing changed: no data file is read; another known bucket reads all again.
        known = {period.mark.period: period.mark for period in second.periods}
        s3.calls.clear()
        third = bc.read_export(s3, SYSTEM, ROLES, known=known)
        self.assertEqual([period.totals for period in third.periods], [None, None])
        self.assertEqual(third.data_through, second.data_through)
        self.assertFalse([key for key in s3.got() if "/data/" in key])
        roles = {**ROLES, "frostvault-new-00000000": bc.BucketRole("vault", 4)}
        fourth = bc.read_export(s3, SYSTEM, roles, known=known)
        self.assertTrue(all(period.totals is not None for period in fourth.periods))

    def test_provider_messages_never_reach_the_status(self):
        s3 = FakeExportS3()
        s3.fail["list_objects_v2"] = "AccessDenied"
        with self.assertRaises(ClientError) as raised:
            bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual(bc.classify_error(raised.exception), ("access_denied", "s3:ListBucket (AccessDenied)"))
        self.assertEqual(bc.classify_error(ValueError("/secret/path")), ("internal", "ValueError"))
        self.assertEqual(bc.classify_error(RuntimeError("AWS credentials are not configured in the .env file")),
                         ("credentials", None))

    def test_errors_are_transient_or_deterministic(self):
        def client_error(code, status=400):
            return ClientError({"Error": {"Code": code, "Message": "secret"},
                                "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject")
        cases = {
            "throttled": (client_error("SlowDown", 503), "s3_throttled"),
            "5xx": (client_error("InternalError", 500), "s3_server_error"),
            "unreachable": (EndpointConnectionError(endpoint_url="https://s3"), "s3_unreachable"),
            "read timeout": (ReadTimeoutError(endpoint_url="https://s3"), "s3_unreachable"),
            "denied": (client_error("AccessDenied", 403), "access_denied"),
            "missing key": (client_error("NoSuchKey", 404), "s3_refused"),
            "parquet": (bc.BillingExportError("parquet"), "parquet"),
            "no credentials": (NoCredentialsError(), "credentials"),
        }
        for name, (exc, code) in cases.items():
            with self.subTest(name):
                self.assertEqual(bc.classify_error(exc)[0], code)
                status = bc.Status("error", NOW, error_code=code)
                transient = code in {"s3_throttled", "s3_server_error", "s3_unreachable"}
                self.assertEqual(bc.next_check_at(status) - NOW,
                                 timedelta(hours=6) if transient else timedelta(days=1))


class MemoryStore:
    def __init__(self, status: bc.Status | None = None):
        self.status = status
        self.saved: list = []
        self.rows: dict = {}
        self.period_marks: dict = {}

    def load(self):
        return self.status

    def claim(self, previous, now):
        current = self.status.checked_at if self.status else None
        if current != previous:
            return False
        self.status = (bc.Status("checking", now) if self.status is None
                       else replace(self.status, checked_at=now))
        return True

    def release(self, claimed, previous):
        if self.status is not None and self.status.checked_at == claimed:
            self.status = None if previous is None else replace(self.status, checked_at=previous)
        self.saved.append("released")

    def marks(self):
        return dict(self.period_marks)

    def save_absent(self, state, bucket):
        self.rows = {}
        self.period_marks = {}
        self.status = replace(self.status, state=state, bucket=bucket, error_code=None, error_detail=None)
        self.saved.append(state)

    def save_error(self, bucket, code, detail):
        self.status = replace(self.status, state="error", bucket=bucket, error_code=code, error_detail=detail)
        self.saved.append("error")

    def save_reading(self, bucket, reading, roles, now):
        rows = {}
        for period in reading.periods:
            rows[period.mark.period] = (dict(period.totals.amounts) if period.totals is not None
                                        else self.rows[period.mark.period])
        self.rows = rows
        self.period_marks = {period.mark.period: period.mark for period in reading.periods}
        self.status = replace(self.status, state="ok", bucket=bucket, read_at=now,
                              exported_at=reading.exported_at, currency=reading.currency,
                              manifest_key=reading.manifest_key, error_code=None, error_detail=None)
        self.saved.append("ok")

    def roles(self, system):
        return dict(ROLES)


class StallingBody(io.BytesIO):
    """A body that advances a fake clock at every read, like a slow S3."""

    def __init__(self, data: bytes, clock: list[float], step: float):
        super().__init__(data)
        self._clock = clock
        self._step = step

    def read(self, size=-1):  # noqa: D401 - BytesIO signature
        self._clock[0] += self._step
        return super().read(min(size, 64) if size and size > 0 else 64)


class RunDueTests(unittest.TestCase):
    def run_due(self, store, s3, *, now=NOW, bucket=SystemBucket(SYSTEM, "eu-south-1", "created"), **kwargs):
        regions: list[str] = []

        def client_for(region):
            regions.append(region)
            return s3
        ran = bc.run_due(store=store, location=lambda: bucket, client_for=client_for, now=now, **kwargs)
        return ran, regions

    def test_reads_once_a_day_from_the_persisted_last_look(self):
        s3 = FakeExportS3()
        delivered_export(s3)
        store = MemoryStore()
        self.assertEqual(self.run_due(store, s3), (True, ["eu-south-1"]))
        self.assertEqual(store.status.state, "ok")
        self.assertEqual(store.rows[date(2026, 11, 1)][(VAULT_1, "storage")], Decimal("2.15"))
        # A restart (a new loop, the same persisted status) does not read early.
        reads = len(s3.calls)
        self.assertEqual(self.run_due(store, s3, now=NOW + timedelta(hours=23, minutes=59)), (False, []))
        self.assertEqual(len(s3.calls), reads)
        self.assertTrue(self.run_due(store, s3, now=NOW + timedelta(days=1))[0])
        # The export did not change: its totals stay and no data file was read again.
        self.assertEqual(store.rows[date(2026, 11, 1)][(VAULT_1, "storage")], Decimal("2.15"))
        self.assertFalse([key for key in s3.got()[2:] if "/data/" in key])

    def test_a_missing_export_is_looked_for_every_six_hours(self):
        store = MemoryStore()
        s3 = FakeExportS3()
        self.assertTrue(self.run_due(store, s3)[0])
        self.assertEqual(store.status.state, "not_configured")
        self.assertFalse(self.run_due(store, s3, now=NOW + timedelta(hours=5))[0])
        self.assertTrue(self.run_due(store, s3, now=NOW + timedelta(hours=6))[0])

    def test_a_broken_export_is_a_quiet_status_retried_the_next_day(self):
        store = MemoryStore()
        s3 = FakeExportS3()
        s3.fail["list_objects_v2"] = "AccessDenied"
        with patch("app.services.problems.report_problem") as problem:
            self.assertTrue(self.run_due(store, s3)[0])
        problem.assert_not_called()
        self.assertEqual((store.status.state, store.status.error_code, store.status.error_detail),
                         ("error", "access_denied", "s3:ListBucket (AccessDenied)"))
        self.assertFalse(self.run_due(store, s3, now=NOW + timedelta(hours=12))[0])
        self.assertTrue(self.run_due(store, s3, now=NOW + timedelta(days=1))[0])

    def test_a_transient_error_is_retried_after_six_hours(self):
        store = MemoryStore()
        s3 = FakeExportS3()
        s3.fail["list_objects_v2"] = "SlowDown"
        self.assertTrue(self.run_due(store, s3)[0])
        self.assertEqual(store.status.error_code, "s3_throttled")
        self.assertFalse(self.run_due(store, s3, now=NOW + timedelta(hours=5))[0])
        self.assertTrue(self.run_due(store, s3, now=NOW + timedelta(hours=6))[0])

    def test_an_error_keeps_the_last_good_totals(self):
        s3 = FakeExportS3()
        delivered_export(s3)
        store = MemoryStore()
        self.run_due(store, s3)
        s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json", b"[1",
               DELIVERED + timedelta(days=1))
        self.run_due(store, s3, now=NOW + timedelta(days=1))
        self.assertEqual((store.status.state, store.status.error_code), ("error", "manifest_invalid"))
        self.assertIn(date(2026, 11, 1), store.rows)

    def test_a_stalled_read_aborts_at_once_and_keeps_the_totals(self):
        s3 = FakeExportS3()
        delivered_export(s3, "2026-10", modified=DELIVERED - timedelta(days=16))
        delivered_export(s3, "2026-11")
        store = MemoryStore()
        self.run_due(store, s3)
        before = dict(store.rows)
        delivered_export(s3, "2026-11", modified=DELIVERED + timedelta(days=1), chunks=2)
        delivered_export(s3, "2026-10", modified=DELIVERED + timedelta(days=1))
        s3.calls.clear()

        def stall(key):
            if "/data/" in key:
                raise ReadTimeoutError(endpoint_url="https://s3.eu-south-1.amazonaws.com")
        s3.on_get = stall
        self.assertTrue(self.run_due(store, s3, now=NOW + timedelta(days=1))[0])
        # The first data file timed out: nothing else was requested.
        self.assertEqual(len([key for key in s3.got() if "/data/" in key]), 1)
        self.assertEqual((store.status.state, store.status.error_code), ("error", "s3_unreachable"))
        self.assertEqual(bc.next_check_at(store.status), NOW + timedelta(days=1, hours=6))
        self.assertEqual(store.rows, before)

    def test_a_slow_read_over_budget_records_nothing(self):
        s3 = FakeExportS3()
        delivered_export(s3)
        store = MemoryStore(bc.Status("ok", NOW - timedelta(days=2), bucket=SYSTEM))
        store.rows = {"previous": "kept"}
        clock = [0.0]
        data = gz(csv_text([storage_row()] * (bc.CHECK_EVERY_ROWS * 4)))
        key = f"{EXPORT}data/BILLING_PERIOD=2026-11/frostvault-00001.csv.gz"
        s3.put(key, data)
        original = s3.get_object

        def stalling_get(Bucket, Key):
            response = original(Bucket=Bucket, Key=Key)
            if Key == key:
                response["Body"] = StallingBody(data, clock, 1.0)
            return response
        s3.get_object = stalling_get
        self.assertTrue(self.run_due(store, s3, budget_seconds=30, clock=lambda: clock[0])[0])
        self.assertEqual(store.saved, [])
        self.assertEqual(store.rows, {"previous": "kept"})
        self.assertEqual(store.status.state, "ok")
        # Stopped at a row checkpoint, long before the end of the file.
        self.assertLess(clock[0], len(data) / 64)

    def test_shutdown_stops_the_read_and_leaves_it_due(self):
        s3 = FakeExportS3()
        delivered_export(s3, "2026-10", modified=DELIVERED - timedelta(days=16))
        delivered_export(s3, "2026-11")
        previous = NOW - timedelta(days=2)
        store = MemoryStore(bc.Status("ok", previous, bucket=SYSTEM))
        stopping = []
        s3.on_get = lambda key: stopping.append(key)
        self.assertTrue(self.run_due(store, s3, should_stop=lambda: len(stopping) >= 2)[0])
        self.assertEqual(store.saved, ["released"])
        self.assertEqual(store.status.checked_at, previous)
        self.assertEqual(len([key for key in s3.got() if "/data/" in key]), 1)
        # The first look ever leaves no status row behind.
        first = MemoryStore()
        self.assertTrue(self.run_due(first, s3, should_stop=lambda: True)[0])
        self.assertIsNone(first.status)

    def test_the_pass_uses_its_own_short_timeout_client(self):
        with patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "example"}), \
                patch("app.services.s3.new_service_client") as factory:
            bc._default_client("eu-south-1")
        service = factory.call_args.args[0]
        config = factory.call_args.kwargs["config"]
        self.assertEqual((service, factory.call_args.kwargs["region"]), ("s3", "eu-south-1"))
        self.assertEqual((config.connect_timeout, config.read_timeout), (3, 10))
        self.assertEqual(config.retries["total_max_attempts"], 2)
        with patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": "REPLACE_ME", "AWS_SECRET_ACCESS_KEY": "REPLACE_ME"}):
            with self.assertRaises(bc.BillingExportError) as raised:
                bc._default_client("eu-south-1")
        self.assertEqual(raised.exception.code, "credentials")

    def test_nothing_runs_without_a_ready_system_bucket(self):
        store = MemoryStore()
        self.assertEqual(self.run_due(store, FakeExportS3(), bucket=None), (False, []))
        pending = SystemBucket(SYSTEM, "eu-south-1", "created", ready=False)
        self.assertEqual(self.run_due(store, FakeExportS3(), bucket=pending), (False, []))
        self.assertIsNone(store.status)

    def test_another_system_bucket_is_looked_at_at_once(self):
        s3 = FakeExportS3()
        store = MemoryStore(bc.Status("ok", NOW - timedelta(hours=1), bucket="frostvault-system-old00000"))
        self.assertTrue(self.run_due(store, s3)[0])
        self.assertEqual((store.status.state, store.status.bucket), ("not_configured", SYSTEM))

    def test_a_look_another_process_claimed_does_not_run(self):
        store = MemoryStore(bc.Status("ok", NOW - timedelta(days=2)))
        store.claim = lambda previous, now: False
        self.assertEqual(self.run_due(store, FakeExportS3()), (False, []))

    def test_the_maintenance_task_only_logs_and_honours_shutdown(self):
        from app import storage

        with patch.object(bc, "run_due", side_effect=RuntimeError("db down")) as run, \
                patch.object(storage.worker_error_store, "log_worker_error") as logged, \
                patch.object(storage, "_report_worker_error") as reported:
            storage.read_billed_cost_once()
        logged.assert_called_once()
        reported.assert_not_called()
        should_stop = run.call_args.kwargs["should_stop"]
        self.assertFalse(should_stop())
        with patch.object(storage, "shutdown_state", SimpleNamespace(requested=True)):
            self.assertTrue(should_stop())


class StaleTests(unittest.TestCase):
    def status(self, exported):
        return bc.Status("ok", NOW, exported_at=exported)

    def test_an_old_export_or_a_missing_current_month_is_not_up_to_date(self):
        november, october = date(2026, 11, 1), date(2026, 10, 1)
        self.assertFalse(bc.is_stale(self.status(DELIVERED), [november, october], NOW))
        self.assertTrue(bc.is_stale(self.status(NOW - timedelta(days=3, hours=1)), [november], NOW))
        # After the 3rd of the month the current month must be there.
        self.assertTrue(bc.is_stale(self.status(DELIVERED), [october], NOW))
        early = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        self.assertFalse(bc.is_stale(self.status(early - timedelta(hours=6)), [october], early))
        self.assertFalse(bc.is_stale(None, [], NOW))


class CatalogTextTests(unittest.TestCase):
    def test_every_error_code_and_category_has_an_english_and_italian_text(self):
        root = Path(__file__).resolve().parents[1] / "app" / "locales"
        for locale in ("en", "it"):
            catalog = json.loads((root / f"{locale}.json").read_text(encoding="utf-8"))
            for code in bc.ERROR_CODES:
                with self.subTest(locale=locale, code=code):
                    self.assertTrue(catalog.get(f"billed_cost.error.{code}"))
            for category in bc.CATEGORIES:
                with self.subTest(locale=locale, category=category):
                    self.assertTrue(catalog.get(f"billed_cost.category_{category}"))
        self.assertEqual(json.loads((root / "it.json").read_text(encoding="utf-8"))["billed_cost.category_credits"],
                         "Crediti e sconti")


class SystemBucketPolicyTests(unittest.TestCase):
    def test_the_data_exports_bucket_policy_keeps_the_system_bucket_verified(self):
        """The policy the AWS console adds for bcm-data-exports is not a check."""
        s3 = FakeBucketS3()
        reservation = system_bucket.Reservation(SYSTEM, "eu-south-1", False)
        from app.services import bucket_provisioning
        bucket_provisioning.provision_bucket(s3, system_bucket.spec(SYSTEM, "eu-south-1"))
        s3.buckets[SYSTEM]["policy"] = json.dumps({"Statement": [{
            "Effect": "Allow", "Principal": {"Service": ["bcm-data-exports.amazonaws.com"]},
            "Action": ["s3:PutObject"], "Resource": f"arn:aws:s3:::{SYSTEM}/*"}]})
        self.assertTrue(system_bucket.verify_created(s3, reservation).ok)


class DatabaseStoreTests(FrostVaultTestCase):
    """``billing_export_status`` and ``billed_costs`` of migration 0012 (PostgreSQL)."""

    owner = None

    def setUp(self):
        super().setUp()
        self.connect = lambda: self.database.connect()
        self.store = bc.DatabaseStore(self.connect)
        with self.connect() as connection:
            seed_vault(connection, id=1, name="Foto", source_root="/sources/foto", bucket=VAULT_1,
                       target={"id": 1, "key_layout": "bucket_root"})
            seed_vault(connection, id=2, name="Documenti", source_root="/sources/documenti",
                       bucket=VAULT_2, target={"id": 2, "key_layout": "bucket_root"})
            seed_vault(connection, id=3, name="Vecchio", source_root="/sources/vecchio", bucket=SHARED,
                       target={"id": 3})
            connection.execute(
                "INSERT INTO system_bucket(id, name, region, reserved_at, ready_at) "
                f"VALUES (1, '{SYSTEM}', 'eu-south-1', now(), now())")
        self.cfg = replace(Settings(), aws_region="eu-south-1", metadata_backup_bucket="")

    def read_fixture(self, now: datetime = NOW, s3: FakeExportS3 | None = None) -> FakeExportS3:
        if s3 is None:
            s3 = FakeExportS3()
            delivered_export(s3, "2026-10", modified=DELIVERED - timedelta(days=16),
                             text=csv_text([storage_row(cost="1.5")]))
            delivered_export(s3, "2026-11")
        with patch.object(system_bucket, "location",
                          return_value=SystemBucket(SYSTEM, "eu-south-1", "created")):
            self.assertTrue(bc.run_due(store=self.store, client_for=lambda region: s3, now=now))
        return s3

    def views(self, vault_id: int = 1):
        with self.connect() as connection:
            return (bc.installation_view(connection, self.cfg, now=NOW),
                    bc.vault_view(connection, vault_id, self.cfg, now=NOW))

    def test_roles_come_from_the_vault_targets(self):
        with self.connect() as connection:
            roles = bc.bucket_roles(connection, SYSTEM)
        self.assertEqual(roles, {SYSTEM: bc.BucketRole("system"), VAULT_1: bc.BucketRole("vault", 1),
                                 VAULT_2: bc.BucketRole("vault", 2), SHARED: bc.BucketRole("shared")})

    def test_a_read_is_stored_and_shown_per_vault_and_installation(self):
        self.read_fixture()
        view, vault = self.views()
        with self.connect() as connection:
            shared = bc.vault_view(connection, 3, self.cfg, now=NOW)
        self.assertEqual((view["state"], view["bucket"], view["currency"], view["stale"]),
                         ("ok", SYSTEM, "USD", False))
        self.assertEqual(datetime.fromisoformat(view["exported_at"]), DELIVERED)
        self.assertEqual(datetime.fromisoformat(view["data_through"]),
                         datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertEqual([period["period"] for period in view["periods"]], ["2026-11", "2026-10"])
        november = view["periods"][0]
        self.assertEqual([(row["name"], row["total"]) for row in november["vaults"]],
                         [("Documenti", 2.2), ("Foto", 2.62)])
        self.assertEqual({row["role"]: row["total"] for row in november["installation"]},
                         {"system": 0.015, "shared": 3.0})
        self.assertEqual((november["total"], november["vaults_total"]), (7.835, 4.82))
        self.assertEqual(vault["periods"][0]["categories"],
                         {"storage": 2.15, "requests": 0.11, "transitions": 0.3, "retrievals": 0.07,
                          "data_transfer": 0.09, "other": 0.0, "credits": -0.1})
        self.assertEqual([period["total"] for period in vault["periods"]], [2.62, 1.5])
        self.assertFalse(vault["shared"])
        self.assertNotIn("estimate", vault)
        # An older Vault in a shared bucket has no figure of its own.
        self.assertEqual((shared["shared"], shared["periods"]), (True, []))

    def test_an_unchanged_period_keeps_its_totals_and_a_dropped_one_goes(self):
        s3 = self.read_fixture()
        with self.connect() as connection:
            first = {row["billing_period"]: row["read_at"] for row in connection.execute(
                "SELECT billing_period, read_at FROM billed_cost_periods").fetchall()}
        # A day later AWS refreshed November only.
        delivered_export(s3, "2026-11", text=csv_text([storage_row(cost="9")]),
                         modified=DELIVERED + timedelta(days=1))
        self.read_fixture(NOW + timedelta(days=1), s3)
        view, vault = self.views()
        self.assertEqual([period["total"] for period in vault["periods"]], [9.0, 1.5])
        with self.connect() as connection:
            second = {row["billing_period"]: row["read_at"] for row in connection.execute(
                "SELECT billing_period, read_at FROM billed_cost_periods").fetchall()}
        self.assertEqual(second[date(2026, 10, 1)], first[date(2026, 10, 1)])
        self.assertGreater(second[date(2026, 11, 1)], first[date(2026, 11, 1)])
        # December arrives: October is no longer listed, and its totals go.
        delivered_export(s3, "2026-12", text=csv_text([storage_row(cost="0.5")]),
                         modified=DELIVERED + timedelta(days=20))
        self.read_fixture(NOW + timedelta(days=21), s3)
        with self.connect() as connection:
            periods = {row["billing_period"] for row in connection.execute(
                "SELECT DISTINCT billing_period FROM billed_costs").fetchall()}
        self.assertEqual(periods, {date(2026, 12, 1), date(2026, 11, 1)})

    def test_old_data_are_marked_not_up_to_date(self):
        self.read_fixture()
        with self.connect() as connection:
            view = bc.installation_view(connection, self.cfg, now=NOW + timedelta(days=4))
        self.assertTrue(view["stale"])

    def test_an_error_hides_the_totals_and_a_missing_export_clears_them(self):
        self.read_fixture()
        self.store.save_error(SYSTEM, "manifest_invalid", None)
        _, view = self.views()
        with self.connect() as connection:
            stored = connection.execute("SELECT count(*) AS n FROM billed_costs").fetchone()["n"]
        self.assertEqual((view["state"], view["periods"], view["read_at"]), ("error", [], None))
        self.assertEqual((view["error_code"], view["error_detail"]), ("manifest_invalid", None))
        self.assertGreater(stored, 0)
        self.store.save_absent("not_configured", SYSTEM)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM billed_costs").fetchone()["n"], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) AS n FROM billed_cost_periods").fetchone()["n"], 0)

    def test_without_a_system_bucket_the_state_is_unavailable(self):
        with self.connect() as connection:
            connection.execute("DELETE FROM system_bucket")
            view = bc.installation_view(connection, self.cfg)
        self.assertEqual((view["state"], view["bucket"], view["periods"]), ("unavailable", None, []))

    def test_claims_are_single_persisted_and_released(self):
        self.assertTrue(self.store.claim(None, NOW))
        self.assertFalse(self.store.claim(None, NOW))
        self.assertEqual(self.store.load().state, "checking")
        self.assertFalse(self.store.claim(NOW - timedelta(days=1), NOW + timedelta(days=1)))
        self.assertTrue(self.store.claim(NOW, NOW + timedelta(days=1)))
        self.store.release(NOW + timedelta(days=1), NOW)
        self.assertEqual(self.store.load().checked_at, NOW)
        self.store.release(NOW, None)
        self.assertIsNone(self.store.load())

    def test_a_deleted_vault_takes_its_rows(self):
        # Vault Deletion (#717) deletes the Vault row last: its totals go with it.
        with self.connect() as connection:
            action = connection.execute(
                "SELECT confdeltype FROM pg_constraint WHERE conrelid = 'billed_costs'::regclass "
                "AND confrelid = 'vaults'::regclass AND contype = 'f'").fetchone()
        self.assertEqual(action["confdeltype"], "c")

    def test_constraints(self):
        import psycopg

        self.read_fixture()
        bad = [
            "INSERT INTO billed_cost_periods VALUES ('2026-11-02', 'k', 'e', now(), 'd', 'USD', NULL, now())",
            "INSERT INTO billed_costs VALUES ('2026-11-01', 'x', 'vault', NULL, 'storage', 1)",
            "INSERT INTO billed_costs VALUES ('2026-11-01', 'x', 'system', 1, 'storage', 1)",
            "INSERT INTO billed_costs VALUES ('2026-11-01', 'x', 'system', NULL, 'tax', 1)",
            "UPDATE billing_export_status SET state = 'error', error_code = NULL",
            "UPDATE billing_export_status SET state = 'error', error_code = 'S3 refused it'",
            "UPDATE billing_export_status SET error_detail = 'not an error'",
            "UPDATE billing_export_status SET currency = 'usd'",
            "INSERT INTO billing_export_status(id, state, checked_at) VALUES (2, 'checking', now())",
        ]
        for statement in bad:
            with self.subTest(statement), self.connect() as connection, \
                    self.assertRaises(psycopg.errors.CheckViolation):
                connection.execute(statement)
        with self.connect() as connection, self.assertRaises(psycopg.errors.ForeignKeyViolation):
            connection.execute("INSERT INTO billed_costs VALUES ('2026-01-01', 'x', 'system', NULL, 'credits', -1)")

if __name__ == "__main__":
    unittest.main()
