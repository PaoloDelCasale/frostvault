"""Owner-domain contracts that do not need a PostgreSQL service."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from app import database, oidc, sessions
from app.audit import audit_log
from app.config import settings
from app.owner import OWNER_ID, job_actor
from app.services import audit_events, notifications


class OwnerSingletonTests(TestCase):
    def test_owner_identity_is_constant_without_database_lookup(self):
        self.assertEqual(OWNER_ID, 1)
        self.assertEqual(job_actor({"origin": "automatic"}), "system")
        for origin in ("manual", "stop-protection", "decommission"):
            self.assertEqual(job_actor({"origin": origin}), "owner")

    def claims(self, subject="owner", issuer="https://id.example"):
        return oidc.OidcClaims(issuer=issuer, subject=subject, return_to=None, raw={})

    def test_first_oidc_binding_locks_the_singleton_and_writes_pair(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = {"issuer": None, "subject": None}
        with patch.object(oidc, "settings", replace(settings, oidc_issuer="https://id.example")):
            self.assertEqual(oidc.bind_owner_identity(connection, self.claims()), 1)
        self.assertIn("FROM owner WHERE id=1 FOR UPDATE", connection.execute.call_args_list[0].args[0])
        self.assertEqual(connection.execute.call_args_list[1].args,
                         ("UPDATE owner SET issuer=%s, subject=%s WHERE id=1", ("https://id.example", "owner")))

    def test_bound_pair_cannot_be_replaced_by_subject_or_issuer_change(self):
        for incoming in (self.claims("foreign"), self.claims(issuer="https://new.example")):
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = {
                "issuer": "https://id.example", "subject": "owner"}
            with patch.object(oidc, "settings", replace(settings, oidc_issuer=incoming.issuer)):
                with self.assertRaises(oidc.IdentityRejected):
                    oidc.bind_owner_identity(connection, incoming)
            self.assertEqual(connection.execute.call_count, 1)

    def test_missing_owner_and_unconfigured_issuer_fail_closed(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = None
        with patch.object(oidc, "settings", replace(settings, oidc_issuer="https://id.example")):
            with self.assertRaises(oidc.IdentityRejected):
                oidc.bind_owner_identity(connection, self.claims())
            connection.reset_mock()
            with self.assertRaises(oidc.IdentityRejected):
                oidc.bind_owner_identity(connection, self.claims(issuer="https://foreign.example"))
            connection.execute.assert_not_called()

    def test_password_change_bumps_version_but_name_change_does_not(self):
        for password_current, expected_bump in ((True, 0), (False, 1)):
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = {
                "id": 1, "display_name": "Old name", "password_hash": "stored-hash"}
            configured = replace(settings, bootstrap_admin_username="new-name", bootstrap_admin_password="new-password-value",
                                 bootstrap_admin_display_name="Owner")
            with patch.object(database, "settings", configured), patch.object(database, "verify_password", return_value=password_current), patch.object(database, "hash_password", return_value="new-hash"):
                database._bootstrap_owner(connection)
            args = connection.execute.call_args.args
            self.assertIn("session_version=session_version + %s", args[0])
            self.assertEqual(args[1], ("Owner", "stored-hash" if password_current else "new-hash", expected_bump, 1))

    def test_bootstrap_stores_no_username_or_additional_principal(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = None
        configured = replace(settings, bootstrap_admin_username="emergency", bootstrap_admin_password="new-password-value",
                             bootstrap_admin_display_name="Owner")
        with patch.object(database, "settings", configured), patch.object(database, "hash_password", return_value="hash"):
            database._bootstrap_owner(connection)
        query, params = connection.execute.call_args.args
        self.assertIn("INSERT INTO owner(id, display_name, password_hash)", query)
        self.assertEqual(params, ("Owner", "hash"))

    def test_no_session_or_notification_for_second_principal(self):
        connection = Mock()
        with self.assertRaises(ValueError):
            sessions.create_session(connection, user_id=2, auth_method="oidc")
        for operation, kwargs in (
            (notifications.enqueue_notification, {"event": "test", "title": "test"}),
            (notifications.list_in_app_notifications, {}),
            (notifications.count_unread_notifications, {}),
            (notifications.count_open_problems, {}),
            (notifications.mark_notification_read, {"notification_id": 1}),
            (notifications.mark_all_notifications_read, {}),
        ):
            with self.assertRaises(ValueError):
                operation(connection, user_id=2, **kwargs)
        connection.execute.assert_not_called()

    def test_audit_adapter_distinguishes_owner_emergency_and_system(self):
        for event, fields, actor in (
            ("local_cleanup.cancelled", {"actor_user_id": 1}, "owner"),
            ("local_cleanup.finished", {}, "system"),
            ("break_glass_failed", {}, "emergency"),
            ("break_glass_denied", {}, "emergency"),
            ("reauth_succeeded", {"actor_user_id": 1}, "emergency"),
            ("auth_backoff_blocked", {"flow": "break_glass"}, "emergency"),
            ("auth_backoff_blocked", {"flow": "reauth"}, "emergency"),
        ):
            with patch("app.audit.record_audit_event") as record:
                audit_log(event, connection=Mock(), **fields)
            self.assertEqual(record.call_args.kwargs["actor"], actor)
            self.assertNotIn("actor_user_id", record.call_args.kwargs)

    def test_invalid_audit_actor_rejected_before_write(self):
        connection = Mock()
        with self.assertRaises(ValueError):
            audit_events.record_audit_event(connection, event="test", actor="other")
        connection.execute.assert_not_called()

    def test_emergency_login_uses_current_env_name_and_never_authenticates_dummy_path(self):
        from fastapi import HTTPException, Response
        from starlette.requests import Request
        from app import main

        for configured_name, submitted_name, accepted in (
            ("Rescue", " rescue ", True),
            ("Rescue", "old-restored-name", False),
            ("", "", False),
        ):
            request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
            manager = MagicMock()
            connection = manager.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = {"id": 1, "password_hash": "stored-hash"}
            configured = replace(settings, bootstrap_admin_username=configured_name)
            with (
                patch.object(main, "settings", configured),
                patch.object(main, "db", return_value=manager),
                patch.object(main, "backoff_guard"),
                patch.object(main, "record_failure") as failed,
                patch.object(main, "record_success"),
                patch.object(main, "audit_log"),
                patch.object(main, "verify_password", return_value=True) as verify,
                patch.object(main, "create_session", return_value="test-token") as create,
                patch.object(main, "csrf_token_for", return_value=None),
                patch.object(main, "_set_session_cookie"),
                patch.object(main, "_api_message", return_value={"message": "ok"}),
            ):
                action = main.LoginRequest(username=submitted_name, password="test-only-password")
                if accepted:
                    main.login(action, request, Response())
                    create.assert_called_once()
                    failed.assert_not_called()
                    self.assertEqual(verify.call_args.args[0], "stored-hash")
                else:
                    with self.assertRaises(HTTPException) as error:
                        main.login(action, request, Response())
                    self.assertEqual(error.exception.status_code, 401)
                    create.assert_not_called()
                    self.assertEqual(verify.call_args.args[0], main.DUMMY_PASSWORD_HASH)
                    self.assertEqual(failed.call_count, 2)
