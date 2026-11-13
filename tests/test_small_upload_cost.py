"""Database budget of one small upload (#597, ADR-0035).

A bulk upload of small files is bounded by what one file costs the catalog, not
by the network. These tests run one real upload on disposable PostgreSQL 16:
admission through the API's ``queue_jobs``, the scheduler's claim, then
``storage.process_job`` with its S3 readiness gate, verification and the exact
cleanup, up to the completed Job. Only the S3 transport is synthetic
(``VersionedProvider``, as in ``tests.test_upload_worker``).

What is counted, on every connection the run uses:

- a transaction is one unit of work: an explicit transaction (psycopg's
  implicit BEGIN through COMMIT/ROLLBACK) or an application statement run in
  autocommit;
- a statement is every SQL statement sent, including the pool's own two per
  checkout (the liveness check and the ``DISCARD ALL`` reset), which are
  counted as statements of the unit they serve;
- lease renewals are the ``UPDATE job_leases`` actually executed (fenced units
  extend the lease too), and they are inside the counts above.

The budget is 14 transactions and 110 statements for a small file in read-back
mode (105 at the Vault root: a file in a directory journals its dirty
directories in five units), 13 and 99 with an S3 checksum at the root (#622;
#597 set 15 and 120), and the same cost with much accumulated history. A new key is listed once, after its PUT
(ADR-0038).

TEST_POSTGRES_URL is mandatory; never skipped locally as database evidence.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from psycopg.pq import TransactionStatus

from app import storage
from app.catalog import ArchiveCatalog
from app.services import cloud_preflight_gate
from tests import test_upload_worker as worker_fixture
from tests.postgres import PostgresConnection

MAX_TRANSACTIONS = 14
MAX_STATEMENTS = 110
# A root file with an S3 checksum: no read-back unit, no dirty directory.
CHECKSUM_TRANSACTIONS = 13
CHECKSUM_STATEMENTS = 99
# Every per-file statement on these tables must be servable by an index: they
# grow with the history of a Vault. ``job_leases`` is not one: a row exists only
# while a worker holds its Job (``jobs_end`` deletes it).
HISTORY_TABLES = frozenset({
    "jobs", "job_uploads", "job_groups", "files", "file_path_history",
    "archive_versions", "upload_snapshots", "upload_attempts", "upload_receipts",
    "upload_multiparts", "cleanup_batches", "cleanup_candidates", "upload_remediations",
    "upload_remediation_jobs", "notifications", "job_terminal_notifications",
    "audit_events", "catalog_events", "copy_attempts",
})
# Predicates (as pg_get_expr prints them) of partial indexes holding only open rows.
_OPEN_ONLY = re.compile(
    r"status <> ALL \(ARRAY\['completed'::text, 'failed'::text, 'cancelled'::text\]\)"
    r"|problem_status = 'open'::text|closed_at IS NULL")
# The real gate, captured before the fixture patches it out.
_VALIDATE_CLOUD_VAULT = storage.validate_cloud_vault
_POOL_STATEMENTS = frozenset({"", "DISCARD ALL"})
_VAULT_LOCK = re.compile(r"FROM\s+vaults\b.*?\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|SHARE|KEY\s+SHARE)", re.I | re.S)
_EXPLAINABLE = re.compile(r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE)\b", re.I)


@dataclass
class Cost:
    """What one upload asked the database, in order."""

    statements: list[tuple[int, str, object]] = field(default_factory=list)  # (transaction, sql, params)
    transactions: int = 0
    pool: int = 0

    @property
    def lease_renewals(self) -> int:
        return sum(1 for _, sql, _ in self.statements if re.search(r"UPDATE\s+job_leases\b", sql))

    def units(self) -> dict[int, list[str]]:
        grouped: dict[int, list[str]] = {}
        for transaction, sql, _ in self.statements:
            grouped.setdefault(transaction, []).append(sql)
        return grouped

    def shape(self) -> list[str]:
        """The statements, normalized: the same shape is the same cost."""
        return [" ".join(sql.split()) for _, sql, _ in self.statements]

    def report(self) -> str:
        lines = [f"{self.transactions} transactions, {len(self.statements)} statements "
                 f"({self.pool} by the pool), {self.lease_renewals} lease renewals"]
        lines += [f"  {transaction:>2} {' '.join(sql.split())[:120]}" for transaction, sql, _ in self.statements]
        return "\n".join(lines)


@contextmanager
def counted():
    """Count server transactions and statements on every psycopg connection."""
    cost = Cost()
    open_transaction: dict[int, int] = {}
    execute = psycopg.Cursor.execute

    def record(cursor, query, params=None, **kwargs):
        connection = cursor.connection
        sql = query if isinstance(query, str) else str(query)
        if connection.autocommit and sql.strip() in _POOL_STATEMENTS:
            # The pool's per-checkout liveness check and reset: statements of
            # the unit they serve, not units of work of their own.
            cost.pool += 1
        elif connection.autocommit or connection.info.transaction_status == TransactionStatus.IDLE:
            cost.transactions += 1
            open_transaction[id(connection)] = cost.transactions
        cost.statements.append((open_transaction.get(id(connection), 0), sql, params))
        return execute(cursor, query, params, **kwargs)

    def record_many(cursor, query, params_seq, **kwargs):
        raise AssertionError("executemany is not expected on the upload path; count it explicitly")

    with patch.object(psycopg.Cursor, "execute", record), \
            patch.object(psycopg.Cursor, "executemany", record_many):
        yield cost


class SmallUploadCostTests(unittest.TestCase):
    setUp = worker_fixture.UploadWorkerTests.setUp
    db = worker_fixture.UploadWorkerTests.db
    observe = worker_fixture.UploadWorkerTests.observe
    rows = worker_fixture.UploadWorkerTests.rows
    job = worker_fixture.UploadWorkerTests.job
    file_row = worker_fixture.UploadWorkerTests.file_row
    version = worker_fixture.UploadWorkerTests.version
    assert_current = worker_fixture.UploadWorkerTests.assert_current
    claim = worker_fixture.UploadWorkerTests.claim

    # -- one measured upload --------------------------------------------------

    def ready_gate(self) -> None:
        """S3 readiness already confirmed by this process: the gate only reads."""
        with self.db() as connection:
            connection.execute(
                "INSERT INTO cloud_preflight_state(id, status, next_probe_at) "
                "VALUES (1, 'ready', now() + interval '50 seconds') "
                "ON CONFLICT (id) DO UPDATE SET status='ready', next_probe_at=EXCLUDED.next_probe_at")
        self.enterContext(patch.object(cloud_preflight_gate, "_ready_pid", os.getpid()))
        self.enterContext(patch.object(storage, "validate_cloud_vault", _VALIDATE_CLOUD_VAULT))

    def upload(self, path: str = "report.txt", *, mode: str | None = None) -> Cost:
        """Admit, claim and run one upload of ``path``; return its whole cost."""
        from app import main
        if mode is not None:
            with self.db() as connection:
                connection.execute(
                    "INSERT INTO vault_operation_policies(vault_id, upload_verification) VALUES (2, %s) "
                    "ON CONFLICT (vault_id) DO UPDATE SET upload_verification=EXCLUDED.upload_verification",
                    (mode,))
        with counted() as cost:
            main.queue_jobs(path, "upload", 2, 1)
        job_id = self.rows("SELECT j.id FROM jobs j JOIN files f ON f.id=j.file_id "
                           "WHERE f.path=%s AND j.status='queued'", path)[0]["id"]
        # The scheduler hands the worker the candidate row it read for the
        # whole batch (``list_claimable_jobs``); only the claim is per file.
        candidate = self.rows(
            """SELECT j.*, f.path, v.source_root, v.uuid AS vault_uuid, v.name AS vault_name,
                      v.lifecycle AS vault_lifecycle, t.bucket AS s3_bucket
               FROM jobs j JOIN files f ON f.id=j.file_id JOIN vaults v ON v.id=j.vault_id
               JOIN vault_targets t ON t.id=v.target_id WHERE j.id=%s""", job_id)[0]
        with counted() as run:
            with storage.db() as connection:
                claimed = ArchiveCatalog(connection).claim_job(
                    job_id=job_id, claim_token=str(uuid.uuid4()), claimed_at=storage.now_iso(),
                    claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                    restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed, "the Job must be claimable")
            storage.process_job({**candidate, "claim_token": claimed["claim_token"],
                                 "claim_expires_at": claimed["claim_expires_at"]})
        offset = cost.transactions
        cost.statements += [(transaction + offset, sql, params) for transaction, sql, params in run.statements]
        cost.transactions += run.transactions
        cost.pool += run.pool
        job = self.job(job_id)
        self.assertEqual(job["status"], "completed", job["message"])
        return cost

    def assert_within_budget(self, cost: Cost) -> None:
        self.assertLessEqual(cost.transactions, MAX_TRANSACTIONS, cost.report())
        self.assertLessEqual(len(cost.statements), MAX_STATEMENTS, cost.report())

    # -- the budget -----------------------------------------------------------

    def test_a_small_s3_checksum_upload_stays_within_budget(self) -> None:
        self.ready_gate()
        cost = self.upload(mode="s3_checksum")
        version = self.assert_current(self.job_id(), worker_fixture.A)
        self.assertEqual(version["verification_method"], "s3_checksum")
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 0))
        self.assert_within_budget(cost)
        self.assertLessEqual(cost.transactions, CHECKSUM_TRANSACTIONS, cost.report())
        self.assertLessEqual(len(cost.statements), CHECKSUM_STATEMENTS, cost.report())
        self.assertGreaterEqual(cost.pool, 2 * cost.transactions,
                                "every DB unit is a pool checkout: check and reset are counted")
        self.assertGreaterEqual(cost.lease_renewals, 1, "the run extends its lease")

    def test_a_small_read_back_upload_stays_within_budget(self) -> None:
        self.ready_gate()
        cost = self.upload(mode="read_back")
        version = self.assert_current(self.job_id(), worker_fixture.A)
        self.assertEqual((version["verification_method"], version["verification_egress_bytes"]),
                         ("read_back", len(worker_fixture.A)))
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 1))
        self.assert_within_budget(cost)

    def test_accumulated_history_does_not_change_the_cost(self) -> None:
        self.ready_gate()
        self.new_file("history/fresh-before.txt")
        fresh = self.upload("history/fresh-before.txt", mode="read_back")
        self.seed_history()
        self.new_file("history/fresh-after.txt")
        after = self.upload("history/fresh-after.txt")
        self.assertEqual((fresh.transactions, len(fresh.statements), fresh.lease_renewals),
                         (after.transactions, len(after.statements), after.lease_renewals),
                         after.report())
        self.assertEqual(fresh.shape(), after.shape())
        self.assert_within_budget(after)
        self.assert_indexed(after)

    # -- what the budget must not hide ---------------------------------------

    # -- what the provider is asked (#622, ADR-0038) ------------------------

    def listings_by_phase(self) -> list[str]:
        """Record each ListObjectVersions as before or after this run's PUT."""
        phases: list[str] = []
        previous = self.provider.hook

        def hook(stage, **kwargs):
            previous(stage, **kwargs)
            if stage == "before_put":
                phases.append("put")
            elif stage == "list":
                phases.append("list")
        self.provider.hook = hook
        return phases

    def test_a_new_key_is_never_listed_before_its_put(self) -> None:
        self.ready_gate()
        phases = self.listings_by_phase()
        self.upload()
        # No inventory before the PUT; one listing proves the keeper alone.
        self.assertEqual(phases, ["put", "list"])
        attempt = self.rows("SELECT inventory_versions, inventory_markers FROM upload_attempts")[0]
        self.assertEqual((list(attempt["inventory_versions"]), list(attempt["inventory_markers"])),
                         ([], []))

    def test_an_unknown_object_at_a_new_key_still_blocks_the_first_completion(self) -> None:
        self.ready_gate()
        key = f"vaults/{self.uuid}/report.txt"
        stranger = self.provider.add(key, b"written outside FrostVault")
        phases = self.listings_by_phase()
        with self.assertRaises(AssertionError):
            self.upload()  # The Job does not complete.
        self.assertEqual(phases, ["put", "list"])
        self.assertEqual(self.file_row()["reconciliation_block"], "inventory_changed")
        self.assertIn((key, stranger), self.provider.objects)
        self.assertEqual(self.provider.deletes, 0)

    def test_a_written_key_is_listed_before_its_put_and_its_cleanup_still_deletes(self) -> None:
        self.ready_gate()
        self.upload()
        old = self.assert_current(self.job_id(), worker_fixture.A)
        (self.source / "report.txt").write_bytes(worker_fixture.B)
        stat = (self.source / "report.txt").stat()
        with self.db() as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path="report.txt", file_type="regular", size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())
        phases = self.listings_by_phase()
        self.upload()
        current = self.assert_current(self.job_id(), worker_fixture.B)
        # The pinned inventory is two equal listings; the cleanup lists again
        # before freezing, before the DELETE and after it.
        self.assertEqual(phases[:3], ["list", "list", "put"])
        self.assertEqual(phases.count("list"), 8)
        key = f"vaults/{self.uuid}/report.txt"
        self.assertEqual(self.provider.deleted, [(key, old["provider_version_id"])])
        self.assertEqual(sorted(v for k, v in self.provider.objects if k == key),
                         [current["provider_version_id"]])

    def test_a_key_an_earlier_holder_of_the_path_sent_to_is_listed_before_the_put(self) -> None:
        """Path History: another Vault File's send to this key counts as written."""
        self.ready_gate()

        def lost_before_put(stage, **kwargs):
            if stage == "before_put":
                raise ConnectionError("connection reset before the request left")
        self.provider.hook = lost_before_put
        with self.assertRaises(AssertionError):
            self.upload()  # A send-started attempt that never landed.
        self.provider.hook = lambda stage, **kwargs: None
        self.assertEqual(self.provider.puts, 0)
        with self.db() as connection:
            # The file leaves the path; a new Vault File takes it.
            ArchiveCatalog(connection).rename_file(
                self.file_id, new_path="renamed.txt", changed_at=storage.now_iso(), vault_id=2)
        self.new_file("report.txt")
        self.assertEqual(self.rows("SELECT * FROM archive_versions"), [])
        phases = self.listings_by_phase()
        self.upload("report.txt")
        self.assertEqual(phases[:3], ["list", "list", "put"])

    def test_no_provider_request_runs_inside_a_catalog_permit(self) -> None:
        """S3 latency never occupies a Vault's catalog slot (#622)."""
        from app.services import upload_pipeline
        self.ready_gate()
        held: list[int] = []
        inside: list[str] = []
        original = upload_pipeline._Pipeline.catalog_unit

        @contextmanager
        def counted_unit(pipeline, job, check):
            with original(pipeline, job, check):
                held.append(1)
                try:
                    yield
                finally:
                    held.pop()
        previous = self.provider.hook

        def hook(stage, **kwargs):
            previous(stage, **kwargs)
            if held:
                inside.append(stage)
        self.provider.hook = hook
        with patch.object(upload_pipeline._Pipeline, "catalog_unit", counted_unit):
            self.upload()
            (self.source / "report.txt").write_bytes(worker_fixture.B)
            stat = (self.source / "report.txt").stat()
            with self.db() as connection:
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2, path="report.txt", file_type="regular", size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())
            self.upload()
        self.assertEqual(self.provider.deletes, 1)
        self.assertEqual(inside, [])

    def test_one_upload_takes_a_bounded_number_of_exclusive_vault_locks(self) -> None:
        self.ready_gate()
        cost = self.upload()
        modes = [" ".join(match.group(1).upper().split())
                 for _, sql, _ in cost.statements if (match := _VAULT_LOCK.search(sql))]
        exclusive = [mode for mode in modes if mode in ("NO KEY UPDATE", "UPDATE")]
        # Admission, materialization, publication and completion.
        self.assertEqual(len(exclusive), 4, modes)

    def test_a_unit_that_shares_the_vault_lock_never_asks_for_more(self) -> None:
        """Two shared holders that both upgrade deadlock (ADR-0031)."""
        self.ready_gate()
        cost = self.upload()
        shared = 0
        for queries in cost.units().values():
            modes = [" ".join(match.group(1).upper().split()) for query in queries
                     if (match := _VAULT_LOCK.search(query))]
            if "SHARE" not in modes:
                continue
            shared += 1
            after = modes[modes.index("SHARE"):]
            text = "\n".join(queries)
            self.assertNotIn("NO KEY UPDATE", after, text)
            self.assertNotIn("UPDATE", after, text)
            for forbidden in ("UPDATE vaults", "INSERT INTO catalog_events", "INSERT INTO vault_files",
                              "INTO directory_aggregates", "directory_aggregate_status"):
                self.assertNotIn(forbidden, text)
        # Load, the digest comparison and send-started. A keeper that
        # superseded nothing needs no cleanup read (#622).
        self.assertGreaterEqual(shared, 3, "The probe no longer sees shared units")

    def test_cancellation_between_units_is_seen_before_the_put(self) -> None:
        """No lease read was dropped before an effect: a cancel wins before S3."""
        self.ready_gate()
        from app.services import upload_execution
        original = upload_execution.UploadExecution._hash

        def cancel_after_hash(execution, run):
            result = original(execution, run)
            with self.db() as connection:
                connection.execute("UPDATE jobs SET status='cancelled', finished_at=now() WHERE id=%s",
                                   (run.job["id"],))
            return result

        with patch.object(upload_execution.UploadExecution, "_hash", cancel_after_hash):
            with self.assertRaises(Exception):
                self.upload()
        self.assertEqual(self.provider.puts, 0)
        self.assertEqual(self.rows("SELECT * FROM upload_attempts WHERE send_started_at IS NOT NULL"), [])

    def test_a_takeover_after_send_started_never_publishes(self) -> None:
        self.ready_gate()
        from app.services import upload_execution
        original = upload_execution.UploadExecution._materialize

        def taken_over(execution, run, attempt, response):
            with self.db() as connection:
                connection.execute("UPDATE job_leases SET token=gen_random_uuid() WHERE job_id=%s",
                                   (run.job["id"],))
            return original(execution, run, attempt, response)

        with patch.object(upload_execution.UploadExecution, "_materialize", taken_over):
            with self.assertRaises(Exception):
                self.upload()
        self.assertEqual(self.provider.puts, 1)
        row = self.file_row()
        self.assertIsNone(row["current_version_id"])
        self.assertEqual(len(self.rows("SELECT * FROM upload_receipts")), 1, "the receipt stays")

    # -- helpers ---------------------------------------------------------------

    def job_id(self) -> int:
        return int(self.rows("SELECT id FROM jobs WHERE file_id=%s ORDER BY id DESC LIMIT 1",
                             self.file_id)[0]["id"])

    def new_file(self, path: str) -> None:
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(worker_fixture.A)
        stat = target.stat()
        with self.db() as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path=path, file_type="regular", size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())

    def seed_history(self, files: int = 300) -> None:
        """Much history: terminal Jobs, closed remediations, cleaned-up versions."""
        # Real uploads replaced once: closed snapshots, landed attempts,
        # receipts and superseded versions removed by their exact cleanup.
        for index in range(3):
            path = f"history/replaced-{index}.txt"
            self.new_file(path)
            self.upload(path)
            (self.source / path).write_bytes(worker_fixture.B + bytes([index]))
            stat = (self.source / path).stat()
            with self.db() as connection:
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2, path=path, file_type="regular", size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())
            self.upload(path)
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            for index in range(files):
                catalog.observe_local_copy(
                    vault_id=2, path=f"history/old/{index:04d}.txt", file_type="regular", size=1,
                    mtime_ns=1, observed_at=storage.now_iso())
            connection.execute(
                """INSERT INTO jobs(vault_id, file_id, action, status, origin, total_bytes, finished_at)
                   SELECT 2, f.id, 'upload', s.status, 'manual', 1, now()
                   FROM files f CROSS JOIN unnest(ARRAY['completed', 'failed', 'cancelled',
                                                        'completed']) AS s(status)
                   WHERE f.vault_id=2""")
            # Closed versions: proven mismatches no Current references.
            connection.execute(
                """INSERT INTO archive_versions(file_id, object_key, provider_version_id, size,
                                                 integrity, created_at)
                   SELECT f.id, f.path, 'old-' || n, 1, 'mismatch', now()
                   FROM files f CROSS JOIN generate_series(1, 3) AS n
                   WHERE f.vault_id=2 AND f.path LIKE 'history/old/%'""")
            connection.execute(
                """INSERT INTO upload_remediations(vault_id, file_id, state, reason, due_at, closed_at)
                   SELECT 2, f.id, s.state, NULL, now(), now()
                   FROM files f CROSS JOIN unnest(ARRAY['resolved', 'cancelled']) AS s(state)
                   WHERE f.vault_id=2""")
            connection.execute("ANALYZE")

    def assert_indexed(self, cost: Cost) -> None:
        """Every statement on a history table can be served by an index."""
        unindexed = {}
        with PostgresConnection(self.db_path) as connection:
            # With sequential scans priced out, one remains only where no index
            # can serve the statement: independent of table size and statistics.
            connection.execute("SET enable_seqscan=off")
            # A partial index that keeps only open rows (Jobs not terminal,
            # problems still open, episodes not closed) is bounded by open work:
            # walking it does not grow with history.
            open_work = {
                row["name"] for row in connection.execute(
                    """SELECT c.relname AS name, pg_get_expr(i.indpred, i.indrelid) AS predicate
                       FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                       WHERE i.indpred IS NOT NULL""").fetchall()
                if _OPEN_ONLY.fullmatch(row["predicate"][1:-1])
            }
            # An index walked without a condition is a size-dependent planner
            # choice on a near-empty table; it signals a missing index only on
            # a table the seeded history grew (a Seq Scan always does).
            grown = {
                row["relname"] for row in connection.execute(
                    "SELECT relname FROM pg_class WHERE relkind='r' AND reltuples >= 100")
            }
            seen = set()
            for _, query, params in cost.statements:
                key = (query, json.dumps(params, default=str))
                if key in seen or not _EXPLAINABLE.match(query):
                    continue
                seen.add(key)
                plan = connection.execute("EXPLAIN (FORMAT JSON) " + query, params).fetchone()["QUERY PLAN"]
                found: set[str] = set()
                _whole_relation_reads(plan[0]["Plan"], found, open_work, grown)
                for table in found & HISTORY_TABLES:
                    unindexed.setdefault(table, " ".join(query.split())[:160])
        self.assertEqual(unindexed, {})


def _whole_relation_reads(plan, found, open_work=frozenset(), grown=None):
    """Relations read in full: a sequential scan, or an index of a ``grown``
    relation walked without a condition (except an ``open_work`` partial index,
    which excludes history)."""
    kind = plan.get("Node Type")
    relation = plan.get("Relation Name")
    walked = (kind in ("Index Scan", "Index Only Scan") and not plan.get("Index Cond")
              and plan.get("Index Name") not in open_work
              and (grown is None or relation in grown))
    if kind == "Seq Scan" or walked:
        found.add(relation)
    for child in plan.get("Plans", ()):
        _whole_relation_reads(child, found, open_work, grown)


class ClaimCheckpointTests(unittest.TestCase):
    setUp = worker_fixture.UploadWorkerTests.setUp
    db = worker_fixture.UploadWorkerTests.db
    observe = worker_fixture.UploadWorkerTests.observe
    queue = worker_fixture.UploadWorkerTests.queue
    claim = worker_fixture.UploadWorkerTests.claim

    def test_checkpoints_inside_the_renew_window_read_without_writing(self):
        job = self.claim(self.queue())
        with storage._job_claim_context(job), patch.object(
            storage, "_renew_claim_if_owned", wraps=storage._renew_claim_if_owned,
        ) as renew:
            for _ in range(5):
                storage.ensure_job_active(job["id"], "stopped")
            self.assertEqual(renew.call_count, 1)
            with patch.object(storage, "CLAIM_CHECKPOINT_RENEW_SECONDS", 0.0):
                storage.ensure_job_active(job["id"], "stopped")
            self.assertEqual(renew.call_count, 2)

    def test_the_dispatch_renewal_opens_the_renew_window(self):
        job = self.claim(self.queue())
        with storage._job_claim_context(job):
            self.assertIsNotNone(storage._load_claimed_job_status(job))
            with patch.object(storage, "_renew_claim_if_owned") as renew:
                storage.ensure_job_active(job["id"], "stopped")
            renew.assert_not_called()

    def test_a_read_only_checkpoint_still_observes_cancellation_and_takeover(self):
        for change in ("UPDATE jobs SET status='cancelled', finished_at=now() WHERE id=%s",
                       "UPDATE job_leases SET token=gen_random_uuid() WHERE job_id=%s",
                       "UPDATE job_leases SET claimed_at='1999-12-31T23:00:00+00:00', "
                       "expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=%s"):
            with self.subTest(change=change):
                with self.db() as connection:
                    connection.execute("DELETE FROM job_leases")
                    connection.execute("UPDATE jobs SET status='cancelled', finished_at=now() "
                                       "WHERE status NOT IN ('completed', 'failed', 'cancelled')")
                job = self.claim(self.queue())
                with storage._job_claim_context(job):
                    storage.ensure_job_active(job["id"], "stopped")
                    with self.db() as connection:
                        connection.execute(change, (job["id"],))
                    with patch.object(storage, "_renew_claim_if_owned") as renew:
                        with self.assertRaises((storage.JobLeaseLost, storage.OperationCancelled)):
                            storage.ensure_job_active(job["id"], "stopped")
                    renew.assert_not_called()


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
                self.aggregate_flushes = storage.process_directory_aggregate_maintenance.call_count
                return depth_reads.call_count, stall.call_count

        return asyncio.run(run())

    def test_a_short_queue_is_counted_every_cycle(self):
        self.assertEqual(self.cycles(depth=39), (12, 6))

    def test_directory_aggregates_are_folded_once_per_batch_interval(self):
        """A deep queue spins every 100 ms; aggregates fold once per interval (#622)."""
        self.cycles(depth=40)
        self.assertEqual(self.aggregate_flushes, 1)
        with patch.object(storage, "DIRECTORY_AGGREGATE_BATCH_SECONDS", 0.0):
            self.cycles(depth=40)
        self.assertEqual(self.aggregate_flushes, 6)

    def test_a_deep_queue_is_sampled_not_recounted_every_cycle(self):
        self.assertEqual(self.cycles(depth=40), (2, 1))
        with patch.object(storage, "BACKLOG_OBSERVE_SECONDS", 0.0):
            self.assertEqual(self.cycles(depth=40), (12, 6))


if __name__ == "__main__":
    unittest.main()
