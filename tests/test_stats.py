"""Archive statistics aggregates and bounded filesystem health (issue #228).

Seams:
- ArchiveCatalog.summary — SQL aggregates, never list_file_rows
- GET /api/stats — returns summary before/without synchronous os.walk
- fs_preflight health cache — single-flight bounded synopsis
"""
from __future__ import annotations

import json
import threading
import time
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import stats
from app.services import source_layout
from app.services.fs_preflight import (
    FINDINGS_SAMPLE_LIMIT,
    check_vault_filesystem,
    ensure_vault_filesystem_health,
    reset_filesystem_health_cache_for_tests,
)
from tests.test_database import run_alembic


def _settings(database_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db_backend="sqlite",
        sqlite_path=str(database_path),
        allow_local_delete=False,
        bootstrap_vault_source_root="",
    )


def _insert_vault(connection: SQLiteConnection, vault_id: int, source_root: str) -> None:
    connection.execute(
        """
        INSERT INTO vaults(
            id, slug, name, source_root, s3_bucket, s3_prefix,
            rclone_remote
        ) VALUES (%s, %s, %s, %s, 'bucket', %s, 'remote')
        """,
        (vault_id, f"v{vault_id}", f"Vault {vault_id}", source_root, f"p{vault_id}"),
    )


class StatsTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_filesystem_health_cache_for_tests()

    def tearDown(self) -> None:
        reset_filesystem_health_cache_for_tests()

    def test_stats_read_local_and_cloud_totals_from_versioned_catalog(self) -> None:
        with TemporaryDirectory() as directory:
            self.addCleanup(source_layout.reset_sources_root_override)
            source_layout.override_sources_root("/source")
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _insert_vault(connection, 2, "/source")
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=2,
                    path="report.txt",
                    file_type="regular",
                    size=12,
                    mtime_ns=10,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=2,
                    path="report.txt",
                    object_key="docs/report.txt",
                    provider_version_id="version-1",
                    size=44,
                    storage_class="STANDARD",
                    etag="etag",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="2026-07-21T10:00:00+00:00",
                )
            test_settings = _settings(database_path)
            with (
                patch("app.database.settings", test_settings),
                patch("app.main.settings", test_settings),
            ):
                result = stats({"id": 2, "role": "viewer", "source_root": "/source"})

            self.assertEqual(result["states"], {"both": 1})
            self.assertEqual(result["storage"], {"local_bytes": 12, "cloud_bytes": 44})

    def test_summary_matches_list_file_rows_without_calling_it(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _insert_vault(connection, 3, "/source")
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=3,
                    path="local-only.bin",
                    file_type="regular",
                    size=5,
                    mtime_ns=1,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.observe_local_copy(
                    vault_id=3,
                    path="both.bin",
                    file_type="regular",
                    size=7,
                    mtime_ns=2,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=3,
                    path="both.bin",
                    object_key="p/both.bin",
                    provider_version_id="v-both",
                    size=70,
                    storage_class="STANDARD",
                    etag="etag-both",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="scan-both",
                )
                catalog.record_archive_version(
                    vault_id=3,
                    path="cloud-only.bin",
                    object_key="p/cloud-only.bin",
                    provider_version_id="v-cloud",
                    size=9,
                    storage_class="STANDARD",
                    etag="etag-cloud",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="scan-cloud",
                )
                rows = catalog.list_file_rows(3)
                expected_states: dict[str, int] = {}
                expected_local = 0
                expected_cloud = 0
                for row in rows:
                    expected_states[row["state"]] = expected_states.get(row["state"], 0) + 1
                    if row["local_exists"]:
                        expected_local += int(row["local_size"] or 0)
                    if row["cloud_exists"]:
                        expected_cloud += int(row["cloud_size"] or 0)

                original = ArchiveCatalog.list_file_rows

                def blocked(self, *args, **kwargs):
                    raise AssertionError("summary must not call list_file_rows")

                with patch.object(ArchiveCatalog, "list_file_rows", blocked):
                    summary = catalog.summary(3)

            self.assertEqual(summary["states"], expected_states)
            self.assertEqual(
                summary["storage"],
                {"local_bytes": expected_local, "cloud_bytes": expected_cloud},
            )
            # Sanity: restore still works for later tests in the process.
            self.assertIs(ArchiveCatalog.list_file_rows, original)

    def test_large_synthetic_catalog_summary_is_aggregate_and_bounded(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "large.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            n = 2500
            with SQLiteConnection(str(database_path)) as connection:
                _insert_vault(connection, 40, "/source")
                now = "2026-07-21T10:00:00+00:00"
                for index in range(n):
                    file_id = str(uuid.uuid4())
                    path = f"files/item-{index:05d}.bin"
                    connection.execute(
                        """
                        INSERT INTO vault_files(id, vault_id, status, created_at)
                        VALUES (%s, 40, 'active', %s)
                        """,
                        (file_id, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO file_paths(
                            vault_file_id, vault_id, path, valid_from, valid_to
                        ) VALUES (%s, 40, %s, %s, NULL)
                        """,
                        (file_id, path, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO local_copies(
                            vault_file_id, presence, file_type, size, mtime_ns,
                            last_seen_at, observed_at
                        ) VALUES (%s, 'present', 'regular', %s, %s, %s, %s)
                        """,
                        (file_id, 100 + (index % 7), index, now, now),
                    )
                    if index % 2 == 0:
                        version_id = str(uuid.uuid4())
                        connection.execute(
                            """
                            INSERT INTO archive_versions(
                                id, vault_file_id, vault_id, version_number,
                                object_key, provider_version_id, size,
                                storage_class, etag, discovered_at, origin,
                                integrity, availability
                            ) VALUES (
                                %s, %s, 40, 1, %s, %s, %s,
                                'STANDARD', 'etag', %s, 'upload',
                                'verified', 'available'
                            )
                            """,
                            (
                                version_id,
                                file_id,
                                f"p/{path}",
                                f"pv-{index}",
                                200 + (index % 5),
                                now,
                            ),
                        )
                query_count = {"n": 0}
                original_execute = connection.execute

                def counting_execute(sql, params=()):
                    query_count["n"] += 1
                    return original_execute(sql, params)

                connection.execute = counting_execute  # type: ignore[method-assign]
                with patch.object(
                    ArchiveCatalog,
                    "list_file_rows",
                    side_effect=AssertionError("list_file_rows forbidden"),
                ):
                    summary = ArchiveCatalog(connection).summary(40)

            self.assertEqual(summary["states"]["local_only"] + summary["states"]["both"], n)
            self.assertEqual(summary["states"]["both"], n // 2)
            self.assertEqual(summary["states"]["local_only"], n - n // 2)
            self.assertGreater(summary["storage"]["local_bytes"], 0)
            self.assertGreater(summary["storage"]["cloud_bytes"], 0)
            # One aggregate query + one active-jobs count — not O(n) statements.
            self.assertLessEqual(query_count["n"], 4)

    def test_stats_returns_summary_while_health_walk_still_running(self) -> None:
        with TemporaryDirectory() as directory:
            self.addCleanup(source_layout.reset_sources_root_override)
            source_layout.override_sources_root(directory)
            database_path = Path(directory) / "catalog.db"
            source = Path(directory) / "sources"
            source.mkdir()
            (source / "a.txt").write_text("a", encoding="utf-8")
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _insert_vault(connection, 50, str(source))
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=50,
                    path="a.txt",
                    file_type="regular",
                    size=3,
                    mtime_ns=1,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
            started = threading.Event()
            release = threading.Event()
            calls = {"n": 0}

            def slow_walk(root, *, allowed_bases):
                calls["n"] += 1
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test release not signaled")
                return check_vault_filesystem(root, allowed_bases=allowed_bases)

            test_settings = _settings(database_path)
            access = SimpleNamespace(
                local_operations_allowed=True,
                cloud_catalog_allowed=True,
                volume_alias="photos",
                volume_health="ok",
            )
            from app import main as main_module

            with (
                patch("app.main.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch.object(main_module, "vault_local_access", return_value=access),
                patch(
                    "app.services.fs_preflight.check_vault_filesystem",
                    side_effect=slow_walk,
                ),
            ):
                payload = stats(
                    {"id": 50, "role": "viewer", "source_root": str(source)}
                )
                self.assertTrue(started.wait(timeout=2))
                self.assertEqual(payload["states"], {"local_only": 1})
                self.assertEqual(payload["storage"]["local_bytes"], 3)
                self.assertEqual(payload["filesystem"]["health_status"], "checking")
                self.assertFalse(payload["filesystem"]["ok"])
                # Request finished while the walk is still blocked.
                self.assertFalse(release.is_set())
                release.set()
                # Wait for single-flight completion and a follow-up stats read.
                deadline = time.time() + 5
                final = payload
                while time.time() < deadline:
                    final = stats(
                        {"id": 50, "role": "viewer", "source_root": str(source)}
                    )
                    if final["filesystem"]["health_status"] == "current":
                        break
                    time.sleep(0.05)
            self.assertEqual(final["filesystem"]["health_status"], "current")
            self.assertEqual(calls["n"], 1)
            self.assertLessEqual(
                len(final["filesystem"]["findings"]), FINDINGS_SAMPLE_LIMIT
            )


class FilesystemHealthCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_filesystem_health_cache_for_tests()

    def tearDown(self) -> None:
        reset_filesystem_health_cache_for_tests()

    def test_single_flight_coalesces_concurrent_recomputes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            started = threading.Event()
            release = threading.Event()
            calls = {"n": 0}

            def slow_walk(path, *, allowed_bases):
                calls["n"] += 1
                started.set()
                self.assertTrue(release.wait(timeout=5))
                return check_vault_filesystem(path, allowed_bases=allowed_bases)

            def worker():
                ensure_vault_filesystem_health(
                    77,
                    source_root=str(root),
                    allowed_bases=[root],
                    preflight_allowed=True,
                    walker=slow_walk,
                    spawn=True,
                )

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.05)
            self.assertEqual(calls["n"], 1)
            release.set()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = ensure_vault_filesystem_health(
                    77,
                    source_root=str(root),
                    allowed_bases=[root],
                    preflight_allowed=True,
                    walker=slow_walk,
                    spawn=True,
                )
                if snapshot.status == "current":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "current")
            self.assertEqual(calls["n"], 1)

    def test_findings_sample_is_bounded(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(FINDINGS_SAMPLE_LIMIT + 40):
                target = root / f"real-{index}.txt"
                target.write_text("x", encoding="utf-8")
                link = root / f"link-{index}.txt"
                link.symlink_to(target)
            snapshot = ensure_vault_filesystem_health(
                88,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                spawn=False,
            )
            self.assertEqual(snapshot.status, "current")
            self.assertGreater(snapshot.findings_total, FINDINGS_SAMPLE_LIMIT)
            self.assertEqual(len(snapshot.findings_sample), FINDINGS_SAMPLE_LIMIT)
            self.assertIn("fs.symlink", snapshot.finding_counts)
            encoded = json.dumps(
                {
                    "findings": list(snapshot.findings_sample),
                    "finding_counts": snapshot.finding_counts,
                    "findings_total": snapshot.findings_total,
                }
            )
            # Sample payload stays small even when totals are large.
            self.assertLess(len(encoded), 20_000)

    def test_identity_unsafe_volume_never_walks(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def boom(*_args, **_kwargs):
                raise AssertionError("walk must not run for gated volumes")

            snapshot = ensure_vault_filesystem_health(
                99,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=False,
                walker=boom,
                spawn=True,
            )
            self.assertEqual(snapshot.status, "current")
            self.assertFalse(snapshot.ok)
            self.assertEqual(snapshot.findings_total, 0)


if __name__ == "__main__":
    unittest.main()
