from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.database import SQLiteConnection
from app.sessions import (
    create_session,
    csrf_token_for,
    resolve_session,
    revoke_session,
    rotate_session,
    set_session_vault,
)
from tests.test_database import run_alembic


def _past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def _insert_user(
    connection: SQLiteConnection,
    *,
    username: str = "owner",
    is_admin: bool = True,
    active: bool = True,
) -> int:
    row = connection.execute(
        """
        INSERT INTO users(username, display_name, password_hash, is_admin, active)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (username, username.title(), "hash", is_admin, active),
    ).fetchone()
    return row["id"]


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database_path = Path(self._directory.name) / "sessions.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

    def _connect(self) -> SQLiteConnection:
        return SQLiteConnection(str(self.database_path))

    def test_created_session_resolves_to_the_same_user(self) -> None:
        with self._connect() as connection:
            user_id = _insert_user(connection, username="paola")
            token = create_session(
                connection,
                user_id=user_id,
                auth_method="local",
                ip="10.0.0.1",
                user_agent="pytest",
            )

        self.assertTrue(token)
        with self._connect() as connection:
            session = resolve_session(connection, token)

        self.assertIsNotNone(session)
        self.assertEqual(session["user"]["id"], user_id)
        self.assertEqual(session["user"]["username"], "paola")

    def _set_session_column(self, column: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                f"UPDATE sessions SET {column}=%s", (value,)
            )

    def _read_session_column(self, column: str) -> Any:
        with self._connect() as connection:
            return connection.execute(
                f"SELECT {column} AS value FROM sessions"
            ).fetchone()["value"]

    def _new_session(self, **user_kwargs: Any) -> str:
        with self._connect() as connection:
            user_id = _insert_user(connection, **user_kwargs)
            return create_session(
                connection, user_id=user_id, auth_method="local"
            )

    def test_session_past_absolute_expiry_does_not_resolve(self) -> None:
        token = self._new_session()
        self._set_session_column("absolute_expires_at", _past_iso())
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, token))

    def test_idle_expired_session_does_not_resolve(self) -> None:
        token = self._new_session()
        self._set_session_column("idle_expires_at", _past_iso())
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, token))

    def test_activity_slides_the_idle_window(self) -> None:
        token = self._new_session()
        soon = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
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
        token = self._new_session()
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET session_version=session_version+1"
            )
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, token))

    def test_bug_004_csrf_honors_session_version(self) -> None:
        """[BUG-004][Req: REQ-008] force-logout must invalidate CSRF lookup.

        After users.session_version bump, csrf_token_for must return None
        just like resolve_session (parity for CSRF middleware fail-closed).
        """
        with self._connect() as connection:
            user_id = _insert_user(connection, username="csrf-user", is_admin=False)
            token = create_session(
                connection, user_id=user_id, auth_method="local"
            )
            self.assertIsNotNone(csrf_token_for(connection, token))
            self.assertIsNotNone(resolve_session(connection, token))
            connection.execute(
                "UPDATE users SET session_version=session_version+1 WHERE id=%s",
                (user_id,),
            )
            self.assertIsNone(resolve_session(connection, token))
            self.assertIsNone(
                csrf_token_for(connection, token),
                "csrf_token_for must return None after session_version bump",
            )

    def test_session_for_deactivated_user_does_not_resolve(self) -> None:
        token = self._new_session()
        with self._connect() as connection:
            connection.execute("UPDATE users SET active=%s", (False,))
        with self._connect() as connection:
            self.assertIsNone(resolve_session(connection, token))

    def test_raw_token_is_never_stored(self) -> None:
        token = self._new_session()
        stored = self._read_session_column("token_hash")
        self.assertNotEqual(stored, token)
        self.assertNotIn(token, stored)

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

    def test_selected_vault_persists_across_resolves(self) -> None:
        with self._connect() as connection:
            user_id = _insert_user(connection)
            vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/src', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            token = create_session(
                connection, user_id=user_id, auth_method="local"
            )
            session = resolve_session(connection, token)
            set_session_vault(connection, session["id"], vault_id)

        with self._connect() as connection:
            resolved = resolve_session(connection, token)
        self.assertEqual(resolved["vault_id"], vault_id)





if __name__ == "__main__":
    unittest.main()
