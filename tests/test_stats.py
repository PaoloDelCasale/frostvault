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
from typing import Any
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import stats
from app.services import source_layout
import app.services.fs_preflight as fs_preflight
from app.services.fs_preflight import (
    CHECKS_SAMPLE_LIMIT,
    FINDING_COUNTS_UNKNOWN_KEY_BUDGET,
    FINDINGS_SAMPLE_LIMIT,
    FilesystemFinding,
    FilesystemPreflightResult,
    KNOWN_FINDING_COUNT_CODES,
    SYNOPSIS_MAX_STRING_CHARS,
    bound_runtime_filesystem_synopsis,
    build_stats_filesystem_payload,
    check_vault_filesystem,
    ensure_vault_filesystem_health,
    get_filesystem_health_snapshot,
    mark_vault_filesystem_health_stale,
    normalize_finding_counts,
    reset_filesystem_health_cache_for_tests,
)
from app.services.s3_preflight import PreflightCheck
from app.storage import (
    _AUDIT_REPORT_KNOWN_KEYS,
    _RUNTIME_STATUS_LIST_ITEM_BUDGET,
    _RUNTIME_STATUS_MAPPING_KEY_BUDGET,
    _RUNTIME_STATUS_MAX_DEPTH,
    _RUNTIME_STATUS_MAX_STRING_CHARS,
    _RUNTIME_STATUS_UNKNOWN_KEY_BUDGET,
    _record_scan_finding,
    runtime_status,
    snapshot_runtime_status_for_stats,
    status_lock,
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


class CountingFindings:
    """Adversarial findings source: fails if the request path walks the full set."""

    def __init__(self, total: int, *, budget: int) -> None:
        self.total = total
        self.budget = budget
        self.iterated = 0

    def __len__(self) -> int:
        return self.total

    def __iter__(self):
        for index in range(self.total):
            self.iterated += 1
            if self.iterated > self.budget:
                raise AssertionError(
                    f"unbounded scan_findings iteration ({self.iterated} > {self.budget})"
                )
            yield {
                "path": f"bad-{index}.bin",
                "code": "fs.unreadable_file",
                "message": "denied",
            }


class CountingMapping(dict):
    """Adversarial mapping: fails if more than ``budget`` items() are consumed."""

    def __init__(self, data: dict, *, budget: int) -> None:
        super().__init__(data)
        self.budget = budget
        self.iterated = 0

    def items(self):
        for key, value in super().items():
            self.iterated += 1
            if self.iterated > self.budget:
                raise AssertionError(
                    f"unbounded mapping iteration ({self.iterated} > {self.budget})"
                )
            yield key, value


class HugeKeyFilesystem(dict):
    """Filesystem-shaped mapping with a huge arbitrary key set."""

    def __init__(self, *, core: dict, junk_keys: int, budget: int) -> None:
        data = {f"junk-{index}": index for index in range(junk_keys)}
        data.update(core)
        super().__init__(data)
        self.budget = budget
        self.iterated = 0

    def items(self):
        for key, value in super().items():
            self.iterated += 1
            if self.iterated > self.budget:
                raise AssertionError(
                    f"unbounded filesystem key iteration ({self.iterated} > {self.budget})"
                )
            yield key, value

    def keys(self):
        for key, _value in self.items():
            yield key


class BoundedScanFindingsTests(unittest.TestCase):
    """Scan-time findings stay synopsis-first on the hot stats path (#228)."""

    _VAULT_IDS = (901, 902, 903, 904, 905, 906, 907, 908, 909, 910)

    def setUp(self) -> None:
        reset_filesystem_health_cache_for_tests()
        for vault_id in self._VAULT_IDS:
            runtime_status.pop(vault_id, None)

    def tearDown(self) -> None:
        reset_filesystem_health_cache_for_tests()
        for vault_id in self._VAULT_IDS:
            runtime_status.pop(vault_id, None)

    def test_record_scan_finding_stores_bounded_synopsis(self) -> None:
        vault_id = 901
        total = FINDINGS_SAMPLE_LIMIT + 80
        for index in range(total):
            _record_scan_finding(
                vault_id,
                path=f"secret-{index}.bin",
                code="fs.unreadable_file",
                message="denied",
            )
        with status_lock:
            filesystem = dict((runtime_status.get(vault_id) or {}).get("filesystem") or {})
        self.assertFalse(filesystem.get("ok"))
        self.assertEqual(filesystem.get("findings_total"), total)
        self.assertEqual(
            filesystem.get("finding_counts"),
            {"fs.unreadable_file": total},
        )
        self.assertEqual(len(filesystem.get("findings") or []), FINDINGS_SAMPLE_LIMIT)
        self.assertTrue(filesystem.get("findings_truncated"))

    def test_stats_merge_uses_synopsis_without_full_iteration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            ensure_vault_filesystem_health(
                902,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                spawn=False,
            )
            huge_total = 50_000
            adversarial = CountingFindings(
                huge_total, budget=FINDINGS_SAMPLE_LIMIT
            )
            payload = build_stats_filesystem_payload(
                vault_id=902,
                source_root=str(root),
                allowed_bases=[root],
                volume_alias="photos",
                volume_health="ok",
                local_operations_allowed=True,
                cloud_catalog_allowed=True,
                preflight_allowed=True,
                scan_findings=adversarial,
                scan_finding_counts={"fs.unreadable_file": huge_total},
                scan_findings_total=huge_total,
                spawn=False,
            )
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["findings_total"], huge_total)
            self.assertEqual(
                payload["finding_counts"].get("fs.unreadable_file"), huge_total
            )
            self.assertLessEqual(len(payload["findings"]), FINDINGS_SAMPLE_LIMIT)
            self.assertTrue(payload["findings_truncated"])
            self.assertLessEqual(adversarial.iterated, FINDINGS_SAMPLE_LIMIT)

    def test_critical_beyond_sample_fail_closed_via_synopsis(self) -> None:
        """Totals/severity stay fail-closed when the critical row is outside the sample."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            ensure_vault_filesystem_health(
                903,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                spawn=False,
            )
            # Sample is empty; only the synopsis says there are critical findings.
            payload = build_stats_filesystem_payload(
                vault_id=903,
                source_root=str(root),
                allowed_bases=[root],
                volume_alias="photos",
                volume_health="ok",
                local_operations_allowed=True,
                cloud_catalog_allowed=True,
                preflight_allowed=True,
                scan_findings=(),
                scan_finding_counts={"fs.unreadable_file": 12},
                scan_findings_total=12,
                spawn=False,
            )
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["findings_total"], 12)
            self.assertEqual(payload["finding_counts"]["fs.unreadable_file"], 12)
            self.assertEqual(payload["findings"], [])
            self.assertTrue(payload["findings_truncated"])

    def test_legacy_list_without_synopsis_is_bounded_and_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            ensure_vault_filesystem_health(
                904,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                spawn=False,
            )
            huge_total = FINDINGS_SAMPLE_LIMIT + 200
            adversarial = CountingFindings(
                huge_total, budget=FINDINGS_SAMPLE_LIMIT
            )
            payload = build_stats_filesystem_payload(
                vault_id=904,
                source_root=str(root),
                allowed_bases=[root],
                volume_alias="photos",
                volume_health="ok",
                local_operations_allowed=True,
                cloud_catalog_allowed=True,
                preflight_allowed=True,
                scan_findings=adversarial,
                spawn=False,
            )
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["findings_total"], huge_total)
            self.assertLessEqual(len(payload["findings"]), FINDINGS_SAMPLE_LIMIT)
            self.assertTrue(payload["findings_truncated"])
            self.assertIn("fs.unreadable_file", payload["finding_counts"])
            self.assertLessEqual(adversarial.iterated, FINDINGS_SAMPLE_LIMIT)

    def test_empty_and_malformed_scan_findings_are_safe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            ensure_vault_filesystem_health(
                905,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                spawn=False,
            )
            for scan_findings, counts, total in (
                (None, None, None),
                ((), {}, 0),
                ([{}], None, None),
                ([{"path": None, "code": None, "message": None}], {"": 1}, 1),
            ):
                with self.subTest(scan_findings=scan_findings, total=total):
                    payload = build_stats_filesystem_payload(
                        vault_id=905,
                        source_root=str(root),
                        allowed_bases=[root],
                        volume_alias="photos",
                        volume_health="ok",
                        local_operations_allowed=True,
                        cloud_catalog_allowed=True,
                        preflight_allowed=True,
                        scan_findings=scan_findings,
                        scan_finding_counts=counts,
                        scan_findings_total=total,
                        spawn=False,
                    )
                    self.assertIn("findings", payload)
                    self.assertIn("findings_total", payload)
                    self.assertGreaterEqual(payload["findings_total"], 0)
                    self.assertLessEqual(
                        len(payload["findings"]), FINDINGS_SAMPLE_LIMIT
                    )

    def test_zero_synopsis_with_sample_evidence_fails_closed(self) -> None:
        """Inconsistent total=0/empty counts must not ignore sample findings."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            ensure_vault_filesystem_health(
                906,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                spawn=False,
            )
            sample = [
                {
                    "path": "hidden-a.bin",
                    "code": "fs.unreadable_file",
                    "message": "denied",
                },
                {
                    "path": "hidden-b.bin",
                    "code": "fs.unreadable_file",
                    "message": "denied",
                },
                {
                    "path": "hidden-c.bin",
                    "code": "fs.symlink",
                    "message": "symlink",
                },
            ]
            for counts, total in (({}, 0), (None, 0), ({}, -3), ({"fs.x": -1}, 0)):
                with self.subTest(counts=counts, total=total):
                    payload = build_stats_filesystem_payload(
                        vault_id=906,
                        source_root=str(root),
                        allowed_bases=[root],
                        volume_alias="photos",
                        volume_health="ok",
                        local_operations_allowed=True,
                        cloud_catalog_allowed=True,
                        preflight_allowed=True,
                        scan_findings=sample,
                        scan_finding_counts=counts,
                        scan_findings_total=total,
                        spawn=False,
                    )
                    self.assertFalse(payload["ok"])
                    self.assertGreaterEqual(payload["findings_total"], len(sample))
                    self.assertGreaterEqual(
                        payload["finding_counts"].get("fs.unreadable_file", 0), 2
                    )
                    self.assertGreaterEqual(
                        payload["finding_counts"].get("fs.symlink", 0), 1
                    )
                    self.assertGreaterEqual(len(payload["findings"]), 1)
                    self.assertLessEqual(
                        len(payload["findings"]), FINDINGS_SAMPLE_LIMIT
                    )

    def test_stats_response_runtime_filesystem_is_bounded_synopsis(self) -> None:
        """GET /api/stats must not serialize an unbounded runtime findings list."""
        vault_id = 907
        with TemporaryDirectory() as directory:
            self.addCleanup(source_layout.reset_sources_root_override)
            source_layout.override_sources_root(directory)
            database_path = Path(directory) / "catalog.db"
            source = Path(directory) / "sources" / "photos"
            source.mkdir(parents=True)
            (source / "ok.txt").write_text("x", encoding="utf-8")
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _insert_vault(connection, vault_id, str(source))

            huge_total = FINDINGS_SAMPLE_LIMIT + 500
            adversarial = CountingFindings(
                huge_total, budget=FINDINGS_SAMPLE_LIMIT
            )
            # Legacy shared runtime shape: unbounded findings, no synopsis fields.
            with status_lock:
                runtime_status[vault_id] = {
                    "scanning": False,
                    "filesystem": {
                        "ok": False,
                        "uid": None,
                        "gid": None,
                        "checks": [],
                        "findings": adversarial,
                    },
                }

            ensure_vault_filesystem_health(
                vault_id,
                source_root=str(source),
                allowed_bases=[directory],
                preflight_allowed=True,
                spawn=False,
            )
            test_settings = _settings(database_path)
            with (
                patch("app.database.settings", test_settings),
                patch("app.main.settings", test_settings),
                patch(
                    "app.main.vault_local_access",
                    return_value=SimpleNamespace(
                        local_operations_allowed=True,
                        cloud_catalog_allowed=True,
                        volume_alias="photos",
                        volume_health="ok",
                    ),
                ),
            ):
                result = stats(
                    {"id": vault_id, "role": "viewer", "source_root": str(source)}
                )

            runtime_fs = result["runtime"]["filesystem"]
            self.assertIsInstance(runtime_fs, dict)
            self.assertLessEqual(len(runtime_fs.get("findings") or []), FINDINGS_SAMPLE_LIMIT)
            self.assertGreaterEqual(int(runtime_fs.get("findings_total") or 0), huge_total)
            self.assertFalse(runtime_fs.get("ok", True))
            self.assertTrue(runtime_fs.get("findings_truncated"))
            # Shared runtime must not be rewritten in place to a truncated list that
            # loses producer truth; response sanitization is a copy.
            with status_lock:
                shared = (runtime_status.get(vault_id) or {}).get("filesystem") or {}
            self.assertIs(shared.get("findings"), adversarial)
            self.assertLessEqual(adversarial.iterated, FINDINGS_SAMPLE_LIMIT)
            # Top-level filesystem payload stays independently bounded.
            self.assertLessEqual(
                len(result["filesystem"].get("findings") or []), FINDINGS_SAMPLE_LIMIT
            )
            self.assertFalse(result["filesystem"]["ok"])

    def test_normalize_finding_counts_bounds_huge_custom_mapping(self) -> None:
        """Known codes stay exact; oversized unknown keys fail closed without O(n)."""
        known_total = 17
        junk = {
            f"custom-{index}": 1
            for index in range(FINDING_COUNTS_UNKNOWN_KEY_BUDGET + 500)
        }
        junk["fs.unreadable_file"] = known_total
        budget = (
            len(KNOWN_FINDING_COUNT_CODES) + FINDING_COUNTS_UNKNOWN_KEY_BUDGET
        )
        adversarial = CountingMapping(junk, budget=budget)
        counts = normalize_finding_counts(adversarial)
        self.assertEqual(counts.get("fs.unreadable_file"), known_total)
        self.assertLessEqual(adversarial.iterated, budget)
        self.assertLessEqual(
            len(counts),
            len(KNOWN_FINDING_COUNT_CODES) + FINDING_COUNTS_UNKNOWN_KEY_BUDGET,
        )
        # Overflow mass collapses fail-closed into fs.unknown.
        self.assertGreaterEqual(counts.get("fs.unknown", 0), 1)
        self.assertGreaterEqual(sum(counts.values()), known_total + 1)

    def test_public_normalize_finding_counts_seam_is_exported(self) -> None:
        """storage must use the public normalize seam, not a private import."""
        import app.storage as storage_mod

        self.assertTrue(hasattr(fs_preflight, "normalize_finding_counts"))
        self.assertFalse(
            hasattr(storage_mod, "_normalize_finding_counts")
            and "_normalize_finding_counts"
            in getattr(storage_mod, "__dict__", {})
        )
        # Public name is the callable used by producers/tests.
        self.assertIs(
            fs_preflight.normalize_finding_counts,
            normalize_finding_counts,
        )
        src_path = Path(storage_mod.__file__ or "")
        source = src_path.read_text(encoding="utf-8")
        self.assertNotIn("_normalize_finding_counts", source)
        self.assertIn("normalize_finding_counts", source)

    def test_bound_runtime_synopsis_ignores_huge_raw_filesystem_keys(self) -> None:
        core = {
            "ok": False,
            "uid": 1000,
            "gid": 1000,
            "checks": [],
            "findings": [
                {
                    "path": "a.bin",
                    "code": "fs.unreadable_file",
                    "message": "denied",
                }
            ],
            "findings_total": 3,
            "finding_counts": {"fs.unreadable_file": 3},
            "findings_truncated": True,
        }
        # dict(raw) would walk every junk key; known-key lookup must not.
        adversarial = HugeKeyFilesystem(
            core=core,
            junk_keys=20_000,
            budget=len(KNOWN_FINDING_COUNT_CODES) + FINDING_COUNTS_UNKNOWN_KEY_BUDGET + 8,
        )
        synopsis = bound_runtime_filesystem_synopsis(adversarial)
        self.assertEqual(synopsis["findings_total"], 3)
        self.assertEqual(synopsis["finding_counts"].get("fs.unreadable_file"), 3)
        self.assertLessEqual(len(synopsis["findings"]), FINDINGS_SAMPLE_LIMIT)
        self.assertEqual(adversarial.iterated, 0)
        self.assertNotIn("junk-0", synopsis)

    def test_snapshot_under_lock_is_consistent_during_producer_mutation(self) -> None:
        vault_id = 908
        stop = threading.Event()
        errors: list[BaseException] = []

        def producer() -> None:
            try:
                while not stop.is_set():
                    with status_lock:
                        runtime_status[vault_id] = {
                            "scanning": True,
                            "last_scan": None,
                            "last_error": None,
                            "filesystem": {
                                "ok": False,
                                "uid": None,
                                "gid": None,
                                "checks": [],
                                "findings": [
                                    {
                                        "path": "hot.bin",
                                        "code": "fs.unreadable_file",
                                        "message": "denied",
                                    }
                                ],
                                "findings_total": 42,
                                "finding_counts": {"fs.unreadable_file": 42},
                                "findings_truncated": True,
                            },
                        }
                    with status_lock:
                        filesystem = (runtime_status.get(vault_id) or {}).get(
                            "filesystem"
                        )
                        if isinstance(filesystem, dict):
                            filesystem["findings_total"] = 99
                            filesystem["finding_counts"] = {
                                "fs.unreadable_file": 99
                            }
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        worker = threading.Thread(target=producer, name="stats-producer", daemon=True)
        worker.start()
        try:
            for _ in range(200):
                snap = snapshot_runtime_status_for_stats(vault_id)
                filesystem = snap.get("filesystem") or {}
                total = int(filesystem.get("findings_total") or 0)
                counted = int(
                    (filesystem.get("finding_counts") or {}).get(
                        "fs.unreadable_file", 0
                    )
                )
                # Snapshot fields for one read must agree (42/42 or 99/99).
                self.assertIn(total, {0, 42, 99})
                if total in {42, 99}:
                    self.assertEqual(counted, total)
                self.assertLessEqual(
                    len(filesystem.get("findings") or []), FINDINGS_SAMPLE_LIMIT
                )
                self.assertFalse(status_lock.locked())
        finally:
            stop.set()
            worker.join(timeout=2.0)
        self.assertEqual(errors, [])

    def test_snapshot_does_not_deadlock_with_nested_producer_lock(self) -> None:
        vault_id = 909
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": False,
                "filesystem": _empty_scan_like(),
            }
        # Consumer path must acquire/release status_lock without requiring the
        # caller to already hold it; repeated snapshots stay deadlock-free.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            snap = snapshot_runtime_status_for_stats(vault_id)
            self.assertIn("filesystem", snap)
            self.assertFalse(status_lock.locked())

    def test_valid_synopsis_parity_through_snapshot_and_merge(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            ensure_vault_filesystem_health(
                910,
                source_root=str(root),
                allowed_bases=[root],
                preflight_allowed=True,
                spawn=False,
            )
            with status_lock:
                runtime_status[910] = {
                    "scanning": False,
                    "last_scan": "2020-01-01T00:00:00+00:00",
                    "last_error": None,
                    "filesystem": {
                        "ok": False,
                        "uid": 1,
                        "gid": 2,
                        "checks": [],
                        "findings": [
                            {
                                "path": "x.bin",
                                "code": "fs.symlink",
                                "message": "symlink",
                            }
                        ],
                        "findings_total": 5,
                        "finding_counts": {"fs.symlink": 5},
                        "findings_truncated": True,
                    },
                }
            snap = snapshot_runtime_status_for_stats(910)
            filesystem = snap["filesystem"]
            payload = build_stats_filesystem_payload(
                vault_id=910,
                source_root=str(root),
                allowed_bases=[root],
                volume_alias="photos",
                volume_health="ok",
                local_operations_allowed=True,
                cloud_catalog_allowed=True,
                preflight_allowed=True,
                scan_findings=filesystem.get("findings"),
                scan_finding_counts=filesystem.get("finding_counts"),
                scan_findings_total=filesystem.get("findings_total"),
                spawn=False,
            )
            self.assertEqual(filesystem["findings_total"], 5)
            self.assertEqual(filesystem["finding_counts"]["fs.symlink"], 5)
            self.assertEqual(payload["finding_counts"].get("fs.symlink"), 5)
            self.assertGreaterEqual(payload["findings_total"], 5)
            self.assertFalse(payload["ok"])

    def test_snapshot_detaches_last_audit_report_from_producer(self) -> None:
        vault_id = 920
        report = {
            "catalog_versions": 10,
            "cloud_versions": 9,
            "missing_in_cloud": 1,
            "missing_in_catalog": 0,
            "storage_class_drift": 2,
            "policy_tag_drift": 0,
            "missing_delete_markers": 0,
        }
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": False,
                "last_scan": "2020-01-01T00:00:00+00:00",
                "last_error": None,
                "scan_id": "scan-1",
                "last_audit": "2020-01-02T00:00:00+00:00",
                "last_audit_report": report,
            }
        snap = snapshot_runtime_status_for_stats(vault_id)
        self.assertEqual(snap["scanning"], False)
        self.assertEqual(snap["last_scan"], "2020-01-01T00:00:00+00:00")
        self.assertIsNone(snap["last_error"])
        self.assertEqual(snap["scan_id"], "scan-1")
        self.assertEqual(snap["last_audit"], "2020-01-02T00:00:00+00:00")
        self.assertEqual(snap["last_audit_report"]["catalog_versions"], 10)
        self.assertEqual(snap["last_audit_report"]["missing_in_cloud"], 1)
        # Post-lock producer mutation must not alter the detached snapshot.
        report["catalog_versions"] = 999
        report["missing_in_cloud"] = 999
        self.assertEqual(snap["last_audit_report"]["catalog_versions"], 10)
        self.assertEqual(snap["last_audit_report"]["missing_in_cloud"], 1)
        self.assertIsNot(snap["last_audit_report"], report)

    def test_snapshot_bounds_huge_nested_last_audit_report(self) -> None:
        vault_id = 921
        huge = {f"junk-{i}": i for i in range(50_000)}
        for key in _AUDIT_REPORT_KNOWN_KEYS:
            huge[key] = 3
        # Nested list/mapping poison under known-looking keys must not explode.
        huge["nested_list"] = list(range(100_000))
        huge["nested_map"] = {f"k{i}": {"v": i} for i in range(5_000)}
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": True,
                "last_audit_report": huge,
            }
        snap = snapshot_runtime_status_for_stats(vault_id)
        report = snap["last_audit_report"]
        self.assertIsInstance(report, dict)
        self.assertEqual(report["catalog_versions"], 3)
        self.assertLessEqual(len(report), _RUNTIME_STATUS_MAPPING_KEY_BUDGET + 2)
        # Serialization of the detached graph stays O(budget), not O(n).
        encoded = json.dumps(snap, default=str)
        self.assertLess(len(encoded), 64_000)
        self.assertTrue(snap.get("runtime_truncated") or report.get("truncated"))

    def test_snapshot_omits_unsupported_unknown_nested_keys_fail_closed(self) -> None:
        vault_id = 922
        nested = {"deep": {"x": 1}}
        items = list(range(10_000))
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": False,
                "last_error": None,
                "custom_scalar": 7,
                "custom_text": "ok",
                "custom_nested": nested,
                "custom_list": items,
                "custom_none": None,
            }
        snap = snapshot_runtime_status_for_stats(vault_id)
        self.assertEqual(snap["custom_scalar"], 7)
        self.assertEqual(snap["custom_text"], "ok")
        self.assertIsNone(snap["custom_none"])
        self.assertNotIn("custom_nested", snap)
        self.assertNotIn("custom_list", snap)
        self.assertTrue(snap.get("runtime_truncated"))
        # Detached: mutating producer containers after unlock is irrelevant.
        nested["deep"]["x"] = 99
        items.append(123456)
        self.assertNotIn("custom_nested", snap)

    def test_snapshot_unknown_key_budget_and_overflow_marker(self) -> None:
        vault_id = 923
        payload = {"scanning": False, "last_error": None}
        for i in range(_RUNTIME_STATUS_UNKNOWN_KEY_BUDGET + 40):
            payload[f"extra-{i}"] = i
        with status_lock:
            runtime_status[vault_id] = payload
        snap = snapshot_runtime_status_for_stats(vault_id)
        extras = [k for k in snap if str(k).startswith("extra-")]
        self.assertLessEqual(len(extras), _RUNTIME_STATUS_UNKNOWN_KEY_BUDGET)
        self.assertTrue(snap.get("runtime_truncated"))

    def test_snapshot_self_referential_values_do_not_recurse(self) -> None:
        vault_id = 924
        cycle: dict = {"catalog_versions": 1}
        cycle["self"] = cycle
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": False,
                "last_audit_report": cycle,
                "loop": cycle,
            }
        snap = snapshot_runtime_status_for_stats(vault_id)
        report = snap["last_audit_report"]
        self.assertEqual(report["catalog_versions"], 1)
        # No shared cycle reference may escape the lock.
        self.assertIsNot(report, cycle)
        self.assertNotIn("self", report)
        self.assertNotIn("loop", snap)
        json.dumps(snap, default=str)  # must terminate

    def test_snapshot_scalar_known_fields_parity(self) -> None:
        vault_id = 925
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": True,
                "last_scan": "ts",
                "last_error": "boom",
                "scan_id": 42,
                "last_audit": "audit-ts",
            }
        snap = snapshot_runtime_status_for_stats(vault_id)
        self.assertIs(snap["scanning"], True)
        self.assertEqual(snap["last_scan"], "ts")
        self.assertEqual(snap["last_error"], "boom")
        self.assertEqual(snap["scan_id"], 42)
        self.assertEqual(snap["last_audit"], "audit-ts")
        self.assertIn("filesystem", snap)

    def test_snapshot_budgets_are_explicit_and_finite(self) -> None:
        self.assertGreaterEqual(_RUNTIME_STATUS_UNKNOWN_KEY_BUDGET, 1)
        self.assertGreaterEqual(_RUNTIME_STATUS_MAPPING_KEY_BUDGET, len(_AUDIT_REPORT_KNOWN_KEYS))
        self.assertGreaterEqual(_RUNTIME_STATUS_LIST_ITEM_BUDGET, 1)
        self.assertGreaterEqual(_RUNTIME_STATUS_MAX_DEPTH, 1)
        self.assertLessEqual(_RUNTIME_STATUS_MAX_DEPTH, 4)

    def test_oversized_scalar_string_sets_truthful_truncation_marker(self) -> None:
        vault_id = 930
        huge = "E" * (_RUNTIME_STATUS_MAX_STRING_CHARS + 200)
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": False,
                "last_error": huge,
                "last_scan": "S" * (_RUNTIME_STATUS_MAX_STRING_CHARS + 5),
            }
        snap = snapshot_runtime_status_for_stats(vault_id)
        self.assertEqual(len(snap["last_error"]), _RUNTIME_STATUS_MAX_STRING_CHARS)
        self.assertEqual(len(snap["last_scan"]), _RUNTIME_STATUS_MAX_STRING_CHARS)
        self.assertTrue(snap.get("runtime_truncated"))
        self.assertTrue(snap["last_error"].startswith("E"))
        json.dumps(snap, default=str)

    def test_bound_synopsis_detaches_nested_checks_and_bounds_uid_gid(self) -> None:
        nested_msg = {"inner": "live"}
        checks = [
            {
                "code": "perm.root",
                "status": "fail",
                "message": nested_msg,
                "remediation": ["do", "not", "share"],
            }
        ]
        raw = {
            "ok": False,
            "uid": "not-an-int",
            "gid": ["bad"],
            "checks": checks,
            "findings": [],
            "findings_total": 0,
            "finding_counts": {},
        }
        synopsis = bound_runtime_filesystem_synopsis(raw)
        self.assertIsNone(synopsis["uid"])
        self.assertIsNone(synopsis["gid"])
        self.assertEqual(len(synopsis["checks"]), 1)
        detached = synopsis["checks"][0]
        self.assertIsInstance(detached.get("message"), str)
        self.assertIsInstance(detached.get("remediation"), str)
        self.assertIsNot(detached, checks[0])
        # Producer mutation after detach must not leak into the synopsis.
        nested_msg["inner"] = "mutated"
        checks[0]["code"] = "mutated"
        self.assertEqual(detached["code"], "perm.root")
        self.assertNotIn("mutated", detached.get("message", ""))
        json.dumps(synopsis, default=str)

    def test_bound_synopsis_accepts_int_uid_gid_and_rejects_bool(self) -> None:
        ok = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": 1000,
                "gid": 100,
                "checks": [],
                "findings": [],
                "findings_total": 0,
                "finding_counts": {},
            }
        )
        self.assertEqual(ok["uid"], 1000)
        self.assertEqual(ok["gid"], 100)
        bad = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": True,
                "gid": False,
                "checks": [],
                "findings": [],
                "findings_total": 0,
                "finding_counts": {},
            }
        )
        self.assertIsNone(bad["uid"])
        self.assertIsNone(bad["gid"])

    def test_finding_fields_are_bounded_and_detached(self) -> None:
        huge_path = "p" * (SYNOPSIS_MAX_STRING_CHARS + 80)
        huge_code = "c" * (SYNOPSIS_MAX_STRING_CHARS + 40)
        huge_msg = "m" * (SYNOPSIS_MAX_STRING_CHARS + 40)
        mutable = {"x": 1}
        raw = {
            "ok": False,
            "uid": None,
            "gid": None,
            "checks": [],
            "findings": [
                {
                    "path": huge_path,
                    "code": huge_code,
                    "message": huge_msg,
                },
                {
                    "path": mutable,
                    "code": ["not", "a", "str"],
                    "message": {"nested": True},
                },
            ],
            "findings_total": 2,
            "finding_counts": {"fs.symlink": 2},
        }
        synopsis = bound_runtime_filesystem_synopsis(raw)
        self.assertEqual(len(synopsis["findings"]), 2)
        first, second = synopsis["findings"]
        self.assertLessEqual(len(first["path"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(first["code"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(first["message"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertIsInstance(second["path"], str)
        self.assertIsInstance(second["code"], str)
        self.assertIsInstance(second["message"], str)
        mutable["x"] = 99
        self.assertNotIn("x", second["path"])
        # Field clipping must surface synopsis truncation and fail closed.
        self.assertTrue(synopsis.get("synopsis_truncated"))
        self.assertFalse(synopsis.get("ok", True))
        json.dumps(synopsis, default=str)

    def test_bound_synopsis_checks_over_limit_marks_truncation_and_fails_closed(
        self,
    ) -> None:
        """CHECKS_SAMPLE_LIMIT + 1 must not stay ok=True without a marker."""
        checks = [
            {
                "code": f"check.{index}",
                "status": "pass",
                "message": f"ok-{index}",
            }
            for index in range(CHECKS_SAMPLE_LIMIT + 1)
        ]
        synopsis = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": 1000,
                "gid": 1000,
                "checks": checks,
                "findings": [],
                "findings_total": 0,
                "finding_counts": {},
            }
        )
        self.assertEqual(len(synopsis["checks"]), CHECKS_SAMPLE_LIMIT)
        self.assertTrue(synopsis.get("synopsis_truncated"))
        self.assertFalse(synopsis.get("ok", True))
        # findings_truncated keeps sample-vs-total semantics only.
        self.assertFalse(synopsis.get("findings_truncated"))

    def test_bound_synopsis_oversized_check_fields_mark_truncation(
        self,
    ) -> None:
        huge = "K" * (SYNOPSIS_MAX_STRING_CHARS + 64)
        synopsis = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": 1,
                "gid": 1,
                "checks": [
                    {
                        "code": huge,
                        "status": "pass",
                        "message": huge,
                        "remediation": huge,
                    }
                ],
                "findings": [],
                "findings_total": 0,
                "finding_counts": {},
            }
        )
        check = synopsis["checks"][0]
        self.assertLessEqual(len(check["code"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(check["status"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(check["message"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(check["remediation"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertTrue(synopsis.get("synopsis_truncated"))
        self.assertFalse(synopsis.get("ok", True))

    def test_bound_synopsis_oversized_finding_fields_mark_truncation(
        self,
    ) -> None:
        huge_path = "P" * (SYNOPSIS_MAX_STRING_CHARS + 10)
        huge_code = "C" * (SYNOPSIS_MAX_STRING_CHARS + 10)
        huge_msg = "M" * (SYNOPSIS_MAX_STRING_CHARS + 10)
        synopsis = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": None,
                "gid": None,
                "checks": [],
                "findings": [
                    {
                        "path": huge_path,
                        "code": huge_code,
                        "message": huge_msg,
                    }
                ],
                "findings_total": 1,
                "finding_counts": {"fs.unknown": 1},
            }
        )
        finding = synopsis["findings"][0]
        self.assertLessEqual(len(finding["path"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(finding["code"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(finding["message"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertTrue(synopsis.get("synopsis_truncated"))
        self.assertFalse(synopsis.get("ok", True))

    def test_bound_synopsis_mixed_valid_and_truncated_fields(self) -> None:
        """Valid rows stay intact; any clipped neighbor fails the synopsis closed."""
        synopsis = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": 7,
                "gid": 7,
                "checks": [
                    {
                        "code": "root.ok",
                        "status": "pass",
                        "message": "fine",
                    },
                    {
                        "code": "root.warn",
                        "status": "warn",
                        "message": "W" * (SYNOPSIS_MAX_STRING_CHARS + 3),
                    },
                ],
                "findings": [
                    {
                        "path": "ok.bin",
                        "code": "fs.symlink",
                        "message": "link",
                    },
                    {
                        "path": "big.bin",
                        "code": "fs.unreadable_file",
                        "message": "X" * (SYNOPSIS_MAX_STRING_CHARS + 9),
                    },
                ],
                "findings_total": 2,
                "finding_counts": {
                    "fs.symlink": 1,
                    "fs.unreadable_file": 1,
                },
            }
        )
        self.assertEqual(synopsis["checks"][0]["message"], "fine")
        self.assertEqual(synopsis["findings"][0]["path"], "ok.bin")
        self.assertTrue(synopsis.get("synopsis_truncated"))
        self.assertFalse(synopsis.get("ok", True))
        # Exact producer counts preserved through merge.
        self.assertEqual(synopsis["findings_total"], 2)
        self.assertEqual(synopsis["finding_counts"].get("fs.symlink"), 1)
        self.assertEqual(synopsis["finding_counts"].get("fs.unreadable_file"), 1)

    def test_bound_synopsis_ok_true_without_truncation_stays_healthy(self) -> None:
        synopsis = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": 1000,
                "gid": 100,
                "checks": [
                    {
                        "code": "root.readable",
                        "status": "pass",
                        "message": "ok",
                    }
                ],
                "findings": [],
                "findings_total": 0,
                "finding_counts": {},
            }
        )
        self.assertTrue(synopsis["ok"])
        self.assertFalse(synopsis.get("synopsis_truncated", False))
        self.assertFalse(synopsis.get("findings_truncated", False))

    def test_snapshot_propagates_synopsis_truncated_to_runtime_truncated(
        self,
    ) -> None:
        vault_id = 940
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": False,
                "last_error": None,
                "filesystem": {
                    "ok": True,
                    "uid": 1,
                    "gid": 1,
                    "checks": [
                        {
                            "code": f"c{i}",
                            "status": "pass",
                            "message": "m",
                        }
                        for i in range(CHECKS_SAMPLE_LIMIT + 1)
                    ],
                    "findings": [],
                    "findings_total": 0,
                    "finding_counts": {},
                },
            }
        snap = snapshot_runtime_status_for_stats(vault_id)
        filesystem = snap["filesystem"]
        self.assertTrue(filesystem.get("synopsis_truncated"))
        self.assertFalse(filesystem.get("ok", True))
        self.assertTrue(snap.get("runtime_truncated"))
        # Additive marker reaches the detached response graph.
        encoded = json.dumps(snap)
        self.assertIn("synopsis_truncated", encoded)
        self.assertIn("runtime_truncated", encoded)

    def test_findings_truncated_semantics_unchanged_without_field_clip(
        self,
    ) -> None:
        """Sample-vs-total still uses findings_truncated; no false synopsis clip."""
        sample = [
            {
                "path": f"f{i}.bin",
                "code": "fs.symlink",
                "message": "link",
            }
            for i in range(FINDINGS_SAMPLE_LIMIT)
        ]
        synopsis = bound_runtime_filesystem_synopsis(
            {
                "ok": False,
                "uid": None,
                "gid": None,
                "checks": [],
                "findings": sample,
                "findings_total": FINDINGS_SAMPLE_LIMIT + 10,
                "finding_counts": {"fs.symlink": FINDINGS_SAMPLE_LIMIT + 10},
            }
        )
        self.assertTrue(synopsis.get("findings_truncated"))
        self.assertEqual(len(synopsis["findings"]), FINDINGS_SAMPLE_LIMIT)
        self.assertEqual(
            synopsis["findings_total"], FINDINGS_SAMPLE_LIMIT + 10
        )
        # No field clipping occurred — synopsis_truncated stays false.
        self.assertFalse(synopsis.get("synopsis_truncated", False))
        self.assertFalse(synopsis.get("ok", True))

    def test_adversarial_mapping_key_str_is_not_invoked(self) -> None:
        class ExplodingKey:
            def __str__(self) -> str:  # pragma: no cover - must not run
                raise AssertionError("str(key) must not be called on arbitrary keys")

            def __repr__(self) -> str:
                return "ExplodingKey()"

        exploding = ExplodingKey()
        counts_raw = {exploding: 5, "fs.symlink": 3}
        counts = normalize_finding_counts(counts_raw)
        self.assertEqual(counts.get("fs.symlink"), 3)
        # Arbitrary key objects are skipped fail-closed (no str(key)).
        self.assertNotIn(exploding, counts)

        report = {"catalog_versions": 1, exploding: 9}
        with status_lock:
            runtime_status[931] = {
                "scanning": False,
                exploding: "nope",
                "last_audit_report": report,
                "safe_extra": 4,
            }
        snap = snapshot_runtime_status_for_stats(931)
        self.assertEqual(snap["last_audit_report"]["catalog_versions"], 1)
        self.assertEqual(snap.get("safe_extra"), 4)
        # No exploding key name may appear; graph stays JSON-serializable.
        for key in snap:
            self.assertNotIsInstance(key, ExplodingKey)
        for key in snap["last_audit_report"]:
            self.assertNotIsInstance(key, ExplodingKey)
        json.dumps(snap, default=str)
        self.assertTrue(snap.get("runtime_truncated") or snap["last_audit_report"].get("truncated"))

    def test_snapshot_cycle_and_json_serialization_stay_bounded(self) -> None:
        vault_id = 932
        cycle_list: list[Any] = []
        cycle_map: dict[str, Any] = {"catalog_versions": 2}
        cycle_map["loop"] = cycle_map
        cycle_list.append(cycle_list)
        with status_lock:
            runtime_status[vault_id] = {
                "scanning": False,
                "last_error": "x" * (_RUNTIME_STATUS_MAX_STRING_CHARS + 10),
                "last_audit_report": cycle_map,
                "filesystem": {
                    "ok": False,
                    "uid": object(),
                    "gid": cycle_map,
                    "checks": [{"code": cycle_map, "status": "fail", "message": cycle_list}],
                    "findings": [
                        {
                            "path": cycle_list,
                            "code": "fs.symlink",
                            "message": cycle_map,
                        }
                    ],
                    "findings_total": 1,
                    "finding_counts": {"fs.symlink": 1},
                },
                "poison": cycle_list,
            }
        snap = snapshot_runtime_status_for_stats(vault_id)
        self.assertTrue(snap.get("runtime_truncated"))
        filesystem = snap["filesystem"]
        self.assertIsNone(filesystem["uid"])
        self.assertIsNone(filesystem["gid"])
        encoded = json.dumps(snap)
        self.assertIsInstance(encoded, str)
        self.assertLess(len(encoded), 64_000)


def _empty_scan_like() -> dict:
    return {
        "ok": True,
        "uid": None,
        "gid": None,
        "checks": [],
        "findings": [],
        "findings_total": 0,
        "finding_counts": {},
        "findings_truncated": False,
        "synopsis_truncated": False,
    }


class CountingChecks:
    """Adversarial checks source: fails if the payload path walks the full set."""

    def __init__(self, total: int, *, budget: int) -> None:
        self.total = total
        self.budget = budget
        self.iterated = 0

    def __len__(self) -> int:
        return self.total

    def __iter__(self):
        for index in range(self.total):
            self.iterated += 1
            if self.iterated > self.budget:
                raise AssertionError(
                    f"unbounded checks iteration ({self.iterated} > {self.budget})"
                )
            yield {
                "code": f"check.{index}",
                "status": "pass",
                "message": f"ok-{index}",
            }


class TopLevelFilesystemPayloadBoundTests(unittest.TestCase):
    """Live preflight top-level filesystem payload shares the runtime bound contract."""

    _VAULT_IDS = (950, 951, 952, 953, 954, 955)

    def setUp(self) -> None:
        reset_filesystem_health_cache_for_tests()

    def tearDown(self) -> None:
        reset_filesystem_health_cache_for_tests()

    def _payload_with_walker(
        self,
        vault_id: int,
        root: Path,
        walker,
        *,
        scan_findings=None,
        scan_finding_counts=None,
        scan_findings_total=None,
    ) -> dict[str, Any]:
        return build_stats_filesystem_payload(
            vault_id=vault_id,
            source_root=str(root),
            allowed_bases=[root],
            volume_alias="photos",
            volume_health="ok",
            local_operations_allowed=True,
            cloud_catalog_allowed=True,
            preflight_allowed=True,
            scan_findings=scan_findings,
            scan_finding_counts=scan_finding_counts,
            scan_findings_total=scan_findings_total,
            walker=walker,
            spawn=False,
        )

    def test_live_preflight_checks_over_limit_marks_truncation_and_fails_closed(
        self,
    ) -> None:
        """CHECKS_SAMPLE_LIMIT + 1 from live preflight must not stay ok without marker."""

        def walker(path, *, allowed_bases):
            checks = tuple(
                PreflightCheck(
                    code=f"check.{index}",
                    status="pass",
                    message=f"ok-{index}",
                )
                for index in range(CHECKS_SAMPLE_LIMIT + 1)
            )
            return FilesystemPreflightResult(
                root=str(path),
                ok=True,
                uid=1000,
                gid=1000,
                checks=checks,
                findings=(),
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            payload = self._payload_with_walker(950, root, walker)

        self.assertEqual(len(payload["checks"]), CHECKS_SAMPLE_LIMIT)
        self.assertTrue(payload.get("synopsis_truncated"))
        self.assertFalse(payload.get("ok", True))
        self.assertFalse(payload.get("findings_truncated", False))

    def test_live_preflight_oversized_check_and_finding_fields_mark_truncation(
        self,
    ) -> None:
        huge = "H" * (SYNOPSIS_MAX_STRING_CHARS + 64)

        def walker(path, *, allowed_bases):
            return FilesystemPreflightResult(
                root=str(path),
                ok=True,
                uid=1,
                gid=1,
                checks=(
                    PreflightCheck(
                        code=huge,
                        status="pass",
                        message=huge,
                        remediation=huge,
                    ),
                ),
                findings=(
                    FilesystemFinding(
                        path=huge,
                        code=huge,
                        message=huge,
                    ),
                ),
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            payload = self._payload_with_walker(951, root, walker)

        check = payload["checks"][0]
        finding = payload["findings"][0]
        self.assertLessEqual(len(check["code"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(check["message"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(check["remediation"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(finding["path"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(finding["code"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertLessEqual(len(finding["message"]), SYNOPSIS_MAX_STRING_CHARS)
        self.assertTrue(payload.get("synopsis_truncated"))
        self.assertFalse(payload.get("ok", True))
        # Live totals stay accurate even when sample fields clip.
        self.assertEqual(payload["findings_total"], 1)

    def test_live_preflight_plus_scan_mixed_truncation_propagates(self) -> None:
        """Either preflight field clip or scan merge clip must fail the synopsis closed."""

        def walker(path, *, allowed_bases):
            return FilesystemPreflightResult(
                root=str(path),
                ok=True,
                uid=7,
                gid=7,
                checks=(
                    PreflightCheck(
                        code="root.ok",
                        status="pass",
                        message="fine",
                    ),
                    PreflightCheck(
                        code="root.warn",
                        status="warn",
                        message="W" * (SYNOPSIS_MAX_STRING_CHARS + 3),
                    ),
                ),
                findings=(
                    FilesystemFinding(
                        path="ok.bin",
                        code="fs.symlink",
                        message="link",
                    ),
                ),
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            payload = self._payload_with_walker(
                952,
                root,
                walker,
                scan_findings=[
                    {
                        "path": "scan.bin",
                        "code": "fs.unreadable_file",
                        "message": "X" * (SYNOPSIS_MAX_STRING_CHARS + 9),
                    }
                ],
                scan_finding_counts={"fs.unreadable_file": 1},
                scan_findings_total=1,
            )

        self.assertEqual(payload["checks"][0]["message"], "fine")
        self.assertLessEqual(
            len(payload["checks"][1]["message"]), SYNOPSIS_MAX_STRING_CHARS
        )
        self.assertEqual(payload["findings"][0]["path"], "ok.bin")
        self.assertTrue(payload.get("synopsis_truncated"))
        self.assertFalse(payload.get("ok", True))
        self.assertGreaterEqual(payload["findings_total"], 2)
        self.assertEqual(payload["finding_counts"].get("fs.symlink"), 1)
        self.assertEqual(payload["finding_counts"].get("fs.unreadable_file"), 1)

    def test_live_preflight_healthy_path_stays_ok_without_truncation(self) -> None:
        def walker(path, *, allowed_bases):
            return FilesystemPreflightResult(
                root=str(path),
                ok=True,
                uid=1000,
                gid=100,
                checks=(
                    PreflightCheck(
                        code="root.readable",
                        status="pass",
                        message="ok",
                    ),
                ),
                findings=(),
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            payload = self._payload_with_walker(953, root, walker)

        self.assertTrue(payload["ok"])
        self.assertFalse(payload.get("synopsis_truncated", False))
        self.assertFalse(payload.get("findings_truncated", False))
        self.assertEqual(len(payload["checks"]), 1)
        self.assertEqual(payload["checks"][0]["code"], "root.readable")

    def test_top_level_and_runtime_marker_parity_for_checks_over_limit(self) -> None:
        checks = [
            {
                "code": f"check.{index}",
                "status": "pass",
                "message": f"ok-{index}",
            }
            for index in range(CHECKS_SAMPLE_LIMIT + 1)
        ]
        runtime = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": 1000,
                "gid": 1000,
                "checks": checks,
                "findings": [],
                "findings_total": 0,
                "finding_counts": {},
            }
        )

        def walker(path, *, allowed_bases):
            return FilesystemPreflightResult(
                root=str(path),
                ok=True,
                uid=1000,
                gid=1000,
                checks=tuple(
                    PreflightCheck(
                        code=item["code"],
                        status=item["status"],
                        message=item["message"],
                    )
                    for item in checks
                ),
                findings=(),
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            payload = self._payload_with_walker(954, root, walker)

        self.assertEqual(len(runtime["checks"]), len(payload["checks"]))
        self.assertEqual(runtime.get("synopsis_truncated"), payload.get("synopsis_truncated"))
        self.assertEqual(runtime.get("ok"), payload.get("ok"))
        self.assertTrue(payload.get("synopsis_truncated"))
        self.assertFalse(payload.get("ok", True))

    def test_top_level_payload_does_not_consume_unbounded_checks_iterable(self) -> None:
        """Shared checks sanitizer must stop after the sample budget (+ optional probe)."""
        huge_total = CHECKS_SAMPLE_LIMIT + 500
        # Budget allows sample + one non-sized probe, or sized len() without full walk.
        adversarial = CountingChecks(
            huge_total, budget=CHECKS_SAMPLE_LIMIT + 1
        )

        # Exercise the shared seam through the runtime binder first (same helper).
        synopsis = bound_runtime_filesystem_synopsis(
            {
                "ok": True,
                "uid": 1,
                "gid": 1,
                "checks": adversarial,
                "findings": [],
                "findings_total": 0,
                "finding_counts": {},
            }
        )
        self.assertEqual(len(synopsis["checks"]), CHECKS_SAMPLE_LIMIT)
        self.assertTrue(synopsis.get("synopsis_truncated"))
        self.assertLessEqual(adversarial.iterated, CHECKS_SAMPLE_LIMIT + 1)

        # Live preflight path: walker yields many checks; payload must sample-bound.
        def walker(path, *, allowed_bases):
            return FilesystemPreflightResult(
                root=str(path),
                ok=True,
                uid=1,
                gid=1,
                checks=tuple(
                    PreflightCheck(
                        code=f"check.{index}",
                        status="pass",
                        message=f"ok-{index}",
                    )
                    for index in range(huge_total)
                ),
                findings=(),
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.txt").write_text("x", encoding="utf-8")
            payload = self._payload_with_walker(955, root, walker)

        self.assertEqual(len(payload["checks"]), CHECKS_SAMPLE_LIMIT)
        self.assertTrue(payload.get("synopsis_truncated"))
        self.assertFalse(payload.get("ok", True))
        json.dumps(payload, default=str)


if __name__ == "__main__":
    unittest.main()
