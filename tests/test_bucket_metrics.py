"""Daily Vault Bucket sizes from CloudWatch (#737, ADR-0051).

No AWS: ``FakeCloudWatch`` answers ``GetMetricStatistics`` like CloudWatch and
fails on anything else, ``GetMetricData`` above all (always billed). The
catalog tests run on a disposable PostgreSQL 16 database.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from botocore.exceptions import ClientError, EndpointConnectionError

from app.services import bucket_metrics

NOW = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)
BUCKET = "frostvault-docs-ab12cd34"


def denied(operation: str = "GetMetricStatistics", code: str = "AccessDenied") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "User arn:aws:iam::123:user/x is not authorized"}},
                       operation)


class FakeCloudWatch:
    """Daily data points per (bucket, metric, StorageType); GetMetricStatistics only."""

    def __init__(self, points: dict[tuple[str, str, str], dict[date, float]] | None = None,
                 error: BaseException | None = None) -> None:
        self.points = points or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        dimensions = {item["Name"]: item["Value"] for item in kwargs["Dimensions"]}
        series = self.points.get((dimensions["BucketName"], kwargs["MetricName"], dimensions["StorageType"]), {})
        return {"Label": kwargs["MetricName"], "Datapoints": [
            {"Timestamp": datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
             "Average": value, "Unit": "Bytes"}
            for day, value in series.items()
            if kwargs["StartTime"] <= datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) <= kwargs["EndTime"]]}

    def get_metric_data(self, **_: Any) -> dict[str, Any]:  # pragma: no cover - must never run
        raise AssertionError("GetMetricData is always billed: never call it")

    def close(self) -> None:
        self.closed = True


def bucket_points(bucket: str = BUCKET, *, day: date = date(2026, 11, 15)) -> dict:
    return {
        (bucket, "BucketSizeBytes", "StandardStorage"): {day: 1_000.0, day - timedelta(days=1): 900.0},
        (bucket, "BucketSizeBytes", "DeepArchiveStorage"): {day: 5_000_000.0},
        (bucket, "BucketSizeBytes", "DeepArchiveObjectOverhead"): {day: 32_768.0},
        (bucket, "BucketSizeBytes", "DeepArchiveS3ObjectOverhead"): {day: 8_192.0},
        (bucket, "NumberOfObjects", "AllStorageTypes"): {day: 3.0, day - timedelta(days=1): 2.0},
    }


class StorageTypeTests(unittest.TestCase):
    def test_every_storage_type_maps_to_a_class_and_a_kind(self) -> None:
        for name in ("StandardStorage", "StandardIAStorage", "GlacierInstantRetrievalStorage",
                     "GlacierStorage", "GlacierS3ObjectOverhead", "DeepArchiveStorage",
                     "DeepArchiveS3ObjectOverhead", "DeepArchiveObjectOverhead"):
            self.assertIn(name, bucket_metrics.STORAGE_TYPES)
        for name, meaning in bucket_metrics.STORAGE_TYPES.items():
            with self.subTest(name=name):
                self.assertIn(meaning.storage_class, bucket_metrics.CLASS_ORDER)
                self.assertIn(meaning.billed_as, bucket_metrics.CLASS_ORDER)
                self.assertIn(meaning.kind, {"data", "object_overhead", "size_overhead", "staging"})
        deep = bucket_metrics.STORAGE_TYPES["DeepArchiveS3ObjectOverhead"]
        self.assertEqual((deep.storage_class, deep.kind, deep.billed_as),
                         ("DEEP_ARCHIVE", "object_overhead", "STANDARD"))
        self.assertEqual(bucket_metrics.STORAGE_TYPES["GlacierIRSizeOverhead"].kind, "size_overhead")

    def test_a_day_is_summarized_by_class_with_the_overhead_apart(self) -> None:
        summary = bucket_metrics.summarize_day({
            "DeepArchiveStorage": 5_000, "DeepArchiveObjectOverhead": 32, "DeepArchiveS3ObjectOverhead": 8,
            "StandardStorage": 100, "GlacierStorage": 0, "SomethingNew": 7})
        self.assertEqual(summary["bytes"], 5_100)
        self.assertEqual(summary["overhead_bytes"], 40)
        self.assertEqual([entry["storage_class"] for entry in summary["by_storage_class"]],
                         ["STANDARD", "DEEP_ARCHIVE"])
        deep = summary["by_storage_class"][1]
        self.assertEqual((deep["bytes"], deep["overhead_bytes"]), (5_000, 40))
        self.assertEqual({part["storage_type"]: part["billed_as"] for part in deep["overhead"]},
                         {"DeepArchiveObjectOverhead": "DEEP_ARCHIVE",
                          "DeepArchiveS3ObjectOverhead": "STANDARD"})


class ReadTests(unittest.TestCase):
    def test_only_get_metric_statistics_daily_over_the_kept_days(self) -> None:
        client = FakeCloudWatch(bucket_points())
        days = bucket_metrics.read_bucket(client, BUCKET, now=NOW)
        self.assertEqual(len(client.calls), len(bucket_metrics.STORAGE_TYPES) + 1)
        for call in client.calls:
            self.assertEqual((call["Namespace"], call["Period"], call["Statistics"]),
                             ("AWS/S3", 86_400, ["Average"]))
            self.assertEqual(call["Dimensions"][0], {"Name": "BucketName", "Value": BUCKET})
            self.assertEqual(call["StartTime"], datetime(2026, 10, 18, tzinfo=timezone.utc))
            self.assertEqual(call["EndTime"], NOW)
        count = client.calls[-1]
        self.assertEqual((count["MetricName"], count["Dimensions"][1]["Value"]),
                         ("NumberOfObjects", "AllStorageTypes"))
        self.assertEqual(sorted(days), [date(2026, 11, 14), date(2026, 11, 15)])
        self.assertEqual(days[date(2026, 11, 15)].storage_bytes, {
            "StandardStorage": 1_000, "DeepArchiveStorage": 5_000_000,
            "DeepArchiveObjectOverhead": 32_768, "DeepArchiveS3ObjectOverhead": 8_192})
        self.assertEqual(days[date(2026, 11, 15)].object_count, 3)
        self.assertEqual(days[date(2026, 11, 14)].storage_bytes, {"StandardStorage": 900})

    def test_errors_keep_their_code_never_their_message(self) -> None:
        self.assertEqual(bucket_metrics.classify_error(denied()), ("access_denied", "AccessDenied"))
        self.assertEqual(bucket_metrics.classify_error(denied(code="AccessDeniedException")),
                         ("access_denied", "AccessDeniedException"))
        self.assertEqual(bucket_metrics.classify_error(denied(code="Throttling")), ("failed", "Throttling"))
        outcome, code = bucket_metrics.classify_error(EndpointConnectionError(endpoint_url="https://x"))
        self.assertEqual((outcome, code), ("failed", "EndpointConnectionError"))

    def test_placeholder_credentials_read_nothing(self) -> None:
        with patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": "REPLACE_ME", "AWS_SECRET_ACCESS_KEY": "REPLACE_ME"}):
            with self.assertRaises(bucket_metrics.CloudWatchUnavailable):
                bucket_metrics._default_client("eu-south-1")


class CatalogFixture(unittest.TestCase):
    def setUp(self) -> None:
        from tests.postgres import PostgresConnection
        from tests.test_database import run_alembic

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "catalog"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connect = lambda: PostgresConnection(str(self.path))
        with self.connect() as connection:
            self.dedicated = self.vault(connection, 2, "Docs", BUCKET, "bucket_root")
            self.shared = self.vault(connection, 3, "Immich", "shared-archive", "vault_prefix")
            self.other = self.vault(connection, 4, "Photos", "frostvault-photos-ab12cd35", "bucket_root",
                                    region="eu-west-1")

    def vault(self, connection: Any, vault_id: int, name: str, bucket: str, layout: str, *,
              region: str = "eu-south-1", lifecycle: str = "active") -> int:
        target = connection.execute(
            "INSERT INTO vault_targets(provider, bucket, region, key_layout) VALUES ('s3', %s, %s, %s) "
            "RETURNING id", (bucket, region, layout)).fetchone()["id"]
        connection.execute(
            "INSERT INTO vaults(id, target_id, name, source_root, lifecycle) VALUES (%s, %s, %s, %s, %s)",
            (vault_id, target, name, f"/sources/managed/{vault_id}", lifecycle))
        return vault_id

    def rows(self, sql: str, *params: Any) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def run_due(self, clients: dict[str, FakeCloudWatch], now: datetime = NOW) -> int:
        return bucket_metrics.run_due(self.connect, client_for=lambda region: clients[region], now=now)


class RunTests(CatalogFixture):
    def test_each_vault_bucket_is_read_once_a_day_in_its_region_never_a_shared_bucket(self) -> None:
        south = FakeCloudWatch(bucket_points())
        west = FakeCloudWatch(bucket_points("frostvault-photos-ab12cd35"))
        self.assertEqual(self.run_due({"eu-south-1": south, "eu-west-1": west}), 2)
        buckets = {call["Dimensions"][0]["Value"] for call in south.calls}
        self.assertEqual(buckets, {BUCKET})
        self.assertEqual({call["Dimensions"][0]["Value"] for call in west.calls},
                         {"frostvault-photos-ab12cd35"})
        days = self.rows("SELECT vault_id, day, storage_bytes, object_count FROM vault_bucket_metric_days "
                         "WHERE vault_id=2 ORDER BY day")
        self.assertEqual([(row["day"], row["object_count"]) for row in days],
                         [(date(2026, 11, 14), 2), (date(2026, 11, 15), 3)])
        self.assertEqual(days[1]["storage_bytes"]["DeepArchiveStorage"], 5_000_000)
        reads = self.rows("SELECT vault_id, outcome, error_code FROM vault_bucket_metric_reads ORDER BY vault_id")
        self.assertEqual([(row["vault_id"], row["outcome"]) for row in reads], [(2, "ok"), (4, "ok")])
        # Persisted: a restart within the day reads nothing again.
        later = FakeCloudWatch(bucket_points())
        self.assertEqual(self.run_due({"eu-south-1": later, "eu-west-1": later}, NOW + timedelta(hours=23)), 0)
        self.assertEqual(later.calls, [])
        self.assertEqual(self.run_due({"eu-south-1": later, "eu-west-1": later}, NOW + timedelta(days=1)), 2)

    def test_a_provisioning_vault_is_not_read(self) -> None:
        with self.connect() as connection:
            self.vault(connection, 5, "New", "frostvault-new-ab12cd36", "bucket_root", lifecycle="provisioning")
        self.run_due({"eu-south-1": FakeCloudWatch(), "eu-west-1": FakeCloudWatch()})
        self.assertEqual(self.rows("SELECT vault_id FROM vault_bucket_metric_reads WHERE vault_id=5"), [])

    def test_without_the_permission_the_read_is_a_note_never_a_problem(self) -> None:
        south = FakeCloudWatch(error=denied())
        west = FakeCloudWatch(bucket_points("frostvault-photos-ab12cd35"))
        self.assertEqual(self.run_due({"eu-south-1": south, "eu-west-1": west}), 2)
        reads = {row["vault_id"]: row for row in self.rows("SELECT * FROM vault_bucket_metric_reads")}
        self.assertEqual((reads[2]["outcome"], reads[2]["error_code"]), ("access_denied", "AccessDenied"))
        # One failing Vault never holds back the others.
        self.assertEqual(reads[4]["outcome"], "ok")
        self.assertEqual(self.rows("SELECT id FROM notifications"), [])
        self.assertEqual(len(south.calls), 1)  # a read stops at its first failure
        with self.connect() as connection:
            usage = bucket_metrics.vault_usage(connection, 2)
        self.assertEqual(usage["measured"]["status"], "access_denied")
        self.assertNotIn("not authorized", repr(usage))

    def test_a_new_bucket_without_data_points_is_no_data(self) -> None:
        self.run_due({"eu-south-1": FakeCloudWatch(), "eu-west-1": FakeCloudWatch()})
        with self.connect() as connection:
            usage = bucket_metrics.vault_usage(connection, 2)
        self.assertEqual(usage["measured"]["status"], "no_data")
        self.assertEqual(usage["measured"]["next_read_after"], (NOW + timedelta(days=1)).isoformat())

    def test_older_days_are_pruned_and_the_latest_kept_after_a_failure(self) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO vault_bucket_metric_days(vault_id, day, storage_bytes, object_count, read_at) "
                "VALUES (2, '2026-09-01', '{\"StandardStorage\": 1}', 1, now())")
        self.run_due({"eu-south-1": FakeCloudWatch(bucket_points()), "eu-west-1": FakeCloudWatch()})
        self.assertEqual([row["day"] for row in self.rows(
            "SELECT day FROM vault_bucket_metric_days WHERE vault_id=2 ORDER BY day")],
            [date(2026, 11, 14), date(2026, 11, 15)])
        failing = FakeCloudWatch(error=denied(code="Throttling"))
        self.run_due({"eu-south-1": failing, "eu-west-1": failing}, NOW + timedelta(days=1))
        with self.connect() as connection:
            measured = bucket_metrics.vault_usage(connection, 2)["measured"]
        self.assertEqual((measured["status"], measured["day"]), ("available", "2026-11-15"))
        self.assertEqual(measured["last_read"]["outcome"], "failed")
        self.assertEqual(measured["last_read"]["error_code"], "Throttling")

    def test_an_unexpected_error_is_raised_after_every_vault_was_tried(self) -> None:
        class Broken(FakeCloudWatch):
            def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
                raise ValueError("bug")

        west = FakeCloudWatch(bucket_points("frostvault-photos-ab12cd35"))
        with self.assertRaises(ValueError):
            self.run_due({"eu-south-1": Broken(), "eu-west-1": west})
        self.assertEqual([row["vault_id"] for row in self.rows("SELECT vault_id FROM vault_bucket_metric_reads")], [4])

    def test_the_vault_page_shows_the_catalog_estimate_next_to_the_measured_sizes(self) -> None:
        self.run_due({"eu-south-1": FakeCloudWatch(bucket_points()), "eu-west-1": FakeCloudWatch()})
        with self.connect() as connection:
            usage = bucket_metrics.vault_usage(connection, 2)
            shared = bucket_metrics.vault_usage(connection, 3)
            with self.assertRaises(LookupError):
                bucket_metrics.vault_usage(connection, 999)
        self.assertTrue(usage["dedicated_bucket"])
        self.assertEqual(usage["estimate"]["by_storage_class"], [])
        self.assertTrue(usage["estimate"]["currency"])
        measured = usage["measured"]
        self.assertEqual((measured["status"], measured["day"], measured["object_count"]),
                         ("available", "2026-11-15", 3))
        self.assertEqual((measured["bytes"], measured["overhead_bytes"]), (5_001_000, 40_960))
        self.assertEqual([entry["storage_class"] for entry in measured["by_storage_class"]],
                         ["STANDARD", "DEEP_ARCHIVE"])
        self.assertEqual([day["day"] for day in measured["history"]], ["2026-11-15", "2026-11-14"])
        self.assertFalse(shared["dedicated_bucket"])
        self.assertEqual(shared["measured"], {"status": "shared_bucket"})


class HttpTests(CatalogFixture):
    def test_the_cloud_usage_is_served_and_a_missing_vault_is_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from app import main
        from app.config import settings
        from app.sessions import create_session
        from tests.owner_fixtures import insert_owner
        from tests.postgres import database_dsn

        configured = replace(settings, database_dsn=database_dsn(self.path), cookie_secure=False)
        for target in ("app.main.settings", "app.database.settings", "app.sessions.settings"):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        client = TestClient(main.app, client=("127.0.0.1", 50000))
        with self.connect() as connection:
            insert_owner(connection)
            token = create_session(connection, auth_method="oidc")
        client.cookies.set(settings.session_cookie_name, token)
        served = client.get("/api/vaults/3/cloud-usage")
        self.assertEqual(served.status_code, 200, served.text)
        self.assertEqual(served.json()["measured"], {"status": "shared_bucket"})
        self.assertEqual(served.json()["bucket"], "shared-archive")
        missing = client.get("/api/vaults/999/cloud-usage")
        self.assertEqual(missing.status_code, 404, missing.text)


if __name__ == "__main__":
    unittest.main()
