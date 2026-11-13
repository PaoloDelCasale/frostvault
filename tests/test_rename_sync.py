"""Rename Jobs on the rebuilt model (#595, ADR-0035).

Real admission, claim and ``storage.process_job`` on a disposable PostgreSQL 16
database; the S3 transport is the synthetic ``VersionedProvider`` (exact
VersionIds, server-side CopyObject, counted requests). Current is always
produced by a real upload Job first. Renames in the catalog live in
``tests.test_catalog_rename``.

Covered: the copy names the exact verified Current (key + VersionId) and is
attributed by its own token; Current changes only once the copy is verified;
the old key's version is removed afterwards by exact VersionId; the Local Copy
is matched only through a fresh digest of the real local bytes. A crash after
the provider accepted the copy, an expired lease and a takeover by another Job
reconcile from S3 and never copy again; an unsent copy is proven absent first;
a foreign write keeps every copy and blocks; a cold Current needs an explicit
restore; a suspended Vault (root change) waits and resumes.
"""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import vault_relocation
from app.services.s3_copy import COPY_TOKEN_KEY
from tests import test_upload_worker as fixtures
from tests.test_upload_worker import A, B, sha

ORIGINAL_MTIME_NS = 1_700_000_000_123_456_789


class _Crash(storage.WorkerStopping):
    """The worker process dies at a chosen provider stage."""


class CopyWorkerFixture(unittest.TestCase):
    setUp = fixtures.UploadWorkerTests.setUp
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

    def protected(self, payload: bytes = A) -> dict:
        self.write(payload, mtime_ns=ORIGINAL_MTIME_NS)
        job_id = self.queue()
        self.run_job(job_id)
        return self.assert_current(job_id, payload)

    def admit(self, action: str, path: str, **kwargs) -> int:
        with self.db() as connection:
            job_ids, _bytes, eligible = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path=path, action=action, requested_at=storage.now_iso(),
                group_id=str(uuid.uuid4()), is_directory=False, **kwargs)
        self.assertEqual((len(job_ids), eligible), (1, 1), f"{action} must be admitted")
        return job_ids[0]

    def expire(self, job_id: int, status: str = "queued") -> None:
        """Leave the Job as a crashed worker would: held, with an expired lease."""
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status=%s WHERE id=%s", (status, job_id))
            connection.execute(
                """INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
                   VALUES (%s, %s, now() - interval '2 hours', now() - interval '1 hour')
                   ON CONFLICT (job_id) DO UPDATE SET claimed_at=EXCLUDED.claimed_at,
                       expires_at=EXCLUDED.expires_at""",
                (job_id, str(uuid.uuid4())))

    def crash_at(self, stage: str) -> None:
        previous = self.provider.hook
        fired = []

        def hook(name, **kwargs):
            previous(name, **kwargs)
            if name == stage and not fired:
                fired.append(name)
                raise _Crash(stage)
        self.provider.hook = hook

    def attempts(self) -> list[dict]:
        return self.rows("SELECT * FROM copy_attempts ORDER BY created_at")


class RenameWorkerTests(CopyWorkerFixture):
    def rename(self, new_path: str = "renamed.txt") -> None:
        target = self.source / new_path
        os.replace(self.file, target)
        self.file = target
        with self.db() as connection:
            ArchiveCatalog(connection).rename_file(
                self.file_id, new_path=new_path, changed_at=storage.now_iso(), vault_id=2)
        self.old_key, self.key = self.key, f"vaults/{self.uuid}/{new_path}"

    def assert_renamed(self, job_id: int, before: dict, *, matched: bool = True) -> dict:
        current = self.assert_current(job_id, A, matched=matched)
        self.assertNotEqual(current["id"], before["id"])
        self.assertIsNotNone(current["copy_attempt_id"])
        self.assertEqual(self.provider.objects[self.key, current["provider_version_id"]][1][COPY_TOKEN_KEY],
                         str(self.attempts()[-1]["token"]))
        # Exact cleanup of the old key's version, after the new Current.
        self.assertIn((self.old_key, before["provider_version_id"]), self.provider.deleted)
        self.assertEqual(self.rows("SELECT id FROM archive_versions WHERE file_id=%s", self.file_id),
                         [{"id": current["id"]}])
        self.assertEqual(self.provider.copies, 1, "a copy is never sent twice")
        self.assertEqual(self.provider.puts, 1, "a rename never uploads")
        return current

    def test_rename_copies_the_exact_current_and_removes_the_old_key_exactly(self) -> None:
        before = self.protected()
        sources = []
        self.provider.hook = lambda stage, **kw: sources.append(kw["source"]) if stage == "before_copy" else None
        self.rename()
        job_id = self.admit("rename", "renamed.txt")
        self.run_job(job_id)
        self.assert_renamed(job_id, before)
        self.assertEqual(sources, [(self.old_key, before["provider_version_id"])])
        attempt = self.attempts()[0]
        self.assertEqual((attempt["purpose"], attempt["outcome"], attempt["source_provider_version_id"]),
                         ("rename", "landed", before["provider_version_id"]))

    def test_local_copy_matches_only_through_a_fresh_local_digest(self) -> None:
        before = self.protected()
        self.rename()
        # The bytes at the new path changed after the rename observation: the
        # remote digest never stands in for them.
        self.file.write_bytes(B + b" longer")
        job_id = self.admit("rename", "renamed.txt")
        self.run_job(job_id)
        self.assert_renamed(job_id, before, matched=False)
        self.assertIsNone(self.file_row()["matched_version_id"])

    def test_crash_after_the_copy_landed_resumes_from_its_token_without_copying_again(self) -> None:
        before = self.protected()
        self.rename()
        job_id = self.admit("rename", "renamed.txt")
        self.crash_at("copied")
        self.run_job(job_id)
        self.assertEqual(self.provider.copies, 1)
        self.assertIsNotNone(self.attempts()[0]["send_started_at"])
        self.assertIsNone(self.file_row()["replacing_version_id"])
        self.assertEqual(self.file_row()["current_version_id"], before["id"])
        self.expire(job_id)
        self.run_job(job_id)
        self.assert_renamed(job_id, before)

    def test_another_job_takes_over_the_started_copy(self) -> None:
        before = self.protected()
        self.rename()
        first = self.admit("rename", "renamed.txt")
        self.crash_at("copied")
        self.run_job(first)
        with self.db() as connection:
            connection.execute("DELETE FROM job_leases WHERE job_id=%s", (first,))
            connection.execute("UPDATE jobs SET status='failed', finished_at=now() WHERE id=%s", (first,))
        second = self.admit("rename", "renamed.txt")
        self.run_job(second)
        self.assert_renamed(second, before)
        self.assertEqual(self.attempts()[0]["job_id"], second)

    def test_database_retries_replay_db_units_only_never_the_copy(self) -> None:
        before = self.protected()
        self.rename()
        job_id = self.admit("rename", "renamed.txt")
        plan = {"INSERT INTO copy_attempts": 1, "SET send_started_at": 1,
                "INSERT INTO copy_receipts": 1, "outcome='landed'": 1,
                "SET replacing_version_id": 1, "SET current_version_id": 1}
        hits: list[str] = []
        real = storage.db
        with patch.object(storage, "db", side_effect=lambda: fixtures._FlakyCommit(real(), plan, hits)):
            self.run_job(job_id)
        self.assert_renamed(job_id, before)
        self.assertEqual(sorted(set(hits)), sorted(plan), "every unit must hit one commit failure")
        self.assertEqual(len(self.rows("SELECT * FROM copy_receipts")), 1)

    def test_a_crash_after_promotion_still_removes_the_old_key_exactly(self) -> None:
        before = self.protected()
        self.rename()
        job_id = self.admit("rename", "renamed.txt")
        with patch.object(storage, "cleanup_superseded_upload", side_effect=_Crash("cleanup")):
            self.run_job(job_id)
        self.assertEqual(self.provider.deleted, [])
        self.assertEqual(self.version(self.file_row()["current_version_id"])["object_key"], self.key)
        self.expire(job_id)
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.provider.deleted, [(self.old_key, before["provider_version_id"])])
        self.assertEqual(self.provider.copies, 1)

    def test_an_unsent_copy_is_proven_absent_before_a_new_attempt(self) -> None:
        before = self.protected()
        self.rename()
        job_id = self.admit("rename", "renamed.txt")
        self.crash_at("before_copy")
        self.run_job(job_id)
        self.assertEqual(self.provider.copies, 0)
        self.expire(job_id)
        self.run_job(job_id)
        self.assert_renamed(job_id, before)
        first, second = self.attempts()
        self.assertEqual((first["outcome"], first["no_write_operation"]), ("no_write", "ProvenAbsent"))
        self.assertEqual(second["outcome"], "landed")

    def test_a_foreign_write_keeps_every_copy_and_blocks(self) -> None:
        before = self.protected()
        self.rename()
        job_id = self.admit("rename", "renamed.txt")
        self.crash_at("before_copy")
        self.run_job(job_id)
        foreign = self.provider.add(self.key, b"someone else")
        self.expire(job_id)
        self.run_job(job_id)
        job = self.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("ambiguous_send", self.file_row()["reconciliation_block"] or job["message"])
        self.assertEqual(self.file_row()["current_version_id"], before["id"])
        self.assertEqual(self.provider.deleted, [])
        self.assertIn((self.key, foreign), self.provider.objects)
        self.assertIsNone(self.attempts()[0]["settled_at"])

    def test_a_cold_current_needs_an_explicit_restore(self) -> None:
        before = self.protected()
        self.provider.classes[self.key, before["provider_version_id"]] = "GLACIER"
        self.rename()
        job_id = self.admit("rename", "renamed.txt")
        self.run_job(job_id)
        job = self.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("restore it explicitly", job["message"])
        self.assertEqual((self.provider.copies, self.attempts()), (0, []))

    def test_a_vault_waiting_for_reconnect_holds_the_rename_then_resumes(self) -> None:
        before = self.protected()
        self.rename()
        job_id = self.admit("rename", "renamed.txt")
        claimed = self.claim(job_id)
        # The root changes after the claim: the target fence holds the copy.
        with self.db() as connection:
            connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=2")
        storage.process_job(claimed)
        self.assertEqual(self.job(job_id)["status"], "queued")
        self.assertEqual(self.provider.copies, 0)
        with self.db() as connection:
            vault_relocation.complete_relocation_scan(connection, 2)
        self.run_job(job_id)
        self.assert_renamed(job_id, before)


if __name__ == "__main__":
    unittest.main()
