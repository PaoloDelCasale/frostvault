"""#485: one Vault-row-first lock order for aggregate writers; watcher recovery.

Real PostgreSQL transactions (TEST_POSTGRES_URL, never skipped). The deadlock
test reproduces the production cycle: a scanner-style writer holds the Vault
row and then writes ``directory_aggregates``, while the catalog-revision
publisher used to write the same aggregate rows BEFORE taking the Vault row.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from watchfiles import Change

from app import storage
from app.catalog import ArchiveCatalog
from app.services import directory_aggregates as aggregates
from app.services import notifications, worker_errors
from app.services.catalog_events import record_catalog_revision
from app.services.database_retry import is_retryable_transaction_error
from app.services.problems import report_problem
from tests.postgres import PostgresConnection, database_dsn, run_alembic

STAMP = "2026-11-06T10:00:00+00:00"


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "catalog"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        aggregates.set_maintenance_scheduling_enabled(False)
        self.addCleanup(aggregates.set_maintenance_scheduling_enabled, True)
        with PostgresConnection(self.path) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash)"
                " VALUES (1,'owner','Owner','hash')"
            )
            for vault_id in (2, 3):
                connection.execute(
                    "INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix)"
                    " VALUES (%s,%s,%s,%s,'bucket',%s)",
                    (vault_id, f"v{vault_id}", f"V{vault_id}", f"/tmp/v{vault_id}", f"p{vault_id}"),
                )

    def seed_unflushed(self, vault_id=2, count=3):
        """Committed catalog rows with durable dirty marks and NO aggregates."""
        with PostgresConnection(self.path) as connection:
            catalog = ArchiveCatalog(connection)
            for index in range(count):
                catalog.observe_local_copy(
                    vault_id=vault_id, path=f"moved/dir{index}/leaf/file.bin",
                    file_type="regular", size=4, mtime_ns=1, observed_at=STAMP,
                )

    def row_lock_is_held(self, vault_id):
        """True when ANOTHER transaction holds the Vault row (NOWAIT probe)."""
        with PostgresConnection(self.path) as probe:
            try:
                probe.execute(
                    "SELECT id FROM vaults WHERE id=%s FOR NO KEY UPDATE NOWAIT", (vault_id,)
                )
            except psycopg.errors.LockNotAvailable:
                return True
            return False

    def wait_until_waiting(self, backend_pid, timeout=10):
        with PostgresConnection(self.path) as probe:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                probe.execute("SELECT pg_stat_clear_snapshot()")
                row = probe.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (backend_pid,)
                ).fetchone()
                if row and row["wait_event_type"] == "Lock":
                    return
                time.sleep(0.01)
        raise AssertionError("writer never waited on a lock")


class AggregateVaultLockOrderTests(DatabaseCase):
    def test_flush_with_work_takes_the_vault_row_before_writing_aggregates(self):
        self.seed_unflushed(2)
        with PostgresConnection(self.path) as connection:
            self.assertFalse(self.row_lock_is_held(2))
            aggregates.flush_directory_aggregates(connection, vault_id=2)
            self.assertTrue(self.row_lock_is_held(2))
            self.assertFalse(self.row_lock_is_held(3), "only Vaults with work are locked")
            self.assertGreaterEqual(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM directory_aggregates WHERE vault_id=2"
                ).fetchone()["n"], 3,
            )

    def test_flush_without_work_takes_no_lock(self):
        with PostgresConnection(self.path) as connection:
            aggregates.flush_directory_aggregates(connection, vault_id=2)
            self.assertFalse(self.row_lock_is_held(2))

    def test_request_vault_rebuild_takes_the_vault_row_first(self):
        with PostgresConnection(self.path) as connection:
            aggregates.request_vault_rebuild(connection, 2)
            self.assertTrue(self.row_lock_is_held(2))

    def test_global_flush_locks_every_vault_with_work(self):
        self.seed_unflushed(2)
        self.seed_unflushed(3)
        with PostgresConnection(self.path) as connection:
            aggregates.flush_directory_aggregates(connection)
            self.assertTrue(self.row_lock_is_held(2))
            self.assertTrue(self.row_lock_is_held(3))

    def test_scanner_and_revision_publisher_no_longer_deadlock(self):
        self.seed_unflushed(2, count=4)
        errors: list[BaseException] = []
        scanner_has_vault = threading.Event()
        publisher_waiting = threading.Event()

        def scanner():
            # Scanner order: Vault row first (``_vault_lock_touch``), then the
            # aggregates it flushes at the end of its batch.
            try:
                with PostgresConnection(self.path) as connection:
                    connection.execute("UPDATE vaults SET name=name WHERE id=2")
                    scanner_has_vault.set()
                    self.assertTrue(publisher_waiting.wait(15))
                    aggregates.flush_directory_aggregates(connection, vault_id=2)
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)
                scanner_has_vault.set()

        thread = threading.Thread(target=scanner)
        thread.start()
        self.assertTrue(scanner_has_vault.wait(15))

        def publish():
            # Used to flush (write aggregates) BEFORE its Vault-row lock.
            with PostgresConnection(self.path) as connection:
                pid = connection.info.backend_pid
                watcher = threading.Thread(
                    target=lambda: (self.wait_until_waiting(pid), publisher_waiting.set())
                )
                watcher.start()
                try:
                    record_catalog_revision(connection, vault_id=2, reason="test")
                finally:
                    watcher.join(20)

        try:
            publish()
        finally:
            publisher_waiting.set()
            thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with PostgresConnection(self.path) as connection:
            self.assertGreaterEqual(
                connection.execute("SELECT COUNT(*) AS n FROM directory_aggregates WHERE vault_id=2").fetchone()["n"], 4,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS n FROM catalog_events WHERE vault_id=2").fetchone()["n"], 1,
            )


class WorkerErrorLoggingTests(DatabaseCase):
    def test_exception_with_traceback_reaches_the_server_log(self):
        try:
            raise RuntimeError("boom: connection refused")
        except RuntimeError as error:
            exc = error
        with PostgresConnection(self.path) as connection, \
                self.assertLogs("app.services.worker_errors", level="ERROR") as captured:
            worker_errors.record_worker_error(
                connection, component="source_watcher", exc=exc, vault_id=2, password="SECRET",
            )
        record = captured.records[0]
        self.assertEqual(record.exc_info[1], exc)
        text = "\n".join(captured.output)
        self.assertIn("source_watcher", text)
        self.assertIn("boom: connection refused", text)
        self.assertIn("Traceback", text)
        self.assertNotIn("SECRET", text, "caller detail kwargs must never be logged")

    def test_exception_is_logged_even_when_no_owner_exists_to_notify(self):
        with PostgresConnection(self.path) as connection:
            connection.execute("DELETE FROM users")
            with self.assertLogs("app.services.worker_errors", level="ERROR"):
                worker_errors.record_worker_error(connection, component="startup", exc=RuntimeError("x"))

    def test_report_worker_error_logs_when_the_database_itself_is_down(self):
        exc = RuntimeError("original failure")
        with patch("app.storage.db", side_effect=psycopg.OperationalError("db down")), \
                self.assertLogs("app.services.worker_errors", level="ERROR") as captured:
            storage._report_worker_error("source_watcher", exc, 2)  # must not raise
        text = "\n".join(captured.output)
        self.assertIn("original failure", text)
        self.assertIn("db down", text)
        # The original is logged once, not once per layer.
        self.assertEqual(text.count("original failure"), 1)


class WatcherRecoveryTests(DatabaseCase):
    def open_problem(self, component="source_watcher"):
        with PostgresConnection(self.path) as connection:
            worker_errors.record_worker_error(
                connection, component=component, exc=RuntimeError("x"), vault_id=2,
            )

    def open_problem_count(self):
        with PostgresConnection(self.path) as connection:
            return notifications.count_open_problems(connection, user_id=1)

    def test_retryable_error_classification_covers_deadlock(self):
        self.assertTrue(is_retryable_transaction_error(psycopg.errors.DeadlockDetected()))

    def test_apply_changes_retries_a_deadlock_victim_and_replays_the_same_batch(self):
        calls = []

        def apply(vault, changes):
            calls.append(set(changes))
            if len(calls) < 3:
                raise psycopg.errors.DeadlockDetected("deadlock detected")
            return 7

        changes = {(Change.added, "/x/new")}
        with patch("app.storage.apply_filesystem_changes", side_effect=apply), \
                patch("app.storage.time.sleep"):
            self.assertEqual(storage._apply_watcher_changes({"id": 2}, changes), 7)
        self.assertEqual(calls, [changes] * 3)

    def test_apply_changes_does_not_retry_other_errors(self):
        with patch("app.storage.apply_filesystem_changes", side_effect=RuntimeError("bug")) as apply, \
                patch("app.storage.time.sleep"):
            with self.assertRaises(RuntimeError):
                storage._apply_watcher_changes({"id": 2}, {(Change.added, "/x")})
        self.assertEqual(apply.call_count, 1)

    def test_apply_changes_gives_up_after_the_bounded_attempts(self):
        with patch("app.storage.apply_filesystem_changes",
                   side_effect=psycopg.errors.DeadlockDetected("deadlock detected")) as apply, \
                patch("app.storage.time.sleep"):
            with self.assertRaises(psycopg.errors.DeadlockDetected):
                storage._apply_watcher_changes({"id": 2}, {(Change.added, "/x")})
        self.assertEqual(apply.call_count, storage.WATCHER_APPLY_MAX_ATTEMPTS)

    def test_resolve_worker_error_closes_only_that_components_problem(self):
        self.open_problem("source_watcher")
        self.open_problem("cloud_scan")
        self.assertEqual(self.open_problem_count(), 2)
        with patch("app.storage.db", side_effect=lambda: PostgresConnection(self.path)):
            storage._resolve_worker_error("source_watcher", 2)
        self.assertEqual(self.open_problem_count(), 1)
        with PostgresConnection(self.path) as connection:
            rows = connection.execute(
                "SELECT problem_status FROM notifications WHERE event='internal_error' ORDER BY id"
            ).fetchall()
        self.assertEqual(sorted(row["problem_status"] for row in rows), ["open", "resolved"])

    def test_resolve_worker_error_never_raises_when_the_database_is_unavailable(self):
        with patch("app.storage.db", side_effect=psycopg.OperationalError("down")):
            storage._resolve_worker_error("source_watcher", 2)

    def test_first_successful_watcher_cycle_resolves_the_open_problem(self):
        """The production loop: failure reports; the next healthy cycle closes."""
        self.open_problem("source_watcher")
        self.assertEqual(self.open_problem_count(), 1)
        root = Path(self.tmp.name) / "root"
        root.mkdir()

        async def run():
            cycles = asyncio.Event()

            async def quiet_watch(*args, **kwargs):
                # Timeout ticks: the registration barrier of a healthy observer.
                while True:
                    cycles.set()
                    yield set()
                    await asyncio.sleep(0.01)

            async def direct(function, *args, **kwargs):
                return function(*args, **kwargs)

            with ExitStack() as stack:
                stack.enter_context(patch("app.storage.db", side_effect=lambda: PostgresConnection(self.path)))
                stack.enter_context(patch("app.storage.awatch", side_effect=quiet_watch))
                stack.enter_context(patch("app.storage.asyncio.to_thread", side_effect=direct))
                stack.enter_context(patch("app.storage.source_layout.vault_local_access",
                                          return_value=SimpleNamespace(local_operations_allowed=True)))
                stack.enter_context(patch("app.storage._runtime_settings", return_value=SimpleNamespace(
                    filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=30)))
                stack.enter_context(patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=False)))
                task = asyncio.create_task(storage._watch_vault_filesystem(
                    {"id": 2, "source_root": str(root)}))
                try:
                    await asyncio.wait_for(cycles.wait(), 5)
                    for _ in range(100):
                        if self.open_problem_count() == 0:
                            break
                        await asyncio.sleep(0.05)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
        self.assertEqual(self.open_problem_count(), 0)

    def test_failing_cycle_does_not_resolve_the_problem(self):
        self.open_problem("source_watcher")
        root = Path(self.tmp.name) / "root"
        root.mkdir()
        (root / "new").mkdir()

        async def run():
            async def watch_with_change(*args, **kwargs):
                yield {(Change.modified, str(root / "new"))}
                await asyncio.sleep(3600)

            attempted = asyncio.Event()

            def failing_apply(vault, changes):
                attempted.set()
                raise RuntimeError("catalog write failed")

            async def direct(function, *args, **kwargs):
                return function(*args, **kwargs)

            real_sleep = asyncio.sleep
            with ExitStack() as stack:
                stack.enter_context(patch("app.storage.db", side_effect=lambda: PostgresConnection(self.path)))
                stack.enter_context(patch("app.storage.awatch", side_effect=watch_with_change))
                stack.enter_context(patch("app.storage.asyncio.to_thread", side_effect=direct))
                stack.enter_context(patch("app.storage.apply_filesystem_changes", side_effect=failing_apply))
                stack.enter_context(patch("app.storage.source_layout.vault_local_access",
                                          return_value=SimpleNamespace(local_operations_allowed=True)))
                stack.enter_context(patch("app.storage._runtime_settings", return_value=SimpleNamespace(
                    filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=30)))
                stack.enter_context(patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=True)))
                task = asyncio.create_task(storage._watch_vault_filesystem(
                    {"id": 2, "source_root": str(root)}))
                try:
                    await asyncio.wait_for(attempted.wait(), 5)
                    await real_sleep(0.2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        with self.assertLogs("app.services.worker_errors", level="ERROR"):
            asyncio.run(run())
        self.assertEqual(self.open_problem_count(), 1)


if __name__ == "__main__":
    unittest.main()
