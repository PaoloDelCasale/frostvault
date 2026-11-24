from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import psycopg

from app.sessions import (
    SessionTransitionError,
    create_session,
    csrf_token_for,
    resolve_session,
    revoke_session,
    rotate_session,
    session_actor,
)
from tests.fixtures import FrostVaultTestCase, bump_session_version, create_owner, seed_vault


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


class SessionStoreTests(FrostVaultTestCase):
    owner = None

    def _connect(self) -> psycopg.Connection:
        return self.database.connect()

    def test_created_session_resolves_to_the_owner(self) -> None:
        with self._connect() as connection:
            create_owner(connection, display_name="Paola")
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
            create_owner(connection)
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
                create_owner(connection)
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
            create_owner(connection)
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

    def test_a_revoked_session_cannot_be_rotated_back(self) -> None:
        token = self._new_session()
        with self._connect() as connection:
            original = resolve_session(connection, token)
            rotated_token = rotate_session(connection, original["id"], reauthenticated=True)
            current = resolve_session(connection, rotated_token)
            self.assertIsNotNone(current["reauth_at"])
            self.assertTrue(revoke_session(connection, original["id"]))
            self.assertFalse(revoke_session(connection, original["id"]))
            with self.assertRaises(SessionTransitionError):
                rotate_session(connection, original["id"])

    def test_expiry_cannot_be_undone_by_rotation(self) -> None:
        token = self._new_session()
        with self._connect() as connection:
            session = resolve_session(connection, token)
            connection.execute(
                "UPDATE sessions SET idle_expires_at=%s WHERE id=%s",
                (_past(), session["id"]),
            )
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

    def test_a_session_names_no_vault(self) -> None:
        # ``sessions.vault_id`` stays in the schema, unwritten and unread: a value
        # left by an older release is not part of the resolved Session.
        with self._connect() as connection:
            create_owner(connection)
            vault_id = seed_vault(connection)["id"]
            token = create_session(connection, auth_method="local")
            self.assertIsNone(connection.execute("SELECT vault_id FROM sessions").fetchone()["vault_id"])
            connection.execute("UPDATE sessions SET vault_id=%s", (vault_id,))

        with self._connect() as connection:
            resolved = resolve_session(connection, token)
        self.assertNotIn("vault_id", resolved)


@contextmanager
def session_statements() -> Iterator[list[str]]:
    """The text of every statement that names the ``sessions`` table, in order."""
    seen: list[str] = []
    execute = psycopg.Cursor.execute

    def recording_execute(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        text = " ".join(str(query).split())
        if "sessions" in text:
            seen.append(text)
        return execute(cursor, query, *args, **kwargs)

    with patch.object(psycopg.Cursor, "execute", recording_execute):
        yield seen


class SessionVaultColumnTests(FrostVaultTestCase):
    """``sessions.vault_id`` stays in the schema; no request writes or names it (#805)."""

    owner = {"password": "correct horse battery staple"}

    def test_a_whole_session_never_names_the_vault_column(self) -> None:
        with self.database.connect() as connection:
            seed_vault(connection, id=1, name="Docs")
            seed_vault(connection, id=2, name="Archive", source_root="/sources/managed/archive")
        client = self.http_client()
        with session_statements() as statements:
            signed_in = self.sign_in(client, password=self.owner["password"])
            headers = signed_in.csrf_headers
            for url in ("/api/me", "/api/vaults", "/api/vaults/2/catalog/revision",
                        "/api/vaults/1/catalog/events?subscribe=false"):
                self.assertEqual(client.get(url).status_code, 200, url)
            self.assertEqual(client.put("/api/locale", json={"locale": "it"},
                                        headers=headers).status_code, 200)
            self.assertEqual(client.post("/api/reauth", json={"password": self.owner["password"]},
                                         headers=headers).status_code, 200)
            self.assertEqual(client.post("/api/logout", headers=headers).status_code, 200)
        verbs = {statement.split()[0] for statement in statements}
        self.assertTrue({"INSERT", "SELECT", "UPDATE"} <= verbs, statements)
        self.assertEqual([statement for statement in statements if "vault_id" in statement], [])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT vault_id FROM sessions").fetchall(),
                             [{"vault_id": None}])


if __name__ == "__main__":
    unittest.main()
