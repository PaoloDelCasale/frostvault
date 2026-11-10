"""DB-free request-scope, deep-link and Owner shell contracts (#430)."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import main
from app.catalog import ArchiveCatalog
from app.services.notification_links import notification_path, notification_url
from app.services.operation_policies import OperationPolicy


class EssentialInterfaceTests(unittest.TestCase):
    def request(self):
        return SimpleNamespace(state=SimpleNamespace(session={"id": 5, "vault_id": 1, "transition_version": 9}))

    @contextmanager
    def connection(self, connection):
        yield connection

    def test_explicit_scope_ignores_session_selection_and_never_mutates_it(self):
        connection = Mock(); connection.execute.return_value.fetchone.return_value = {"id": 2, "name": "Second"}
        request = self.request()
        with patch.object(main, "db", lambda: self.connection(connection)), patch.object(main, "set_session_vault") as select:
            vault = main.current_vault(request, {"id": 1}, vault_id=2)
        self.assertEqual(vault["id"], 2)
        self.assertEqual(request.state.session["vault_id"], 1)
        select.assert_not_called()
        self.assertEqual(connection.execute.call_args.args[1], (2,))

    def test_explicit_invalid_or_stopped_scope_does_not_fall_back_to_another_vault(self):
        connection = Mock(); connection.execute.return_value.fetchone.return_value = None
        with patch.object(main, "db", lambda: self.connection(connection)), patch.object(main, "set_session_vault") as select:
            with self.assertRaises(HTTPException) as raised:
                main.current_vault(self.request(), {"id": 1}, vault_id=99)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(connection.execute.call_count, 1)
        select.assert_not_called()

    def test_all_vault_scoped_contracts_accept_explicit_identity(self):
        paths = main.app.openapi()["paths"]
        for path in ["/api/files", "/api/files/detail", "/api/files/versions", "/api/recover", "/api/free-space", "/api/cloud-purge", "/api/jobs/cancel", "/api/vault/operation-policy", "/api/vault/lifecycle", "/api/vault/move"]:
            for method, operation in paths[path].items():
                if method in {"get", "post", "put"}:
                    self.assertIn("vault_id", [p["name"] for p in operation.get("parameters", [])], (path, method))

    def test_new_global_endpoints_require_owner_and_retired_admin_apis_are_gone(self):
        client = TestClient(main.app)
        for path in ["/api/files/search", "/api/files/detail?path=photo.jpg", "/api/general", "/api/problems"]:
            self.assertEqual(client.get(path).status_code, 401, path)
        for path in main.app.openapi()["paths"]:
            self.assertFalse(path.startswith("/api/admin/"), path)
        self.assertNotIn("/vault/access", main.app.openapi()["paths"])

    def test_deep_links_encode_vault_path_directory_and_hostile_characters(self):
        path = notification_path(2, {"path": "photos/a & #雪.jpg"})
        self.assertIn("vault_id=2", path)
        self.assertIn("directory=photos", path)
        self.assertIn("file=photos%2Fa", path)
        self.assertEqual(notification_url("https://example.test", 2, {"path": "photo.jpg"}), "https://example.test/?vault_id=2&directory=&file=photo.jpg")
        self.assertIsNone(notification_url("https://userinfo@example.com", 2, {}))
        self.assertIsNone(notification_url("javascript:alert(1)", 2, {}))
        self.assertEqual(notification_path(None, {}), "/general")

    def test_recursive_cold_recovery_estimate_has_numeric_time_and_no_version_picker(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = [
            {"storage_class": "DEEP_ARCHIVE", "bytes": 1024**3},
            {"storage_class": "STANDARD", "bytes": 12},
        ]
        with patch.object(main, "db", lambda: self.connection(connection)):
            preview = main.recover_estimate(main.RecoverEstimateRequest(path="photos", is_directory=True, restore_tier="Bulk"), {"id": 2})
        self.assertTrue(preview["requires_restore"])
        self.assertIsNone(preview["archive_version_id"])
        self.assertIsInstance(preview["estimate"]["estimated_hours"], (float, int))
        self.assertEqual(preview["estimate"]["tier"], "Bulk")
        self.assertEqual(connection.execute.call_args.args[1], [2, "photos/"])
        self.assertIn("starts_with(fp.path, %s)", connection.execute.call_args.args[0])
        self.assertTrue(all(call.args[0].startswith("SELECT") for call in connection.execute.call_args_list))

    def test_explicit_live_stream_keeps_owner_revocation_gate_but_not_shared_selection(self):
        from app.services import catalog_event_stream as stream
        auth = {"session_vault_id": 1, "revoked_at": None, "idle_expires_at": "2099-01-01T00:00:00+00:00",
                "absolute_expires_at": "2099-01-01T00:00:00+00:00", "session_session_version": 1,
                "user_session_version": 1, "vault_available": 1}
        connection = Mock()
        connection.execute.return_value.fetchone.side_effect = [auth, {"revision": 0, "retained_from_revision": 1}]
        with patch.object(stream, "db", lambda: self.connection(connection)):
            tick = stream.stream_tick_snapshot(session_id="s", user_id=1, vault_id=2, after_revision=0, explicit_vault=True)
        self.assertTrue(tick.ok)
        connection.execute.return_value.fetchone.side_effect = [{**auth, "revoked_at": "2026-01-01"}]
        with patch.object(stream, "db", lambda: self.connection(connection)):
            tick = stream.stream_tick_snapshot(session_id="s", user_id=1, vault_id=2, after_revision=0, explicit_vault=True)
        self.assertEqual(tick.error, "session_revoked")

    def test_search_is_sql_paged_and_missing_volume_cannot_match_cloud_only(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = {"total": 0}
        connection.execute.return_value.fetchall.return_value = []
        # The Vault's exclusion rules are part of the state SQL (#498); the policy
        # lookup is a separate read, so keep this test's call sequence about search.
        with patch("app.services.operation_policies.get_policy", return_value=OperationPolicy()):
            result = ArchiveCatalog(connection).search_protected_vaults([
                {"id": 1, "name": "First", "unconfirmed": False},
                {"id": 2, "name": "Missing", "unconfirmed": True},
            ], search="photo", state="cloud_only", page=2, page_size=10)
        self.assertEqual(result["items"], [])
        calls = connection.execute.call_args_list
        self.assertIn("UNION ALL", calls[1].args[0])
        self.assertIn("1=0", calls[1].args[0])
        self.assertIn("LIMIT %s OFFSET %s", calls[1].args[0])
        self.assertEqual(calls[1].args[1][-2:], [10, 10])
        self.assertTrue(all("DELETE" not in call.args[0] and "UPDATE" not in call.args[0] for call in calls))
