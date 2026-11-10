from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings

from app.main import app
from app.services import source_layout
from app.sessions import create_session
from tests.spa_fixture import write_spa_dist
from tests.test_database import run_alembic


class VaultCreationHttpTestCase(unittest.TestCase):
    """Shared harness: a migrated PostgreSQL database with the single owner
    (#412), authenticated through an already-established session as after
    an OIDC login."""

    settings_overrides: dict[str, object] = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        self.managed_root = source_layout.ensure_managed_directory()

        self.username = "alice"
        with PostgresConnection(str(self.database_path)) as connection:
            self.user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash)
                VALUES (%s, 'Alice', '') RETURNING id
                """,
                (self.username,),
            ).fetchone()["id"]

        overrides: dict[str, object] = {
            "database_dsn": database_dsn(str(self.database_path)),
            "cookie_secure": False,
            "vault_s3_bucket": "test-bucket",

            "frontend_dist_dir": str(write_spa_dist(Path(self._tmp.name))),
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
        with PostgresConnection(str(self.database_path)) as connection:
            raw_token = create_session(connection, user_id=user_id, auth_method="oidc")
        client.cookies.set(self.settings.session_cookie_name, raw_token)

    def _login(self) -> None:
        self._authenticate(self.client, self.user_id)

    def _csrf(self) -> str:
        return self.client.get("/api/me").json()["csrf_token"]

    def _create(self, name: str, slug: str | None = None, csrf: str | None = None):
        payload: dict[str, object] = {
            "name": name,
        }
        if slug is not None:
            payload["slug"] = slug
        headers = {"X-CSRF-Token": csrf if csrf is not None else self._csrf()}
        return self.client.post("/api/vaults", json=payload, headers=headers)


class VaultCreationTests(VaultCreationHttpTestCase):
    def test_unauthenticated_vault_create_page_serves_spa(self) -> None:
        """HTML is the SPA shell; auth is enforced by the JSON create API."""
        response = self.client.get("/vaults/new", follow_redirects=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('id="root"', response.text)

        denied = self.client.post(
            "/api/vaults",
            json={"name": "My Archive"},
            headers={"X-CSRF-Token": "missing"},
        )
        self.assertEqual(denied.status_code, 401, denied.text)

    def test_vault_create_api_works_without_html_form_markup(self) -> None:
        self._login()

        no_vault = self.client.get("/")
        self.assertEqual(no_vault.status_code, 200)
        self.assertIn('id="root"', no_vault.text)

        create_page = self.client.get("/vaults/new")
        self.assertEqual(create_page.status_code, 200)
        self.assertIn('id="root"', create_page.text)

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
        self.assertIn('id="root"', archive.text)

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
        self.assertIn('id="root"', archive.text)
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["vault"]["name"], "My Archive")
        self.assertEqual(me.json()["vault"]["slug"], "my-archive")

    def test_the_owner_can_create_a_vault(self) -> None:
        self._login()

        response = self._create("My Archive")

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["name"], "My Archive")
        self.assertEqual(body["slug"], "my-archive")
        self.assertNotIn("role", body)
        self.assertEqual(len(body["uuid"]), 36)

        # It is immediately usable/selectable.
        listed = self.client.get("/api/vaults").json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], body["id"])

        # The generated directory exists on disk under managed/<uuid>.
        created_dirs = [p.name for p in self.managed_root.iterdir() if p.is_dir()]
        self.assertEqual(created_dirs, [body["uuid"]])

    def test_plaintext_creation_has_no_mode_or_recovery_material_and_needs_no_master_key(self) -> None:
        self._login()
        with patch("app.services.vaults.settings", replace(self.settings, archive_master_key="")):
            response = self._create("Readable")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(set(response.json()), {"id", "uuid", "slug", "name", "creation_mode"})
        with PostgresConnection(str(self.database_path)) as connection:
            row = connection.execute("SELECT * FROM vaults WHERE id=%s", (response.json()["id"],)).fetchone()
        self.assertEqual(row["s3_prefix"], f"vaults/{row['uuid']}/")
        self.assertFalse(any("crypt" in key or "custody" in key for key in row))

    def test_retired_encryption_options_and_recovery_routes_are_not_available(self) -> None:
        self._login()
        headers = {"X-CSRF-Token": self._csrf()}
        for mode in ("plain", "crypt"):
            response = self.client.post("/api/vaults", json={"name": "Retired", "encryption_mode": mode}, headers=headers)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/vaults").json()["items"], [])
        for route in ("confirm", "export"):
            response = self.client.post(f"/api/vault/recovery/{route}", json={}, headers=headers)
            self.assertEqual(response.status_code, 404, response.text)

    def test_creation_requires_authentication(self) -> None:
        response = self.client.post("/api/vaults", json={"name": "Docs"})

        self.assertEqual(response.status_code, 401)

    def test_creation_requires_a_valid_csrf_token(self) -> None:
        self._login()

        response = self._create("Docs", csrf="not-the-token")

        self.assertEqual(response.status_code, 403, response.text)

    def test_creation_needs_no_history_choice_and_rejects_retired_option(self) -> None:
        self._login()
        created = self.client.post(
            "/api/vaults", json={"name": "Docs"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn("cloud_history_policy", created.json())
        rejected = self.client.post(
            "/api/vaults", json={"name": "Retired", "cloud_history_policy": "current_snapshot"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)

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
        self.assertEqual(second.json()["message_key"], "api.vault_name_taken")
        self.assertNotIn("slug", second.json()["message"].lower())

    def test_creation_errors_are_localized_for_the_owner_locale(self) -> None:
        self._login()
        self.assertEqual(self._create("Docs", slug="docs").status_code, 201)
        self.client.cookies.set("frostvault_locale", "it")

        response = self._create("Docs Two", slug="docs")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["message"], "Esiste già un Vault con questo nome. Scegline un altro.")
        self.assertEqual(response.json()["detail"], response.json()["message"])

    def test_no_new_user_is_ever_provisioned_by_creating_a_vault(self) -> None:
        self._login()
        with PostgresConnection(str(self.database_path)) as connection:
            before = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()[
                "total"
            ]

        response = self._create("Docs")

        self.assertEqual(response.status_code, 201, response.text)
        with PostgresConnection(str(self.database_path)) as connection:
            after = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()[
                "total"
            ]
        self.assertEqual(before, after)

    def test_concurrent_creation_requests_never_collide(self) -> None:
        self._login()
        csrf = self._csrf()

        def _create(index: int):
            return self.client.post(
                "/api/vaults",
                json={
                    "name": f"Vault {index}",
                        },
                headers={"X-CSRF-Token": csrf},
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(_create, range(6)))

        for response in responses:
            self.assertEqual(response.status_code, 201, response.text)
        uuids = {response.json()["uuid"] for response in responses}
        self.assertEqual(len(uuids), 6)
        created_dirs = {p.name for p in self.managed_root.iterdir() if p.is_dir()}
        self.assertEqual(created_dirs, uuids)


class UnconfiguredVaultCreationTests(VaultCreationHttpTestCase):
    settings_overrides = {"vault_s3_bucket": ""}

    def test_creation_fails_clearly_when_the_server_has_no_default_storage(self) -> None:
        self._login()

        response = self._create("Docs")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["message_key"], "api.vault_provisioning_unavailable")
        self.assertEqual(self.client.get("/api/vaults").json()["items"], [])


if __name__ == "__main__":
    unittest.main()
