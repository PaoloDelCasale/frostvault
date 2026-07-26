from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import (
    build_directory_items,
    build_job_groups,
    list_files,
    normalize_directory,
)
from tests.test_database import run_alembic


class FileBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            {"path": "photos/2025/sea.jpg", "state": "both"},
            {"path": "photos/cover.jpg", "state": "local_only"},
            {"path": "documents/invoice.pdf", "state": "cloud_only"},
            {"path": "leggimi.txt", "state": "both"},
        ]

    def test_root_contains_folders_before_files(self) -> None:
        items = build_directory_items(self.rows, "")

        self.assertEqual(
            [(item["type"], item["name"]) for item in items],
            [
                ("directory", "documents"),
                ("directory", "photos"),
                ("file", "leggimi.txt"),
            ],
        )
        self.assertEqual(items[1]["item_count"], 2)

    def test_nested_level_only_contains_direct_children(self) -> None:
        rows = [row for row in self.rows if row["path"].startswith("photos/")]
        items = build_directory_items(rows, "photos")

        self.assertEqual(
            [(item["type"], item["name"]) for item in items],
            [("directory", "2025"), ("file", "cover.jpg")],
        )

    def test_directory_aggregates_size_state_and_available_actions(self) -> None:
        rows = [
            {
                "path": "photos/small.jpg",
                "state": "local_only",
                "local_size": 1_000_000,
                "cloud_size": None,
                "storage_class": None,
            },
            {
                "path": "photos/large.jpg",
                "state": "cloud_only",
                "local_size": None,
                "cloud_size": 100_000_000,
                "storage_class": "STANDARD",
            },
            {
                "path": "photos/safe.jpg",
                "state": "both",
                "local_size": 2_000_000,
                "cloud_size": 2_000_032,
                "storage_class": "STANDARD",
            },
        ]

        folder = build_directory_items(rows, "")[0]

        self.assertEqual(folder["total_size"], 103_000_000)
        self.assertEqual(folder["state"], "mixed")
        self.assertEqual(folder["state_counts"], {"local_only": 1, "cloud_only": 1, "both": 1})
        self.assertEqual(
            folder["available_actions"],
            {
                "upload": 1,
                "recover": 1,
                "free-space": 1,
                "cloud-archive": 2,
                "cloud-purge": 2,
            },
        )

    def test_state_filter_keeps_real_directory_aggregates(self) -> None:
        rows = [
            {"path": "photos/a.jpg", "state": "local_only"},
            {"path": "photos/b.jpg", "state": "cloud_only"},
            {"path": "documents/c.pdf", "state": "cloud_only"},
        ]

        items = build_directory_items(rows, "", "local_only")

        self.assertEqual([item["name"] for item in items], ["photos"])
        self.assertEqual(items[0]["state"], "mixed")
        self.assertEqual(items[0]["item_count"], 2)

    def test_folder_progress_is_weighted_by_bytes_not_file_count(self) -> None:
        rows = []
        for job_id in range(1, 10):
            rows.append(
                {
                    "id": job_id,
                    "path": f"folder/small-{job_id}.bin",
                    "action": "upload",
                    "status": "completed",
                    "message": "",
                    "requested_at": "2026-07-20T12:00:00+00:00",
                    "updated_at": "2026-07-20T12:00:01+00:00",
                    "group_id": "folder-upload",
                    "group_path": "folder",
                    "total_bytes": 1_000_000,
                    "transferred_bytes": 1_000_000,
                }
            )
        rows.append(
            {
                "id": 10,
                "path": "folder/large.bin",
                "action": "upload",
                "status": "uploading",
                "message": "Encrypted upload in progress",
                "requested_at": "2026-07-20T12:00:00+00:00",
                "updated_at": "2026-07-20T12:00:02+00:00",
                "group_id": "folder-upload",
                "group_path": "folder",
                "total_bytes": 100_000_000,
                "transferred_bytes": 0,
            }
        )

        group = build_job_groups(rows)[0]

        self.assertEqual(group["completed_count"], 9)
        self.assertEqual(group["item_count"], 10)
        self.assertEqual(group["percent"], 8)

    def test_interrupted_group_has_a_terminal_cancelled_status(self) -> None:
        rows = [
            {
                "id": 1,
                "path": "folder/completed.bin",
                "action": "upload",
                "status": "completed",
                "message": "",
                "requested_at": "2026-07-20T12:00:00+00:00",
                "updated_at": "2026-07-20T12:00:01+00:00",
                "group_id": "folder-upload",
                "group_path": "folder",
                "total_bytes": 10,
                "transferred_bytes": 10,
            },
            {
                "id": 2,
                "path": "folder/stopped.bin",
                "action": "upload",
                "status": "cancelled",
                "message": "Upload stopped",
                "requested_at": "2026-07-20T12:00:00+00:00",
                "updated_at": "2026-07-20T12:00:02+00:00",
                "group_id": "folder-upload",
                "group_path": "folder",
                "total_bytes": 90,
                "transferred_bytes": 20,
            },
        ]

        group = build_job_groups(rows)[0]

        self.assertEqual(group["status"], "cancelled")
        self.assertEqual(group["cancelled_count"], 1)
        self.assertEqual(group["percent"], 30)

    def test_cleanup_group_reports_freed_bytes_and_files(self) -> None:
        rows = [
            {
                "id": 1,
                "path": "folder/a.bin",
                "action": "free-space",
                "status": "completed",
                "message": "Local space freed",
                "requested_at": "2026-07-20T12:00:00+00:00",
                "updated_at": "2026-07-20T12:00:01+00:00",
                "group_id": "folder-cleanup",
                "group_path": "folder",
                "total_bytes": 25,
                "transferred_bytes": 25,
            },
            {
                "id": 2,
                "path": "folder/b.bin",
                "action": "free-space",
                "status": "cleaning",
                "message": "Verifying cloud copy",
                "requested_at": "2026-07-20T12:00:00+00:00",
                "updated_at": "2026-07-20T12:00:02+00:00",
                "group_id": "folder-cleanup",
                "group_path": "folder",
                "total_bytes": 75,
                "transferred_bytes": 0,
            },
        ]

        group = build_job_groups(rows)[0]

        self.assertEqual(group["action"], "free-space")
        self.assertEqual(group["status"], "cleaning")
        self.assertEqual(group["completed_count"], 1)
        self.assertEqual(group["percent"], 25)

    def test_directory_is_normalized_and_traversal_is_rejected(self) -> None:
        self.assertEqual(normalize_directory(r"photos\2025"), "photos/2025")
        with self.assertRaises(HTTPException):
            normalize_directory("../segreti")

    def test_file_browser_reads_the_versioned_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=2,
                    path="reports/annual.txt",
                    file_type="regular",
                    size=7,
                    mtime_ns=10,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=2,
                    path="reports/annual.txt",
                    object_key="docs/reports/annual.txt",
                    provider_version_id="version-1",
                    size=7,
                    storage_class="STANDARD",
                    etag="etag",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="2026-07-21T10:00:00+00:00",
                )
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", test_settings):
                result = list_files(
                    directory="reports",
                    page=1,
                    page_size=100,
                    vault={"id": 2},
                )

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["path"], "reports/annual.txt")
            self.assertEqual(result["items"][0]["state"], "both")
            self.assertEqual(result["items"][0]["cloud_size"], 7)
            self.assertFalse(result["items"][0]["cleanup_eligible"])


if __name__ == "__main__":
    unittest.main()
