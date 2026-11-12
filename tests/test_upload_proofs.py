"""Upload proofs on the rebuilt schema (#588, ADR-0035).

Seams: the DB functions of ``app.services.upload_provenance`` (snapshot,
target, attempts, receipts, multipart journal) and the ``upload_*`` tables of
``0001_baseline`` on a disposable PostgreSQL database. The snapshot and its
target are stored once; attempts and receipts are independent of the Job.
No provider or filesystem effect is involved: provider reconciliation keeps its
own database-free tests (``test_upload_provenance``).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import psycopg

from app.catalog import ArchiveCatalog
from app.services import upload_provenance as provenance
from app.services.upload_provenance import UploadBlocked
from app.services.upload_snapshot import (
    DEFAULT_PART_SIZE, MULTIPART_THRESHOLD, UploadSnapshot, prepare_upload_snapshot,
)
from tests.catalog_support import seed_vault
from tests.postgres import PostgresConnection, run_alembic

T0 = "2026-07-21T10:00:00+00:00"
T1 = "2026-07-21T11:00:00+00:00"
T2 = "2026-07-21T12:00:00+00:00"
PROVIDER = '{"endpoint":"https://s3.eu-south-1.amazonaws.com","region":"eu-south-1"}'
EMPTY = {"versions": [], "markers": []}
MTIME_NS = 1_750_000_000_123_456_789


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database_path = self.root / "proofs.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connection = PostgresConnection(str(self.database_path))
        self.addCleanup(self.connection.close)
        seed_vault(self.connection)
        self.catalog = ArchiveCatalog(self.connection)
        self.snapshot = self.hashed(b"content-a")
        self.file_id = self.catalog.observe_local_copy(
            vault_id=2, path="a.txt", file_type="regular", size=self.snapshot.size,
            mtime_ns=MTIME_NS, observed_at=T0)
        self.job = self.new_job()
        uuid = self.connection.execute("SELECT uuid FROM vaults WHERE id=2").fetchone()["uuid"]
        self.key = f"vaults/{uuid}/a.txt"
        self.connection.commit()

    # -- helpers -----------------------------------------------------------

    def hashed(self, payload: bytes, mtime_ns: int = MTIME_NS) -> UploadSnapshot:
        source = self.root / f"source-{len(payload)}-{payload[-1]}"
        source.write_bytes(payload)
        os.utime(source, ns=(mtime_ns, mtime_ns))
        snapshot, _ = prepare_upload_snapshot(source, lambda: None)
        return snapshot

    def new_job(self, *, file_id: str | None = None, vault_id: int = 2,
                status: str = "running") -> dict:
        file_id = file_id or self.file_id
        row = self.connection.execute(
            "INSERT INTO jobs(vault_id, file_id, action, status, origin) "
            "VALUES (%s, %s, 'upload', %s, 'automatic') RETURNING id",
            (vault_id, file_id, status)).fetchone()
        return {"id": row["id"], "vault_id": vault_id, "file_id": file_id}

    def generation(self) -> int:
        return self.connection.execute("SELECT generation FROM files WHERE id=%s",
                                       (self.file_id,)).fetchone()["generation"]

    def pin(self, job: dict | None = None) -> dict:
        return provenance.target(self.connection, job or self.job, key=self.key, provider=PROVIDER)

    def prepare(self, job: dict | None = None, *, snapshot: UploadSnapshot | None = None,
                before: dict = EMPTY) -> provenance.UploadAttempt:
        job = job or self.job
        return provenance.prepare(
            self.connection, job, pinned=self.pin(job), snapshot=snapshot or self.snapshot,
            requested="s3_checksum", local_generation=self.generation(), before=before,
            timestamp=T1)

    def started(self) -> provenance.UploadAttempt:
        attempt = provenance.mark_send_started(self.connection, self.job, self.prepare())
        self.connection.commit()
        return attempt

    def db(self):
        return PostgresConnection(str(self.database_path))

    def count(self, table: str, where: str = "true", params: tuple = ()) -> int:
        return self.connection.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}",
                                       params).fetchone()["n"]

    def rejects(self, statement: str, params=(), error=psycopg.errors.IntegrityError) -> None:
        self.connection.execute("SAVEPOINT rejected")
        with self.assertRaises(error):
            self.connection.execute(statement, params)
        self.connection.execute("ROLLBACK TO SAVEPOINT rejected")

    def blocked(self, reason: str):
        test = self

        class _Blocked:
            def __enter__(self):
                self.context = test.assertRaises(UploadBlocked)
                return self.context.__enter__()

            def __exit__(self, *exc):
                handled = self.context.__exit__(*exc)
                test.assertEqual(self.context.exception.reason, reason)
                test.connection.rollback()
                return handled

        return _Blocked()

    def land(self, attempt: provenance.UploadAttempt, version: str = "v1") -> str:
        provenance.append_receipt(self.db, attempt.id, {"VersionId": version}, T1)
        provenance.record_landed(self.connection, self.job, attempt, version, T1)
        return self.catalog.begin_replacement(
            self.file_id, object_key=self.key, provider_version_id=version,
            size=self.snapshot.size, upload_attempt_id=attempt.id, created_at=T1)


class SnapshotOnceTests(_Fixture):
    def test_jobs_and_versions_carry_no_copy_of_snapshot_target_or_digest(self) -> None:
        columns = {
            (row["table_name"], row["column_name"])
            for row in self.connection.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name IN ('jobs','archive_versions')"
            ).fetchall()
        }
        names = {name for _, name in columns}
        for duplicated in ("upload_snapshot_json", "upload_plaintext_sha256", "expected_sha256",
                           "upload_verification_requested", "target_json", "snapshot_json",
                           "token", "part_sha256", "mtime_ns", "verification", "path"):
            self.assertNotIn(duplicated, names)
        self.assertIn(("archive_versions", "upload_attempt_id"), columns)

    def test_the_snapshot_and_its_target_are_written_once_for_every_attempt(self) -> None:
        attempt = self.prepare()
        self.assertEqual(provenance.prepare(self.connection, self.job, pinned=self.pin(),
                         snapshot=self.snapshot, requested="s3_checksum",
                         local_generation=self.generation(), before=EMPTY, timestamp=T2).id,
                         attempt.id, "a DB retry reuses the attempt")
        self.assertEqual(provenance.bound_snapshot(self.connection, self.job["id"]).id,
                         attempt.admitted.id)
        self.assertEqual(attempt.target, self.pin())
        self.assertEqual(attempt.target["key"], self.key)
        attempt = provenance.mark_send_started(self.connection, self.job, attempt)
        provenance.record_no_write(self.connection, self.job, attempt, provenance.ABSENT_OPERATION, T1)
        # S3 proved nothing landed: the next send is a new attempt and token
        # on the very same snapshot and target.
        again = self.prepare(before={"versions": ["older"], "markers": []})
        self.assertNotEqual((again.id, again.token), (attempt.id, attempt.token))
        self.assertEqual(again.admitted.id, attempt.admitted.id)
        self.assertEqual(again.inventory, {"versions": ["older"], "markers": []})
        self.assertEqual(self.count("upload_snapshots"), 1)
        self.assertEqual(self.count("upload_attempts"), 2)
        again = provenance.mark_send_started(self.connection, self.job, again)
        self.connection.commit()
        version = self.land(again, "v2")
        expected = provenance.expected_for_version(self.connection, version)
        self.assertEqual((expected.id, expected.snapshot), (attempt.admitted.id, self.snapshot))
        stored = self.connection.execute(
            "SELECT plaintext_sha256, integrity FROM archive_versions WHERE id=%s", (version,)).fetchone()
        self.assertEqual((stored["plaintext_sha256"], stored["integrity"]), (None, "unverified"))
        self.assertTrue(self.catalog.promote_replacement(
            self.file_id, archive_version_id=version, plaintext_sha256=self.snapshot.plaintext_sha256,
            verified_at=T2, local_generation=expected.local_generation) is not None)

    def test_the_stored_snapshot_keeps_mtime_layout_and_expected_checksum(self) -> None:
        attempt = self.prepare()
        self.connection.commit()
        loaded = provenance.load_for_job(self.connection, self.job["id"])
        self.assertEqual(loaded.snapshot, self.snapshot)
        self.assertEqual(loaded.snapshot.mtime_ns, MTIME_NS)
        self.assertEqual(loaded.snapshot.file_metadata(), self.snapshot.file_metadata())
        self.assertEqual(loaded.admitted.verification, "s3_checksum")
        self.assertEqual(loaded.token, attempt.token)
        parts = tuple(UploadSnapshot.from_record(
            size=MULTIPART_THRESHOLD + 1, plaintext_sha256=bytes(32), part_size=DEFAULT_PART_SIZE,
            part_sha256=[bytes([1]) * 32, bytes([2]) * 32], mtime_ns=-1).part_checksums)
        composite = UploadSnapshot.from_record(
            size=MULTIPART_THRESHOLD + 1, plaintext_sha256=bytes(32), part_size=DEFAULT_PART_SIZE,
            part_sha256=[bytes([1]) * 32, bytes([2]) * 32], mtime_ns=-1)
        self.assertEqual((composite.checksum_type, composite.part_checksums), ("COMPOSITE", parts))
        self.assertTrue(composite.checksum_sha256.endswith("-2"))
        self.assertEqual(UploadSnapshot.from_record(**composite.record()), composite)
        with self.assertRaises(ValueError):
            UploadSnapshot.from_record(**{**composite.record(), "part_sha256": [bytes(32)]})
        with self.assertRaises(ValueError):
            UploadSnapshot(**{**self.snapshot.__dict__, "mtime_ns": None}).record()

    def test_the_target_is_frozen_and_fenced_by_live_state(self) -> None:
        attempt = self.prepare()
        self.connection.commit()
        with self.blocked("identity_changed"):
            provenance.target(self.connection, self.job, key=self.key + ".other", provider=PROVIDER)
        self.connection.execute("UPDATE files SET path='b.txt', path_since=%s WHERE id=%s",
                                (T2, self.file_id))
        with self.blocked("identity_changed"):
            provenance.mark_send_started(self.connection, self.job, attempt)
        self.connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=2")
        with self.blocked("vault_suspended"):
            self.pin()
        other = self.new_job(status="queued")
        with self.blocked("active_operation"):
            self.pin()
        self.connection.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (self.job["id"],))
        with self.blocked("owner_cancelled"):
            self.pin()
        self.connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (self.job["id"],))
        with self.blocked("already_resolved"):
            self.pin()
        self.assertIsNotNone(other)

    def test_a_changed_content_closes_the_unsent_snapshot_never_rewrites_it(self) -> None:
        first = self.prepare()
        self.connection.commit()
        changed = self.hashed(b"content-b")
        with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
            self.prepare(snapshot=changed)
        self.connection.rollback()
        self.assertTrue(provenance.close_unsent_for_job(self.connection, self.job, T2))
        self.assertIsNone(provenance.bound_snapshot(self.connection, self.job["id"]))
        second = self.prepare(snapshot=changed)
        self.connection.commit()
        self.assertNotEqual((second.admitted.id, second.token), (first.admitted.id, first.token))
        old = self.connection.execute(
            "SELECT s.closed_at, a.outcome, s.plaintext_sha256 FROM upload_snapshots s "
            "JOIN upload_attempts a ON a.snapshot_id=s.id WHERE s.id=%s", (first.admitted.id,)).fetchone()
        self.assertIsNotNone(old["closed_at"])
        self.assertEqual(old["outcome"], "unsent")
        self.assertEqual(old["plaintext_sha256"].hex(), self.snapshot.plaintext_sha256)

    def test_an_abandoned_unsent_snapshot_does_not_block_the_next_upload(self) -> None:
        stale = self.prepare()
        self.connection.execute("UPDATE jobs SET status='failed' WHERE id=%s", (self.job["id"],))
        self.connection.commit()
        job = self.new_job()
        fresh = self.prepare(job, snapshot=self.hashed(b"content-c"))
        self.connection.commit()
        self.assertNotEqual(fresh.admitted.id, stale.admitted.id)
        self.assertEqual(provenance.load_attempt(self.connection, stale.id).outcome, "unsent")


class SendStartTests(_Fixture):
    def test_send_start_is_committed_before_the_effect_and_authorizes_nothing_more(self) -> None:
        attempt = self.prepare()
        self.connection.commit()
        self.assertTrue(attempt.may_send)
        sending = provenance.mark_send_started(self.connection, self.job, attempt)
        self.assertFalse(sending.may_send)
        with self.db() as other:
            row = other.execute("SELECT send_started_at FROM upload_attempts WHERE id=%s",
                                (attempt.id,)).fetchone()
            self.assertIsNone(row["send_started_at"], "not durable until the caller commits")
        self.connection.commit()
        with self.db() as other:
            self.assertIsNotNone(other.execute(
                "SELECT send_started_at FROM upload_attempts WHERE id=%s", (attempt.id,)
            ).fetchone()["send_started_at"])
        # A committed send-start never admits a second send of the attempt.
        with self.blocked("ambiguous_send"):
            provenance.mark_send_started(self.connection, self.job, attempt)
        loaded = provenance.load_for_job(self.connection, self.job["id"])
        self.assertTrue(loaded.send_started)
        self.assertFalse(loaded.may_send)
        with self.blocked("ambiguous_send"):
            self.prepare()
        with self.blocked("ambiguous_send"):
            self.prepare(before={"versions": ["changed"], "markers": []})
        self.assertFalse(provenance.close_unsent_for_job(self.connection, self.job, T2))
        self.assertEqual(provenance.outstanding(self.connection, self.file_id).id, attempt.id)
        self.rejects("UPDATE upload_attempts SET send_started_at=NULL WHERE id=%s", (attempt.id,))

    def test_one_open_attempt_per_file_and_distinct_tokens(self) -> None:
        attempt = self.prepare()
        self.rejects(
            "INSERT INTO upload_attempts(snapshot_id, file_id, inventory_versions, inventory_markers) "
            "VALUES (%s, %s, '{}', '{}')", (attempt.admitted.id, self.file_id))
        self.rejects(
            "INSERT INTO upload_attempts(snapshot_id, file_id, token, inventory_versions, "
            "inventory_markers, settled_at, outcome) VALUES (%s, %s, %s, '{}', '{}', now(), 'unsent')",
            (attempt.admitted.id, self.file_id, attempt.token))
        self.rejects(
            "INSERT INTO upload_attempts(snapshot_id, file_id, inventory_versions, inventory_markers, "
            "send_started_at) VALUES (%s, %s, '{}', '{}', now())", (attempt.admitted.id, self.file_id))

    def test_a_reconcile_only_job_binds_the_snapshot_and_never_resurrects_a_cancelled_one(self) -> None:
        attempt = self.started()
        self.connection.execute("UPDATE jobs SET status='failed' WHERE id=%s", (self.job["id"],))
        resume = self.new_job(status="queued")
        provenance.bind_for_resume(self.connection, resume["id"], attempt)
        self.assertEqual(provenance.load_for_job(self.connection, resume["id"]).id, attempt.id)
        self.assertTrue(self.connection.execute(
            "SELECT reconcile_only FROM upload_snapshot_jobs WHERE job_id=%s", (resume["id"],)
        ).fetchone()["reconcile_only"])
        self.assertFalse(provenance.close_unsent_for_job(self.connection, resume, T2))
        self.connection.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (resume["id"],))
        another = self.new_job(status="queued")
        with self.blocked("identity_changed"):
            provenance.bind_for_resume(self.connection, another["id"], attempt)


class ReceiptTests(_Fixture):
    def test_receipts_survive_cancel_rollback_commit_error_and_job_collection(self) -> None:
        attempt = self.started()
        self.connection.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (self.job["id"],))
        self.connection.commit()
        provenance.append_receipt(self.db, attempt.id, {"VersionId": "v1"}, T1)
        # The worker's own unit rolls back; the journal committed on its own.
        self.connection.execute("UPDATE jobs SET message='x' WHERE id=%s", (self.job["id"],))
        provenance.append_receipt(self.db, attempt.id, {"VersionId": "v1", "ETag": '"e"'}, T1)
        self.connection.rollback()
        # COMMIT reports an error after the row was committed: only the DB
        # unit is retried, and the fact stays one row.
        calls = []

        class LostCommit:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, *exc):
                self.inner.__exit__(*exc)
                if not calls:
                    calls.append(1)
                    raise psycopg.errors.SerializationFailure("commit outcome unknown")
                return False

            def close(self):
                self.inner.close()

        provenance.append_receipt(lambda: LostCommit(self.db()), attempt.id,
                                  {"VersionId": "v1", "ChecksumSHA256": "c"}, T1)
        self.assertEqual(calls, [1])
        self.assertEqual(self.count("upload_receipts", "attempt_id=%s", (attempt.id,)), 3)
        # Retention collects the original Job: the attempt and receipts stay.
        self.connection.execute("DELETE FROM jobs WHERE id=%s", (self.job["id"],))
        self.connection.commit()
        kept = provenance.load_attempt(self.connection, attempt.id)
        self.assertIsNone(kept.job_id)
        self.assertEqual(self.count("upload_snapshot_jobs"), 0)
        self.assertEqual(provenance.receipt(self.connection, attempt.id),
                         {"VersionId": "v1", "ETag": '"e"', "ChecksumSHA256": "c"})
        self.assertEqual(provenance.outstanding(self.connection, self.file_id).id, attempt.id)

    def test_the_journal_deduplicates_facts_and_the_probe_writes_nothing(self) -> None:
        attempt = self.started()
        journal = provenance.ReceiptJournal(self.db, attempt.id, lambda: T1)
        journal({"VersionId": "v1", "Server": "AmazonS3", "x-amz-request-id": "secret"})
        journal({"VersionId": "v1"})
        provenance.append_receipt(self.db, attempt.id, {"VersionId": "v1", "Set-Cookie": "c"}, T2)
        rows = self.connection.execute("SELECT response FROM upload_receipts").fetchall()
        self.assertEqual([row["response"] for row in rows], [{"VersionId": "v1"}])
        provenance.probe_receipt_journal(self.connection)
        self.connection.commit()
        self.assertEqual(self.count("upload_receipts"), 1)


class NoWriteTests(_Fixture):
    def test_no_write_settles_without_erasing_history_or_becoming_a_receipt(self) -> None:
        attempt = self.started()
        provenance.journal_multipart(self.db, attempt.id, "mpu-1", T1)
        with self.blocked("ambiguous_send"):
            provenance.record_no_write(self.connection, self.job, attempt, "HeadObject", T1)
        settled = provenance.record_no_write(self.connection, self.job, attempt,
                                             provenance.ABSENT_OPERATION, T1)
        self.connection.commit()
        self.assertEqual((settled.outcome, settled.send_started), ("no_write", True))
        row = self.connection.execute("SELECT * FROM upload_attempts WHERE id=%s", (attempt.id,)).fetchone()
        self.assertIsNotNone(row["send_started_at"], "the send-start stays as history")
        self.assertEqual(row["no_write_operation"], provenance.ABSENT_OPERATION)
        self.assertEqual(self.count("upload_receipts"), 0)
        self.assertIsNone(provenance.receipt(self.connection, attempt.id))
        self.assertTrue(provenance.proven_absent(self.connection, attempt.id))
        self.assertTrue(provenance.unsent(self.connection, attempt.id))
        self.assertEqual(provenance.open_multiparts(self.connection, attempt.id), [])
        self.assertIsNone(provenance.outstanding(self.connection, self.file_id))
        self.assertIsNone(provenance.load_for_job(self.connection, self.job["id"]))
        with self.blocked("ambiguous_receipt"):
            provenance.record_no_write(self.connection, self.job, settled, "PutObject", T2)

    def test_a_receipt_forbids_a_no_write_outcome(self) -> None:
        attempt = self.started()
        provenance.append_receipt(self.db, attempt.id, {"VersionId": "v1"}, T1)
        with self.blocked("ambiguous_receipt"):
            provenance.record_no_write(self.connection, self.job, attempt, "PutObject", T1)

    def test_a_late_contradiction_blocks_even_after_the_settlement(self) -> None:
        attempt = self.started()
        provenance.record_no_write(self.connection, self.job, attempt, "PutObject", T1)
        self.connection.commit()
        provenance.append_receipt(self.db, attempt.id, {"VersionId": "late"}, T2)
        with self.blocked("ambiguous_receipt"):
            provenance.receipt(self.connection, attempt.id)
        with self.blocked("ambiguous_receipt"):
            provenance.outstanding(self.connection, self.file_id)
        with self.blocked("ambiguous_receipt"):
            self.prepare()
        self.assertFalse(provenance.unsent(self.connection, attempt.id))
        self.assertFalse(provenance.close_unsent_for_job(self.connection, self.job, T2))
        self.rejects("DELETE FROM upload_attempts WHERE id=%s", (attempt.id,))

    def test_a_receipt_of_another_version_contradicts_a_landed_attempt(self) -> None:
        attempt = self.started()
        self.land(attempt, "v1")
        self.connection.commit()
        provenance.append_receipt(self.db, attempt.id, {"VersionId": "v9"}, T2)
        with self.blocked("ambiguous_receipt"):
            provenance.outstanding(self.connection, self.file_id)
        with self.blocked("ambiguous_receipt"):
            provenance.receipt(self.connection, attempt.id)


class IntegrityTests(_Fixture):
    def other_file(self) -> tuple[str, dict]:
        file_id = self.catalog.observe_local_copy(
            vault_id=2, path="b.txt", file_type="regular", size=1, mtime_ns=1, observed_at=T0)
        return file_id, self.new_job(file_id=file_id, status="queued")

    def test_scope_is_enforced_by_keys(self) -> None:
        attempt = self.started()
        other_file, other_job = self.other_file()
        snapshot = attempt.admitted.id
        self.rejects(
            "INSERT INTO upload_attempts(snapshot_id, file_id, inventory_versions, inventory_markers) "
            "VALUES (%s, %s, '{}', '{}')", (snapshot, other_file))
        self.rejects("INSERT INTO upload_snapshot_jobs(job_id, snapshot_id) VALUES (%s, %s)",
                     (other_job["id"], snapshot))
        self.rejects(
            "INSERT INTO upload_snapshots(vault_id, file_id, path, path_since, source_root, provider, "
            "local_generation, size, mtime_ns, plaintext_sha256, part_size, part_sha256, verification) "
            "SELECT 3, file_id, path, path_since, source_root, provider, local_generation, size, "
            "mtime_ns, plaintext_sha256, part_size, part_sha256, verification "
            "FROM upload_snapshots WHERE id=%s", (snapshot,))
        provenance.append_receipt(self.db, attempt.id, {"VersionId": "v1"}, T1)
        provenance.record_landed(self.connection, self.job, attempt, "v1", T1)
        self.connection.execute("UPDATE files SET cloud_since=now() WHERE id=%s", (other_file,))
        base = ("INSERT INTO archive_versions(file_id, object_key, provider_version_id, size, "
                "upload_attempt_id) VALUES (%s, %s, %s, %s, %s)")
        size = self.snapshot.size
        self.rejects(base, (other_file, self.key, "v1", size, attempt.id))
        for key, version, length in ((self.key + "x", "v1", size), (self.key, "v2", size),
                                     (self.key, "v1", size + 1)):
            with self.subTest(key=key, version=version, size=length):
                self.rejects(base, (self.file_id, key, version, length, attempt.id))
        self.connection.execute(base, (self.file_id, self.key, "v1", size, attempt.id))

    def test_a_version_needs_a_landed_attempt(self) -> None:
        attempt = self.started()
        self.rejects(
            "INSERT INTO archive_versions(file_id, object_key, provider_version_id, size, "
            "upload_attempt_id) VALUES (%s, %s, 'v1', %s, %s)",
            (self.file_id, self.key, self.snapshot.size, attempt.id))
        with self.blocked("ambiguous_receipt"):
            provenance.record_landed(self.connection, self.job, attempt, "null", T1)
        provenance.append_receipt(self.db, attempt.id, {"VersionId": "v1"}, T1)
        with self.blocked("ambiguous_receipt"):
            provenance.record_landed(self.connection, self.job, attempt, "v2", T1)

    def test_proofs_are_immutable(self) -> None:
        attempt = self.started()
        version = self.land(attempt)
        other = self.new_job(status="queued")
        statements = [
            ("UPDATE upload_snapshots SET plaintext_sha256=%s WHERE id=%s", (bytes(32), attempt.admitted.id)),
            ("UPDATE upload_snapshots SET path='b.txt' WHERE id=%s", (attempt.admitted.id,)),
            ("UPDATE upload_snapshots SET mtime_ns=0 WHERE id=%s", (attempt.admitted.id,)),
            ("UPDATE upload_snapshots SET closed_at=now() WHERE id=%s", (attempt.admitted.id,)),
            ("UPDATE upload_attempts SET token=gen_random_uuid() WHERE id=%s", (attempt.id,)),
            ("UPDATE upload_attempts SET inventory_versions='{x}' WHERE id=%s", (attempt.id,)),
            ("UPDATE upload_attempts SET landed_version_id='v2' WHERE id=%s", (attempt.id,)),
            ("UPDATE upload_attempts SET job_id=%s WHERE id=%s", (other["id"], attempt.id)),
            ("UPDATE upload_receipts SET response='{\"VersionId\":\"v2\"}' WHERE attempt_id=%s", (attempt.id,)),
            ("DELETE FROM upload_receipts WHERE attempt_id=%s", (attempt.id,)),
            ("UPDATE archive_versions SET upload_attempt_id=NULL WHERE id=%s", (version,)),
            ("INSERT INTO upload_attempts(snapshot_id, file_id, inventory_versions, inventory_markers) "
             "VALUES (%s, %s, '{}', '{}')", (attempt.admitted.id, self.file_id)),
        ]
        for statement, params in statements:
            with self.subTest(statement=statement):
                self.rejects(statement, params)
        provenance.journal_multipart(self.db, attempt.id, "mpu-1", T1)
        self.rejects("UPDATE upload_multiparts SET upload_id='mpu-2' WHERE attempt_id=%s", (attempt.id,))
        self.rejects("DELETE FROM upload_multiparts WHERE attempt_id=%s", (attempt.id,))

    def test_a_snapshot_with_an_unsettled_attempt_stays_open(self) -> None:
        attempt = self.prepare()
        self.rejects("UPDATE upload_snapshots SET closed_at=now() WHERE id=%s", (attempt.admitted.id,))

    def test_open_uncertainty_is_never_collected_and_settled_at_is_no_authority(self) -> None:
        attempt = self.started()
        self.rejects("DELETE FROM upload_attempts WHERE id=%s", (attempt.id,))
        self.rejects("DELETE FROM upload_snapshots WHERE id=%s", (attempt.admitted.id,))
        version = self.land(attempt)
        self.connection.commit()
        # Settled and landed, but its version still references it.
        self.rejects("DELETE FROM upload_attempts WHERE id=%s", (attempt.id,))
        self.assertIsNotNone(version)
        # A late multipart row of a settled attempt keeps it too.
        provenance.journal_multipart(self.db, attempt.id, "late-mpu", T2)
        self.rejects("DELETE FROM upload_attempts WHERE id=%s", (attempt.id,))

    def test_a_cleanly_settled_history_can_be_collected(self) -> None:
        attempt = self.prepare()
        self.assertTrue(provenance.close_unsent_for_job(self.connection, self.job, T2))
        self.connection.execute("DELETE FROM upload_attempts WHERE id=%s", (attempt.id,))
        self.connection.execute("DELETE FROM upload_snapshots WHERE id=%s", (attempt.admitted.id,))
        self.connection.commit()
        self.assertEqual(self.count("upload_snapshots"), 0)
        # A landed attempt goes once its version's exact cleanup removed the
        # version, together with its agreeing receipts and closed multiparts.
        landed = self.started()
        provenance.journal_multipart(self.db, landed.id, "mpu", T1)
        version = self.land(landed)
        self.catalog.abandon_replacement(self.file_id, archive_version_id=version)
        self.connection.execute("DELETE FROM archive_versions WHERE id=%s", (version,))
        self.connection.execute("DELETE FROM upload_attempts WHERE id=%s", (landed.id,))
        self.connection.execute("DELETE FROM upload_snapshot_jobs WHERE snapshot_id=%s",
                                (landed.admitted.id,))
        self.connection.execute("DELETE FROM upload_snapshots WHERE id=%s", (landed.admitted.id,))
        self.connection.commit()
        for table in ("upload_snapshots", "upload_attempts", "upload_receipts", "upload_multiparts"):
            self.assertEqual(self.count(table), 0, table)


class MultipartJournalTests(_Fixture):
    def test_the_journal_names_only_open_upload_ids_of_the_attempt(self) -> None:
        attempt = self.started()
        provenance.journal_multipart(self.db, attempt.id, "b", T1)
        provenance.journal_multipart(self.db, attempt.id, "a", T1)
        provenance.journal_multipart(self.db, attempt.id, "a", T2)
        self.assertEqual(provenance.open_multiparts(self.connection, attempt.id), ["a", "b"])
        provenance.close_multipart(self.db, attempt.id, "a", T2)
        self.connection.rollback()
        self.assertEqual(provenance.open_multiparts(self.connection, attempt.id), ["b"])
        provenance.close_multiparts(self.connection, attempt.id, T2)
        self.assertEqual(provenance.open_multiparts(self.connection, attempt.id), [])


if __name__ == "__main__":
    unittest.main()
