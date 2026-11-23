"""#723 Delayed Cloud Deletion of folders, mixed selections and list views.

Real admission on a disposable PostgreSQL 16 database (fixtures of
``tests.test_cloud_purge``). A folder, multi-item or list selection covers
only its eligible files and counts the rest per reason; one exact file and a
Stop Protection stay all-or-nothing. Cloud Duplicates are Cloud-only files
whose content is still present and protected at another path of the Vault.
The confirmation schedules exactly what the preview listed (fingerprint).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app import storage
from app.catalog import ArchiveCatalog
from app.services import cloud_deletion
from tests import test_cloud_deletion as fixtures

PHOTO = b"photo-bytes-723"
ORIGINAL = b"only-in-the-cloud-723"


class SelectionFixture(fixtures.CloudDeletionFixture):

    def put(self, path: str, payload: bytes, *, upload: bool = True) -> str:
        """A present file at ``path``; uploaded (protected) unless told otherwise."""
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        stat = target.stat()
        with self.db() as connection:
            file_id = ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path=path, file_type="regular", size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())
            if not upload:
                return str(file_id)
            job = connection.execute(
                "INSERT INTO jobs(vault_id, file_id, action, status, origin, total_bytes) "
                "VALUES (2, %s, 'upload', 'queued', 'manual', %s) RETURNING id",
                (file_id, stat.st_size)).fetchone()
            connection.execute(
                "INSERT INTO job_uploads(job_id, verification_mode) VALUES (%s, 's3_checksum')",
                (job["id"],))
        self.run_job(int(job["id"]))
        self.assertEqual(self.job(int(job["id"]))["status"], "completed")
        return str(file_id)

    def lose(self, path: str, file_id: str) -> None:
        (self.source / path).unlink()
        with self.db() as connection:
            ArchiveCatalog(connection).mark_local_copy_missing(file_id, observed_at=storage.now_iso())

    def cloud_only_at(self, path: str, payload: bytes) -> str:
        file_id = self.put(path, payload)
        self.lose(path, file_id)
        return file_id

    def preview(self, **selection) -> cloud_deletion.DeletionPreview:
        selection.setdefault("paths", [])
        with self.db() as connection:
            return cloud_deletion.preview_selection(connection, vault_id=2, **selection)

    def scheduled_paths(self, group_id: str) -> list[str]:
        return [r["path"] for r in self.rows(
            """SELECT f.path FROM jobs j JOIN files f ON f.id=j.file_id
               WHERE j.group_id=%s ORDER BY f.path""", group_id)]


class FolderSelectionTests(SelectionFixture):

    def library(self) -> None:
        """An Immich-like tree: a moved photo, an original only in the cloud,
        a protected photo and a pending local file (report.txt at the root)."""
        self.put("library/2026/new/photo.jpg", PHOTO)                 # protected
        self.cloud_only_at("library/2026/old/photo.jpg", PHOTO)       # Cloud Duplicate
        self.cloud_only_at("library/2026/old/original.jpg", ORIGINAL)  # no local copy
        self.put("library/2026/old/pending.jpg", b"not uploaded", upload=False)

    def test_a_mixed_folder_covers_only_its_eligible_files(self) -> None:
        self.library()
        preview = self.preview(directories=["library"])
        self.assertEqual(preview.mode, "selection")
        self.assertEqual((preview.eligible_count, preview.duplicate_count,
                          preview.no_local_copy_count), (2, 1, 1))
        self.assertEqual(preview.eligible_bytes, len(PHOTO) + len(ORIGINAL))
        self.assertEqual(preview.skipped, {"present_locally": 2})
        self.assertEqual(preview.as_dict()["skipped_count"], 2)
        self.assertEqual((preview.object_count, preview.byte_count),
                         (2, len(PHOTO) + len(ORIGINAL)))

        only = self.preview(directories=["library"], duplicates_only=True)
        self.assertEqual((only.object_count, only.byte_count), (1, len(PHOTO)))
        self.assertNotEqual(only.fingerprint, preview.fingerprint)

        scheduled = self.schedule(paths=[], directories=["library"], duplicates_only=True)
        self.assertEqual(self.scheduled_paths(scheduled.group_id), ["library/2026/old/photo.jpg"])
        [event] = self.rows(
            "SELECT detail FROM audit_events WHERE event='cloud_deletion.requested'")
        self.assertTrue(event["detail"]["preview"]["duplicates_only"])
        self.assertEqual(event["detail"]["preview"]["skipped"]["present_locally"], 2)

    def test_files_and_folders_mix_and_an_empty_selection_previews_zero(self) -> None:
        self.library()
        other = self.cloud_only_at("elsewhere/lonely.bin", b"lonely")
        mixed = self.preview(paths=["report.txt", "elsewhere/lonely.bin"],
                             directories=["library/2026/old"])
        self.assertEqual((mixed.eligible_count, mixed.skipped),
                         (3, {"present_locally": 2}))
        nothing = self.preview(directories=["library/2026/new"])
        self.assertEqual((nothing.object_count, nothing.eligible_count), (0, 0))
        with self.assertRaisesRegex(cloud_deletion.CloudDeletionError, "No Cloud-only"):
            self.schedule(paths=[], directories=["library/2026/new"])
        self.assertTrue(other)

    def test_skip_reasons_are_counted_without_failing_the_selection(self) -> None:
        busy = self.cloud_only_at("album/busy.jpg", b"busy")
        unverified = self.cloud_only_at("album/unverified.jpg", b"unverified")
        self.cloud_only_at("album/fine.jpg", b"fine")
        with self.db() as connection:
            current = connection.execute(
                "SELECT current_version_id FROM files WHERE id=%s", (busy,)).fetchone()
            connection.execute(
                "INSERT INTO jobs(vault_id, file_id, archive_version_id, action, status, origin) "
                "VALUES (2, %s, %s, 'recover', 'queued', 'manual')",
                (busy, current["current_version_id"]))
            connection.execute(
                """UPDATE archive_versions SET integrity='unverified'
                   WHERE id=(SELECT current_version_id FROM files WHERE id=%s)""", (unverified,))
        preview = self.preview(directories=["album"])
        self.assertEqual(preview.skipped, {"pending_work": 1, "not_verified": 1})
        scheduled = self.schedule(paths=[], directories=["album"])
        self.assertEqual(self.scheduled_paths(scheduled.group_id), ["album/fine.jpg"])

    def test_a_present_entry_the_catalog_has_not_seen_is_skipped(self) -> None:
        self.cloud_only_at("album/back.jpg", b"back")
        self.cloud_only_at("album/gone.jpg", b"gone")
        (self.source / "album/back.jpg").write_bytes(b"back")  # not yet observed
        preview = self.preview(directories=["album"])
        self.assertEqual((preview.eligible_count, preview.skipped), (1, {"present_locally": 1}))

    def test_the_confirmation_must_present_the_previewed_fingerprint(self) -> None:
        self.library()
        preview = self.preview(directories=["library"])
        self.cloud_only_at("library/2026/old/late.jpg", b"arrived after the preview")

        def confirm(fingerprint):
            with self.db() as connection:
                return cloud_deletion.schedule_cloud_purge(
                    connection, vault_id=2, paths=[], directories=["library"],
                    is_directory=False, actor="owner", requested_at=fixtures.purge.PAST,
                    confirmation="Docs", reason="cleanup", fingerprint=fingerprint)
        for fingerprint in (preview.fingerprint, None, "0" * 64):
            with self.subTest(fingerprint=fingerprint), self.assertRaises(
                    cloud_deletion.SelectionChanged):
                confirm(fingerprint)
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)
        fresh = self.preview(directories=["library"])
        self.assertEqual(len(confirm(fresh.fingerprint).job_ids), 3)


class DuplicateDefinitionTests(SelectionFixture):

    def test_an_unprotected_present_copy_is_not_a_duplicate_until_protected(self) -> None:
        self.cloud_only_at("old/photo.jpg", PHOTO)
        file_id = self.put("new/photo.jpg", PHOTO, upload=False)  # present, never uploaded
        self.assertEqual(self.preview(directories=["old"]).duplicate_count, 0)
        with self.db() as connection:
            job = connection.execute(
                "INSERT INTO jobs(vault_id, file_id, action, status, origin, total_bytes) "
                "VALUES (2, %s, 'upload', 'queued', 'manual', %s) RETURNING id",
                (file_id, len(PHOTO))).fetchone()
            connection.execute(
                "INSERT INTO job_uploads(job_id, verification_mode) VALUES (%s, 's3_checksum')",
                (job["id"],))
        self.run_job(int(job["id"]))
        self.assertEqual(self.preview(directories=["old"]).duplicate_count, 1)
        with self.db() as connection:
            connection.execute(
                """UPDATE archive_versions SET availability='missing'
                   WHERE id=(SELECT current_version_id FROM files WHERE id=%s)""", (file_id,))
        self.assertEqual(self.preview(directories=["old"]).duplicate_count, 0,
                         "an unavailable snapshot does not protect the other copy")

    def test_another_cloud_only_copy_or_the_file_itself_is_not_a_duplicate(self) -> None:
        self.cloud_only_at("a/photo.jpg", PHOTO)
        self.cloud_only_at("b/photo.jpg", PHOTO)
        preview = self.preview(directories=["a", "b"])
        self.assertEqual((preview.eligible_count, preview.duplicate_count), (2, 0))
        only = self.preview(directories=["a", "b"], duplicates_only=True)
        self.assertEqual(only.object_count, 0)

    def test_one_exact_file_stays_strict_and_honours_duplicates_only(self) -> None:
        self.cloud_only_at("old/original.jpg", ORIGINAL)
        exact = self.preview(paths=["old/original.jpg"])
        self.assertEqual((exact.mode, exact.object_count, exact.duplicate_count), ("exact", 1, 0))
        with self.assertRaisesRegex(cloud_deletion.CloudDeletionError, "Cloud Duplicate"):
            self.preview(paths=["old/original.jpg"], duplicates_only=True)
        with self.assertRaisesRegex(cloud_deletion.CloudDeletionError, "Only Cloud-only"):
            self.preview(paths=["report.txt"])  # present: an exact file still fails
        # Without a fingerprint the exact flow schedules as before.
        self.assertEqual(len(self.schedule(paths=["old/original.jpg"]).job_ids), 1)

    def test_stop_protection_stays_all_or_nothing(self) -> None:
        self.cloud_only_at("old/original.jpg", ORIGINAL)  # report.txt is still present
        with self.assertRaises(cloud_deletion.CloudDeletionError):
            self.stop("delete")
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)


class ListingSelectionTests(SelectionFixture):

    def test_a_list_view_resolves_like_the_file_list(self) -> None:
        self.put("library/2026/new/photo.jpg", PHOTO)
        self.cloud_only_at("library/2026/old/photo.jpg", PHOTO)
        self.cloud_only_at("library/2026/old/original.jpg", ORIGINAL)
        self.cloud_only_at("top.bin", b"top")
        listing = cloud_deletion.ListingFilter
        root = self.preview(listing=listing(state="cloud_only"))
        self.assertEqual(root.eligible_count, 3)
        # report.txt is not in the Cloud-only view; the library folder is, whole.
        self.assertEqual(root.skipped, {"present_locally": 1})
        everything = self.preview(listing=listing())
        self.assertEqual((everything.eligible_count, everything.skipped),
                         (3, {"present_locally": 2}))
        folder = self.preview(listing=listing(directory="library/2026", state="cloud_only"))
        self.assertEqual(folder.eligible_count, 2, "only the folder holding Cloud-only files")
        self.assertEqual(folder.skipped, {})
        search = self.preview(listing=listing(q="original"))
        self.assertEqual(search.eligible_count, 1)
        with self.assertRaises(cloud_deletion.CloudDeletionError):
            self.preview(listing=listing(state="bogus"))
        scheduled = self.schedule(paths=[], listing=listing(state="cloud_only"),
                                  duplicates_only=True)
        self.assertEqual(self.scheduled_paths(scheduled.group_id), ["library/2026/old/photo.jpg"])


class SelectionHttpTests(SelectionFixture):

    owner = {}

    def setUp(self):
        super().setUp()
        self.client = self.http_client()
        self.headers = self.sign_in(
            reauthenticated_until=datetime.now(timezone.utc).isoformat()).csrf_headers

    def test_preview_then_confirm_and_a_stale_fingerprint_is_409(self) -> None:
        self.put("library/new/photo.jpg", PHOTO)
        self.cloud_only_at("library/old/photo.jpg", PHOTO)
        self.cloud_only_at("library/old/original.jpg", ORIGINAL)
        body = {"directories": ["library"], "paths": ["report.txt"], "duplicates_only": True}
        preview = self.client.post("/api/cloud-deletion/preview", json=body, headers=self.headers)
        self.assertEqual(preview.status_code, 200, preview.text)
        data = preview.json()
        self.assertEqual((data["object_count"], data["duplicate_count"], data["no_local_copy_count"],
                          data["skipped"]["present_locally"], data["mode"]),
                         (1, 1, 1, 2, "selection"))
        confirm = {**body, "confirmation": "Docs", "reason": "moved by Immich"}
        stale = self.client.post("/api/cloud-purge", json={**confirm, "fingerprint": "f" * 64},
                                 headers={**self.headers, "Accept-Language": "it"})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["message_key"], "api.cloud_purge_selection_changed")
        missing = self.client.post("/api/cloud-purge", json=confirm, headers=self.headers)
        self.assertEqual(missing.status_code, 409, missing.text)
        done = self.client.post("/api/cloud-purge", json={**confirm, "fingerprint": data["fingerprint"]},
                                headers=self.headers)
        self.assertEqual(done.status_code, 202, done.text)
        self.assertEqual(len(done.json()["job_ids"]), 1)

    def test_a_list_view_and_an_empty_request(self) -> None:
        self.cloud_only_at("top.bin", b"top")
        response = self.client.post("/api/cloud-deletion/preview",
                                    json={"listing": {"state": "cloud_only"}}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["eligible_count"], 1)
        empty = self.client.post("/api/cloud-deletion/preview", json={}, headers=self.headers)
        self.assertEqual(empty.status_code, 422, empty.text)
        single = self.client.post("/api/cloud-deletion/preview", json={"path": "report.txt"},
                                  headers=self.headers)
        self.assertEqual(single.status_code, 409, single.text)


if __name__ == "__main__":
    unittest.main()
