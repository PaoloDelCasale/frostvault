"""Per-attempt namespace sharing through the claimed Job/provider/catalog Seams."""
from __future__ import annotations

import json
import unittest
from contextlib import contextmanager

import psycopg
from unittest.mock import patch

from app import storage
from app.services import upload_provenance as provenance
from tests import test_upload_provenance_worker as worker_tests
from tests.postgres import PostgresConnection


class UploadNamespaceWorkerTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real admission/PG/provider fixture, not its test methods.
        self.worker = worker_tests.UploadProvenanceWorkerTests(methodName="runTest")
        self.addCleanup(self.worker.doCleanups)
        self.worker.setUp()

    def test_fresh_upload_shares_initial_namespace_without_skipping_cleanup(self):
        listings = []
        listing = self.worker.provider.list_object_versions

        def observe(**kwargs):
            listings.append((kwargs["Bucket"], kwargs["Prefix"]))
            return listing(**kwargs)

        with patch.object(self.worker.provider, "list_object_versions", side_effect=observe):
            job = self.worker.queue()
            self.worker.run_job(job)

        version = self.worker.assert_keeper(job)
        # Two complete equal pre-send observations, two cleanup observations,
        # and a final postcondition. Sharing saves only the duplicate initial pair.
        self.assertEqual(listings, [("bucket", "docs/report.txt")] * 5)
        self.assertEqual(self.worker.provider.puts, 1)
        with PostgresConnection(self.worker.db_path) as connection:
            inventory = connection.execute(
                "SELECT * FROM snapshot_cleanup_inventories WHERE job_id=%s", (job,),
            ).fetchone()
            self.assertEqual(inventory["keeper_id"], version["id"])
            self.assertIsNotNone(inventory["completed_at"])

    def test_invalid_historical_pin_cannot_commit_a_keeper_binding(self):
        original = storage.purge_current_snapshot_superseded

        def changed_pin(job, **kwargs):
            # Corrupt the durable observation at cleanup entry, not a particular
            # inventory-loading implementation. All rejection assertions stay.
            with PostgresConnection(self.worker.db_path) as connection:
                batch = connection.execute('SELECT * FROM snapshot_cleanup_inventories WHERE job_id=%s', (job['id'],)).fetchone()
                pinned = json.loads(batch['target_json'])
                pinned['path_since'] = 'not-the-admitted-path-generation'
                connection.execute('UPDATE snapshot_cleanup_inventories SET target_json=%s WHERE job_id=%s',
                                   (json.dumps(pinned), batch['job_id']))
            return original(job, **kwargs)

        job = self.worker.queue()
        with patch.object(storage, 'purge_current_snapshot_superseded', side_effect=changed_pin):
            self.worker.run_job(job)
        self.assertNotEqual(self.worker.row(job)['status'], 'completed')
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertEqual(self.worker.provider.deleted, [])
        with PostgresConnection(self.worker.db_path) as connection:
            batch = connection.execute('SELECT keeper_id,completed_at FROM snapshot_cleanup_inventories WHERE job_id=%s', (job,)).fetchone()
        self.assertIsNone(batch['keeper_id'], 'Reject the historical pin before committing its keeper')
        self.assertIsNone(batch['completed_at'])

    def test_keeper_binding_retry_does_not_repeat_provider_effects(self):
        self.worker.provider.add('docs/report.txt', worker_tests.A, version='old-orphan')
        execute = psycopg.Cursor.execute
        conflicts = []
        stages = []
        self.worker.provider.hook = lambda stage, **kwargs: stages.append(stage)

        def conflict_after_write(cursor, query, params=None, **kwargs):
            result = execute(cursor, query, params, **kwargs)
            if isinstance(query, str) and query.startswith('UPDATE snapshot_cleanup_inventories SET keeper_id=') and not conflicts:
                conflicts.append(True)
                raise psycopg.errors.SerializationFailure('injected keeper binding conflict')
            return result

        job = self.worker.queue()
        with patch.object(psycopg.Cursor, 'execute', conflict_after_write):
            self.worker.run_job(job)
        keeper = self.worker.assert_keeper(job)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertEqual(self.worker.provider.gets, 1)
        self.assertEqual(self.worker.provider.deleted, [('docs/report.txt', 'old-orphan')])
        self.assertEqual(stages.count('list'), 6, 'DB retry must not replay S3 observations')
        with PostgresConnection(self.worker.db_path) as connection:
            batch = connection.execute('SELECT keeper_id,completed_at FROM snapshot_cleanup_inventories WHERE job_id=%s', (job,)).fetchone()
            self.assertEqual(connection.execute('SELECT COUNT(*) n FROM upload_receipts').fetchone()['n'], 1)
        self.assertEqual(batch['keeper_id'], keeper['id'])
        self.assertIsNotNone(batch['completed_at'])

    def test_cold_pin_observations_stay_outside_database_retry(self):
        self.worker.provider.add('docs/report.txt', worker_tests.A, version='old-orphan')
        original = storage.purge_current_snapshot_superseded
        original_db, execute = storage.db, psycopg.Cursor.execute
        active, conflicts, stages, cold = [], [], [], []

        @contextmanager
        def tracked_db():
            with original_db() as connection:
                active.append(connection)
                try:
                    yield connection
                finally:
                    active.pop()

        def remove_pin(job, **kwargs):
            with PostgresConnection(self.worker.db_path) as connection:
                self.assertEqual(connection.execute('DELETE FROM snapshot_cleanup_inventories WHERE job_id=%s', (job['id'],)).rowcount, 1)
            cold.append(job['id'])
            return original(job, **kwargs)

        def observe(stage, **kwargs):
            stages.append(stage)
            if stage in {'list', 'get'}:
                self.assertEqual(active, [], 'Provider observations must not run inside DB units')

        def conflict_after_write(cursor, query, params=None, **kwargs):
            result = execute(cursor, query, params, **kwargs)
            if isinstance(query, str) and query.startswith('UPDATE snapshot_cleanup_inventories SET keeper_id=') and not conflicts:
                conflicts.append(True)
                raise psycopg.errors.SerializationFailure('injected cold binding conflict')
            return result

        self.worker.provider.hook = observe
        job = self.worker.queue()
        with patch.object(storage, 'db', side_effect=tracked_db), \
                patch.object(storage, 'purge_current_snapshot_superseded', side_effect=remove_pin), \
                patch.object(psycopg.Cursor, 'execute', conflict_after_write):
            self.worker.run_job(job)
        keeper = self.worker.assert_keeper(job)
        self.assertEqual(cold, [job])
        self.assertEqual(conflicts, [True])
        self.assertEqual(stages.count('list'), 8, 'Only the cold path adds one observation pair')
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertEqual(self.worker.provider.gets, 1)
        self.assertEqual(self.worker.provider.deleted, [('docs/report.txt', 'old-orphan')])
        with PostgresConnection(self.worker.db_path) as connection:
            batch = connection.execute('SELECT keeper_id,completed_at FROM snapshot_cleanup_inventories WHERE job_id=%s', (job,)).fetchone()
            self.assertEqual(connection.execute('SELECT COUNT(*) n FROM upload_receipts').fetchone()['n'], 1)
        self.assertEqual(batch['keeper_id'], keeper['id'])
        self.assertIsNotNone(batch['completed_at'])

    def test_cold_pin_inventory_change_stops_without_binding_or_deletion(self):
        original = storage.purge_current_snapshot_superseded
        cold, observations = [], []

        def remove_pin(job, **kwargs):
            with PostgresConnection(self.worker.db_path) as connection:
                self.assertEqual(connection.execute('DELETE FROM snapshot_cleanup_inventories WHERE job_id=%s', (job['id'],)).rowcount, 1)
            cold.append(job['id'])
            return original(job, **kwargs)

        def external_writer(stage, **kwargs):
            if stage == 'list' and cold:
                observations.append(True)
                if len(observations) == 2:
                    self.worker.provider.add('docs/report.txt', worker_tests.B, version='external-during-cold-pin')

        self.worker.provider.hook = external_writer
        job = self.worker.queue()
        with patch.object(storage, 'purge_current_snapshot_superseded', side_effect=remove_pin):
            self.worker.run_job(job)
        self.assertEqual(cold, [job])
        self.assertEqual(len(observations), 2)
        self.assertNotEqual(self.worker.row(job)['status'], 'completed')
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertEqual(self.worker.provider.deleted, [])
        self.assertIn(('docs/report.txt', 'external-during-cold-pin'), self.worker.provider.objects)
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertIsNone(connection.execute('SELECT job_id FROM snapshot_cleanup_inventories WHERE job_id=%s', (job,)).fetchone())
            self.assertEqual(connection.execute('SELECT COUNT(*) n FROM upload_receipts').fetchone()['n'], 1)

    def test_historical_pin_is_not_rebased_when_an_unsent_job_retries(self):
        job = self.worker.queue()
        with patch.object(provenance, "mark_send_started", side_effect=RuntimeError("pre-send unavailable")):
            self.worker.run_job(job)
        self.assertEqual(self.worker.provider.puts, 0)
        with PostgresConnection(self.worker.db_path) as connection:
            before = connection.execute(
                "SELECT inventory_json FROM snapshot_cleanup_inventories WHERE job_id=%s", (job,),
            ).fetchone()["inventory_json"]
            connection.execute("UPDATE jobs SET status='queued',retry_after=NULL WHERE id=%s", (job,))
        self.worker.provider.add("docs/report.txt", worker_tests.A, version="external-after-pin")
        self.worker.run_job(job)
        self.assertNotEqual(self.worker.row(job)["status"], "completed")
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertEqual(self.worker.provider.deleted, [])
        self.assertIn(("docs/report.txt", "external-after-pin"), self.worker.provider.objects)
        with PostgresConnection(self.worker.db_path) as connection:
            after = connection.execute(
                "SELECT inventory_json,completed_at FROM snapshot_cleanup_inventories WHERE job_id=%s", (job,),
            ).fetchone()
        self.assertEqual(after["inventory_json"], before)
        self.assertIsNone(after["completed_at"])

    def test_change_between_initial_observations_blocks_before_send(self):
        observed = 0

        def external_writer(stage, **kwargs):
            nonlocal observed
            if stage == "list":
                observed += 1
                if observed == 2:
                    self.worker.provider.add("docs/report.txt", worker_tests.B, version="external")

        self.worker.provider.hook = external_writer
        job = self.worker.queue()
        self.worker.run_job(job)
        self.assertEqual(self.worker.provider.puts, 0)
        self.assertEqual(self.worker.provider.deleted, [])
        self.assertNotEqual(self.worker.row(job)["status"], "completed")

    def test_claim_lost_during_initial_observation_cannot_commit_pin_or_send(self):
        observed = 0
        job = self.worker.queue()

        def takeover(stage, **kwargs):
            nonlocal observed
            if stage == "list":
                observed += 1
                if observed == 2:
                    with PostgresConnection(self.worker.db_path) as connection:
                        connection.execute("UPDATE jobs SET claim_token='new-owner' WHERE id=%s", (job,))

        self.worker.provider.hook = takeover
        self.worker.run_job(job)
        self.assertEqual(self.worker.provider.puts, 0)
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertIsNone(connection.execute(
                "SELECT job_id FROM snapshot_cleanup_inventories WHERE job_id=%s", (job,),
            ).fetchone())

    def test_fresh_shared_pin_still_blocks_an_external_arrival_after_send(self):
        def external_writer(stage, **kwargs):
            if stage == "acknowledged":
                self.worker.provider.add("docs/report.txt", worker_tests.B, version="external-after-send")

        self.worker.provider.hook = external_writer
        job = self.worker.queue()
        self.worker.run_job(job)
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertNotEqual(self.worker.row(job)["status"], "completed")
        self.assertEqual(self.worker.provider.deleted, [])
        self.assertIn(("docs/report.txt", "external-after-send"), self.worker.provider.objects)
