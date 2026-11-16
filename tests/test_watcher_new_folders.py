"""A folder created where the watcher reports no ``added`` event (#721).

On an Unraid ``fuse.shfs`` source Immich creates ``library/<user>/<year>/<date>/``
and renames a file into it, and the watcher sees at most a ``modified`` of the
folder or of its parent. Every directory event therefore lists that directory
one level deep: child files the catalog does not know are observed, and child
folders with no catalogued file are walked like an added folder.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from watchfiles import Change

from app import storage
from app.services import watch_plan
from app.services.excluded_folders import excluded_folders
from app.services.operation_policies import OperationPolicy
from tests import test_auto_upload_watcher as fixtures


class NewFolderTests(unittest.TestCase):
    setUp = fixtures.WatcherAutomaticUploadTests.setUp
    db = fixtures.WatcherAutomaticUploadTests.db
    advance = fixtures.WatcherAutomaticUploadTests.advance
    write = fixtures.WatcherAutomaticUploadTests.write

    def watch(self, *events: tuple[Change, str]) -> int:
        return storage.apply_filesystem_changes(
            self.vault, {(change, str(self.root / path)) for change, path in events})

    def catalogued(self) -> dict[str, str | None]:
        with self.db() as connection:
            return {row["path"]: row["local_presence"] for row in connection.execute(
                "SELECT path, local_presence FROM files WHERE vault_id=2").fetchall()}

    def existing_year(self) -> None:
        self.write("library/Paolo/2026/2026-11-15/old.heic", b"old")
        self.advance(60)

    def immich_moves_a_photo_into_a_new_day(self) -> None:
        day = self.root / "library/Paolo/2026/2026-11-16"
        day.mkdir()
        upload = self.root / "upload/IMG.heic"
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"new photo")
        upload.rename(day / "IMG.heic")

    def test_a_new_folder_reported_as_a_modified_parent_is_catalogued(self):
        self.existing_year()
        self.immich_moves_a_photo_into_a_new_day()
        with self.assertLogs("uvicorn.error", level="INFO") as logs:
            changed = self.watch((Change.modified, "library/Paolo/2026"))
        self.assertEqual(self.catalogued().get("library/Paolo/2026/2026-11-16/IMG.heic"), "present")
        self.assertEqual(changed, 1)
        self.assertTrue(any(
            "new folder found: library/Paolo/2026/2026-11-16, 1 files" in line for line in logs.output
        ), logs.output)

    def test_a_new_folder_reported_as_its_own_modification_is_catalogued(self):
        self.existing_year()
        self.immich_moves_a_photo_into_a_new_day()
        self.watch((Change.modified, "library/Paolo/2026/2026-11-16"))
        self.assertEqual(self.catalogued().get("library/Paolo/2026/2026-11-16/IMG.heic"), "present")

    def test_nested_new_folders_under_a_modified_root_are_walked(self):
        self.immich_moves_a_photo_into_a_new_day_from_nothing()
        self.watch((Change.modified, "."))
        self.assertEqual(self.catalogued().get("library/Paolo/2026/2026-11-16/IMG.heic"), "present")

    def immich_moves_a_photo_into_a_new_day_from_nothing(self) -> None:
        day = self.root / "library/Paolo/2026/2026-11-16"
        day.mkdir(parents=True)
        (day / "IMG.heic").write_bytes(b"new photo")

    def test_an_unknown_file_beside_a_modified_folder_event_is_observed(self):
        self.existing_year()
        (self.root / "library/Paolo/2026/2026-11-15/second.heic").write_bytes(b"second")
        self.watch((Change.modified, "library/Paolo/2026/2026-11-15"))
        self.assertEqual(self.catalogued().get("library/Paolo/2026/2026-11-15/second.heic"), "present")

    def test_a_catalogued_folder_is_not_walked_again(self):
        self.existing_year()
        self.write("library/Paolo/2026/top.heic", b"top")
        # A file that raised no event of its own inside a catalogued subfolder:
        # a metadata event of the parent does not walk known subtrees.
        hidden = self.root / "library/Paolo/2026/2026-11-15/hidden.heic"
        hidden.write_bytes(b"hidden")
        with patch("app.storage._scan_tree", wraps=storage._scan_tree) as walk:
            changed = self.watch((Change.modified, "library/Paolo/2026"))
        walk.assert_not_called()
        self.assertEqual(changed, 0)
        self.assertNotIn("library/Paolo/2026/2026-11-15/hidden.heic", self.catalogued())

    def test_an_unchanged_catalogued_file_is_not_observed_again(self):
        self.existing_year()
        with self.db() as connection:
            before = connection.execute(
                "SELECT generation, local_observed_at FROM files WHERE path=%s",
                ("library/Paolo/2026/2026-11-15/old.heic",)).fetchone()
        self.advance(60)
        self.assertEqual(self.watch((Change.modified, "library/Paolo/2026/2026-11-15")), 0)
        with self.db() as connection:
            after = connection.execute(
                "SELECT generation, local_observed_at FROM files WHERE path=%s",
                ("library/Paolo/2026/2026-11-15/old.heic",)).fetchone()
        self.assertEqual(after, before)

    def test_an_excluded_child_folder_is_not_entered(self):
        self.existing_year()
        private = self.root / "library/Paolo/2026/private"
        private.mkdir()
        (private / "secret.heic").write_bytes(b"secret")
        rules = excluded_folders(OperationPolicy(include_globs=(), exclude_globs=("**/private/**",)))
        self.addCleanup(watch_plan.forget, 2)
        with patch("app.storage.excluded_folder_rules.load", return_value=rules):
            self.watch((Change.modified, "library/Paolo/2026"))
        self.assertNotIn("library/Paolo/2026/private/secret.heic", self.catalogued())
        self.assertIn("library/Paolo/2026/private", watch_plan.unobserved(2).excluded)

    def test_a_symlinked_child_folder_is_not_followed(self):
        self.existing_year()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "elsewhere.heic").write_bytes(b"elsewhere")
        os.symlink(outside, self.root / "library/Paolo/2026/link", target_is_directory=True)
        self.watch((Change.modified, "library/Paolo/2026"))
        catalogued = self.catalogued()
        self.assertFalse([path for path in catalogued if path.startswith("library/Paolo/2026/link/")])

    def test_a_restore_temporary_child_is_ignored(self):
        self.existing_year()
        name = ".old.heic.restore-" + "0" * 32 + ".tmp"
        (self.root / "library/Paolo/2026/2026-11-15" / name).write_bytes(b"partial")
        self.assertEqual(self.watch((Change.modified, "library/Paolo/2026/2026-11-15")), 0)
        self.assertNotIn(f"library/Paolo/2026/2026-11-15/{name}", self.catalogued())


class WatcherDiscoveryTests(unittest.TestCase):
    """The watcher hands a folder found this way to a successor observer."""

    setUp = fixtures.WatcherAutomaticUploadTests.setUp
    db = fixtures.WatcherAutomaticUploadTests.db
    write = fixtures.WatcherAutomaticUploadTests.write

    def test_unknown_folders_under_a_modified_folder_become_added_directories(self):
        self.write("library/known/old.heic", b"old")
        (self.root / "library/new/deeper").mkdir(parents=True)
        (self.root / "library/empty").mkdir()
        handed_off: set[tuple[int, int]] = set()
        found = storage._unknown_watcher_directories(
            2, self.root, {(Change.modified, str(self.root / "library"))}, handed_off)
        self.assertEqual(found, {
            (Change.added, str(self.root / "library/new")),
            (Change.added, str(self.root / "library/empty")),
        })
        # The same folder inode is handed off once per watcher run.
        self.assertEqual(storage._unknown_watcher_directories(
            2, self.root, {(Change.modified, str(self.root / "library"))}, handed_off), set())
        # A folder recreated under the same name is another inode.
        (self.root / "library/empty").rmdir()
        (self.root / "library/empty").mkdir()
        self.assertEqual(storage._unknown_watcher_directories(
            2, self.root, {(Change.modified, str(self.root / "library"))}, handed_off),
            {(Change.added, str(self.root / "library/empty"))})

    def test_file_and_deletion_events_list_nothing(self):
        (self.root / "library/new").mkdir(parents=True)
        self.assertEqual(storage._unknown_watcher_directories(2, self.root, {
            (Change.deleted, str(self.root / "library")),
            (Change.added, str(self.root / "library/new/file.heic")),
        }, set()), set())


class WatcherBatchLogTests(unittest.TestCase):
    def test_the_raw_batch_is_logged_at_debug_and_truncated(self):
        changes = {(Change.modified, f"/source/file-{index:03}.heic") for index in range(60)}
        with self.assertLogs("uvicorn.error", level="DEBUG") as logs:
            storage._log_watcher_batch(2, changes)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "DEBUG")
        message = logs.records[0].getMessage()
        self.assertIn("watcher batch of 60 changes", message)
        self.assertIn("modified /source/file-000.heic", message)
        self.assertNotIn("file-059", message)
        self.assertIn("(and 10 more)", message)


if __name__ == "__main__":
    unittest.main()
