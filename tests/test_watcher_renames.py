"""#621: the watcher decides renames and reappearances by digest, without a scan.

The watcher reports additions and deletions, never a move. Before automatic
upload admits a stable new Local Copy, its digest decides whether it is a
renamed Vault File (ADR-0001) or content back at a cloud-only path
(ADR-0016). Real watcher event path (``apply_filesystem_changes``), automatic
admission and Job worker on disposable PostgreSQL 16 with the synthetic
``VersionedProvider``; no full scan.
"""
from __future__ import annotations

import hashlib
import os
import unittest
from unittest.mock import patch

from watchfiles import Change

from app import storage
from app.services import catalog_incidents
from app.services import notifications as notification_service
from tests import test_auto_upload_watcher as watcher_fixtures
from tests.upload_provenance_provider import VersionedProvider


class WatcherRenameTests(unittest.TestCase):
    setUp = watcher_fixtures.WatcherAutomaticUploadTests.setUp
    db = watcher_fixtures.WatcherAutomaticUploadTests.db
    advance = watcher_fixtures.WatcherAutomaticUploadTests.advance
    write = watcher_fixtures.WatcherAutomaticUploadTests.write
    queue = watcher_fixtures.WatcherAutomaticUploadTests.queue
    jobs = watcher_fixtures.WatcherAutomaticUploadTests.jobs

    def run_jobs(self, provider: VersionedProvider) -> int:
        processed = 0
        with (
            patch("app.storage.scan_vault", side_effect=AssertionError("must not scan")),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client", return_value=provider),
            patch("app.storage.upload_file", side_effect=provider.upload),
        ):
            while storage.process_jobs_once():
                processed += 1
        return processed

    def watch(self, *events: tuple[Change, str]) -> None:
        storage.apply_filesystem_changes(
            self.vault, {(change, str(self.root / path)) for change, path in events})

    def protect_by_watcher(self, provider: VersionedProvider, contents: dict[str, bytes]) -> dict:
        for name, payload in contents.items():
            self.write(name, payload)
        self.advance(300)
        self.assertEqual(self.queue(), len(contents))
        self.assertEqual(self.run_jobs(provider), len(contents))
        self.assertEqual(provider.puts, len(contents))
        return {name: self.file(name) for name in contents}

    def file(self, path: str) -> dict | None:
        with self.db() as connection:
            return connection.execute(
                """SELECT f.*, cur.object_key, cur.provider_version_id AS current_provider_version,
                          cur.plaintext_sha256 AS current_sha256
                   FROM files f LEFT JOIN archive_versions cur ON cur.id=f.current_version_id
                   WHERE f.vault_id=2 AND f.path=%s""", (path,)).fetchone()

    def history(self, file_id) -> list[str]:
        with self.db() as connection:
            return [row["path"] for row in connection.execute(
                "SELECT path FROM file_path_history WHERE file_id=%s ORDER BY valid_to",
                (file_id,)).fetchall()]

    def live_keys(self, provider: VersionedProvider) -> list[str]:
        return sorted(key for key, _version in provider.objects)

    def assert_moved(self, provider, before: dict, old: str, new: str) -> None:
        moved = self.file(new)
        self.assertIsNotNone(moved, new)
        self.assertEqual(moved["id"], before["id"], "the Vault File keeps its identity")
        self.assertIsNone(self.file(old), "no cloud-only copy is left at the old path")
        self.assertEqual(self.history(before["id"]), [old])
        self.assertTrue(moved["object_key"].endswith("/" + new), moved["object_key"])
        self.assertEqual(moved["matched_version_id"], moved["current_version_id"])
        self.assertIn((before["object_key"], before["current_provider_version"]), provider.deleted)

    def open_problems(self) -> list[dict]:
        with self.db() as connection:
            notification_service.reconcile_problem_states(connection)
            return connection.execute(
                "SELECT event, vault_id, message_params FROM notifications "
                "WHERE problem_status='open' AND in_app_enabled ORDER BY id").fetchall()

    # -- renames -------------------------------------------------------------

    def test_moved_protected_file_is_renamed_not_uploaded_again(self):
        provider = VersionedProvider()
        before = self.protect_by_watcher(provider, {"d/ren-a.bin": b"rename me"})["d/ren-a.bin"]
        os.replace(self.root / "d/ren-a.bin", self.root / "d/ren-b.bin")
        self.watch((Change.deleted, "d/ren-a.bin"), (Change.added, "d/ren-b.bin"))
        # Not stable yet: neither hashed nor uploaded.
        self.assertEqual(self.queue(), 0)
        self.assertIsNotNone(self.file("d/ren-a.bin"))
        self.advance(300)
        self.assertEqual(self.queue(), 0, "a rename is never admitted as a new upload")
        self.assertEqual([(j["action"], j["status"]) for j in self.jobs()],
                         [("upload", "completed"), ("rename", "queued")])
        self.assertEqual(self.run_jobs(provider), 1)
        self.assertEqual(self.jobs()[-1]["status"], "completed", self.jobs()[-1]["message"])
        self.assert_moved(provider, before, "d/ren-a.bin", "d/ren-b.bin")
        self.assertEqual(provider.puts, 1, "no second PUT")
        self.assertEqual(provider.copies, 1)
        self.assertEqual(len(provider.objects), 1, "one version, at the new key")
        self.assertTrue(self.live_keys(provider)[0].endswith("/d/ren-b.bin"))
        self.advance(300)
        self.assertEqual(self.queue(), 0)

    def test_moved_folder_of_three_files_keeps_every_identity(self):
        provider = VersionedProvider()
        contents = {f"d4/cx/{name}": name.encode() * 3 for name in ("one", "two", "six")}
        before = self.protect_by_watcher(provider, contents)
        os.replace(self.root / "d4/cx", self.root / "d4/cy")
        self.watch((Change.deleted, "d4/cx"), (Change.added, "d4/cy"))
        self.advance(300)
        self.assertEqual(self.queue(), 0)
        self.assertEqual(sorted(j["action"] for j in self.jobs() if j["status"] == "queued"),
                         ["rename"] * 3)
        self.assertEqual(self.run_jobs(provider), 3)
        for old, row in before.items():
            self.assert_moved(provider, row, old, old.replace("d4/cx/", "d4/cy/"))
        self.assertEqual((provider.puts, provider.copies), (3, 3))
        self.assertEqual(len(provider.objects), 3)
        self.assertTrue(all("/d4/cy/" in key for key in self.live_keys(provider)))

    def test_same_size_different_content_is_still_a_new_file(self):
        provider = VersionedProvider()
        before = self.protect_by_watcher(provider, {"a.bin": b"aaaa"})["a.bin"]
        (self.root / "a.bin").unlink()  # disposable fixture only
        self.watch((Change.deleted, "a.bin"))
        self.write("b.bin", b"bbbb")
        self.advance(300)
        self.assertEqual(self.queue(), 1)
        self.assertEqual(self.run_jobs(provider), 1)
        self.assertNotEqual(self.file("b.bin")["id"], before["id"])
        self.assertEqual(self.file("a.bin")["id"], before["id"])
        self.assertEqual(provider.puts, 2)
        self.assertEqual(provider.deleted, [])

    def test_two_equal_copies_of_a_moved_file_are_ambiguous(self):
        provider = VersionedProvider()
        before = self.protect_by_watcher(provider, {"a.bin": b"twin"})["a.bin"]
        os.replace(self.root / "a.bin", self.root / "b.bin")
        self.write("c.bin", b"twin", watch=False)
        self.watch((Change.deleted, "a.bin"), (Change.added, "b.bin"), (Change.added, "c.bin"))
        self.advance(300)
        self.assertEqual(self.queue(), 2, "ambiguity is two new files, never a merge")
        self.assertEqual(self.file("a.bin")["id"], before["id"])
        self.assertEqual(self.history(before["id"]), [])

    def test_an_unhashed_possible_rename_is_never_uploaded_first(self):
        provider = VersionedProvider()
        self.protect_by_watcher(provider, {"a.bin": b"same"})
        os.replace(self.root / "a.bin", self.root / "b.bin")
        self.watch((Change.deleted, "a.bin"), (Change.added, "b.bin"))
        self.advance(300)
        with patch("app.storage.hash_stable_regular_file", side_effect=PermissionError("denied")):
            self.assertEqual(self.queue(), 0, "no upload before the rename analysis")
            self.advance(300)
            self.assertEqual(self.queue(), 0)
        self.assertEqual([p["event"] for p in self.open_problems()], ["file_unreadable"])
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.queue(), 0)
        self.assertEqual([j["action"] for j in self.jobs()], ["upload", "rename"])

    # -- content back at a cloud-only path -----------------------------------

    def test_identical_content_recreated_is_protected_without_a_scan(self):
        provider = VersionedProvider()
        before = self.protect_by_watcher(provider, {"photo.jpg": b"photo"})["photo.jpg"]
        (self.root / "photo.jpg").unlink()  # disposable fixture only
        self.watch((Change.deleted, "photo.jpg"))
        self.write("photo.jpg", b"photo")
        self.assertEqual(self.file("photo.jpg")["held_version_id"], before["current_version_id"])
        self.advance(300)
        self.assertEqual(self.queue(), 0)
        row = self.file("photo.jpg")
        self.assertIsNone(row["held_version_id"])
        self.assertEqual(row["matched_version_id"], before["current_version_id"])
        self.assertEqual(bytes(row["local_sha256"]), hashlib.sha256(b"photo").digest())
        self.assertEqual(self.open_problems(), [])
        self.assertEqual(provider.puts, 1)

    def test_different_content_opens_a_problem_and_notifies_once(self):
        provider = VersionedProvider()
        before = self.protect_by_watcher(provider, {"photo.jpg": b"photo"})["photo.jpg"]
        (self.root / "photo.jpg").unlink()  # disposable fixture only
        self.watch((Change.deleted, "photo.jpg"))
        self.write("photo.jpg", b"different")
        self.advance(300)
        self.assertEqual(self.queue(), 0)
        self.assertEqual(self.file("photo.jpg")["held_version_id"], before["current_version_id"])
        problems = self.open_problems()
        self.assertEqual([(p["event"], p["vault_id"]) for p in problems],
                         [("cloud_only_conflict", 2)])
        self.assertEqual(problems[0]["message_params"]["path"], "photo.jpg")
        with self.db() as connection:
            self.assertEqual(notification_service.count_open_problems(connection), 1)
        self.advance(300)
        self.assertEqual(self.queue(), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) AS n FROM notifications WHERE event='cloud_only_conflict'"
            ).fetchone()["n"], 1, "one notification per conflict")
            catalog_incidents.confirm_cloud_only_replacement(
                connection, vault_id=2, path="photo.jpg", actor="owner")
        self.assertEqual(self.open_problems(), [])
        self.assertEqual(provider.puts, 1)


if __name__ == "__main__":
    unittest.main()
