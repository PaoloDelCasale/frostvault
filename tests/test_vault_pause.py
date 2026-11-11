"""Persistent hold and Vault-first races on disposable PostgreSQL16 (#537)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, storage
from app.catalog import ArchiveCatalog
from app.config import settings
from app.services import recovery_approval, vault_pause
from app.sessions import create_session, csrf_token_for
from tests.postgres import database_dsn
from tests.test_job_queue_fairness import QueueFixture


class VaultPauseTests(QueueFixture):
    def seed_jobs(self, vault_id=2, count=1):
        for index in range(count):
            self.write(vault_id, f"{index}.txt")
        self.advance(300)
        self.assertEqual(self.queue(vault_id), count)
        return self.jobs(vault_id)

    def pause(self, paused=True, vault_id=2):
        with self.db() as c:
            return vault_pause.set_paused(c, vault_id=vault_id, paused=paused, user_id=1)

    def claim(self, c, job_id):
        return ArchiveCatalog(c).claim_job(
            job_id=job_id, claim_token=f"claim-{job_id}", claimed_at=self.clock.isoformat(),
            claim_expires_at=(self.clock + timedelta(hours=1)).isoformat(),
            now=self.clock.isoformat(), restore_due_before=self.clock.isoformat(),
        )

    def test_persistent_idempotent_hold_keeps_queue_and_resume_does_not_requeue(self):
        before = self.seed_jobs(count=3)
        self.assertTrue(self.pause()["paused"])
        self.pause()
        # New connection simulates process restart: no runtime flag needed.
        with self.db() as c:
            self.assertTrue(c.execute("SELECT paused FROM vaults WHERE id=2").fetchone()["paused"])
            self.assertEqual(ArchiveCatalog(c).claimable_queue_depth(
                now=self.clock.isoformat(), restore_due_before=self.clock.isoformat()), 0)
            self.assertIsNone(self.claim(c, before[0]["id"]))
            self.assertEqual(c.execute("SELECT count(*) AS n FROM notifications WHERE event='vault_paused'").fetchone()["n"], 1)
        self.assertEqual(self.jobs(2), before)
        self.assertEqual(self.candidates(limit=10), [])
        self.assertFalse(self.pause(False)["paused"])
        self.pause(False)
        self.assertEqual(self.jobs(2), before)
        self.assertEqual({j["id"] for j in self.candidates(limit=10)}, {j["id"] for j in before})
        self.assertEqual(self.queue(2), 0)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM notifications WHERE event='vault_resumed'").fetchone()["n"], 1)

    def test_local_observations_and_admission_continue_while_paused_other_vault_is_served(self):
        self.pause()
        paused_jobs = self.seed_jobs(count=25)
        other = self.seed_jobs(3)[0]
        self.assertEqual([j["id"] for j in self.candidates(limit=1, per_vault_limit=1)], [other["id"]])
        self.write(2, "new.txt", b"new copy")
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        self.assertEqual(len(self.jobs(2)), len(paused_jobs) + 1)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM local_copies lc JOIN vault_files vf ON vf.id=lc.vault_file_id WHERE vf.vault_id=2 AND lc.presence='present'").fetchone()["n"], 26)
        with patch("app.storage.validate_cloud_vault"), patch("app.storage.process_job") as process:
            self.assertEqual(storage.process_jobs_once(wait=True), 1)
        self.assertEqual(process.call_args.args[0]["id"], other["id"])
        self.assertEqual(len(self.jobs(2)), 26)

    def test_full_scan_catalogues_unwatched_local_copy_during_hold(self):
        self.pause()
        target = self.roots[2] / "missed-by-watcher.txt"
        target.write_bytes(b"scan catch-up")
        with patch("app.storage.scan_cloud", return_value=0), \
                patch("app.storage.validate_cloud_vault"), patch("app.storage.s3_client"), \
                patch("app.storage.sync_lifecycle_rules_for_bucket", return_value=0):
            result = storage.scan_vault(self.vaults[2])
        self.assertEqual(result["local"], 1)
        with self.db() as c:
            row = c.execute("SELECT lc.presence FROM local_copies lc JOIN file_paths fp ON fp.vault_file_id=lc.vault_file_id WHERE fp.vault_id=2 AND fp.path='missed-by-watcher.txt'").fetchone()
            self.assertEqual(row["presence"], "present")
            self.assertTrue(c.execute("SELECT paused FROM vaults WHERE id=2").fetchone()["paused"])
        self.advance(300)
        self.assertEqual(self.queue(2), 1)
        self.assertEqual(self.candidates(limit=10), [])

    def test_all_new_cloud_jobs_wait_including_owner_uploads_but_recoveries_continue(self):
        jobs = self.seed_jobs(count=9)
        cases = [("upload", "queued", "manual"), ("rename", "queued", "automatic"),
                 ("free-space", "queued", "automatic"), ("storage-class", "queued", "manual"),
                 ("recover", "queued", "manual"), ("recover", "restoring", "manual"),
                 ("storage-class", "restoring", "manual"), ("upload", "retrying", "automatic"),
                 ("recover", "pending_approval", "manual")]
        with self.db() as c:
            for job, (action, status, origin) in zip(jobs, cases):
                c.execute("UPDATE jobs SET action=%s,status=%s,origin=%s,updated_at=%s WHERE id=%s",
                          (action, status, origin, self.clock.isoformat(), job["id"]))
        self.pause()
        expected = {jobs[index]["id"] for index in (4, 5, 6)}
        self.assertEqual({j["id"] for j in self.candidates(limit=50)}, expected)
        with self.db() as c:
            self.assertEqual(ArchiveCatalog(c).claimable_queue_depth(now=self.clock.isoformat(), restore_due_before=self.clock.isoformat()), 3)
            for job in jobs:
                self.assertEqual(self.claim(c, job["id"]) is not None, job["id"] in expected)
        for job in self.jobs(2):
            if job["id"] in expected:
                with storage._job_claim_context(job):
                    self.assertTrue(storage.set_job(job["id"], "downloading"))
                    self.assertTrue(storage.set_job(job["id"], "completed"))
        self.assertTrue(all(j["status"] == "completed" for j in self.jobs(2) if j["id"] in expected))

    def test_notification_failure_does_not_rollback_the_hold(self):
        with patch("app.services.vault_pause.notifications.enqueue_notification", side_effect=RuntimeError("mail unavailable")):
            self.assertTrue(self.pause()["paused"])
        with self.db() as c:
            self.assertTrue(c.execute("SELECT paused FROM vaults WHERE id=2").fetchone()["paused"])

    def test_pause_preserves_claim_and_running_job_finishes(self):
        job = self.seed_jobs()[0]
        with self.db() as c:
            claimed = self.claim(c, job["id"])
        job.update(claimed)
        self.pause()
        # Includes the claim-to-handler handoff: pause never revokes a lease.
        with storage._job_claim_context(job):
            self.assertTrue(storage.set_job(job["id"], "uploading"))
            self.assertTrue(storage.set_job(job["id"], "verifying"))
            self.assertTrue(storage.set_job(job["id"], "completed"))
        completed = self.jobs(2)[0]
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(completed["claim_token"])
        self.assertTrue(self.pause()["paused"])

    def test_pause_wins_stale_candidate_and_contended_vault_does_not_block_other(self):
        job = self.seed_jobs()[0]
        other = self.seed_jobs(3)[0]
        # Candidate was selected before the Owner pause.
        self.assertIn(job["id"], {j["id"] for j in self.candidates(limit=10)})
        with self.db() as holder:
            vault_pause.set_paused(holder, vault_id=2, paused=True, user_id=1)
            with self.db() as worker:
                self.assertIsNone(self.claim(worker, job["id"]))
                self.assertIsNotNone(self.claim(worker, other["id"]))
        with self.db() as worker:
            self.assertIsNone(self.claim(worker, job["id"]))
        self.assertEqual(self.jobs(2)[0]["status"], "queued")

    def test_claim_wins_pause_serializes_and_parallel_claims_keep_shared_lock(self):
        jobs = self.seed_jobs(count=2)
        entered = threading.Event()
        def pause_waiter():
            with self.db() as c:
                c.execute("SET LOCAL lock_timeout='5s'")
                entered.set()
                vault_pause.set_paused(c, vault_id=2, paused=True, user_id=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.db() as claimant:
                self.assertIsNotNone(self.claim(claimant, jobs[0]["id"]))
                # A second worker can claim the same Vault, not an exclusive lock.
                with self.db() as second:
                    self.assertIsNotNone(self.claim(second, jobs[1]["id"]))
                future = pool.submit(pause_waiter)
                self.assertTrue(entered.wait(3))
                self.assertFalse(future.done())
            future.result(timeout=8)
        self.assertTrue(all(j["claim_token"] for j in self.jobs(2)))
        self.assertTrue(self.pause()["paused"])

    def test_pending_recovery_approval_still_expires_while_paused(self):
        job = self.seed_jobs()[0]
        with self.db() as c:
            c.execute("UPDATE jobs SET action='recover',status='pending_approval',pending_until=%s WHERE id=%s", (self.clock.isoformat(), job["id"]))
        self.pause()
        with self.db() as c:
            self.assertEqual(recovery_approval.sweep_expired(c, now=self.clock.isoformat()), 1)
        expired = self.jobs(2)[0]
        self.assertEqual(expired["status"], "cancelled")
        self.assertEqual(expired["message_key"], "job.recovery_approval_expired")

    def test_purge_group_is_held_but_explicit_stop_protection_is_not_overloaded(self):
        job = self.seed_jobs()[0]
        with self.db() as c:
            c.execute("UPDATE jobs SET action='cloud-purge',origin='manual' WHERE id=%s", (job["id"],))
        self.pause()
        def claim_group(c):
            return ArchiveCatalog(c).claim_purge_group(lead_job_id=job["id"], claim_token="purge",
                claimed_at=self.clock.isoformat(), claim_expires_at=(self.clock+timedelta(hours=1)).isoformat(),
                now=self.clock.isoformat(), message="deleting", message_key="job.cloud_purge_deleting")
        with self.db() as c:
            self.assertEqual(claim_group(c), [])
            c.execute("UPDATE jobs SET origin='stop-protection' WHERE id=%s", (job["id"],))
            self.assertEqual(len(claim_group(c)), 1)

    def test_api_requires_owner_session_and_csrf_and_exposes_persistent_status(self):
        configured = replace(settings, database_dsn=database_dsn(self.database), cookie_secure=False)
        with patch("app.main.settings", configured), patch("app.sessions.settings", configured):
            client = TestClient(main.app)
            self.addCleanup(client.close)
            for action in ("pause", "resume"):
                self.assertIn(client.post(f"/api/vaults/2/{action}").status_code, (401, 403))
            with self.db() as c:
                token = create_session(c, user_id=1, auth_method="oidc")
                csrf = csrf_token_for(c, token)
                c.execute("UPDATE sessions SET reauth_at='2000-01-01T00:00:00+00:00'")
            client.cookies.set(configured.session_cookie_name, token)
            for action, expected in (("pause", True), ("resume", False)):
                self.assertEqual(client.post(f"/api/vaults/2/{action}").status_code, 403)
                response = client.post(f"/api/vaults/2/{action}", headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["paused"], expected)
                self.assertEqual(next(v for v in client.get("/api/vaults").json()["items"] if v["id"] == 2)["paused"], expected)
                self.assertEqual(next(v for v in client.get("/api/general/vaults").json()["items"] if v["id"] == 2)["paused"], expected)
            self.assertEqual(client.post("/api/vaults/999/pause", headers={"X-CSRF-Token": csrf}).status_code, 404)
            with self.db() as c:
                c.execute("UPDATE vaults SET enabled=FALSE WHERE id=2")
            self.assertEqual(client.post("/api/vaults/2/pause", headers={"X-CSRF-Token": csrf}).status_code, 409)
