"""Per-attempt namespace observation through the claimed Job/provider/catalog Seams."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import upload_provenance as provenance
from tests import test_upload_provenance_worker as worker_tests
from tests.postgres import PostgresConnection


class UploadNamespaceWorkerTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real admission/PG/provider fixture, not its test methods.
        self.worker = worker_tests.UploadProvenanceWorkerTests(methodName="runTest")
        self.addCleanup(self.worker.doCleanups)
        self.worker.setUp()

    def listed(self):
        listings = []
        listing = self.worker.provider.list_object_versions

        def observe(**kwargs):
            listings.append((kwargs["Bucket"], kwargs["Prefix"]))
            return listing(**kwargs)
        return listings, patch.object(self.worker.provider, "list_object_versions", side_effect=observe)

    def written_key(self):
        """The catalog wrote the key: a verified A, then B is observed."""
        self.worker.seed_keeper()
        self.worker.observe_content(worker_tests.B)

    def test_a_never_written_key_is_listed_once_after_its_put(self):
        listings, observing = self.listed()
        with observing:
            job = self.worker.queue()
            self.worker.run_job(job)

        version = self.worker.assert_keeper(job)
        # ADR-0038: no inventory before the PUT of a key the catalog never
        # wrote; one listing of the exact key proves the keeper alone.
        self.assertEqual(listings, [("bucket", self.worker.key)])
        self.assertEqual(self.worker.provider.puts, 1)
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM cleanup_batches").fetchone())
            attempt = connection.execute("SELECT * FROM upload_attempts").fetchone()
            self.assertEqual((attempt["outcome"], attempt["landed_version_id"]),
                             ("landed", version["provider_version_id"]))
            self.assertEqual((list(attempt["inventory_versions"]), list(attempt["inventory_markers"])),
                             ([], []))

    def test_a_written_key_observes_the_exact_namespace_without_skipping_cleanup(self):
        self.written_key()
        listings, observing = self.listed()
        with observing:
            job = self.worker.queue()
            self.worker.run_job(job)

        self.worker.assert_keeper(job)
        # Two complete equal pre-send observations; the keeper's cleanup (#591)
        # then reads its own exact pair before freezing, a pair before the
        # DELETE and a final pair. Nothing outside the key is read.
        self.assertEqual(listings, [("bucket", self.worker.key)] * 8)
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertEqual(len(self.worker.provider.deleted), 1)
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM cleanup_batches").fetchone())

    def test_historical_pin_is_not_rebased_when_an_unsent_job_retries(self):
        self.written_key()
        job = self.worker.queue()
        with patch.object(provenance, "mark_send_started", side_effect=RuntimeError("pre-send unavailable")):
            self.worker.run_job(job)
        self.assertEqual(self.worker.provider.puts, 0)
        with PostgresConnection(self.worker.db_path) as connection:
            # Prepare and send-started commit in one unit: nothing was pinned.
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM upload_attempts WHERE job_id=%s", (job,)).fetchone())
            connection.execute("UPDATE jobs SET run_after=NULL WHERE id=%s", (job,))
        self.worker.provider.add(self.worker.key, worker_tests.A, version="external-after-pin")
        self.worker.run_job(job)
        self.assertNotEqual(self.worker.row(job)["status"], "completed")
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertEqual(self.worker.provider.deleted, [])
        self.assertIn((self.worker.key, "external-after-pin"), self.worker.provider.objects)
        with PostgresConnection(self.worker.db_path) as connection:
            (attempt,) = connection.execute(
                "SELECT * FROM upload_attempts WHERE job_id=%s", (job,)).fetchall()
            # The retry pins the namespace it observed; an object no landed
            # attempt wrote is never adopted nor deleted (#591).
            self.assertIn("external-after-pin", attempt["inventory_versions"])
            self.assertEqual(provenance.describe(connection, vault_id=2, path="report.txt")["reason"],
                             "inventory_changed")

    def test_change_between_initial_observations_blocks_before_send(self):
        observed = 0

        def external_writer(stage, **kwargs):
            nonlocal observed
            if stage == "list":
                observed += 1
                if observed == 2:
                    self.worker.provider.add(self.worker.key, worker_tests.B, version="external")

        self.written_key()
        self.worker.provider.hook = external_writer
        job = self.worker.queue()
        self.worker.run_job(job)
        self.assertEqual(self.worker.provider.puts, 0)
        self.assertEqual(self.worker.provider.deleted, [])
        self.assertNotEqual(self.worker.row(job)["status"], "completed")

    def test_claim_lost_during_initial_observation_cannot_commit_pin_or_send(self):
        observed = 0
        self.written_key()
        job = self.worker.queue()

        def takeover(stage, **kwargs):
            nonlocal observed
            if stage == "list":
                observed += 1
                if observed == 2:
                    with PostgresConnection(self.worker.db_path) as connection:
                        connection.execute("UPDATE job_leases SET token=gen_random_uuid() WHERE job_id=%s", (job,))

        self.worker.provider.hook = takeover
        self.worker.run_job(job)
        self.assertEqual(self.worker.provider.puts, 0)
        with PostgresConnection(self.worker.db_path) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM upload_attempts WHERE job_id=%s", (job,),
            ).fetchone())

    def test_fresh_shared_pin_still_blocks_an_external_arrival_after_send(self):
        def external_writer(stage, **kwargs):
            if stage == "acknowledged":
                self.worker.provider.add(self.worker.key, worker_tests.B, version="external-after-send")

        self.worker.provider.hook = external_writer
        job = self.worker.queue()
        self.worker.run_job(job)
        self.assertEqual(self.worker.provider.puts, 1)
        self.assertNotEqual(self.worker.row(job)["status"], "completed")
        self.assertEqual(self.worker.provider.deleted, [])
        self.assertIn((self.worker.key, "external-after-send"), self.worker.provider.objects)
