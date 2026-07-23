"""Persistent append-only audit events (issue #16).

Seams under test:
- ``app.services.audit_events.record_audit_event`` / ``list_vault_audit_events``
  / ``list_admin_audit_events`` (public store API)
- Sensitive-field redaction on the recorded detail payload
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.database import SQLiteConnection
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

        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES (%s, 'Owner', %s, FALSE)
                """,
                ("owner", hash_password("owner-password-1")),
            )
            self.owner_id = connection.execute(
                "SELECT id FROM users WHERE username=%s", ("owner",)
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO vaults(
                    name, slug, source_root, s3_bucket, s3_prefix,
                    rclone_remote, enabled
                )
                VALUES ('Archive', 'archive', '/sources/a', 'bucket', 'vaults/a/',
                        'remote', TRUE)
                """
            )
            self.vault_id = connection.execute(
                "SELECT id FROM vaults WHERE slug=%s", ("archive",)
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO vault_members(vault_id, user_id, role)
                VALUES (%s, %s, 'owner')
                """,
                (self.vault_id, self.owner_id),
            )

    def test_recorded_audit_event_is_retrievable_for_the_vault(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            recorded = audit_events.record_audit_event(
                connection,
                event="vault_membership_changed",
                actor_user_id=self.owner_id,
                vault_id=self.vault_id,
                outcome="success",
                correlation_id="corr-1",
                target_user_id=99,
                role="viewer",
            )
            events = audit_events.list_vault_audit_events(
                connection, self.vault_id
            )

        self.assertEqual(recorded["event"], "vault_membership_changed")
        self.assertEqual(recorded["outcome"], "success")
        self.assertEqual(recorded["correlation_id"], "corr-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], recorded["id"])
        self.assertEqual(events[0]["actor_user_id"], self.owner_id)
        detail = events[0]["detail"]
        self.assertEqual(detail["target_user_id"], 99)
        self.assertEqual(detail["role"], "viewer")

    def test_sensitive_fields_are_redacted_in_persisted_detail(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            recorded = audit_events.record_audit_event(
                connection,
                event="break_glass_failed",
                actor_user_id=None,
                vault_id=None,
                outcome="failure",
                password="super-secret-password",
                authorization="Bearer oidc-token-value",
                cookie="session=abc",
                recovery_secret="recovery-material",
                ip="203.0.113.9",
            )
            admin_events = audit_events.list_admin_audit_events(connection)

        self.assertEqual(len(admin_events), 1)
        detail = admin_events[0]["detail"]
        self.assertEqual(detail["password"], "[REDACTED]")
        self.assertEqual(detail["authorization"], "[REDACTED]")
        self.assertEqual(detail["cookie"], "[REDACTED]")
        self.assertEqual(detail["recovery_secret"], "[REDACTED]")
        self.assertEqual(detail["ip"], "203.0.113.9")
        with SQLiteConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT detail_json FROM audit_events WHERE id=%s",
                (recorded["id"],),
            ).fetchone()
        stored = json.loads(row["detail_json"])
        self.assertNotIn("super-secret-password", json.dumps(stored))
        self.assertNotIn("oidc-token-value", json.dumps(stored))


if __name__ == "__main__":
    unittest.main()
