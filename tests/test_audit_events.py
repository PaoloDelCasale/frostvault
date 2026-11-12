"""Append-only action log and sensitive-field redaction (no read API)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import psycopg

from app.services import audit_events
from tests.owner_fixtures import insert_owner, insert_vault
from tests.postgres import PostgresConnection
from tests.test_database import run_alembic


class AuditEventPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "audit.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection)
            self.vault_id = insert_vault(connection, name="Archive")["id"]

    def test_recorded_audit_event_is_retrievable_for_the_vault(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            recorded = audit_events.record_audit_event(
                connection,
                event="vault_root_relocated",
                actor="owner",
                vault_id=self.vault_id,
                outcome="success",
                correlation_id="corr-1",
                relative_path="photos",
            )
            events = [audit_events._row_to_event(row) for row in connection.execute(
                "SELECT * FROM audit_events WHERE vault_id=%s", (self.vault_id,)
            ).fetchall()]

        self.assertEqual(recorded["event"], "vault_root_relocated")
        self.assertEqual(recorded["outcome"], "success")
        self.assertEqual(recorded["correlation_id"], "corr-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], recorded["id"])
        self.assertEqual(events[0]["actor"], "owner")
        self.assertEqual(events[0]["detail"], {"relative_path": "photos"})

    def test_actor_is_owner_emergency_or_system(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for actor in ("owner", "emergency", "system"):
                audit_events.record_audit_event(
                    connection, event="probe", actor=actor, emit_log=False,
                )
            with self.assertRaises(ValueError):
                audit_events.record_audit_event(
                    connection, event="probe", actor="admin", emit_log=False,
                )
        # The database enforces it too, whatever the caller.
        with PostgresConnection(str(self.path)) as connection:
            with self.assertRaises(psycopg.errors.CheckViolation):
                connection.execute(
                    "INSERT INTO audit_events(event, actor) VALUES ('probe', '1')"
                )
        with PostgresConnection(str(self.path)) as connection:
            columns = {row["column_name"] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='audit_events'"
            ).fetchall()}
        self.assertNotIn("actor_user_id", columns)
        self.assertNotIn("user_id", columns)

    def test_audit_events_are_append_only_but_retention_can_delete(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            recorded = audit_events.record_audit_event(
                connection, event="probe", actor="system", emit_log=False,
            )
        with PostgresConnection(str(self.path)) as connection:
            with self.assertRaises(psycopg.errors.RestrictViolation):
                connection.execute(
                    "UPDATE audit_events SET actor='owner' WHERE id=%s", (recorded["id"],)
                )
        with PostgresConnection(str(self.path)) as connection:
            connection.execute("DELETE FROM audit_events WHERE id=%s", (recorded["id"],))
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM audit_events WHERE id=%s", (recorded["id"],)
            ).fetchone())

    def test_sensitive_fields_are_redacted_in_persisted_detail(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            recorded = audit_events.record_audit_event(
                connection,
                event="break_glass_failed",
                actor="emergency",
                vault_id=None,
                outcome="failure",
                password="super-secret-password",
                authorization="Bearer oidc-token-value",
                cookie="session=abc",
                recovery_secret="recovery-material",
                ip="203.0.113.9",
            )

        detail = recorded["detail"]
        self.assertEqual(detail["password"], "[REDACTED]")
        self.assertEqual(detail["authorization"], "[REDACTED]")
        self.assertEqual(detail["cookie"], "[REDACTED]")
        self.assertEqual(detail["recovery_secret"], "[REDACTED]")
        self.assertEqual(detail["ip"], "203.0.113.9")
        with PostgresConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT detail FROM audit_events WHERE id=%s",
                (recorded["id"],),
            ).fetchone()
        stored = json.dumps(row["detail"])
        self.assertNotIn("super-secret-password", stored)
        self.assertNotIn("oidc-token-value", stored)


if __name__ == "__main__":
    unittest.main()
