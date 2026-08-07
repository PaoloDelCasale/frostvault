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

    def test_stale_rebuild_flag_repairs_on_list(self) -> None:
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
        page = self.catalog.list_files_page(2)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["item_count"], 1)


if __name__ == "__main__":
    unittest.main()
