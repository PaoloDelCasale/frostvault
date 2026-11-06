"""Filesystem problems come from the actual scan/watcher, never a sampled banner."""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from watchfiles import Change

from app import storage
from app.catalog import ArchiveCatalog
from app.config import settings

from app.services import source_layout
from app.services.fs_preflight import inspect_entry
from tests.test_database import run_alembic


class FilesystemProblemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "sources" / "managed" / "vault"
        self.root.mkdir(parents=True)
        source_layout.override_sources_root(base / "sources")
        self.addCleanup(source_layout.reset_sources_root_override)
        self.path = base / "catalog.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.path)) as connection:
            connection.execute("INSERT INTO users(id, username, display_name, password_hash) VALUES (1, 'owner', 'Owner', 'hash')")
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (9, 'files', 'Files', %s, 'bucket', 'files')",
                (str(self.root),),
            )
        self.vault = {"id": 9, "source_root": str(self.root)}
        self.cfg = replace(settings, database_dsn=database_dsn(str(self.path)))
        self.patch = patch("app.database.settings", self.cfg)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        storage.runtime_status.pop(9, None)

    def problems(self):
        with PostgresConnection(str(self.path)) as connection:
            return connection.execute("SELECT event, message_params FROM notifications ORDER BY id").fetchall()

    def test_symlink_is_catalogued_as_unsupported_and_never_followed(self):
        (self.root / "ok.txt").write_text("hello")
        (self.root / "link.txt").symlink_to(self.root / "ok.txt")
        self.assertEqual(storage.scan_tree(self.vault, "scan"), 2)
        with PostgresConnection(str(self.path)) as connection:
            rows = {r["path"]: r for r in ArchiveCatalog(connection).list_file_rows(9)}
        self.assertTrue(rows["ok.txt"]["upload_eligible"])
        self.assertFalse(rows["link.txt"]["upload_eligible"])
        self.assertFalse(rows["link.txt"]["cleanup_eligible"])
        for helper in (storage.safe_local_path, storage.safe_local_entry_path):
            with self.assertRaisesRegex(ValueError, "(?i)symbolic link"):
                helper(str(self.root), "link.txt")
        self.assertEqual(self.problems(), [])

    def test_every_unreadable_file_is_reported_beyond_old_sample_limit(self):
        import json
        for number in range(40):
            (self.root / f"{number}.bin").write_bytes(b"data")
        storage.scan_tree(self.vault, "before")
        real_open = os.open

        def denied(path, *args, **kwargs):
            if str(path).endswith(".bin"):
                raise PermissionError("sensitive exception text must not be stored")
            return real_open(path, *args, **kwargs)

        with patch("app.services.fs_preflight.os.open", side_effect=denied):
            for scan in ("denied-1", "denied-2"):
                self.assertEqual(storage.scan_tree(self.vault, scan), 0)
        problems = self.problems()
        self.assertEqual(len(problems), 40)  # all paths, deduplicated across scans
        self.assertEqual({json.loads(p["message_params"])["path"] for p in problems}, {f"{n}.bin" for n in range(40)})
        self.assertTrue(all(p["event"] == "file_unreadable" for p in problems))
        self.assertNotIn("sensitive exception", str(problems))
        with PostgresConnection(str(self.path)) as connection:
            copies = connection.execute("SELECT presence FROM local_copies").fetchall()
        self.assertEqual(len(copies), 40)
        self.assertTrue(all(row["presence"] == "present" for row in copies))

    def test_stat_failure_is_not_silently_skipped(self):
        (self.root / "stat.bin").write_bytes(b"x")
        original = Path.lstat
        def denied(path, *args, **kwargs):
            if path.name == "stat.bin":
                raise PermissionError("denied")
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", denied):
            self.assertEqual(storage.scan_tree(self.vault, "scan"), 0)
        self.assertIn("stat.bin", str(self.problems()))

    def test_inaccessible_subtree_is_reported_and_never_marks_copies_missing(self):
        (self.root / "old.txt").write_text("data")
        storage.scan_tree(self.vault, "before")
        private = self.root / "private"
        private.mkdir()
        (self.root / "old.txt").rename(private / "old.txt")
        real_scandir = os.scandir
        def inaccessible(fd):
            if Path(os.readlink(f"/proc/self/fd/{fd}")) == private:
                raise PermissionError(13, "denied", str(private))
            return real_scandir(fd)
        with patch("app.services.fs_preflight.os.scandir", side_effect=inaccessible):
            self.assertEqual(storage.scan_tree(self.vault, "incomplete"), 0)
        self.assertIn("private", str(self.problems()))
        with PostgresConnection(str(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT presence FROM local_copies").fetchone()["presence"], "present")

    def test_watcher_checks_content_readability_not_just_stat(self):
        target = self.root / "watch.bin"
        target.write_bytes(b"x")
        real_open = os.open
        def denied(path, *args, **kwargs):
            if Path(path) == target:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)
        with patch("app.services.fs_preflight.os.open", side_effect=denied):
            self.assertEqual(storage.apply_filesystem_changes(self.vault, {(Change.modified, str(target))}), 0)
        self.assertIn("watch.bin", str(self.problems()))

    def test_root_inaccessible_becomes_protection_blocked(self):
        with (
            patch("app.storage._validate_enrolled_scan_root"),
            patch("app.services.fs_preflight.os.access", return_value=False),
            patch("app.storage.scan_cloud", return_value=0),
        ):
            result = storage.scan_vault(self.vault)
        self.assertEqual(result["local"], -1)
        self.assertIn("protection_blocked", {p["event"] for p in self.problems()})

    def test_unreadable_non_utf8_filename_is_still_signalled(self):
        target = self.root / "bad\udcff.jpg"
        target.write_bytes(b"data")
        real_open = os.open
        def denied(path, *args, **kwargs):
            if str(path).endswith(".jpg"):
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)
        with patch("app.services.fs_preflight.os.open", side_effect=denied):
            self.assertEqual(storage.scan_tree(self.vault, "non-utf8"), 0)
        problems = self.problems()
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["event"], "file_unreadable")
        import json
        self.assertEqual(json.loads(problems[0]["message_params"])["path"], "bad\\udcff.jpg")

    def test_hash_read_failure_also_reports_the_exact_path(self):
        target = self.root / "hash.bin"
        target.write_bytes(b"data")
        storage.scan_tree(self.vault, "scan")
        with patch("app.storage.hash_stable_regular_file", side_effect=PermissionError("denied")):
            self.assertEqual(storage._collect_local_fingerprint_updates(self.vault, self.root), [])
        self.assertIn("hash.bin", str(self.problems()))

    def test_identity_unsafe_volume_never_starts_a_readability_walk(self):
        from app.services.source_layout import VaultLocalAccess
        with (
            patch("app.storage.source_layout.vault_local_access", return_value=VaultLocalAccess(False, True, "photos", "replaced")),
            patch("app.storage.PinnedTree", side_effect=AssertionError("unsafe walk")),
            patch("app.storage.inspect_entry", side_effect=AssertionError("unsafe read")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Source Volume"):
                storage.scan_tree(self.vault, "blocked")
            with self.assertRaisesRegex(RuntimeError, "Source Volume"):
                storage.apply_filesystem_changes(self.vault, {(Change.modified, str(self.root / "photo.jpg"))})

    def test_substituted_symlink_cannot_be_opened(self):
        target = self.root / "race.bin"
        target.write_bytes(b"x")
        real_open = os.open
        def swapped(path, *args, **kwargs):
            target.unlink()
            target.symlink_to(self.path)
            return real_open(path, *args, **kwargs)
        with patch("app.services.fs_preflight.os.open", side_effect=swapped):
            with self.assertRaises(OSError):
                inspect_entry(target)
