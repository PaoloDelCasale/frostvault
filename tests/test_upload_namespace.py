"""Per-attempt namespace sharing through the claimed Job/provider/catalog Seams."""
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
