"""Single-owner HTTP surface (#412): multi-user endpoints are gone (404)."""

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
from app.sessions import create_session
from tests.spa_fixture import write_spa_dist
from tests.test_database import run_alembic

# Every operation removed with multi-user support, invites, runtime OIDC
# configuration and Source Areas.
REMOVED_OPERATIONS = (
    ("GET", "/api/admin/users"),
    ("POST", "/api/admin/users"),
    ("PATCH", "/api/admin/users/1"),
    ("GET", "/api/admin/users/1/identities"),
    ("DELETE", "/api/admin/users/1/identities/1"),
    ("GET", "/api/admin/invites"),
    ("POST", "/api/admin/invites"),
    ("POST", "/api/admin/invites/1/revoke"),
    ("GET", "/api/admin/oidc-configuration"),
    ("PUT", "/api/admin/oidc-configuration/draft"),
    ("POST", "/api/admin/oidc-configuration/draft/validate"),
    ("POST", "/api/admin/oidc-configuration/activate"),
    ("POST", "/api/admin/oidc-configuration/disable"),
    ("POST", "/api/admin/oidc-configuration/rotate-secret"),
    ("GET", "/api/admin/source-areas"),
    ("POST", "/api/admin/source-areas"),
    ("DELETE", "/api/admin/source-areas/1"),
    ("GET", "/api/source-areas"),
    ("GET", "/api/admin/source-volumes/media/browse"),
    ("GET", "/api/vault/members"),
    ("POST", "/api/vault/members"),
    ("DELETE", "/api/vault/members/1"),
    ("POST", "/api/vault/transfer-owner"),
    ("POST", "/api/vault/user-lookup"),
    ("POST", "/api/vault/user-suggest"),
    ("POST", "/api/admin/vaults"),
    ("GET", "/api/admin/vaults/1/members"),
    ("POST", "/api/admin/vaults/1/members"),
    ("DELETE", "/api/admin/vaults/1/members/1"),
    ("POST", "/api/admin/vaults/1/transfer-owner"),
    ("POST", "/api/admin/vaults/1/recovery/export"),
    ("POST", "/api/admin/vaults/1/decommission/preview"),
    ("POST", "/api/admin/vaults/1/decommission"),
    ("GET", "/api/admin/vaults/1/decommission/status"),
    ("POST", "/api/admin/vaults/1/decommission/cloud-purge/cancel"),
    ("GET", "/auth/oidc/link"),
)


class RemovedMultiUserEndpointsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "app.db"
        migrated = run_alembic(db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with PostgresConnection(str(db_path)) as connection:
            connection.execute(
                """
                INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')
                """
            )
        configured = replace(
            settings,
            database_dsn=database_dsn(str(db_path)),
            cookie_secure=False,
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
        self.client = TestClient(app, client=("127.0.0.1", 50001))
        with PostgresConnection(str(db_path)) as connection:
            token = create_session(connection, user_id=1, auth_method="oidc")
        self.client.cookies.set(configured.session_cookie_name, token)
        self.csrf = self.client.get("/api/me").json()["csrf_token"]

    def test_removed_endpoints_answer_not_found_to_the_owner(self) -> None:
        for method, path in REMOVED_OPERATIONS:
            with self.subTest(method=method, path=path):
                response = self.client.request(
                    method,
                    path,
                    json={},
                    headers={"X-CSRF-Token": self.csrf},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 404, response.text)

    def test_removed_endpoints_are_absent_from_the_openapi_document(self) -> None:
        paths = app.openapi()["paths"]
        for prefix in (
            "/api/admin/users",
            "/api/admin/invites",
            "/api/admin/oidc-configuration",
            "/api/vault/members",
            "/api/admin/source-areas",
            "/api/source-areas",
        ):
            with self.subTest(prefix=prefix):
                self.assertFalse(
                    [path for path in paths if path.startswith(prefix)], prefix
                )


if __name__ == "__main__":
    unittest.main()
