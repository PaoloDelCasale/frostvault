"""Owner reauthentication, typed confirmation, cancellation and retired HTTP surface.

Fixtures from ``tests.test_cloud_deletion`` (rebuilt model, #594): the file is
Cloud-only after a real upload, in Vault 2 named ``Docs``.
"""
from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import main
from app.config import settings
from app.sessions import create_session, csrf_token_for
from tests import test_cloud_deletion as fixtures
from tests.owner_fixtures import insert_owner
from tests.postgres import database_dsn


class CloudDeletionHttpTests(fixtures.CloudDeletionFixture):
    def setUp(self):
        super().setUp()
        self.cloud_only()
        configured = replace(settings, database_dsn=database_dsn(self.db_path), cookie_secure=False)
        for target in ("app.main.settings", "app.database.settings", "app.sessions.settings"):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))
        with self.db() as connection:
            insert_owner(connection)
            token = create_session(connection,auth_method="oidc")
            csrf = csrf_token_for(connection,token)
        self.client.cookies.set(settings.session_cookie_name,token)
        self.headers = {"X-CSRF-Token":csrf}
        self.payload = {"path":"report.txt", "confirmation":"Docs", "reason":"deliberate deletion"}

    def reauth(self, recent):
        with self.db() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (
                datetime.now(timezone.utc).isoformat() if recent else "2000-01-01T00:00:00+00:00",))

    def test_recent_reauthentication_and_exact_name_required(self):
        self.reauth(False)
        denied = self.client.post("/api/cloud-purge",json=self.payload,headers=self.headers)
        self.assertEqual(denied.status_code,403,denied.text)
        self.assertEqual(denied.json(),{"error":"reauth_required"})
        self.reauth(True)
        denied = self.client.post("/api/cloud-purge",json={**self.payload,"confirmation":"wrong"},headers=self.headers)
        self.assertEqual(denied.status_code,422,denied.text)
        denied = self.client.post("/api/cloud-purge",json={**self.payload,"generated_phrase":"echo-me"},headers=self.headers)
        self.assertEqual(denied.status_code,422,denied.text)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM jobs WHERE action='cloud-purge'").fetchone()["n"],0)

    def test_preview_schedule_and_cancel_exact_seven_day_delay(self):
        self.reauth(True)
        preview = self.client.post("/api/cloud-deletion/preview",json={"path":"report.txt"},headers=self.headers)
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertEqual(preview.json()["version_count"],1)
        self.assertEqual(self.client.get("/api/cloud-deletion").json(),{"purge_delay_seconds":604800})
        scheduled = self.client.post("/api/cloud-purge",json=self.payload,headers=self.headers)
        self.assertEqual(scheduled.status_code,202,scheduled.text)
        with self.db() as connection:
            row = connection.execute("SELECT requested_at,run_after FROM jobs WHERE action='cloud-purge'").fetchone()
        self.assertEqual((row["run_after"]-row["requested_at"]).total_seconds(),604800)
        self.reauth(False)  # Cancellation never requires step-up.
        cancelled = self.client.post("/api/jobs/cancel",json={"group_id":scheduled.json()["group_id"],"action":"cloud-purge"},headers=self.headers)
        self.assertEqual(cancelled.status_code,200,cancelled.text)
        self.assertEqual(cancelled.json()["cancelled_count"],1)

    def test_actual_present_file_is_never_deletable(self):
        self.reauth(True)
        self.file.write_bytes(b"not yet seen by watcher")
        for path, body in (("/api/cloud-purge",self.payload),("/api/cloud-deletion/preview",{"path":"report.txt"})):
            response = self.client.post(path,json=body,headers=self.headers)
            self.assertEqual(response.status_code,409,response.text)

    def test_stop_delete_requires_reauth_keep_does_not_and_status_remains_accessible(self):
        self.reauth(False)
        payload = {"cloud_disposition":"delete","confirmation":"Docs","reason":"stop"}
        denied = self.client.post("/api/vaults/2/stop-protection",json=payload,headers=self.headers)
        self.assertEqual(denied.status_code,403,denied.text)
        kept = self.client.post("/api/vaults/2/stop-protection",json={**payload,"cloud_disposition":"keep"},headers=self.headers)
        self.assertEqual(kept.status_code,202,kept.text)
        self.assertTrue(kept.json()["root_released"])
        self.assertTrue(self.client.get("/api/vaults/2/stop-protection").json()["stopped"])
        me = self.client.get("/api/me").json()
        self.assertIsNone(me["vault"])
        self.assertEqual(me["stopped_vault"]["id"],2)

    def test_stopped_folder_deletion_can_be_cancelled_without_selected_operational_vault(self):
        self.reauth(True)
        response = self.client.post("/api/vaults/2/stop-protection",json={
            "cloud_disposition":"delete","confirmation":"Docs","reason":"stop"},headers=self.headers)
        self.assertEqual(response.status_code,202,response.text)
        self.assertTrue(response.json()["cancellable"])
        self.reauth(False)
        cancelled = self.client.post("/api/vaults/2/stop-protection/cancel-deletion",headers=self.headers)
        self.assertEqual(cancelled.status_code,200,cancelled.text)
        self.assertTrue(cancelled.json()["root_released"])

    def test_removed_endpoints_and_acceleration_are_absent(self):
        paths = main.app.openapi()["paths"]
        for route in ("/api/cloud-purge/accelerate","/api/vault/cloud-deletion",
                      "/api/vault/decommission","/api/vault/decommission/preview",
                      "/api/vaults/{vault_id}/decommission", "/api/vaults/{vault_id}/decommission/status"):
            self.assertNotIn(route,paths)
        for method,path in (("post","/api/cloud-purge/accelerate"),("put","/api/vault/cloud-deletion"),
                            ("post","/api/vaults/2/decommission")):
            response = getattr(self.client,method)(path,json={},headers=self.headers)
            self.assertEqual(response.status_code,404,response.text)

    def test_csrf_and_authentication_still_gate_deletion(self):
        response = self.client.post("/api/cloud-purge",json=self.payload)
        self.assertEqual(response.status_code,403)
        self.client.cookies.clear()
        response = self.client.post("/api/cloud-purge",json=self.payload)
        self.assertIn(response.status_code,(401,403))
