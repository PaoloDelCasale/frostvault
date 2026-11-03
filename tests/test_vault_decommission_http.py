from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings

from app.main import app
from app.services.vault_relocation import enroll_vault_root_identity
from app.sessions import create_session
from tests.spa_fixture import write_spa_dist
from tests.test_database import run_alembic


class VaultDecommissionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash)
                VALUES (1, 'owner', 'Owner', 'hash')
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (7, 'archive', 'Exact Archive', %s, 'bucket',
                          'vaults/archive/')
                """,
                (str(self.root),),
            )
            enroll_vault_root_identity(connection, 7, str(self.root))
        configured = replace(
            settings,
            database_dsn=database_dsn(str(self.db_path)),
            cookie_secure=False,
            allow_local_delete=True,
            frontend_dist_dir=str(write_spa_dist(Path(self.tmp.name))),
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = configured

    def client_for(
        self, *, stale_reauth: bool = False
    ) -> tuple[TestClient, dict[str, str]]:
        client = TestClient(app, client=("127.0.0.1", 50001))
        with PostgresConnection(str(self.db_path)) as connection:
            token = create_session(connection, user_id=1, auth_method="oidc")
            if stale_reauth:
                connection.execute(
                    "UPDATE sessions SET reauth_at='2000-01-01T00:00:00+00:00' "
                    "WHERE user_id=1"
                )
        client.cookies.set(self.settings.session_cookie_name, token)
        csrf = client.get("/api/me").json()["csrf_token"]
        return client, {"X-CSRF-Token": csrf}

    @staticmethod
    def choices() -> dict[str, str]:
        return {"local_disposition": "retain", "cloud_disposition": "retain"}

    def test_owner_has_preview_and_start_seam(self) -> None:
        client, headers = self.client_for()
        preview = client.post(
            "/api/vaults/7/decommission/preview",
            json=self.choices(),
            headers=headers,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertTrue(body["can_start"])
        self.assertEqual(len(body["fingerprint"]), 64)

        started = client.post(
            "/api/vaults/7/decommission",
            json={
                **self.choices(),
                "confirmation": "Exact Archive",
                "reason": "retire the completed archive",
                "preview_fingerprint": body["fingerprint"],
            },
            headers=headers,
        )
        self.assertEqual(started.status_code, 202, started.text)
        self.assertEqual(started.json()["state"], "completed")
        self.assertTrue(started.json()["root_released"])

        status = client.get("/api/vaults/7/decommission/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["state"], "completed")
        me = client.get("/api/me").json()
        self.assertIsNone(me["vault"])
        self.assertEqual(me["decommission_vault"]["id"], 7)
        self.assertTrue(me["decommission_vault"]["root_released"])

    def test_start_requires_recent_reauth_exact_name_reason_and_fingerprint(self) -> None:
        stale_client, stale_headers = self.client_for(stale_reauth=True)
        rejected = stale_client.post(
            "/api/vaults/7/decommission",
            json={
                **self.choices(),
                "confirmation": "Exact Archive",
                "reason": "retire the completed archive",
                "preview_fingerprint": "0" * 64,
            },
            headers=stale_headers,
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json(), {"error": "reauth_required"})

        client, headers = self.client_for()
        preview = client.post(
            "/api/vaults/7/decommission/preview",
            json=self.choices(),
            headers=headers,
        ).json()
        wrong_name = client.post(
            "/api/vaults/7/decommission",
            json={
                **self.choices(),
                "confirmation": "exact archive",
                "reason": "retire the completed archive",
                "preview_fingerprint": preview["fingerprint"],
            },
            headers=headers,
        )
        self.assertEqual(wrong_name.status_code, 422)

        stale = client.post(
            "/api/vaults/7/decommission",
            json={
                **self.choices(),
                "confirmation": "Exact Archive",
                "reason": "retire the completed archive",
                "preview_fingerprint": "f" * 64,
            },
            headers=headers,
        )
        self.assertEqual(stale.status_code, 409)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT decommission_state FROM vaults WHERE id=7"
                ).fetchone()["decommission_state"],
                "active",
            )

    def test_extra_deletion_fields_and_missing_csrf_fail_closed(self) -> None:
        client, headers = self.client_for()
        extra = client.post(
            "/api/vaults/7/decommission/preview",
            json={**self.choices(), "delete_local": True},
            headers=headers,
        )
        self.assertEqual(extra.status_code, 422)
        no_csrf = client.post(
            "/api/vaults/7/decommission/preview",
            json=self.choices(),
        )
        self.assertEqual(no_csrf.status_code, 403)

    def test_removed_admin_decommission_routes_are_gone(self) -> None:
        client, headers = self.client_for()
        for path in (
            "/api/admin/vaults/7/decommission/preview",
            "/api/admin/vaults/7/decommission",
        ):
            with self.subTest(path=path):
                response = client.post(path, json=self.choices(), headers=headers)
                self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
