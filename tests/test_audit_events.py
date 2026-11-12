"""Append-only action log and sensitive-field redaction (no read API)."""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import json
import tempfile
import unittest
from pathlib import Path


from app.security import hash_password
from app.services import audit_events
from tests.test_database import run_alembic


class AuditEventPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "audit.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', %s)
                """,
                (hash_password("owner-password-1"),),
            )
            self.owner_id = connection.execute(
                "SELECT id FROM owner WHERE id=1"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO vaults(
                    name, slug, source_root, s3_bucket, s3_prefix, enabled
                )
                VALUES ('Archive', 'archive', '/sources/a', 'bucket', 'vaults/a/', TRUE)
                """
            )
            self.vault_id = connection.execute(
                "SELECT id FROM vaults WHERE slug=%s", ("archive",)
            ).fetchone()["id"]

    def test_recorded_audit_event_is_retrievable_for_the_vault(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            recorded = audit_events.record_audit_event(
                connection,
                event="vault_root_relocated",
                actor="owner",
                vault_id=self.vault_id,
                outcome="success",
                correlation_id="corr-1",
                target_user_id=99,
                role="viewer",
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
        detail = events[0]["detail"]
        self.assertEqual(detail["target_user_id"], 99)
        self.assertEqual(detail["role"], "viewer")

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
            admin_events = [recorded]

        self.assertEqual(len(admin_events), 1)
        detail = admin_events[0]["detail"]
        self.assertEqual(detail["password"], "[REDACTED]")
        self.assertEqual(detail["authorization"], "[REDACTED]")
        self.assertEqual(detail["cookie"], "[REDACTED]")
        self.assertEqual(detail["recovery_secret"], "[REDACTED]")
        self.assertEqual(detail["ip"], "203.0.113.9")
        with PostgresConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT detail_json FROM audit_events WHERE id=%s",
                (recorded["id"],),
            ).fetchone()
        stored = json.loads(row["detail_json"])
        self.assertNotIn("super-secret-password", json.dumps(stored))
        self.assertNotIn("oidc-token-value", json.dumps(stored))


if __name__ == "__main__":
    unittest.main()
