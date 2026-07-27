from __future__ import annotations

import tempfile
import unittest
import base64
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.oidc_configuration import save_oidc_draft
from app.sessions import create_session, csrf_token_for
from tests.oidc_fake import FakeOidcProvider
from tests.test_database import run_alembic


class OidcConfigurationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', 'hash', TRUE) RETURNING id
                """
            ).fetchone()["id"]

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            oidc_enabled=True,
            oidc_issuer="https://identity.example",
            oidc_client_id="frostvault",
            oidc_client_secret="must-never-be-returned",
            oidc_scopes="openid profile",
            oidc_login_ttl_seconds=420,
            break_glass_allowed_cidrs="127.0.0.1/32",
            oidc_settings_encryption_key=base64.urlsafe_b64encode(
                b"k" * 32
            ).decode("ascii"),
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.oidc.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))

        with SQLiteConnection(str(self.database_path)) as connection:
            token = create_session(
                connection,
                user_id=self.admin_id,
                auth_method="oidc",
            )
            csrf_token = csrf_token_for(connection, token)
        self.client.cookies.set(self.test_settings.session_cookie_name, token)
        self.client.cookies.set(
            self.test_settings.csrf_cookie_name,
            csrf_token,
        )

    def _csrf(self) -> dict[str, str]:
        return {
            "X-CSRF-Token": (
                self.client.cookies.get(self.test_settings.csrf_cookie_name)
                or ""
            )
        }

    def _save_and_validate(
        self,
        provider: FakeOidcProvider,
        *,
        client_secret: str = "managed-secret",
    ) -> None:
        saved = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": provider.issuer,
                "client_id": provider.client_id,
                "client_secret": client_secret,
                "scopes": ["openid", "profile"],
                "login_transaction_ttl_seconds": 300,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        with patch.object(main, "_oidc_client", lambda: provider.client()), patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
        ):
            validated = self.client.post(
                "/api/admin/oidc-configuration/draft/validate",
                headers=self._csrf(),
            )
        self.assertEqual(validated.status_code, 200, validated.text)

    def _activate(self) -> None:
        activated = self.client.post(
            "/api/admin/oidc-configuration/activate",
            headers=self._csrf(),
        )
        self.assertEqual(activated.status_code, 200, activated.text)

    def test_admin_inspects_effective_configuration_without_secret_value(self) -> None:
        response = self.client.get("/api/admin/oidc-configuration")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "active": {
                    "enabled": True,
                    "issuer": "https://identity.example",
                    "client_id": "frostvault",
                    "client_secret_configured": True,
                    "scopes": ["openid", "profile"],
                    "login_transaction_ttl_seconds": 420,
                    "callback_url": (
                        "http://testserver/auth/oidc/callback"
                    ),
                    "source": "environment",
                },
                "draft": None,
                "configuration_status": "active",
                "last_validation": None,
            },
        )
        self.assertNotIn("must-never-be-returned", response.text)
        self.assertNotIn("client_secret\"", response.text)

    def test_non_admin_cannot_inspect_oidc_configuration(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            member_id = connection.execute(
                """
                INSERT INTO users(
                    username, display_name, password_hash, is_admin
                ) VALUES ('member', 'Member', 'hash', FALSE)
                RETURNING id
                """
            ).fetchone()["id"]
            token = create_session(
                connection,
                user_id=member_id,
                auth_method="oidc",
            )
        self.client.cookies.set(self.test_settings.session_cookie_name, token)

        response = self.client.get("/api/admin/oidc-configuration")

        self.assertEqual(response.status_code, 403, response.text)

    def test_admin_saves_encrypted_draft_and_reads_it_redacted(self) -> None:
        secret = "new-draft-secret-that-must-stay-write-only"
        response = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": "https://new-identity.example",
                "client_id": "new-frostvault",
                "client_secret": secret,
                "scopes": ["openid", "profile", "groups"],
                "login_transaction_ttl_seconds": 300,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        inspected = self.client.get("/api/admin/oidc-configuration")
        self.assertEqual(inspected.status_code, 200, inspected.text)
        self.assertEqual(
            inspected.json()["draft"],
            {
                "issuer": "https://new-identity.example",
                "client_id": "new-frostvault",
                "client_secret_configured": True,
                "scopes": ["openid", "profile", "groups"],
                "login_transaction_ttl_seconds": 300,
                "version": 1,
                "validation_status": "not_validated",
            },
        )
        self.assertEqual(
            inspected.json()["configuration_status"],
            "draft",
        )
        self.assertNotIn(secret, response.text)
        self.assertNotIn(secret, inspected.text)
        persisted_bytes = self.database_path.read_bytes()
        wal_path = Path(f"{self.database_path}-wal")
        if wal_path.exists():
            persisted_bytes += wal_path.read_bytes()
        self.assertNotIn(secret.encode("utf-8"), persisted_bytes)

    def test_draft_requires_the_openid_scope(self) -> None:
        response = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": "https://identity.example",
                "client_id": "frostvault",
                "client_secret": "secret",
                "scopes": ["profile"],
                "login_transaction_ttl_seconds": 300,
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        inspected = self.client.get("/api/admin/oidc-configuration")
        self.assertIsNone(inspected.json()["draft"])

    def test_saving_draft_emits_secret_free_admin_audit_event(self) -> None:
        secret = "audit-must-never-contain-this-secret"
        saved = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": "https://identity.example",
                "client_id": "frostvault",
                "client_secret": secret,
                "scopes": ["openid"],
                "login_transaction_ttl_seconds": 300,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)

        response = self.client.get("/api/admin/audit-events")
        self.assertEqual(response.status_code, 200, response.text)
        events = response.json()["events"]
        self.assertEqual(events[0]["event"], "oidc_configuration_draft_saved")
        self.assertEqual(events[0]["actor_user_id"], self.admin_id)
        self.assertEqual(events[0]["outcome"], "success")
        self.assertNotIn(secret, response.text)

    def test_saving_draft_requires_recent_reauthentication(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at='2000-01-01T00:00:00+00:00'"
            )

        response = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": "https://identity.example",
                "client_id": "frostvault",
                "client_secret": "secret",
                "scopes": ["openid"],
                "login_transaction_ttl_seconds": 300,
            },
        )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"error": "reauth_required"})
        inspected = self.client.get("/api/admin/oidc-configuration")
        self.assertIsNone(inspected.json()["draft"])

    def test_admin_validates_draft_discovery_issuer_and_jwks(self) -> None:
        provider = FakeOidcProvider()
        saved = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": provider.issuer,
                "client_id": provider.client_id,
                "client_secret": "validation-secret",
                "scopes": ["openid"],
                "login_transaction_ttl_seconds": 300,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        oidc_client = patch.object(
            main,
            "_oidc_client",
            lambda: provider.client(),
        )
        resolver = patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
            create=True,
        )
        oidc_client.start()
        resolver.start()
        self.addCleanup(oidc_client.stop)
        self.addCleanup(resolver.stop)

        response = self.client.post(
            "/api/admin/oidc-configuration/draft/validate",
            headers=self._csrf(),
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["configuration_status"], "validated")
        self.assertEqual(payload["draft"]["validation_status"], "valid")
        self.assertEqual(payload["last_validation"]["status"], "valid")
        self.assertTrue(payload["last_validation"]["validated_at"])
        self.assertEqual(payload["active"]["source"], "environment")

    def test_validation_rejects_incomplete_discovery_metadata(self) -> None:
        provider = FakeOidcProvider()
        metadata = provider.discovery()
        metadata.pop("token_endpoint")
        saved = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": provider.issuer,
                "client_id": provider.client_id,
                "client_secret": "validation-secret",
                "scopes": ["openid"],
                "login_transaction_ttl_seconds": 300,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)

        with patch.object(
            provider,
            "discovery",
            return_value=metadata,
        ), patch.object(
            main,
            "_oidc_client",
            lambda: provider.client(),
        ), patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
        ):
            response = self.client.post(
                "/api/admin/oidc-configuration/draft/validate",
                headers=self._csrf(),
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["last_validation"]["error"],
            "token_endpoint_missing",
        )

    def test_validation_blocks_private_network_issuer_without_changing_active(
        self,
    ) -> None:
        saved = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": "https://127.0.0.1",
                "client_id": "private-client",
                "client_secret": "private-secret",
                "scopes": ["openid"],
                "login_transaction_ttl_seconds": 300,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        with patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["127.0.0.1"],
        ):
            response = self.client.post(
                "/api/admin/oidc-configuration/draft/validate",
                headers=self._csrf(),
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["last_validation"]["error"],
            "ssrf_blocked",
        )
        self.assertEqual(
            response.json()["active"]["issuer"],
            "https://identity.example",
        )
        self.assertEqual(response.json()["active"]["source"], "environment")

    def test_validation_cannot_mark_a_newer_draft_as_valid(self) -> None:
        provider = FakeOidcProvider()
        saved = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": provider.issuer,
                "client_id": provider.client_id,
                "client_secret": "first-secret",
                "scopes": ["openid"],
                "login_transaction_ttl_seconds": 300,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        draft_replaced = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal draft_replaced
            if str(request.url) == provider.discovery_url:
                if not draft_replaced:
                    draft_replaced = True
                    with SQLiteConnection(
                        str(self.database_path)
                    ) as connection:
                        save_oidc_draft(
                            connection,
                            issuer="https://newer-draft.example",
                            client_id="newer-client",
                            client_secret="newer-secret",
                            scopes=["openid"],
                            login_transaction_ttl_seconds=300,
                            updated_by=self.admin_id,
                            settings_obj=self.test_settings,
                        )
                return httpx.Response(200, json=provider.discovery())
            if str(request.url) == provider.discovery()["jwks_uri"]:
                return httpx.Response(200, json=provider.jwks())
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        with patch.object(main, "_oidc_client", lambda: client), patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
        ):
            response = self.client.post(
                "/api/admin/oidc-configuration/draft/validate",
                headers=self._csrf(),
            )

        self.assertEqual(response.status_code, 409, response.text)
        inspected = self.client.get("/api/admin/oidc-configuration")
        self.assertEqual(
            inspected.json()["draft"]["issuer"],
            "https://newer-draft.example",
        )
        self.assertEqual(
            inspected.json()["draft"]["validation_status"],
            "not_validated",
        )

    def test_unvalidated_draft_cannot_replace_active_configuration(self) -> None:
        saved = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": "https://unvalidated.example",
                "client_id": "unvalidated-client",
                "client_secret": "unvalidated-secret",
                "scopes": ["openid"],
                "login_transaction_ttl_seconds": 300,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)

        response = self.client.post(
            "/api/admin/oidc-configuration/activate",
            headers=self._csrf(),
        )

        self.assertEqual(response.status_code, 409, response.text)
        inspected = self.client.get("/api/admin/oidc-configuration")
        self.assertEqual(inspected.status_code, 200, inspected.text)
        self.assertEqual(inspected.json()["active"]["source"], "environment")
        self.assertEqual(
            inspected.json()["active"]["issuer"],
            "https://identity.example",
        )
        self.assertEqual(
            inspected.json()["draft"]["validation_status"],
            "not_validated",
        )

    def test_validated_draft_activates_and_invalidates_pending_logins_only(
        self,
    ) -> None:
        provider = FakeOidcProvider()
        saved = self.client.put(
            "/api/admin/oidc-configuration/draft",
            headers=self._csrf(),
            json={
                "issuer": provider.issuer,
                "client_id": provider.client_id,
                "client_secret": "active-secret",
                "scopes": ["openid", "profile"],
                "login_transaction_ttl_seconds": 300,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        with patch.object(main, "_oidc_client", lambda: provider.client()), patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
        ):
            validated = self.client.post(
                "/api/admin/oidc-configuration/draft/validate",
                headers=self._csrf(),
            )
        self.assertEqual(validated.status_code, 200, validated.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO oidc_login(
                    id, state, nonce, code_verifier, created_at, expires_at
                ) VALUES (
                    'pending-login', 'pending-state', 'nonce', 'verifier',
                    '2026-07-27T00:00:00+00:00',
                    '2026-07-28T00:00:00+00:00'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO user_identities(user_id, issuer, subject, created_at)
                VALUES (%s, 'https://old-issuer.example', 'subject',
                        '2026-07-27T00:00:00+00:00')
                """,
                (self.admin_id,),
            )

        response = self.client.post(
            "/api/admin/oidc-configuration/activate",
            headers=self._csrf(),
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["active"],
            {
                "enabled": True,
                "issuer": provider.issuer,
                "client_id": provider.client_id,
                "client_secret_configured": True,
                "scopes": ["openid", "profile"],
                "login_transaction_ttl_seconds": 300,
                "callback_url": "http://testserver/auth/oidc/callback",
                "source": "database",
            },
        )
        self.assertIsNone(response.json()["draft"])
        self.assertEqual(response.json()["configuration_status"], "active")
        self.assertEqual(
            response.json()["last_validation"]["status"],
            "valid",
        )
        with SQLiteConnection(str(self.database_path)) as connection:
            pending = connection.execute(
                "SELECT COUNT(*) AS total FROM oidc_login"
            ).fetchone()["total"]
            identities = connection.execute(
                "SELECT COUNT(*) AS total FROM user_identities"
            ).fetchone()["total"]
        self.assertEqual(pending, 0)
        self.assertEqual(identities, 1)
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_activation_rolls_back_when_its_audit_event_cannot_commit(
        self,
    ) -> None:
        provider = FakeOidcProvider()
        self._save_and_validate(provider)

        with patch.object(
            main.audit_event_store,
            "record_audit_event",
            side_effect=RuntimeError("audit store unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "audit store unavailable",
            ):
                self.client.post(
                    "/api/admin/oidc-configuration/activate",
                    headers=self._csrf(),
                )

        inspected = self.client.get("/api/admin/oidc-configuration")
        self.assertEqual(inspected.status_code, 200, inspected.text)
        self.assertEqual(inspected.json()["active"]["source"], "environment")
        self.assertEqual(
            inspected.json()["draft"]["validation_status"],
            "valid",
        )

    def test_activation_requires_available_break_glass_recovery(self) -> None:
        provider = FakeOidcProvider()
        self._save_and_validate(provider)
        unsafe_settings = replace(
            self.test_settings,
            break_glass_allowed_cidrs="",
        )

        with patch.object(main, "settings", unsafe_settings):
            response = self.client.post(
                "/api/admin/oidc-configuration/activate",
                headers=self._csrf(),
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(
            "break_glass_recovery_unavailable",
            response.text,
        )
        inspected = self.client.get("/api/admin/oidc-configuration")
        self.assertEqual(inspected.json()["active"]["source"], "environment")
        self.assertEqual(
            inspected.json()["draft"]["validation_status"],
            "valid",
        )

    def test_disabling_oidc_preserves_sessions_and_identities(self) -> None:
        provider = FakeOidcProvider()
        self._save_and_validate(provider)
        self._activate()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO oidc_login(
                    id, state, nonce, code_verifier, created_at, expires_at
                ) VALUES (
                    'disable-pending', 'disable-state', 'nonce', 'verifier',
                    '2026-07-27T00:00:00+00:00',
                    '2026-07-28T00:00:00+00:00'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO user_identities(user_id, issuer, subject, created_at)
                VALUES (%s, %s, 'stable-subject',
                        '2026-07-27T00:00:00+00:00')
                """,
                (self.admin_id, provider.issuer),
            )

        response = self.client.post(
            "/api/admin/oidc-configuration/disable",
            headers=self._csrf(),
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["active"]["enabled"])
        self.assertEqual(response.json()["active"]["source"], "database")
        self.assertEqual(response.json()["configuration_status"], "disabled")
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM oidc_login"
                ).fetchone()["total"],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM user_identities"
                ).fetchone()["total"],
                1,
            )
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_disabling_environment_oidc_does_not_require_encryption_key(
        self,
    ) -> None:
        settings_without_key = replace(
            self.test_settings,
            oidc_settings_encryption_key="",
        )

        with patch.object(main, "settings", settings_without_key):
            response = self.client.post(
                "/api/admin/oidc-configuration/disable",
                headers=self._csrf(),
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["active"]["enabled"])
        self.assertFalse(
            response.json()["active"]["client_secret_configured"]
        )

    def test_rotating_secret_requires_a_managed_active_configuration(
        self,
    ) -> None:
        response = self.client.post(
            "/api/admin/oidc-configuration/rotate-secret",
            headers=self._csrf(),
            json={"client_secret": "must-not-promote-environment"},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("oidc_managed_configuration_not_active", response.text)
        inspected = self.client.get("/api/admin/oidc-configuration")
        self.assertEqual(inspected.json()["active"]["source"], "environment")

    def test_rotating_secret_is_write_only_and_invalidates_pending_logins(
        self,
    ) -> None:
        provider = FakeOidcProvider()
        self._save_and_validate(provider, client_secret="old-secret")
        self._activate()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO oidc_login(
                    id, state, nonce, code_verifier, created_at, expires_at
                ) VALUES (
                    'rotate-pending', 'rotate-state', 'nonce', 'verifier',
                    '2026-07-27T00:00:00+00:00',
                    '2026-07-28T00:00:00+00:00'
                )
                """
            )
        new_secret = "new-secret-must-remain-write-only"

        response = self.client.post(
            "/api/admin/oidc-configuration/rotate-secret",
            headers=self._csrf(),
            json={"client_secret": new_secret},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["active"]["enabled"])
        self.assertTrue(
            response.json()["active"]["client_secret_configured"]
        )
        self.assertNotIn(new_secret, response.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM oidc_login"
                ).fetchone()["total"],
                0,
            )
        persisted_bytes = self.database_path.read_bytes()
        wal_path = Path(f"{self.database_path}-wal")
        if wal_path.exists():
            persisted_bytes += wal_path.read_bytes()
        self.assertNotIn(new_secret.encode("utf-8"), persisted_bytes)

    def test_completed_login_uses_the_rotated_secret(self) -> None:
        provider = FakeOidcProvider()
        self._save_and_validate(provider, client_secret="old-secret")
        self._activate()
        rotated = self.client.post(
            "/api/admin/oidc-configuration/rotate-secret",
            headers=self._csrf(),
            json={"client_secret": "rotated-secret"},
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO user_identities(user_id, issuer, subject, created_at)
                VALUES (%s, %s, 'rotated-subject',
                        '2026-07-27T00:00:00+00:00')
                """,
                (self.admin_id, provider.issuer),
            )
        with patch.object(main, "_oidc_client", lambda: provider.client()), patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
        ):
            started = self.client.get(
                "/auth/oidc/login",
                follow_redirects=False,
            )
        self.assertEqual(started.status_code, 303, started.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            pending = connection.execute(
                "SELECT state, nonce FROM oidc_login"
            ).fetchone()
        id_token = provider.make_id_token(
            nonce=pending["nonce"],
            subject="rotated-subject",
        )
        with patch.object(
            main,
            "_oidc_client",
            lambda: provider.client(id_token=id_token),
        ), patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
        ):
            completed = self.client.get(
                "/auth/oidc/callback",
                params={
                    "state": pending["state"],
                    "code": "auth-code",
                },
                follow_redirects=False,
            )

        self.assertEqual(completed.status_code, 303, completed.text)
        self.assertEqual(
            provider.token_requests[-1]["client_secret"],
            "rotated-secret",
        )

    def test_login_uses_activated_database_configuration(self) -> None:
        provider = FakeOidcProvider(
            issuer="https://managed-oidc.example",
            client_id="managed-client",
        )
        self._save_and_validate(provider, client_secret="managed-secret")
        self._activate()

        with patch.object(main, "_oidc_client", lambda: provider.client()), patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
        ):
            response = self.client.get(
                "/auth/oidc/login",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303, response.text)
        location = urlparse(response.headers["location"])
        self.assertEqual(
            f"{location.scheme}://{location.netloc}{location.path}",
            f"{provider.issuer}/authorize",
        )
        query = parse_qs(location.query)
        self.assertEqual(query["client_id"], ["managed-client"])
        self.assertEqual(query["scope"], ["openid profile"])

    def test_effective_settings_inventory_uses_activated_oidc_values(
        self,
    ) -> None:
        provider = FakeOidcProvider(
            issuer="https://inventory-oidc.example",
            client_id="inventory-client",
        )
        self._save_and_validate(provider)
        self._activate()

        response = self.client.get("/api/admin/settings")

        self.assertEqual(response.status_code, 200, response.text)
        oidc_settings = {
            item["key"]: item for item in response.json()["groups"]["oidc"]
        }
        self.assertEqual(
            oidc_settings["oidc_issuer"]["effective_value"],
            provider.issuer,
        )
        self.assertEqual(
            oidc_settings["oidc_issuer"]["source"],
            "database_override",
        )
        self.assertTrue(
            oidc_settings["oidc_client_secret"]["configured"]
        )

    def test_login_rechecks_ssrf_constraints_after_dns_rebinding(self) -> None:
        provider = FakeOidcProvider(
            issuer="https://rebound-oidc.example",
            client_id="rebound-client",
        )
        self._save_and_validate(provider)
        self._activate()

        with patch.object(main, "_oidc_client", lambda: provider.client()), patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["127.0.0.1"],
        ):
            response = self.client.get(
                "/auth/oidc/login",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("ssrf_blocked", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
