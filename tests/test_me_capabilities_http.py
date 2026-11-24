"""GET /api/me: the Owner's identity and, without an operational Vault, the stopped one.

The single owner (#412) has no Vault role: every Vault is theirs (#791). The
interface chooses the Vault it opens on (the route, the one remembered in the
browser, else the first listed): ``/api/me`` names none (#805).
"""

from __future__ import annotations

import re
import unittest
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

import psycopg

from tests.fixtures import FrostVaultTestCase, override_settings, seed_vault
from tests.spa_fixture import write_spa_dist


_TARGET = re.compile(r"\b(?:FROM|INTO|UPDATE)\s+(\w+)")


@contextmanager
def recorded_statements() -> Iterator[list[str]]:
    """Each statement as its verb and first table, and the commits, in order.

    The pool's own checkout probe (an empty query) and reset (``DISCARD ALL``)
    are left out: they are not the route's.
    """
    seen: list[str] = []
    execute = psycopg.Cursor.execute
    commit = psycopg.Connection.commit

    def recording_execute(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        text = " ".join(str(query).split())
        if text and text != "DISCARD ALL":
            target = _TARGET.search(text)
            seen.append(f"{text.split()[0]} {target.group(1) if target else '?'}")
        return execute(cursor, query, *args, **kwargs)

    def recording_commit(connection: Any) -> None:
        seen.append("COMMIT")
        commit(connection)

    with (
        patch.object(psycopg.Cursor, "execute", recording_execute),
        patch.object(psycopg.Connection, "commit", recording_commit),
    ):
        yield seen


class MeCapabilitiesHttpTests(FrostVaultTestCase):
    PASSWORD = "correct horse battery staple"
    owner = {"password": PASSWORD}

    def setUp(self) -> None:
        super().setUp()
        self.dist_dir = write_spa_dist(self.tmp_path)
        self.enterContext(override_settings(frontend_dist_dir=str(self.dist_dir)))
        self.client = self.http_client()

    def _create_vault(self, **values) -> dict:
        with self.database.connect() as connection:
            return seed_vault(connection, name="Docs Archive", **values)

    def _me(self) -> dict:
        self.sign_in()
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_me_names_no_vault_to_open(self) -> None:
        self._create_vault()
        payload = self._me()

        self.assertNotIn("vault", payload)
        self.assertIsNone(payload["stopped_vault"])

    def test_owner_without_vault_gets_no_stopped_vault(self) -> None:
        """Seam 6: before the first Vault exists."""
        payload = self._me()

        self.assertNotIn("vault", payload)
        self.assertIsNone(payload["stopped_vault"])
        self.assertEqual(payload["display_name"], "Owner")

    def test_stopped_vault_is_reported_when_no_vault_is_protected(self) -> None:
        stopping = self._create_vault(lifecycle="stopping")
        payload = self._me()

        self.assertNotIn("vault", payload)
        stopped = payload["stopped_vault"]
        self.assertEqual(stopped["id"], stopping["id"])
        self.assertEqual(stopped["uuid"], str(stopping["uuid"]))
        self.assertEqual(stopped["lifecycle"], "stopping")
        self.assertFalse(stopped["root_released"])

    def test_released_vault_reports_when_its_root_was_released(self) -> None:
        released = self._create_vault(lifecycle="stopping")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE vaults SET lifecycle='tombstone', root_released_at=%s WHERE id=%s",
                ("2026-11-01T10:20:30.123456+00:00", released["id"]),
            )
        self.assertEqual(self._me()["stopped_vault"], {
            "id": released["id"],
            "uuid": str(released["uuid"]),
            "name": "Docs Archive",
            "lifecycle": "tombstone",
            "root_released_at": "2026-11-01T10:20:30.123456Z",
            "root_released": True,
        })

    def test_stopped_vault_order_stopping_then_latest_release_then_name(self) -> None:
        """The order once kept for a Session without a Vault (#805)."""
        with self.database.connect() as connection:
            for vault_id, name, lifecycle, released in (
                (1, "beta", "tombstone", "2026-11-02T00:00:00+00:00"),
                (2, "Alpha", "tombstone", "2026-11-02T00:00:00+00:00"),
                (3, "Gamma", "tombstone", "2026-11-03T00:00:00+00:00"),
                (4, "Zeta", "stopping", None),
                (5, "Eta", "stopping", None),
            ):
                seed_vault(connection, id=vault_id, name=name, lifecycle=lifecycle,
                           source_root=f"/sources/managed/{vault_id}", root_released_at=released)
        chosen = []
        for _ in range(5):
            stopped = self._me()["stopped_vault"]
            chosen.append(stopped["id"])
            with self.database.connect() as connection:
                connection.execute("DELETE FROM vaults WHERE id=%s", (stopped["id"],))
        # Stopping before Tombstone; then the latest root release; then the name, without case.
        self.assertEqual(chosen, [5, 4, 3, 2, 1])

    def test_a_stopped_vault_is_not_reported_beside_an_operational_one(self) -> None:
        with self.database.connect() as connection:
            seed_vault(connection, id=1, name="Stopping", lifecycle="stopping",
                       source_root="/sources/managed/1")
            seed_vault(connection, id=2, name="Reconnect", lifecycle="reconnect_required",
                       source_root="/sources/managed/2")
            seed_vault(connection, id=3, name="New", lifecycle="provisioning",
                       source_root="/sources/managed/3")
        self.assertIsNone(self._me()["stopped_vault"])
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='active' WHERE id=2")
        self.assertIsNone(self._me()["stopped_vault"])
        # A Vault still being created is not operational.
        with self.database.connect() as connection:
            connection.execute("DELETE FROM vaults WHERE id=2")
        self.assertEqual(self._me()["stopped_vault"]["id"], 1)

    def test_me_reads_the_vaults_with_one_statement_and_writes_nothing(self) -> None:
        """After the Session lookup: one read of ``vaults``, whatever their state (#805)."""
        self.sign_in()
        session = ["SELECT sessions", "UPDATE sessions"]
        for lifecycle in (None, "active", "stopping"):
            with self.subTest(lifecycle=lifecycle):
                if lifecycle is not None:
                    with self.database.connect() as connection:
                        connection.execute("DELETE FROM vaults")
                        seed_vault(connection, lifecycle=lifecycle)
                with recorded_statements() as statements:
                    response = self.client.get("/api/me")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(statements, [*session, "COMMIT", "SELECT vaults", "COMMIT"])

    def test_me_reports_the_owner_identity(self) -> None:
        """Seam 7: identity and session fields, without multi-user flags."""
        self._create_vault()
        payload = self._me()

        self.assertEqual(payload["id"], 1)
        self.assertNotIn("username", payload)
        self.assertEqual(payload["display_name"], "Owner")
        self.assertNotIn("is_admin", payload)
        self.assertNotIn("active", payload)
        self.assertEqual(payload["session_version"], 1)
        self.assertIsInstance(payload["csrf_token"], str)
        self.assertTrue(payload["csrf_token"])
        self.assertEqual(payload["auth_method"], "oidc")
        self.assertEqual(payload["locale"], "en")
        self.assertEqual(payload["locales"], ["en", "it"])

if __name__ == "__main__":
    unittest.main()
