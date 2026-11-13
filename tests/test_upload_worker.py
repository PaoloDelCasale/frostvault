"""The upload worker on the rebuilt model (#590, ADR-0035/0036).

Real admission, claim and ``storage.process_job`` on a disposable PostgreSQL 16
database; the S3 transport is the synthetic ``VersionedProvider``, which keeps
version bytes and metadata across worker runs and counts every PUT/GET/HEAD.
Upload Remediation (#592) and the exact cleanup of superseded versions (#591)
run for real; their own cases live in ``tests.test_upload_remediation`` and
``tests.test_upload_cleanup``.
"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from app import storage
from app.catalog import ArchiveCatalog
from app.services import upload_provenance as provenance
from app.services.vault_relocation import enroll_vault_root_identity
from tests.catalog_support import seed_vault
from tests.postgres import PostgresConnection, database_dsn, run_alembic
from tests.upload_provenance_provider import VersionedProvider

A = b"immutable content A"
B = b"different content B"


def sha(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()


class _FlakyCommit:
    """A connection whose COMMIT fails once with 40001 after chosen statements."""

    def __init__(self, connection: psycopg.Connection, plan: dict[str, int], hits: list[str]):
        self._connection, self._plan, self._hits = connection, plan, hits
        self._seen: set[str] = set()

    def execute(self, sql: str, *args, **kwargs):
        for marker in self._plan:
            if marker in sql:
                self._seen.add(marker)
        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, kind, value, traceback):
        failing = [m for m in self._seen if self._plan.get(m, 0) > 0]
        if kind is None and failing:
            marker = failing[0]
            self._plan[marker] -= 1
            self._hits.append(marker)
            self._connection.rollback()
            self._connection.close()
            raise psycopg.errors.SerializationFailure("could not serialize access")
        return self._connection.__exit__(kind, value, traceback)


class UploadWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "report.txt"
        self.file.write_bytes(A)
        self.db_path = self.root / "catalog"
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.provider = VersionedProvider()
        self.heads = 0
        with self.db() as connection:
            seed_vault(connection, vault_id=2, source_root=str(self.source))
            enroll_vault_root_identity(connection, 2, str(self.source))
            self.file_id = self.observe(connection)
            self.uuid = connection.execute("SELECT uuid FROM vaults WHERE id=2").fetchone()["uuid"]
        self.key = f"vaults/{self.uuid}/report.txt"
        self.enterContext(patch("app.database.settings",
                                SimpleNamespace(database_dsn=database_dsn(self.db_path))))
        self.enterContext(patch("app.storage.validate_cloud_vault"))
        self.enterContext(patch("app.storage.s3_client", return_value=self.provider))
        self.enterContext(patch("app.storage.upload_file", side_effect=self.provider.upload))
        self.addCleanup(storage.shutdown_state.reset)
        # Job ids restart at 1 in every disposable catalog: a cancel flag
        # another test raised for the same id must not stop this test's Job.
        self.enterContext(patch("app.storage.cancelled_jobs", set()))

    # -- helpers -------------------------------------------------------------

    def db(self) -> psycopg.Connection:
        return PostgresConnection(self.db_path)

    def observe(self, connection) -> str:
        stat = self.file.stat()
        return ArchiveCatalog(connection).observe_local_copy(
            vault_id=2, path="report.txt", file_type="regular", size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())

    def write(self, payload: bytes, *, mtime_ns: int | None = None) -> None:
        self.file.write_bytes(payload)
        if mtime_ns is not None:
            os.utime(self.file, ns=(mtime_ns, mtime_ns))
        with self.db() as connection:
            self.observe(connection)

    def queue(self, mode: str = "s3_checksum") -> int:
        """One manual upload Job with its upload detail, as admission writes it."""
        with self.db() as connection:
            job = connection.execute(
                "INSERT INTO jobs(vault_id, file_id, action, status, origin, total_bytes) "
                "VALUES (2, %s, 'upload', 'queued', 'manual', %s) RETURNING id",
                (self.file_id, self.file.stat().st_size)).fetchone()
            connection.execute("INSERT INTO job_uploads(job_id, verification_mode) VALUES (%s, %s)",
                               (job["id"], mode))
        return int(job["id"])

    def claim(self, job_id: int) -> dict:
        with self.db() as connection:
            claimed = ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=str(uuid.uuid4()), claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed, "the Job must be claimable")
            job = connection.execute(
                """SELECT j.*, f.path, v.source_root, v.uuid AS vault_uuid, v.name AS vault_name,
                          v.lifecycle AS vault_lifecycle, t.bucket AS s3_bucket
                   FROM jobs j JOIN files f ON f.id=j.file_id JOIN vaults v ON v.id=j.vault_id
                   JOIN vault_targets t ON t.id=v.target_id WHERE j.id=%s""", (job_id,)).fetchone()
        return {**job, "claim_token": claimed["claim_token"],
                "claim_expires_at": claimed["claim_expires_at"]}

    def run_job(self, job_id: int) -> bool:
        return storage.process_job(self.claim(job_id))

    def due(self, job_id: int) -> None:
        """Make a retrying Job runnable now (its backoff is not under test)."""
        with self.db() as connection:
            connection.execute("UPDATE jobs SET run_after=now() - interval '1 second' WHERE id=%s",
                               (job_id,))

    def abandon(self, job_id: int) -> None:
        """End a Job without completing it (another owner gave up on it)."""
        with self.db() as connection:
            connection.execute("DELETE FROM job_leases WHERE job_id=%s", (job_id,))
            connection.execute("UPDATE jobs SET status='failed', finished_at=now() WHERE id=%s",
                               (job_id,))

    def job(self, job_id: int) -> dict:
        with self.db() as connection:
            return connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def file_row(self) -> dict:
        with self.db() as connection:
            return connection.execute("SELECT * FROM files WHERE id=%s", (self.file_id,)).fetchone()

    def rows(self, sql: str, *params) -> list[dict]:
        with self.db() as connection:
            return connection.execute(sql, params).fetchall()

    def version(self, version_id) -> dict:
        return self.rows("SELECT * FROM archive_versions WHERE id=%s", version_id)[0]

    def count_heads(self) -> None:
        previous = self.provider.hook

        def hook(stage, **kwargs):
            if stage == "head":
                self.heads += 1
            previous(stage, **kwargs)
        self.provider.hook = hook

    def assert_current(self, job_id: int, payload: bytes, *, matched: bool = True) -> dict:
        job = self.job(job_id)
        self.assertEqual(job["status"], "completed", job["message"])
        row = self.file_row()
        version = self.version(row["current_version_id"])
        self.assertEqual(str(job["archive_version_id"]), str(row["current_version_id"]))
        self.assertIsNone(row["replacing_version_id"])
        self.assertEqual((version["integrity"], version["availability"]), ("verified", "available"))
        self.assertEqual(bytes(version["plaintext_sha256"]), sha(payload))
        self.assertEqual(version["object_key"], self.key)
        self.assertEqual(self.provider.objects[self.key, version["provider_version_id"]][0], payload)
        if matched:
            self.assertEqual(row["matched_version_id"], row["current_version_id"])
            self.assertEqual(bytes(row["local_sha256"]), sha(payload))
        return version

    # -- both Upload Verification Modes -------------------------------------

    def test_s3_checksum_mode_proves_the_digest_without_reading_back(self) -> None:
        job_id = self.queue("s3_checksum")
        self.assertTrue(self.run_job(job_id))
        version = self.assert_current(job_id, A)
        self.assertEqual((version["verification_method"], version["verification_fallback"],
                          version["verification_egress_bytes"]), ("s3_checksum", None, 0))
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 0, 0))
        attempt = self.rows("SELECT * FROM upload_attempts")
        self.assertEqual([(a["outcome"], a["landed_version_id"]) for a in attempt],
                         [("landed", version["provider_version_id"])])
        self.assertEqual(str(version["upload_attempt_id"]), str(attempt[0]["id"]))
        self.assertEqual(len(self.rows("SELECT * FROM upload_receipts")), 1)
        snapshot = self.rows("SELECT * FROM upload_snapshots")[0]
        self.assertIsNotNone(snapshot["closed_at"])
        self.assertEqual(snapshot["verification"], "s3_checksum")
        self.assertEqual(bytes(snapshot["plaintext_sha256"]), sha(A))

    def test_read_back_mode_reads_the_exact_version_once(self) -> None:
        job_id = self.queue("read_back")
        self.run_job(job_id)
        version = self.assert_current(job_id, A)
        self.assertEqual((version["verification_method"], version["verification_fallback"],
                          version["verification_egress_bytes"]), ("read_back", None, len(A)))
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 1))

    def test_missing_provider_checksum_falls_back_to_read_back(self) -> None:
        head = self.provider.head_object

        def without_checksum(**kwargs):
            response = head(**kwargs)
            response.pop("ChecksumSHA256")
            return response
        self.provider.head_object = without_checksum
        job_id = self.queue("s3_checksum")
        self.run_job(job_id)
        version = self.assert_current(job_id, A)
        self.assertEqual((version["verification_method"], version["verification_fallback"],
                          version["verification_egress_bytes"]),
                         ("read_back", "checksum_missing", len(A)))
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 1))

    def test_contradicting_proof_never_becomes_current(self) -> None:
        def corrupt(stage, **kwargs):
            if stage == "accepted":
                data, metadata = self.provider.objects[self.key, kwargs["version"]]
                self.provider.objects[self.key, kwargs["version"]] = (b"X" * len(data), metadata)
        self.provider.hook = corrupt
        job_id = self.queue("read_back")
        self.run_job(job_id)
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        row = self.file_row()
        self.assertIsNone(row["current_version_id"])
        self.assertIsNone(row["replacing_version_id"])
        self.assertEqual(row["reconciliation_block"], "proof_mismatch")
        version = self.rows("SELECT * FROM archive_versions")[0]
        self.assertEqual(version["integrity"], "mismatch")
        self.assertEqual(self.provider.gets, 0, "contradictory checksum evidence needs no read-back")

    # -- expected proof: Current only, never a historical object -------------

    def test_unchanged_content_is_matched_to_current_without_a_send(self) -> None:
        first = self.queue()
        self.run_job(first)
        current = self.assert_current(first, A)
        # A new observation drops the match; the next Job proves it again.
        os.utime(self.file, ns=(1_700_000_000_000_000_000,) * 2)
        with self.db() as connection:
            self.observe(connection)
        self.assertIsNone(self.file_row()["matched_version_id"])
        second = self.queue()
        self.run_job(second)
        self.assertEqual(self.job(second)["status"], "completed")
        self.assertIsNone(self.job(second)["archive_version_id"])
        row = self.file_row()
        self.assertEqual((row["current_version_id"], row["matched_version_id"]),
                         (current["id"], current["id"]))
        self.assertEqual(self.provider.puts, 1)

    def test_content_equal_to_an_older_version_is_sent_again(self) -> None:
        first = self.queue()
        self.run_job(first)
        old = self.assert_current(first, A)
        self.write(B)
        second = self.queue()
        self.run_job(second)
        self.assert_current(second, B)
        self.write(A)
        third = self.queue()
        self.run_job(third)
        current = self.assert_current(third, A)
        self.assertNotEqual(current["id"], old["id"], "an older version is never elected")
        self.assertEqual(self.provider.puts, 3)
        attempts = self.rows("SELECT a.token FROM upload_attempts a "
                             "JOIN archive_versions av ON av.upload_attempt_id=a.id WHERE av.id=%s",
                             current["id"])
        self.assertEqual(self.provider.objects[self.key, current["provider_version_id"]][1]
                         [provenance.TOKEN_KEY], str(attempts[0]["token"]))

    # -- receipts and materialization never publish by themselves -------------

    def test_materialized_version_waits_for_verification_and_another_job_finishes_it(self) -> None:
        heads = []

        def fail_verification(stage, **kwargs):
            # The first HEAD answers the PUT itself; later ones verify.
            if stage == "head" and heads.append(stage) is None and len(heads) > 1:
                raise ConnectionError("connection reset during verification")
        self.provider.hook = fail_verification
        original = self.queue("read_back")
        self.run_job(original)
        self.assertNotEqual(self.job(original)["status"], "completed")
        row = self.file_row()
        self.assertIsNone(row["current_version_id"])
        replacing = self.version(row["replacing_version_id"])
        self.assertEqual(replacing["integrity"], "unverified")
        self.assertEqual(len(self.rows("SELECT * FROM upload_receipts")), 1)
        self.assertEqual(self.rows("SELECT outcome FROM upload_attempts")[0]["outcome"], "landed")
        # The original Job is gone; a new Job verifies the same exact version.
        self.abandon(original)
        self.provider.hook = lambda stage, **kwargs: None
        recovery = self.queue("s3_checksum")
        self.run_job(recovery)
        current = self.assert_current(recovery, A)
        self.assertEqual(current["id"], replacing["id"])
        # The version keeps its own snapshot's mode, not the new Job's.
        self.assertEqual(current["verification_method"], "read_back")
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 1))

    def test_lost_claim_at_publication_keeps_receipt_and_publishes_nothing(self) -> None:
        heads = []

        def steal(stage, **kwargs):
            # After the PUT's own HEAD: the version is materialized by then.
            if stage == "head" and heads.append(stage) is None and len(heads) == 2:
                with self.db() as connection:
                    connection.execute("UPDATE job_leases SET token=gen_random_uuid()")
        self.provider.hook = steal
        job_id = self.queue("s3_checksum")
        self.assertFalse(self.run_job(job_id))
        row = self.file_row()
        self.assertIsNone(row["current_version_id"])
        self.assertIsNone(row["matched_version_id"])
        version = self.version(row["replacing_version_id"])
        self.assertEqual(version["integrity"], "unverified")
        self.assertEqual(len(self.rows("SELECT * FROM upload_receipts")), 1)
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        self.abandon(job_id)
        self.provider.hook = lambda stage, **kwargs: None
        recovery = self.queue("s3_checksum")
        self.run_job(recovery)
        self.assertEqual(self.assert_current(recovery, A)["id"], version["id"])
        self.assertEqual(self.provider.puts, 1)

    def test_stale_local_observation_keeps_current_but_not_the_match(self) -> None:
        def newer_observation(stage, **kwargs):
            if stage == "accepted":
                os.utime(self.file, ns=(1_700_000_000_000_000_000,) * 2)
                with self.db() as connection:
                    self.observe(connection)
        self.provider.hook = newer_observation
        before = self.file_row()["generation"]
        job_id = self.queue("s3_checksum")
        self.run_job(job_id)
        self.assert_current(job_id, A, matched=False)
        row = self.file_row()
        self.assertIsNone(row["matched_version_id"])
        self.assertIsNone(row["local_sha256"], "the digest of an older observation is discarded")
        self.assertGreater(row["generation"], before)
        snapshot = self.rows("SELECT local_generation FROM upload_snapshots")[0]
        self.assertEqual(snapshot["local_generation"], before, "the pre-hash observation is kept")
        self.assertEqual(len(self.rows("SELECT * FROM upload_receipts")), 1)

    def test_hash_of_a_file_changed_after_observation_is_refused(self) -> None:
        job_id = self.queue()
        prepare = storage.prepare_upload_snapshot

        def touched(source, check):
            snapshot, stat = prepare(source, check)
            os.utime(self.file, ns=(stat.st_mtime_ns + 10**9,) * 2)
            return snapshot, self.file.stat()
        with patch.object(storage, "prepare_upload_snapshot", side_effect=touched):
            self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "retrying")
        self.assertEqual(self.provider.puts, 0)
        self.assertEqual(self.rows("SELECT count(*) AS n FROM upload_attempts")[0]["n"], 0)

    # -- DB retries never replay provider effects ------------------------------

    def test_serialization_failure_at_commit_retries_only_the_database(self) -> None:
        self.count_heads()
        clean = self.queue("read_back")
        self.run_job(clean)
        self.assert_current(clean, A)
        baseline = (self.provider.puts, self.provider.gets, self.heads)
        self.write(B)
        plan = {"INSERT INTO upload_attempts": 1, "SET send_started_at": 1,
                "INSERT INTO archive_versions": 1, "SET current_version_id": 1,
                "INSERT INTO upload_receipts": 1}
        hits: list[str] = []
        real = storage.db
        self.enterContext(patch.object(storage, "db", side_effect=lambda: _FlakyCommit(real(), plan, hits)))
        retried = self.queue("read_back")
        self.run_job(retried)
        self.assert_current(retried, B)
        self.assertEqual(sorted(set(hits)), sorted(plan), "every unit must hit one commit failure")
        self.assertEqual((self.provider.puts, self.provider.gets, self.heads),
                         tuple(2 * count for count in baseline))
        self.assertEqual(len(self.rows("SELECT * FROM upload_attempts")), 2)
        self.assertEqual(len(self.rows("SELECT * FROM upload_receipts")), 2)

    # -- uncertain sends reconcile, never send twice --------------------------

    def test_ambiguous_send_reconciles_from_s3_without_a_second_put(self) -> None:
        def crash_after_put(stage, **kwargs):
            if stage == "accepted":
                raise RuntimeError("connection reset after the provider stored the object")
        self.provider.hook = crash_after_put
        job_id = self.queue("s3_checksum")
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "retrying")
        self.assertEqual(self.rows("SELECT * FROM upload_receipts"), [])
        attempt = self.rows("SELECT * FROM upload_attempts")[0]
        self.assertIsNotNone(attempt["send_started_at"])
        self.assertIsNone(attempt["settled_at"])
        self.assertIsNone(self.file_row()["replacing_version_id"])
        self.provider.hook = lambda stage, **kwargs: None
        self.due(job_id)
        self.run_job(job_id)
        self.assert_current(job_id, A)
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.rows("SELECT outcome FROM upload_attempts")[0]["outcome"], "landed")

    def test_proven_absence_sends_a_new_attempt_of_the_same_snapshot(self) -> None:
        def crash_before_put(stage, **kwargs):
            if stage == "before_put":
                raise ConnectionError("connection reset before the request left")
        self.provider.hook = crash_before_put
        job_id = self.queue("s3_checksum")
        self.run_job(job_id)
        self.assertEqual(self.provider.puts, 0)
        self.provider.hook = lambda stage, **kwargs: None
        self.due(job_id)
        self.run_job(job_id)
        self.assert_current(job_id, A)
        attempts = self.rows("SELECT * FROM upload_attempts ORDER BY created_at")
        self.assertEqual([(a["outcome"], a["no_write_operation"]) for a in attempts],
                         [("no_write", provenance.ABSENT_OPERATION), ("landed", None)])
        self.assertEqual(attempts[0]["snapshot_id"], attempts[1]["snapshot_id"])
        self.assertNotEqual(attempts[0]["token"], attempts[1]["token"])
        self.assertEqual(len(self.rows("SELECT * FROM upload_snapshots")), 1)
        self.assertEqual(self.provider.puts, 1)

    def test_a_new_job_adopts_the_outstanding_attempt_of_the_file(self) -> None:
        def crash_after_put(stage, **kwargs):
            if stage == "accepted":
                raise RuntimeError("connection reset")
        self.provider.hook = crash_after_put
        original = self.queue("s3_checksum")
        self.run_job(original)
        self.abandon(original)
        self.provider.hook = lambda stage, **kwargs: None
        adopter = self.queue("read_back")
        self.run_job(adopter)
        version = self.assert_current(adopter, A)
        self.assertEqual(version["verification_method"], "s3_checksum")
        self.assertEqual(self.provider.puts, 1)
        detail = self.rows("SELECT * FROM job_uploads WHERE job_id=%s", adopter)[0]
        self.assertFalse(detail["reconcile_only"])
        self.assertEqual(detail["snapshot_id"], self.rows("SELECT id FROM upload_snapshots")[0]["id"])

    # -- admission -------------------------------------------------------------

    def test_bulk_admission_queues_files_existing_at_request_time(self) -> None:
        from app.services import bulk_upload_admission as bulk
        (self.source / "docs").mkdir()
        nested = self.source / "docs" / "a.txt"
        nested.write_bytes(B)
        with self.db() as connection:
            stat = nested.stat()
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path="docs/a.txt", file_type="regular", size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())
            connection.execute(
                "INSERT INTO vault_operation_policies(vault_id, upload_verification) "
                "VALUES (2, 'read_back')")
            status = bulk.create(connection, vault_id=2, path="", user_id=1)
        self.assertEqual((status["status"], status["selected_count"]), ("pending", 2))
        with self.db() as connection:
            # Created after acceptance: outside this admission.
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path="later.txt", file_type="regular", size=1, mtime_ns=1,
                observed_at=storage.now_iso())
        with self.db() as connection:
            self.assertTrue(bulk.advance(connection, vault_id=2, group_id=status["group_id"],
                                         batch_size=1))
        with self.db() as connection:
            self.assertTrue(bulk.advance(connection, vault_id=2, group_id=status["group_id"],
                                         batch_size=1))
        with self.db() as connection:
            self.assertTrue(bulk.advance(connection, vault_id=2, group_id=status["group_id"],
                                         batch_size=1))
            final = bulk.get(connection, vault_id=2, group_id=status["group_id"])
        self.assertEqual((final["status"], final["processed_count"], final["admitted_count"]),
                         ("completed", 2, 2))
        jobs = self.rows("SELECT j.id, f.path, ju.verification_mode FROM jobs j "
                         "JOIN files f ON f.id=j.file_id JOIN job_uploads ju ON ju.job_id=j.id "
                         "WHERE j.group_id=%s ORDER BY f.path", status["group_id"])
        self.assertEqual([(j["path"], j["verification_mode"]) for j in jobs],
                         [("docs/a.txt", "read_back"), ("report.txt", "read_back")])
        for job in jobs:
            self.run_job(job["id"])
            self.assertEqual(self.job(job["id"])["status"], "completed")
        self.assertEqual((self.provider.puts, self.provider.gets), (2, 2))

    def test_automatic_admission_skips_a_file_already_matched_to_current(self) -> None:
        from app.services.operation_policies import queue_auto_uploads
        old = 1_600_000_000_000_000_000
        os.utime(self.file, ns=(old, old))
        with self.db() as connection:
            self.observe(connection)
            connection.execute("UPDATE files SET local_content_observed_at=now() - interval '1 day', "
                               "local_observed_at=now() - interval '1 day'")
            self.assertEqual(queue_auto_uploads(connection, vault_id=2, source_root=str(self.source)), 1)
        job_id = self.rows("SELECT id FROM jobs WHERE origin='automatic'")[0]["id"]
        self.run_job(job_id)
        self.assert_current(job_id, A)
        with self.db() as connection:
            self.assertEqual(queue_auto_uploads(connection, vault_id=2, source_root=str(self.source)), 0)


if __name__ == "__main__":
    unittest.main()
