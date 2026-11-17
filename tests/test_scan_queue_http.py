"""POST /api/scan on a busy Vault queues the scan instead of dropping it (#759)."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, storage
from app.config import settings
from app.services import runtime_status
from app.sessions import create_session, csrf_token_for
from tests.owner_fixtures import insert_owner, insert_vault
from tests.postgres import PostgresConnection, database_dsn
from tests.test_database import run_alembic


class ScanQueueHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        root = Path(self.tmp.name) / "photos"
        root.mkdir()
        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection)
            self.vault_id = int(insert_vault(connection, source_root=str(root), name="Immich")["id"])
        self.addCleanup(runtime_status.runtime_status.pop, self.vault_id, None)
        self.addCleanup(runtime_status.scan_locks.pop, self.vault_id, None)
        self.addCleanup(storage._queued_scans.pop, self.vault_id, None)
        test_settings = replace(settings, database_dsn=database_dsn(str(self.path)), cookie_secure=False)
        for patcher in (
            patch("app.main.settings", test_settings),
            patch("app.database.settings", test_settings),
            patch("app.sessions.settings", test_settings),
            patch("app.storage.QUEUED_SCAN_POLL_SECONDS", 0.01),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))
        with PostgresConnection(str(self.path)) as connection:
            token = create_session(connection, auth_method="oidc")
            csrf = csrf_token_for(connection, token)
        self.client.cookies.set(settings.session_cookie_name, token)
        self.client.cookies.set(settings.csrf_cookie_name, csrf)
        self.headers = {"X-CSRF-Token": csrf}

    def scan(self):
        return self.client.post(f"/api/scan?vault_id={self.vault_id}", headers=self.headers)

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
