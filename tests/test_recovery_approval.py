"""Request expiry, selection admission and serialized approval/cancellation (#536)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

from fastapi import HTTPException

from app import main
from app.services import notifications, recovery_approval
from tests.postgres import PostgresConnection
from tests import test_restore_guardrails as fixtures


class RecoveryApprovalTests(unittest.TestCase):
    # Reuse the realistic disposable catalog fixture, not its tests.
    setUp = fixtures.GroupRestoreApprovalTests.setUp
    _runtime = fixtures.GroupRestoreApprovalTests._runtime
    _jobs = fixtures.GroupRestoreApprovalTests._jobs
    _client = fixtures.GroupRestoreApprovalTests._client
    _run_worker = fixtures.GroupRestoreApprovalTests._run_worker
    _queue_and_estimate = fixtures.GroupRestoreApprovalTests._queue_and_estimate
    COLD_FILES = 24
    FILE_GIB = 5

    def _expire_deadline(self):
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE jobs SET pending_until='2000-01-01T00:00:00+00:00'")

    def _approve(self, group):
        return main.approve_recover(main.RecoverApproveAction(group_id=group), {"id": 1}, {"id": 2})

    def test_expiry_cancels_whole_request_with_localized_reason_and_no_cloud_calls(self):
        _, queued = self._queue_and_estimate(self._runtime())
        self._expire_deadline()
        client = self._client()
        self._run_worker(client, fixtures._worker_settings(), rounds=2)
        client.head_object.assert_not_called()
        client.restore_object.assert_not_called()
        client.get_object.assert_not_called()
        with PostgresConnection(str(self.database_path)) as connection:
            rows = connection.execute("SELECT status, message_key, approved_at FROM jobs").fetchall()
        self.assertEqual(len(rows), queued["item_count"])
        self.assertEqual({row["status"] for row in rows}, {"cancelled"})
        self.assertEqual({row["message_key"] for row in rows}, {"job.recovery_approval_expired"})
        self.assertTrue(all(row["approved_at"] is None for row in rows))

    def test_late_approval_refused_and_expiry_committed_without_waiting_for_worker(self):
        _, queued = self._queue_and_estimate(self._runtime())
        self._expire_deadline()
        with patch("app.database.settings", self.database_settings):
            with self.assertRaises(HTTPException) as raised:
                self._approve(queued["group_id"])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual({j["status"] for j in self._jobs().values()}, {"cancelled"})

    def test_deadline_is_inclusive_and_offsets_compare_as_instants(self):
        _, queued = self._queue_and_estimate(self._runtime())
        deadline = "2030-01-01T01:00:00+01:00"
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE jobs SET pending_until=%s", (deadline,))
            connection.execute("SELECT id FROM vaults WHERE id=2 FOR UPDATE")
            self.assertEqual(recovery_approval.expire_pending(connection, vault_id=2, now="2029-12-31T23:59:59.999999+00:00"), 0)
            self.assertEqual(recovery_approval.expire_pending(connection, vault_id=2, now="2030-01-01T00:00:00+00:00"), queued["item_count"])

    def test_approved_request_is_not_expired_even_with_stale_deadline(self):
        _, queued = self._queue_and_estimate(self._runtime())
        with patch("app.database.settings", self.database_settings):
            self._approve(queued["group_id"])
        with PostgresConnection(str(self.database_path)) as connection:
            self.assertTrue(all(row["pending_until"] is None for row in connection.execute("SELECT pending_until FROM jobs").fetchall()))
            connection.execute("UPDATE jobs SET pending_until='2000-01-01T00:00:00+00:00'")
            self.assertEqual(recovery_approval.sweep_expired(connection, now=datetime.now(timezone.utc).isoformat()), 0)
        self.assertEqual({j["status"] for j in self._jobs().values()}, {"queued"})

    def test_expiry_approval_and_cancel_race_has_no_partial_or_late_approval(self):
        _, queued = self._queue_and_estimate(self._runtime())
        self._expire_deadline()
        def approve():
            try:
                self._approve(queued["group_id"])
            except HTTPException as error:
                self.assertEqual(error.status_code, 409)
        def cancel():
            try:
                main.cancel_job_group(queued["group_id"], "recover", {"id": 2}, 1)
            except HTTPException as error:
                self.assertEqual(error.status_code, 409)
        def expire():
            with PostgresConnection(str(self.database_path)) as connection:
                recovery_approval.sweep_expired(connection, now=datetime.now(timezone.utc).isoformat())
        with patch("app.database.settings", self.database_settings), ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(call) for call in (approve, cancel, expire)]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual({j["status"] for j in self._jobs().values()}, {"cancelled"})
        self.assertTrue(all(j["approved_at"] is None for j in self._jobs().values()))

    def test_live_approval_and_cancel_race_serializes_whole_group(self):
        _, queued = self._queue_and_estimate(self._runtime())
        def approve():
            try:
                self._approve(queued["group_id"])
            except HTTPException as error:
                self.assertEqual(error.status_code, 409)
        with patch("app.database.settings", self.database_settings), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve), pool.submit(main.cancel_job_group, queued["group_id"], "recover", {"id": 2}, 1)]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual({j["status"] for j in self._jobs().values()}, {"cancelled"})
        self.assertEqual(len({j["approved_at"] for j in self._jobs().values()}), 1)

    def test_separate_files_are_one_request_priced_on_selection_total(self):
        selection = [(f"photos/p{i:02d}.jpg", False) for i in range(self.COLD_FILES)]
        with patch("app.database.settings", self.database_settings), patch("app.main.settings", self._runtime()):
            estimate = main.recover_estimate(main.RecoverEstimateRequest(selection=[{"path": p} for p, _ in selection]), {"id": 2})
            queued = main.queue_jobs("", "recover", 2, 1, recovery_selection=selection)
            approved = self._approve(queued["group_id"])
        self.assertTrue(estimate["high_impact"])
        self.assertTrue(queued["approval_required"])
        self.assertEqual(queued["item_count"], self.COLD_FILES)
        self.assertEqual(queued["total_bytes"], self.COLD_FILES * self.FILE_GIB * fixtures.GIB)
        self.assertAlmostEqual(estimate["estimate"]["estimated_cost_eur"], queued["estimated_cost_eur"], places=6)
        self.assertEqual(sorted(approved["job_ids"]), sorted(queued["job_ids"]))
        self.assertEqual({j["status"] for j in self._jobs().values()}, {"queued"})

    def test_overlapping_folders_files_deduplicate_for_estimate_queue_and_bulk_cancel(self):
        selection = [("photos", True), ("photos/p00.jpg", False), ("photos", True)]
        with patch("app.database.settings", self.database_settings), patch("app.main.settings", self._runtime()):
            estimate = main.recover_estimate(main.RecoverEstimateRequest(selection=[{"path": p, "is_directory": d} for p, d in selection]), {"id": 2})
            queued = main.queue_jobs("", "recover", 2, 1, recovery_selection=selection)
            cancelled = main.cancel_job_group(queued["group_id"], "recover", {"id": 2}, 1)
        self.assertEqual(queued["item_count"], self.COLD_FILES + 1)
        self.assertEqual(cancelled["cancelled_count"], queued["item_count"])
        self.assertAlmostEqual(estimate["estimate"]["estimated_cost_eur"], queued["estimated_cost_eur"], places=6)
        self.assertEqual({j["status"] for j in self._jobs().values()}, {"cancelled"})

    def test_one_notification_per_request_and_optional_email_deduplicated(self):
        with patch("app.services.notifications._smtp_config", return_value={"configured": True}):
            _, queued = self._queue_and_estimate(self._runtime())
            with PostgresConnection(str(self.database_path)) as connection:
                notifications.enqueue_recovery_approval_best_effort(connection, job_ids=queued["job_ids"])
                notices = connection.execute("SELECT * FROM notifications WHERE event='recovery_approval'").fetchall()
                deliveries = connection.execute("SELECT * FROM notification_deliveries").fetchall()
        self.assertEqual(len(notices), 1)
        self.assertEqual(len(deliveries), 1)
        self.assertTrue(notices[0]["in_app_enabled"])
        self.assertIn(queued["group_id"], notices[0]["dedupe_key"])
        self.assertIsNone(notices[0]["job_id"], "request notice, not per-file")
        self.assertIn("approvazione", notifications._row_notification(notices[0], locale="it")["title"])

    def test_notification_sql_failure_does_not_break_queue_transaction(self):
        def broken(connection, **kwargs):
            connection.execute("SELECT 1/0")
        with patch("app.services.notifications.enqueue_notification", side_effect=broken):
            _, queued = self._queue_and_estimate(self._runtime())
        self.assertEqual(len(self._jobs()), queued["item_count"])
        self.assertEqual({j["status"] for j in self._jobs().values()}, {"pending_approval"})
        with PostgresConnection(str(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM notifications").fetchone()["n"], 0)
