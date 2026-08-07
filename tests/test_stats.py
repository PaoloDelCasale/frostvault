"""Archive statistics aggregates and bounded filesystem health (issue #228).

Seams:
- ArchiveCatalog.summary — SQL aggregates, never list_file_rows
- GET /api/stats — returns summary before/without synchronous os.walk
- fs_preflight health cache — single-flight bounded synopsis
"""
from __future__ import annotations

import json
import os
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
import app.services.fs_preflight as fs_preflight
from app.services.fs_preflight import (
    FINDINGS_SAMPLE_LIMIT,
    check_vault_filesystem,
    ensure_vault_filesystem_health,
    get_filesystem_health_snapshot,
    mark_vault_filesystem_health_stale,
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

    def test_inflight_walk_cannot_overwrite_fail_closed_gate(self) -> None:
        """A walk started while allowed must not clobber a later identity gate."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            started = threading.Event()
            release = threading.Event()
            walk_returned = threading.Event()
            worker_box: list[threading.Thread] = []

            def slow_healthy_walk(path, *, allowed_bases):
                worker_box.append(threading.current_thread())
                started.set()
                self.assertTrue(
                    release.wait(timeout=5),
                    "timed out waiting for test to release obsolete walk",
                )
                try:
                    # Healthy result that must never replace the gated synopsis.
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    # Walker returned; the same thread still runs generation-gated
                    # writeback in _run_health_recompute after this point.
                    walk_returned.set()

            first = ensure_vault_filesystem_health(
                101,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=slow_healthy_walk,
                spawn=True,
            )
            self.assertEqual(first.status, "checking")
            self.assertTrue(started.wait(timeout=2), "walk worker did not start")
            self.assertEqual(len(worker_box), 1)
            worker = worker_box[0]
            self.assertTrue(worker.is_alive(), "expected live in-flight walk worker")

            gated = ensure_vault_filesystem_health(
                101,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=False,
                walker=slow_healthy_walk,
                spawn=True,
            )
            self.assertFalse(gated.ok)
            self.assertEqual(gated.status, "current")
            gated_revision = gated.revision

            # Generation invalidation suppresses writeback while the captured
            # worker remains blocked; slot ownership stays with that worker.
            self.assertTrue(worker.is_alive())

            release.set()
            self.assertTrue(
                walk_returned.wait(timeout=5),
                "obsolete walk did not complete before writeback attempt",
            )
            worker.join(timeout=5)
            self.assertFalse(
                worker.is_alive(),
                "obsolete walk daemon worker leaked after gated invalidation",
            )

            # Observe cache directly after the obsolete worker's writeback path
            # finished. A second ensure(..., preflight_allowed=False) would mint a
            # fresh gated revision and hide whether the walk clobbered state.
            after = get_filesystem_health_snapshot(101)
            self.assertIsNotNone(after)
            assert after is not None
            self.assertFalse(after.ok)
            self.assertEqual(after.status, "current")
            # Gate revision must remain authoritative; suppressed writeback must
            # not bump past it with a healthy ok=True synopsis.
            self.assertEqual(after.revision, gated_revision)
            self.assertFalse(after.ok)

            # Fail-closed re-entry still must not walk.
            reentered = ensure_vault_filesystem_health(
                101,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=False,
                walker=lambda *_a, **_k: (_ for _ in ()).throw(
                    AssertionError("gated path must not walk")
                ),
                spawn=True,
            )
            self.assertFalse(reentered.ok)
            self.assertEqual(reentered.status, "current")

    def test_gate_then_allow_before_walker_exit_does_not_overlap(self) -> None:
        """Gate keeps the live slot; allow queues one replacement after release."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release_first = threading.Event()
            release_second = threading.Event()
            started_first = threading.Event()
            started_second = threading.Event()
            walk_starts = {"n": 0}
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            first_finished = threading.Event()

            def gated_walk(path, *, allowed_bases):
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walk_starts["n"] += 1
                    start_index = walk_starts["n"]
                try:
                    if start_index == 1:
                        started_first.set()
                        self.assertTrue(
                            release_first.wait(timeout=5),
                            "timed out waiting to release first walk",
                        )
                    else:
                        started_second.set()
                        self.assertTrue(
                            release_second.wait(timeout=5),
                            "timed out waiting to release replacement walk",
                        )
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1
                    if start_index == 1:
                        first_finished.set()

            ensure_vault_filesystem_health(
                108,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started_first.wait(timeout=2))

            gated = ensure_vault_filesystem_health(
                108,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=False,
                walker=gated_walk,
                spawn=True,
            )
            self.assertFalse(gated.ok)
            self.assertEqual(gated.status, "current")
            gated_revision = gated.revision
            self.assertFalse(started_second.is_set())
            with active_lock:
                self.assertEqual(active["n"], 1)
                self.assertEqual(active["max"], 1)
                self.assertEqual(walk_starts["n"], 1)

            # Allow resumes while the obsolete walker is still alive: must not
            # start a second concurrent walk; queue behind the live owner.
            reallowed = ensure_vault_filesystem_health(
                108,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertEqual(reallowed.status, "checking")
            self.assertFalse(started_second.is_set())
            # Gated snapshot remains until the replacement completes.
            still_gated = get_filesystem_health_snapshot(108)
            self.assertIsNotNone(still_gated)
            assert still_gated is not None
            # checking placeholder may replace cache view for the new config;
            # the prior gated ok=False current must not be clobbered by the
            # obsolete walk. Observe via revision after first release below.
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(walk_starts["n"], 1)

            release_first.set()
            self.assertTrue(
                first_finished.wait(timeout=5),
                "first walker did not finish after release",
            )
            self.assertTrue(
                started_second.wait(timeout=5),
                "replacement walk did not start after obsolete walker released slot",
            )
            with active_lock:
                self.assertEqual(active["max"], 1, "walkers overlapped after gate→allow")
                self.assertEqual(walk_starts["n"], 2)

            # While replacement runs, obsolete writeback must not restore a
            # healthy current synopsis; allow already published checking.
            mid = get_filesystem_health_snapshot(108)
            self.assertIsNotNone(mid)
            assert mid is not None
            self.assertEqual(mid.status, "checking")
            self.assertFalse(mid.ok)

            release_second.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(108)
                if snapshot is not None and snapshot.status == "current" and snapshot.ok:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "current")
            self.assertTrue(snapshot.ok)
            self.assertGreater(snapshot.revision, gated_revision)
            with active_lock:
                self.assertEqual(active["n"], 0)
                self.assertEqual(active["max"], 1)

    def test_repeated_gate_allow_churn_keeps_single_walker(self) -> None:
        """Gate/allow churn coalesces to one post-release walk and never overlaps."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release_first = threading.Event()
            release_final = threading.Event()
            started_first = threading.Event()
            started_final = threading.Event()
            walk_starts = {"n": 0}
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()

            def gated_walk(path, *, allowed_bases):
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walk_starts["n"] += 1
                    start_index = walk_starts["n"]
                try:
                    if start_index == 1:
                        started_first.set()
                        self.assertTrue(release_first.wait(timeout=5))
                    else:
                        started_final.set()
                        self.assertTrue(release_final.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                109,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started_first.wait(timeout=2))

            for _ in range(3):
                ensure_vault_filesystem_health(
                    109,
                    source_root=str(root),
                    allowed_bases=[root],
                    preflight_allowed=False,
                    walker=gated_walk,
                    spawn=True,
                )
                ensure_vault_filesystem_health(
                    109,
                    source_root=str(root),
                    allowed_bases=[root],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=True,
                )

            self.assertFalse(started_final.is_set())
            with active_lock:
                self.assertEqual(walk_starts["n"], 1)
                self.assertEqual(active["max"], 1)

            release_first.set()
            self.assertTrue(
                started_final.wait(timeout=5),
                "latest allow after churn must schedule exactly one replacement",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(walk_starts["n"], 2)

            release_final.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(109)
                if snapshot is not None and snapshot.status == "current" and snapshot.ok:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertTrue(snapshot.ok)
            with active_lock:
                self.assertEqual(active["n"], 0)
                self.assertEqual(active["max"], 1)
                self.assertEqual(walk_starts["n"], 2)

    def test_exceptional_walker_still_releases_slot_for_pending(self) -> None:
        """Walker errors must release ownership so a queued allow can run."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release_first = threading.Event()
            release_second = threading.Event()
            started_first = threading.Event()
            started_second = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            starts = {"n": 0}

            def flaky_walk(path, *, allowed_bases):
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    starts["n"] += 1
                    index = starts["n"]
                try:
                    if index == 1:
                        started_first.set()
                        self.assertTrue(release_first.wait(timeout=5))
                        raise RuntimeError("injected walker failure")
                    started_second.set()
                    self.assertTrue(release_second.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                110,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=flaky_walk,
                spawn=True,
            )
            self.assertTrue(started_first.wait(timeout=2))

            ensure_vault_filesystem_health(
                110,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=False,
                walker=flaky_walk,
                spawn=True,
            )
            ensure_vault_filesystem_health(
                110,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=flaky_walk,
                spawn=True,
            )
            self.assertFalse(started_second.is_set())

            release_first.set()
            self.assertTrue(
                started_second.wait(timeout=5),
                "pending allow must start after exceptional walker releases",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)

            release_second.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(110)
                if snapshot is not None and snapshot.status == "current" and snapshot.ok:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertTrue(snapshot.ok)
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_config_change_mid_flight_does_not_overlap_walkers(self) -> None:
        """Replacement waits for the active Vault flight; never two walkers."""
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            (root_b / "ok.txt").write_text("b", encoding="utf-8")

            release_a = threading.Event()
            release_b = threading.Event()
            started_a = threading.Event()
            started_b = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            walk_roots: list[str] = []

            def gated_walk(path, *, allowed_bases):
                root_s = str(path)
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walk_roots.append(root_s)
                try:
                    if os.path.realpath(root_s) == os.path.realpath(root_a):
                        started_a.set()
                        self.assertTrue(
                            release_a.wait(timeout=5),
                            "timed out waiting to release root A walk",
                        )
                    else:
                        started_b.set()
                        self.assertTrue(
                            release_b.wait(timeout=5),
                            "timed out waiting to release root B walk",
                        )
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            first = ensure_vault_filesystem_health(
                105,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertEqual(first.status, "checking")
            self.assertTrue(started_a.wait(timeout=2), "root A walk did not start")

            # Config change while A is still walking: request path stays nonblocking
            # and must not start a second concurrent walker.
            mid = ensure_vault_filesystem_health(
                105,
                source_root=str(root_b),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertEqual(mid.status, "checking")
            self.assertEqual(
                os.path.realpath(mid.root),
                os.path.realpath(root_b),
                "gated state must expose the new config root, not the old snapshot",
            )
            self.assertFalse(
                started_b.is_set(),
                "replacement walk must not start before the active flight finishes",
            )
            with active_lock:
                self.assertEqual(active["n"], 1)
                self.assertEqual(active["max"], 1)
                self.assertEqual(len(walk_roots), 1)

            release_a.set()
            self.assertTrue(
                started_b.wait(timeout=5),
                "replacement walk did not start after prior flight completed",
            )
            with active_lock:
                # A has exited (or is exiting) and B is the sole active walker.
                self.assertEqual(active["max"], 1, "walkers overlapped for one Vault")
                self.assertLessEqual(active["n"], 1)

            release_b.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(105)
                if snapshot is not None and snapshot.status == "current":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "current")
            self.assertEqual(
                os.path.realpath(snapshot.root),
                os.path.realpath(root_b),
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)
                self.assertEqual(len(walk_roots), 2)
                self.assertEqual(
                    os.path.realpath(walk_roots[0]), os.path.realpath(root_a)
                )
                self.assertEqual(
                    os.path.realpath(walk_roots[1]), os.path.realpath(root_b)
                )

    def test_repeated_config_churn_coalesces_to_latest_after_flight(self) -> None:
        """Mid-flight churn keeps one walker and recomputes only the latest config."""
        with TemporaryDirectory() as directory:
            roots = []
            for name in ("a", "b", "c"):
                root = Path(directory) / name
                root.mkdir()
                (root / "ok.txt").write_text(name, encoding="utf-8")
                roots.append(root)
            root_a, root_b, root_c = roots

            release_a = threading.Event()
            release_c = threading.Event()
            started_a = threading.Event()
            started_b = threading.Event()
            started_c = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            walk_roots: list[str] = []

            def gated_walk(path, *, allowed_bases):
                root_s = str(path)
                real = os.path.realpath(root_s)
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walk_roots.append(root_s)
                try:
                    if real == os.path.realpath(root_a):
                        started_a.set()
                        self.assertTrue(release_a.wait(timeout=5))
                    elif real == os.path.realpath(root_b):
                        started_b.set()
                        # B must be coalesced away; if it ever runs, fail loudly.
                        raise AssertionError("intermediate config B walk must not run")
                    else:
                        started_c.set()
                        self.assertTrue(release_c.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                106,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started_a.wait(timeout=2))

            ensure_vault_filesystem_health(
                106,
                source_root=str(root_b),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            latest = ensure_vault_filesystem_health(
                106,
                source_root=str(root_c),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertEqual(latest.status, "checking")
            self.assertEqual(
                os.path.realpath(latest.root), os.path.realpath(root_c)
            )
            self.assertFalse(started_b.is_set())
            self.assertFalse(started_c.is_set())
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(len(walk_roots), 1)

            release_a.set()
            self.assertTrue(
                started_c.wait(timeout=5),
                "latest config C must run after A completes",
            )
            self.assertFalse(
                started_b.is_set(),
                "coalesced intermediate config B must never walk",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)

            release_c.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(106)
                if snapshot is not None and snapshot.status == "current":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "current")
            self.assertEqual(
                os.path.realpath(snapshot.root), os.path.realpath(root_c)
            )
            with active_lock:
                self.assertEqual(active["n"], 0)
                self.assertEqual(active["max"], 1)
                self.assertEqual(len(walk_roots), 2)
                self.assertEqual(
                    os.path.realpath(walk_roots[0]), os.path.realpath(root_a)
                )
                self.assertEqual(
                    os.path.realpath(walk_roots[1]), os.path.realpath(root_c)
                )

    def test_config_change_then_same_config_still_recomputes_after_obsolete_flight(
        self,
    ) -> None:
        """A->B->A mid-flight must not trust the obsolete original A writeback."""
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            (root_b / "ok.txt").write_text("b", encoding="utf-8")

            release_first_a = threading.Event()
            release_second_a = threading.Event()
            started_first_a = threading.Event()
            started_second_a = threading.Event()
            a_starts = {"n": 0}
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()

            def gated_walk(path, *, allowed_bases):
                real = os.path.realpath(str(path))
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                try:
                    if real == os.path.realpath(root_a):
                        a_starts["n"] += 1
                        if a_starts["n"] == 1:
                            started_first_a.set()
                            self.assertTrue(release_first_a.wait(timeout=5))
                        else:
                            started_second_a.set()
                            self.assertTrue(release_second_a.wait(timeout=5))
                    else:
                        # B is only a transient pending config.
                        raise AssertionError("config B must be coalesced away")
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                107,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started_first_a.wait(timeout=2))

            ensure_vault_filesystem_health(
                107,
                source_root=str(root_b),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            back_to_a = ensure_vault_filesystem_health(
                107,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertEqual(back_to_a.status, "checking")
            self.assertFalse(started_second_a.is_set())

            release_first_a.set()
            self.assertTrue(
                started_second_a.wait(timeout=5),
                "returning to config A after churn must schedule a fresh walk",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)

            release_second_a.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(107)
                if snapshot is not None and snapshot.status == "current":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "current")
            self.assertEqual(
                os.path.realpath(snapshot.root), os.path.realpath(root_a)
            )
            self.assertEqual(a_starts["n"], 2)
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_spawn_false_join_timeout_does_not_start_second_walker(self) -> None:
        """Simulated join timeout while old walker lives: no overlap, queue only."""
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            (root_b / "ok.txt").write_text("b", encoding="utf-8")

            release_a = threading.Event()
            release_b = threading.Event()
            started_a = threading.Event()
            started_b = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            walk_roots: list[str] = []

            def gated_walk(path, *, allowed_bases):
                root_s = str(path)
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walk_roots.append(root_s)
                try:
                    if os.path.realpath(root_s) == os.path.realpath(root_a):
                        started_a.set()
                        self.assertTrue(release_a.wait(timeout=5))
                    else:
                        started_b.set()
                        self.assertTrue(release_b.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            first = ensure_vault_filesystem_health(
                201,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertEqual(first.status, "checking")
            self.assertTrue(started_a.wait(timeout=2))

            # Zero join budget: deterministic timeout while A is still alive.
            with patch.object(fs_preflight, "HEALTH_INLINE_JOIN_TIMEOUT_SECONDS", 0.0):
                timed_out = ensure_vault_filesystem_health(
                    201,
                    source_root=str(root_b),
                    allowed_bases=[directory],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=False,
                )

            self.assertIn(timed_out.status, {"checking", "stale"})
            self.assertEqual(
                os.path.realpath(timed_out.root), os.path.realpath(root_b)
            )
            self.assertFalse(
                started_b.is_set(),
                "inline recompute must not start while the prior walker lives",
            )
            with active_lock:
                self.assertEqual(active["n"], 1)
                self.assertEqual(active["max"], 1)
                self.assertEqual(len(walk_roots), 1)

            # Eventual queued replacement after the old owner releases.
            release_a.set()
            self.assertTrue(
                started_b.wait(timeout=5),
                "queued config B must start after prior flight releases the slot",
            )
            with active_lock:
                self.assertEqual(active["max"], 1, "walkers overlapped after timeout queue")
                self.assertLessEqual(active["n"], 1)

            release_b.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(201)
                if snapshot is not None and snapshot.status == "current":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "current")
            self.assertEqual(
                os.path.realpath(snapshot.root), os.path.realpath(root_b)
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)
                self.assertEqual(len(walk_roots), 2)

    def test_spawn_false_join_timeout_old_worker_exception_still_queues(
        self,
    ) -> None:
        """Exceptional prior walker still releases; timeout-queued config runs."""
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            (root_b / "ok.txt").write_text("b", encoding="utf-8")

            release_a = threading.Event()
            release_b = threading.Event()
            started_a = threading.Event()
            started_b = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()

            def flaky_walk(path, *, allowed_bases):
                root_s = str(path)
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                try:
                    if os.path.realpath(root_s) == os.path.realpath(root_a):
                        started_a.set()
                        self.assertTrue(release_a.wait(timeout=5))
                        raise RuntimeError("injected walker failure")
                    started_b.set()
                    self.assertTrue(release_b.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                202,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=flaky_walk,
                spawn=True,
            )
            self.assertTrue(started_a.wait(timeout=2))

            with patch.object(fs_preflight, "HEALTH_INLINE_JOIN_TIMEOUT_SECONDS", 0.0):
                timed_out = ensure_vault_filesystem_health(
                    202,
                    source_root=str(root_b),
                    allowed_bases=[directory],
                    preflight_allowed=True,
                    walker=flaky_walk,
                    spawn=False,
                )
            self.assertIn(timed_out.status, {"checking", "stale"})
            self.assertFalse(started_b.is_set())

            release_a.set()
            self.assertTrue(
                started_b.wait(timeout=5),
                "pending B must run after exceptional A releases ownership",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)

            release_b.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(202)
                if snapshot is not None and snapshot.status == "current":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "current")
            self.assertEqual(
                os.path.realpath(snapshot.root), os.path.realpath(root_b)
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_spawn_false_after_slot_release_returns_recomputed_snapshot(self) -> None:
        """spawn=False waits for ownership then returns the inline recompute."""
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            (root_b / "ok.txt").write_text("b", encoding="utf-8")

            release_a = threading.Event()
            started_a = threading.Event()
            started_b = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()

            def gated_walk(path, *, allowed_bases):
                root_s = str(path)
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                try:
                    if os.path.realpath(root_s) == os.path.realpath(root_a):
                        started_a.set()
                        self.assertTrue(release_a.wait(timeout=5))
                    else:
                        started_b.set()
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                203,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started_a.wait(timeout=2))

            result_holder: dict[str, object] = {}

            def sync_caller() -> None:
                result_holder["snap"] = ensure_vault_filesystem_health(
                    203,
                    source_root=str(root_b),
                    allowed_bases=[directory],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=False,
                )

            caller = threading.Thread(target=sync_caller, name="spawn-false-waiter")
            caller.start()
            # Allow the waiter to reach join before releasing A.
            deadline = time.time() + 2
            while time.time() < deadline and not started_b.is_set():
                # Release shortly after the waiter is almost certainly blocked in join.
                if caller.is_alive():
                    time.sleep(0.05)
                    release_a.set()
                    break
                time.sleep(0.01)
            else:
                release_a.set()

            caller.join(timeout=5)
            self.assertFalse(caller.is_alive(), "spawn=False caller did not finish")
            self.assertTrue(started_b.is_set(), "inline B walk did not run after release")
            snap = result_holder.get("snap")
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertEqual(snap.status, "current")  # type: ignore[attr-defined]
            self.assertEqual(
                os.path.realpath(snap.root),  # type: ignore[attr-defined]
                os.path.realpath(root_b),
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_spawn_false_join_timeout_survives_gate_churn(self) -> None:
        """Gate churn during a timed-out spawn=False wait must not overlap walkers."""
        with TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            root.mkdir()
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release_first = threading.Event()
            release_final = threading.Event()
            started_first = threading.Event()
            started_final = threading.Event()
            phase = {"n": 0}
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()

            def gated_walk(path, *, allowed_bases):
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    phase["n"] += 1
                    my_phase = phase["n"]
                try:
                    if my_phase == 1:
                        started_first.set()
                        self.assertTrue(release_first.wait(timeout=5))
                    else:
                        started_final.set()
                        self.assertTrue(release_final.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                204,
                source_root=str(root),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started_first.wait(timeout=2))

            # Fail-closed while first walker is alive.
            gated = ensure_vault_filesystem_health(
                204,
                source_root=str(root),
                allowed_bases=[directory],
                preflight_allowed=False,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(gated.ok is False or gated.status == "current")

            with patch.object(fs_preflight, "HEALTH_INLINE_JOIN_TIMEOUT_SECONDS", 0.0):
                timed_out = ensure_vault_filesystem_health(
                    204,
                    source_root=str(root),
                    allowed_bases=[directory],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=False,
                )
            self.assertIn(timed_out.status, {"checking", "stale"})
            self.assertFalse(started_final.is_set())
            with active_lock:
                self.assertEqual(active["n"], 1)
                self.assertEqual(active["max"], 1)

            release_first.set()
            self.assertTrue(
                started_final.wait(timeout=5),
                "post-gate allow queued by spawn=False timeout must eventually run",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)

            release_final.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(204)
                if snapshot is not None and snapshot.status == "current" and snapshot.ok:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertTrue(snapshot.ok)
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_spawn_false_same_config_waits_and_returns_current(self) -> None:
        """spawn=False + same-config inflight waits and returns the owner's result."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release = threading.Event()
            started = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            walks = {"n": 0}

            def gated_walk(path, *, allowed_bases):
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walks["n"] += 1
                try:
                    started.set()
                    self.assertTrue(release.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                220,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started.wait(timeout=2))

            result_holder: dict[str, object] = {}

            def sync_caller() -> None:
                result_holder["snap"] = ensure_vault_filesystem_health(
                    220,
                    source_root=str(root),
                    allowed_bases=[root],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=False,
                )

            caller = threading.Thread(
                target=sync_caller, name="same-config-spawn-false-waiter"
            )
            caller.start()
            # Waiter must block on the live same-config owner, not return early.
            deadline = time.time() + 1.0
            while time.time() < deadline and caller.is_alive():
                time.sleep(0.01)
            self.assertTrue(caller.is_alive(), "spawn=False same-config returned without waiting")

            release.set()
            caller.join(timeout=5)
            self.assertFalse(caller.is_alive())
            snap = result_holder.get("snap")
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertEqual(snap.status, "current")  # type: ignore[attr-defined]
            self.assertTrue(snap.ok)  # type: ignore[attr-defined]
            with active_lock:
                self.assertEqual(walks["n"], 1, "same-config wait must not start a second walk")
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_spawn_false_same_config_timeout_returns_bounded_no_overlap(self) -> None:
        """spawn=False same-config join timeout returns bounded state without overlap."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release = threading.Event()
            started = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            walks = {"n": 0}

            def gated_walk(path, *, allowed_bases):
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walks["n"] += 1
                try:
                    started.set()
                    self.assertTrue(release.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            first = ensure_vault_filesystem_health(
                221,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertEqual(first.status, "checking")
            self.assertTrue(started.wait(timeout=2))

            with patch.object(fs_preflight, "HEALTH_INLINE_JOIN_TIMEOUT_SECONDS", 0.0):
                timed_out = ensure_vault_filesystem_health(
                    221,
                    source_root=str(root),
                    allowed_bases=[root],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=False,
                )

            self.assertIn(timed_out.status, {"checking", "stale"})
            with active_lock:
                self.assertEqual(walks["n"], 1)
                self.assertEqual(active["n"], 1)
                self.assertEqual(active["max"], 1)

            release.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(221)
                if snapshot is not None and snapshot.status == "current":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "current")
            with active_lock:
                self.assertEqual(
                    walks["n"],
                    1,
                    "same-config timeout must not queue a redundant second walk",
                )
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_spawn_false_same_config_owner_exception_returns_failed(self) -> None:
        """spawn=False same-config wait surfaces the owner's failed synopsis."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release = threading.Event()
            started = threading.Event()
            walks = {"n": 0}

            def flaky_walk(path, *, allowed_bases):
                walks["n"] += 1
                started.set()
                self.assertTrue(release.wait(timeout=5))
                raise RuntimeError("same-config owner boom")

            ensure_vault_filesystem_health(
                222,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=flaky_walk,
                spawn=True,
            )
            self.assertTrue(started.wait(timeout=2))

            result_holder: dict[str, object] = {}

            def sync_caller() -> None:
                result_holder["snap"] = ensure_vault_filesystem_health(
                    222,
                    source_root=str(root),
                    allowed_bases=[root],
                    preflight_allowed=True,
                    walker=flaky_walk,
                    spawn=False,
                )

            caller = threading.Thread(target=sync_caller, name="same-config-fail-waiter")
            caller.start()
            deadline = time.time() + 1.0
            while time.time() < deadline and caller.is_alive():
                time.sleep(0.01)
            self.assertTrue(caller.is_alive())

            release.set()
            caller.join(timeout=5)
            self.assertFalse(caller.is_alive())
            snap = result_holder.get("snap")
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertEqual(snap.status, "failed")  # type: ignore[attr-defined]
            self.assertEqual(walks["n"], 1)

    def test_spawn_false_same_config_completion_race_before_wait(self) -> None:
        """If the owner finishes before spawn=False joins, return completed result."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release = threading.Event()
            started = threading.Event()
            finished = threading.Event()
            walks = {"n": 0}

            def gated_walk(path, *, allowed_bases):
                walks["n"] += 1
                started.set()
                self.assertTrue(release.wait(timeout=5))
                try:
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    finished.set()

            ensure_vault_filesystem_health(
                223,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started.wait(timeout=2))
            release.set()
            self.assertTrue(finished.wait(timeout=5))

            # Owner already done; spawn=False must observe the completed synopsis
            # without starting another walk (fresh TTL hit or post-join read).
            snap = ensure_vault_filesystem_health(
                223,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=False,
            )
            self.assertEqual(snap.status, "current")
            self.assertTrue(snap.ok)
            self.assertEqual(walks["n"], 1)

    def test_spawn_false_same_config_different_config_during_wait(self) -> None:
        """Different-config churn while waiting must not overlap walkers."""
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            (root_b / "ok.txt").write_text("b", encoding="utf-8")

            release_a = threading.Event()
            release_b = threading.Event()
            started_a = threading.Event()
            started_b = threading.Event()
            waiter_entered = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            walk_roots: list[str] = []

            def gated_walk(path, *, allowed_bases):
                root_s = str(path)
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walk_roots.append(root_s)
                try:
                    if os.path.realpath(root_s) == os.path.realpath(root_a):
                        started_a.set()
                        self.assertTrue(release_a.wait(timeout=5))
                    else:
                        started_b.set()
                        self.assertTrue(release_b.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            ensure_vault_filesystem_health(
                224,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertTrue(started_a.wait(timeout=2))

            result_holder: dict[str, object] = {}

            def sync_caller() -> None:
                waiter_entered.set()
                result_holder["snap"] = ensure_vault_filesystem_health(
                    224,
                    source_root=str(root_a),
                    allowed_bases=[directory],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=False,
                )

            caller = threading.Thread(
                target=sync_caller, name="same-config-churn-waiter"
            )
            caller.start()
            self.assertTrue(waiter_entered.wait(timeout=2))
            # Give the waiter time to reach the join before churn.
            time.sleep(0.05)

            queued = ensure_vault_filesystem_health(
                224,
                source_root=str(root_b),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertIn(queued.status, {"checking", "stale"})
            self.assertFalse(started_b.is_set())

            release_a.set()
            caller.join(timeout=5)
            self.assertFalse(caller.is_alive())
            snap = result_holder.get("snap")
            self.assertIsNotNone(snap)
            assert snap is not None
            # Suppressed A writeback and/or B promotion may yield bounded or B's result;
            # the critical invariant is no walker overlap and a defined snapshot.
            self.assertIn(
                snap.status,  # type: ignore[attr-defined]
                {"checking", "stale", "current", "failed"},
            )

            self.assertTrue(
                started_b.wait(timeout=5),
                "queued config B must run after A releases",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)

            release_b.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(224)
                if (
                    snapshot is not None
                    and snapshot.status == "current"
                    and os.path.realpath(snapshot.root) == os.path.realpath(root_b)
                ):
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(
                os.path.realpath(snapshot.root), os.path.realpath(root_b)
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_inline_flight_blocks_concurrent_spawn_same_config(self) -> None:
        """Reserved inline ownership: concurrent spawn=True shares; max walkers=1."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")

            release_inline = threading.Event()
            started_inline = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()
            walks = {"n": 0}

            def gated_walk(path, *, allowed_bases):
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                    walks["n"] += 1
                try:
                    started_inline.set()
                    self.assertTrue(release_inline.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            result_holder: dict[str, object] = {}

            def inline_caller() -> None:
                result_holder["snap"] = ensure_vault_filesystem_health(
                    210,
                    source_root=str(root),
                    allowed_bases=[root],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=False,
                )

            caller = threading.Thread(target=inline_caller, name="inline-owner")
            caller.start()
            self.assertTrue(started_inline.wait(timeout=2))

            concurrent = ensure_vault_filesystem_health(
                210,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertIn(concurrent.status, {"checking", "stale"})
            with active_lock:
                self.assertEqual(active["n"], 1)
                self.assertEqual(active["max"], 1)
                self.assertEqual(walks["n"], 1)

            release_inline.set()
            caller.join(timeout=5)
            self.assertFalse(caller.is_alive())
            snap = result_holder.get("snap")
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertEqual(snap.status, "current")  # type: ignore[attr-defined]
            self.assertTrue(snap.ok)  # type: ignore[attr-defined]
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)
                self.assertEqual(walks["n"], 1)

    def test_inline_flight_queues_concurrent_spawn_other_config(self) -> None:
        """Concurrent spawn=True with new config queues; never overlaps inline."""
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            (root_b / "ok.txt").write_text("b", encoding="utf-8")

            release_inline = threading.Event()
            release_b = threading.Event()
            started_inline = threading.Event()
            started_b = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()

            def gated_walk(path, *, allowed_bases):
                root_s = str(path)
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                try:
                    if os.path.realpath(root_s) == os.path.realpath(root_a):
                        started_inline.set()
                        self.assertTrue(release_inline.wait(timeout=5))
                    else:
                        started_b.set()
                        self.assertTrue(release_b.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            result_holder: dict[str, object] = {}

            def inline_caller() -> None:
                result_holder["snap"] = ensure_vault_filesystem_health(
                    211,
                    source_root=str(root_a),
                    allowed_bases=[directory],
                    preflight_allowed=True,
                    walker=gated_walk,
                    spawn=False,
                )

            caller = threading.Thread(target=inline_caller, name="inline-owner-a")
            caller.start()
            self.assertTrue(started_inline.wait(timeout=2))

            queued = ensure_vault_filesystem_health(
                211,
                source_root=str(root_b),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=gated_walk,
                spawn=True,
            )
            self.assertEqual(queued.status, "checking")
            self.assertFalse(started_b.is_set())
            with active_lock:
                self.assertEqual(active["n"], 1)
                self.assertEqual(active["max"], 1)

            release_inline.set()
            caller.join(timeout=5)
            self.assertFalse(caller.is_alive())

            self.assertTrue(
                started_b.wait(timeout=5),
                "queued config B must start after inline A releases ownership",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertLessEqual(active["n"], 1)

            release_b.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(211)
                if (
                    snapshot is not None
                    and snapshot.status == "current"
                    and os.path.realpath(snapshot.root) == os.path.realpath(root_b)
                ):
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(
                os.path.realpath(snapshot.root), os.path.realpath(root_b)
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_inline_exception_releases_slot_and_promotes_pending(self) -> None:
        """Inline walker failure still releases ownership so pending can run."""
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            (root_b / "ok.txt").write_text("b", encoding="utf-8")

            release_inline = threading.Event()
            release_b = threading.Event()
            started_inline = threading.Event()
            started_b = threading.Event()
            active = {"n": 0, "max": 0}
            active_lock = threading.Lock()

            def flaky_walk(path, *, allowed_bases):
                root_s = str(path)
                with active_lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                try:
                    if os.path.realpath(root_s) == os.path.realpath(root_a):
                        started_inline.set()
                        self.assertTrue(release_inline.wait(timeout=5))
                        raise RuntimeError("inline walker boom")
                    started_b.set()
                    self.assertTrue(release_b.wait(timeout=5))
                    return check_vault_filesystem(path, allowed_bases=allowed_bases)
                finally:
                    with active_lock:
                        active["n"] -= 1

            result_holder: dict[str, object] = {}

            def inline_caller() -> None:
                result_holder["snap"] = ensure_vault_filesystem_health(
                    212,
                    source_root=str(root_a),
                    allowed_bases=[directory],
                    preflight_allowed=True,
                    walker=flaky_walk,
                    spawn=False,
                )

            caller = threading.Thread(target=inline_caller, name="inline-boom")
            caller.start()
            self.assertTrue(started_inline.wait(timeout=2))

            ensure_vault_filesystem_health(
                212,
                source_root=str(root_b),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=flaky_walk,
                spawn=True,
            )
            self.assertFalse(started_b.is_set())

            release_inline.set()
            caller.join(timeout=5)
            self.assertFalse(caller.is_alive())
            snap = result_holder.get("snap")
            self.assertIsNotNone(snap)
            assert snap is not None
            # Failed inline may be superseded by queued B; either failed A or
            # checking/current B is acceptable as long as ownership released.
            self.assertIn(
                snap.status,  # type: ignore[attr-defined]
                {"failed", "checking", "current", "stale"},
            )

            self.assertTrue(
                started_b.wait(timeout=5),
                "pending B must run after exceptional inline release",
            )
            with active_lock:
                self.assertEqual(active["max"], 1)

            release_b.set()
            deadline = time.time() + 5
            snapshot = None
            while time.time() < deadline:
                snapshot = get_filesystem_health_snapshot(212)
                if (
                    snapshot is not None
                    and snapshot.status == "current"
                    and os.path.realpath(snapshot.root) == os.path.realpath(root_b)
                ):
                    break
                time.sleep(0.01)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(
                os.path.realpath(snapshot.root), os.path.realpath(root_b)
            )
            with active_lock:
                self.assertEqual(active["max"], 1)
                self.assertEqual(active["n"], 0)

    def test_source_root_change_invalidates_fresh_cache(self) -> None:
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "a"
            root_b = Path(directory) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "ok.txt").write_text("a", encoding="utf-8")
            # Make B unhealthy so a reused A-cache would be wrong.
            (root_b / "link.txt").symlink_to(root_b / "missing.txt")
            calls: list[str] = []

            def tracking_walk(path, *, allowed_bases):
                calls.append(str(path))
                return check_vault_filesystem(path, allowed_bases=allowed_bases)

            first = ensure_vault_filesystem_health(
                102,
                source_root=str(root_a),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=tracking_walk,
                spawn=False,
            )
            self.assertTrue(first.ok)
            self.assertEqual(len(calls), 1)

            second = ensure_vault_filesystem_health(
                102,
                source_root=str(root_b),
                allowed_bases=[directory],
                preflight_allowed=True,
                walker=tracking_walk,
                spawn=False,
            )
            self.assertEqual(len(calls), 2)
            self.assertFalse(second.ok)
            self.assertIn("fs.symlink", second.finding_counts)
            self.assertEqual(os.path.realpath(second.root), os.path.realpath(root_b))

    def test_allowed_bases_change_invalidates_fresh_cache(self) -> None:
        with TemporaryDirectory() as directory:
            allowed = Path(directory) / "allowed"
            other = Path(directory) / "other"
            allowed.mkdir()
            other.mkdir()
            vault = allowed / "vault"
            vault.mkdir()
            (vault / "ok.txt").write_text("x", encoding="utf-8")
            calls = {"n": 0}

            def tracking_walk(path, *, allowed_bases):
                calls["n"] += 1
                return check_vault_filesystem(path, allowed_bases=allowed_bases)

            first = ensure_vault_filesystem_health(
                103,
                source_root=str(vault),
                allowed_bases=[allowed],
                preflight_allowed=True,
                walker=tracking_walk,
                spawn=False,
            )
            self.assertTrue(first.ok)
            self.assertEqual(calls["n"], 1)

            # Same source_root string, but bases no longer authorize it → recompute.
            second = ensure_vault_filesystem_health(
                103,
                source_root=str(vault),
                allowed_bases=[other],
                preflight_allowed=True,
                walker=tracking_walk,
                spawn=False,
            )
            self.assertEqual(calls["n"], 2)
            self.assertFalse(second.ok)

    def test_explicit_stale_marker_forces_recompute(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            calls = {"n": 0}

            def tracking_walk(path, *, allowed_bases):
                calls["n"] += 1
                return check_vault_filesystem(path, allowed_bases=allowed_bases)

            first = ensure_vault_filesystem_health(
                104,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=tracking_walk,
                spawn=False,
            )
            self.assertTrue(first.ok)
            self.assertEqual(calls["n"], 1)

            # Fresh TTL hit would reuse without walk.
            reused = ensure_vault_filesystem_health(
                104,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=tracking_walk,
                spawn=False,
            )
            self.assertEqual(calls["n"], 1)
            self.assertEqual(reused.revision, first.revision)

            mark_vault_filesystem_health_stale(104)
            refreshed = ensure_vault_filesystem_health(
                104,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                walker=tracking_walk,
                spawn=False,
            )
            self.assertEqual(calls["n"], 2)
            self.assertGreater(refreshed.revision, first.revision)


if __name__ == "__main__":
    unittest.main()
