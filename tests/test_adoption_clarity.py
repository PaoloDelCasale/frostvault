"""What the Owner reads while a Vault is adopted (#711).

Scan error causes, internal errors that name their type and close by
themselves, and Problems that say a running scan can close them.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import storage
from app.services import notifications, worker_errors
from app.services.operation_policies import SOURCE_SCAN_INCOMPLETE, SOURCE_WATCHER_DENIED
from app.services import scan_causes
from app.services.scan_causes import source_error_cause
from tests.catalog_support import seed_vault
from tests.owner_fixtures import insert_owner
from tests.postgres import PostgresConnection
from tests.test_database import run_alembic

CAUSES = tuple(code for code, _ in scan_causes._CAUSES)


class SourceErrorCauseTests(unittest.TestCase):
    def test_known_messages_name_their_cause(self) -> None:
        cases = {
            "Source scan blocked: Visited directory changed during traversal: a/b": "tree_changed",
            "Source scan blocked: Start folder changed during traversal: x": "tree_changed",
            "Source scan blocked: Scan root changed during traversal": "tree_changed",
            "Source scan blocked: Vault root changed during local scan": "tree_changed",
            "Source scan blocked: Local scan was superseded": "superseded",
            "Source scan blocked: Vault root identity mismatch; local scan blocked": "root_changed",
            "Source scan blocked: Vault root changed before traversal": "root_changed",
            "Source scan blocked: Scan root is no longer accessible": "root_unavailable",
            "Source scan blocked: Vault root identity is unavailable: [Errno 2]": "root_unavailable",
            SOURCE_SCAN_INCOMPLETE: "incomplete",
            SOURCE_WATCHER_DENIED: "watcher_denied",
        }
        for message, cause in cases.items():
            with self.subTest(message=message):
                self.assertEqual(source_error_cause(message), cause)
                self.assertIn(cause, CAUSES)

    def test_unknown_or_missing_messages_have_no_cause(self) -> None:
        for message in (None, "", "Source scan: boom"):
            self.assertIsNone(source_error_cause(message))

    def test_every_cause_has_its_messages(self) -> None:
        from app.i18n import has_message
        for cause in CAUSES:
            for locale in ("en", "it"):
                with self.subTest(cause=cause, locale=locale):
                    self.assertTrue(has_message(f"vault.scan.cause.{cause}", locale))
                    self.assertTrue(has_message(f"notification.protection_blocked.cause.{cause}", locale))


class ErrorTypeTests(unittest.TestCase):
    def test_names_the_class_and_sqlstate_never_the_message(self) -> None:
        class DiskFull(Exception):
            sqlstate = "53100"
        self.assertEqual(worker_errors.error_type(DiskFull("could not resize /dev/shm SECRET")),
                         "DiskFull, SQLSTATE 53100")
        self.assertEqual(worker_errors.error_type(RuntimeError("SECRET")), "RuntimeError")

        class Odd(Exception):
            sqlstate = "x'; DROP"
        self.assertEqual(worker_errors.error_type(Odd()), "Odd")


class BackgroundLoopRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        with storage._workers_known_ok_lock:
            saved = set(storage._workers_known_ok)
            storage._workers_known_ok.discard(("background_loop", None))
        self.addCleanup(self.restore, saved)

    @staticmethod
    def restore(saved: set) -> None:
        with storage._workers_known_ok_lock:
            storage._workers_known_ok.clear()
            storage._workers_known_ok.update(saved)

    def test_closes_after_clean_cycles_in_a_row_once_per_recovery(self) -> None:
        streak, close = 0, False
        for _ in range(storage.BACKGROUND_LOOP_OK_CYCLES - 1):
            streak, close = storage._background_loop_closes_problem(True, streak)
            self.assertFalse(close)
        # A failed cycle starts the count again.
        streak, close = storage._background_loop_closes_problem(False, streak)
        self.assertEqual((streak, close), (0, False))
        for _ in range(storage.BACKGROUND_LOOP_OK_CYCLES):
            streak, close = storage._background_loop_closes_problem(True, streak)
        self.assertTrue(close)
        with storage._workers_known_ok_lock:
            storage._workers_known_ok.add(("background_loop", None))
        self.assertFalse(storage._background_loop_closes_problem(True, streak)[1])

    def test_a_loop_error_forgets_that_the_loop_was_healthy(self) -> None:
        with storage._workers_known_ok_lock:
            storage._workers_known_ok.add(("background_loop", None))
        with patch.object(storage, "db", side_effect=RuntimeError("no database")):
            storage._record_background_loop_error(RuntimeError("boom"))
        with storage._workers_known_ok_lock:
            self.assertNotIn(("background_loop", None), storage._workers_known_ok)

    def test_directory_aggregates_report_as_their_own_component(self) -> None:
        with (
            patch.object(storage, "process_directory_aggregate_maintenance", side_effect=RuntimeError("x")),
            patch.object(storage, "_report_worker_error") as reported,
            patch.object(storage, "_note_worker_ok") as healthy,
        ):
            storage._run_directory_aggregates()
        self.assertEqual(reported.call_args.args[0], "directory_aggregates")
        healthy.assert_not_called()
        with (
            patch.object(storage, "process_directory_aggregate_maintenance"),
            patch.object(storage, "_note_worker_ok") as healthy,
        ):
            storage._run_directory_aggregates()
        healthy.assert_called_once_with("directory_aggregates")


class ProblemTextTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "problems.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_internal_error_names_its_type_and_closes_by_itself(self) -> None:
        class DiskFull(Exception):
            sqlstate = "53100"
        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection)
            worker_errors.record_worker_error(connection, component="background_loop",
                                              exc=DiskFull("SECRET /dev/shm"), log=False)
            italian = notifications.list_in_app_notifications(connection, locale="it")[0]
            english = notifications.list_in_app_notifications(connection, locale="en")[0]
        self.assertIn("background_loop (DiskFull, SQLSTATE 53100)", italian["body"])
        self.assertIn("si chiude da solo", italian["body"])
        self.assertIn("closes by itself", english["body"])
        self.assertNotIn("SECRET", str(italian) + str(english))

    def test_blocked_protection_says_its_cause_or_keeps_the_generic_advice(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection)
            notifications.set_problem(
                connection, key="blocked:1:source", event="protection_blocked", vault_id=None, active=True,
                params={"component": "source",
                        "cause_key": "notification.protection_blocked.cause.tree_changed"})
            notifications.set_problem(
                connection, key="blocked:1:cloud", event="protection_blocked", vault_id=None, active=True,
                params={"component": "cloud"})
            rows = {row["dedupe_key"]: row for row in
                    notifications.list_in_app_notifications(connection, locale="it")}
        self.assertIn("Una cartella è cambiata mentre la scansione la leggeva", rows["blocked:1:source"]["body"])
        self.assertNotIn("credenziali", rows["blocked:1:source"]["body"])
        self.assertIn("credenziali cloud", rows["blocked:1:cloud"]["body"])

    def test_a_failed_source_scan_records_its_cause(self) -> None:
        def connect():
            return PostgresConnection(str(self.path))
        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection)
            seed_vault(connection, vault_id=7, name="Seven", source_root="/seven")
        with patch.object(storage, "db", side_effect=connect):
            storage._record_scan_component_health(
                7, "source", ok=False,
                error="Source scan blocked: Visited directory changed during traversal: a")
        with PostgresConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT message_params FROM notifications WHERE dedupe_key='blocked:7:source'").fetchone()
        self.assertEqual(row["message_params"]["cause_key"],
                         "notification.protection_blocked.cause.tree_changed")


class ProblemResolvingTests(unittest.TestCase):
    def test_a_running_scan_can_close_a_blocked_source(self) -> None:
        from app.main import _problem_resolving
        blocked = {"event": "protection_blocked", "vault_id": 4, "message_params": {"component": "source"}}
        with patch.dict(storage.runtime_status, {4: {"scanning": True}}):
            self.assertEqual(_problem_resolving(blocked), "scan")
            self.assertIsNone(_problem_resolving({**blocked, "message_params": {"component": "cloud"}}))
            self.assertIsNone(_problem_resolving({**blocked, "event": "internal_error"}))
        with patch.dict(storage.runtime_status, {4: {"scanning": False}}):
            self.assertIsNone(_problem_resolving(blocked))


if __name__ == "__main__":
    unittest.main()
