"""Owner reauthentication, typed confirmation, cancellation and retired HTTP surface."""
from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import main
from app.config import settings
from app.sessions import create_session, csrf_token_for
from tests import test_cloud_deletion as fixtures
from tests.postgres import database_dsn


class CloudDeletionHttpTests(unittest.TestCase):
    connection = fixtures.CloudDeletionTests.connection
    add_file = fixtures.CloudDeletionTests.add_file

    def setUp(self):
        fixtures.CloudDeletionTests.setUp(self)
        configured = replace(settings, database_dsn=database_dsn(str(self.path)), cookie_secure=False)
        for target in ("app.main.settings", "app.database.settings", "app.sessions.settings"):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))
        with self.connection() as connection:
            token = create_session(connection,user_id=1,auth_method="oidc")
            csrf = csrf_token_for(connection,token)
        self.client.cookies.set(settings.session_cookie_name,token)
        self.headers = {"X-CSRF-Token":csrf}
        self.payload = {"path":"photo.jpg", "confirmation":"Photos", "reason":"deliberate deletion"}

    def reauth(self, recent):
        with self.connection() as connection:
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
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"],0)

    def test_preview_schedule_and_cancel_exact_seven_day_delay(self):
        self.reauth(True)
        preview = self.client.post("/api/cloud-deletion/preview",json={"path":"photo.jpg"},headers=self.headers)
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertEqual(preview.json()["version_count"],2)
        self.assertEqual(self.client.get("/api/cloud-deletion").json(),{"purge_delay_seconds":604800})
        scheduled = self.client.post("/api/cloud-purge",json=self.payload,headers=self.headers)
        self.assertEqual(scheduled.status_code,202,scheduled.text)
        with self.connection() as connection:
            row = connection.execute("SELECT requested_at,pending_until FROM jobs").fetchone()
        self.assertEqual((datetime.fromisoformat(row["pending_until"])-datetime.fromisoformat(row["requested_at"])).total_seconds(),604800)
        self.reauth(False)  # Cancellation never requires step-up.
        cancelled = self.client.post("/api/jobs/cancel",json={"group_id":scheduled.json()["group_id"],"action":"cloud-purge"},headers=self.headers)
        self.assertEqual(cancelled.status_code,200,cancelled.text)
        self.assertEqual(cancelled.json()["cancelled_count"],1)

    def test_actual_present_file_is_never_deletable(self):
        self.reauth(True)
        (self.source / "photo.jpg").write_bytes(b"not yet seen by watcher")
        for path, body in (("/api/cloud-purge",self.payload),("/api/cloud-deletion/preview",{"path":"photo.jpg"})):
            response = self.client.post(path,json=body,headers=self.headers)
            self.assertEqual(response.status_code,409,response.text)

    def test_stop_delete_requires_reauth_keep_does_not_and_status_remains_accessible(self):
        self.reauth(False)
        payload = {"cloud_disposition":"delete","confirmation":"Photos","reason":"stop"}
        denied = self.client.post("/api/vaults/1/stop-protection",json=payload,headers=self.headers)
        self.assertEqual(denied.status_code,403,denied.text)
        kept = self.client.post("/api/vaults/1/stop-protection",json={**payload,"cloud_disposition":"keep"},headers=self.headers)
        self.assertEqual(kept.status_code,202,kept.text)
        self.assertTrue(kept.json()["root_released"])
        self.assertTrue(self.client.get("/api/vaults/1/stop-protection").json()["stopped"])
        me = self.client.get("/api/me").json()
        self.assertIsNone(me["vault"])
        self.assertEqual(me["stopped_vault"]["id"],1)

    def test_stopped_folder_deletion_can_be_cancelled_without_selected_operational_vault(self):
        self.reauth(True)
        response = self.client.post("/api/vaults/1/stop-protection",json={
            "cloud_disposition":"delete","confirmation":"Photos","reason":"stop"},headers=self.headers)
        self.assertEqual(response.status_code,202,response.text)
        self.assertTrue(response.json()["cancellable"])
        self.reauth(False)
        cancelled = self.client.post("/api/vaults/1/stop-protection/cancel-deletion",headers=self.headers)
        self.assertEqual(cancelled.status_code,200,cancelled.text)
        self.assertTrue(cancelled.json()["root_released"])

    def test_removed_endpoints_and_acceleration_are_absent(self):
        paths = main.app.openapi()["paths"]
        for route in ("/api/cloud-purge/accelerate","/api/vault/cloud-deletion",
                      "/api/vault/decommission","/api/vault/decommission/preview",
                      "/api/vaults/{vault_id}/decommission", "/api/vaults/{vault_id}/decommission/status"):
            self.assertNotIn(route,paths)
        for method,path in (("post","/api/cloud-purge/accelerate"),("put","/api/vault/cloud-deletion"),
                            ("post","/api/vaults/1/decommission")):
            response = getattr(self.client,method)(path,json={},headers=self.headers)
            self.assertEqual(response.status_code,404,response.text)

    def test_csrf_and_authentication_still_gate_deletion(self):
        response = self.client.post("/api/cloud-purge",json=self.payload)
        self.assertEqual(response.status_code,403)
        self.client.cookies.clear()
        response = self.client.post("/api/cloud-purge",json=self.payload)
        self.assertIn(response.status_code,(401,403))
