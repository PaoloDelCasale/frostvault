from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.sessions import create_session
from tests.postgres import PostgresConnection, database_dsn
from tests.spa_fixture import write_spa_dist
from tests.test_database import run_alembic


class ReconnectRecoveryHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog"
        result = run_alembic(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
        configured = replace(settings, database_dsn=database_dsn(str(self.db_path)), cookie_secure=False,
                             archive_master_key=Fernet.generate_key().decode(), vault_s3_bucket="test-bucket",
                             frontend_dist_dir=str(write_spa_dist(Path(self.tmp.name))))
        for target in ("app.main.settings", "app.database.settings", "app.sessions.settings", "app.services.recovery_kit.settings"):
            patcher = patch(target, configured); patcher.start(); self.addCleanup(patcher.stop)
        self.settings = configured

    def client_for(self, stale=False):
        client = TestClient(app, client=("127.0.0.1", 50000))
        with PostgresConnection(str(self.db_path)) as connection:
            token = create_session(connection, user_id=1, auth_method="oidc")
            if stale:
                connection.execute("UPDATE sessions SET reauth_at='2000-01-01T00:00:00+00:00' WHERE user_id=1")
        client.cookies.set(self.settings.session_cookie_name, token)
        return client, {"X-CSRF-Token": client.get("/api/me").json()["csrf_token"]}

    def test_reconnect_needs_session_csrf_but_not_recent_reauthentication(self):
        client, headers = self.client_for(stale=True)
        payload = {"volume_alias": "photos", "relative_path": "new", "reason": "restore"}
        self.assertEqual(client.post("/api/vaults/7/reconnect", json=payload, headers=headers).status_code, 404)
        self.assertEqual(client.post("/api/vaults/7/reconnect", json=payload).status_code, 403)
        self.assertEqual(client.post("/api/vaults/7/relocate", json=payload, headers=headers).status_code, 404)

    def test_kit_requires_authentication_and_recent_reauthentication(self):
        anonymous = TestClient(app)
        self.assertIn(anonymous.get("/api/recovery-kit/download").status_code, (401, 403))
        client, _ = self.client_for(stale=True)
        with patch("app.services.recovery_kit.build_recovery_kit") as build:
            response = client.get("/api/recovery-kit/download")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "reauth_required"})
        build.assert_not_called()

    def test_no_store_download_is_non_catalog_and_audited_without_secret(self):
        client, _ = self.client_for()
        # Session creation is a fresh identity proof.
        with self.assertLogs("app.audit", level="WARNING") as logs:
            response = client.get("/api/recovery-kit/download")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        kit = response.json()
        self.assertEqual(set(kit), {"format", "fingerprint", "ARCHIVE_MASTER_KEY", "bucket", "region", "instructions"})
        self.assertTrue(kit["ARCHIVE_MASTER_KEY"] == self.settings.archive_master_key)
        with PostgresConnection(str(self.db_path)) as connection:
            event = connection.execute("SELECT detail_json FROM audit_events WHERE event='recovery_kit_downloaded'").fetchone()
        self.assertNotIn(self.settings.archive_master_key, "\n".join(logs.output) + event["detail_json"])
        self.assertIn("s3api restore-object", kit["instructions"])

    def test_one_custody_is_fingerprint_bound_and_csrf_protected(self):
        client, headers = self.client_for()
        status = client.get("/api/recovery-kit").json()
        self.assertFalse(status["custody_acknowledged"])
        self.assertNotIn(self.settings.archive_master_key, json.dumps(status))
        payload = {"fingerprint": status["fingerprint"]}
        self.assertEqual(client.post("/api/recovery-kit/custody", json=payload).status_code, 403)
        response = client.post("/api/recovery-kit/custody", json=payload, headers=headers)
        self.assertTrue(response.json()["custody_acknowledged"], response.text)
        changed = replace(self.settings, archive_master_key=Fernet.generate_key().decode())
        with patch("app.services.recovery_kit.settings", changed):
            current = client.get("/api/recovery-kit").json()
            self.assertNotEqual(current["fingerprint"], status["fingerprint"])
            self.assertFalse(current["custody_acknowledged"])
            self.assertEqual(client.post("/api/recovery-kit/custody", json=payload, headers=headers).status_code, 409)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM recovery_kit_custody").fetchone()["n"], 1)


if __name__ == "__main__": unittest.main()
