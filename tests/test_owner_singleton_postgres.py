"""Real PostgreSQL singleton/security contracts; run in PR CI."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch

import psycopg

from app import oidc, sessions
from app.config import settings
from app.services import audit_events, notifications
from tests.postgres import PostgresConnection, run_alembic


class OwnerCheckpointMigrationTests(TestCase):
    """Temporary graph coverage, separate from the final baseline contracts."""

    def test_checkpoint_retains_bound_identity_session_and_audit_semantics(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint"
            result = run_alembic(path, "0072_upload_lineage_indexes")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(path) as connection:
                connection.execute("""INSERT INTO users(id,username,display_name,password_hash,session_version)
                    VALUES (7,'old-emergency-name','Owner','password-hash',9)""")
                connection.execute("""INSERT INTO user_identities(user_id,issuer,subject,created_at)
                    VALUES (7,'https://id.example','bound-subject','2026-11-12')""")
                connection.execute("""INSERT INTO sessions(id,user_id,token_hash,auth_method,csrf_token,session_version,
                    created_at,last_seen_at,idle_expires_at,absolute_expires_at)
                    VALUES ('session',7,'token-hash','oidc','csrf',9,'2026-11-12','2026-11-12','2099-01-01','2099-01-01')""")
                connection.execute("""INSERT INTO notifications(user_id,event,title,dedupe_key,created_at)
                    VALUES (7,'test','Owner notification','same','2026-11-12')""")
                for event, actor_id in (("owner_action", 7), ("break_glass_failed", None), ("worker_action", None)):
                    connection.execute("""INSERT INTO audit_events(created_at,event,actor_user_id,detail_json)
                        VALUES ('2026-11-12',%s,%s,'{}')""", (event, actor_id))
            result = run_alembic(path, "0074_owner_singleton")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(path) as connection:
                self.assertEqual(connection.execute("SELECT * FROM owner").fetchone(), {
                    "id": 1, "display_name": "Owner", "password_hash": "password-hash", "session_version": 9,
                    "issuer": "https://id.example", "subject": "bound-subject"})
                self.assertEqual(connection.execute("SELECT token_hash,csrf_token,session_version FROM sessions").fetchone(),
                                 {"token_hash": "token-hash", "csrf_token": "csrf", "session_version": 9})
                self.assertEqual(connection.execute("SELECT title,dedupe_key FROM notifications").fetchone(),
                                 {"title": "Owner notification", "dedupe_key": "same"})
                self.assertEqual([r["actor"] for r in connection.execute("SELECT actor FROM audit_events ORDER BY id")],
                                 ["owner", "emergency", "system"])


class OwnerSingletonPostgresTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "owner"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(self.path) as connection:
            connection.execute("INSERT INTO owner(id,display_name,password_hash) VALUES (1,'Owner','hash')")

    def test_schema_has_no_multiuser_tables_columns_or_redundant_authorship(self):
        with PostgresConnection(self.path) as connection:
            tables = {r["table_name"] for r in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
            self.assertTrue({"users", "user_identities"}.isdisjoint(tables))
            forbidden = connection.execute("""SELECT table_name,column_name FROM information_schema.columns
                WHERE table_schema='public' AND column_name IN ('user_id','requested_by','approved_by','actor_user_id')""").fetchall()
            self.assertEqual(forbidden, [])
            columns = {r["column_name"] for r in connection.execute("""SELECT column_name
                FROM information_schema.columns WHERE table_schema='public' AND table_name='owner'""")}
            self.assertEqual(columns, {"id", "display_name", "password_hash", "session_version", "issuer", "subject"})
            self.assertEqual(connection.execute("""SELECT data_type FROM information_schema.columns
                WHERE table_schema='public' AND table_name='owner' AND column_name='id'""").fetchone()["data_type"], "smallint")
            job_columns = {r["column_name"] for r in connection.execute("""SELECT column_name
                FROM information_schema.columns WHERE table_schema='public' AND table_name='jobs'""")}
            self.assertTrue({"origin", "approved_at"} <= job_columns)
            for query in (
                "INSERT INTO owner(id,display_name) VALUES (2,'Intruder')",
                "UPDATE owner SET id=2",
                "UPDATE owner SET issuer='https://id.example'",
                "UPDATE owner SET subject='subject'",
                "INSERT INTO audit_events(created_at,event,actor,detail_json) VALUES ('2026-11-12','test','foreign','{}')",
            ):
                with self.assertRaises(psycopg.IntegrityError), connection.transaction():
                    connection.execute(query)

    def test_first_login_race_binds_exactly_one_identity(self):
        barrier = Barrier(2)
        configured = replace(settings, oidc_issuer="https://id.example")

        def login(subject):
            with PostgresConnection(self.path) as connection:
                barrier.wait(timeout=10)
                try:
                    oidc.bind_owner_identity(connection, oidc.OidcClaims(
                        issuer=configured.oidc_issuer, subject=subject, return_to=None, raw={}))
                except oidc.IdentityRejected:
                    return "denied"
                return "bound"

        with patch.object(oidc, "settings", configured), ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(login, ("one", "two")))
        self.assertCountEqual(outcomes, ["bound", "denied"])
        with PostgresConnection(self.path) as connection:
            bound = connection.execute("SELECT issuer,subject FROM owner WHERE id=1").fetchone()
        self.assertEqual(bound["issuer"], configured.oidc_issuer)
        self.assertIn(bound["subject"], {"one", "two"})

    def test_password_version_revokes_sessions_csrf_and_stream_authority(self):
        from app.services.catalog_event_stream import session_stream_state
        from tests.postgres import database_dsn
        with PostgresConnection(self.path) as connection:
            token = sessions.create_session(connection, user_id=1, auth_method="oidc")
            resolved = sessions.resolve_session(connection, token)
            self.assertIsNotNone(sessions.csrf_token_for(connection, token))
            connection.execute("UPDATE owner SET session_version=session_version+1 WHERE id=1")
        with PostgresConnection(self.path) as connection:
            self.assertIsNone(sessions.resolve_session(connection, token))
            self.assertIsNone(sessions.csrf_token_for(connection, token))
        with patch("app.database.settings", replace(settings, database_dsn=database_dsn(self.path))):
            row = session_stream_state(resolved["id"])
        self.assertIsNone(row, "Stream authority must reject the revoked session, not expose mismatched versions")

    def test_bootstrap_reapplies_current_emergency_password_without_rebinding_oidc(self):
        from app import database
        from app.security import hash_password, verify_password
        configured = replace(settings, bootstrap_admin_username="current-emergency-name",
                             bootstrap_admin_password="current-emergency-password", bootstrap_admin_display_name="Current Owner")
        with PostgresConnection(self.path) as connection:
            connection.execute("UPDATE owner SET password_hash=%s,issuer='https://id.example',subject='bound' WHERE id=1",
                               (hash_password("previous-emergency-password"),))
            token = sessions.create_session(connection, user_id=1, auth_method="oidc")
            previous = connection.execute("SELECT session_version FROM owner WHERE id=1").fetchone()["session_version"]
            with patch.object(database, "settings", configured):
                database._bootstrap_owner(connection)
                database._bootstrap_owner(connection)  # idempotent restart
            owner = connection.execute("SELECT * FROM owner WHERE id=1").fetchone()
            self.assertEqual(owner["display_name"], "Current Owner")
            self.assertEqual((owner["issuer"], owner["subject"]), ("https://id.example", "bound"))
            self.assertEqual(owner["session_version"], previous + 1)
            self.assertTrue(verify_password(owner["password_hash"], configured.bootstrap_admin_password))
            self.assertFalse(verify_password(owner["password_hash"], "previous-emergency-password"))
            self.assertIsNone(sessions.resolve_session(connection, token))
            self.assertIsNone(sessions.csrf_token_for(connection, token))

    def test_global_notification_deduplication_and_audit_principals(self):
        with PostgresConnection(self.path) as connection:
            first = notifications.enqueue_notification(connection, user_id=1, event="test", title="one", dedupe_key="same")
            second = notifications.enqueue_notification(connection, user_id=1, event="test", title="two", dedupe_key="same")
            self.assertEqual(first["id"], second["id"])
            for actor in ("owner", "emergency", "system"):
                record = audit_events.record_audit_event(connection, event="test", actor=actor, password="not-persisted")
                self.assertEqual(record["actor"], actor)
                self.assertEqual(record["detail"]["password"], "[REDACTED]")

    def test_populated_owner_catalog_cannot_be_downgraded(self):
        result = run_alembic(self.path, "0072_upload_lineage_indexes", "downgrade")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible Current Snapshot baseline cannot be downgraded", result.stderr)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT display_name FROM owner WHERE id=1").fetchone()["display_name"], "Owner")
