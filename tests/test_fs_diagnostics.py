"""Symlink rejection and permission-error reporting (issue #9).

Seams:
- app.storage.safe_local_path / safe_local_entry_path (operation refusal)
- app.storage.scan_tree (catalog + diagnostics; never silent skip)
- /api/stats filesystem (vault health visible to operators)
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import stats
from app.storage import (
    runtime_status,
    safe_local_entry_path,
    safe_local_path,
    scan_tree,
)
from tests.test_database import run_alembic


class SymlinkPathRejectionTests(unittest.TestCase):
    def test_safe_local_path_rejects_final_symlink_without_following(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.txt"
            real.write_text("payload", encoding="utf-8")
            link = root / "alias.txt"
            link.symlink_to(real)
            with self.assertRaisesRegex(ValueError, r"(?i)symbolic link"):
                safe_local_path(str(root), "alias.txt")

    def test_safe_local_entry_path_rejects_symlink_for_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.txt"
            real.write_text("payload", encoding="utf-8")
            link = root / "alias.txt"
            link.symlink_to(real)
            with self.assertRaisesRegex(ValueError, r"(?i)symbolic link"):
                safe_local_entry_path(str(root), "alias.txt")


class ScanSymlinkAndPermissionTests(unittest.TestCase):
    def test_scan_catalogues_symlink_as_unsupported_not_regular(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            source = Path(directory) / "sources"
            source.mkdir()
            (source / "ok.txt").write_text("hi", encoding="utf-8")
            (source / "link.txt").symlink_to(source / "ok.txt")
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (9, 'fs', 'FS', %s, 'bucket', 'fs', 'remote')
                    """,
                    (str(source),),
                )
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", test_settings):
                count = scan_tree(
                    {"id": 9, "source_root": str(source)},
                    "scan-1",
                )
            self.assertEqual(count, 2)
            with SQLiteConnection(str(database_path)) as connection:
                rows = ArchiveCatalog(connection).list_file_rows(9)
            by_path = {row["path"]: row for row in rows}
            self.assertEqual(by_path["ok.txt"]["local_file_type"], "regular")
            self.assertTrue(by_path["ok.txt"]["upload_eligible"])
            self.assertEqual(by_path["link.txt"]["local_file_type"], "symlink")
            self.assertFalse(by_path["link.txt"]["upload_eligible"])
            self.assertFalse(by_path["link.txt"]["cleanup_eligible"])

    def test_unreadable_file_during_scan_is_recorded_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            source = Path(directory) / "sources"
            source.mkdir()
            secret = source / "secret.bin"
            secret.write_bytes(b"x")
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (10, 'perm', 'Perm', %s, 'bucket', 'perm', 'remote')
                    """,
                    (str(source),),
                )
            vault = {"id": 10, "source_root": str(source)}
            runtime_status.pop(10, None)
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            original_stat = Path.stat
            original_lstat = Path.lstat

            def guarded_stat(self, *args, **kwargs):
                if self.name == "secret.bin":
                    raise PermissionError("denied")
                return original_stat(self, *args, **kwargs)

            def guarded_lstat(self, *args, **kwargs):
                if self.name == "secret.bin":
                    raise PermissionError("denied")
                return original_lstat(self, *args, **kwargs)

            with (
                patch("app.database.settings", test_settings),
                patch.object(Path, "stat", guarded_stat),
                patch.object(Path, "lstat", guarded_lstat),
            ):
                scan_tree(vault, "scan-perm")
            status = runtime_status.get(10) or {}
            findings = status.get("filesystem", {}).get("findings") or []
            codes = {item["code"] for item in findings}
            self.assertIn("fs.unreadable_file", codes)
            paths = {item["path"] for item in findings}
            self.assertIn("secret.bin", paths)


class VaultFilesystemHealthStatsTests(unittest.TestCase):
    def test_stats_include_filesystem_preflight_for_vault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            source = Path(directory) / "sources"
            source.mkdir()
            (source / "a.txt").write_text("a", encoding="utf-8")
            (source / "b.txt").symlink_to(source / "a.txt")
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (11, 'health', 'Health', %s, 'bucket', 'h', 'remote')
                    """,
                    (str(source),),
                )
            vault = {
                "id": 11,
                "role": "owner",
                "source_root": str(source),
            }
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
                allow_local_delete=True,
            )
            from app import main as main_module

            with (
                patch.object(main_module, "settings", test_settings),
                patch("app.database.settings", test_settings),
                patch.object(main_module, "is_owner", lambda _role: True),
            ):
                payload = stats(vault=vault)
            filesystem = payload["filesystem"]
            self.assertFalse(filesystem["ok"])
            self.assertEqual(filesystem["uid"], os.geteuid())
            finding_codes = {f["code"] for f in filesystem["findings"]}
            self.assertIn("fs.symlink", finding_codes)


if __name__ == "__main__":
    unittest.main()
