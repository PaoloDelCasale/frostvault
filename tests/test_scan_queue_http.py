"""POST /api/scan on a busy Vault queues the scan instead of dropping it (#759)."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from app import storage
from app.services import runtime_status
from tests.fixtures import FrostVaultTestCase, seed_vault


class ScanQueueHttpTests(FrostVaultTestCase):
    def setUp(self) -> None:
        super().setUp()
        root = self.tmp_path / "photos"
        root.mkdir()
        with self.database.connect() as connection:
            self.vault_id = int(seed_vault(connection, source_root=str(root), name="Immich")["id"])
        self.addCleanup(runtime_status.runtime_status.pop, self.vault_id, None)
        self.addCleanup(runtime_status.scan_locks.pop, self.vault_id, None)
        self.addCleanup(storage._queued_scans.pop, self.vault_id, None)
        self.enterContext(patch("app.storage.QUEUED_SCAN_POLL_SECONDS", 0.01))
        self.client = self.http_client()
        self.sign_in()

    def scan(self):
        return self.client.post(f"/api/scan?vault_id={self.vault_id}", headers=self.signed_in.csrf_headers)

    def test_a_scan_requested_while_admission_holds_the_vault_is_queued_and_runs_after_it(self) -> None:
        lock = runtime_status.scan_lock_for_vault(self.vault_id)
        lock.acquire(operation="auto_admission")
        released = False
        try:
            with patch("app.storage.scan_vault", return_value={"local": 0}) as scan:
                response = self.scan()
                self.assertEqual(response.status_code, 202, response.text)
                body = response.json()
                self.assertTrue(body["queued"])
                self.assertEqual(body["message_key"], "api.scan_queued")
                self.assertEqual(body["waiting_for"], "auto_admission")
                self.assertEqual(body["waiting_since"], lock.holder()["since"])
                self.assertIn("automatic upload admission", body["message"])
                waiting = runtime_status.snapshot_runtime_status_for_stats(self.vault_id)
                self.assertTrue(waiting["scan_queued"])
                self.assertEqual(waiting["scan_waiting_for"], "auto_admission")
                scan.assert_not_called()
                lock.release()
                released = True
                for _ in range(500):
                    if scan.called:
                        break
                    time.sleep(0.01)
                scan.assert_called_once()
        finally:
            if not released:
                lock.release()

    def test_a_free_vault_starts_the_scan_at_once(self) -> None:
        with patch("app.storage.scan_vault", return_value={"local": 0}) as scan:
            response = self.scan()
            self.assertEqual(response.status_code, 202, response.text)
            self.assertFalse(response.json()["queued"])
            self.assertEqual(response.json()["message_key"], "api.scan_started")
            for _ in range(500):
                if scan.called:
                    break
                time.sleep(0.01)
            scan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
