"""Per-attempt namespace sharing through the claimed Job/provider/catalog Seams."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import storage
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
