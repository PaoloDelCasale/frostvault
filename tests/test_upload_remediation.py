"""Upload Remediation guided by open obligations (#592, ADR-0035).

Real admission, claim, ``storage.process_job`` and the remediation sweep on a
disposable PostgreSQL 16 database, with the synthetic ``VersionedProvider``
counting every PUT/GET/HEAD. An episode exists only for a file whose upload
ended with cloud work still open; it resolves only with the exact
postcondition, and the normal case writes nothing.
"""
from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg

from app import storage
from app.catalog import ArchiveCatalog
from app.services import upload_failure_problems
from app.services import upload_provenance as provenance
from app.services import upload_remediation as remediation
from tests import test_upload_worker as worker
from tests.catalog_support import seed_vault

A = worker.A

_HELPERS = ("setUp", "db", "observe", "write", "queue", "claim", "run_job", "due", "abandon",
            "job", "file_row", "rows", "version", "assert_current")


class UploadRemediationTests(unittest.TestCase):
    locals().update({name: getattr(worker.UploadWorkerTests, name) for name in _HELPERS})

    # -- helpers -------------------------------------------------------------

    def fail_original(self, stage: str, *, retries: int = 0, mode: str = "s3_checksum") -> int:
        """Run an upload that ends failed (a permanent error) at ``stage``."""
        def crash(name, **kwargs):
            if name == stage and not crashed:
                crashed.append(name)
                raise ValueError(f"provider answer at {stage} is malformed")
        crashed: list[str] = []
        self.provider.hook = crash
        job_id = self.queue(mode)
        if retries:
            with self.db() as connection:
                connection.execute("UPDATE jobs SET retry_count=%s WHERE id=%s", (retries, job_id))
        self.run_job(job_id)
        self.provider.hook = lambda name, **kwargs: None
        self.assertEqual(self.job(job_id)["status"], "failed", self.job(job_id)["message"])
        return job_id

    def episodes(self) -> list[dict]:
        return self.rows("SELECT * FROM upload_remediations ORDER BY created_at, id")

    def episode(self) -> dict:
        rows = self.episodes()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def make_due(self) -> None:
        with self.db() as connection:
            connection.execute("UPDATE upload_remediations SET due_at=now() - interval '1 second' "
                               "WHERE closed_at IS NULL AND due_at <> 'infinity'")

    def sweep(self) -> int:
        return remediation.sweep(storage.db)

    def admitted(self) -> int:
        episode = self.episode()
        self.assertEqual(episode["state"], "queued", episode)
        detail = self.rows("SELECT * FROM job_uploads WHERE job_id=%s", episode["current_job_id"])[0]
        self.assertTrue(detail["reconcile_only"])
        return int(episode["current_job_id"])

    def failure_closed(self, job_id: int) -> bool:
        return bool(self.rows(
            f"SELECT {upload_failure_problems.CLOSED_UPLOAD_FAILURE_SQL} AS closed "
            "FROM jobs original WHERE original.id=%s", job_id)[0]["closed"])

    def request_upload(self) -> list[int]:
        with self.db() as connection:
            job_ids, _, _ = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path="report.txt", action="upload", requested_at=storage.now_iso(),
                group_id=str(uuid.uuid4()), is_directory=False)
        return job_ids

    # -- the normal case writes nothing ----------------------------------------

    def test_successful_and_workless_failed_uploads_leave_no_backlog(self) -> None:
        job_id = self.queue()
        self.run_job(job_id)
        self.assert_current(job_id, A)
        self.write(b"new content, never sent")
        failed = self.queue()
        self.assertTrue(storage.set_job(failed, "failed", "the Owner's disk is gone"))
        self.assertEqual(self.episodes(), [])
        self.assertEqual(self.rows("SELECT * FROM upload_remediation_jobs"), [])
        self.assertEqual(self.sweep(), 0)
        # Ordinary requests never consult the arbiter without an episode.
        with patch.object(remediation, "intercept_upload",
                          side_effect=AssertionError("per-file remediation work")):
            self.assertEqual(len(self.request_upload()), 1)

    def test_a_workless_failure_closes_by_later_protection_or_removal_only(self) -> None:
        failed = self.queue()
        self.assertTrue(storage.set_job(failed, "failed", "the Owner's disk is gone"))
        self.assertFalse(self.failure_closed(failed))
        successor = self.queue()
        self.run_job(successor)
        self.assert_current(successor, A)
        self.assertTrue(self.failure_closed(failed), "superseded by the protected Current")
        self.write(b"edited, then removed")
        removed = self.queue()
        self.assertTrue(storage.set_job(removed, "failed", "gone"))
        self.assertFalse(self.failure_closed(removed))
        with self.db() as connection:
            connection.execute("UPDATE files SET local_presence='missing' WHERE id=%s", (self.file_id,))
        self.assertTrue(self.failure_closed(removed), "nothing left to protect")
        # A failure that started a send closes only through its episode.
        sent = self.fail_original("accepted")
        self.assertFalse(self.failure_closed(sent))

    # -- an original failure resolves from its own evidence --------------------

    def test_ambiguous_send_failure_is_reconciled_without_a_second_put(self) -> None:
        original = self.fail_original("accepted")
        episode = self.episode()
        attempt = self.rows("SELECT * FROM upload_attempts")[0]
        self.assertEqual((episode["state"], str(episode["attempt_id"]), episode["retry_count"]),
                         ("pending", str(attempt["id"]), 0))
        self.assertEqual(self.sweep(), 0, "the grace comes first")
        self.make_due()
        self.assertEqual(self.sweep(), 1)
        resumed = self.admitted()
        self.assertIsNone(self.job(resumed)["archive_version_id"])
        self.run_job(resumed)
        self.assert_current(resumed, A)
        self.assertEqual(self.provider.puts, 1)
        episode = self.episode()
        self.assertEqual((episode["state"], episode["resolved_by_job_id"]), ("resolved", resumed))
        self.assertEqual({r["job_id"] for r in self.rows("SELECT job_id FROM upload_remediation_jobs")},
                         {original, resumed})
        self.assertTrue(self.failure_closed(original))
        self.assertEqual(self.sweep(), 0)

    def test_failure_with_a_receipt_resolves_without_a_new_put(self) -> None:
        heads: list[str] = []

        def fail_verification(name, **kwargs):
            if name == "head" and heads.append(name) is None and len(heads) == 2:
                raise ValueError("malformed verification answer")
        self.provider.hook = fail_verification
        original = self.queue("read_back")
        self.run_job(original)
        self.provider.hook = lambda name, **kwargs: None
        self.assertEqual(self.job(original)["status"], "failed")
        replacing = self.file_row()["replacing_version_id"]
        self.assertIsNotNone(replacing)
        self.assertEqual(len(self.rows("SELECT * FROM upload_receipts")), 1)
        self.assertEqual(str(self.episode()["archive_version_id"]), str(replacing))
        self.make_due()
        self.assertEqual(self.sweep(), 1)
        resumed = self.admitted()
        self.assertEqual(str(self.job(resumed)["archive_version_id"]), str(replacing))
        self.run_job(resumed)
        current = self.assert_current(resumed, A)
        self.assertEqual(str(current["id"]), str(replacing))
        self.assertEqual(current["verification_method"], "read_back")
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.episode()["state"], "resolved")
        self.assertTrue(self.failure_closed(original))

    def test_proven_no_write_rearms_the_same_snapshot_with_a_new_token(self) -> None:
        # The automatic resend waits for stable content like any upload.
        self.enterContext(patch("app.storage.file_is_stable", return_value=True))
        self.fail_original("before_put")
        self.assertEqual(self.provider.puts, 0)
        self.make_due()
        self.assertEqual(self.sweep(), 1)
        resumed = self.admitted()
        self.run_job(resumed)
        self.assert_current(resumed, A)
        attempts = self.rows("SELECT * FROM upload_attempts ORDER BY created_at")
        self.assertEqual([(a["outcome"], a["no_write_operation"]) for a in attempts],
                         [("no_write", provenance.ABSENT_OPERATION), ("landed", None)])
        self.assertEqual(attempts[0]["snapshot_id"], attempts[1]["snapshot_id"])
        self.assertNotEqual(attempts[0]["token"], attempts[1]["token"])
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.episode()["state"], "resolved")

    def test_obligation_survives_collection_of_the_original_job(self) -> None:
        original = self.fail_original("accepted")
        with self.db() as connection:
            connection.execute("DELETE FROM notifications WHERE job_id=%s", (original,))
            connection.execute("UPDATE upload_attempts SET job_id=NULL WHERE job_id=%s", (original,))
            connection.execute("DELETE FROM jobs WHERE id=%s", (original,))
        self.assertEqual(self.rows("SELECT * FROM upload_remediation_jobs"), [])
        self.make_due()
        self.assertEqual(self.sweep(), 1)
        resumed = self.admitted()
        self.run_job(resumed)
        self.assert_current(resumed, A)
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.episode()["state"], "resolved")

    # -- the exact postcondition -------------------------------------------------

    def test_completion_requires_the_exact_postcondition(self) -> None:
        self.fail_original("accepted")
        self.make_due()
        self.sweep()
        resumed = self.admitted()
        with self.assertRaisesRegex(RuntimeError, "exact cleanup postcondition"):
            with self.db() as connection:
                remediation.transition(connection, resumed, "completed", storage.now_iso())
        self.assertEqual(self.episode()["state"], "queued")

    def test_an_ordinary_success_resolves_the_episode_only_when_the_work_is_closed(self) -> None:
        original = self.fail_original("accepted")
        # An ordinary request for the file goes to the episode instead.
        self.make_due()
        job_ids = self.request_upload()
        self.assertEqual(len(job_ids), 1)
        self.assertEqual(self.admitted(), job_ids[0])
        self.run_job(job_ids[0])
        self.assertEqual(self.episode()["state"], "resolved")
        self.assertTrue(self.failure_closed(original))
        # With nothing left, later uploads are ordinary again.
        self.write(b"edited after the resolution")
        later = self.request_upload()
        self.assertEqual(len(later), 1)
        self.assertFalse(self.rows("SELECT reconcile_only FROM job_uploads WHERE job_id=%s",
                                   later[0])[0]["reconcile_only"])

    # -- grace, active Jobs, budget and cancel -------------------------------------

    def test_an_active_job_of_the_file_suspends_admission(self) -> None:
        self.fail_original("accepted")
        other = self.queue()
        self.make_due()
        self.assertEqual(self.sweep(), 0)
        episode = self.episode()
        self.assertEqual((episode["state"], episode["reason"]), ("suspended", "active_operation"))
        with self.db() as connection:
            ArchiveCatalog(connection).cancel_jobs(vault_id=2, job_ids=[other], message="",
                                                   message_key=None, now=storage.now_iso())
        self.make_due()
        self.assertEqual(self.sweep(), 1)

    def test_retry_budget_is_monotonic_and_explicit_retry_keeps_it(self) -> None:
        self.fail_original("accepted", retries=remediation.MAX_RETRIES)
        episode = self.episode()
        self.assertEqual((episode["state"], episode["reason"], episode["retry_count"]),
                         ("blocked", "retry_budget_exhausted", remediation.MAX_RETRIES))
        self.make_due()
        self.assertEqual(self.sweep(), 0)
        with self.assertRaises(psycopg.errors.RestrictViolation):
            with self.db() as connection:
                connection.execute("UPDATE upload_remediations SET retry_count=0")
        with self.db() as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            resumed = remediation.schedule_resume(connection, vault=vault, path="report.txt",
                                                  user_id=None, timestamp=storage.now_iso())
        self.assertEqual(self.job(resumed["job_id"])["retry_count"], remediation.MAX_RETRIES)
        self.run_job(resumed["job_id"])
        self.assert_current(resumed["job_id"], A)
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.episode()["state"], "resolved")

    def test_owner_cancel_is_durable(self) -> None:
        self.fail_original("accepted")
        self.make_due()
        self.sweep()
        resumed = self.admitted()
        with self.db() as connection:
            ArchiveCatalog(connection).cancel_jobs(vault_id=2, job_ids=[resumed], message="",
                                                   message_key=None, now=storage.now_iso())
            remediation.transition(connection, resumed, "cancelled", storage.now_iso())
        episode = self.episode()
        self.assertEqual((episode["state"], episode["reason"]), ("cancelled", "owner_cancelled"))
        self.make_due()
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.request_upload(), [], "a cancelled episode is never resurrected")
        with self.db() as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            with self.assertRaises(provenance.UploadBlocked) as refused:
                remediation.schedule_resume(connection, vault=vault, path="report.txt",
                                            user_id=None, timestamp=storage.now_iso())
        self.assertEqual(refused.exception.reason, "owner_cancelled")
        with self.assertRaises(psycopg.errors.RestrictViolation):
            with self.db() as connection:
                connection.execute("UPDATE upload_remediations SET state='pending', closed_at=NULL")
        self.assertEqual(self.provider.puts, 1)

    def test_a_failed_remediation_blocks_and_never_restarts_by_itself(self) -> None:
        self.fail_original("accepted")
        self.make_due()
        self.sweep()
        resumed = self.admitted()
        with self.db() as connection:
            ArchiveCatalog(connection).block_reconciliation(
                str(self.file_id), reason="inventory_changed", at=storage.now_iso())
        self.assertTrue(storage.set_job(resumed, "failed", "inventory changed"))
        episode = self.episode()
        self.assertEqual((episode["state"], episode["reason"]), ("blocked", "inventory_changed"))
        self.make_due()
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.request_upload()[:1], [], "blocked work is not bypassed")

    def test_a_stale_queued_episode_catches_up_with_its_collected_job(self) -> None:
        self.fail_original("accepted")
        self.make_due()
        self.sweep()
        resumed = self.admitted()
        with self.db() as connection:
            connection.execute("DELETE FROM jobs WHERE id=%s", (resumed,))
        self.assertIsNone(self.episode()["current_job_id"])
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.episode()["state"], "pending")
        self.assertEqual(self.sweep(), 1)
        self.run_job(self.admitted())
        self.assertEqual(self.episode()["state"], "resolved")

    # -- per-Vault fairness and bounds ---------------------------------------------

    def test_sweep_is_round_robin_across_vaults_and_bounded(self) -> None:
        with self.db() as connection:
            other_root = self.root / "other"
            other_root.mkdir()
            seed_vault(connection, vault_id=3, source_root=str(other_root))
            other = ArchiveCatalog(connection).observe_local_copy(
                vault_id=3, path="other.txt", file_type="regular", size=1, mtime_ns=1,
                observed_at=storage.now_iso())
            past = datetime.now(timezone.utc) - timedelta(seconds=5)
            for vault_id, file_id in ((2, self.file_id), (3, other)):
                connection.execute(
                    "INSERT INTO upload_remediations(vault_id, file_id, reason, due_at) "
                    "VALUES (%s, %s, 'transient', %s)", (vault_id, file_id, past))
        seen: list[int] = []

        def record(connection, episode, vault, **kwargs):
            seen.append(vault["id"])
            return None
        with patch.object(remediation, "VAULT_BATCH", 1), patch.object(remediation, "admit", record):
            self.sweep()
            self.sweep()
            self.sweep()
        self.assertEqual(seen, [2, 3, 2])
        with self.db() as connection:
            self.assertFalse(remediation.sweep_status(connection)["stale"])


if __name__ == "__main__":
    unittest.main()
