"""Billed Cost from the AWS Data Exports CUR 2.0 export (#738, ADR-0051).

Seam: ``app.services.billed_cost`` with an in-memory S3 (``FakeExportS3``) and
an in-memory store. ``tests/fixtures/billing/cur2-2026-11.csv`` is a small
synthetic CUR 2.0 file with the real column names; it is gzipped like AWS
delivers it. ``DatabaseStoreTests`` runs the store and the views against a
disposable PostgreSQL database (migration 0011).
"""
from __future__ import annotations

import gzip
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

from app.config import Settings
from app.services import billed_cost as bc
from app.services import system_bucket
from app.services.system_bucket import SystemBucket
from tests.fake_bucket_s3 import FakeBucketS3

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
        self.fail: dict[str, str] = {}

    def put(self, key: str, data: bytes, modified: datetime = DELIVERED) -> None:
        self.objects[key] = (data, modified)

    def list_objects_v2(self, Bucket, Prefix, Delimiter, ContinuationToken=None):
        self.calls.append(("list_objects_v2", Prefix))
        if "list_objects_v2" in self.fail:
            raise ClientError({"Error": {"Code": self.fail["list_objects_v2"], "Message": "secret detail"}},
                              "ListObjectsV2")
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
                entries.append(("object", {"Key": key, "LastModified": self.objects[key][1],
                                           "Size": len(self.objects[key][0])}))
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
        if "get_object" in self.fail:
            raise ClientError({"Error": {"Code": self.fail["get_object"], "Message": "secret detail"}},
                              "GetObject")
        return {"Body": RecordingBody(self.objects[Key][0], self.reads)}


def manifest(period: str, files: list[str], **extra) -> bytes:
    return json.dumps({"executionId": "synthetic", "exportName": "frostvault",
                       "billingPeriod": {"start": f"{period}-01T00:00:00.000Z"},
                       "dataFiles": files, **extra}).encode()


def delivered_export(s3: FakeExportS3, period: str = "2026-11", text: str | None = None,
                     modified: datetime = DELIVERED, chunks: int = 1) -> None:
    data = f"{EXPORT}data/BILLING_PERIOD={period}/"
    keys = [f"{data}frostvault-{index:05d}.csv.gz" for index in range(1, chunks + 1)]
    for index, key in enumerate(keys):
        s3.put(key, gz(text if text is not None else fixture_csv()) if index == 0 else b"", modified)
    s3.put(f"{EXPORT}metadata/BILLING_PERIOD={period}/frostvault-Manifest.json",
           manifest(period, [f"s3://{SYSTEM}/{key}" for key in keys]), modified)


def csv_text(rows: list[dict[str, str]]) -> str:
    header = ["line_item_resource_id", "line_item_unblended_cost", "line_item_currency_code",
              "line_item_usage_type", "line_item_operation", "line_item_product_code",
              "line_item_usage_end_date"]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(row.get(name, "") for name in header))
    return "\n".join(lines) + "\n"


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
        self.assertEqual(amounts[(VAULT_1, "storage")], Decimal("2.05"))
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

    def test_a_large_file_is_streamed_not_loaded(self):
        row = {"line_item_resource_id": VAULT_1, "line_item_unblended_cost": "0.0000010000",
               "line_item_currency_code": "USD", "line_item_usage_type": "EUS1-Requests-Tier1",
               "line_item_operation": "PutObject"}
        text = csv_text([row] * 200_000)
        self.assertGreater(len(text), 10 * 1024 * 1024)
        totals, state, reads = self.aggregate(text)
        self.assertEqual(state.rows, 200_000)
        self.assertEqual(totals.amounts[(VAULT_1, "requests")], Decimal("0.2"))
        self.assertLess(max(reads), 1024 * 1024)

    def test_an_empty_chunk_adds_nothing(self):
        totals = bc.PeriodTotals(date(2026, 11, 1))
        bc.aggregate_gzip(io.BytesIO(b""), totals, bc._Accumulator(ROLES))
        bc.aggregate_gzip(io.BytesIO(gz("")), totals, bc._Accumulator(ROLES))
        self.assertEqual(totals.amounts, {})

    def test_unreadable_exports_are_errors_with_safe_messages(self):
        base = {"line_item_resource_id": VAULT_1, "line_item_unblended_cost": "1",
                "line_item_currency_code": "USD", "line_item_usage_type": "x",
                "line_item_operation": "y"}
        cases = {
            "without resource IDs": ("line_item_unblended_cost\n1\n", "Include resource IDs"),
            "a cost that is text": (csv_text([{**base, "line_item_unblended_cost": "abc"}]), "not a number"),
            "mixed currencies": (csv_text([base, {**base, "line_item_currency_code": "EUR"}]), "mixes currencies"),
            "a short row": (csv_text([base]) + f"{VAULT_1},1\n", "fewer columns"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name), self.assertRaises(bc.BillingExportError) as raised:
                self.aggregate(text)
            self.assertIn(message, str(raised.exception))
        with self.assertRaises(bc.BillingExportError) as raised:
            bc.aggregate_gzip(io.BytesIO(b"not gzip at all"), bc.PeriodTotals(date(2026, 11, 1)),
                              bc._Accumulator(ROLES))
        self.assertIn("gzip CSV", str(raised.exception))


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
        # An older month is not read; neither is a manifest of one execution.
        delivered_export(s3, "2026-08", modified=DELIVERED - timedelta(days=90))
        s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/20261117T061200Z-abc/frostvault-Manifest.json",
               b"{not read}")
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual(reading.state, "ok")
        self.assertEqual([totals.period for totals in reading.periods],
                         [date(2026, 11, 1), date(2026, 10, 1)])
        self.assertEqual(reading.manifest_key,
                         f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json")
        self.assertEqual((reading.exported_at, reading.currency), (DELIVERED, "USD"))
        self.assertEqual(reading.data_through, datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertEqual(reading.periods[0].amounts[(VAULT_1, "storage")], Decimal("2.05"))
        read = [key for name, key in s3.calls if name == "get_object"]
        self.assertFalse(any("2026-08" in key or "20261117T" in key for key in read), read)

    def test_a_gap_month_is_not_called_previous(self):
        s3 = FakeExportS3()
        delivered_export(s3, "2026-09", modified=DELIVERED - timedelta(days=40))
        delivered_export(s3, "2026-11")
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual([totals.period for totals in reading.periods], [date(2026, 11, 1)])

    def test_the_export_delivered_last_wins(self):
        s3 = FakeExportS3()
        delivered_export(s3, "2026-11", modified=DELIVERED - timedelta(days=30))
        other = "billing/cur/newer/"
        s3.put(f"{other}data/BILLING_PERIOD=2026-11/newer-00001.csv.gz",
               gz(csv_text([{"line_item_resource_id": VAULT_2, "line_item_unblended_cost": "4",
                             "line_item_currency_code": "EUR", "line_item_usage_type": "EUS1-TimedStorage-ByteHrs",
                             "line_item_operation": "StandardStorage"}])))
        s3.put(f"{other}metadata/BILLING_PERIOD=2026-11/newer-Manifest.json",
               manifest("2026-11", [f"{other}data/BILLING_PERIOD=2026-11/newer-00001.csv.gz"]))
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual(reading.currency, "EUR")
        self.assertEqual(reading.periods[0].amounts, {(VAULT_2, "storage"): Decimal("4")})

    def test_manifest_entries_may_be_keys_or_objects(self):
        s3 = FakeExportS3()
        delivered_export(s3)
        key = f"{EXPORT}data/BILLING_PERIOD=2026-11/frostvault-00001.csv.gz"
        for files in ([key], [{"key": key}], [f"s3://{SYSTEM}/{key}"]):
            with self.subTest(files=files):
                s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json",
                       manifest("2026-11", files))
                self.assertEqual(bc.read_export(s3, SYSTEM, ROLES).state, "ok")

    def test_a_manifest_that_points_elsewhere_or_at_parquet_is_refused(self):
        cases = {
            "another bucket": ([f"s3://someone-else/{EXPORT}data/BILLING_PERIOD=2026-11/x.csv.gz"], "another bucket"),
            "outside its folder": (["system/backups/backup.fvb.csv.gz"], "outside its folder"),
            "parquet": ([f"{EXPORT}data/BILLING_PERIOD=2026-11/frostvault-00001.snappy.parquet"], "gzip – text/csv"),
            "no data files": ("missing", "lists no data files"),
        }
        for name, (files, message) in cases.items():
            with self.subTest(name):
                s3 = FakeExportS3()
                body = (json.dumps({"exportName": "frostvault"}).encode() if files == "missing"
                        else manifest("2026-11", files))
                s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json", body)
                with self.assertRaises(bc.BillingExportError) as raised:
                    bc.read_export(s3, SYSTEM, ROLES)
                self.assertIn(message, str(raised.exception))
                self.assertFalse([key for op, key in s3.calls if op == "get_object" and "Manifest" not in key])

    def test_a_month_without_frostvault_lines_is_zero_in_usd(self):
        s3 = FakeExportS3()
        delivered_export(s3, text=csv_text([]))
        reading = bc.read_export(s3, SYSTEM, ROLES)
        self.assertEqual((reading.state, reading.currency, reading.periods[0].amounts), ("ok", "USD", {}))

    def test_provider_messages_never_reach_the_status(self):
        s3 = FakeExportS3()
        s3.fail["list_objects_v2"] = "AccessDenied"
        with self.assertRaises(ClientError) as raised:
            bc.read_export(s3, SYSTEM, ROLES)
        message = bc.safe_error(raised.exception)
        self.assertEqual(message, "S3 refused s3:ListBucket (AccessDenied)")
        self.assertEqual(bc.safe_error(ValueError("/secret/path")), "internal error (ValueError)")


class MemoryStore:
    def __init__(self, status: bc.Status | None = None):
        self.status = status
        self.saved: list = []
        self.rows: dict = {}

    def load(self):
        return self.status

    def claim(self, previous, now):
        current = self.status.checked_at if self.status else None
        if current != previous:
            return False
        self.status = (bc.Status("checking", now) if self.status is None
                       else replace(self.status, checked_at=now))
        return True

    def save_absent(self, state, bucket):
        self.rows = {}
        self.status = replace(self.status, state=state, bucket=bucket, error=None)
        self.saved.append(state)

    def save_error(self, bucket, error):
        self.status = replace(self.status, state="error", bucket=bucket, error=error)
        self.saved.append("error")

    def save_reading(self, bucket, reading, roles, now):
        self.rows = {totals.period: dict(totals.amounts) for totals in reading.periods}
        self.status = replace(self.status, state="ok", bucket=bucket, read_at=now,
                              exported_at=reading.exported_at, currency=reading.currency,
                              manifest_key=reading.manifest_key, error=None)
        self.saved.append("ok")

    def roles(self, system):
        return dict(ROLES)


class RunDueTests(unittest.TestCase):
    def run_due(self, store, s3, *, now=NOW, bucket=SystemBucket(SYSTEM, "eu-south-1", "created")):
        regions: list[str] = []

        def client_for(region):
            regions.append(region)
            return s3
        ran = bc.run_due(store=store, location=lambda: bucket, client_for=client_for, now=now)
        return ran, regions

    def test_reads_once_a_day_from_the_persisted_last_look(self):
        s3 = FakeExportS3()
        delivered_export(s3)
        store = MemoryStore()
        self.assertEqual(self.run_due(store, s3), (True, ["eu-south-1"]))
        self.assertEqual(store.status.state, "ok")
        self.assertEqual(store.rows[date(2026, 11, 1)][(VAULT_1, "storage")], Decimal("2.05"))
        # A restart (a new loop, the same persisted status) does not read early.
        reads = len(s3.calls)
        self.assertEqual(self.run_due(store, s3, now=NOW + timedelta(hours=23, minutes=59)), (False, []))
        self.assertEqual(len(s3.calls), reads)
        self.assertTrue(self.run_due(store, s3, now=NOW + timedelta(days=1))[0])

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
        self.assertEqual((store.status.state, store.status.error),
                         ("error", "S3 refused s3:ListBucket (AccessDenied)"))
        self.assertFalse(self.run_due(store, s3, now=NOW + timedelta(hours=12))[0])

    def test_an_error_keeps_the_last_good_totals(self):
        s3 = FakeExportS3()
        delivered_export(s3)
        store = MemoryStore()
        self.run_due(store, s3)
        s3.put(f"{EXPORT}metadata/BILLING_PERIOD=2026-11/frostvault-Manifest.json", b"[1")
        self.run_due(store, s3, now=NOW + timedelta(days=1))
        self.assertEqual(store.status.state, "error")
        self.assertIn(date(2026, 11, 1), store.rows)

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

    def test_the_maintenance_task_only_logs(self):
        from app import storage

        with patch.object(bc, "run_due", side_effect=RuntimeError("db down")), \
                patch.object(storage.worker_error_store, "log_worker_error") as logged, \
                patch.object(storage, "_report_worker_error") as reported:
            storage.read_billed_cost_once()
        logged.assert_called_once()
        reported.assert_not_called()


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


class DatabaseStoreTests(unittest.TestCase):
    """``billing_export_status`` and ``billed_costs`` of migration 0011 (PostgreSQL)."""

    def setUp(self):
        from tests.postgres import PostgresConnection
        from tests.test_database import run_alembic

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "billed-cost.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connect = lambda: PostgresConnection(str(self.path))
        self.store = bc.DatabaseStore(self.connect)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO vault_targets(id, provider, bucket, region, key_layout) VALUES "
                f"(1, 's3', '{VAULT_1}', 'eu-south-1', 'bucket_root'), "
                f"(2, 's3', '{VAULT_2}', 'eu-south-1', 'bucket_root'), "
                f"(3, 's3', '{SHARED}', 'eu-south-1', 'vault_prefix')")
            connection.execute(
                "INSERT INTO vaults(id, target_id, name, source_root) VALUES "
                "(1, 1, 'Foto', '/sources/foto'), (2, 2, 'Documenti', '/sources/documenti'), "
                "(3, 3, 'Vecchio', '/sources/vecchio')")
            connection.execute(
                "INSERT INTO system_bucket(id, name, region, reserved_at, ready_at) "
                f"VALUES (1, '{SYSTEM}', 'eu-south-1', now(), now())")
        self.cfg = replace(Settings(), aws_region="eu-south-1", metadata_backup_bucket="")

    def read_fixture(self) -> None:
        s3 = FakeExportS3()
        delivered_export(s3, "2026-10", modified=DELIVERED - timedelta(days=16),
                         text=csv_text([{"line_item_resource_id": VAULT_1, "line_item_unblended_cost": "1.5",
                                         "line_item_currency_code": "USD",
                                         "line_item_usage_type": "EUS1-TimedStorage-ByteHrs",
                                         "line_item_operation": "StandardStorage"}]))
        delivered_export(s3, "2026-11")
        with patch.object(system_bucket, "location",
                          return_value=SystemBucket(SYSTEM, "eu-south-1", "created")):
            self.assertTrue(bc.run_due(store=self.store, client_for=lambda region: s3, now=NOW))

    def test_roles_come_from_the_vault_targets(self):
        with self.connect() as connection:
            roles = bc.bucket_roles(connection, SYSTEM)
        self.assertEqual(roles, {SYSTEM: bc.BucketRole("system"), VAULT_1: bc.BucketRole("vault", 1),
                                 VAULT_2: bc.BucketRole("vault", 2), SHARED: bc.BucketRole("shared")})

    def test_a_read_is_stored_and_shown_per_vault_and_installation(self):
        self.read_fixture()
        with self.connect() as connection:
            view = bc.installation_view(connection, self.cfg)
            vault = bc.vault_view(connection, 1, self.cfg)
            shared = bc.vault_view(connection, 3, self.cfg)
        self.assertEqual((view["state"], view["bucket"], view["currency"]), ("ok", SYSTEM, "USD"))
        self.assertEqual(datetime.fromisoformat(view["exported_at"]), DELIVERED)
        self.assertEqual(datetime.fromisoformat(view["data_through"]),
                         datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertEqual([period["period"] for period in view["periods"]], ["2026-11", "2026-10"])
        november = view["periods"][0]
        self.assertEqual([(row["name"], row["total"]) for row in november["vaults"]],
                         [("Documenti", 2.2), ("Foto", 2.62)])
        self.assertEqual({row["role"]: row["total"] for row in november["installation"]},
                         {"system": 0.015, "shared": 3.0})
        self.assertEqual(november["total"], 7.835)
        self.assertEqual(vault["periods"][0]["categories"],
                         {"storage": 2.05, "requests": 0.11, "transitions": 0.3, "retrievals": 0.07,
                          "data_transfer": 0.09, "other": 0.0})
        self.assertEqual([period["total"] for period in vault["periods"]], [2.62, 1.5])
        self.assertFalse(vault["shared"])
        self.assertEqual(vault["estimate"]["monthly"], 0)
        # An older Vault in a shared bucket sees the whole bucket, flagged.
        self.assertTrue(shared["shared"])
        self.assertEqual([period["total"] for period in shared["periods"]], [3.0, 0.0])

    def test_an_error_hides_the_totals_and_a_missing_export_clears_them(self):
        self.read_fixture()
        self.store.save_error(SYSTEM, "The export's Manifest.json is not valid JSON")
        with self.connect() as connection:
            view = bc.vault_view(connection, 1, self.cfg)
            stored = connection.execute("SELECT count(*) AS n FROM billed_costs").fetchone()["n"]
        self.assertEqual((view["state"], view["periods"], view["read_at"]), ("error", [], None))
        self.assertEqual(view["error"], "The export's Manifest.json is not valid JSON")
        self.assertGreater(stored, 0)
        self.store.save_absent("not_configured", SYSTEM)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM billed_costs").fetchone()["n"], 0)

    def test_without_a_system_bucket_the_state_is_unavailable(self):
        with self.connect() as connection:
            connection.execute("DELETE FROM system_bucket")
            view = bc.installation_view(connection, self.cfg)
        self.assertEqual((view["state"], view["bucket"], view["periods"]), ("unavailable", None, []))

    def test_claims_are_single_and_persisted(self):
        self.assertTrue(self.store.claim(None, NOW))
        self.assertFalse(self.store.claim(None, NOW))
        self.assertEqual(self.store.load().state, "checking")
        self.assertFalse(self.store.claim(NOW - timedelta(days=1), NOW + timedelta(days=1)))
        self.assertTrue(self.store.claim(NOW, NOW + timedelta(days=1)))

    def test_a_deleted_vault_takes_its_rows(self):
        # Vault Deletion (#717) deletes the Vault row last: its totals go with it.
        with self.connect() as connection:
            action = connection.execute(
                "SELECT confdeltype FROM pg_constraint WHERE conrelid = 'billed_costs'::regclass "
                "AND contype = 'f'").fetchone()
        self.assertEqual(action["confdeltype"], "c")

    def test_constraints(self):
        import psycopg

        self.read_fixture()
        bad = [
            "INSERT INTO billed_costs VALUES ('2026-11-02', 'x', 'system', NULL, 'storage', 1)",
            "INSERT INTO billed_costs VALUES ('2026-11-01', 'x', 'vault', NULL, 'storage', 1)",
            "INSERT INTO billed_costs VALUES ('2026-11-01', 'x', 'system', 1, 'storage', 1)",
            "INSERT INTO billed_costs VALUES ('2026-11-01', 'x', 'system', NULL, 'tax', 1)",
            "UPDATE billing_export_status SET state = 'error', error = NULL",
            "UPDATE billing_export_status SET currency = 'usd'",
            "INSERT INTO billing_export_status(id, state, checked_at) VALUES (2, 'checking', now())",
        ]
        for statement in bad:
            with self.subTest(statement), self.connect() as connection, \
                    self.assertRaises(psycopg.errors.CheckViolation):
                connection.execute(statement)


if __name__ == "__main__":
    unittest.main()
