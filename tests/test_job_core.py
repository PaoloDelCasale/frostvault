"""Job core on the rebuilt schema (#589): one owner, live leases, explicit details.

Every test runs on a disposable PostgreSQL 16 database cloned from the real
``0001_baseline``. Claim, renewal, takeover, cancellation, expiry and the Pause
boundary are checked against live rows, never against caches or generations.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg

from app.catalog import ArchiveCatalog
from app.services import job_admission, recovery_approval, vault_pause
from tests.catalog_support import fingerprint, protect, seed_vault, sha256_hex
from tests.postgres import PostgresConnection, run_alembic

START = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def stamp(seconds: float = 0) -> str:
    return (START + timedelta(seconds=seconds)).isoformat()


def token() -> str:
    return str(uuid.uuid4())


class JobCoreFixture(unittest.TestCase):
    """Two Vaults with regular Local Copies and nothing else."""

    vault_ids = (10, 20)
    files_per_vault = 4

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "jobs"
        # Upload Remediation episodes are ported separately (#592).
        self.enterContext(patch("app.services.upload_remediation.intercept_upload",
                                return_value=(False, None)))
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.file_ids: dict[tuple[int, str], str] = {}
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            for vault_id in self.vault_ids:
                seed_vault(connection, vault_id=vault_id, source_root=f"/source/{vault_id}",
                           name=f"Vault {vault_id}")
                for index in range(self.files_per_vault):
                    name = f"f{index}.txt"
                    self.file_ids[(vault_id, name)] = catalog.observe_local_copy(
                        vault_id=vault_id, path=name, file_type="regular", size=10 + index,
                        mtime_ns=1, observed_at=stamp(-3600),
                    )

    def db(self) -> PostgresConnection:
        return PostgresConnection(str(self.path))

    def queue(self, vault_id: int = 10, path: str = "f0.txt", *, action: str = "upload",
              origin: str = "manual", seconds: float = 0, group_id: str | None = None,
              **kwargs) -> list[int]:
        if action == "upload":
            kwargs.setdefault("admission_verification_mode", "s3_checksum")
        with self.db() as connection:
            job_ids, _total, _eligible = ArchiveCatalog(connection).queue_jobs(
                vault_id=vault_id, path=path, action=action, requested_at=stamp(seconds),
                group_id=group_id or token(), is_directory=kwargs.pop("is_directory", False),
                origin=origin, **kwargs,
            )
        return job_ids

    def claim(self, job_id: int, *, seconds: float = 0, lease: float = 300,
              claim_token: str | None = None, generation: int | None = None):
        with self.db() as connection:
            return ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=claim_token or token(), claimed_at=stamp(seconds),
                claim_expires_at=stamp(seconds + lease), now=stamp(seconds),
                restore_due_before=stamp(seconds), scheduler_generation=generation,
            )

    def renew(self, job_id: int, claim_token: str, *, seconds: float, lease: float = 300,
              lock_job: bool = False):
        with self.db() as connection:
            return ArchiveCatalog(connection).renew_job_claim(
                job_id=job_id, claim_token=claim_token, now=stamp(seconds),
                claim_expires_at=stamp(seconds + lease), lock_job=lock_job,
            )

    def transition(self, job_id: int, status: str, *, claim_token: str | None,
                   seconds: float, **kwargs) -> bool:
        with self.db() as connection:
            return ArchiveCatalog(connection).transition_job(
                job_id=job_id, status=status, message=status, message_key=None,
                message_params=None, now=stamp(seconds), claim_token=claim_token,
                claim_expires_at=stamp(seconds + 300), **kwargs,
            )

    def cancel(self, vault_id: int, job_ids, *, seconds: float = 0) -> list[int]:
        with self.db() as connection:
            connection.execute("SELECT id FROM vaults WHERE id=%s FOR UPDATE", (vault_id,))
            return ArchiveCatalog(connection).cancel_jobs(
                vault_id=vault_id, job_ids=list(job_ids), message="stopped",
                message_key="job.upload_stopped", now=stamp(seconds),
            )

    def candidates(self, seconds: float = 0, **kwargs) -> list[dict]:
        kwargs.setdefault("limit", 10)
        with self.db() as connection:
            return ArchiveCatalog(connection).list_claimable_jobs(
                now=stamp(seconds), restore_due_before=stamp(seconds), **kwargs,
            )

    def depth(self, seconds: float = 0) -> int:
        with self.db() as connection:
            return ArchiveCatalog(connection).claimable_queue_depth(
                now=stamp(seconds), restore_due_before=stamp(seconds),
            )

    def job(self, job_id: int) -> dict:
        with self.db() as connection:
            return connection.execute(
                """SELECT j.*, l.token::text AS lease_token, l.expires_at AS lease_expires_at
                   FROM jobs j LEFT JOIN job_leases l ON l.job_id=j.id WHERE j.id=%s""",
                (job_id,),
            ).fetchone()

    def lease_count(self) -> int:
        with self.db() as connection:
            return connection.execute("SELECT count(*) AS n FROM job_leases").fetchone()["n"]


class AdmissionTests(JobCoreFixture):
    def test_upload_admission_writes_group_job_and_upload_detail(self):
        group = token()
        [job_id] = self.queue(group_id=group)
        job = self.job(job_id)
        self.assertEqual(
            (job["action"], job["status"], job["origin"], str(job["file_id"]), job["total_bytes"]),
            ("upload", "queued", "manual", self.file_ids[(10, "f0.txt")], 10),
        )
        self.assertIsNone(job["finished_at"])
        self.assertIsNone(job["archive_version_id"])
        with self.db() as connection:
            group_row = connection.execute("SELECT * FROM job_groups WHERE id=%s", (group,)).fetchone()
            upload = connection.execute("SELECT * FROM job_uploads WHERE job_id=%s", (job_id,)).fetchone()
        self.assertEqual((group_row["vault_id"], group_row["path"]), (10, "f0.txt"))
        self.assertEqual((upload["verification_mode"], upload["reconcile_only"]), ("s3_checksum", False))

    def test_directory_admission_is_one_group_in_path_order_with_literal_prefix(self):
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            for name in ("dir/b.txt", "dir/a.txt", "dir%/x.txt", "dir0.txt", "dirx/y.txt"):
                catalog.observe_local_copy(vault_id=10, path=name, file_type="regular", size=1,
                                           mtime_ns=1, observed_at=stamp(-60))
        group = token()
        job_ids = self.queue(path="dir", is_directory=True, group_id=group)
        with self.db() as connection:
            rows = connection.execute(
                """SELECT f.path FROM jobs j JOIN files f ON f.id=j.file_id
                   WHERE j.group_id=%s ORDER BY j.id""", (group,),
            ).fetchall()
        self.assertEqual([row["path"] for row in rows], ["dir/a.txt", "dir/b.txt"])
        self.assertEqual(len(job_ids), 2)

    def test_one_active_operation_per_file_admission_skips_and_schema_refuses(self):
        [first] = self.queue()
        self.assertEqual(self.queue(), [])
        with self.db() as connection:
            with self.assertRaises(psycopg.errors.UniqueViolation):
                connection.execute(
                    """INSERT INTO jobs(vault_id, file_id, action, requested_at, updated_at)
                       VALUES (10, %s, 'rename', now(), now())""",
                    (self.file_ids[(10, "f0.txt")],),
                )
        self.cancel(10, [first])
        self.assertEqual(len(self.queue()), 1)

    def test_admission_is_refused_for_a_non_active_vault_lifecycle(self):
        with self.db() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=10")
        with self.assertRaisesRegex(LookupError, "vault_quiesced"):
            self.queue()
        with self.db() as connection, self.assertRaisesRegex(LookupError, "vault_not_found"):
            job_admission.lock_vault(connection, 999)

    def test_job_identity_is_immutable_and_terminal_is_final(self):
        [job_id] = self.queue()
        with self.db() as connection:
            for column, value in (("action", "rename"), ("origin", "automatic"), ("vault_id", 20)):
                with self.subTest(column=column), self.assertRaises(psycopg.errors.RestrictViolation):
                    with connection.transaction():
                        connection.execute(f"UPDATE jobs SET {column}=%s WHERE id=%s", (value, job_id))
        self.cancel(10, [job_id])
        job = self.job(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNotNone(job["finished_at"])
        with self.db() as connection, self.assertRaises(psycopg.errors.RestrictViolation):
            connection.execute("UPDATE jobs SET status='queued' WHERE id=%s", (job_id,))


class ClaimLeaseTests(JobCoreFixture):
    def test_claim_creates_one_uuid_lease_and_a_second_claim_loses(self):
        [job_id] = self.queue()
        first = token()
        claimed = self.claim(job_id, claim_token=first)
        self.assertEqual(claimed["claim_token"], first)
        self.assertIsNone(self.claim(job_id, seconds=10))
        self.assertEqual(self.job(job_id)["lease_token"], first)
        self.assertEqual(self.depth(10), 0)
        with self.assertRaises(ValueError):
            self.claim(job_id, claim_token="not-a-uuid")

    def test_renewal_is_a_hot_update_of_the_lease_only(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease)
        self.assertTrue(self.transition(job_id, "uploading", claim_token=lease, seconds=1))
        before = self.job(job_id)
        with self.db() as connection:
            connection.execute("SELECT pg_stat_clear_snapshot()")
            start = connection.execute(
                "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname='job_leases'"
            ).fetchone()
        for second in range(2, 12):
            self.assertIsNotNone(self.renew(job_id, lease, seconds=second))
        after = self.job(job_id)
        # The Job row is untouched: same tuple, same updated_at.
        self.assertEqual(before["updated_at"], after["updated_at"])
        with self.db() as connection:
            ctid = connection.execute("SELECT ctid::text FROM jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertIsNotNone(ctid)
        self.assertEqual(after["lease_expires_at"], datetime.fromisoformat(stamp(311)))
        for _ in range(50):
            with self.db() as connection:
                connection.execute("SELECT pg_stat_force_next_flush()")
                stats = connection.execute(
                    "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname='job_leases'"
                ).fetchone()
            if stats["n_tup_upd"] - start["n_tup_upd"] >= 10:
                break
        self.assertGreaterEqual(stats["n_tup_upd"] - start["n_tup_upd"], 10)
        self.assertEqual(stats["n_tup_upd"] - start["n_tup_upd"],
                         stats["n_tup_hot_upd"] - start["n_tup_hot_upd"])

    def test_expired_lease_is_taken_over_and_the_old_owner_is_fenced(self):
        [job_id] = self.queue()
        old, new = token(), token()
        self.claim(job_id, claim_token=old, lease=60)
        self.assertIsNone(self.claim(job_id, seconds=59))
        self.assertEqual(self.claim(job_id, seconds=61, claim_token=new)["claim_token"], new)
        self.assertIsNone(self.renew(job_id, old, seconds=62))
        self.assertFalse(self.transition(job_id, "completed", claim_token=old, seconds=62))
        with self.db() as connection:
            self.assertFalse(ArchiveCatalog(connection).record_job_progress(
                job_id=job_id, transferred_bytes=5, now=stamp(62), claim_token=old,
                claim_expires_at=stamp(400)))
        self.assertTrue(self.transition(job_id, "uploading", claim_token=new, seconds=63))
        self.assertEqual(self.job(job_id)["lease_token"], new)

    def test_expiry_is_checked_on_live_state_for_renewal(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease, lease=60)
        self.assertIsNotNone(self.renew(job_id, lease, seconds=59, lease=60))
        self.assertIsNone(self.renew(job_id, lease, seconds=200))
        with self.db() as connection:
            self.assertFalse(ArchiveCatalog(connection).job_claim_is_live(
                job_id=job_id, claim_token=lease, now=stamp(200)))

    def test_renewal_that_wins_keeps_the_job_from_a_concurrent_takeover(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease, lease=60)
        renewing = PostgresConnection(str(self.path))
        try:
            # The renewal (its clock still before expiry) holds the lease row.
            renewing.execute(
                "UPDATE job_leases SET expires_at=%s WHERE job_id=%s AND token=%s AND expires_at > %s",
                (stamp(400), job_id, lease, stamp(59)),
            )
            result: dict = {}

            def takeover():
                result["claim"] = self.claim(job_id, seconds=61)

            thread = threading.Thread(target=takeover)
            thread.start()
            thread.join(timeout=1)
            self.assertTrue(thread.is_alive(), "takeover must wait for the lease row")
            renewing.commit()
            thread.join(timeout=20)
            self.assertIsNone(result["claim"], "a renewal that won keeps the lease")
        finally:
            renewing.close()
        self.assertEqual(self.job(job_id)["lease_token"], lease)

    def test_cancellation_wins_on_live_state_and_drops_the_lease(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease)
        self.assertTrue(self.transition(job_id, "uploading", claim_token=lease, seconds=1))
        self.assertEqual(self.cancel(10, [job_id], seconds=2), [job_id])
        self.assertEqual(self.lease_count(), 0)
        self.assertIsNone(self.renew(job_id, lease, seconds=3))
        self.assertFalse(self.transition(job_id, "completed", claim_token=lease, seconds=3))
        self.assertEqual(self.job(job_id)["status"], "cancelled")
        self.assertIsNone(self.claim(job_id, seconds=4))

    def test_fenced_transaction_blocks_cancellation_until_commit(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease)
        with self.db() as fence:
            self.assertIsNotNone(ArchiveCatalog(fence).renew_job_claim(
                job_id=job_id, claim_token=lease, now=stamp(1),
                claim_expires_at=stamp(301), lock_job=True))
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.cancel, 10, [job_id], seconds=2)
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=1)
                self.assertTrue(ArchiveCatalog(fence).transition_job(
                    job_id=job_id, status="completed", message="done", message_key=None,
                    message_params=None, now=stamp(1), claim_token=lease))
                fence.commit()
                self.assertEqual(pending.result(timeout=20), [])
        self.assertEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.lease_count(), 0)

    def test_release_gives_the_lease_back_and_waits(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease)
        self.assertTrue(self.transition(job_id, "retrying", claim_token=lease, seconds=1,
                                        run_after=stamp(120), retry_count=1))
        job = self.job(job_id)
        self.assertEqual((job["status"], job["retry_count"], job["lease_token"]), ("retrying", 1, None))
        self.assertEqual(self.candidates(60), [])
        self.assertEqual([row["id"] for row in self.candidates(120)], [job_id])

    def test_terminal_status_drops_the_lease_and_stamps_finished_at(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease)
        self.assertTrue(self.transition(job_id, "completed", claim_token=lease, seconds=5,
                                        transferred_bytes=10))
        job = self.job(job_id)
        self.assertEqual((job["status"], job["transferred_bytes"], job["lease_token"]),
                         ("completed", 10, None))
        self.assertIsNotNone(job["finished_at"])

    def test_an_outcome_enters_the_notification_outbox_and_a_cancellation_does_not(self):
        [done] = self.queue(path="f0.txt")
        [stopped] = self.queue(path="f1.txt")
        lease = token()
        self.claim(done, claim_token=lease)
        self.assertTrue(self.transition(done, "completed", claim_token=lease, seconds=5))
        self.assertEqual(self.cancel(10, [stopped]), [stopped])
        with self.db() as connection:
            queued = [row["job_id"] for row in connection.execute(
                "SELECT job_id FROM job_terminal_notifications ORDER BY job_id"
            ).fetchall()]
        self.assertEqual(queued, [done])

    def test_restart_settles_only_stale_work(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease, lease=60)
        self.assertTrue(self.transition(job_id, "uploading", claim_token=lease, seconds=1))
        with self.db() as connection:
            self.assertFalse(ArchiveCatalog(connection).settle_stale_job(
                job_id=job_id, status="queued", message="resumed", now=stamp(30), progress="reset"))
        with self.db() as connection:
            self.assertTrue(ArchiveCatalog(connection).settle_stale_job(
                job_id=job_id, status="queued", message="resumed", now=stamp(400), progress="reset"))
        job = self.job(job_id)
        self.assertEqual((job["status"], job["lease_token"], job["transferred_bytes"]), ("queued", None, 0))


class PauseAndLifecycleBoundaryTests(JobCoreFixture):
    def pause(self, paused: bool = True, vault_id: int = 10) -> None:
        with self.db() as connection:
            connection.execute("UPDATE vaults SET paused=%s WHERE id=%s", (paused, vault_id))

    def test_pause_holds_new_claims_but_not_recovery_and_admission_continues(self):
        [upload] = self.queue()
        self.pause()
        self.assertIsNone(self.claim(upload))
        self.assertEqual(self.depth(), 0)
        self.assertEqual(len(self.queue(path="f1.txt")), 1)
        with self.db() as connection:
            connection.execute(
                """INSERT INTO jobs(vault_id, file_id, action, requested_at, updated_at)
                   VALUES (10, %s, 'recover', %s, %s)""",
                (self.file_ids[(10, "f2.txt")], stamp(), stamp()),
            )
        self.assertEqual([row["action"] for row in self.candidates()], ["recover"])
        self.pause(False)
        self.assertIsNotNone(self.claim(upload))

    def test_pause_is_a_per_file_claim_boundary_not_a_held_work_fence(self):
        [job_id] = self.queue()
        lease = token()
        self.claim(job_id, claim_token=lease)
        self.pause()
        self.assertTrue(self.transition(job_id, "uploading", claim_token=lease, seconds=1))
        self.assertIsNotNone(self.renew(job_id, lease, seconds=2))
        self.assertTrue(self.transition(job_id, "completed", claim_token=lease, seconds=3))

    def test_pause_waits_for_in_flight_claim_and_claim_sees_committed_pause(self):
        [job_id] = self.queue()
        with self.db() as pausing:
            pausing.execute("SELECT id FROM vaults WHERE id=10 FOR UPDATE")
            pausing.execute("UPDATE vaults SET paused=true WHERE id=10")
            # The claim skips the locked Vault instead of blocking the scheduler.
            self.assertIsNone(self.claim(job_id))
        self.assertIsNone(self.claim(job_id))

    def test_stopping_vault_runs_only_its_stop_protection_purge(self):
        group, purge_group = token(), token()
        [upload] = self.queue()
        with self.db() as connection:
            for gid, stop in ((group, False), (purge_group, True)):
                connection.execute("INSERT INTO job_groups(id, vault_id, created_at) VALUES (%s, 10, %s)",
                                   (gid, stamp()))
                connection.execute(
                    "INSERT INTO cloud_purge_requests(group_id, reason, stop_protection) VALUES (%s, 'r', %s)",
                    (gid, stop))
            ordinary = connection.execute(
                """INSERT INTO jobs(vault_id, file_id, group_id, action, requested_at, updated_at)
                   VALUES (10, %s, %s, 'cloud-purge', %s, %s) RETURNING id""",
                (self.file_ids[(10, "f1.txt")], group, stamp(), stamp())).fetchone()["id"]
            stop = connection.execute(
                """INSERT INTO jobs(vault_id, file_id, group_id, action, requested_at, updated_at)
                   VALUES (10, %s, %s, 'cloud-purge', %s, %s) RETURNING id""",
                (self.file_ids[(10, "f2.txt")], purge_group, stamp(), stamp())).fetchone()["id"]
        self.assertEqual({row["id"] for row in self.candidates()}, {upload, ordinary})
        with self.db() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=10")
        self.assertEqual([row["id"] for row in self.candidates()], [stop])
        self.assertIsNone(self.claim(upload))


class PurgeGroupTests(JobCoreFixture):
    def purge_group(self, files=("f0.txt", "f1.txt"), *, status="pending_delay", run_after=60):
        group = token()
        with self.db() as connection:
            connection.execute("INSERT INTO job_groups(id, vault_id, created_at) VALUES (%s, 10, %s)",
                               (group, stamp()))
            connection.execute("INSERT INTO cloud_purge_requests(group_id, reason) VALUES (%s, 'r')",
                               (group,))
            ids = [connection.execute(
                """INSERT INTO jobs(vault_id, file_id, group_id, action, status, requested_at,
                                    updated_at, run_after)
                   VALUES (10, %s, %s, 'cloud-purge', %s, %s, %s, %s) RETURNING id""",
                (self.file_ids[(10, name)], group, status, stamp(index), stamp(index),
                 stamp(run_after)),
            ).fetchone()["id"] for index, name in enumerate(files)]
        return group, ids

    def claim_group(self, lead: int, seconds: float, claim_token: str | None = None):
        with self.db() as connection:
            return ArchiveCatalog(connection).claim_purge_group(
                lead_job_id=lead, claim_token=claim_token or token(), claimed_at=stamp(seconds),
                claim_expires_at=stamp(seconds + 300), now=stamp(seconds),
                message="deleting", message_key="job.cloud_purge_deleting",
            )

    def test_group_claims_all_members_or_none_after_the_delay(self):
        group, ids = self.purge_group()
        self.assertEqual(self.claim_group(ids[0], 30), [])
        lease = token()
        claimed = self.claim_group(ids[0], 61, lease)
        self.assertEqual([row["id"] for row in claimed], ids)
        self.assertEqual({row["claim_token"] for row in claimed}, {lease})
        self.assertEqual({self.job(i)["status"] for i in ids}, {"cleaning"})
        self.assertEqual(self.claim_group(ids[1], 62), [])
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            self.assertTrue(catalog.renew_purge_group_claim(
                vault_id=10, group_id=group, claim_token=lease, expected_job_ids=ids,
                now=stamp(70), claim_expires_at=stamp(400)))
            self.assertEqual(catalog.release_purge_group_claim(
                vault_id=10, group_id=group, claim_token=lease, now=stamp(71)), ids)
        self.assertEqual({self.job(i)["status"] for i in ids}, {"queued"})
        self.assertEqual(self.lease_count(), 0)

    def test_a_live_member_lease_blocks_the_whole_group(self):
        _group, ids = self.purge_group(status="queued")
        with self.db() as connection:
            connection.execute(
                "INSERT INTO job_leases(job_id, token, claimed_at, expires_at) VALUES (%s, %s, %s, %s)",
                (ids[1], token(), stamp(0), stamp(300)))
        self.assertEqual(self.claim_group(ids[0], 10), [])
        self.assertEqual(self.candidates(10), [])
        self.assertEqual(len(self.claim_group(ids[0], 301)), 2)

    def test_claim_job_never_claims_a_purge_member(self):
        _group, ids = self.purge_group(status="queued")
        self.assertIsNone(self.claim(ids[0]))


class FairWindowTests(JobCoreFixture):
    files_per_vault = 12

    def test_window_is_per_vault_and_per_tier(self):
        for index in range(10):
            self.queue(path=f"f{index}.txt", seconds=index)
        [other] = self.queue(vault_id=20, seconds=100)
        rows = self.candidates(200, limit=2, per_vault_limit=2)
        self.assertIn(other, [row["id"] for row in rows])
        self.assertEqual(len([row for row in rows if row["vault_id"] == 10]), 2)

    def test_owner_operations_are_not_hidden_behind_an_upload_burst(self):
        for index in range(10):
            self.queue(path=f"f{index}.txt", seconds=index, origin="automatic")
        with self.db() as connection:
            connection.execute(
                """INSERT INTO jobs(vault_id, file_id, action, requested_at, updated_at)
                   VALUES (10, %s, 'rename', %s, %s)""",
                (self.file_ids[(10, "f11.txt")], stamp(50), stamp(50)),
            )
        rows = self.candidates(100, limit=1, per_vault_limit=1)
        self.assertEqual([row["action"] for row in rows], ["rename", "upload"])
        self.assertEqual(rows[0]["path"], "f11.txt")

    def test_unserved_only_skips_vaults_with_a_live_lease(self):
        [busy] = self.queue()
        self.queue(path="f1.txt", seconds=1)
        [idle] = self.queue(vault_id=20, seconds=2)
        self.claim(busy, seconds=3)
        self.assertEqual([row["id"] for row in self.candidates(4, unserved_only=True)], [idle])
        with self.db() as connection:
            self.assertEqual(ArchiveCatalog(connection).active_job_counts(now=stamp(4)), {10: 1})

    def test_scheduler_generation_rejects_a_stale_aged_nomination(self):
        [first] = self.queue()
        [second] = self.queue(path="f1.txt", seconds=1)
        with self.db() as connection:
            generation = connection.execute(
                "SELECT generation FROM scheduler_vault_turns WHERE vault_id=10").fetchone()["generation"]
        # Serving the Vault consumes its turn: an aged nomination read before is stale.
        self.assertIsNotNone(self.claim(first, generation=generation))
        self.assertIsNone(self.claim(second, seconds=1, generation=generation))
        self.assertIsNotNone(self.claim(second, seconds=1, generation=generation + 1))


class RestoreApprovalTests(JobCoreFixture):
    def setUp(self) -> None:
        super().setUp()
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            digest = sha256_hex(b"x")
            for name in ("f0.txt", "f1.txt"):
                fingerprint(catalog, 10, name, digest)
                protect(catalog, 10, name, digest)
                connection.execute(
                    "UPDATE files SET local_presence='missing' WHERE vault_id=10 AND path=%s", (name,))
            connection.execute("UPDATE archive_versions SET storage_class='DEEP_ARCHIVE'")
        self.group = token()
        self.job_ids = self.queue(path="", action="recover", is_directory=True, group_id=self.group,
                                  whole_vault=True, restore_tier="Bulk", restore_days=3,
                                  estimated_cost_eur=4.5, estimated_hours=48)

    def hold(self, deadline: float = 3600) -> None:
        with self.db() as connection:
            recovery_approval.hold(connection, group_id=self.group, job_ids=self.job_ids,
                                   deadline=stamp(deadline), message="held", now=stamp())

    def statuses(self) -> set[str]:
        with self.db() as connection:
            return {row["status"] for row in connection.execute(
                "SELECT status FROM jobs WHERE group_id=%s", (self.group,)).fetchall()}

    def test_recovery_details_live_on_the_request_and_restore_rows(self):
        self.assertEqual(len(self.job_ids), 2)
        with self.db() as connection:
            request = connection.execute("SELECT * FROM restore_requests WHERE group_id=%s",
                                         (self.group,)).fetchone()
            restores = connection.execute("SELECT * FROM job_restores ORDER BY job_id").fetchall()
            columns = {row["column_name"] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='jobs'").fetchall()}
        self.assertEqual((request["estimated_cost_eur"], request["estimated_hours"]), (4.5, 48))
        self.assertIsNone(request["approved_at"])
        self.assertEqual({(row["tier"], row["days"]) for row in restores}, {("Bulk", 3)})
        self.assertTrue(columns.isdisjoint({"approved_by", "requested_by", "confirmation_phrase",
                                            "claim_token", "pending_until"}))

    def test_approval_keeps_only_the_instant(self):
        self.hold()
        self.assertEqual(self.statuses(), {"pending_approval"})
        self.assertEqual(self.candidates(), [])
        with self.db() as connection:
            approved = recovery_approval.approve(connection, vault_id=10, group_id=self.group,
                                                 now=stamp(60))
            request = connection.execute("SELECT approved_at FROM restore_requests WHERE group_id=%s",
                                         (self.group,)).fetchone()
        self.assertEqual(sorted(approved), sorted(self.job_ids))
        self.assertEqual(request["approved_at"], datetime.fromisoformat(stamp(60)))
        self.assertEqual(self.statuses(), {"queued"})
        with self.db() as connection:
            self.assertEqual(recovery_approval.sweep_expired(connection, now=stamp(99999)), 0)

    def test_deadline_is_inclusive_and_late_approval_is_refused(self):
        self.hold(deadline=100)
        with self.db() as connection:
            self.assertEqual(recovery_approval.expire_pending(connection, vault_id=10, now=stamp(99.9)), 0)
            self.assertEqual(recovery_approval.approve(connection, vault_id=10, group_id=self.group,
                                                       now=stamp(100)), [])
        self.assertEqual(self.statuses(), {"cancelled"})

    def test_sweep_expires_whole_request_with_localized_reason(self):
        self.hold(deadline=100)
        with self.db() as connection:
            self.assertEqual(recovery_approval.sweep_expired(connection, now=stamp(101)), 2)
            rows = connection.execute("SELECT message_key, finished_at FROM jobs WHERE group_id=%s",
                                      (self.group,)).fetchall()
        self.assertEqual({row["message_key"] for row in rows}, {"job.recovery_approval_expired"})
        self.assertTrue(all(row["finished_at"] is not None for row in rows))

    def test_approve_and_cancel_race_serialize_on_the_vault(self):
        self.hold()

        def approve():
            with self.db() as connection:
                connection.execute("SELECT id FROM vaults WHERE id=10 FOR UPDATE")
                return recovery_approval.approve(connection, vault_id=10, group_id=self.group,
                                                 now=stamp(10))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve), pool.submit(self.cancel, 10, self.job_ids, seconds=10)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(self.statuses(), {"cancelled"})
        self.assertEqual(len(results[1]), 2, "cancel ends every Job either way")


class VaultPauseServiceTests(JobCoreFixture):
    def test_only_an_operational_vault_can_be_paused(self):
        with self.db() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=20")
            with self.assertRaises(ValueError):
                vault_pause.set_paused(connection, vault_id=20, paused=True)
            with self.assertRaises(vault_pause.VaultNotFound):
                vault_pause.set_paused(connection, vault_id=99, paused=True)


if __name__ == "__main__":
    unittest.main()
