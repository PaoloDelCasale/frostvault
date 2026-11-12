"""Database cost of one small upload, pinned so it cannot silently grow back.

A bulk upload of small files is bounded by what one file costs the catalog, not
by the network. These tests run one real upload through admission, claim and
``process_job`` on disposable PostgreSQL 16 and count what it asked the database.

TEST_POSTGRES_URL is mandatory; never skipped locally as database evidence.
"""
import asyncio
import json
import re
import unittest
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from app import storage
from app.catalog import ArchiveCatalog
from tests import test_upload_provenance_worker as fixture
from tests.postgres import PostgresConnection

# Tables that grow with the history of a Vault. A statement of the upload path
# that cannot be served by an index on one of these reads all of it per file.
HISTORY_TABLES = frozenset({
    "jobs", "file_paths", "vault_files", "local_copies", "archive_versions",
    "upload_intents", "upload_intent_jobs", "upload_receipts",
    "snapshot_cleanup_inventories", "upload_remediations",
})
# Measured: 5 exclusive Vault row locks and 1 lease extension per upload (10 and
# 16 before this change). The second extension allows for one keeper beat.
MAX_EXCLUSIVE_VAULT_LOCKS = 5
MAX_LEASE_EXTENSIONS = 2
_VAULT_LOCK = re.compile(r"FROM\s+vaults\b.*?\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|SHARE|KEY\s+SHARE)", re.I | re.S)
_EXPLAINABLE = re.compile(r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE)\b", re.I)


def _whole_relation_reads(plan, found):
    """Relations read in full: a sequential scan, or an index walked without a condition."""
    kind = plan.get("Node Type")
    if kind == "Seq Scan" or (kind in ("Index Scan", "Index Only Scan") and not plan.get("Index Cond")):
        found.add(plan.get("Relation Name"))
    for child in plan.get("Plans", ()):
        _whole_relation_reads(child, found)


class SmallUploadCostTests(unittest.TestCase):
    setUp = fixture.UploadProvenanceWorkerTests.setUp
    queue = fixture.UploadProvenanceWorkerTests.queue
    run_job = fixture.UploadProvenanceWorkerTests.run_job
    row = fixture.UploadProvenanceWorkerTests.row

    def upload(self):
        """Run one upload; return its statements and its lease extensions."""
        job_id = self.queue()
        statements = []
        execute = psycopg.Cursor.execute

        def record(cursor, query, params=None, **kwargs):
            if isinstance(query, str):
                statements.append((query, params))
                self.transactions.setdefault(id(cursor.connection), [[]])[-1].append(query)
            return execute(cursor, query, params, **kwargs)

        def boundary(original):
            def ended(connection, *args, **kwargs):
                self.transactions.setdefault(id(connection), [[]]).append([])
                return original(connection, *args, **kwargs)
            return ended

        self.transactions = {}
        with patch.object(psycopg.Cursor, "execute", record), patch.object(
            psycopg.Connection, "commit", boundary(psycopg.Connection.commit),
        ), patch.object(
            psycopg.Connection, "rollback", boundary(psycopg.Connection.rollback),
        ), patch.object(
            storage, "_renew_claim_if_owned", wraps=storage._renew_claim_if_owned,
        ) as renew:
            self.run_job(job_id)
        self.assertEqual(self.row(job_id)["status"], "completed", self.row(job_id)["message"])
        return statements, renew.call_count

    def test_one_upload_takes_a_bounded_number_of_exclusive_vault_locks(self):
        statements, _ = self.upload()
        modes = [" ".join(match.group(1).upper().split())
                 for query, _ in statements if (match := _VAULT_LOCK.search(query))]
        exclusive = [mode for mode in modes if mode in ("NO KEY UPDATE", "UPDATE")]
        self.assertTrue(exclusive, "The probe no longer sees Vault row locks")
        self.assertLessEqual(len(exclusive), MAX_EXCLUSIVE_VAULT_LOCKS, modes)

    def test_a_unit_that_shares_the_vault_lock_never_asks_for_more(self):
        """Two shared holders that both upgrade deadlock (ADR-0031)."""
        self.upload()
        shared = 0
        for transactions in self.transactions.values():
            for queries in transactions:
                modes = [" ".join(match.group(1).upper().split()) for query in queries
                         if (match := _VAULT_LOCK.search(query))]
                if "SHARE" not in modes:
                    continue
                shared += 1
                after = queries[next(i for i, query in enumerate(queries) if _VAULT_LOCK.search(query)
                                     and "SHARE" == " ".join(_VAULT_LOCK.search(query).group(1).upper().split())):]
                text = "\n".join(after)
                self.assertNotIn("NO KEY UPDATE", modes[modes.index("SHARE"):], text)
                self.assertNotIn("UPDATE", modes[modes.index("SHARE"):], text)
                for forbidden in ("UPDATE vaults", "INSERT INTO catalog_events", "INSERT INTO vault_files",
                                  "INTO directory_aggregates", "directory_aggregate_status"):
                    self.assertNotIn(forbidden, text)
        self.assertGreaterEqual(shared, 5, "The probe no longer sees shared units")

    def test_one_upload_does_not_extend_its_lease_at_every_checkpoint(self):
        _, extensions = self.upload()
        self.assertGreaterEqual(extensions, 1, "The first checkpoint must extend the lease")
        self.assertLessEqual(extensions, MAX_LEASE_EXTENSIONS)

    def test_every_statement_of_an_upload_can_use_an_index_on_history_tables(self):
        statements, _ = self.upload()
        unindexed = {}
        with PostgresConnection(self.db_path) as connection:
            # With sequential scans priced out, one remains only where no index
            # can serve the statement: independent of table size and statistics.
            connection.execute("SET enable_seqscan=off")
            for query, params in dict.fromkeys(
                (query, json.dumps(params, default=str)) for query, params in statements
            ):
                if not _EXPLAINABLE.match(query):
                    continue
                original = next(p for q, p in statements if q == query and json.dumps(p, default=str) == params)
                plan = connection.execute("EXPLAIN (FORMAT JSON) " + query, original).fetchone()["QUERY PLAN"]
                found = set()
                _whole_relation_reads(plan[0]["Plan"], found)
                for table in found & HISTORY_TABLES:
                    unindexed.setdefault(table, " ".join(query.split())[:160])
        self.assertEqual(unindexed, {})

    def test_directory_rollups_reach_current_paths_by_prefix_index(self):
        from app.services import directory_aggregates
        from app.services.operation_policies import get_policy
        with PostgresConnection(self.db_path) as connection:
            # The real rollup SELECT: it filters the Vault on vault_files and
            # reaches file_paths only through the directory prefix.
            select_sql, extra = directory_aggregates._visible_file_select_sql(
                policy=get_policy(connection, 2), path_prefix="photos")
            connection.execute("SET enable_seqscan=off")
            plan = connection.execute("EXPLAIN (FORMAT JSON) " + select_sql, (2, *extra)).fetchone()["QUERY PLAN"]
        found = set()
        _whole_relation_reads(plan[0]["Plan"], found)
        self.assertNotIn("file_paths", found, plan)
        # The catalog database uses a linguistic collation: only an index under
        # text_pattern_ops can turn the prefix into an index range.
        self.assertIn("path ~>=~ 'photos/'", json.dumps(plan), "The prefix must be an index range, not a filter")


class ClaimCheckpointTests(unittest.TestCase):
    setUp = fixture.UploadProvenanceWorkerTests.setUp
    queue = fixture.UploadProvenanceWorkerTests.queue

    def claim(self):
        job_id = self.queue()
        with PostgresConnection(self.db_path) as connection:
            claimed = ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso())
        self.assertIsNotNone(claimed)
        return claimed

    def test_checkpoints_inside_the_renew_window_read_without_writing(self):
        job = self.claim()
        with storage._job_claim_context(job), patch.object(
            storage, "_renew_claim_if_owned", wraps=storage._renew_claim_if_owned,
        ) as renew:
            for _ in range(5):
                storage.ensure_job_active(job["id"], "stopped")
            self.assertEqual(renew.call_count, 1)
            with patch.object(storage, "CLAIM_CHECKPOINT_RENEW_SECONDS", 0.0):
                storage.ensure_job_active(job["id"], "stopped")
            self.assertEqual(renew.call_count, 2)

    def test_a_read_only_checkpoint_still_observes_cancellation_and_takeover(self):
        for change in ("UPDATE jobs SET status='cancelled' WHERE id=%s",
                       "UPDATE jobs SET claim_token='another-worker' WHERE id=%s",
                       "UPDATE jobs SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id=%s"):
            with self.subTest(change=change):
                job = self.claim()
                with storage._job_claim_context(job):
                    storage.ensure_job_active(job["id"], "stopped")
                    with PostgresConnection(self.db_path) as connection:
                        connection.execute(change, (job["id"],))
                    with patch.object(storage, "_renew_claim_if_owned") as renew:
                        with self.assertRaises((storage.JobLeaseLost, storage.OperationCancelled)):
                            storage.ensure_job_active(job["id"], "stopped")
                    renew.assert_not_called()
                with PostgresConnection(self.db_path) as connection:
                    connection.execute("UPDATE jobs SET status='cancelled',claim_token=NULL WHERE id=%s", (job["id"],))


class BacklogObservationTests(unittest.TestCase):
    """The backlog gauges read the whole queue: a deep queue is sampled."""

    def cycles(self, depth, count=6):
        runtime = SimpleNamespace(
            operation_concurrency=4, queue_poll_interval=2, scan_interval=10**9,
            audit_interval=10**9, metadata_backup_interval_seconds=0,
            metadata_backup_verify_interval_seconds=0)
        seen = 0

        async def refill(wakeup, delay):
            nonlocal seen
            seen += 1
            if seen >= count:
                raise asyncio.CancelledError

        async def run():
            with ExitStack() as stack:
                for owner, name in (
                    (storage.source_layout, "verify_mounts_once"), (storage, "process_jobs_once"),
                    (storage, "_deliver_notifications_once"), (storage, "process_directory_aggregate_maintenance"),
                    (storage.protection_stop_service, "reconcile_all"), (storage, "_submit_maintenance"),
                ):
                    stack.enter_context(patch.object(owner, name))
                stack.enter_context(patch.object(storage, "persisted_maintenance_ages", return_value={}))
                stack.enter_context(patch.object(storage, "_runtime_settings", return_value=runtime))
                stack.enter_context(patch.object(storage, "observe_stuck_lease_jobs", return_value=(0, 0.0)))
                stack.enter_context(patch.object(storage, "observe_failed_job_backlog", return_value=(0, 0.0)))
                stack.enter_context(patch.object(storage, "_refill_until_deadline", refill))
                depth_reads = stack.enter_context(patch.object(storage, "claimable_queue_depth", return_value=depth))
                stall = stack.enter_context(patch.object(storage, "observe_queue_stall"))
                with self.assertRaises(asyncio.CancelledError):
                    await storage._background_loop(asyncio.get_running_loop(), asyncio.Event())
                return depth_reads.call_count, stall.call_count

        return asyncio.run(run())

    def test_a_short_queue_is_counted_every_cycle(self):
        self.assertEqual(self.cycles(depth=39), (12, 6))

    def test_a_deep_queue_is_sampled_not_recounted_every_cycle(self):
        self.assertEqual(self.cycles(depth=40), (2, 1))
        with patch.object(storage, "BACKLOG_OBSERVE_SECONDS", 0.0):
            self.assertEqual(self.cycles(depth=40), (12, 6))


if __name__ == "__main__":
    unittest.main()
