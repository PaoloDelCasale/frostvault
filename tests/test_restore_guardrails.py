"""Restore cost/time estimates and high-impact approval (issue #4).

Seams under test:
- Pure estimator `estimate_restore` (size, storage class, tier → cost/time).
- Restore Approval observed through Job status: the high-impact thresholds
  apply once, at queue time, to the whole recovery request (#533). Above them
  every Job of the group is `pending_approval` and never calls AWS
  RestoreObject until one primary-owner approval per group. The worker never
  re-evaluates the thresholds (#534).

BUG-015 (REQ-027): the queue-time high-impact seam must price Glacier restores
from the bundled AWS list prices of the configured region (parity with
``POST /api/recover/estimate``), not from another region's rates (#413).
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import hashlib
import tempfile
from dataclasses import replace
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import config
from app.catalog import ArchiveCatalog

from fastapi import HTTPException

from app.main import (
    RecoverApproveAction,
    RecoverEstimateRequest,
    approve_recover,
    queue_jobs,
    recover_estimate,
)
from app.services.cost_estimates import list_prices
from app.services.restore_estimates import (
    estimate_restore,
    is_high_impact_restore,
)
from app.storage import process_jobs_once
from tests.test_database import run_alembic
from tests.test_recovery_verification import (
    _FakeBody,
    _add_cloud_only_version,
    _prepare_cloud_only_version,
)


class RestoreEstimateTests(unittest.TestCase):
    def test_bulk_glacier_estimate_uses_configured_pricing(self) -> None:
        estimate = estimate_restore(
            size_bytes=10 * 1024**3,
            storage_class="GLACIER",
            tier="Bulk",
            days=3,
            pricing={
                "GLACIER": {"Expedited": 0.03, "Standard": 0.01, "Bulk": 0.0025},
                "DEEP_ARCHIVE": {"Standard": 0.02, "Bulk": 0.0025},
            },
            hours={
                "GLACIER": {"Expedited": 5 / 60, "Standard": 5, "Bulk": 12},
                "DEEP_ARCHIVE": {"Standard": 12, "Bulk": 48},
            },
        )
        self.assertEqual(estimate.tier, "Bulk")
        self.assertEqual(estimate.days, 3)
        self.assertAlmostEqual(estimate.estimated_cost_eur, 0.025, places=6)
        self.assertEqual(estimate.estimated_hours, 12)
        self.assertTrue(estimate.restore_object_irreversible)

    def test_high_impact_triggered_by_size_or_cost(self) -> None:
        self.assertTrue(
            is_high_impact_restore(
                size_bytes=100 * 1024**3,
                estimated_cost_eur=1.0,
                size_threshold_gib=100,
                cost_threshold_eur=10.0,
            )
        )
        self.assertTrue(
            is_high_impact_restore(
                size_bytes=1024,
                estimated_cost_eur=10.0,
                size_threshold_gib=100,
                cost_threshold_eur=10.0,
            )
        )
        self.assertFalse(
            is_high_impact_restore(
                size_bytes=50 * 1024**3,
                estimated_cost_eur=5.0,
                size_threshold_gib=100,
                cost_threshold_eur=10.0,
            )
        )


class HighImpactApprovalTests(unittest.TestCase):
    def test_high_impact_glacier_restore_waits_for_owner_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 100 GiB → high impact by size even with cheap Bulk pricing
            size = 100 * 1024**3
            payload = b"x"  # content unused until download
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="huge.bin",
                payload=payload,
                storage_class="GLACIER",
                object_key="docs/huge.bin",
            )
            with PostgresConnection(str(database_path)) as connection:
                connection.execute(
                    "UPDATE archive_versions SET size=%s WHERE id=%s",
                    (size, version_id),
                )

            database_settings = SimpleNamespace(
                database_dsn=database_dsn(str(database_path)),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
                restore_days=3,
                restore_tier="Bulk",
                restore_high_impact_gib=100,
                restore_high_impact_eur=10.0,
                restore_approval_hold_seconds=3600,
            )
            client = Mock()
            client.head_object = Mock(
                return_value={
                    "ContentLength": size,
                    "StorageClass": "GLACIER",
                    "VersionId": "s3-version-1",
                }
            )
            client.restore_object = Mock()
            client.get_object = Mock()
            with patch("app.database.settings", database_settings):
                queued = queue_jobs("huge.bin", "recover", 2, 1)
                self.assertTrue(queued["approval_required"])
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    patch("app.storage.s3_client", return_value=client),
                ):
                    process_jobs_once()

            client.restore_object.assert_not_called()
            client.get_object.assert_not_called()
            client.head_object.assert_not_called()
            with PostgresConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT j.status, j.message, rr.approval_deadline AS pending_until FROM jobs j JOIN files f ON f.id=j.file_id LEFT JOIN job_restores jr ON jr.job_id=j.id LEFT JOIN restore_requests rr ON rr.group_id=j.group_id WHERE f.path=%s",
                    ("huge.bin",),
                ).fetchone()
            self.assertEqual(job["status"], "pending_approval")
            self.assertIsNotNone(job["pending_until"])
            self.assertFalse((source / "huge.bin").exists())

    def test_bug_015_recover_uses_configured_region_list_prices(self) -> None:
        """[BUG-015][Req: REQ-027] queue-time high-impact uses the region's prices.

        Size stays under the GiB threshold. The Glacier Standard list price of
        eu-south-1 crosses ``restore_high_impact_eur`` while us-east-1's does
        not, so pending_approval proves the worker priced the configured region.
        """
        milan = list_prices("eu-south-1").restore_rates["GLACIER"]["Standard"]
        virginia = list_prices("us-east-1").restore_rates["GLACIER"]["Standard"]
        self.assertGreater(milan, virginia)
        threshold = (milan + virginia) / 2
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            size = 1 * 1024**3
            expected_cost = milan  # 1 GiB × eu-south-1 Glacier Standard rate
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="priced.bin",
                payload=b"x",
                storage_class="GLACIER",
                object_key="docs/priced.bin",
            )
            with PostgresConnection(str(database_path)) as connection:
                connection.execute(
                    "UPDATE archive_versions SET size=%s WHERE id=%s",
                    (size, version_id),
                )

            database_settings = SimpleNamespace(
                database_dsn=database_dsn(str(database_path)),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
                restore_days=3,
                restore_tier="Standard",
                restore_high_impact_gib=100,
                restore_high_impact_eur=threshold,
                restore_approval_hold_seconds=3600,
                aws_region="eu-south-1",
            )
            client = Mock()
            client.head_object = Mock(
                return_value={
                    "ContentLength": size,
                    "StorageClass": "GLACIER",
                    "VersionId": "s3-version-1",
                }
            )
            client.restore_object = Mock()
            client.get_object = Mock()
            runtime = replace(
                config.settings,
                restore_tier="Standard",
                restore_days=3,
                restore_high_impact_gib=100,
                restore_high_impact_eur=threshold,
                restore_approval_hold_seconds=3600,
                aws_region="eu-south-1",
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.main.settings", runtime),
            ):
                queue_jobs("priced.bin", "recover", 2, 1, restore_tier="Standard")
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    patch("app.storage.s3_client", return_value=client),
                ):
                    process_jobs_once()

            client.restore_object.assert_not_called()
            client.head_object.assert_not_called()
            with PostgresConnection(str(database_path)) as connection:
                job = connection.execute(
                    """
                    SELECT j.status, rr.approval_deadline AS pending_until, rr.estimated_cost_eur
                    FROM jobs j JOIN files f ON f.id=j.file_id LEFT JOIN job_restores jr ON jr.job_id=j.id LEFT JOIN restore_requests rr ON rr.group_id=j.group_id WHERE f.path=%s
                    """,
                    ("priced.bin",),
                ).fetchone()
            self.assertEqual(
                job["status"],
                "pending_approval",
                "eu-south-1 list prices must trigger the high-impact hold",
            )
            self.assertIsNotNone(job["pending_until"])
            self.assertAlmostEqual(
                float(job["estimated_cost_eur"]),
                expected_cost,
                places=6,
                msg="queue-time estimate must use the configured region's list prices",
            )
            self.assertFalse((source / "priced.bin").exists())


def _add_cloud_only_file(
    database_path: Path, *, relative_path: str, storage_class: str
) -> str:
    """Seed one more verified, locally released file in Vault 2."""
    return _add_cloud_only_version(
        database_path, relative_path=relative_path, payload=b"x", storage_class=storage_class,
        provider_version_id=f"s3-{relative_path}", object_key=f"docs/{relative_path}",
    )


class DirectoryRestoreTierTests(unittest.TestCase):
    """#532: a directory recovery persists the tier/days chosen in the dialog."""

    def _prepare_directory(self, root: Path) -> Path:
        _source, database_path, _version_id = _prepare_cloud_only_version(
            root,
            relative_path="photos/a.jpg",
            payload=b"x",
            storage_class="DEEP_ARCHIVE",
            object_key="docs/photos/a.jpg",
        )
        _add_cloud_only_file(
            database_path, relative_path="photos/b.jpg", storage_class="DEEP_ARCHIVE"
        )
        _add_cloud_only_file(
            database_path, relative_path="photos/sub/c.jpg", storage_class="GLACIER"
        )
        _add_cloud_only_file(
            database_path, relative_path="photos/d.jpg", storage_class="STANDARD"
        )
        return database_path

    def _jobs(self, database_path: Path) -> dict[str, dict]:
        with PostgresConnection(str(database_path)) as connection:
            rows = connection.execute(
                "SELECT f.path, jr.tier AS restore_tier, jr.days AS restore_days FROM jobs j JOIN files f ON f.id=j.file_id LEFT JOIN job_restores jr ON jr.job_id=j.id LEFT JOIN restore_requests rr ON rr.group_id=j.group_id WHERE j.action='recover'"
            ).fetchall()
        return {row["path"]: dict(row) for row in rows}

    def _queue(self, database_path: Path, **kwargs):
        database_settings = SimpleNamespace(
            database_dsn=database_dsn(str(database_path)),
        )
        runtime = replace(config.settings, restore_tier="Bulk", restore_days=3)
        with (
            patch("app.database.settings", database_settings),
            patch("app.main.settings", runtime),
        ):
            return queue_jobs("photos", "recover", 2, 1, True, **kwargs)

    def test_directory_standard_tier_and_days_reach_every_cold_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = self._prepare_directory(Path(directory))
            database_settings = SimpleNamespace(
                database_dsn=database_dsn(str(database_path)),
            )
            runtime = replace(config.settings, restore_tier="Bulk", restore_days=3)
            with (
                patch("app.database.settings", database_settings),
                patch("app.main.settings", runtime),
            ):
                estimate = recover_estimate(
                    RecoverEstimateRequest(
                        path="photos",
                        is_directory=True,
                        restore_tier="Standard",
                        restore_days=5,
                    ),
                    {"id": 2},
                )
            result = self._queue(database_path, restore_tier="Standard", restore_days=5)
            jobs = self._jobs(database_path)

        self.assertEqual(result["item_count"], 4)
        self.assertEqual(result["restore_tier"], "Standard")
        self.assertEqual(result["restore_days"], 5)
        self.assertEqual(
            (estimate["estimate"]["tier"], estimate["estimate"]["days"]),
            (result["restore_tier"], result["restore_days"]),
            "the dialog's Cost Estimate and the queued restore must agree",
        )
        for path in ("photos/a.jpg", "photos/b.jpg", "photos/sub/c.jpg"):
            self.assertEqual(jobs[path]["restore_tier"], "Standard", path)
            self.assertEqual(jobs[path]["restore_days"], 5, path)
        # An instant-access file never restores, so it carries no tier.
        self.assertIsNone(jobs["photos/d.jpg"]["restore_tier"])
        self.assertIsNone(jobs["photos/d.jpg"]["restore_days"])

    def test_directory_bulk_tier_uses_default_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = self._prepare_directory(Path(directory))
            result = self._queue(database_path, restore_tier="Bulk")
            jobs = self._jobs(database_path)

        self.assertEqual(result["restore_tier"], "Bulk")
        self.assertEqual(result["restore_days"], 3)
        for path in ("photos/a.jpg", "photos/b.jpg", "photos/sub/c.jpg"):
            self.assertEqual(jobs[path]["restore_tier"], "Bulk", path)
            self.assertEqual(jobs[path]["restore_days"], 3, path)

    def test_directory_rejects_tier_invalid_for_archived_classes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = self._prepare_directory(Path(directory))
            for tier in ("Expedited", "Fast"):
                with self.subTest(tier=tier):
                    with self.assertRaises(HTTPException) as raised:
                        self._queue(database_path, restore_tier=tier)
                    self.assertEqual(raised.exception.status_code, 422)
            self.assertEqual(self._jobs(database_path), {})

    def test_directory_without_archived_files_echoes_no_tier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _source, database_path, _version_id = _prepare_cloud_only_version(
                root, relative_path="photos/a.jpg", payload=b"x"
            )
            result = self._queue(database_path, restore_tier="Standard")
            jobs = self._jobs(database_path)

        self.assertIsNone(result["restore_tier"])
        self.assertIsNone(result["restore_days"])
        self.assertIsNone(jobs["photos/a.jpg"]["restore_tier"])

    def test_worker_requests_the_directory_tier_from_aws(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="photos/a.jpg",
                payload=b"x",
                storage_class="DEEP_ARCHIVE",
                object_key="docs/photos/a.jpg",
            )
            self._queue(database_path, restore_tier="Standard", restore_days=5)
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
                restore_days=3,
                restore_tier="Bulk",
                restore_high_impact_gib=100,
                restore_high_impact_eur=10.0,
                restore_approval_hold_seconds=3600,
            )
            client = Mock()
            client.head_object = Mock(
                return_value={
                    "ContentLength": 1,
                    "StorageClass": "DEEP_ARCHIVE",
                    "VersionId": "s3-version-1",
                }
            )
            client.restore_object = Mock()
            client.get_object = Mock()
            with (
                patch(
                    "app.database.settings",
                    SimpleNamespace(database_dsn=database_dsn(str(database_path))),
                ),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

        client.restore_object.assert_called_once()
        request = client.restore_object.call_args.kwargs["RestoreRequest"]
        self.assertEqual(request["GlacierJobParameters"]["Tier"], "Standard")
        self.assertEqual(request["Days"], 5)
        client.get_object.assert_not_called()


GIB = 1024**3


def _worker_settings(**overrides):
    values = dict(
        operation_concurrency=1,
        restore_poll_interval=900,
        restore_days=3,
        restore_tier="Bulk",
        restore_high_impact_gib=100,
        restore_high_impact_eur=10.0,
        restore_approval_hold_seconds=3600,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class GroupRestoreApprovalTests(unittest.TestCase):
    """#533/#534: Restore Approval is one decision per recovery request."""

    COLD_FILES = 24
    FILE_GIB = 5  # 24 × 5 GiB = 120 GiB: each file is far below 100 GiB

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # ``cancel_job_group`` raises process-wide cancel flags by Job id; keep
        # them out of later tests whose fresh catalogs reuse the same ids.
        self.enterContext(patch("app.storage.cancelled_jobs", set()))
        _source, self.database_path, _version_id = _prepare_cloud_only_version(
            Path(self._tmp.name),
            relative_path="photos/p00.jpg",
            payload=b"x",
            storage_class="DEEP_ARCHIVE",
            object_key="docs/photos/p00.jpg",
        )
        for index in range(1, self.COLD_FILES):
            _add_cloud_only_file(
                self.database_path,
                relative_path=f"photos/p{index:02d}.jpg",
                storage_class="DEEP_ARCHIVE",
            )
        _add_cloud_only_file(
            self.database_path, relative_path="photos/hot.jpg", storage_class="STANDARD"
        )
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE archive_versions SET size=%s WHERE storage_class='DEEP_ARCHIVE'",
                (self.FILE_GIB * GIB,),
            )
        self.database_settings = SimpleNamespace(
            database_dsn=database_dsn(str(self.database_path)),
        )

    def _runtime(self, **overrides):
        values = dict(
            restore_tier="Bulk",
            restore_days=3,
            restore_high_impact_gib=100,
            restore_high_impact_eur=10.0,
            restore_approval_hold_seconds=3600,
        )
        values.update(overrides)
        return replace(config.settings, **values)

    def _queue_and_estimate(self, runtime):
        with (
            patch("app.database.settings", self.database_settings),
            patch("app.main.settings", runtime),
        ):
            estimate = recover_estimate(
                RecoverEstimateRequest(path="photos", is_directory=True, restore_tier="Bulk"),
                {"id": 2},
            )
            queued = queue_jobs("photos", "recover", 2, 1, True, restore_tier="Bulk")
        return estimate, queued

    def _jobs(self) -> dict[str, dict]:
        with PostgresConnection(str(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT f.path, j.status, rr.approval_deadline AS pending_until, rr.approved_at, rr.estimated_cost_eur
                FROM jobs j JOIN files f ON f.id=j.file_id LEFT JOIN job_restores jr ON jr.job_id=j.id LEFT JOIN restore_requests rr ON rr.group_id=j.group_id WHERE j.action='recover'
                """
            ).fetchall()
        return {row["path"]: dict(row) for row in rows}

    def _client(self, restore: str | None = None) -> Mock:
        client = Mock()

        def head_object(**kwargs):
            if kwargs["Key"].endswith("hot.jpg"):
                return {"ContentLength": 1, "StorageClass": "STANDARD"}
            head = {"ContentLength": self.FILE_GIB * GIB, "StorageClass": "DEEP_ARCHIVE"}
            if restore:
                head["Restore"] = restore
            return head

        client.head_object = Mock(side_effect=head_object)
        client.restore_object = Mock()
        client.get_object = Mock()
        return client

    def _run_worker(self, client: Mock, worker_settings, rounds: int = 1) -> None:
        with (
            patch("app.database.settings", self.database_settings),
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client", return_value=client),
        ):
            for _ in range(rounds):
                process_jobs_once()

    def _drain_worker(self, client: Mock, worker_settings) -> None:
        # Each pass claims a bounded batch; repeat until nothing is runnable.
        for _ in range(self.COLD_FILES + 2):
            before = client.restore_object.call_count
            self._run_worker(client, worker_settings)
            statuses = {row["status"] for row in self._jobs().values()}
            if "queued" not in statuses and client.restore_object.call_count == before:
                return

    def test_folder_of_small_files_over_the_total_threshold_waits_as_one_group(self) -> None:
        estimate, queued = self._queue_and_estimate(self._runtime())
        jobs = self._jobs()

        self.assertTrue(estimate["high_impact"], "the dialog announces the approval")
        self.assertTrue(queued["approval_required"])
        self.assertEqual(queued["item_count"], self.COLD_FILES + 1)
        self.assertEqual(len(jobs), self.COLD_FILES + 1)
        for path, job in jobs.items():
            # Every Job of the group waits, including the instant-access file.
            self.assertEqual(job["status"], "pending_approval", path)
            self.assertIsNotNone(job["pending_until"], path)
        # The group shows the whole request's Cost Estimate.
        self.assertAlmostEqual(
            float(jobs["photos/p00.jpg"]["estimated_cost_eur"]),
            float(estimate["estimate"]["estimated_cost_eur"]),
            places=6,
        )
        self.assertAlmostEqual(
            float(queued["estimated_cost_eur"]),
            float(estimate["estimate"]["estimated_cost_eur"]),
            places=6,
        )

        client = self._client()
        self._run_worker(client, _worker_settings(), rounds=3)
        client.restore_object.assert_not_called()
        client.head_object.assert_not_called()
        self.assertEqual(
            {job["status"] for job in self._jobs().values()}, {"pending_approval"}
        )

    def test_folder_under_the_total_threshold_needs_no_approval(self) -> None:
        estimate, queued = self._queue_and_estimate(self._runtime(restore_high_impact_gib=200))
        self.assertFalse(estimate["high_impact"])
        self.assertFalse(queued["approval_required"])
        self.assertEqual(
            {job["status"] for job in self._jobs().values()}, {"queued"}
        )

    def test_one_approval_releases_the_whole_group(self) -> None:
        _estimate, queued = self._queue_and_estimate(self._runtime())
        with patch("app.database.settings", self.database_settings):
            approved = approve_recover(
                RecoverApproveAction(group_id=queued["group_id"]), {"id": 1}, {"id": 2}
            )
        self.assertEqual(sorted(approved["job_ids"]), sorted(queued["job_ids"]))
        jobs = self._jobs()
        for path, job in jobs.items():
            self.assertEqual(job["status"], "queued", path)
            self.assertIsNotNone(job["approved_at"], path)

        client = self._client()
        self._drain_worker(client, _worker_settings())
        # The worker does not ask again: every archived file is restored.
        self.assertEqual(client.restore_object.call_count, self.COLD_FILES)
        statuses = {path: job["status"] for path, job in self._jobs().items()}
        self.assertNotIn("pending_approval", statuses.values())
        for index in range(self.COLD_FILES):
            self.assertEqual(statuses[f"photos/p{index:02d}.jpg"], "restoring")

    def test_lower_thresholds_never_move_restoring_jobs_back_to_approval(self) -> None:
        self._queue_and_estimate(self._runtime(restore_high_impact_gib=200))
        client = self._client()
        self._drain_worker(client, _worker_settings(restore_high_impact_gib=200))
        self.assertEqual(client.restore_object.call_count, self.COLD_FILES)
        restoring = {
            path for path, job in self._jobs().items() if job["status"] == "restoring"
        }
        self.assertEqual(len(restoring), self.COLD_FILES)

        # Settings change after AWS accepted the restores (#534): the worker
        # polls the ongoing restores and must not hold them again.
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE jobs SET updated_at='2000-01-01T00:00:00+00:00' WHERE status='restoring'"
            )
        ongoing = self._client(restore='ongoing-request="true"')
        lowered = _worker_settings(
            restore_high_impact_gib=0.000001, restore_high_impact_eur=0.000001
        )
        with patch("app.main.settings", self._runtime(
            restore_high_impact_gib=0.000001, restore_high_impact_eur=0.000001
        )):
            self._run_worker(ongoing, lowered, rounds=self.COLD_FILES + 2)

        self.assertGreater(ongoing.head_object.call_count, 0, "restores were polled")
        ongoing.restore_object.assert_not_called()
        jobs = self._jobs()
        for path in restoring:
            self.assertEqual(jobs[path]["status"], "restoring", path)
