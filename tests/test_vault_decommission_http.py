from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
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
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash, is_admin)
                VALUES (1, 'owner', 'Owner', 'hash', FALSE),
                       (2, 'operator', 'Operator', 'hash', FALSE),
                       (3, 'admin', 'Admin', 'hash', TRUE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote
                ) VALUES (7, 'archive', 'Exact Archive', %s, 'bucket',
                          'vaults/archive/', 'remote')
                """,
                (str(self.root),),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (7, 1, 'owner'), (7, 2, 'operator')"
            )
            enroll_vault_root_identity(connection, 7, str(self.root))
        configured = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            cookie_secure=False,
            allow_local_delete=True,
            frontend_dist_dir=str(write_spa_dist(Path(self.tmp.name))),
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.services.source_areas.settings",
        ):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = configured

    def client_for(
        self, user_id: int, *, stale_reauth: bool = False
    ) -> tuple[TestClient, dict[str, str]]:
        client = TestClient(app, client=("127.0.0.1", 50000 + user_id))
        with SQLiteConnection(str(self.db_path)) as connection:
            token = create_session(connection, user_id=user_id, auth_method="oidc")
            if stale_reauth:
                connection.execute(
                    "UPDATE sessions SET reauth_at='2000-01-01T00:00:00+00:00' "
                    "WHERE user_id=%s",
                    (user_id,),
                )
        client.cookies.set(self.settings.session_cookie_name, token)
        csrf = client.get("/api/me").json()["csrf_token"]
        return client, {"X-CSRF-Token": csrf}

    @staticmethod
    def choices() -> dict[str, str]:
        return {"local_disposition": "retain", "cloud_disposition": "retain"}

    def test_primary_owner_has_preview_and_start_seam(self) -> None:
        client, headers = self.client_for(1)
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

    def test_operator_cannot_use_owner_or_admin_seam(self) -> None:
        client, headers = self.client_for(2)
        owner = client.post(
            "/api/vaults/7/decommission/preview",
            json=self.choices(),
            headers=headers,
        )
        admin = client.post(
            "/api/admin/vaults/7/decommission/preview",
            json=self.choices(),
            headers=headers,
        )
        self.assertEqual(owner.status_code, 403)
        self.assertEqual(admin.status_code, 403)

    def test_admin_requires_recent_reauth_exact_name_reason_and_fingerprint(self) -> None:
        stale_client, stale_headers = self.client_for(3, stale_reauth=True)
        rejected = stale_client.post(
            "/api/admin/vaults/7/decommission",
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

        client, headers = self.client_for(3)
        preview = client.post(
            "/api/admin/vaults/7/decommission/preview",
            json=self.choices(),
            headers=headers,
        ).json()
        wrong_name = client.post(
            "/api/admin/vaults/7/decommission",
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
            "/api/admin/vaults/7/decommission",
            json={
                **self.choices(),
                "confirmation": "Exact Archive",
                "reason": "retire the completed archive",
                "preview_fingerprint": "f" * 64,
            },
            headers=headers,
        )
        self.assertEqual(stale.status_code, 409)
        with SQLiteConnection(str(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT decommission_state FROM vaults WHERE id=7"
                ).fetchone()["decommission_state"],
                "active",
            )

    def test_extra_deletion_fields_and_missing_csrf_fail_closed(self) -> None:
        client, headers = self.client_for(3)
        extra = client.post(
            "/api/admin/vaults/7/decommission/preview",
            json={**self.choices(), "delete_local": True},
            headers=headers,
        )
        self.assertEqual(extra.status_code, 422)
        no_csrf = client.post(
            "/api/admin/vaults/7/decommission/preview",
            json=self.choices(),
        )
        self.assertEqual(no_csrf.status_code, 403)


if __name__ == "__main__":
    unittest.main()
