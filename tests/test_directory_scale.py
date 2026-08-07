"""Directory listing scale + durable aggregates (issue #229)."""
from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import build_directory_items, list_files
from app.services import directory_aggregates as aggregates
from app.services import metrics as metrics_service
from app.services.lifecycle_pins import set_lifecycle_pin
from tests.test_database import run_alembic


def _seed_vault(connection: SQLiteConnection, vault_id: int = 2) -> None:
    connection.execute(
        """
        INSERT INTO vaults(
            id, slug, name, source_root, s3_bucket, s3_prefix,
            rclone_remote
        ) VALUES (%s, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
        """,
        (vault_id,),
    )


def _bulk_local_files(
    connection: SQLiteConnection,
    *,
    vault_id: int,
    paths_and_sizes: list[tuple[str, int]],
    observed_at: str = "2026-07-21T10:00:00+00:00",
) -> None:
    """Insert many active local-only files with minimal Python overhead."""
    raw = connection.connection
    file_rows = []
    path_rows = []
    local_rows = []
    for path, size in paths_and_sizes:
        file_id = str(uuid.uuid4())
        file_rows.append((file_id, vault_id, "active", observed_at))
        path_rows.append((file_id, vault_id, path, observed_at))
        local_rows.append((file_id, "present", "regular", size, 1, observed_at, observed_at))
    raw.executemany(
        """
        INSERT INTO vault_files(id, vault_id, status, created_at)
        VALUES (?, ?, ?, ?)
        """,
        file_rows,
    )
    raw.executemany(
        """
        INSERT INTO file_paths(
            vault_file_id, vault_id, path, valid_from, valid_to
        ) VALUES (?, ?, ?, ?, NULL)
        """,
        path_rows,
    )
    raw.executemany(
        """
        INSERT INTO local_copies(
            vault_file_id, presence, file_type, size, mtime_ns,
            plaintext_sha256, matched_archive_version_id,
            last_seen_at, observed_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        """,
        local_rows,
    )


class DirectoryScaleTests(unittest.TestCase):
    def setUp(self) -> None:
        metrics_service.reset_for_tests()
        # Request-path scheduling must not spawn background rebuild threads in
        # unit tests (they race the shared SQLite fixture connection).
        aggregates.set_maintenance_scheduling_enabled(False)
        self._tmp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._tmp.name) / "catalog.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connection = SQLiteConnection(str(self.database_path))
        self.connection.__enter__()
        _seed_vault(self.connection, 2)
        self.catalog = ArchiveCatalog(self.connection)

    def tearDown(self) -> None:
        self.connection.__exit__(None, None, None)
        self._tmp.cleanup()
        aggregates.set_maintenance_scheduling_enabled(True)

    def test_migration_creates_directory_aggregate_tables(self) -> None:
        tables = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("directory_aggregates", tables)
        self.assertIn("directory_aggregate_status", tables)
        self.assertIn("directory_aggregate_dirty", tables)

    def test_list_files_page_matches_build_directory_items_semantics(self) -> None:
        catalog = self.catalog
        catalog.observe_local_copy(
            vault_id=2,
            path="photos/small.jpg",
            file_type="regular",
            size=1_000_000,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        catalog.observe_local_copy(
            vault_id=2,
            path="photos/large.jpg",
            file_type="regular",
            size=2_000_000,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        catalog.record_archive_version(
            vault_id=2,
            path="photos/large.jpg",
            object_key="docs/photos/large.jpg",
            provider_version_id="v-large",
            size=2_000_000,
            storage_class="STANDARD",
            etag="e1",
            uploaded_at="2026-07-21T10:00:00+00:00",
            observed_at="2026-07-21T10:00:00+00:00",
            scan_id="scan-1",
        )
        catalog.observe_local_copy(
            vault_id=2,
            path="documents/invoice.pdf",
            file_type="regular",
            size=500,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        catalog.observe_local_copy(
            vault_id=2,
            path="readme.txt",
            file_type="regular",
            size=12,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)

        rows = catalog.list_file_rows(2)
        expected = build_directory_items(rows, "")
        page = catalog.list_files_page(2, page=1, page_size=100)
        self.assertEqual(page["mode"], "browse")
        self.assertEqual(page["total"], len(expected))
        self.assertEqual(
            [(item["type"], item["name"]) for item in page["items"]],
            [(item["type"], item["name"]) for item in expected],
        )
        photos = next(item for item in page["items"] if item["name"] == "photos")
        expected_photos = next(item for item in expected if item["name"] == "photos")
        for key in (
            "item_count",
            "total_size",
            "local_size",
            "cloud_size",
            "state",
            "state_counts",
            "available_actions",
            "storage_class",
            "storage_class_count",
            "lifecycle_pinned",
            "lifecycle_pinned_partial",
        ):
            self.assertEqual(photos[key], expected_photos[key], key)

    def test_root_listing_does_not_call_list_file_rows(self) -> None:
        catalog = self.catalog
        catalog.observe_local_copy(
            vault_id=2,
            path="a/one.bin",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)

        def blocked(*_args, **_kwargs):
            raise AssertionError("list_file_rows must not be used for directory pages")

        with patch.object(ArchiveCatalog, "list_file_rows", blocked):
            page = catalog.list_files_page(2, page=1, page_size=50)
        self.assertEqual(page["total"], 1)
        self.assertEqual(catalog.last_listing_rows_materialized, 1)

    def test_pagination_bounds_materialized_rows(self) -> None:
        paths = [(f"bucket/file-{index:04d}.bin", 10) for index in range(250)]
        paths.extend((f"other/file-{index:04d}.bin", 5) for index in range(20))
        _bulk_local_files(self.connection, vault_id=2, paths_and_sizes=paths)
        aggregates.rebuild_vault_directory_aggregates(self.connection, 2)

        page = self.catalog.list_files_page(2, directory="bucket", page=2, page_size=50)
        self.assertEqual(page["total"], 250)
        self.assertEqual(len(page["items"]), 50)
        self.assertEqual(self.catalog.last_listing_rows_materialized, 50)
        self.assertTrue(all(item["type"] == "file" for item in page["items"]))

    def test_search_and_state_filter_are_server_side_paged(self) -> None:
        _bulk_local_files(
            self.connection,
            vault_id=2,
            paths_and_sizes=[
                ("clips/keep-me.bin", 1),
                ("clips/skip-me.bin", 1),
                ("recordings/keep-me.bin", 1),
            ],
        )
        # Make one cloud_only file for state filtering.
        self.catalog.record_archive_version(
            vault_id=2,
            path="clips/cloud-only.bin",
            object_key="docs/clips/cloud-only.bin",
            provider_version_id="v-cloud",
            size=9,
            storage_class="STANDARD",
            etag="e",
            uploaded_at="2026-07-21T10:00:00+00:00",
            observed_at="2026-07-21T10:00:00+00:00",
            scan_id="scan-1",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)

        search = self.catalog.list_files_page(
            2, search="keep-me", page=1, page_size=10
        )
        self.assertEqual(search["mode"], "search")
        self.assertEqual(search["total"], 2)
        self.assertEqual(len(search["items"]), 2)

        filtered = self.catalog.list_files_page(
            2, search="clips", state="cloud_only", page=1, page_size=10
        )
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["path"], "clips/cloud-only.bin")

        browse_filtered = self.catalog.list_files_page(
            2, directory="", state="cloud_only", page=1, page_size=10
        )
        self.assertEqual(
            [item["name"] for item in browse_filtered["items"]],
            ["clips"],
        )
        # Full aggregates are preserved under state filter.
        self.assertGreaterEqual(browse_filtered["items"][0]["item_count"], 2)

    def test_watcher_style_delta_updates_ancestors_without_full_relist(self) -> None:
        self.catalog.observe_local_copy(
            vault_id=2,
            path="recordings/cam1/a.bin",
            file_type="regular",
            size=100,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        before = self.catalog.list_files_page(2)["items"][0]
        self.assertEqual(before["item_count"], 1)

        self.catalog.observe_local_copy(
            vault_id=2,
            path="recordings/cam1/b.bin",
            file_type="regular",
            size=50,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:01+00:00",
        )
        # Dirty rows are durable before flush.
        dirty = self.connection.execute(
            "SELECT path FROM directory_aggregate_dirty WHERE vault_id=2"
        ).fetchall()
        self.assertTrue(dirty)
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        after = self.catalog.list_files_page(2)["items"][0]
        self.assertEqual(after["item_count"], 2)
        self.assertEqual(after["total_size"], 150)

    def test_rename_moves_contribution_between_ancestor_chains(self) -> None:
        file_id = self.catalog.observe_local_copy(
            vault_id=2,
            path="old-dir/nested/file.bin",
            file_type="regular",
            size=40,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self.catalog.rename_file(
            file_id,
            new_path="new-dir/nested/file.bin",
            changed_at="2026-07-21T11:00:00+00:00",
            vault_id=2,
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        root = self.catalog.list_files_page(2, page=1, page_size=20)
        names = {item["name"] for item in root["items"]}
        self.assertIn("new-dir", names)
        self.assertNotIn("old-dir", names)

    def test_lifecycle_pin_triggers_rebuild_and_partial_flag(self) -> None:
        self.catalog.observe_local_copy(
            vault_id=2,
            path="photos/a.jpg",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        self.catalog.observe_local_copy(
            vault_id=2,
            path="photos/b.jpg",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        set_lifecycle_pin(
            self.connection,
            vault_id=2,
            path="photos/a.jpg",
            is_directory=False,
            pinned_by=None,
            pinned_at="2026-07-21T12:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        photos = self.catalog.list_files_page(2)["items"][0]
        self.assertTrue(photos["lifecycle_pinned_partial"])
        self.assertFalse(photos["lifecycle_pinned"])

    def test_burst_updates_coalesce_directory_rebuilds(self) -> None:
        # Baseline directory.
        self.catalog.observe_local_copy(
            vault_id=2,
            path="burst/seed.bin",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)

        burst = 10_000
        started = time.perf_counter()
        for index in range(burst):
            self.catalog.observe_local_copy(
                vault_id=2,
                path=f"burst/file-{index:05d}.bin",
                file_type="regular",
                size=1,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:01+00:00",
            )
        dirty_before = self.connection.execute(
            "SELECT COUNT(*) AS total FROM directory_aggregate_dirty WHERE vault_id=2"
        ).fetchone()["total"]
        # One dirty directory row for "burst", not one per file.
        self.assertEqual(int(dirty_before), 1)
        result = aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        duration = time.perf_counter() - started
        self.assertEqual(result["rebuilt_directories"], 1)
        page = self.catalog.list_files_page(2)
        self.assertEqual(page["items"][0]["item_count"], burst + 1)
        self.assertLess(duration, 120.0)

    def test_synthetic_400k_root_page_is_bounded(self) -> None:
        # 400k files under three top-level directories, plus a couple of roots.
        per_dir = 133_334
        paths: list[tuple[str, int]] = []
        for folder, count in (
            ("clips", per_dir),
            ("recordings", per_dir),
            ("exports", 400_000 - (2 * per_dir)),
        ):
            for index in range(count):
                paths.append((f"{folder}/f-{index:06d}.bin", 1))
        paths.append(("readme.txt", 4))
        _bulk_local_files(self.connection, vault_id=2, paths_and_sizes=paths)
        rebuilt = aggregates.rebuild_vault_directory_aggregates(self.connection, 2)
        self.assertEqual(rebuilt, 3)

        def blocked(*_a, **_k):
            raise AssertionError("list_file_rows forbidden on large root listing")

        with patch.object(ArchiveCatalog, "list_file_rows", blocked):
            page = self.catalog.list_files_page(2, page=1, page_size=100)
        self.assertEqual(page["total"], 4)
        self.assertEqual(len(page["items"]), 4)
        self.assertEqual(self.catalog.last_listing_rows_materialized, 4)
        names = [item["name"] for item in page["items"]]
        self.assertEqual(names[:3], ["clips", "exports", "recordings"])
        self.assertEqual(page["items"][3]["name"], "readme.txt")
        self.assertEqual(page["items"][0]["item_count"], per_dir)

        # HTTP seam also stays bounded. Release this connection's write lock
        # first so list_files can open its own SQLite handle.
        self.connection.commit()
        test_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        with patch("app.database.settings", test_settings):
            with patch.object(ArchiveCatalog, "list_file_rows", blocked):
                result = list_files(directory="", page=1, page_size=100, vault={"id": 2})
        self.assertEqual(result["total"], 4)
        self.assertEqual(len(result["items"]), 4)

    def test_stale_rebuild_flag_does_not_block_list(self) -> None:
        """Listing must not run full rebuild; maintenance converges off-path."""
        self.catalog.observe_local_copy(
            vault_id=2,
            path="repair/me.bin",
            file_type="regular",
            size=8,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self.connection.execute("DELETE FROM directory_aggregates WHERE vault_id=2")
        aggregates.request_vault_rebuild(self.connection, 2)

        def blocked_rebuild(*_args, **_kwargs):
            raise AssertionError("list path must not rebuild vault aggregates")

        with patch.object(
            aggregates,
            "rebuild_vault_directory_aggregates",
            side_effect=blocked_rebuild,
        ):
            page = self.catalog.list_files_page(2)
        self.assertEqual(page["aggregate_status"], "loading")
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["items"], [])
        self.assertEqual(self.catalog.last_listing_rows_materialized, 0)

        # Worker/maintenance path repairs without holding /api/files.
        result = aggregates.process_directory_aggregate_maintenance(
            vault_id=2,
            publish=False,
            connection=self.connection,
        )
        self.assertEqual(result["full_rebuilds"], 1)
        page = self.catalog.list_files_page(2)
        self.assertEqual(page["aggregate_status"], "ready")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["item_count"], 1)

    def test_list_with_existing_projection_returns_stale_while_rebuild_required(
        self,
    ) -> None:
        self.catalog.observe_local_copy(
            vault_id=2,
            path="keep/me.bin",
            file_type="regular",
            size=3,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        aggregates.request_vault_rebuild(self.connection, 2)

        def blocked_rebuild(*_args, **_kwargs):
            raise AssertionError("stale listing must not rebuild")

        with patch.object(
            aggregates,
            "rebuild_vault_directory_aggregates",
            side_effect=blocked_rebuild,
        ):
            page = self.catalog.list_files_page(2)
        self.assertEqual(page["aggregate_status"], "stale")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["name"], "keep")

    def test_dirty_directory_rebuild_does_not_materialize_descendants(self) -> None:
        paths = [(f"photos/file-{index:04d}.jpg", 10) for index in range(250)]
        _bulk_local_files(self.connection, vault_id=2, paths_and_sizes=paths)
        aggregates.rebuild_vault_directory_aggregates(self.connection, 2)
        aggregates.mark_directory_dirty(self.connection, 2, "photos")

        def blocked_iter(*_args, **_kwargs):
            raise AssertionError(
                "dirty refresh must use SQL rollup, not contribution lists"
            )

        with patch.object(
            aggregates,
            "_iter_visible_file_contributions",
            side_effect=blocked_iter,
        ):
            with patch.object(
                aggregates,
                "_iter_visible_file_contribution_batches",
                side_effect=blocked_iter,
            ):
                result = aggregates.flush_directory_aggregates(
                    self.connection, vault_id=2
                )
        self.assertEqual(result["rebuilt_directories"], 1)
        page = self.catalog.list_files_page(2)
        self.assertEqual(page["items"][0]["item_count"], 250)

    def test_full_rebuild_streams_bounded_batches(self) -> None:
        paths = [(f"bucket/f-{index:03d}.bin", 1) for index in range(25)]
        paths.append(("other/x.bin", 2))
        _bulk_local_files(self.connection, vault_id=2, paths_and_sizes=paths)
        observed_sizes: list[int] = []
        original = aggregates._iter_visible_file_contribution_batches

        def tracking_batches(*args, **kwargs):
            for batch in original(*args, **kwargs):
                observed_sizes.append(len(batch))
                yield batch

        with patch.object(
            aggregates,
            "REBUILD_CONTRIBUTION_BATCH_SIZE",
            10,
        ):
            with patch.object(
                aggregates,
                "_iter_visible_file_contribution_batches",
                side_effect=tracking_batches,
            ):
                rebuilt = aggregates.rebuild_vault_directory_aggregates(
                    self.connection, 2
                )
        self.assertEqual(rebuilt, 2)
        self.assertTrue(observed_sizes)
        self.assertLessEqual(max(observed_sizes), 10)
        self.assertGreaterEqual(sum(observed_sizes), 26)
        page = self.catalog.list_files_page(2)
        names = {item["name"] for item in page["items"]}
        self.assertEqual(names, {"bucket", "other"})

    def test_400k_first_listing_after_rebuild_required_is_bounded(self) -> None:
        per_dir = 5_000
        paths: list[tuple[str, int]] = []
        for folder in ("clips", "recordings", "exports"):
            for index in range(per_dir):
                paths.append((f"{folder}/f-{index:06d}.bin", 1))
        _bulk_local_files(self.connection, vault_id=2, paths_and_sizes=paths)
        aggregates.request_vault_rebuild(self.connection, 2)
        self.connection.commit()

        def blocked_rebuild(*_args, **_kwargs):
            raise AssertionError("first listing must not rebuild 15k+ descendants")

        started = time.perf_counter()
        with patch.object(
            aggregates,
            "rebuild_vault_directory_aggregates",
            side_effect=blocked_rebuild,
        ):
            with patch.object(ArchiveCatalog, "list_file_rows", blocked_rebuild):
                page = self.catalog.list_files_page(2, page=1, page_size=100)
        duration = time.perf_counter() - started
        self.assertEqual(page["aggregate_status"], "loading")
        self.assertEqual(page["items"], [])
        self.assertEqual(self.catalog.last_listing_rows_materialized, 0)
        self.assertLess(duration, 2.0)

    def test_remark_bumps_durable_marked_at(self) -> None:
        self.catalog.observe_local_copy(
            vault_id=2,
            path="photos/a.jpg",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        # Discard observe-time dirty rows so this test owns marked_at values.
        self.connection.execute("DELETE FROM directory_aggregate_dirty WHERE vault_id=2")
        aggregates._tracker(self.connection).clear()
        with patch.object(aggregates, "_now", return_value="2026-07-21T10:00:00+00:00"):
            aggregates.mark_path_dirty(self.connection, 2, "photos/a.jpg")
        first = self.connection.execute(
            "SELECT marked_at FROM directory_aggregate_dirty WHERE vault_id=2 AND path='photos'"
        ).fetchone()["marked_at"]
        # Drop in-connection coalescing so the second mark hits durable storage.
        aggregates._tracker(self.connection).clear()
        with patch.object(aggregates, "_now", return_value="2026-07-21T10:00:05+00:00"):
            aggregates.mark_path_dirty(self.connection, 2, "photos/a.jpg")
        second = self.connection.execute(
            "SELECT marked_at FROM directory_aggregate_dirty WHERE vault_id=2 AND path='photos'"
        ).fetchone()["marked_at"]
        self.assertEqual(first, "2026-07-21T10:00:00+00:00")
        self.assertEqual(second, "2026-07-21T10:00:05+00:00")

    def test_flush_preserves_dirty_marks_newer_than_claim_cutoff(self) -> None:
        """A mark committed after the flush snapshot must survive delete."""
        self.catalog.observe_local_copy(
            vault_id=2,
            path="alpha/one.bin",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        self.catalog.observe_local_copy(
            vault_id=2,
            path="beta/two.bin",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)

        # Pre-claim dirty row (older than flush cutoff).
        self.connection.execute("DELETE FROM directory_aggregate_dirty WHERE vault_id=2")
        aggregates._tracker(self.connection).clear()
        self.connection.execute(
            """
            INSERT INTO directory_aggregate_dirty(vault_id, path, marked_at)
            VALUES (2, 'alpha', '2026-07-21T10:00:01+00:00')
            """
        )

        original_rebuild = aggregates._rebuild_directory

        def rebuild_and_concurrent_mark(connection, vault_id, directory):
            original_rebuild(connection, vault_id, directory)
            if directory == "alpha":
                # Concurrent transaction committed a newer dirty mark mid-flush.
                connection.execute(
                    """
                    INSERT INTO directory_aggregate_dirty(vault_id, path, marked_at)
                    VALUES (2, 'beta', '2026-07-21T10:00:09+00:00')
                    ON CONFLICT(vault_id, path) DO UPDATE SET marked_at=excluded.marked_at
                    """
                )
                # Same-path re-mark after claim must also survive.
                connection.execute(
                    """
                    INSERT INTO directory_aggregate_dirty(vault_id, path, marked_at)
                    VALUES (2, 'alpha', '2026-07-21T10:00:09+00:00')
                    ON CONFLICT(vault_id, path) DO UPDATE SET marked_at=excluded.marked_at
                    """
                )

        with patch.object(aggregates, "_now", return_value="2026-07-21T10:00:05+00:00"):
            with patch.object(
                aggregates, "_rebuild_directory", side_effect=rebuild_and_concurrent_mark
            ):
                aggregates.flush_directory_aggregates(self.connection, vault_id=2)

        remaining = {
            row["path"]: row["marked_at"]
            for row in self.connection.execute(
                "SELECT path, marked_at FROM directory_aggregate_dirty WHERE vault_id=2"
            ).fetchall()
        }
        self.assertEqual(
            remaining,
            {
                "alpha": "2026-07-21T10:00:09+00:00",
                "beta": "2026-07-21T10:00:09+00:00",
            },
        )

    def test_full_rebuild_preserves_post_cutoff_dirty_marks(self) -> None:
        self.catalog.observe_local_copy(
            vault_id=2,
            path="keep/me.bin",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        aggregates._tracker(self.connection).clear()
        self.connection.execute("DELETE FROM directory_aggregate_dirty WHERE vault_id=2")

        original_batches = aggregates._iter_visible_file_contribution_batches

        def batches_and_mark(connection, vault_id, **kwargs):
            marked = False
            for batch in original_batches(connection, vault_id, **kwargs):
                if not marked:
                    # Concurrent dirty mark after the rebuild claim cutoff.
                    connection.execute(
                        """
                        INSERT INTO directory_aggregate_dirty(vault_id, path, marked_at)
                        VALUES (2, 'keep', '2026-07-21T12:00:00+00:00')
                        ON CONFLICT(vault_id, path) DO UPDATE SET
                            marked_at=excluded.marked_at
                        """
                    )
                    marked = True
                yield batch

        with patch.object(aggregates, "_now", return_value="2026-07-21T11:00:00+00:00"):
            with patch.object(
                aggregates,
                "_iter_visible_file_contribution_batches",
                side_effect=batches_and_mark,
            ):
                aggregates.rebuild_vault_directory_aggregates(self.connection, 2)

        remaining = self.connection.execute(
            "SELECT path, marked_at FROM directory_aggregate_dirty WHERE vault_id=2"
        ).fetchall()
        self.assertEqual(
            [(row["path"], row["marked_at"]) for row in remaining],
            [("keep", "2026-07-21T12:00:00+00:00")],
        )

    def test_flush_rollback_leaves_durable_dirty_for_restart(self) -> None:
        self.catalog.observe_local_copy(
            vault_id=2,
            path="roll/a.bin",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self.connection.commit()

        self.catalog.observe_local_copy(
            vault_id=2,
            path="roll/b.bin",
            file_type="regular",
            size=2,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:01+00:00",
        )
        dirty_before = self.connection.execute(
            "SELECT path FROM directory_aggregate_dirty WHERE vault_id=2 ORDER BY path"
        ).fetchall()
        self.assertTrue(dirty_before)
        self.connection.rollback()
        aggregates._tracker(self.connection).clear()

        # After rollback the in-tx dirty write is gone; a committed mark must
        # still converge. Simulate a prior committed dirty row + restart.
        self.connection.execute(
            """
            INSERT INTO directory_aggregate_dirty(vault_id, path, marked_at)
            VALUES (2, 'roll', '2026-07-21T10:00:02+00:00')
            """
        )
        self.connection.commit()
        # Fresh connection-scoped tracker (restart).
        aggregates._tracker(self.connection).clear()
        # Apply the missing file on a new transaction, then flush durable dirty.
        self.catalog.observe_local_copy(
            vault_id=2,
            path="roll/b.bin",
            file_type="regular",
            size=2,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:03+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        page = self.catalog.list_files_page(2)
        self.assertEqual(page["items"][0]["item_count"], 2)

    def test_two_flush_passes_are_idempotent_on_shared_dirty_set(self) -> None:
        self.catalog.observe_local_copy(
            vault_id=2,
            path="shared/x.bin",
            file_type="regular",
            size=3,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        aggregates.mark_path_dirty(self.connection, 2, "shared/x.bin")
        first = aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        second = aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self.assertGreaterEqual(first["rebuilt_directories"], 1)
        self.assertEqual(second["rebuilt_directories"], 0)
        dirty = self.connection.execute(
            "SELECT COUNT(*) AS total FROM directory_aggregate_dirty WHERE vault_id=2"
        ).fetchone()["total"]
        self.assertEqual(int(dirty), 0)


class DirectoryAggregateInvalidationTests(unittest.TestCase):
    """Direct catalog writers must invalidate aggregate ancestors."""

    def setUp(self) -> None:
        metrics_service.reset_for_tests()
        aggregates.set_maintenance_scheduling_enabled(False)
        self._tmp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._tmp.name) / "catalog.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connection = SQLiteConnection(str(self.database_path))
        self.connection.__enter__()
        _seed_vault(self.connection, 2)
        self.catalog = ArchiveCatalog(self.connection)

    def tearDown(self) -> None:
        self.connection.__exit__(None, None, None)
        self._tmp.cleanup()
        aggregates.set_maintenance_scheduling_enabled(True)

    def _seed_both_file(self, path: str = "photos/shot.jpg") -> str:
        digest = "a" * 64
        file_id = self.catalog.observe_local_copy(
            vault_id=2,
            path=path,
            file_type="regular",
            size=100,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        version_id = self.catalog.record_archive_version(
            vault_id=2,
            path=path,
            object_key=f"docs/{path}",
            provider_version_id="v-1",
            size=100,
            storage_class="STANDARD",
            etag="e1",
            uploaded_at="2026-07-21T10:00:00+00:00",
            observed_at="2026-07-21T10:00:00+00:00",
            scan_id="scan-1",
        )
        self.connection.execute(
            """
            UPDATE archive_versions
            SET plaintext_sha256=%s, integrity='verified', verified_at=%s
            WHERE id=%s
            """,
            (digest, "2026-07-21T10:00:00+00:00", version_id),
        )
        self.catalog.set_local_fingerprint(
            vault_id=2,
            path=path,
            plaintext_sha256=digest,
            matched_archive_version_id=version_id,
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        return file_id

    def test_catalog_audit_availability_change_invalidates_state(self) -> None:
        from app.services.catalog_audit import audit_vault_catalog

        self._seed_both_file()
        before = self.catalog.list_files_page(2)["items"][0]
        self.assertEqual(before["state"], "both")
        self.assertEqual(before["available_actions"]["free-space"], 1)

        vault = self.connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()

        class _EmptyClient:
            def get_paginator(self, _name):
                return self

            def paginate(self, **_kwargs):
                yield {"Versions": [], "DeleteMarkers": []}

        audit_vault_catalog(self.connection, vault, _EmptyClient())
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        after = self.catalog.list_files_page(2)["items"][0]
        self.assertEqual(after["state"], "local_only")
        self.assertEqual(after["available_actions"]["upload"], 1)
        self.assertEqual(after["available_actions"]["free-space"], 0)
        self.assertIsNone(after.get("storage_class"))

    def test_catalog_audit_storage_class_drift_invalidates_storage(self) -> None:
        from app.services.catalog_audit import audit_vault_catalog

        self._seed_both_file()
        vault = self.connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
        version = self.connection.execute(
            "SELECT object_key, provider_version_id FROM archive_versions"
        ).fetchone()

        class _DriftClient:
            def get_paginator(self, _name):
                return self

            def paginate(self, **_kwargs):
                yield {
                    "Versions": [
                        {
                            "Key": version["object_key"],
                            "VersionId": version["provider_version_id"],
                            "StorageClass": "GLACIER",
                        }
                    ],
                    "DeleteMarkers": [],
                }

            def get_object_tagging(self, **_kwargs):
                return {"TagSet": []}

        audit_vault_catalog(self.connection, vault, _DriftClient())
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        after = self.catalog.list_files_page(2)["items"][0]
        self.assertEqual(after["storage_class"], "GLACIER")
        self.assertEqual(after["storage_class_count"], 1)

    def test_cloud_deletion_purge_invalidates_counts_and_actions(self) -> None:
        from app.services import cloud_deletion as cloud_deletion_service

        file_id = self._seed_both_file(path="purge/me.bin")
        version_id = self.connection.execute(
            "SELECT id FROM archive_versions WHERE provider_version_id='v-1'"
        ).fetchone()["id"]
        # Minimal cloud_deletion_items row for the purge bookkeeping seam.
        self.connection.execute(
            """
            INSERT INTO jobs(
                vault_id, vault_file_id, path, action, status,
                requested_by, requested_at, updated_at
            ) VALUES (
                2, %s, 'purge/me.bin', 'cloud-purge', 'running',
                NULL, '2026-07-21T10:00:00+00:00', '2026-07-21T10:00:00+00:00'
            )
            """,
            (file_id,),
        )
        job_id = self.connection.execute("SELECT id FROM jobs").fetchone()["id"]
        self.connection.execute(
            """
            INSERT INTO cloud_deletion_items(
                job_id, vault_id, vault_file_id, kind, archive_version_id,
                object_key, provider_version_id, status, updated_at
            ) VALUES (
                %s, 2, %s, 'version', %s, 'docs/purge/me.bin', 'v-1',
                'pending', '2026-07-21T10:00:00+00:00'
            )
            """,
            (job_id, file_id, version_id),
        )
        item_id = self.connection.execute(
            "SELECT id FROM cloud_deletion_items"
        ).fetchone()["id"]
        cloud_deletion_service.mark_item_deleted(
            self.connection,
            item_id=int(item_id),
            updated_at="2026-07-21T11:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        after = self.catalog.list_files_page(2)["items"][0]
        self.assertEqual(after["state"], "local_only")
        self.assertEqual(after["available_actions"]["cloud-purge"], 0)
        self.assertEqual(after["cloud_size"], 0)

    def test_direct_local_copy_fingerprint_update_invalidates_actions(self) -> None:
        self._seed_both_file(path="hash/me.bin")
        before = self.catalog.list_files_page(2)["items"][0]
        self.assertEqual(before["available_actions"]["free-space"], 1)

        file_id = self.connection.execute(
            """
            SELECT vf.id FROM vault_files vf
            JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            WHERE fp.path='hash/me.bin'
            """
        ).fetchone()["id"]
        # Mimic storage._apply_local_fingerprint_updates direct SQL.
        self.connection.execute(
            """
            UPDATE local_copies
            SET plaintext_sha256=%s,
                matched_archive_version_id=NULL,
                size=100,
                mtime_ns=1,
                last_seen_at='scan-2',
                observed_at='2026-07-21T12:00:00+00:00'
            WHERE vault_file_id=%s
            """,
            ("b" * 64, file_id),
        )
        aggregates.invalidate_for_vault_file(self.connection, 2, file_id)
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        after = self.catalog.list_files_page(2)["items"][0]
        self.assertEqual(after["available_actions"]["free-space"], 0)

    def test_invalidate_for_archive_version_ids_marks_ancestors(self) -> None:
        self._seed_both_file(path="deep/nested/file.bin")
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self.connection.execute("DELETE FROM directory_aggregate_dirty WHERE vault_id=2")
        aggregates._tracker(self.connection).clear()
        version_id = self.connection.execute(
            "SELECT id FROM archive_versions WHERE provider_version_id='v-1'"
        ).fetchone()["id"]
        aggregates.invalidate_for_archive_version_ids(self.connection, [version_id])
        dirty = {
            row["path"]
            for row in self.connection.execute(
                "SELECT path FROM directory_aggregate_dirty WHERE vault_id=2"
            ).fetchall()
        }
        self.assertEqual(dirty, {"deep", "deep/nested"})

    def _seed_rename_candidate(
        self,
        *,
        old_path: str,
        new_path: str,
        size: int = 40,
        with_cloud: bool = False,
    ) -> str:
        """Seed missing@old + present@new sharing a digest for confirmation."""
        digest = "c" * 64
        file_id = self.catalog.observe_local_copy(
            vault_id=2,
            path=old_path,
            file_type="regular",
            size=size,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        version_id = None
        if with_cloud:
            version_id = self.catalog.record_archive_version(
                vault_id=2,
                path=old_path,
                object_key=f"docs/{old_path}",
                provider_version_id="rename-v1",
                size=size,
                storage_class="STANDARD",
                etag="rename-etag",
                uploaded_at="2026-07-21T10:00:00+00:00",
                observed_at="2026-07-21T10:00:00+00:00",
                scan_id="scan-rename",
            )
            self.connection.execute(
                """
                UPDATE archive_versions
                SET plaintext_sha256=%s, integrity='verified', verified_at=%s
                WHERE id=%s
                """,
                (digest, "2026-07-21T10:00:00+00:00", version_id),
            )
        self.catalog.set_local_fingerprint(
            vault_id=2,
            path=old_path,
            plaintext_sha256=digest,
            matched_archive_version_id=version_id,
        )
        self.catalog.mark_local_copy_missing(
            file_id, observed_at="2026-07-21T11:00:00+00:00"
        )
        self.catalog.observe_local_copy(
            vault_id=2,
            path=new_path,
            file_type="regular",
            size=size,
            mtime_ns=2,
            observed_at="2026-07-21T11:00:00+00:00",
        )
        # Carry the verified match onto the provisional so post-confirm
        # free-space eligibility (local match == archive version) is realistic.
        self.catalog.set_local_fingerprint(
            vault_id=2,
            path=new_path,
            plaintext_sha256=digest,
            matched_archive_version_id=version_id,
        )
        return file_id

    def _clear_dirty(self) -> None:
        self.connection.execute("DELETE FROM directory_aggregate_dirty WHERE vault_id=2")
        aggregates._tracker(self.connection).clear()

    def _dirty_paths(self) -> set[str]:
        return {
            row["path"]
            for row in self.connection.execute(
                "SELECT path FROM directory_aggregate_dirty WHERE vault_id=2"
            ).fetchall()
        }

    def test_confirm_file_rename_marks_old_and_new_ancestors(self) -> None:
        file_id = self._seed_rename_candidate(
            old_path="old-dir/nested/file.bin",
            new_path="new-dir/nested/file.bin",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self._clear_dirty()

        self.catalog.confirm_file_rename(
            vault_file_id=file_id,
            new_path="new-dir/nested/file.bin",
            changed_at="2026-07-21T12:00:00+00:00",
            vault_id=2,
        )

        self.assertEqual(
            self._dirty_paths(),
            {"old-dir", "old-dir/nested", "new-dir", "new-dir/nested"},
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        root = self.catalog.list_files_page(2, page=1, page_size=20)
        names = {item["name"] for item in root["items"]}
        self.assertIn("new-dir", names)
        self.assertNotIn("old-dir", names)
        new_dir = next(item for item in root["items"] if item["name"] == "new-dir")
        self.assertEqual(new_dir["item_count"], 1)
        self.assertEqual(new_dir["total_size"], 40)

    def test_confirm_file_rename_state_action_storage_parity(self) -> None:
        file_id = self._seed_rename_candidate(
            old_path="cloud-old/shot.jpg",
            new_path="cloud-new/shot.jpg",
            size=100,
            with_cloud=True,
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self._clear_dirty()

        self.catalog.confirm_file_rename(
            vault_file_id=file_id,
            new_path="cloud-new/shot.jpg",
            changed_at="2026-07-21T12:00:00+00:00",
            vault_id=2,
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)

        root = self.catalog.list_files_page(2)["items"]
        names = {item["name"] for item in root}
        self.assertIn("cloud-new", names)
        self.assertNotIn("cloud-old", names)
        folder = next(item for item in root if item["name"] == "cloud-new")
        self.assertEqual(folder["state"], "both")
        self.assertEqual(folder["available_actions"]["free-space"], 1)
        self.assertEqual(folder["storage_class"], "STANDARD")
        self.assertEqual(folder["cloud_size"], 100)

    def test_confirm_folder_rename_converges_directory_chains(self) -> None:
        first = self._seed_rename_candidate(
            old_path="album/2024/a.bin",
            new_path="archive/2024/a.bin",
            size=10,
        )
        # Second sibling under the same folder prefixes.
        second_old = "album/2024/b.bin"
        second_new = "archive/2024/b.bin"
        digest = "d" * 64
        second_id = self.catalog.observe_local_copy(
            vault_id=2,
            path=second_old,
            file_type="regular",
            size=20,
            mtime_ns=1,
            observed_at="2026-07-21T10:05:00+00:00",
        )
        self.catalog.set_local_fingerprint(
            vault_id=2,
            path=second_old,
            plaintext_sha256=digest,
            matched_archive_version_id=None,
        )
        self.catalog.mark_local_copy_missing(
            second_id, observed_at="2026-07-21T11:05:00+00:00"
        )
        self.catalog.observe_local_copy(
            vault_id=2,
            path=second_new,
            file_type="regular",
            size=20,
            mtime_ns=2,
            observed_at="2026-07-21T11:05:00+00:00",
        )
        self.catalog.set_local_fingerprint(
            vault_id=2,
            path=second_new,
            plaintext_sha256=digest,
            matched_archive_version_id=None,
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self._clear_dirty()

        renamed = self.catalog.confirm_folder_rename(
            vault_id=2,
            old_prefix="album/2024",
            new_prefix="archive/2024",
            changed_at="2026-07-21T12:30:00+00:00",
        )
        self.assertEqual(set(renamed), {first, second_id})
        dirty = self._dirty_paths()
        self.assertTrue({"album", "album/2024"}.issubset(dirty))
        self.assertTrue({"archive", "archive/2024"}.issubset(dirty))

        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        root = self.catalog.list_files_page(2)["items"]
        names = {item["name"] for item in root}
        self.assertIn("archive", names)
        self.assertNotIn("album", names)
        archive = next(item for item in root if item["name"] == "archive")
        self.assertEqual(archive["item_count"], 2)
        self.assertEqual(archive["total_size"], 30)

    def test_confirm_file_rename_after_earlier_dirty_flushed(self) -> None:
        # Unrelated dirty work is flushed first; rename must still mark both chains.
        self.catalog.observe_local_copy(
            vault_id=2,
            path="other/seed.bin",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T09:00:00+00:00",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self._clear_dirty()

        file_id = self._seed_rename_candidate(
            old_path="alpha/x.bin",
            new_path="beta/x.bin",
            size=7,
        )
        # Seed leaves dirty from observe/fingerprint; flush those first.
        # Missing@alpha has no cloud so it does not contribute; only beta shows.
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        before_names = {
            item["name"] for item in self.catalog.list_files_page(2)["items"]
        }
        self.assertIn("beta", before_names)
        self.assertNotIn("alpha", before_names)
        self._clear_dirty()

        self.catalog.confirm_file_rename(
            vault_file_id=file_id,
            new_path="beta/x.bin",
            changed_at="2026-07-21T13:00:00+00:00",
            vault_id=2,
        )
        # Old chain still dirtied so any stale alpha rollup would be cleared.
        self.assertEqual(self._dirty_paths(), {"alpha", "beta"})
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        after = {
            item["name"]: item
            for item in self.catalog.list_files_page(2)["items"]
        }
        self.assertIn("beta", after)
        self.assertNotIn("alpha", after)
        self.assertEqual(after["beta"]["item_count"], 1)
        self.assertEqual(after["beta"]["total_size"], 7)

    def test_confirm_file_rename_failed_cas_does_not_leave_dirty(self) -> None:
        file_id = self._seed_rename_candidate(
            old_path="fail-old/a.bin",
            new_path="fail-new/a.bin",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self._clear_dirty()

        from app.catalog import VaultFileNotFound

        with self.assertRaises(VaultFileNotFound):
            self.catalog.confirm_file_rename(
                vault_file_id=file_id,
                new_path="fail-new/does-not-exist.bin",
                changed_at="2026-07-21T14:00:00+00:00",
                vault_id=2,
            )
        self.assertEqual(self._dirty_paths(), set())

        # Successful confirmation after a failed attempt still dirties correctly.
        self.catalog.confirm_file_rename(
            vault_file_id=file_id,
            new_path="fail-new/a.bin",
            changed_at="2026-07-21T14:01:00+00:00",
            vault_id=2,
        )
        self.assertEqual(self._dirty_paths(), {"fail-old", "fail-new"})

    def test_confirm_file_rename_idempotent_second_call(self) -> None:
        file_id = self._seed_rename_candidate(
            old_path="once-old/f.bin",
            new_path="once-new/f.bin",
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self._clear_dirty()

        self.catalog.confirm_file_rename(
            vault_file_id=file_id,
            new_path="once-new/f.bin",
            changed_at="2026-07-21T15:00:00+00:00",
            vault_id=2,
        )
        first_dirty = self._dirty_paths()
        self.assertEqual(first_dirty, {"once-old", "once-new"})
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self._clear_dirty()

        from app.catalog import VaultFileNotFound

        with self.assertRaises(VaultFileNotFound):
            self.catalog.confirm_file_rename(
                vault_file_id=file_id,
                new_path="once-new/f.bin",
                changed_at="2026-07-21T15:01:00+00:00",
                vault_id=2,
            )
        self.assertEqual(self._dirty_paths(), set())

    def test_confirm_file_rename_dirty_survives_reconnect_until_flush(self) -> None:
        file_id = self._seed_rename_candidate(
            old_path="persist-old/p.bin",
            new_path="persist-new/p.bin",
            size=5,
        )
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        self._clear_dirty()
        self.catalog.confirm_file_rename(
            vault_file_id=file_id,
            new_path="persist-new/p.bin",
            changed_at="2026-07-21T16:00:00+00:00",
            vault_id=2,
        )
        self.connection.commit()

        # Restart recovery: a fresh connection still sees durable dirty rows.
        with SQLiteConnection(str(self.database_path)) as other:
            dirty = {
                row["path"]
                for row in other.execute(
                    "SELECT path FROM directory_aggregate_dirty WHERE vault_id=2"
                ).fetchall()
            }
            self.assertEqual(dirty, {"persist-old", "persist-new"})
            aggregates.flush_directory_aggregates(other, vault_id=2)
            other.commit()

        with SQLiteConnection(str(self.database_path)) as verify:
            catalog = ArchiveCatalog(verify)
            names = {
                item["name"] for item in catalog.list_files_page(2)["items"]
            }
            self.assertIn("persist-new", names)
            self.assertNotIn("persist-old", names)


if __name__ == "__main__":
    unittest.main()
