"""#458 acceptance: real PostgreSQL transactions, fake provider, actual Job seam.

No optional skips: TEST_POSTGRES_URL must point to a disposable PG16 server.
"""
from __future__ import annotations

import hashlib
import io
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import directory_aggregates as aggregates
from tests.postgres import PostgresConnection, database_dsn, run_alembic

PAYLOAD = b"nested concurrent upload content"
STAMP = "2026-07-21T10:00:00+00:00"


class Provider:
    """Thread-safe, exact-version fake; counts every external upload/delete."""
    def __init__(self):
        self.lock = threading.Lock()
        self.objects = {}
        self.puts = []
        self.reads = []
        self.deleted = []

    def upload(self, _client, *, source, bucket, key, check_active, on_progress, **_):
        check_active()
        version = uuid.uuid4().hex
        with self.lock:
            self.objects[(key, version)] = source.read_bytes()
            self.puts.append((key, version))
        # Exercise the actual progress DB callback AFTER provider response.
        on_progress(source.stat().st_size)
        check_active()
        return {"VersionId": version}

    def head_object(self, *, Key, VersionId, **_):
        with self.lock:
            content = self.objects[(Key, VersionId)]
        return {"VersionId": VersionId, "ContentLength": len(content), "ETag": '"etag"'}

    def get_object(self, *, Key, VersionId, **_):
        with self.lock:
            self.reads.append((Key, VersionId))
            content = self.objects[(Key, VersionId)]
        return {"VersionId": VersionId, "Body": io.BytesIO(content)}

    def list_object_versions(self, *, Prefix, **_):
        with self.lock:
            versions = [{"Key": key, "VersionId": version} for key, version in self.objects if key.startswith(Prefix)]
        return {"Versions": versions, "DeleteMarkers": [], "IsTruncated": False}

    def delete_object(self, **kwargs):
        with self.lock:
            self.deleted.append(kwargs)
            del self.objects[(kwargs["Key"], kwargs["VersionId"])]
        return {}


class AggregateDeadlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.root / "catalog"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.provider = Provider()
        aggregates.set_maintenance_scheduling_enabled(False)
        self.addCleanup(aggregates.set_maintenance_scheduling_enabled, True)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SHOW server_version_num").fetchone()["server_version_num"][:2], "16")
            connection.execute("INSERT INTO users(id, username, display_name, password_hash) VALUES (1,'owner','Owner','hash')")
            connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix)
                VALUES (2,'docs','Docs',%s,'bucket','docs')""", (str(self.source),))
            from app.services.vault_relocation import enroll_vault_root_identity
            enroll_vault_root_identity(connection, 2, str(self.source))
        self.enterContext(patch("app.database.settings", SimpleNamespace(database_dsn=database_dsn(self.path))))
        self.enterContext(patch("app.storage.validate_cloud_vault"))
        self.enterContext(patch("app.storage.s3_client", return_value=self.provider))
        self.enterContext(patch("app.storage.upload_file", side_effect=self.provider.upload))

    def seed(self, count=1, prefix="0"):
        jobs = []
        with PostgresConnection(self.path) as connection:
            catalog = ArchiveCatalog(connection)
            for index in range(count):
                path = f"{prefix}/{index % 8:04d}/{index % 19:04d}/leaf/{index}.bin"
                target = self.source / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(PAYLOAD)
                catalog.observe_local_copy(vault_id=2, path=path, file_type="regular",
                    size=len(PAYLOAD), mtime_ns=target.stat().st_mtime_ns, observed_at=STAMP)
                file_id = connection.execute("SELECT vault_file_id FROM file_paths WHERE vault_id=2 AND path=%s AND valid_to IS NULL", (path,)).fetchone()["vault_file_id"]
                job = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                    requested_by,requested_at,updated_at,total_bytes)
                    VALUES (2,%s,%s,'upload','queued',1,%s,%s,%s) RETURNING id""",
                    (file_id, path, STAMP, STAMP, len(PAYLOAD))).fetchone()
                jobs.append(job["id"])
            aggregates.flush_directory_aggregates(connection, vault_id=2)
        return jobs

    def run_job(self, job_id):
        with PostgresConnection(self.path) as connection:
            token = uuid.uuid4().hex
            claimed = ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=token, claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso(),
            )
            self.assertIsNotNone(claimed)
            job = connection.execute("""SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix
                FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s""", (job_id,)).fetchone()
        return storage.process_job(job)

    def assert_completed(self, count):
        with PostgresConnection(self.path) as connection:
            states = connection.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
            self.assertEqual(states, [{"status": "completed", "n": count}])
            versions = connection.execute("SELECT provider_version_id,integrity,availability FROM archive_versions").fetchall()
        self.assertEqual(len(versions), count)
        self.assertTrue(all(v["integrity"] == "verified" and v["availability"] == "available" for v in versions))
        self.assertEqual({v["provider_version_id"] for v in versions}, {v for _, v in self.provider.puts})
        self.assertEqual(len(self.provider.puts), count, "DB retry replayed an external PUT")
        self.assertEqual(set(self.provider.reads), set(self.provider.puts))
        self.assertEqual(self.provider.deleted, [])

    def test_16_workers_1024_nested_uploads_with_concurrent_maintenance(self):
        jobs = self.seed(1024)
        stop = threading.Event()
        maintenance_passes = []
        start = threading.Barrier(17)

        def maintenance():
            start.wait(timeout=20)
            while not stop.is_set():
                result = aggregates.process_directory_aggregate_maintenance(vault_id=2)
                maintenance_passes.append(result)
                stop.wait(0.02)

        def worker(batch):
            start.wait(timeout=20)
            for job_id in batch:
                self.assertTrue(self.run_job(job_id))

        with ThreadPoolExecutor(max_workers=17) as pool:
            upkeep = pool.submit(maintenance)
            workers = [pool.submit(worker, jobs[i::16]) for i in range(16)]
            try:
                for future in workers:
                    future.result(timeout=600)
            finally:
                stop.set()
            upkeep.result(timeout=30)
        aggregates.process_directory_aggregate_maintenance(vault_id=2)
        self.assertTrue(maintenance_passes)
        self.assert_completed(1024)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM directory_aggregate_dirty").fetchone()["n"], 0)
            actual = connection.execute("SELECT * FROM directory_aggregates ORDER BY path").fetchall()
            root = next(row for row in actual if row["path"] == "0")
            self.assertEqual(root["item_count"], 1024)
            self.assertEqual(root["state_protected"], 1024)
            self.assertEqual(root["cloud_size"], len(PAYLOAD) * 1024)
            self.assertEqual(root["action_upload"], 0)
            aggregates.rebuild_vault_directory_aggregates(connection, 2)
            expected = connection.execute("SELECT * FROM directory_aggregates ORDER BY path").fetchall()
        # Ignore rebuild timestamps, compare every semantic column/directory.
        def semantic(rows):
            return [{k: v for k, v in row.items() if k != "updated_at"} for row in rows]
        self.assertEqual(semantic(actual), semantic(expected))
        print(f"#458 acceptance: 16 workers / 1024 completed / {len(maintenance_passes)} maintenance passes / exact full-rebuild parity")

    def test_forced_post_response_transaction_abort_retries_linkage_without_put(self):
        original = ArchiveCatalog.link_job_version
        for code in ("40P01", "40001"):
            jobs = self.seed(prefix=code)
            attempts = []

            def fail_link_once(catalog, job_id, version_id):
                result = original(catalog, job_id, version_id)
                attempts.append(version_id)
                if len(attempts) == 1:
                    # Server-side abort: record + link + dirty marks all roll back.
                    catalog.connection.execute(f"DO $$ BEGIN RAISE EXCEPTION 'forced transaction abort' USING ERRCODE='{code}'; END $$")
                return result

            with self.subTest(sqlstate=code), patch.object(ArchiveCatalog, "link_job_version", fail_link_once):
                self.assertTrue(self.run_job(jobs[0]))
                self.assertEqual(len(attempts), 2)
                self.assert_completed(len(self.provider.puts))
                with PostgresConnection(self.path) as connection:
                    self.assertIsNone(connection.execute("SELECT id FROM archive_versions WHERE id=%s", (attempts[0],)).fetchone())
                print(f"#458 forced {code}: process_job completed, two DB attempts, one provider PUT")

    def test_real_postgresql_deadlock_victim_retries_through_process_job(self):
        jobs = self.seed()
        with PostgresConnection(self.path) as connection:
            connection.execute("CREATE TABLE deadlock_gate(id integer PRIMARY KEY, value integer NOT NULL)")
            connection.execute("INSERT INTO deadlock_gate VALUES (1,0),(2,0)")
        original = ArchiveCatalog.link_job_version
        attempts = []
        held = threading.Event()
        other_errors = []
        threads = []

        def link_with_cycle(catalog, job_id, version_id):
            original(catalog, job_id, version_id)
            attempts.append(version_id)
            if len(attempts) != 1:
                return
            connection = catalog.connection
            connection.execute("UPDATE deadlock_gate SET value=value+1 WHERE id=2")
            victim_pid = connection.info.backend_pid

            def contender():
                try:
                    with PostgresConnection(self.path) as other:
                        other.execute("UPDATE deadlock_gate SET value=value+1 WHERE id=1")
                        held.set()
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            other.execute("SELECT pg_stat_clear_snapshot()")
                            waiting = other.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (victim_pid,)).fetchone()
                            if waiting and waiting["wait_event_type"] == "Lock":
                                # Victim started waiting first, so its deadlock
                                # detector fires first, without superuser GUCs.
                                threading.Event().wait(0.1)
                                other.execute("UPDATE deadlock_gate SET value=value+1 WHERE id=2")
                                return
                            threading.Event().wait(0.01)
                        raise AssertionError("Job never waited on the first gate")
                except BaseException as error:
                    other_errors.append(error)
                    held.set()

            thread = threading.Thread(target=contender)
            threads.append(thread)
            thread.start()
            self.assertTrue(held.wait(10))
            # Actual PostgreSQL lock cycle, not an exception mock or text match.
            connection.execute("UPDATE deadlock_gate SET value=value+1 WHERE id=1")

        try:
            with patch.object(ArchiveCatalog, "link_job_version", link_with_cycle):
                self.assertTrue(self.run_job(jobs[0]))
        finally:
            for thread in threads:
                thread.join(20)
                self.assertFalse(thread.is_alive())
        self.assertEqual(other_errors, [])
        self.assertEqual(len(attempts), 2)
        self.assert_completed(1)
        print("#458 real PostgreSQL deadlock: process_job retried catalog+link, exactly one provider PUT")

    def test_post_response_progress_flush_and_status_units_retry_without_put(self):
        jobs = self.seed()
        original_dirty = storage._dirty_job_protection
        original_progress = storage.set_job_progress.__wrapped__
        injected = set()
        progress_calls = []

        def dirty_once(connection, job_id):
            original_dirty(connection, job_id)
            status = connection.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()["status"]
            if status in {"uploading", "verifying", "completed"} and status not in injected:
                injected.add(status)
                connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced status abort' USING ERRCODE='40P01'; END $$")

        def progress_once(*args, **kwargs):
            progress_calls.append(1)
            if len(progress_calls) == 2 and "progress" not in injected:
                injected.add("progress")
                # Real aborted DB handle; operation wrapper must close before retry.
                with storage.db() as connection:
                    connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced flush abort' USING ERRCODE='40001'; END $$")
            return original_progress(*args, **kwargs)

        with patch("app.storage._dirty_job_protection", side_effect=dirty_once), patch(
            "app.storage.set_job_progress", storage.retry_database_unit(progress_once)
        ):
            self.assertTrue(self.run_job(jobs[0]))
        self.assertEqual(injected, {"uploading", "verifying", "completed", "progress"})
        self.assert_completed(1)

    def test_commit_time_abort_replays_whole_linkage_transaction(self):
        jobs = self.seed()
        with PostgresConnection(self.path) as connection:
            # Sequence increments survive rollback, so exactly the first COMMIT
            # fails, after the mutation callback has returned successfully.
            connection.execute("CREATE SEQUENCE forced_commit_attempt")
            connection.execute("""CREATE FUNCTION abort_first_link_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.archive_version_id IS NOT NULL AND NEW.status='uploading'
                       AND nextval('forced_commit_attempt')=1 THEN
                        RAISE EXCEPTION 'forced commit abort' USING ERRCODE='40001';
                    END IF;
                    RETURN NEW;
                END $$""")
            connection.execute("""CREATE CONSTRAINT TRIGGER abort_first_link_commit
                AFTER UPDATE ON jobs DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION abort_first_link_commit()""")
        connections = []
        real_db = storage.db

        def tracked_db():
            connection = real_db()
            connections.append(connection)
            return connection

        with patch("app.storage.db", side_effect=tracked_db):
            self.assertTrue(self.run_job(jobs[0]))
        self.assertTrue(connections)
        self.assertTrue(all(connection.closed for connection in connections), "COMMIT abort leaked an owned handle")
        self.assert_completed(1)
        with PostgresConnection(self.path) as connection:
            self.assertGreaterEqual(connection.execute("SELECT last_value FROM forced_commit_attempt").fetchone()["last_value"], 2)

    def test_verification_and_post_delete_catalog_abort_replay_only_db(self):
        jobs = self.seed()
        with PostgresConnection(self.path) as connection:
            path = connection.execute("SELECT path FROM jobs WHERE id=%s", (jobs[0],)).fetchone()["path"]
            key = f"docs/{path}"
            catalog = ArchiveCatalog(connection)
            previous = catalog.record_archive_version(
                vault_id=2, path=path, object_key=key, provider_version_id="previous-only-copy",
                size=3, storage_class="STANDARD", etag="old", uploaded_at="2026-07-20",
                observed_at="2026-07-20", scan_id="old",
            )
            catalog.mark_version_verified(previous, plaintext_sha256=hashlib.sha256(b"old").hexdigest(), verified_at="2026-07-20")
        self.provider.objects[(key, "previous-only-copy")] = b"old"
        original_verify = ArchiveCatalog.mark_version_verified
        original_clear = ArchiveCatalog.clear_delete_markers_for_file
        attempts = {"verify": 0, "cleanup": 0}

        def verify_once(catalog, *args, **kwargs):
            original_verify(catalog, *args, **kwargs)
            attempts["verify"] += 1
            if attempts["verify"] == 1:
                catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced verification abort' USING ERRCODE='40001'; END $$")

        def cleanup_once(catalog, *args, **kwargs):
            original_clear(catalog, *args, **kwargs)
            attempts["cleanup"] += 1
            if attempts["cleanup"] == 1:
                # Even a verified version is not a completed Job at this seam.
                state = catalog.connection.execute("SELECT status FROM jobs WHERE id=%s", (jobs[0],)).fetchone()["status"]
                self.assertEqual(state, "uploading")
                catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced cleanup abort' USING ERRCODE='40P01'; END $$")

        with patch.object(ArchiveCatalog, "mark_version_verified", verify_once), patch.object(
            ArchiveCatalog, "clear_delete_markers_for_file", cleanup_once
        ):
            self.assertTrue(self.run_job(jobs[0]))
        self.assertEqual(attempts, {"verify": 2, "cleanup": 2})
        self.assertEqual(len(self.provider.puts), 1)
        self.assertEqual(len(self.provider.reads), 1, "Verification stream was replayed with DB work")
        self.assertEqual(len(self.provider.deleted), 1, "DB retry replayed an external deletion")
        self.assertEqual(self.provider.deleted[0]["VersionId"], "previous-only-copy")
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM jobs WHERE id=%s", (jobs[0],)).fetchone()["status"], "completed")
            versions = ArchiveCatalog(connection).list_versions(2, path)
            old = next(version for version in versions if version["id"] == previous)
            current = next(version for version in versions if version["id"] != previous)
            self.assertEqual(old["availability"], "purged")
            self.assertEqual(current["integrity"], "verified")
            self.assertEqual(current["availability"], "available")

    def test_retry_status_transaction_is_itself_retried(self):
        jobs = self.seed()
        original = storage._dirty_job_protection
        attempts = []

        def retry_status_once(connection, job_id):
            original(connection, job_id)
            state = connection.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()["status"]
            if state == "retrying":
                attempts.append(1)
                if len(attempts) == 1:
                    connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced retry status abort' USING ERRCODE='40P01'; END $$")

        with patch("app.storage.upload_file", side_effect=RuntimeError("SlowDown")), patch(
            "app.storage._dirty_job_protection", side_effect=retry_status_once
        ):
            self.assertTrue(self.run_job(jobs[0]))
        self.assertEqual(len(attempts), 2)
        with PostgresConnection(self.path) as connection:
            state = connection.execute("SELECT status,retry_count FROM jobs WHERE id=%s", (jobs[0],)).fetchone()
            self.assertEqual(state, {"status": "retrying", "retry_count": 1})
        self.assertEqual(self.provider.puts, [])

    def test_exhausted_post_response_db_retries_do_not_schedule_blind_reupload(self):
        from app.services.database_retry import DATABASE_RETRY_MAX_ATTEMPTS
        jobs = self.seed()
        attempts = []

        def always_abort(catalog, *_args):
            attempts.append(1)
            catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced exhausted abort' USING ERRCODE='40P01'; END $$")

        with patch.object(ArchiveCatalog, "link_job_version", always_abort):
            self.assertTrue(self.run_job(jobs[0]))
        self.assertEqual(len(attempts), DATABASE_RETRY_MAX_ATTEMPTS)
        self.assertEqual(len(self.provider.puts), 1)
        self.assertEqual(self.provider.deleted, [])
        with PostgresConnection(self.path) as connection:
            state = connection.execute("SELECT status,retry_count,archive_version_id FROM jobs WHERE id=%s", (jobs[0],)).fetchone()
        self.assertEqual(state, {"status": "failed", "retry_count": 0, "archive_version_id": None})
        # Persistent adoption after exhaustion/process death belongs to #464;
        # this fix must not hide that uncertainty by uploading/deleting again.

    def test_late_old_timestamp_commit_survives_incremental_and_full_rebuild(self):
        self.seed()
        for increment, full in enumerate((False, True), 1):
            with PostgresConnection(self.path) as marker:
                marker.execute("INSERT INTO directory_aggregate_dirty(vault_id,path,marked_at) VALUES (2,'0',%s)", (STAMP,))
            with self.subTest(full_rebuild=full), PostgresConnection(self.path) as writer:
                # Change canonical data and mark BEFORE cutoff, but commit only
                # AFTER the consumer has built its projection from old data.
                path = writer.execute("SELECT path FROM jobs LIMIT 1").fetchone()["path"]
                writer.execute("UPDATE local_copies SET size=size+1")
                with patch.object(aggregates, "_now", return_value=STAMP):
                    aggregates.mark_path_dirty(writer, 2, path)
                original_delete = aggregates._delete_claimed_dirty_rows

                def commit_late(connection, **kwargs):
                    writer.commit()
                    return original_delete(connection, **kwargs)

                with PostgresConnection(self.path) as reader, patch.object(
                    aggregates, "_delete_claimed_dirty_rows", side_effect=commit_late
                ):
                    if full:
                        aggregates.rebuild_vault_directory_aggregates(reader, 2)
                    else:
                        aggregates.flush_directory_aggregates(reader, vault_id=2)
                with PostgresConnection(self.path) as reader:
                    remaining = reader.execute("SELECT path FROM directory_aggregate_dirty").fetchall()
                    self.assertIn({"path": "0"}, remaining)
                    aggregates.flush_directory_aggregates(reader, vault_id=2)
                    root = reader.execute("SELECT local_size FROM directory_aggregates WHERE vault_id=2 AND path='0'").fetchone()
                    self.assertEqual(root["local_size"], len(PAYLOAD) + increment)

    def test_writer_does_not_wait_for_consumer_deleting_same_path(self):
        self.seed()
        with PostgresConnection(self.path) as marker:
            aggregates.mark_path_dirty(marker, 2, "0/a.bin")
        with PostgresConnection(self.path) as consumer:
            aggregates.flush_directory_aggregates(consumer, vault_id=2)
            # Consumer holds the DELETE locks, but no writer targets those row
            # identities anymore. The old shared-key upsert times out here.
            with PostgresConnection(self.path) as writer:
                writer.execute("SET LOCAL lock_timeout='250ms'")
                aggregates.mark_path_dirty(writer, 2, "0/b.bin")
        with PostgresConnection(self.path) as reader:
            self.assertEqual(reader.execute("SELECT path FROM directory_aggregate_dirty").fetchall(), [{"path": "0"}])
            aggregates.flush_directory_aggregates(reader, vault_id=2)

    def test_same_connection_rollback_does_not_suppress_durable_mark(self):
        self.seed()
        with PostgresConnection(self.path) as connection:
            aggregates.mark_path_dirty(connection, 2, "0/a.bin")
            connection.rollback()
            aggregates.mark_path_dirty(connection, 2, "0/b.bin")
            connection.commit()
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT path FROM directory_aggregate_dirty").fetchall(), [{"path": "0"}])


class DirtyJournalMigrationTests(unittest.TestCase):
    def test_populated_upgrade_downgrade_preserves_pending_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration"
            result = run_alembic(path, "0060_vault_class_moves")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(path) as connection:
                connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'v','V','/source','bucket','v')")
                connection.execute("INSERT INTO directory_aggregate_dirty VALUES (2,'a',%s),(2,'b',%s)", (STAMP, STAMP))
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            for _ in range(2):
                with PostgresConnection(path) as connection:
                    aggregates.mark_path_dirty(connection, 2, "a/x.bin")
                    aggregates.mark_path_dirty(connection, 2, "a/y.bin")
            with PostgresConnection(path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM directory_aggregate_dirty WHERE path='a'").fetchone()["n"], 3)
            result = run_alembic(path, "0060_vault_class_moves", "downgrade")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(path) as connection:
                self.assertEqual(connection.execute("SELECT path FROM directory_aggregate_dirty ORDER BY path").fetchall(), [{"path": "a"}, {"path": "b"}])
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
