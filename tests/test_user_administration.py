"""Global User administration rules (issue #135).

Framework-agnostic behavior of :mod:`app.services.user_administration`: the
last-active-administrator invariant, safe Identity unlinking and Invite
revocation. These run against real SQLite connections, which is where the
transaction and lock strategy actually has to hold.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from app.database import SQLiteConnection
from app.services.user_administration import AdministrationError, update_user
from tests.test_database import run_alembic


class UserAdministrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with self.connect() as connection:
            self.alice_id = self._add_user(connection, "alice", is_admin=True)
            self.bob_id = self._add_user(connection, "bob", is_admin=True)
            self.carol_id = self._add_user(connection, "carol", is_admin=False)

    def connect(self) -> SQLiteConnection:
        return SQLiteConnection(str(self.database_path))

    @staticmethod
    def _add_user(connection: SQLiteConnection, username: str, *, is_admin: bool) -> int:
        return connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash, is_admin)
            VALUES (%s, %s, 'hash', %s)
            RETURNING id
            """,
            (username, username.title(), is_admin),
        ).fetchone()["id"]

    def read_user(self, user_id: int) -> dict:
        with self.connect() as connection:
            return connection.execute(
                "SELECT is_admin, active FROM users WHERE id=%s", (user_id,)
            ).fetchone()


class LastAdministratorInvariantTests(UserAdministrationTestCase):
    def test_demoting_the_last_active_administrator_is_refused(self) -> None:
        """A racing demotion evaluated after the other one committed must fail."""
        with self.connect() as connection:
            update_user(
                connection,
                user_id=self.bob_id,
                actor_user_id=self.alice_id,
                is_admin=False,
            )
        with self.connect() as connection:
            with self.assertRaises(AdministrationError) as raised:
                update_user(
                    connection,
                    user_id=self.alice_id,
                    actor_user_id=self.bob_id,
                    is_admin=False,
                )

        self.assertEqual(raised.exception.reason, "last_admin")
        self.assertTrue(self.read_user(self.alice_id)["is_admin"])

    def test_concurrent_demotion_and_deactivation_keep_one_administrator(self) -> None:
        """Two individually safe requests must not both remove an administrator."""
        start = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def apply(changes: dict[str, Any], *, user_id: int, actor_user_id: int) -> None:
            start.wait(timeout=10)
            try:
                with self.connect() as connection:
                    update_user(
                        connection,
                        user_id=user_id,
                        actor_user_id=actor_user_id,
                        **changes,
                    )
                outcome = "applied"
            except AdministrationError as exc:
                outcome = exc.reason
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(
                target=apply,
                args=({"is_admin": False},),
                kwargs={"user_id": self.bob_id, "actor_user_id": self.alice_id},
            ),
            threading.Thread(
                target=apply,
                args=({"active": False},),
                kwargs={"user_id": self.alice_id, "actor_user_id": self.bob_id},
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(sorted(outcomes), ["applied", "last_admin"])
        with self.connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) AS total FROM users "
                "WHERE is_admin=TRUE AND active=TRUE"
            ).fetchone()["total"]
        self.assertEqual(remaining, 1)


if __name__ == "__main__":
    unittest.main()
