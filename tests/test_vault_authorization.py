from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.services.vault_governance import (
    GovernanceError,
    assign_member_role,
    primary_owner,
    remove_member,
    transfer_primary_ownership,
)
from app.services.vault_roles import can_operate, is_owner, is_valid_role
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Direct-import tests: the owner/operator/viewer matrix predicates.
# ---------------------------------------------------------------------------
class VaultRolesTests(unittest.TestCase):
    def test_owner_and_operator_can_operate_but_viewer_cannot(self) -> None:
        self.assertTrue(can_operate("owner"))
        self.assertTrue(can_operate("operator"))
        self.assertFalse(can_operate("viewer"))

    def test_only_the_owner_governs_sharing_and_policy(self) -> None:
        self.assertTrue(is_owner("owner"))
        self.assertFalse(is_owner("operator"))
        self.assertFalse(is_owner("viewer"))

    def test_only_the_three_matrix_roles_are_valid(self) -> None:
        self.assertTrue(is_valid_role("owner"))
        self.assertTrue(is_valid_role("operator"))
        self.assertTrue(is_valid_role("viewer"))
        self.assertFalse(is_valid_role("superuser"))


# ---------------------------------------------------------------------------
# Direct-import tests: the governance service (assignment, removal, transfer).
# ---------------------------------------------------------------------------
class VaultGovernanceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "governance.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, "
                "is_admin) VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, "
                "is_admin) VALUES (2, 'bob', 'Bob', 'hash', 0)"
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote
                ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                """
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (1, 1, 'owner')"
            )

    def test_assign_member_role_never_hands_out_owner(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(GovernanceError) as ctx:
                assign_member_role(connection, vault_id=1, user_id=2, role="owner")
        self.assertEqual(ctx.exception.reason, "invalid_role")

    def test_assign_member_role_accepts_operator_and_viewer(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            assign_member_role(connection, vault_id=1, user_id=2, role="operator")
            role = connection.execute(
                "SELECT role FROM vault_members WHERE vault_id=1 AND user_id=2"
            ).fetchone()["role"]
        self.assertEqual(role, "operator")

    def test_assign_member_role_rejects_unknown_vault_or_user(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(GovernanceError) as vault_ctx:
                assign_member_role(connection, vault_id=999, user_id=2, role="viewer")
            self.assertEqual(vault_ctx.exception.reason, "vault_not_found")
            with self.assertRaises(GovernanceError) as user_ctx:
                assign_member_role(connection, vault_id=1, user_id=999, role="viewer")
            self.assertEqual(user_ctx.exception.reason, "user_not_found")

    def test_assign_member_role_refuses_to_demote_the_primary_owner(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(GovernanceError) as ctx:
                assign_member_role(connection, vault_id=1, user_id=1, role="operator")
        self.assertEqual(ctx.exception.reason, "owner_required")
        with SQLiteConnection(str(self.database_path)) as connection:
            owner = connection.execute(
                "SELECT role FROM vault_members WHERE vault_id=1 AND user_id=1"
            ).fetchone()
        self.assertEqual(owner["role"], "owner")

    def test_assignment_cannot_demote_owner_promoted_by_concurrent_transfer(
        self,
    ) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            assign_member_role(connection, vault_id=1, user_id=2, role="viewer")

        role_read = threading.Event()
        continue_assignment = threading.Event()
        assignment_errors: list[BaseException] = []

        class PausingConnection:
            def __init__(self, connection: SQLiteConnection) -> None:
                self.connection = connection

            def execute(self, sql: str, params=()):
                result = self.connection.execute(sql, params)
                if sql.strip().startswith("SELECT role FROM vault_members"):
                    role_read.set()
                    if not continue_assignment.wait(timeout=5):
                        raise TimeoutError("ownership transfer did not complete")
                return result

        def assign_role() -> None:
            try:
                with SQLiteConnection(str(self.database_path)) as connection:
                    assign_member_role(
                        PausingConnection(connection),
                        vault_id=1,
                        user_id=2,
                        role="viewer",
                    )
            except BaseException as exc:
                assignment_errors.append(exc)

        assignment = threading.Thread(target=assign_role)
        assignment.start()
        self.assertTrue(role_read.wait(timeout=5))
        try:
            with SQLiteConnection(str(self.database_path)) as connection:
                transfer_primary_ownership(
                    connection,
                    vault_id=1,
                    new_owner_user_id=2,
                    expected_current_owner_user_id=None,
                )
        finally:
            continue_assignment.set()
            assignment.join(timeout=5)

        self.assertFalse(assignment.is_alive())
        self.assertEqual(
            [getattr(error, "reason", None) for error in assignment_errors],
            ["owner_required"],
        )
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(primary_owner(connection, 1), {"user_id": 2})

    def test_remove_member_refuses_to_remove_the_primary_owner(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(GovernanceError) as ctx:
                remove_member(connection, vault_id=1, user_id=1)
        self.assertEqual(ctx.exception.reason, "owner_required")
        with SQLiteConnection(str(self.database_path)) as connection:
            owner = connection.execute(
                "SELECT role FROM vault_members WHERE vault_id=1 AND user_id=1"
            ).fetchone()
        self.assertEqual(owner["role"], "owner")

    def test_remove_member_deletes_a_non_owner(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            assign_member_role(connection, vault_id=1, user_id=2, role="viewer")
            remove_member(connection, vault_id=1, user_id=2)
            row = connection.execute(
                "SELECT 1 FROM vault_members WHERE vault_id=1 AND user_id=2"
            ).fetchone()
        self.assertIsNone(row)

    def test_remove_member_rejects_when_no_non_owner_row_is_deleted(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(GovernanceError) as ctx:
                remove_member(connection, vault_id=1, user_id=999)
        self.assertEqual(ctx.exception.reason, "member_not_found")

    def test_removal_cannot_delete_owner_promoted_by_concurrent_transfer(
        self,
    ) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            assign_member_role(connection, vault_id=1, user_id=2, role="viewer")

        role_read = threading.Event()
        continue_removal = threading.Event()
        removal_errors: list[BaseException] = []

        class PausingConnection:
            def __init__(self, connection: SQLiteConnection) -> None:
                self.connection = connection

            def execute(self, sql: str, params=()):
                if sql.strip().startswith("DELETE FROM vault_members"):
                    role_read.set()
                    if not continue_removal.wait(timeout=5):
                        raise TimeoutError("ownership transfer did not complete")
                return self.connection.execute(sql, params)

        def remove_role() -> None:
            try:
                with SQLiteConnection(str(self.database_path)) as connection:
                    remove_member(
                        PausingConnection(connection), vault_id=1, user_id=2
                    )
            except BaseException as exc:
                removal_errors.append(exc)

        removal = threading.Thread(target=remove_role)
        removal.start()
        self.assertTrue(role_read.wait(timeout=5))
        try:
            with SQLiteConnection(str(self.database_path)) as connection:
                transfer_primary_ownership(
                    connection,
                    vault_id=1,
                    new_owner_user_id=2,
                    expected_current_owner_user_id=None,
                )
        finally:
            continue_removal.set()
            removal.join(timeout=5)

        self.assertFalse(removal.is_alive())
        self.assertEqual(
            [getattr(error, "reason", None) for error in removal_errors],
            ["owner_required"],
        )
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(primary_owner(connection, 1), {"user_id": 2})

    def test_transfer_swaps_roles_and_never_touches_the_namespace(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            assign_member_role(connection, vault_id=1, user_id=2, role="operator")
            namespace_before = connection.execute(
                "SELECT uuid FROM vaults WHERE id=1"
            ).fetchone()["uuid"]

            result = transfer_primary_ownership(
                connection,
                vault_id=1,
                new_owner_user_id=2,
                expected_current_owner_user_id=1,
            )

            roles = {
                row["user_id"]: row["role"]
                for row in connection.execute(
                    "SELECT user_id, role FROM vault_members WHERE vault_id=1"
                ).fetchall()
            }
            namespace_after = connection.execute(
                "SELECT uuid FROM vaults WHERE id=1"
            ).fetchone()["uuid"]

        self.assertEqual(result, {"previous_owner_id": 1, "new_owner_id": 2})
        self.assertEqual(roles, {1: "operator", 2: "owner"})
        self.assertEqual(namespace_before, namespace_after)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(primary_owner(connection, 1), {"user_id": 2})

    def test_stale_owner_authorization_cannot_transfer_after_concurrent_transfer(
        self,
    ) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, "
                "is_admin) VALUES (3, 'carol', 'Carol', 'hash', 0)"
            )
            assign_member_role(connection, vault_id=1, user_id=2, role="viewer")
            transfer_primary_ownership(
                connection,
                vault_id=1,
                new_owner_user_id=2,
                expected_current_owner_user_id=1,
            )

        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(GovernanceError) as ctx:
                transfer_primary_ownership(
                    connection,
                    vault_id=1,
                    new_owner_user_id=3,
                    expected_current_owner_user_id=1,
                )

        self.assertEqual(ctx.exception.reason, "ownership_changed")
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(primary_owner(connection, 1), {"user_id": 2})

    def test_transfer_rejects_transferring_to_the_current_owner(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(GovernanceError) as ctx:
                transfer_primary_ownership(
                    connection,
                    vault_id=1,
                    new_owner_user_id=1,
                    expected_current_owner_user_id=1,
                )
        self.assertEqual(ctx.exception.reason, "already_owner")

    def test_transfer_rejects_unknown_vault_or_target_user(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(GovernanceError) as vault_ctx:
                transfer_primary_ownership(
                    connection,
                    vault_id=999,
                    new_owner_user_id=2,
                    expected_current_owner_user_id=1,
                )
            self.assertEqual(vault_ctx.exception.reason, "vault_not_found")
            with self.assertRaises(GovernanceError) as user_ctx:
                transfer_primary_ownership(
                    connection,
                    vault_id=1,
                    new_owner_user_id=999,
                    expected_current_owner_user_id=1,
                )
            self.assertEqual(user_ctx.exception.reason, "user_not_found")


# ---------------------------------------------------------------------------
# HTTP tests: the full owner/operator/viewer matrix plus the global-admin
# exception (reauth, reason, audit, owner-notification seam).
# ---------------------------------------------------------------------------
class VaultAuthorizationHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.admin_id = self._create_user(connection, "admin", is_admin=True)
            self.owner_id = self._create_user(connection, "owner")
            self.operator_id = self._create_user(connection, "operator")
            self.viewer_id = self._create_user(connection, "viewer")
            self.outsider_id = self._create_user(connection, "outsider")
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (self.vault_id, self.owner_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'operator')",
                (self.vault_id, self.operator_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'viewer')",
                (self.vault_id, self.viewer_id),
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            allow_local_delete=True,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(
            app=main.app, client=("127.0.0.1", 50000), follow_redirects=False
        )

    def _create_user(self, connection, username: str, *, is_admin: bool = False) -> int:
        return connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash, is_admin)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (username, username.title(), hash_password(self.PASSWORD), is_admin),
        ).fetchone()["id"]

    def _authenticate(self, user_id: int) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(connection, user_id=user_id, auth_method="oidc")
            csrf_token = csrf_token_for(connection, raw_token)
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)
        self.client.cookies.set("frostvault_csrf", csrf_token)

    def _csrf(self) -> dict:
        return {"X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""}

    def _expire_reauth(self, user_id: int) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                ("2000-01-01T00:00:00+00:00", user_id),
            )

    # --- operation endpoints: owner + operator may operate, viewer may not ---

    def test_scan_requires_operation_authorization_without_invoking_scan(
        self,
    ) -> None:
        async def no_op(*_args) -> None:
            return None

        for user_id in (self.owner_id, self.operator_id):
            self._authenticate(user_id)
            with patch(
                "app.main.asyncio.to_thread", side_effect=no_op
            ) as to_thread:
                response = self.client.post("/api/scan", headers=self._csrf())
            self.assertEqual(response.status_code, 202, response.text)
            to_thread.assert_called_once()

        self._authenticate(self.viewer_id)
        with patch(
            "app.main.asyncio.to_thread", side_effect=no_op
        ) as to_thread:
            response = self.client.post("/api/scan", headers=self._csrf())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"], "Vault is read-only")
        to_thread.assert_not_called()

    def test_viewer_cannot_upload_recover_or_free_space(self) -> None:
        self._authenticate(self.viewer_id)
        for endpoint in ("/api/upload", "/api/recover", "/api/free-space"):
            response = self.client.post(
                endpoint,
                json={"path": "report.txt"},
                headers=self._csrf(),
            )
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["detail"], "Vault is read-only")

    def test_operator_can_upload_and_recover_but_not_free_space(self) -> None:
        self._authenticate(self.operator_id)
        for endpoint in ("/api/upload", "/api/recover"):
            response = self.client.post(
                endpoint, json={"path": "report.txt"}, headers=self._csrf()
            )
            # 409 (no eligible files) proves the role check passed, unlike
            # the 403 a viewer gets for the same request.
            self.assertEqual(response.status_code, 409, response.text)

        response = self.client.post(
            "/api/free-space", json={"path": "report.txt"}, headers=self._csrf()
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"], "Vault is read-only")

    def test_owner_can_upload_recover_and_free_space(self) -> None:
        self._authenticate(self.owner_id)
        for endpoint in ("/api/upload", "/api/recover", "/api/free-space"):
            response = self.client.post(
                endpoint, json={"path": "report.txt"}, headers=self._csrf()
            )
            self.assertEqual(response.status_code, 409, response.text)

    # --- owner lookup and sharing: only the owner manages membership --------

    def test_owner_lookup_returns_only_safe_active_user_fields(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            inactive_id = self._create_user(connection, "inactive")
            connection.execute("UPDATE users SET active=FALSE WHERE id=%s", (inactive_id,))

        self._authenticate(self.owner_id)
        response = self.client.post(
            "/api/vault/user-lookup",
            json={"username": "  OPERATOR  "},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            set(response.json()),
            {"id", "username", "display_name", "current_vault_role"},
        )
        self.assertEqual(response.json()["current_vault_role"], "operator")

        unknown = self.client.post(
            "/api/vault/user-lookup",
            json={"username": "does-not-exist"},
            headers=self._csrf(),
        )
        inactive = self.client.post(
            "/api/vault/user-lookup",
            json={"username": "inactive"},
            headers=self._csrf(),
        )
        self.assertEqual(unknown.status_code, inactive.status_code)
        self.assertEqual(unknown.json(), inactive.json())

    def test_lookup_requires_owner_recent_reauth_and_csrf(self) -> None:
        for user_id in (self.operator_id, self.viewer_id):
            self._authenticate(user_id)
            response = self.client.post(
                "/api/vault/user-lookup",
                json={"username": "outsider"},
                headers=self._csrf(),
            )
            self.assertEqual(response.status_code, 403, response.text)

        self._authenticate(self.owner_id)
        response = self.client.post(
            "/api/vault/user-lookup", json={"username": "outsider"}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self._expire_reauth(self.owner_id)
        response = self.client.post(
            "/api/vault/user-lookup",
            json={"username": "outsider"},
            headers=self._csrf(),
        )
        self.assertEqual(response.json(), {"error": "reauth_required"})

    def test_lookup_is_limited_to_ten_attempts_per_owner_and_client_ip(self) -> None:
        self._authenticate(self.owner_id)
        responses = [
            self.client.post(
                "/api/vault/user-lookup",
                json={"username": "unknown-user"},
                headers=self._csrf(),
            )
            for _ in range(11)
        ]
        self.assertTrue(all(response.status_code == 404 for response in responses[:10]))
        self.assertEqual(responses[-1].status_code, 429, responses[-1].text)
        self.assertGreaterEqual(int(responses[-1].headers["Retry-After"]), 1)

    def test_operator_and_viewer_cannot_manage_sharing(self) -> None:
        for user_id in (self.operator_id, self.viewer_id):
            self._authenticate(user_id)
            listing = self.client.get("/api/vault/members")
            self.assertEqual(listing.status_code, 403, listing.text)
            grant = self.client.post(
                "/api/vault/members",
                json={"user_id": self.outsider_id, "role": "viewer"},
                headers=self._csrf(),
            )
            self.assertEqual(grant.status_code, 403, grant.text)

    def test_owner_manages_sharing_as_operator_or_viewer_only(self) -> None:
        self._authenticate(self.owner_id)
        listing = self.client.get("/api/vault/members")
        self.assertEqual(listing.status_code, 200, listing.text)

        grant = self.client.post(
            "/api/vault/members",
            json={"user_id": self.outsider_id, "role": "operator"},
            headers=self._csrf(),
        )
        self.assertEqual(grant.status_code, 201, grant.text)

        rejected = self.client.post(
            "/api/vault/members",
            json={"user_id": self.outsider_id, "role": "owner"},
            headers=self._csrf(),
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)

        revoke = self.client.delete(
            f"/api/vault/members/{self.outsider_id}", headers=self._csrf()
        )
        self.assertEqual(revoke.status_code, 200, revoke.text)

    def test_owner_cannot_demote_themself_via_assignment(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.post(
            "/api/vault/members",
            json={"user_id": self.owner_id, "role": "operator"},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 400, response.text)

        with SQLiteConnection(str(self.database_path)) as connection:
            role = connection.execute(
                "SELECT role FROM vault_members WHERE vault_id=%s AND user_id=%s",
                (self.vault_id, self.owner_id),
            ).fetchone()["role"]
        self.assertEqual(role, "owner")

    def test_owner_cannot_remove_themself_without_transferring_first(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.delete(
            f"/api/vault/members/{self.owner_id}", headers=self._csrf()
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_assignment_rejects_stale_owner_authorization(self) -> None:
        self._authenticate(self.owner_id)

        def transfer_then_assign(connection, **kwargs):
            with SQLiteConnection(str(self.database_path)) as concurrent:
                transfer_primary_ownership(
                    concurrent,
                    vault_id=self.vault_id,
                    new_owner_user_id=self.viewer_id,
                    expected_current_owner_user_id=None,
                )
            return assign_member_role(connection, **kwargs)

        with patch("app.main.assign_member_role", transfer_then_assign):
            response = self.client.post(
                "/api/vault/members",
                json={"user_id": self.outsider_id, "role": "viewer"},
                headers=self._csrf(),
            )

        self.assertEqual(response.status_code, 409, response.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            member = connection.execute(
                "SELECT role FROM vault_members WHERE vault_id=%s AND user_id=%s",
                (self.vault_id, self.outsider_id),
            ).fetchone()
            self.assertEqual(
                primary_owner(connection, self.vault_id),
                {"user_id": self.viewer_id},
            )
        self.assertIsNone(member)

    def test_removal_rejects_stale_owner_authorization(self) -> None:
        self._authenticate(self.owner_id)

        def transfer_then_remove(connection, **kwargs):
            with SQLiteConnection(str(self.database_path)) as concurrent:
                transfer_primary_ownership(
                    concurrent,
                    vault_id=self.vault_id,
                    new_owner_user_id=self.viewer_id,
                    expected_current_owner_user_id=None,
                )
            return remove_member(connection, **kwargs)

        with patch("app.main.remove_member", transfer_then_remove):
            response = self.client.delete(
                f"/api/vault/members/{self.operator_id}",
                headers=self._csrf(),
            )

        self.assertEqual(response.status_code, 409, response.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            member = connection.execute(
                "SELECT role FROM vault_members WHERE vault_id=%s AND user_id=%s",
                (self.vault_id, self.operator_id),
            ).fetchone()
            self.assertEqual(
                primary_owner(connection, self.vault_id),
                {"user_id": self.viewer_id},
            )
        self.assertEqual(member, {"role": "operator"})

    # --- transactional ownership transfer -----------------------------------

    def test_operator_and_viewer_cannot_transfer_ownership(self) -> None:
        for user_id in (self.operator_id, self.viewer_id):
            self._authenticate(user_id)
            response = self.client.post(
                "/api/vault/transfer-owner",
                json={"new_owner_user_id": self.operator_id},
                headers=self._csrf(),
            )
            self.assertEqual(response.status_code, 403, response.text)

    def test_owner_cannot_transfer_to_a_nonmember(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.post(
            "/api/vault/transfer-owner",
            json={"new_owner_user_id": self.outsider_id},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 404, response.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(primary_owner(connection, self.vault_id), {"user_id": self.owner_id})

    def test_owner_transfers_ownership_preserving_exactly_one_owner(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.post(
            "/api/vault/transfer-owner",
            json={"new_owner_user_id": self.operator_id},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["previous_owner_id"], self.owner_id
        )
        self.assertEqual(response.json()["new_owner_id"], self.operator_id)

        with SQLiteConnection(str(self.database_path)) as connection:
            roles = {
                row["user_id"]: row["role"]
                for row in connection.execute(
                    "SELECT user_id, role FROM vault_members WHERE vault_id=%s",
                    (self.vault_id,),
                ).fetchall()
            }
            owners = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_members "
                "WHERE vault_id=%s AND role='owner'",
                (self.vault_id,),
            ).fetchone()["total"]

        self.assertEqual(owners, 1)
        self.assertEqual(roles[self.operator_id], "owner")
        self.assertEqual(roles[self.owner_id], "operator")

    def test_concurrent_transfer_rejects_request_with_stale_owner_authorization(
        self,
    ) -> None:
        self._authenticate(self.owner_id)
        first_authorized = threading.Event()
        continue_first = threading.Event()
        first_responses = []

        def pausing_transfer(connection, **kwargs):
            if kwargs["new_owner_user_id"] == self.operator_id:
                first_authorized.set()
                if not continue_first.wait(timeout=5):
                    raise TimeoutError("concurrent transfer did not complete")
            return transfer_primary_ownership(connection, **kwargs)

        def transfer_to_operator() -> None:
            first_responses.append(
                self.client.post(
                    "/api/vault/transfer-owner",
                    json={"new_owner_user_id": self.operator_id},
                    headers=self._csrf(),
                )
            )

        with patch("app.main.transfer_primary_ownership", pausing_transfer):
            first = threading.Thread(target=transfer_to_operator)
            first.start()
            self.assertTrue(first_authorized.wait(timeout=5))
            try:
                second = self.client.post(
                    "/api/vault/transfer-owner",
                    json={"new_owner_user_id": self.viewer_id},
                    headers=self._csrf(),
                )
                self.assertEqual(second.status_code, 200, second.text)
            finally:
                continue_first.set()
                first.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertEqual(first_responses[0].status_code, 409, first_responses[0].text)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.assertEqual(
                primary_owner(connection, self.vault_id),
                {"user_id": self.viewer_id},
            )

    def test_stale_reauth_blocks_sharing_and_transfer_for_the_owner(self) -> None:
        self._authenticate(self.owner_id)
        self._expire_reauth(self.owner_id)

        grant = self.client.post(
            "/api/vault/members",
            json={"user_id": self.outsider_id, "role": "viewer"},
            headers=self._csrf(),
        )
        self.assertEqual(grant.status_code, 403, grant.text)
        self.assertEqual(grant.json(), {"error": "reauth_required"})

        transfer = self.client.post(
            "/api/vault/transfer-owner",
            json={"new_owner_user_id": self.operator_id},
            headers=self._csrf(),
        )
        self.assertEqual(transfer.status_code, 403, transfer.text)
        self.assertEqual(transfer.json(), {"error": "reauth_required"})

    # --- global-admin exception: reauth + reason + audit + owner notice ---

    def test_non_admin_cannot_use_the_admin_override_routes(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.post(
            f"/api/admin/vaults/{self.vault_id}/members",
            json={"user_id": self.outsider_id, "role": "viewer", "reason": "test"},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_admin_override_without_a_reason_is_rejected(self) -> None:
        self._authenticate(self.admin_id)
        response = self.client.post(
            f"/api/admin/vaults/{self.vault_id}/members",
            json={"user_id": self.outsider_id, "role": "viewer"},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_admin_override_cannot_assign_the_owner_role(self) -> None:
        self._authenticate(self.admin_id)
        response = self.client.post(
            f"/api/admin/vaults/{self.vault_id}/members",
            json={
                "user_id": self.outsider_id,
                "role": "owner",
                "reason": "incident response",
            },
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_admin_override_cannot_demote_the_primary_owner(self) -> None:
        self._authenticate(self.admin_id)
        response = self.client.post(
            f"/api/admin/vaults/{self.vault_id}/members",
            json={
                "user_id": self.owner_id,
                "role": "operator",
                "reason": "incident response",
            },
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 400, response.text)

        with SQLiteConnection(str(self.database_path)) as connection:
            role = connection.execute(
                "SELECT role FROM vault_members WHERE vault_id=%s AND user_id=%s",
                (self.vault_id, self.owner_id),
            ).fetchone()["role"]
        self.assertEqual(role, "owner")

    def test_admin_override_membership_is_audited_and_notifies_the_owner(self) -> None:
        self._authenticate(self.admin_id)
        with self.assertLogs("app.audit", level="WARNING") as captured:
            response = self.client.post(
                f"/api/admin/vaults/{self.vault_id}/members",
                json={
                    "user_id": self.outsider_id,
                    "role": "operator",
                    "reason": "owner is on leave; team needs upload access",
                },
                headers=self._csrf(),
            )
        self.assertEqual(response.status_code, 201, response.text)
        record = captured.records[-1].getMessage()
        self.assertIn('"event": "vault_membership_changed"', record)
        self.assertIn('"admin_override": true', record)
        self.assertIn(str(self.owner_id), record)
        self.assertIn("owner is on leave", record)

    def test_admin_override_removal_requires_a_reason_and_is_audited(self) -> None:
        self._authenticate(self.admin_id)
        missing_reason = self.client.delete(
            f"/api/admin/vaults/{self.vault_id}/members/{self.viewer_id}",
            headers=self._csrf(),
        )
        self.assertEqual(missing_reason.status_code, 422, missing_reason.text)

        with self.assertLogs("app.audit", level="WARNING") as captured:
            response = self.client.delete(
                f"/api/admin/vaults/{self.vault_id}/members/{self.viewer_id}",
                params={"reason": "departing contractor"},
                headers=self._csrf(),
            )
        self.assertEqual(response.status_code, 200, response.text)
        record = captured.records[-1].getMessage()
        self.assertIn('"event": "vault_membership_changed"', record)
        self.assertIn("departing contractor", record)

    def test_admin_members_report_activity_for_transfer_eligibility(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE users SET active=FALSE WHERE id=%s", (self.operator_id,)
            )

        self._authenticate(self.admin_id)
        response = self.client.get(
            f"/api/admin/vaults/{self.vault_id}/members"
        )
        self.assertEqual(response.status_code, 200, response.text)
        members = {item["id"]: item for item in response.json()["items"]}
        self.assertFalse(members[self.operator_id]["active"])
        self.assertTrue(members[self.viewer_id]["active"])

    def test_admin_transfer_ownership_notifies_both_previous_and_new_owner(self) -> None:
        self._authenticate(self.admin_id)
        with self.assertLogs("app.audit", level="WARNING") as captured:
            response = self.client.post(
                f"/api/admin/vaults/{self.vault_id}/transfer-owner",
                json={
                    "new_owner_user_id": self.operator_id,
                    "reason": "owner account compromised",
                },
                headers=self._csrf(),
            )
        self.assertEqual(response.status_code, 200, response.text)
        notified = {
            record.getMessage()
            for record in captured.records
            if '"event": "vault_ownership_transferred"' in record.getMessage()
        }
        self.assertEqual(len(notified), 2)
        combined = " ".join(notified)
        self.assertIn(str(self.owner_id), combined)
        self.assertIn(str(self.operator_id), combined)
        self.assertIn("owner account compromised", combined)

        with SQLiteConnection(str(self.database_path)) as connection:
            owners = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_members "
                "WHERE vault_id=%s AND role='owner'",
                (self.vault_id,),
            ).fetchone()["total"]
        self.assertEqual(owners, 1)

    def test_admin_transfer_ownership_without_a_reason_is_rejected(self) -> None:
        self._authenticate(self.admin_id)
        response = self.client.post(
            f"/api/admin/vaults/{self.vault_id}/transfer-owner",
            json={"new_owner_user_id": self.operator_id},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
