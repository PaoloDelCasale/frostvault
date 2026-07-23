from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.main import app
from app.sessions import create_session
from tests.test_database import run_alembic


class VaultCreationHttpTestCase(unittest.TestCase):
    """Shared harness: a migrated SQLite database with ordinary (non-admin)
    users, authenticated the way real non-admin users are in this app --
    via an already-established session, not the admin-only local-password
    "/api/login" break-glass endpoint."""

    settings_overrides: dict[str, object] = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()

        self.username = "alice"
        with SQLiteConnection(str(self.database_path)) as connection:
            self.user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES (%s, 'Alice', '', FALSE) RETURNING id
                """,
                (self.username,),
            ).fetchone()["id"]

        overrides: dict[str, object] = {
            "db_backend": "sqlite",
            "sqlite_path": str(self.database_path),
            "cookie_secure": False,
            "vault_sources_root": str(self.sources_root),
            "vault_s3_bucket": "test-bucket",
            "vault_rclone_remote": "test-remote",
        }
        overrides.update(self.settings_overrides)
        self.settings = replace(settings, **overrides)
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.services.vaults.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _authenticate(self, client: TestClient, user_id: int) -> None:
        """Attach a live session cookie for ``user_id``, as if the user had
        already completed authentication (e.g. via OIDC)."""
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(connection, user_id=user_id, auth_method="oidc")
        client.cookies.set(self.settings.session_cookie_name, raw_token)

    def _login(self) -> None:
        self._authenticate(self.client, self.user_id)

    def _csrf(self) -> str:
        return self.client.get("/api/me").json()["csrf_token"]

    def _create(self, name: str, slug: str | None = None, csrf: str | None = None):
        payload: dict[str, object] = {"name": name}
        if slug is not None:
            payload["slug"] = slug
        headers = {"X-CSRF-Token": csrf if csrf is not None else self._csrf()}
        return self.client.post("/api/vaults", json=payload, headers=headers)


class SelfServiceVaultCreationTests(VaultCreationHttpTestCase):
    def test_unauthenticated_vault_create_page_redirects_to_login(self) -> None:
        response = self.client.get("/vaults/new", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_vault_create_page_is_linked_from_no_vault_and_archive_pages(self) -> None:
        self._login()

        no_vault = self.client.get("/")
        self.assertEqual(no_vault.status_code, 200)
        self.assertIn('href="/vaults/new"', no_vault.text)

        create_page = self.client.get("/vaults/new")
        self.assertEqual(create_page.status_code, 200)
        self.assertIn('name="name"', create_page.text)
        self.assertIn('name="slug"', create_page.text)
        for storage_field in ("source_root", "s3_bucket", "s3_prefix", "rclone_remote"):
            self.assertNotIn(storage_field, create_page.text)

        created = self._create("My Archive")
        self.assertEqual(created.status_code, 201, created.text)
        selected = self.client.post(
            "/api/vaults/select",
            json={"vault_id": created.json()["id"]},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(selected.status_code, 200, selected.text)

        archive = self.client.get("/")
        self.assertEqual(archive.status_code, 200)
        self.assertIn('href="/vaults/new"', archive.text)

    def test_create_and_select_flow_opens_the_new_archive(self) -> None:
        self._login()
        response = self._create("My Archive", slug="my-archive")
        self.assertEqual(response.status_code, 201, response.text)

        selected = self.client.post(
            "/api/vaults/select",
            json={"vault_id": response.json()["id"]},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        archive = self.client.get("/")
        self.assertEqual(archive.status_code, 200)
        self.assertIn("My Archive · FrostVault", archive.text)

    def test_an_authenticated_user_can_create_their_own_vault(self) -> None:
        self._login()

        response = self._create("My Archive")

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["name"], "My Archive")
        self.assertEqual(body["slug"], "my-archive")
        self.assertEqual(body["role"], "owner")
        self.assertEqual(len(body["uuid"]), 36)

        # It is immediately usable/selectable, with the creator as sole owner.
        listed = self.client.get("/api/vaults").json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], body["id"])
        self.assertEqual(listed[0]["role"], "owner")

        # The generated directory exists on disk under the configured root.
        created_dirs = [p.name for p in self.sources_root.iterdir() if p.is_dir()]
        self.assertEqual(created_dirs, [body["uuid"]])

    def test_creation_requires_authentication(self) -> None:
        response = self.client.post("/api/vaults", json={"name": "Docs"})

        self.assertEqual(response.status_code, 401)

    def test_creation_requires_a_valid_csrf_token(self) -> None:
        self._login()

        response = self._create("Docs", csrf="not-the-token")

        self.assertEqual(response.status_code, 403, response.text)

    def test_caller_supplied_storage_fields_are_rejected_outright(self) -> None:
        self._login()
        headers = {"X-CSRF-Token": self._csrf()}

        response = self.client.post(
            "/api/vaults",
            json={
                "name": "Docs",
                "source_root": "/sources/whatever-i-want",
                "s3_bucket": "attacker-bucket",
                "s3_prefix": "not/my/prefix",
                "rclone_remote": "attacker-remote",
            },
            headers=headers,
        )

        self.assertEqual(response.status_code, 422, response.text)
        # Nothing was created as a side effect of the rejected request.
        self.assertEqual(self.client.get("/api/vaults").json()["items"], [])

    def test_duplicate_slug_is_a_conflict(self) -> None:
        self._login()
        first = self._create("Docs", slug="docs")
        self.assertEqual(first.status_code, 201, first.text)

        second = self._create("Docs Two", slug="docs")

        self.assertEqual(second.status_code, 409, second.text)

    def test_two_users_creating_vaults_never_collide_on_storage_identity(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            bob_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('bob', 'Bob', '', FALSE) RETURNING id
                """
            ).fetchone()["id"]

        client_a = self.client
        self._authenticate(client_a, self.user_id)
        csrf_a = client_a.get("/api/me").json()["csrf_token"]

        client_b = TestClient(app, client=("127.0.0.1", 50000))
        self._authenticate(client_b, bob_id)
        csrf_b = client_b.get("/api/me").json()["csrf_token"]

        response_a = client_a.post(
            "/api/vaults",
            json={"name": "Alice's Archive"},
            headers={"X-CSRF-Token": csrf_a},
        )
        response_b = client_b.post(
            "/api/vaults",
            json={"name": "Bob's Archive"},
            headers={"X-CSRF-Token": csrf_b},
        )

        self.assertEqual(response_a.status_code, 201, response_a.text)
        self.assertEqual(response_b.status_code, 201, response_b.text)
        vault_a = response_a.json()
        vault_b = response_b.json()
        # Distinct labels, and the server-generated identity never collides
        # either -- each user only ever sees and owns their own vault.
        self.assertNotEqual(vault_a["uuid"], vault_b["uuid"])
        self.assertEqual(len(client_a.get("/api/vaults").json()["items"]), 1)
        self.assertEqual(len(client_b.get("/api/vaults").json()["items"]), 1)

    def test_identical_names_from_different_users_collide_on_the_slug_label(
        self,
    ) -> None:
        # A slug is a label, not a storage identity: two vaults happening to
        # share a human-readable name still collide on that label (the
        # storage namespace is unaffected either way), so this must not be
        # confused with a namespace collision.
        with SQLiteConnection(str(self.database_path)) as connection:
            bob_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('bob', 'Bob', '', FALSE) RETURNING id
                """
            ).fetchone()["id"]

        client_a = self.client
        self._authenticate(client_a, self.user_id)
        csrf_a = client_a.get("/api/me").json()["csrf_token"]

        client_b = TestClient(app, client=("127.0.0.1", 50000))
        self._authenticate(client_b, bob_id)
        csrf_b = client_b.get("/api/me").json()["csrf_token"]

        response_a = client_a.post(
            "/api/vaults",
            json={"name": "Archive"},
            headers={"X-CSRF-Token": csrf_a},
        )
        response_b = client_b.post(
            "/api/vaults",
            json={"name": "Archive"},
            headers={"X-CSRF-Token": csrf_b},
        )

        self.assertEqual(response_a.status_code, 201, response_a.text)
        self.assertEqual(response_b.status_code, 409, response_b.text)

    def test_no_new_user_is_ever_provisioned_by_creating_a_vault(self) -> None:
        self._login()
        with SQLiteConnection(str(self.database_path)) as connection:
            before = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()[
                "total"
            ]

        response = self._create("Docs")

        self.assertEqual(response.status_code, 201, response.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            after = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()[
                "total"
            ]
            owner = connection.execute(
                "SELECT user_id, role FROM vault_members WHERE vault_id=%s",
                (response.json()["id"],),
            ).fetchone()
        self.assertEqual(before, after)
        self.assertEqual(owner["user_id"], self.user_id)
        self.assertEqual(owner["role"], "owner")

    def test_concurrent_creation_requests_never_collide(self) -> None:
        self._login()
        csrf = self._csrf()

        def _create(index: int):
            return self.client.post(
                "/api/vaults",
                json={"name": f"Vault {index}"},
                headers={"X-CSRF-Token": csrf},
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(_create, range(6)))

        for response in responses:
            self.assertEqual(response.status_code, 201, response.text)
        uuids = {response.json()["uuid"] for response in responses}
        self.assertEqual(len(uuids), 6)
        created_dirs = {p.name for p in self.sources_root.iterdir() if p.is_dir()}
        self.assertEqual(created_dirs, uuids)


class AdminVaultCreationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()

        with SQLiteConnection(str(self.database_path)) as connection:
            self.admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', '', TRUE) RETURNING id
                """
            ).fetchone()["id"]
            self.owner_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', '', FALSE) RETURNING id
                """
            ).fetchone()["id"]

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            vault_sources_root=str(self.sources_root),
            vault_s3_bucket="server-bucket",
            vault_rclone_remote="server-remote",
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.services.vaults.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _authenticate(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection, user_id=self.admin_id, auth_method="oidc"
            )
        self.client.cookies.set(self.settings.session_cookie_name, raw_token)

    def _csrf(self) -> str:
        return self.client.get("/api/me").json()["csrf_token"]

    def test_admin_creation_uses_server_storage_identity_and_provisioning(self) -> None:
        self._authenticate()
        response = self.client.post(
            "/api/admin/vaults",
            json={
                "name": "Managed Archive",
                "slug": "managed-archive",
                "owner_user_id": self.owner_id,
                "reason": "provision archive for owner",
            },
            headers={"X-CSRF-Token": self._csrf()},
        )

        self.assertEqual(response.status_code, 201, response.text)
        vault = response.json()
        self.assertEqual(vault["s3_bucket"], "server-bucket")
        self.assertEqual(vault["rclone_remote"], "server-remote")
        self.assertEqual(vault["s3_prefix"], f"vaults/{vault['uuid']}/")
        self.assertEqual(
            Path(vault["source_root"]), self.sources_root / vault["uuid"]
        )
        self.assertTrue(Path(vault["source_root"]).is_dir())

    def test_admin_provisioning_failure_rolls_back_vault_and_membership(self) -> None:
        self._authenticate()
        with patch("app.services.vaults.os.makedirs", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.client.post(
                    "/api/admin/vaults",
                    json={
                        "name": "Managed Archive",
                        "slug": "managed-archive",
                        "owner_user_id": self.owner_id,
                        "reason": "provision archive for owner",
                    },
                    headers={"X-CSRF-Token": self._csrf()},
                )

        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM vaults"
                ).fetchone()["total"],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM vault_members"
                ).fetchone()["total"],
                0,
            )
        self.assertEqual(list(self.sources_root.iterdir()), [])

    def test_admin_creation_rejects_caller_storage_identity_fields(self) -> None:
        self._authenticate()
        response = self.client.post(
            "/api/admin/vaults",
            json={
                "name": "Managed Archive",
                "slug": "managed-archive",
                "owner_user_id": self.owner_id,
                "reason": "provision archive for owner",
                "source_root": "/sources/attacker",
                "s3_bucket": "attacker-bucket",
                "s3_prefix": "attacker-prefix",
                "rclone_remote": "attacker-remote",
            },
            headers={"X-CSRF-Token": self._csrf()},
        )

        self.assertEqual(response.status_code, 422, response.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM vaults"
                ).fetchone()["total"],
                0,
            )

    def test_admin_creation_requires_reason_and_is_audited(self) -> None:
        self._authenticate()
        missing_reason = self.client.post(
            "/api/admin/vaults",
            json={
                "name": "Managed Archive",
                "slug": "managed-archive",
                "owner_user_id": self.owner_id,
            },
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(missing_reason.status_code, 422, missing_reason.text)

        with self.assertLogs("app.audit", level="WARNING") as logs:
            response = self.client.post(
                "/api/admin/vaults",
                json={
                    "name": "Managed Archive",
                    "slug": "managed-archive",
                    "owner_user_id": self.owner_id,
                    "reason": "provision archive for owner",
                },
                headers={"X-CSRF-Token": self._csrf()},
            )
        self.assertEqual(response.status_code, 201, response.text)
        record = logs.records[-1].getMessage()
        self.assertIn('"event": "vault_created"', record)
        self.assertIn('"admin_override": true', record)
        self.assertIn("provision archive for owner", record)


class UnconfiguredSelfServiceVaultCreationTests(VaultCreationHttpTestCase):
    settings_overrides = {"vault_s3_bucket": "", "vault_rclone_remote": ""}

    def test_creation_fails_clearly_when_the_server_has_no_default_storage(self) -> None:
        self._login()

        response = self._create("Docs")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.client.get("/api/vaults").json()["items"], [])


if __name__ == "__main__":
    unittest.main()
