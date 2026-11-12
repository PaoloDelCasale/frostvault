"""Exact cleanup of superseded versions on the rebuilt model (#591, ADR-0035).

Real admission, claim and ``storage.process_job`` on a disposable PostgreSQL 16
database with the synthetic ``VersionedProvider`` (exact VersionIds, Delete
Markers, counted PUT/GET/DELETE). The fixtures are those of
``test_upload_worker``; Upload Remediation (#592) stays patched.
"""
from __future__ import annotations

import unittest
import uuid

import psycopg

from app import storage
from app.catalog import ArchiveCatalog
from tests import test_upload_worker as fixtures
from tests.test_upload_worker import A, B, _FlakyCommit, sha

C = b"third content C"


class UploadCleanupTests(unittest.TestCase):
    setUp = fixtures.UploadWorkerTests.setUp
    db = fixtures.UploadWorkerTests.db
    observe = fixtures.UploadWorkerTests.observe
    write = fixtures.UploadWorkerTests.write
    queue = fixtures.UploadWorkerTests.queue
    claim = fixtures.UploadWorkerTests.claim
    run_job = fixtures.UploadWorkerTests.run_job
    due = fixtures.UploadWorkerTests.due
    abandon = fixtures.UploadWorkerTests.abandon
    job = fixtures.UploadWorkerTests.job
    file_row = fixtures.UploadWorkerTests.file_row
    rows = fixtures.UploadWorkerTests.rows
    version = fixtures.UploadWorkerTests.version
    assert_current = fixtures.UploadWorkerTests.assert_current

    # -- helpers -------------------------------------------------------------

    def upload(self, payload: bytes) -> tuple[int, dict]:
        if self.file.read_bytes() != payload:
            self.write(payload)
        job_id = self.queue()
        self.run_job(job_id)
        return job_id, self.assert_current(job_id, payload)

    def versions(self) -> list[dict]:
        return self.rows("SELECT * FROM archive_versions ORDER BY created_at")

    def at_key(self) -> list[str]:
        return sorted(version for key, version in self.provider.objects if key == self.key)

    def after_publish(self, action) -> None:
        """Run ``action`` once, at the first listing after the new keeper is Current."""
        previous, done = self.provider.hook, []

        def hook(stage, **kwargs):
            previous(stage, **kwargs)
            if stage == "list" and not done:
                row = self.file_row()
                if row["current_version_id"] and str(row["current_version_id"]) != self.old_id:
                    done.append(True)
                    action()
        self.provider.hook = hook

    def replace_with_b(self, *, before_run=None) -> tuple[int, dict]:
        """A is Current; B is uploaded and published; returns B's Job and A."""
        _, old = self.upload(A)
        self.old_id = str(old["id"])
        self.write(B)
        job_id = self.queue()
        if before_run is not None:
            before_run(job_id)
        self.run_job(job_id)
        return job_id, old

    def assert_old_kept(self, old: dict) -> None:
        self.assertEqual(self.provider.deletes, 0)
        self.assertIn((self.key, old["provider_version_id"]), self.provider.objects)
        self.assertEqual(len(self.rows("SELECT 1 FROM archive_versions WHERE id=%s", old["id"])), 1)

    def assert_replaced(self, job_id: int, old: dict) -> dict:
        current = self.assert_current(job_id, B)
        self.assertEqual(self.provider.deleted, [(self.key, old["provider_version_id"])])
        self.assertEqual(self.at_key(), [current["provider_version_id"]])
        self.assertEqual([str(v["id"]) for v in self.versions()], [str(current["id"])])
        self.assertEqual(self.rows("SELECT * FROM cleanup_batches"), [])
        self.assertEqual(self.rows("SELECT * FROM cleanup_candidates"), [])
        return current

    # -- the ordinary replacement --------------------------------------------

    def test_replacement_deletes_the_exact_superseded_version_before_completion(self) -> None:
        completions = []
        real = storage._set_job_in_transaction

        def completing(connection, job_id, status, *args, **kwargs):
            if status == "completed":
                completions.append(list(self.provider.deleted))
            return real(connection, job_id, status, *args, **kwargs)
        self.enterContext(fixtures.patch.object(storage, "_set_job_in_transaction",
                                                side_effect=completing))
        first, old = self.upload(A)
        self.old_id = str(old["id"])
        self.write(B)
        second = self.queue()
        self.run_job(second)
        self.assert_replaced(second, old)
        self.assertEqual(completions[-1], [(self.key, old["provider_version_id"])],
                         "the Job completes only after the exact DELETE")
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (2, 0, 1))
        # The Job that produced the removed version now points at nothing; its
        # attempt and receipt stay for retention (#596).
        self.assertIsNone(self.job(first)["archive_version_id"])
        self.assertEqual(len(self.rows("SELECT * FROM upload_attempts")), 2)

    def test_steady_state_keeps_only_current_per_file(self) -> None:
        for payload in (A, B, C, A):
            job_id, current = self.upload(payload)
            self.assertEqual([str(v["id"]) for v in self.versions()], [str(current["id"])])
            self.assertEqual(self.at_key(), [current["provider_version_id"]])
        self.assertEqual((self.provider.puts, self.provider.deletes, self.provider.gets), (4, 3, 0))

    def test_unchanged_content_needs_no_listing_or_delete(self) -> None:
        self.upload(A)
        lists = []
        self.provider.hook = lambda stage, **kwargs: lists.append(stage) if stage == "list" else None
        self.write(A)
        job_id = self.queue()
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed")
        self.assertEqual((lists, self.provider.deletes, self.provider.puts), ([], 0, 1))

    # -- nothing unknown is ever deleted --------------------------------------

    def test_unknown_object_with_equal_bytes_is_never_adopted_or_deleted(self) -> None:
        def stranger(job_id):
            # Same bytes as A, written outside FrostVault: no attempt, no token.
            self.stranger = self.provider.add(self.key, A)
        job_id, old = self.replace_with_b(before_run=stranger)
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.file_row()["reconciliation_block"], "inventory_changed")
        self.assert_old_kept(old)
        self.assertIn((self.key, self.stranger), self.provider.objects)
        self.assertEqual(self.provider.gets, 0, "equal bytes are never adopted by a GET")
        self.assertEqual(self.rows("SELECT * FROM cleanup_batches"), [])

    def test_unknown_delete_marker_stops_the_cleanup(self) -> None:
        def marker(job_id):
            self.provider.markers[self.key, "marker-1"] = True
        job_id, old = self.replace_with_b(before_run=marker)
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.file_row()["reconciliation_block"], "markers_present")
        self.assert_old_kept(old)
        self.assertIn((self.key, "marker-1"), self.provider.markers)

    def test_unknown_object_at_the_keeper_key_blocks_even_the_first_completion(self) -> None:
        stranger = self.provider.add(self.key, b"someone else's bytes")
        job_id = self.queue()
        self.run_job(job_id)
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.file_row()["reconciliation_block"], "inventory_changed")
        self.assertEqual(self.provider.deletes, 0)
        self.assertIn((self.key, stranger), self.provider.objects)

    def test_object_appearing_after_the_freeze_stops_before_any_delete(self) -> None:
        previous, deletes = self.provider.hook, []

        def hook(stage, **kwargs):
            previous(stage, **kwargs)
            if (stage == "list" and not deletes and self.rows("SELECT 1 FROM cleanup_batches")):
                deletes.append(self.provider.add(self.key, b"late external writer"))
        job_id, old = self.replace_with_b(before_run=lambda _: setattr(self.provider, "hook", hook))
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        self.assert_old_kept(old)
        batch = self.rows("SELECT * FROM cleanup_batches")
        self.assertEqual(len(batch), 1, "the frozen batch stays open")
        self.assertEqual([str(c["archive_version_id"]) for c in self.rows("SELECT * FROM cleanup_candidates")],
                         [str(old["id"])], "nothing joins a frozen batch")

    def test_proven_mismatch_is_retained_and_blocks_the_cleanup(self) -> None:
        def corrupt(stage, **kwargs):
            if stage == "accepted":
                data, metadata = self.provider.objects[self.key, kwargs["version"]]
                self.provider.objects[self.key, kwargs["version"]] = (b"X" * len(data), metadata)
        self.provider.hook = corrupt
        bad = self.queue("read_back")
        self.run_job(bad)
        mismatch = self.rows("SELECT * FROM archive_versions")[0]
        self.assertEqual(mismatch["integrity"], "mismatch")
        self.provider.hook = lambda stage, **kwargs: None
        retry = self.queue()
        self.run_job(retry)
        self.assertNotEqual(self.job(retry)["status"], "completed")
        self.assertEqual(self.file_row()["reconciliation_block"], "proof_mismatch")
        self.assertIn((self.key, mismatch["provider_version_id"]), self.provider.objects)
        self.assertEqual(self.provider.deletes, 0)

    # -- every fence stops the real effect after the keeper commit ------------

    def stopped_after_keeper(self, action) -> None:
        job_id, old = self.replace_with_b(
            before_run=lambda queued: self.after_publish(lambda: action(queued)))
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        row = self.file_row()
        self.assertEqual(bytes(self.version(row["current_version_id"])["plaintext_sha256"]), sha(B))
        self.assert_old_kept(old)

    def test_cancellation_after_the_keeper_commit_stops_the_delete(self) -> None:
        def cancel(job_id):
            with self.db() as connection:
                ArchiveCatalog(connection).cancel_jobs(
                    vault_id=2, job_ids=[job_id], message="stop", message_key=None,
                    now=storage.now_iso())
        self.stopped_after_keeper(cancel)

    def test_takeover_after_the_keeper_commit_stops_the_delete(self) -> None:
        def takeover(job_id):
            with self.db() as connection:
                connection.execute("UPDATE job_leases SET token=%s WHERE job_id=%s",
                                   (uuid.uuid4(), job_id))
        self.stopped_after_keeper(takeover)

    def test_lease_expiry_after_the_keeper_commit_stops_the_delete(self) -> None:
        def expire(job_id):
            with self.db() as connection:
                connection.execute("UPDATE job_leases SET claimed_at=now() - interval '2 hours', "
                                   "expires_at=now() - interval '1 hour' WHERE job_id=%s", (job_id,))
        self.stopped_after_keeper(expire)

    def test_root_change_after_the_keeper_commit_stops_the_delete(self) -> None:
        def root(job_id):
            with self.db() as connection:
                connection.execute("UPDATE vaults SET root_identity='another-root' WHERE id=2")
        self.stopped_after_keeper(root)

    def test_invalid_vault_after_the_keeper_commit_stops_the_delete(self) -> None:
        def lifecycle(job_id):
            with self.db() as connection:
                connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=2")
        self.stopped_after_keeper(lifecycle)

    # -- resumption without a second PUT ----------------------------------------

    def test_a_new_job_resumes_after_the_original_job_was_collected(self) -> None:
        from app.services import snapshot_cleanup
        fail = []
        original_delete = snapshot_cleanup._delete_s3_version

        def flaky_delete(client, **kwargs):
            if not fail:
                fail.append(True)
                raise ConnectionError("connection reset during DeleteObject")
            return original_delete(client, **kwargs)
        self.enterContext(fixtures.patch.object(snapshot_cleanup, "_delete_s3_version",
                                                side_effect=flaky_delete))
        job_id, old = self.replace_with_b()
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        self.assert_old_kept(old)
        batch = self.rows("SELECT * FROM cleanup_batches")[0]
        self.assertEqual(batch["job_id"], job_id)
        # Retention collects the original Job: the frozen batch survives it.
        self.abandon(job_id)
        with self.db() as connection:
            connection.execute("DELETE FROM job_terminal_notifications WHERE job_id=%s", (job_id,))
            connection.execute("DELETE FROM jobs WHERE id=%s", (job_id,))
        self.assertIsNone(self.rows("SELECT job_id FROM cleanup_batches")[0]["job_id"])
        puts = self.provider.puts
        resumed = self.queue()
        self.run_job(resumed)
        self.assertEqual(self.job(resumed)["status"], "completed", self.job(resumed)["message"])
        self.assertEqual(self.provider.puts, puts, "resuming a cleanup never sends again")
        self.assertEqual(self.provider.deleted, [(self.key, old["provider_version_id"])])
        self.assertEqual(len(self.versions()), 1)
        self.assertEqual(self.rows("SELECT * FROM cleanup_batches"), [])

    def test_a_retried_job_finishes_its_own_cleanup_without_a_second_put(self) -> None:
        def reset() -> None:
            raise ConnectionError("listing reset")
        job_id, old = self.replace_with_b(before_run=lambda _: self.after_publish(reset))
        self.assertEqual(self.job(job_id)["status"], "retrying")
        self.assert_old_kept(old)
        self.due(job_id)
        self.run_job(job_id)
        self.assert_replaced(job_id, old)
        self.assertEqual(self.provider.puts, 2)

    # -- DB retries never replay the DELETE --------------------------------------

    def test_serialization_failure_at_commit_never_repeats_the_delete(self) -> None:
        plan = {"INSERT INTO cleanup_batches": 1,
                "archive_version_id = ANY(%s::uuid[]) AND deleted_at IS NULL": 1,
                "DELETE FROM archive_versions": 1}
        hits: list[str] = []
        real = storage.db
        self.enterContext(fixtures.patch.object(
            storage, "db", side_effect=lambda: _FlakyCommit(real(), plan, hits)))
        job_id, old = self.replace_with_b()
        self.assertEqual(sorted(set(hits)), sorted(plan), "every cleanup unit hits one failure")
        self.assert_replaced(job_id, old)
        self.assertEqual(self.provider.delete_requests, 1)

    def test_commit_failure_after_the_delete_never_repeats_or_hides_it(self) -> None:
        plan = {"b.id=%s AND b.keeper_id=%s AND b.file_id=%s": 1}
        hits: list[str] = []
        real = storage.db
        self.enterContext(fixtures.patch.object(
            storage, "db", side_effect=lambda: _FlakyCommit(real(), plan, hits)))
        job_id, old = self.replace_with_b()
        self.assertEqual(hits, list(plan))
        self.assert_replaced(job_id, old)
        self.assertEqual((self.provider.delete_requests, self.provider.puts), (1, 2))

    # -- the schema keeps the proof ---------------------------------------------

    def rejects(self, sql: str, *params) -> None:
        with self.assertRaises(psycopg.errors.RestrictViolation):
            with self.db() as connection:
                connection.execute(sql, params)

    def test_schema_removes_a_version_only_with_its_completed_proof(self) -> None:
        from app.services import snapshot_cleanup
        self.enterContext(fixtures.patch.object(
            snapshot_cleanup, "_delete_s3_version", side_effect=ConnectionError("reset")))
        _, old = self.replace_with_b()
        current = self.file_row()["current_version_id"]
        batch = self.rows("SELECT * FROM cleanup_batches")[0]
        self.assertEqual(str(batch["keeper_id"]), str(current))
        # No proof yet: the row stays; the batch is never re-pointed.
        self.rejects("DELETE FROM archive_versions WHERE id=%s", old["id"])
        self.rejects("UPDATE cleanup_batches SET keeper_id=%s WHERE id=%s", old["id"], batch["id"])
        # Frozen: nothing joins later (not even the keeper); an open grant and
        # its batch are never collected.
        self.rejects("INSERT INTO cleanup_candidates(batch_id, file_id, archive_version_id) "
                     "VALUES (%s, %s, %s)", batch["id"], self.file_id, current)
        self.rejects("DELETE FROM cleanup_candidates WHERE batch_id=%s", batch["id"])
        self.rejects("DELETE FROM cleanup_batches WHERE id=%s", batch["id"])
        # A new batch names the verified Current as keeper, in the same file.
        self.rejects("INSERT INTO cleanup_batches(vault_id, file_id, keeper_id, provider, "
                     "path_since, source_root) VALUES (2, %s, %s, 'p', now(), '/x')",
                     self.file_id, old["id"])


if __name__ == "__main__":
    unittest.main()
