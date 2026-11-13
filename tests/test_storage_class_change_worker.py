"""Storage-class changes on the rebuilt model (#595, ADR-0035).

A manual change copies the exact verified Current in place to another class;
it may warm a cold Current through one restore of the exact VersionId. The
copy becomes Current only once verified (same digest, size, class and own
token), keeps the Local Copy match and the original verification time, and
the superseded version is removed by exact VersionId afterwards. A crash after
the provider accepted the copy and restart reconciliation never copy again.
Vault moves (``colder_only``) are covered by ``tests.test_vault_moves``.
"""
from __future__ import annotations

import unittest

from app import storage
from app.services.s3_copy import COPY_TOKEN_KEY
from tests.test_rename_sync import CopyWorkerFixture


class StorageClassWorkerTests(CopyWorkerFixture):
    def change(self, target: str) -> int:
        return self.admit("storage-class", "report.txt", target_storage_class=target)

    def assert_changed(self, job_id: int, before: dict, target: str) -> dict:
        current = self.assert_current(job_id, b"immutable content A")
        self.assertNotEqual(current["id"], before["id"])
        self.assertEqual(current["storage_class"], target)
        self.assertEqual(self.provider.classes[self.key, current["provider_version_id"]], target)
        self.assertEqual(current["verified_at"], before["verified_at"])
        self.assertEqual(self.provider.objects[self.key, current["provider_version_id"]][1][COPY_TOKEN_KEY],
                         str(self.attempts()[-1]["token"]))
        self.assertIn((self.key, before["provider_version_id"]), self.provider.deleted)
        self.assertEqual(self.provider.copies, 1, "a copy is never sent twice")
        return current

    def test_a_manual_change_copies_in_place_and_removes_the_old_version_exactly(self) -> None:
        before = self.protected()
        job_id = self.change("STANDARD_IA")
        self.run_job(job_id)
        self.assert_changed(job_id, before, "STANDARD_IA")
        self.assertEqual(self.attempts()[0]["purpose"], "storage-class")

    def test_the_same_class_completes_without_a_copy(self) -> None:
        before = self.protected()
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET storage_class='STANDARD_IA' WHERE id=%s",
                               (before["id"],))
        self.provider.classes[self.key, before["provider_version_id"]] = "STANDARD_IA"
        with self.db() as connection:
            connection.execute(
                "INSERT INTO jobs(vault_id, file_id, archive_version_id, action, status, origin) "
                "VALUES (2, %s, %s, 'storage-class', 'queued', 'manual') RETURNING id",
                (self.file_id, before["id"]))
            job_id = connection.execute("SELECT max(id) AS id FROM jobs").fetchone()["id"]
            connection.execute("INSERT INTO job_storage_class_changes(job_id, target_storage_class) "
                               "VALUES (%s, 'STANDARD_IA')", (job_id,))
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed")
        self.assertEqual((self.provider.copies, self.attempts()), (0, []))
        self.assertEqual(self.file_row()["current_version_id"], before["id"])

    def test_a_crash_after_the_copy_landed_and_restart_never_copy_again(self) -> None:
        before = self.protected()
        job_id = self.change("GLACIER_IR")
        self.crash_at("copied")
        self.run_job(job_id)
        self.assertEqual(self.provider.copies, 1)
        self.expire(job_id, "uploading")
        summary = storage.reconcile_interrupted_jobs()
        self.assertEqual(self.job(job_id)["status"], "queued", summary)
        self.run_job(job_id)
        self.assert_changed(job_id, before, "GLACIER_IR")

    def test_a_cold_current_is_restored_once_then_waits(self) -> None:
        before = self.protected()
        source = (self.key, before["provider_version_id"])
        self.provider.classes[source] = "GLACIER"
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET storage_class='GLACIER' WHERE id=%s",
                               (before["id"],))
        job_id = self.change("STANDARD")
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "restoring")
        self.assertEqual(self.provider.restores[source], 'ongoing-request="true"')
        self.assertEqual(self.version(before["id"])["restore_state"], "restoring")
        self.assertEqual((self.provider.copies, self.attempts()), (0, []))
        # Once the exact source is readable the same Job copies it.
        self.provider.restores[source] = 'ongoing-request="false", expiry-date="Fri, 20 Nov 2026 00:00:00 GMT"'
        with self.db() as connection:
            connection.execute("UPDATE jobs SET updated_at=now() - interval '1 day' WHERE id=%s", (job_id,))
        self.run_job(job_id)
        current = self.assert_current(job_id, b"immutable content A")
        self.assertEqual(current["storage_class"], "STANDARD")


if __name__ == "__main__":
    unittest.main()
