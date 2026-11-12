from __future__ import annotations

from tests.postgres import PostgresConnection

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


from app.sessions import (
    SessionTransitionError,
    create_session,
    csrf_token_for,
    resolve_session,
    revoke_session,
    rotate_session,
    session_actor,
    set_session_vault,
)
from tests.owner_fixtures import bump_session_version, insert_owner, insert_vault
from tests.test_database import run_alembic


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database_path = Path(self._directory.name) / "sessions.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

    def _connect(self) -> PostgresConnection:
        return PostgresConnection(str(self.database_path))

    def test_created_session_resolves_to_the_owner(self) -> None:
        with self._connect() as connection:
            insert_owner(connection, display_name="Paola")
            token = create_session(
                connection,
                auth_method="local",
                ip="10.0.0.1",
                user_agent="pytest",
            )

        self.assertTrue(token)
        with self._connect() as connection:
            session = resolve_session(connection, token)

        self.assertIsNotNone(session)
        self.assertEqual(session["owner"]["id"], 1)
        self.assertEqual(session["owner"]["display_name"], "Paola")
        self.assertEqual(session["auth_method"], "local")
        self.assertEqual(session_actor(session), "emergency")

    def test_session_actor_follows_the_sign_in_method(self) -> None:
        self.assertEqual(session_actor({"auth_method": "oidc"}), "owner")
        self.assertEqual(session_actor({"auth_method": "local"}), "emergency")
        with self.assertRaises(ValueError):
            session_actor(None)

    def test_no_session_exists_before_the_owner(self) -> None:
        with self._connect() as connection:
            with self.assertRaises(ValueError):
                create_session(connection, auth_method="local")

    def test_unknown_auth_method_is_refused(self) -> None:
        with self._connect() as connection:
            insert_owner(connection)
            with self.assertRaises(ValueError):
                create_session(connection, auth_method="password")

    def _set_session_column(self, column: str, value: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                f"UPDATE sessions SET {column}=%s", (value,)
            )

    def _read_session_column(self, column: str) -> Any:
        with self._connect() as connection:
            return connection.execute(
                f"SELECT {column} AS value FROM sessions"
            ).fetchone()["value"]

    def _new_session(self, auth_method: str = "local") -> str:
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM owner").fetchone() is None:
                insert_owner(connection)
            return create_session(connection, auth_method=auth_method)

    def test_session_past_absolute_expiry_does_not_resolve(self) -> None:
        token = self._new_session()
        self._set_session_column("absolute_expires_at", _past())
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, token))

    def test_idle_expired_session_does_not_resolve(self) -> None:
        token = self._new_session()
        self._set_session_column("idle_expires_at", _past())
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, token))

    def test_activity_slides_the_idle_window(self) -> None:
        token = self._new_session()
        soon = datetime.now(timezone.utc) + timedelta(seconds=5)
        self._set_session_column("idle_expires_at", soon)
        with self._connect() as connection:
            self.assertIsNotNone(resolve_session(connection, token))
        self.assertGreater(self._read_session_column("idle_expires_at"), soon)

    def test_unknown_token_does_not_resolve(self) -> None:
        self._new_session()
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, "not-a-real-token"))

    def test_revoked_session_stops_resolving_immediately(self) -> None:
        token = self._new_session()
        with self._connect() as connection:
            session = resolve_session(connection, token)
            revoke_session(connection, session["id"])
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, token))

    def test_bumping_session_version_invalidates_existing_sessions(self) -> None:
        oidc = self._new_session("oidc")
        emergency = self._new_session("local")
        with self._connect() as connection:
            bump_session_version(connection)
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, oidc))
            self.assertIsNone(resolve_session(connection, emergency))
        # A Session created after the bump carries the new version.
        fresh = self._new_session()
        with self._connect() as connection:
            self.assertIsNotNone(resolve_session(connection, fresh))

    def test_bug_004_csrf_honors_session_version(self) -> None:
        """[BUG-004][Req: REQ-008] force-logout must invalidate CSRF lookup.

        After an owner.session_version bump, csrf_token_for must return None
        just like resolve_session (parity for CSRF middleware fail-closed).
        """
        with self._connect() as connection:
            insert_owner(connection)
            token = create_session(connection, auth_method="local")
            self.assertIsNotNone(csrf_token_for(connection, token))
            self.assertIsNotNone(resolve_session(connection, token))
            bump_session_version(connection)
            self.assertIsNone(resolve_session(connection, token))
            self.assertIsNone(
                csrf_token_for(connection, token),
                "csrf_token_for must return None after session_version bump",
            )

    def test_raw_token_is_never_stored(self) -> None:
        token = self._new_session()
        stored = bytes(self._read_session_column("token_hash"))
        self.assertEqual(len(stored), 32)
        self.assertNotIn(token.encode(), stored)

    def test_rotation_invalidates_the_old_token_and_keeps_the_session(self) -> None:
        token = self._new_session()
        with self._connect() as connection:
            session = resolve_session(connection, token)
            session_id = session["id"]
            new_token = rotate_session(connection, session_id)

        self.assertNotEqual(new_token, token)
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, token))
        with self._connect() as connection:
            rotated = resolve_session(connection, new_token)
        self.assertIsNotNone(rotated)
        self.assertEqual(rotated["id"], session_id)

    def test_stale_selection_cannot_overwrite_a_newer_transition(self) -> None:
        token = self._new_session()
        with self._connect() as connection:
            original = resolve_session(connection, token)
            selected = set_session_vault(
                connection, original["id"], None,
                expected_version=original["transition_version"],
            )
            self.assertIsNotNone(selected)
            # Even an A -> B -> A choice must not recreate old authority.
            self.assertIsNone(set_session_vault(
                connection, original["id"], None,
                expected_version=original["transition_version"],
            ))
            rotated_token = rotate_session(connection, original["id"], reauthenticated=True)
            self.assertIsNone(set_session_vault(
                connection, original["id"], None,
                expected_version=selected["transition_version"],
            ))
            current = resolve_session(connection, rotated_token)
            self.assertIsNotNone(current["reauth_at"])
            self.assertTrue(revoke_session(connection, original["id"]))
            self.assertFalse(revoke_session(connection, original["id"]))
            self.assertIsNone(set_session_vault(
                connection, original["id"], None,
                expected_version=current["transition_version"],
            ))
            with self.assertRaises(SessionTransitionError):
                rotate_session(connection, original["id"])

    def test_expiry_cannot_be_undone_by_selection_or_rotation(self) -> None:
        token = self._new_session()
        with self._connect() as connection:
            session = resolve_session(connection, token)
            connection.execute(
                "UPDATE sessions SET idle_expires_at=%s WHERE id=%s",
                (_past(), session["id"]),
            )
            self.assertIsNone(set_session_vault(
                connection, session["id"], None,
                expected_version=session["transition_version"],
            ))
            with self.assertRaises(SessionTransitionError):
                rotate_session(connection, session["id"])
            self.assertIsNone(resolve_session(connection, token))
            first_version = connection.execute(
                "SELECT transition_version FROM sessions WHERE id=%s", (session["id"],),
            ).fetchone()["transition_version"]
            self.assertIsNone(resolve_session(connection, token))
            second_version = connection.execute(
                "SELECT transition_version FROM sessions WHERE id=%s", (session["id"],),
            ).fetchone()["transition_version"]
            self.assertEqual(second_version, first_version)

    def test_selected_vault_persists_across_resolves(self) -> None:
        with self._connect() as connection:
            insert_owner(connection)
            vault_id = insert_vault(connection)["id"]
            token = create_session(connection, auth_method="local")
            session = resolve_session(connection, token)
            set_session_vault(connection, session["id"], vault_id)

        with self._connect() as connection:
            resolved = resolve_session(connection, token)
        self.assertEqual(resolved["vault_id"], vault_id)


if __name__ == "__main__":
    unittest.main()
