"""Delayed Cloud Deletion and Stop Protection on the rebuilt model (#594, ADR-0035).

Real admission (the consent), group claim and ``storage.process_job`` on a
disposable PostgreSQL 16 database; the S3 transport is the synthetic
``VersionedProvider`` (exact VersionIds, counted deletes). Current is always
produced by a real upload Job first and then lost locally, so the file is
Cloud-only.

Covered: only the consented exact Current is deleted, and only after the
seven-day delay; its Archive Version row goes with the completed proof; a
reappearance revokes the whole pending selection; cancellation; any change of
the consent's facts at execution cancels it; a refused or ambiguous DELETE and
a crash are settled by exact listing, never by a second, wider deletion; Stop
Protection keeps or deletes and releases the root only when nothing can still
run; the schema refuses a grant outside a claimed consent.
"""
from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg

from app import storage
from app.catalog import ArchiveCatalog
from app.services import cloud_deletion, protection_stop, upload_remediation
from app.services import notifications as inbox
from app.services import protection_notifications as monitor
from tests import test_upload_worker as fixtures
from tests.test_upload_worker import A, B
from app.services.job_queue import JobQueue

PAST = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()


class CloudPurgeCase(fixtures.UploadWorkerCase):
    """The upload worker Vault, with the helpers that drive a Cloud Deletion."""

    # -- helpers -------------------------------------------------------------

    def cloud_only(self, payload: bytes = A) -> dict:
        self.write(payload)
        job_id = self.queue()
        self.run_job(job_id)
        current = self.assert_current(job_id, payload)
        self.file.unlink()
        with self.db() as connection:
            ArchiveCatalog(connection).mark_local_copy_missing(
                self.file_id, observed_at=storage.now_iso())
        return current

    def second_cloud_only(self, name: str = "other.txt") -> str:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(B)
        with self.db() as connection:
            stat = path.stat()
            file_id = ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path=name, file_type="regular", size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())
            job = connection.execute(
                "INSERT INTO jobs(vault_id, file_id, action, status, origin, total_bytes) "
                "VALUES (2, %s, 'upload', 'queued', 'manual', %s) RETURNING id",
                (file_id, stat.st_size)).fetchone()
            connection.execute(
                "INSERT INTO job_uploads(job_id, verification_mode) VALUES (%s, 's3_checksum')",
                (job["id"],))
        self.run_job(int(job["id"]))
        self.assertEqual(self.job(int(job["id"]))["status"], "completed")
        path.unlink()
        with self.db() as connection:
            ArchiveCatalog(connection).mark_local_copy_missing(file_id, observed_at=storage.now_iso())
        return str(file_id)

    def schedule(self, paths=("report.txt",), *, requested_at: str = PAST,
                 is_directory: bool = False, **selection) -> cloud_deletion.ScheduledDeletion:
        """Confirm what a fresh preview lists (a selection needs its fingerprint, #723)."""
        fingerprint = None
        try:
            with self.db() as connection:
                fingerprint = cloud_deletion.preview_selection(
                    connection, vault_id=2, paths=list(paths), is_directory=is_directory,
                    **selection).fingerprint
        except cloud_deletion.CloudDeletionError:
            pass  # scheduling reports the refusal itself
        with self.db() as connection:
            return cloud_deletion.schedule_cloud_purge(
                connection, vault_id=2, paths=list(paths), is_directory=is_directory,
                actor="owner", requested_at=requested_at, confirmation="Docs",
                reason="no longer needed", fingerprint=fingerprint, **selection)

    def claim_group(self, lead: int) -> dict | None:
        claimed_at = storage.now_iso()
        token = str(uuid.uuid4())
        with self.db() as connection:
            group = JobQueue(connection).claim_purge_group(
                lead_job_id=lead, claim_token=token, claimed_at=claimed_at,
                claim_expires_at=storage.job_runner.claim_expiry_at(claimed_at), now=claimed_at,
                message="Deleting", message_key="job.cloud_purge_deleting")
            if not group:
                return None
            job = connection.execute(
                """SELECT j.*, f.path, v.source_root, v.uuid AS vault_uuid, v.name AS vault_name,
                          v.lifecycle AS vault_lifecycle, t.bucket AS s3_bucket, t.key_layout AS s3_key_layout
                   FROM jobs j JOIN files f ON f.id=j.file_id JOIN vaults v ON v.id=j.vault_id
                   JOIN vault_targets t ON t.id=v.target_id WHERE j.id=%s""", (lead,)).fetchone()
        return {**job, "claim_token": token}

    def run_purge(self, lead: int) -> None:
        job = self.claim_group(lead)
        self.assertIsNotNone(job, "the elapsed consent must be claimable as a group")
        storage.process_job(job)

    def expire_group(self, group_id: str) -> None:
        with self.db() as connection:
            connection.execute(
                """UPDATE job_leases SET claimed_at=now() - interval '2 hours',
                       expires_at=now() - interval '1 hour'
                   WHERE job_id IN (SELECT id FROM jobs WHERE group_id=%s)""", (group_id,))

    def statuses(self, group_id: str) -> list[str]:
        return [r["status"] for r in self.rows(
            "SELECT status FROM jobs WHERE group_id=%s ORDER BY id", group_id)]

    def assert_deleted(self, current: dict) -> None:
        self.assertIn((self.key, current["provider_version_id"]), self.provider.deleted)
        self.assertNotIn((self.key, current["provider_version_id"]), self.provider.objects)
        self.assertEqual(self.rows("SELECT id FROM archive_versions WHERE id=%s", current["id"]), [])
        row = self.file_row()
        self.assertIsNone(row["current_version_id"])
        self.assertEqual(self.rows("SELECT id FROM cleanup_batches"), [])

    def assert_untouched(self, current: dict) -> None:
        self.assertEqual(self.provider.deleted, [])
        self.assertIn((self.key, current["provider_version_id"]), self.provider.objects)
        self.assertEqual(self.file_row()["current_version_id"], current["id"])
        self.assertEqual(self.rows("SELECT id FROM cleanup_batches"), [])

    def stop(self, disposition: str) -> dict:
        with self.db() as connection:
            return protection_stop.stop_protection(
                connection, vault_id=2, actor="owner", cloud_disposition=disposition,
                confirmation="Docs", reason="folder retired")

    def lifecycle(self) -> str:
        return self.rows("SELECT lifecycle FROM vaults WHERE id=2")[0]["lifecycle"]


class CloudPurgeTests(CloudPurgeCase):
    # -- consent -------------------------------------------------------------

    def test_consent_pins_the_exact_current_and_waits_seven_days(self) -> None:
        current = self.cloud_only()
        with self.db() as connection:
            preview = cloud_deletion.preview_selection(connection, vault_id=2, paths=["report.txt"])
        self.assertLessEqual(dict(object_count=1, version_count=1, delete_marker_count=0,
                                  byte_count=len(A), mode="exact").items(),
                             preview.as_dict().items())
        scheduled = self.schedule(requested_at=storage.now_iso())
        [job_id] = scheduled.job_ids
        job = self.job(job_id)
        self.assertEqual((job["status"], job["action"]), ("pending_delay", "cloud-purge"))
        self.assertEqual(job["archive_version_id"], current["id"])
        self.assertEqual(job["run_after"], datetime.fromisoformat(scheduled.pending_until))
        request = self.rows("SELECT * FROM cloud_purge_requests WHERE group_id=%s", scheduled.group_id)
        self.assertEqual([(r["reason"], r["stop_protection"]) for r in request],
                         [("no longer needed", False)])
        self.assertIsNone(self.claim_group(job_id), "nothing runs before the delay")
        self.assert_untouched(current)

    def test_only_cloud_only_files_with_one_exact_version_are_consented(self) -> None:
        self.write(A)
        self.run_job(self.queue())  # matched Local Copy: not Cloud-only
        with self.db() as connection, self.assertRaisesRegex(
                cloud_deletion.CloudDeletionError, "Only Cloud-only"):
            cloud_deletion.preview_selection(connection, vault_id=2, paths=["report.txt"])
        with self.db() as connection, self.assertRaises(cloud_deletion.ConfirmationRequired):
            cloud_deletion.schedule_cloud_purge(
                connection, vault_id=2, paths=["report.txt"], is_directory=False, actor="owner",
                requested_at=PAST, confirmation="docs", reason="x")

    def test_elapsed_consent_deletes_exactly_the_current_and_its_row(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        [job_id] = scheduled.job_ids
        self.run_purge(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed", self.job(job_id)["message"])
        self.assertEqual(self.provider.deleted, [(self.key, current["provider_version_id"])])
        self.assert_deleted(current)
        events = [r["event"] for r in self.rows(
            "SELECT event FROM audit_events WHERE event LIKE 'cloud_deletion.%%' ORDER BY id")]
        self.assertEqual(events, ["cloud_deletion.requested", "cloud_deletion.completed"])

    def test_a_folder_selection_is_one_group_deleted_together(self) -> None:
        current = self.cloud_only()
        self.second_cloud_only("sub/other.txt")
        with self.db() as connection, self.assertRaises(cloud_deletion.CloudDeletionError):
            cloud_deletion.preview_selection(connection, vault_id=2, paths=["sub"])  # no such file
        scheduled = self.schedule(paths=["sub"], is_directory=True)
        self.assertEqual(len(scheduled.job_ids), 1)
        everything = self.schedule(paths=["report.txt"])
        self.run_purge(scheduled.job_ids[0])
        self.run_purge(everything.job_ids[0])
        self.assertEqual(self.statuses(scheduled.group_id), ["completed"])
        self.assertEqual(self.statuses(everything.group_id), ["completed"])
        self.assertEqual(len(self.provider.deleted), 2)
        self.assert_deleted(current)

    # -- revocation ----------------------------------------------------------

    def test_a_reappearance_revokes_the_whole_pending_selection(self) -> None:
        current = self.cloud_only()
        self.second_cloud_only()
        scheduled = self.schedule(paths=["report.txt", "other.txt"])
        self.assertEqual(len(scheduled.job_ids), 2)
        self.write(A)  # the Local Copy reappears: new evidence the consent never covered
        self.assertEqual(self.statuses(scheduled.group_id), ["cancelled", "cancelled"])
        self.assertIsNone(self.claim_group(scheduled.job_ids[1]))
        self.assert_untouched(current)

    def test_new_bytes_never_inherit_the_old_consent(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        self.write(B)
        self.assertEqual(self.statuses(scheduled.group_id), ["cancelled"])
        row = self.file_row()
        self.assertEqual(row["held_version_id"], current["id"], "different content is held")
        self.file.unlink()
        with self.db() as connection:
            ArchiveCatalog(connection).mark_local_copy_missing(self.file_id, observed_at=storage.now_iso())
        with self.db() as connection, self.assertRaises(cloud_deletion.CloudDeletionError):
            cloud_deletion.preview_selection(connection, vault_id=2, paths=["report.txt"])
        self.assert_untouched(current)

    def test_cancel_before_start_keeps_everything(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        with self.db() as connection:
            cancelled = cloud_deletion.cancel_cloud_deletion(
                connection, vault_id=2, group_id=scheduled.group_id, actor="owner",
                cancelled_at=storage.now_iso())
        self.assertEqual(cancelled.cancelled_count, 1)
        self.assertEqual(self.statuses(scheduled.group_id), ["cancelled"])
        with self.db() as connection, self.assertRaises(cloud_deletion.CloudDeletionError):
            cloud_deletion.cancel_cloud_deletion(
                connection, vault_id=2, group_id=scheduled.group_id, actor="owner",
                cancelled_at=storage.now_iso())
        self.assertIsNone(self.claim_group(scheduled.job_ids[0]))
        self.assert_untouched(current)

    def test_a_present_entry_at_execution_cancels_without_deleting(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        self.file.write_bytes(A)  # not yet observed by the catalog
        self.run_purge(scheduled.job_ids[0])
        job = self.job(scheduled.job_ids[0])
        self.assertEqual(job["status"], "cancelled")
        self.assertIn("present", job["message"])
        self.assert_untouched(current)

    def test_a_changed_cloud_fact_at_execution_cancels_without_deleting(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET availability='missing' WHERE id=%s",
                               (current["id"],))
        self.run_purge(scheduled.job_ids[0])
        self.assertEqual(self.statuses(scheduled.group_id), ["cancelled"])
        self.assertEqual(self.provider.deleted, [])

    def test_a_stopping_vault_never_runs_an_ordinary_deletion(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=2")
        self.assertIsNone(self.claim_group(scheduled.job_ids[0]))
        self.assert_untouched(current)

    # -- provider outcomes and crashes --------------------------------------

    def test_a_refused_delete_revokes_the_grant_and_fails(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()

        def refuse(**kwargs):
            [obj] = kwargs["Delete"]["Objects"]
            return {"Errors": [{"Key": obj["Key"], "VersionId": obj["VersionId"],
                                "Code": "AccessDenied", "Message": "denied"}]}
        self.provider.delete_objects = refuse
        self.run_purge(scheduled.job_ids[0])
        job = self.job(scheduled.job_ids[0])
        self.assertEqual(job["status"], "failed")
        self.assertIn("AccessDenied", job["message"])
        self.assertEqual(self.file_row()["current_version_id"], current["id"])
        self.assertEqual(self.rows("SELECT id FROM cleanup_batches"), [])

    def test_an_ambiguous_delete_is_settled_by_exact_listing(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        delete = self.provider.delete_objects

        def lost_answer(**kwargs):
            delete(**kwargs)
            raise ConnectionError("connection reset after the request")
        self.provider.delete_objects = lost_answer
        self.run_purge(scheduled.job_ids[0])
        self.assertEqual(self.statuses(scheduled.group_id), ["completed"])
        self.assert_deleted(current)
        self.assertEqual(self.provider.delete_requests, 1, "never sent twice")

    def test_an_unrecorded_delete_is_settled_on_restart(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        with patch("app.services.cloud_deletion.record_deleted",
                   side_effect=RuntimeError("database went away")):
            self.run_purge(scheduled.job_ids[0])
        self.assertEqual(self.statuses(scheduled.group_id), ["cleaning"], "held, never failed")
        self.assertEqual(len(self.rows("SELECT id FROM cleanup_batches")), 1)
        self.expire_group(scheduled.group_id)
        storage.reconcile_interrupted_jobs()
        self.assertEqual(self.statuses(scheduled.group_id), ["completed"])
        self.assert_deleted(current)
        self.assertEqual(self.provider.delete_requests, 1)

    def test_a_crash_before_the_delete_revokes_the_grant_and_resumes(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        lead = self.claim_group(scheduled.job_ids[0])
        with self.db() as connection:
            vault = cloud_deletion.lock_vault(connection, 2)
            jobs = connection.execute("SELECT * FROM jobs WHERE id=%s", (lead["id"],)).fetchall()
            cloud_deletion.freeze_grants(connection, vault=vault, group_id=scheduled.group_id,
                                         jobs=jobs, provider="test")
        self.expire_group(scheduled.group_id)
        storage.reconcile_interrupted_jobs()
        self.assertEqual(self.statuses(scheduled.group_id), ["queued"])
        self.assertEqual(self.rows("SELECT id FROM cleanup_batches"), [], "the open grant is revoked")
        self.assertEqual(self.provider.deleted, [])
        self.run_purge(scheduled.job_ids[0])
        self.assertEqual(self.statuses(scheduled.group_id), ["completed"])
        self.assert_deleted(current)

    def test_a_lost_claim_before_the_delete_deletes_nothing(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        lead = self.claim_group(scheduled.job_ids[0])
        with self.db() as connection:
            connection.execute("UPDATE job_leases SET token=%s WHERE job_id=%s",
                               (str(uuid.uuid4()), lead["id"]))
        storage.process_job(lead)
        self.assertEqual(self.provider.deleted, [])
        self.assertEqual(self.file_row()["current_version_id"], current["id"])

    # -- schema --------------------------------------------------------------

    def test_the_schema_grants_only_the_consented_current_of_a_claimed_purge(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        insert = """INSERT INTO cleanup_batches(vault_id, file_id, purge_group_id, provider,
                                                 path_since, source_root)
                    SELECT 2, id, %s, 'p', path_since, 's' FROM files WHERE id=%s RETURNING id"""
        with self.assertRaises(psycopg.errors.RestrictViolation), self.db() as connection:
            connection.execute(insert, (scheduled.group_id, self.file_id))  # not claimed
        self.claim_group(scheduled.job_ids[0])
        with self.db() as connection:
            batch = connection.execute(insert, (scheduled.group_id, self.file_id)).fetchone()
            connection.execute(
                "INSERT INTO cleanup_candidates(batch_id, file_id, archive_version_id) VALUES (%s, %s, %s)",
                (batch["id"], self.file_id, current["id"]))
        with self.assertRaises(psycopg.errors.RestrictViolation), self.db() as connection:
            connection.execute("DELETE FROM archive_versions WHERE id=%s", (current["id"],))
        with self.db() as connection:  # an unexecuted grant may be revoked
            connection.execute("DELETE FROM cleanup_candidates WHERE batch_id=%s", (batch["id"],))
            connection.execute("DELETE FROM cleanup_batches WHERE id=%s", (batch["id"],))
        with self.assertRaises(psycopg.errors.CheckViolation), self.db() as connection:
            connection.execute(
                """INSERT INTO cleanup_batches(vault_id, file_id, keeper_id, purge_group_id, provider,
                                               path_since, source_root)
                   SELECT 2, id, current_version_id, %s, 'p', path_since, 's' FROM files WHERE id=%s""",
                (scheduled.group_id, self.file_id))

    # -- remediation ---------------------------------------------------------

    def test_remediation_is_suspended_while_a_deletion_is_pending(self) -> None:
        self.cloud_only()
        self.schedule()
        with self.db() as connection:
            state = upload_remediation.work(connection, self.file_id)
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            self.assertFalse(upload_remediation.is_open(state))
            self.assertEqual(upload_remediation._suspension(
                connection, state, vault, None, automatic=False), "pending_deletion")

    # -- Stop Protection -----------------------------------------------------

    def test_stop_and_keep_releases_the_root_and_keeps_the_cloud(self) -> None:
        current = self.cloud_only()
        status = self.stop("keep")
        self.assertEqual((status["stopped"], status["root_released"], status["cloud_disposition"]),
                         (True, True, "keep"))
        self.assertEqual(self.lifecycle(), "tombstone")
        self.assert_untouched(current)

    def test_stop_and_delete_holds_the_root_until_the_deletion_ends(self) -> None:
        current = self.cloud_only()
        status = self.stop("delete")
        self.assertEqual((status["cloud_disposition"], status["root_released"], status["cancellable"]),
                         ("delete", False, True))
        self.assertEqual(self.lifecycle(), "stopping")
        group_id = status["group_id"]
        with self.db() as connection:
            connection.execute(
                "UPDATE jobs SET requested_at=%s, run_after=now() - interval '1 hour' WHERE group_id=%s",
                (PAST, group_id))
        [job_id] = [j["id"] for j in status["jobs"]]
        self.run_purge(job_id)
        self.assertEqual(self.statuses(group_id), ["completed"])
        self.assert_deleted(current)
        protection_stop.reconcile_all()
        self.assertEqual(self.lifecycle(), "tombstone")

    def test_cancelling_a_stop_deletion_keeps_the_cloud_and_releases_the_root(self) -> None:
        current = self.cloud_only()
        status = self.stop("delete")
        with self.db() as connection:
            status = protection_stop.cancel_deletion(connection, vault_id=2, actor="owner")
        self.assertEqual((status["cloud_disposition"], status["root_released"]), ("keep", True))
        self.assert_untouched(current)

    def test_keep_after_a_failed_stop_deletion_releases_the_root(self) -> None:
        current = self.cloud_only()
        status = self.stop("delete")
        with self.db() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed', finished_at=now() WHERE group_id=%s",
                (status["group_id"],))
        protection_stop.reconcile_all()
        self.assertEqual(self.lifecycle(), "stopping", "fails closed until the Owner chooses")
        status = self.stop("keep")
        self.assertEqual((status["cloud_disposition"], status["root_released"]), ("keep", True))
        self.assert_untouched(current)

    def test_stop_waits_for_active_work(self) -> None:
        self.cloud_only()
        self.schedule()
        with self.assertRaisesRegex(ValueError, "Wait for active Jobs"):
            self.stop("keep")
        self.assertEqual(self.lifecycle(), "active")

    # -- Problems of a stopped Vault (#715) -----------------------------------

    def open_problem(self, key: str, event: str, **params) -> None:
        with self.db() as connection:
            inbox.set_problem(connection, key=key, event=event, vault_id=2,
                              active=True, email=False, params=params)

    def problem(self, key: str) -> str:
        return self.rows("SELECT problem_status FROM notifications WHERE dedupe_key=%s",
                         key)[0]["problem_status"]

    def test_stop_closes_every_open_problem_of_the_vault(self) -> None:
        self.cloud_only()
        self.open_problem("delay:2", "protection_delayed", pending=8, stalled=8)
        self.open_problem("blocked:2:source", "protection_blocked", component="source")
        self.open_problem("file_unreadable:2:x", "file_unreadable", path="x.txt")
        with self.db() as connection:
            self.assertEqual(inbox.count_open_problems(connection), 3)
        self.stop("keep")
        for key in ("delay:2", "blocked:2:source", "file_unreadable:2:x"):
            self.assertEqual(self.problem(key), "resolved", key)
        [row] = self.rows("SELECT resolved_at, read_at FROM notifications WHERE dedupe_key='delay:2'")
        self.assertIsNotNone(row["resolved_at"])
        self.assertIsNotNone(row["read_at"])

    def test_an_already_open_problem_of_a_tombstone_vault_closes_on_reconcile(self) -> None:
        self.open_problem("delay:2", "protection_delayed", pending=8, stalled=8)
        with self.db() as connection:
            # A row left open by an earlier release, before this rule existed.
            connection.execute("UPDATE vaults SET lifecycle='tombstone', root_released_at=now() WHERE id=2")
            self.assertEqual(inbox.count_open_problems(connection), 0)
        self.assertEqual(self.problem("delay:2"), "resolved")

    def test_a_stopped_vault_never_reopens_a_problem(self) -> None:
        self.cloud_only()
        self.open_problem("delay:2", "protection_delayed", pending=1, stalled=1)
        self.stop("delete")
        self.assertEqual(self.lifecycle(), "stopping")
        self.assertEqual(self.problem("delay:2"), "resolved")
        self.open_problem("delay:2", "protection_delayed", pending=2, stalled=2)
        self.assertEqual(self.problem("delay:2"), "resolved")
        with self.db() as connection:
            monitor.maintain_notifications(connection, now=datetime.now(timezone.utc))
            self.assertEqual(inbox.count_open_problems(connection), 0)
        self.assertEqual(self.problem("delay:2"), "resolved")

    def test_a_tombstone_vault_opens_no_new_problem(self) -> None:
        self.cloud_only()
        self.stop("keep")
        self.open_problem("blocked:2:source", "protection_blocked", component="source")
        self.assertEqual(self.rows("SELECT id FROM notifications WHERE dedupe_key='blocked:2:source'"), [])

    def failed_stop_deletion_problem(self) -> tuple[dict, int]:
        status = self.stop("delete")
        [job_id] = [j["id"] for j in status["jobs"]]
        with self.db() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed', finished_at=now() WHERE group_id=%s",
                (status["group_id"],))
            item = inbox.enqueue_notification(
                connection, vault_id=2, job_id=job_id, event="job_failed", title="",
                title_key="notification.job_failed.title", body_key="notification.job_failed.body",
                message_params={"action": "cloud-purge", "path": "report.txt"},
                dedupe_key=f"job:{job_id}:job_failed")
            connection.execute("UPDATE notifications SET problem_status='open' WHERE id=%s",
                               (item["id"],))
        return status, job_id

    def test_a_failed_stop_deletion_stays_open_while_the_request_stands(self) -> None:
        self.cloud_only()
        self.open_problem("delay:2", "protection_delayed", pending=8, stalled=8)
        _, job_id = self.failed_stop_deletion_problem()
        with self.db() as connection:
            self.assertEqual(inbox.count_open_problems(connection), 1)
        self.assertEqual(self.problem(f"job:{job_id}:job_failed"), "open")
        self.assertEqual(self.problem("delay:2"), "resolved")
        # The Owner keeps the cloud content: the request retires, the failure closes.
        self.stop("keep")
        with self.db() as connection:
            self.assertEqual(inbox.count_open_problems(connection), 0)
        self.assertEqual(self.problem(f"job:{job_id}:job_failed"), "resolved")

    def test_status_counts_local_files_that_block_cloud_deletion(self) -> None:
        with self.db() as connection:
            self.assertEqual(protection_stop.status(connection, 2)["local_files_blocking_cloud_delete"], 1)
        self.cloud_only()
        with self.db() as connection:
            self.assertEqual(protection_stop.status(connection, 2)["local_files_blocking_cloud_delete"], 0)


if __name__ == "__main__":
    unittest.main()
