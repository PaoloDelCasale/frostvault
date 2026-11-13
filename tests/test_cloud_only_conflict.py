"""Different content at the path of a cloud-only file must not replace it (#411).

Rebuilt model (#593, ADR-0035): the file's Current is the only copy after
its Local Copy was freed. New content observed at the same path is held
(``files.held_version_id``); the scan opens one ``cloud_only_conflict``
Owner problem (no incident table). Identical content is matched again. Only
the Owner's explicit confirmation releases the hold, and the upload then
replaces Current only after the new version is verified.

Real scan (``storage.apply_auto_renames``), automatic admission and upload
worker on disposable PostgreSQL 16 with the synthetic ``VersionedProvider``.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace

from fastapi import HTTPException

from app import storage
from app.catalog import ArchiveCatalog
from app.config import settings
from app.main import confirm_cloud_only_replacement, queue_jobs
from app.services import notifications as notification_service
from app.services.email_templates import render_email
from app.services.operation_policies import OperationPolicy, queue_auto_uploads, set_policy
from app.services.vaults import VAULT_ROW_SQL
from tests import test_upload_worker as fixtures
from tests.test_upload_worker import A, B, sha
from unittest.mock import patch

PATH = "report.txt"


class CloudOnlyPathConflictTests(unittest.TestCase):
    db = fixtures.UploadWorkerTests.db
    observe = fixtures.UploadWorkerTests.observe
    write = fixtures.UploadWorkerTests.write
    queue = fixtures.UploadWorkerTests.queue
    claim = fixtures.UploadWorkerTests.claim
    run_job = fixtures.UploadWorkerTests.run_job
    job = fixtures.UploadWorkerTests.job
    file_row = fixtures.UploadWorkerTests.file_row
    rows = fixtures.UploadWorkerTests.rows
    version = fixtures.UploadWorkerTests.version
    assert_current = fixtures.UploadWorkerTests.assert_current

    def setUp(self) -> None:
        fixtures.UploadWorkerTests.setUp(self)
        # The original is protected, then freed: the cloud holds the only copy.
        first = self.queue()
        self.run_job(first)
        self.original = self.assert_current(first, A)
        self.file.unlink()
        with self.db() as connection:
            ArchiveCatalog(connection).mark_local_copy_missing(
                self.file_id, observed_at=storage.now_iso())
            set_policy(connection, 2, OperationPolicy(auto_upload=True, stability_seconds=0))

    # -- helpers -------------------------------------------------------------

    def vault(self) -> dict:
        with self.db() as connection:
            return connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()

    def scan(self) -> int:
        """Hash Local Copies, settle reappearances, queue automatic uploads."""
        storage.apply_auto_renames(self.vault())
        with self.db() as connection:
            return queue_auto_uploads(connection, vault_id=2, source_root=str(self.source))

    def run_queued(self) -> list[int]:
        ids = [r["id"] for r in self.rows(
            "SELECT id FROM jobs WHERE action='upload' AND status='queued' ORDER BY id")]
        for job_id in ids:
            self.run_job(job_id)
        return ids

    def uploads(self) -> list[str]:
        return [r["status"] for r in self.rows("SELECT status FROM jobs WHERE action='upload' ORDER BY id")]

    def problems(self) -> list[tuple[str, str]]:
        with self.db() as connection:
            notification_service.reconcile_problem_states(connection)
            return [(r["event"], r["problem_status"]) for r in connection.execute(
                "SELECT event, problem_status FROM notifications "
                "WHERE event='cloud_only_conflict' ORDER BY id").fetchall()]

    def assert_original_kept(self) -> None:
        row = self.file_row()
        self.assertEqual(row["current_version_id"], self.original["id"])
        self.assertEqual(self.provider.deleted, [], "the only cloud copy must survive")
        self.assertIn((self.key, self.original["provider_version_id"]), self.provider.objects)

    # -- tests ---------------------------------------------------------------

    def test_different_content_is_not_uploaded_and_opens_a_conflict(self) -> None:
        self.write(B)
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.file_row()["held_version_id"], self.original["id"])
        with self.assertRaises(HTTPException) as manual:
            queue_jobs(PATH, "upload", 2, 1)
        self.assertEqual(manual.exception.status_code, 409)
        self.assertEqual(self.run_queued(), [])
        self.assertEqual(self.uploads(), ["completed"], "only the original upload")
        self.assert_original_kept()
        self.assertEqual(self.problems(), [("cloud_only_conflict", "open")])
        notifications = self.rows("SELECT title_key, in_app_enabled FROM notifications "
                                  "WHERE event='cloud_only_conflict'")
        self.assertEqual([(r["title_key"], r["in_app_enabled"]) for r in notifications],
                         [("notification.cloud_only_conflict.title", True)])
        # A second scan keeps one conflict and one notification.
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.problems(), [("cloud_only_conflict", "open")])

    def test_conflict_is_emailed_immediately_when_smtp_is_configured(self) -> None:
        email_settings = replace(settings, smtp_host="smtp.example.com",
                                 smtp_from_address="frostvault@example.com",
                                 smtp_to_address="owner@example.com")
        with patch.object(notification_service, "settings", email_settings):
            self.write(B)
            self.scan()
        channels = self.rows("""SELECT d.channel FROM notification_deliveries d
                                JOIN notifications n ON n.id=d.notification_id
                                WHERE n.event='cloud_only_conflict' ORDER BY d.channel""")
        self.assertIn("email", [r["channel"] for r in channels])
        rendered = render_email("cloud_only_conflict", vault_id=2)
        self.assertIn("only cloud copy", rendered["body"])

    def test_upload_queued_before_the_hold_does_not_replace_the_copy(self) -> None:
        # A Job admitted while the path was empty runs after new bytes appear.
        self.file.write_bytes(B)
        job_id = self.queue()
        with self.db() as connection:
            self.observe(connection)
        self.assertEqual(self.file_row()["held_version_id"], self.original["id"])
        self.run_job(job_id)
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        self.assert_original_kept()
        self.assertEqual(self.provider.puts, 1, "the held bytes are never sent")

    def test_identical_content_becomes_a_local_copy_without_upload(self) -> None:
        self.write(A)
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.run_queued(), [])
        self.assertEqual(self.uploads(), ["completed"], "no new upload")
        self.assertEqual(self.problems(), [])
        row = self.file_row()
        self.assertEqual((row["local_presence"], bytes(row["local_sha256"])), ("present", sha(A)))
        self.assertEqual(row["matched_version_id"], self.original["id"])
        self.assertIsNone(row["held_version_id"])
        self.assert_original_kept()

    def test_confirmed_replacement_uploads_verifies_then_removes_the_previous(self) -> None:
        self.write(B)
        self.scan()
        with self.db() as connection:
            self.assertEqual(notification_service.count_unread_notifications(connection), 1)
        queued = confirm_cloud_only_replacement(PATH, 2, "owner")
        self.assertEqual(queued["item_count"], 1)
        with self.db() as connection:
            self.assertEqual(notification_service.count_unread_notifications(connection), 0)
            self.assertEqual(notification_service.count_open_problems(connection), 0)
        self.assertEqual(self.problems(), [("cloud_only_conflict", "resolved")])
        [job_id] = self.run_queued()
        current = self.assert_current(job_id, B)
        self.assertNotEqual(current["id"], self.original["id"])
        self.assertEqual(self.provider.deleted, [(self.key, self.original["provider_version_id"])])
        self.assertEqual(self.rows("SELECT id FROM archive_versions WHERE id=%s", self.original["id"]), [])
        # A later scan does not reopen the confirmed conflict.
        self.scan()
        self.assertEqual(self.problems(), [("cloud_only_conflict", "resolved")])

    def test_confirmation_requires_an_open_conflict(self) -> None:
        self.write(A)
        self.scan()
        with self.assertRaises(HTTPException) as raised:
            confirm_cloud_only_replacement(PATH, 2, "owner")
        self.assertEqual(raised.exception.status_code, 409)

    def test_renaming_the_new_file_protects_it_and_keeps_the_cloud_copy(self) -> None:
        self.write(B)
        self.scan()
        renamed = self.source / "report-new.txt"
        os.replace(self.file, renamed)
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            catalog.mark_local_path_missing(vault_id=2, path=PATH, observed_at=storage.now_iso())
            stat = renamed.stat()
            catalog.observe_local_copy(vault_id=2, path="report-new.txt", file_type="regular",
                                       size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                       observed_at=storage.now_iso())
        self.assertEqual(self.scan(), 1)
        self.run_queued()
        self.assert_original_kept()
        self.assertEqual(self.problems(), [("cloud_only_conflict", "resolved")])
        paths = self.rows("""SELECT f.path, av.integrity FROM files f
                             JOIN archive_versions av ON av.id=f.current_version_id ORDER BY f.path""")
        self.assertEqual([(r["path"], r["integrity"]) for r in paths],
                         [("report-new.txt", "verified"), (PATH, "verified")])


if __name__ == "__main__":
    unittest.main()
