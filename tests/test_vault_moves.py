"""Vault moves on the rebuilt model (#595, ADR-0035).

A move admits one colder-only ``storage-class`` Job per eligible file along the
saved Storage Plan; the worker copies the exact verified Current through the
shared copy engine and removes the superseded version by exact VersionId. A
move never warms a file and never requests a restore, also when the source
class changed after admission.
"""
from __future__ import annotations

import json
import unittest

from app import storage
from app.services import vault_moves
from tests.test_rename_sync import CopyWorkerFixture

PLAN = {"transitions": [{"days": 30, "storage_class": "STANDARD_IA"},
                        {"days": 120, "storage_class": "GLACIER"},
                        {"days": 300, "storage_class": "DEEP_ARCHIVE"}]}


class VaultMoveTests(CopyWorkerFixture):
    def setUp(self) -> None:
        super().setUp()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET storage_plan=%s::jsonb WHERE id=2", (json.dumps(PLAN),))

    def schedule(self, target: str, *, acknowledged: bool = True) -> dict:
        with self.db() as connection:
            return vault_moves.schedule(connection, vault_id=2, target=target, user_id=1,
                                        acknowledged=acknowledged, timestamp=storage.now_iso())

    def test_a_move_admits_colder_only_jobs_and_the_worker_copies_exactly(self) -> None:
        before = self.protected()
        with self.db() as connection:
            preview = vault_moves.preview(connection, 2)
        self.assertEqual([t["eligible_count"] for t in preview["targets"]], [1, 1, 1])
        result = self.schedule("GLACIER")
        (job_id,) = result["job_ids"]
        self.assertEqual(self.rows("SELECT colder_only, target_storage_class FROM job_storage_class_changes "
                                   "WHERE job_id=%s", job_id),
                         [{"colder_only": True, "target_storage_class": "GLACIER"}])
        self.run_job(job_id)
        current = self.assert_current(job_id, b"immutable content A")
        self.assertEqual(current["storage_class"], "GLACIER")
        self.assertIn((self.key, before["provider_version_id"]), self.provider.deleted)
        self.assertEqual(self.provider.copies, 1)

    def test_a_move_needs_acknowledgement_a_plan_class_and_an_active_vault(self) -> None:
        self.protected()
        with self.assertRaises(vault_moves.MoveBlocked):
            self.schedule("GLACIER", acknowledged=False)
        with self.assertRaises(vault_moves.MoveBlocked):
            self.schedule("GLACIER_IR")
        with self.db() as connection:
            connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=2")
        with self.assertRaises(vault_moves.MoveBlocked):
            self.schedule("GLACIER")
        self.assertEqual(self.rows("SELECT id FROM jobs WHERE action='storage-class'"), [])

    def test_an_unrestored_cold_file_is_never_admitted(self) -> None:
        before = self.protected()
        self.provider.classes[self.key, before["provider_version_id"]] = "GLACIER"
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET storage_class='GLACIER' WHERE id=%s",
                               (before["id"],))
        with self.assertRaisesRegex(vault_moves.MoveBlocked, "No eligible files"):
            self.schedule("DEEP_ARCHIVE")
        self.assertEqual(self.provider.restores, {})

    def test_the_worker_refuses_a_source_that_is_no_longer_warmer(self) -> None:
        before = self.protected()
        (job_id,) = self.schedule("STANDARD_IA")["job_ids"]
        # The exact source moved to GLACIER after admission (lifecycle rule).
        self.provider.classes[self.key, before["provider_version_id"]] = "GLACIER"
        self.run_job(job_id)
        job = self.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("colder-only", job["message"])
        self.assertEqual((self.provider.copies, self.provider.restores, self.attempts()), (0, {}, []))
        self.assertEqual(self.file_row()["current_version_id"], before["id"])


if __name__ == "__main__":
    unittest.main()
