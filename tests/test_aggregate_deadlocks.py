"""#458 acceptance: real PostgreSQL transactions, fake provider, actual Job seam.

No optional skips: TEST_POSTGRES_URL must point to a disposable PG16 server.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
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
from tests.stress_profile import stress_file_count

PAYLOAD = b"nested concurrent upload content"
STAMP = "2026-07-21T10:00:00+00:00"


class Provider:
    """Thread-safe, exact-version fake; counts every external upload/delete."""
    def __init__(self):
        self.lock = threading.Lock()
        self.objects = {}
        self.metadata = {}
        self.puts = []
        self.reads = []
        self.deleted = []

    def upload(self, _client, *, source, bucket, key, check_active, on_progress, on_acknowledged=None, metadata=None, **_):
        check_active()
        version = uuid.uuid4().hex
        with self.lock:
            self.objects[(key, version)] = source.read_bytes()
            self.metadata[(key, version)] = dict(metadata or {})
            self.puts.append((key, version))
        response = self.head_object(Key=key,VersionId=version)
        if on_acknowledged:
            on_acknowledged(response)
        # Exercise the actual progress DB callback AFTER durable raw response.
        on_progress(source.stat().st_size)
        check_active()
        return response

    def head_object(self, *, Key, VersionId, **_):
        with self.lock:
            content = self.objects[(Key, VersionId)]
        import base64
        return {"VersionId": VersionId, "ContentLength": len(content), "ETag": '"etag"',
                "Metadata":dict(self.metadata[(Key,VersionId)]),"ChecksumType":"FULL_OBJECT",
                "ChecksumSHA256":base64.b64encode(hashlib.sha256(content).digest()).decode()}

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


class SharedSQLRecorder:
    """Real PG handle: reject upgrades/derived writers in an opted-in unit.

    Callbacks and all their callees still execute real SQL, including FKs and
    nested provenance checks. Never replace a mutation with a mock.
    """
    def __init__(self, connection, statements):
        self.connection = connection
        self.statements = statements

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, query, *args, **kwargs):
        text = " ".join(str(query).lower().split())
        self.statements.append(text)
        if re.search(r"\b(?:update|insert into|delete from) (?:vaults|directory_aggregates|directory_aggregate_status|catalog_events|vault_catalog_revisions)\b", text):
            raise AssertionError(f"Shared upload unit wrote a Vault/derived row: {text}")
        if re.search(r"\bfrom vaults\b", text) and re.search(r"\bfor (?:no key update|update)\b", text):
            raise AssertionError(f"Shared upload unit upgraded its Vault lock: {text}")
        return self.connection.execute(query, *args, **kwargs)


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

    def assert_completed(self, count, *, cancelled_count=0):
        with PostgresConnection(self.path) as connection:
            states = connection.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
            failed = connection.execute("SELECT id,status,message FROM jobs WHERE status<>'completed' ORDER BY id LIMIT 16").fetchall()
            expected = {"completed": count}
            if cancelled_count:
                expected["cancelled"] = cancelled_count
            self.assertEqual({row["status"]: row["n"] for row in states}, expected, failed)
            versions = connection.execute("SELECT provider_version_id,integrity,availability FROM archive_versions").fetchall()
        self.assertEqual(len(versions), count)
        self.assertTrue(all(v["integrity"] == "verified" and v["availability"] == "available" for v in versions))
        self.assertEqual({v["provider_version_id"] for v in versions}, {v for _, v in self.provider.puts})
        self.assertEqual(len(self.provider.puts), count, "DB retry replayed an external PUT")
        self.assertEqual(set(self.provider.reads), set(self.provider.puts))
        self.assertEqual(self.provider.deleted, [])

    def test_sixteen_error_notifications_with_owner_maintenance_have_no_fk_deadlock(self):
        from app.services import notifications, protection_notifications
        from datetime import datetime, timezone
        jobs = self.seed(16)
        owner_held, vault_held = threading.Event(), threading.Event()
        failures = threading.Barrier(16)
        original_enqueue = notifications.enqueue_job_terminal_notification_best_effort

        def terminal(connection, *, job_id):
            # set_job already holds its Vault lock at this seam.
            vault_held.set()
            self.assertTrue(owner_held.wait(10))
            return original_enqueue(connection,job_id=job_id)

        def monitor(connection,vault,**kwargs):
            # Real maintain_notifications already holds its Owner episode lock.
            owner_held.set()
            self.assertTrue(vault_held.wait(10))
            notifications.set_problem(connection,key='blocked:docs:preflight',event='cloud_preflight_blocked',
                vault_id=2,active=True,params={'remediation':'Check permissions'})

        def fail(job):
            failures.wait(timeout=10)
            self.assertTrue(owner_held.wait(10))
            raise RuntimeError('simultaneous provider failure')

        def upkeep():
            with PostgresConnection(self.path) as connection:
                protection_notifications.maintain_notifications(connection,now=datetime.now(timezone.utc))

        with patch.object(storage,'process_upload',side_effect=fail), patch.object(
            notifications,'enqueue_job_terminal_notification_best_effort',side_effect=terminal
        ), patch.object(protection_notifications,'monitor_vault',side_effect=monitor), ThreadPoolExecutor(max_workers=17) as pool:
            maintenance = pool.submit(upkeep)
            workers = [pool.submit(self.run_job,job) for job in jobs]
            for future in workers:
                self.assertTrue(future.result(timeout=30))
            maintenance.result(timeout=30)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM jobs WHERE status='failed' AND terminal_notification_status='done'").fetchone()['n'],16)
            # A burst beyond GROUP_THRESHOLD folds into one per-Vault group (#499).
            singles = connection.execute("SELECT count(*) n FROM notifications WHERE event='job_failed'").fetchone()['n']
            groups = connection.execute("SELECT count(*) n FROM notifications WHERE event='job_failed_group'").fetchone()['n']
            self.assertTrue(notifications.GROUP_THRESHOLD <= singles <= 16, singles)
            self.assertEqual(groups, 0 if singles == 16 else 1)
            folded = 0
            if groups:
                from app.i18n import parse_message_params
                row = connection.execute("SELECT message_params FROM notifications WHERE event='job_failed_group'").fetchone()
                folded = int(parse_message_params(row['message_params'])['count'])
            self.assertEqual(singles + folded, 16)
            self.assertEqual(connection.execute('SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()').fetchone()['deadlocks'],0)
        self.assertEqual(self.provider.puts,[])

    def test_error_notification_transaction_abort_retries_whole_owned_unit(self):
        from app.services import notifications
        jobs = self.seed()
        original = notifications.enqueue_job_terminal_notification
        for code in ('40P01','40001'):
            calls = []
            def aborted(connection,*,job_id):
                calls.append(job_id)
                original(connection,job_id=job_id)
                if len(calls) == 1:
                    connection.execute(f"DO $$ BEGIN RAISE EXCEPTION 'forced notification abort' USING ERRCODE='{code}'; END $$")
                return 1
            with self.subTest(sqlstate=code), patch.object(notifications,'enqueue_job_terminal_notification',side_effect=aborted):
                self.assertTrue(storage.set_job(jobs[0],'failed','provider refused'))
                self.assertEqual(len(calls),2)
            with PostgresConnection(self.path) as connection:
                self.assertEqual(connection.execute("SELECT count(*) n FROM notifications WHERE event='job_failed'").fetchone()['n'],1)

    def test_16_workers_1024_nested_uploads_with_concurrent_maintenance(self):
        count = stress_file_count(1024)
        jobs = self.seed(count)
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
        self.assert_completed(count)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM directory_aggregate_dirty").fetchone()["n"], 0)
            actual = connection.execute("SELECT * FROM directory_aggregates ORDER BY path").fetchall()
            root = next(row for row in actual if row["path"] == "0")
            self.assertEqual(root["item_count"], count)
            self.assertEqual(root["state_protected"], count)
            self.assertEqual(root["cloud_size"], len(PAYLOAD) * count)
            self.assertEqual(root["action_upload"], 0)
            aggregates.rebuild_vault_directory_aggregates(connection, 2)
            expected = connection.execute("SELECT * FROM directory_aggregates ORDER BY path").fetchall()
        # Ignore rebuild timestamps, compare every semantic column/directory.
        def semantic(rows):
            return [{k: v for k, v in row.items() if k != "updated_at"} for row in rows]
        self.assertEqual(semantic(actual), semantic(expected))
        print(f"#458 acceptance: 16 workers / {count} completed / {len(maintenance_passes)} maintenance passes / exact full-rebuild parity")

    def test_535_held_step_shares_vault_but_completion_stays_exclusive(self):
        from app import database
        import psycopg

        job_id = self.seed()[0]
        token = uuid.uuid4().hex
        with PostgresConnection(self.path) as connection:
            self.assertIsNotNone(ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=token, claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso(),
            ))

        def bounded_db():
            lease = database.db()
            lease.execute("SET LOCAL lock_timeout='200ms'")
            return lease

        # A concurrent derived-row writer owns NO KEY UPDATE on the Vault.
        # An ordinary claimed intermediate step must not wait on that lock,
        # whereas terminal/admission-relevant and unclaimed writes still do.
        with PostgresConnection(self.path) as blocker, \
                patch.object(storage, "db", side_effect=bounded_db), \
                patch.object(storage, "_claim_token_for", return_value=token) as claim:
            blocker.execute("SELECT id FROM vaults WHERE id=2 FOR NO KEY UPDATE")
            self.assertTrue(storage.set_job(job_id, "uploading"))
            self.assertTrue(storage.set_job(job_id, "verifying"))
            for status, owned in (("completed", token), ("queued", token), ("uploading", None)):
                with self.subTest(status=status, claim=bool(owned)):
                    claim.return_value = owned
                    with self.assertRaises(psycopg.errors.LockNotAvailable):
                        storage.set_job(job_id, status)
            claim.return_value = token
            with patch.object(storage, "_held_transition_may_share_vault", return_value=False):
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    storage.set_job(job_id, "uploading")
            for kwargs in ({"remediation_suspension": "paused"}, {"remediation_reason": "file_removed"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(psycopg.errors.LockNotAvailable):
                    storage.set_job(job_id, "uploading", **kwargs)
            blocker.rollback()
            blocker.execute("SELECT id FROM vaults WHERE id=2 FOR UPDATE")
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                storage.set_job(job_id, "uploading")
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()["status"], "verifying")

    def test_551_shared_units_record_complete_callees_without_vault_upgrades(self):
        from app.services import upload_provenance as provenance
        from app.services.upload_snapshot import prepare_upload_snapshot

        real_unit, real_db = storage._upload_catalog_unit, storage.db
        recordings = []

        def audited(job, mutation, *, shared=False):
            if not shared:
                return real_unit(job, mutation)
            units = []
            recordings.append(units)
            def recorded_db():
                statements = []
                units.append(statements)
                return SharedSQLRecorder(real_db(), statements)
            with patch.object(storage, "db", side_effect=recorded_db), \
                    patch.object(storage, "set_job", side_effect=AssertionError("Shared callback called set_job")):
                return real_unit(job, mutation, shared=True)

        jobs = self.seed(4)
        with patch.object(storage, "_upload_catalog_unit", side_effect=audited):
            # Both read-back metadata writes, digest/reuse reads (no keeper yet),
            # prepare and its nested require_target, send-started + capture.
            self.assertTrue(self.run_job(jobs[0]))
            # A materialized continuation must propagate SHARE through
            # require_target, then link the exact receipt without sending again.
            with PostgresConnection(self.path) as c:
                c.execute("UPDATE jobs SET status='queued',archive_version_id=NULL WHERE id=%s", (jobs[0],))
                c.execute("UPDATE upload_intents SET settled=FALSE WHERE original_job_id=%s", (jobs[0],))
                c.execute("DELETE FROM upload_intent_jobs WHERE job_id=%s", (jobs[0],))
            self.assertTrue(self.run_job(jobs[0]))

            def reuse(original, integrity):
                self.assertTrue(self.run_job(original))
                with PostgresConnection(self.path) as c:
                    old = c.execute("SELECT * FROM jobs WHERE id=%s", (original,)).fetchone()
                    c.execute("UPDATE archive_versions SET integrity=%s WHERE id=%s", (integrity, old["archive_version_id"]))
                    new = c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                        requested_by,requested_at,updated_at,total_bytes)
                        VALUES (2,%s,%s,'upload','queued',1,%s,%s,%s) RETURNING id""",
                        (old["vault_file_id"], old["path"], STAMP, STAMP, len(PAYLOAD))).fetchone()["id"]
                attempts = []
                original_link = ArchiveCatalog.link_job_version
                puts_before = len(self.provider.puts)
                def abort_link_once(catalog, job_id, version_id):
                    original_link(catalog, job_id, version_id)
                    attempts.append(version_id)
                    if len(attempts) == 1:
                        catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'shared reuse linkage abort' USING ERRCODE='40001'; END $$")
                with patch.object(ArchiveCatalog, "link_job_version", abort_link_once):
                    self.assertTrue(self.run_job(new))
                self.assertEqual(attempts, [old["archive_version_id"]] * 2)
                self.assertEqual(len(self.provider.puts), puts_before, "Reuse linkage retry must never PUT")

            # Both linkage paths, including verified link+read coalescing and
            # find_reusable_upload_version -> _has_lineage -> receipt/proof.
            reuse(jobs[1], "verified")
            reuse(jobs[2], "unverified")
            with PostgresConnection(self.path) as c:
                c.execute("UPDATE jobs SET upload_verification_requested='s3_checksum' WHERE id=%s", (jobs[3],))
            self.assertTrue(self.run_job(jobs[3]))
            self.assertEqual(len(self.provider.puts), 4)

            # Rename's admitted digest setter is the same audited DB-only seam.
            # Exercise unsent prepare refresh and settling older unsent intents,
            # not only prepare's INSERT branch.
            unsent = self.seed(prefix="unsent")[0]
            with PostgresConnection(self.path) as c:
                job = c.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (unsent,)).fetchone()
            snapshot, _ = prepare_upload_snapshot(self.source / job["path"], lambda: None)
            pinned = audited(job, lambda cat: provenance.target(cat.connection, job,
                key=storage.expected_cloud_key(job["path"], job["s3_prefix"]), provider="fake", shared=True), shared=True)
            audited(job, lambda cat: cat.set_upload_plaintext_digest(unsent,
                plaintext_sha256=snapshot.plaintext_sha256), shared=True)
            for before in ({"versions": [], "markers": []}, {"versions": ["old"], "markers": []}):
                audited(job, lambda cat: provenance.prepare(cat.connection, job, pinned=pinned,
                    snapshot=snapshot, requested="read_back", before=before, timestamp=STAMP, shared=True), shared=True)
            with PostgresConnection(self.path) as c:
                c.execute("DELETE FROM upload_intent_jobs WHERE job_id=%s", (unsent,))
            audited(job, lambda cat: provenance.prepare(cat.connection, job, pinned=pinned,
                snapshot=snapshot, requested="read_back", before=before, timestamp=STAMP, shared=True), shared=True)

        self.assertGreaterEqual(len(recordings), 20)
        for units in recordings:
            fenced_units = 0
            for statements in units:
                self.assertTrue(statements)
                if statements[0].startswith(("update jobs set claim_expires_at=", "select 1 from jobs where id=%s")):
                    # Capacity waits checkpoint the lease BEFORE any connection
                    # holding Vault locks. Audit this autonomous transaction too;
                    # it must not be mistaken for the fenced mutation below. A
                    # checkpoint inside the renew window reads the same
                    # predicate instead of extending the lease.
                    self.assertEqual(len(statements), 1)
                    self.assertIn("claim_expires_at > %s", statements[0])
                    self.assertIn("and claim_token=%s", statements[0])
                    self.assertIn("status not in ('completed', 'failed', 'cancelled')", statements[0])
                else:
                    self.assertIn("from vaults where id=%s for share", statements[0])
                    self.assertTrue(any("for share" in sql for sql in statements))
                    fenced_units += 1
            self.assertGreaterEqual(fenced_units, 1)
        combined = "\n".join(sql for units in recordings for unit in units for sql in unit)
        for sql in ("insert into upload_intent_jobs", "insert into upload_intents",
                    "update upload_intents set token=", "update upload_intents set settled=true",
                    "update upload_intents set send_started=true", "insert into upload_remediation_facts",
                    "update jobs set archive_version_id=", "update jobs set upload_plaintext_sha256=",
                    "update archive_versions set verification_method=", "from upload_receipts"):
            self.assertIn(sql, combined)
        self.assertNotIn("for key share", combined, "Lifecycle/admission need SHARE, not KEY SHARE")

    def test_target_waits_for_path_writer_then_reads_committed_path(self):
        from app.services import upload_provenance as provenance
        job_id = self.seed()[0]
        with PostgresConnection(self.path) as c:
            job = c.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job_id,)).fetchone()
        started = threading.Event()
        reader_pid = []
        def read_target():
            with PostgresConnection(self.path) as c:
                c.execute("SET LOCAL statement_timeout='10s'")
                reader_pid.append(c.info.backend_pid)
                started.set()
                return provenance.target(c, job, key="key", provider="fake", shared=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with PostgresConnection(self.path) as writer:
                # Deliberately do not UPDATE vaults. A joined locking SELECT
                # must not accidentally rely on a changed Vault tuple/EPQ to
                # refresh its snapshot after waiting on this explicit lock.
                writer.execute("SELECT id FROM vaults WHERE id=2 FOR NO KEY UPDATE")
                writer.execute("UPDATE file_paths SET path='renamed' WHERE vault_file_id=%s AND valid_to IS NULL", (job["vault_file_id"],))
                future = pool.submit(read_target)
                self.assertTrue(started.wait(5))
                # Ensure the locking statement took its pre-wait snapshot
                # BEFORE the writer commits, not merely that the thread began.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    writer.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = writer.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (reader_pid[0],)).fetchone()
                    if waiting and waiting["wait_event_type"] == "Lock":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Target reader did not wait for the Vault lock")
            with self.assertRaises(provenance.UploadBlocked) as caught:
                future.result(timeout=15)
            self.assertEqual(caught.exception.reason, "identity_changed")

    def test_admission_and_catalog_locks_do_not_rewrite_the_vault(self):
        from app.services import job_admission, operation_policies
        from app.services.catalog_events import CatalogEventStore
        self.seed()
        with PostgresConnection(self.path) as c:
            before = c.execute("SELECT xmin::text AS xmin,ctid::text AS ctid FROM vaults WHERE id=2").fetchone()
            job_admission.lock_vault(c, 2)
            ArchiveCatalog(c)._vault_lock_touch(2)
            CatalogEventStore(c)._lock_vault(2)
            operation_policies.set_policy(c, 2, operation_policies.get_policy(c, 2))
            after = c.execute("SELECT xmin::text AS xmin,ctid::text AS ctid FROM vaults WHERE id=2").fetchone()
            self.assertEqual(after, before)

    def test_551_shared_catalog_units_overlap_on_one_vault(self):
        jobs = self.seed(2)
        with PostgresConnection(self.path) as c:
            for job_id in jobs:
                self.assertIsNotNone(ArchiveCatalog(c).claim_job(job_id=job_id,
                    claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(),
                    claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                    restore_due_before=storage.now_iso()))
            rows = c.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id ORDER BY j.id").fetchall()
        overlap = threading.Barrier(2)
        def run(job):
            def mutation(catalog):
                # Both transactions already locked the same Vault. An exclusive
                # target (including a hidden upgrade) cannot reach this twice.
                from app.services import upload_provenance as provenance
                pinned = provenance.target(catalog.connection, job, key="key", provider="fake", shared=True)
                provenance.require_target(catalog.connection, job, pinned, shared=True)
                overlap.wait(timeout=10)
                return catalog.get_job_target(job["id"])["job_id"]
            return storage._upload_catalog_unit(job, mutation, shared=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, job) for job in rows]
            self.assertEqual([f.result(timeout=20) for f in futures], jobs)

    def test_551_share_fences_lifecycle_admission_and_exclusive_publication(self):
        import psycopg
        from app.services import job_admission, upload_provenance as provenance
        job_id = self.seed()[0]
        with PostgresConnection(self.path) as c:
            job = c.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job_id,)).fetchone()
        with PostgresConnection(self.path) as holder:
            provenance.target(holder, job, key="key", provider="fake", shared=True)
            writers = [
                lambda c: provenance.target(c, job, key="key", provider="fake"),
                lambda c: job_admission.lock_vault(c, 2),
                lambda c: c.execute("SELECT id FROM vaults WHERE id=2 FOR UPDATE"),
                *[lambda c, column=column: c.execute(f"UPDATE vaults SET {column}={column} WHERE id=2")
                  for column in ("relocation_state", "decommission_state", "source_root", "root_identity", "paused")],
            ]
            for writer in writers:
                with self.subTest(writer=writer), PostgresConnection(self.path) as c:
                    c.execute("SET LOCAL lock_timeout='200ms'")
                    with self.assertRaises(psycopg.errors.LockNotAvailable):
                        writer(c)
                    c.rollback()
            # The exclusive default is intentional, including final publication.
            with PostgresConnection(self.path) as c:
                c.execute("SET LOCAL lock_timeout='200ms'")
                with patch.object(storage, "db", return_value=c), self.assertRaises(psycopg.errors.LockNotAvailable):
                    storage._upload_catalog_unit(job, lambda cat: None)
        with PostgresConnection(self.path) as c:
            job_admission.lock_vault(c, 2)  # available again after shared commit

    def test_551_pause_and_cancel_pending_keep_a_leased_upload_and_its_receipt(self):
        from app.main import cancel_pending_uploads, PendingUploadCancelAction
        from app.services import upload_provenance as provenance, vault_pause
        running, pending = self.seed(2)
        sending, release = threading.Event(), threading.Event()
        original = self.provider.upload
        def held_send(*args, **kwargs):
            with PostgresConnection(self.path) as c:
                intent = provenance.load_for_job(c, running)
                self.assertTrue(intent["send_started"])
            sending.set()
            self.assertTrue(release.wait(15))
            return original(*args, **kwargs)
        with patch.object(storage, "upload_file", side_effect=held_send), ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.run_job, running)
            try:
                self.assertTrue(sending.wait(15))
                with PostgresConnection(self.path) as c:
                    claim = c.execute("SELECT claim_token FROM jobs WHERE id=%s", (running,)).fetchone()["claim_token"]
                    vault_pause.set_paused(c, vault_id=2, paused=True, user_id=1)
                    self.assertEqual(c.execute("SELECT claim_token FROM jobs WHERE id=%s", (running,)).fetchone()["claim_token"], claim)
                result = cancel_pending_uploads(PendingUploadCancelAction(path=""), user={"id": 1}, vault={"id": 2})
                self.assertEqual(result["cancelled_count"], 1)
                with PostgresConnection(self.path) as c:
                    self.assertTrue(c.execute("SELECT paused FROM vaults WHERE id=2").fetchone()["paused"])
                    self.assertEqual(c.execute("SELECT status FROM jobs WHERE id=%s", (pending,)).fetchone()["status"], "cancelled")
                    self.assertIsNone(ArchiveCatalog(c).claim_job(job_id=pending, claim_token=uuid.uuid4().hex,
                        claimed_at=storage.now_iso(), claim_expires_at=storage._claim_expiry_at(),
                        now=storage.now_iso(), restore_due_before=storage.now_iso()))
            finally:
                release.set()
            self.assertTrue(work.result(timeout=30))
        self.assert_completed(1, cancelled_count=1)
        with PostgresConnection(self.path) as c:
            intent = provenance.load_for_job(c, running)
            self.assertEqual(provenance.receipt(c, intent["id"])["VersionId"], self.provider.puts[0][1])
            self.assertTrue(c.execute("SELECT paused FROM vaults WHERE id=2").fetchone()["paused"])

    def test_551_shared_database_retries_preserve_snapshot_and_one_exact_put(self):
        from app.services import upload_provenance as provenance
        seams = ((provenance, "prepare"), (provenance, "mark_send_started"),
                 (ArchiveCatalog, "set_upload_snapshot"), (ArchiveCatalog, "set_version_verification_method"))
        for code in ("40P01", "40001"):
            for owner, name in seams:
                with self.subTest(sqlstate=code, seam=name):
                    job_id = self.seed(prefix=f"{code}-{name}")[0]
                    original = getattr(owner, name)
                    attempts = []
                    def fail_once(*args, **kwargs):
                        result = original(*args, **kwargs)
                        connection = args[0].connection if owner is ArchiveCatalog else args[0]
                        attempts.append(result)
                        if len(attempts) == 1:
                            connection.execute(f"DO $$ BEGIN RAISE EXCEPTION 'shared unit abort' USING ERRCODE='{code}'; END $$")
                        return result
                    # A real function retains descriptor binding for catalog
                    # methods; MagicMock.side_effect would drop their `self`.
                    with patch.object(owner, name, fail_once):
                        self.assertTrue(self.run_job(job_id))
                    self.assertGreaterEqual(len(attempts), 2)
                    with PostgresConnection(self.path) as c:
                        row = c.execute("""SELECT j.status,j.upload_snapshot_json,i.snapshot_json,
                            i.send_started,av.provider_version_id,av.integrity
                            FROM jobs j JOIN upload_intent_jobs b ON b.job_id=j.id
                            JOIN upload_intents i ON i.id=b.intent_id
                            JOIN archive_versions av ON av.id=j.archive_version_id WHERE j.id=%s""", (job_id,)).fetchone()
                    self.assertEqual(row["status"], "completed")
                    self.assertEqual(row["integrity"], "verified")
                    self.assertTrue(row["send_started"])
                    self.assertEqual(row["upload_snapshot_json"], row["snapshot_json"])
                    self.assertEqual(sum(v == row["provider_version_id"] for _, v in self.provider.puts), 1)
                    self.assert_completed(len(self.provider.puts))

    def test_551_send_started_commit_abort_replays_db_before_any_provider_send(self):
        from app.services import upload_provenance as provenance
        for code in ("40P01", "40001"):
            with self.subTest(sqlstate=code):
                job_id = self.seed(prefix=f"commit-{code}")[0]
                with PostgresConnection(self.path) as c:
                    c.execute(f"CREATE SEQUENCE shared_commit_{code}")
                    c.execute(f"""CREATE FUNCTION shared_commit_{code}() RETURNS trigger LANGUAGE plpgsql AS $$
                        BEGIN IF NEW.send_started AND nextval('shared_commit_{code}')=1 THEN
                        RAISE EXCEPTION 'send commit abort' USING ERRCODE='{code}'; END IF; RETURN NEW; END $$""")
                    c.execute(f"""CREATE CONSTRAINT TRIGGER shared_send_commit AFTER UPDATE ON upload_intents
                        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION shared_commit_{code}()""")
                sends = []
                original = self.provider.upload
                def checked_send(*args, **kwargs):
                    with PostgresConnection(self.path) as c:
                        intent = provenance.load_for_job(c, job_id)
                        self.assertTrue(intent["send_started"], "send-started must COMMIT before provider I/O")
                        self.assertEqual(kwargs["metadata"][provenance.TOKEN_KEY], intent["token"])
                        self.assertEqual(json.loads(intent["snapshot_json"])["plaintext_sha256"], hashlib.sha256(PAYLOAD).hexdigest())
                        sends.append(intent["id"])
                    return original(*args, **kwargs)
                with patch.object(storage, "upload_file", side_effect=checked_send):
                    self.assertTrue(self.run_job(job_id))
                self.assertEqual(len(sends), 1)
                self.assert_completed(len(self.provider.puts))
                with PostgresConnection(self.path) as c:
                    c.execute("DROP TRIGGER shared_send_commit ON upload_intents")

    # #535: a burst of small files on 16 workers. Both bounds are deliberately
    # far from the values measured on the PR (see its body) so a slow CI runner
    # has generous headroom. The connection bound catches a return to one
    # PostgreSQL connection per DB unit (tens per file); the throughput floor
    # catches severe slowdowns, not small drifts or pool/lock effects alone.
    # Local 512-file measurements: baseline 2.0 files/s, pooled 3.5 files/s.
    THROUGHPUT_FILES = 2048
    THROUGHPUT_MIN_FILES_PER_SECOND = 0.5
    THROUGHPUT_MAX_NEW_CONNECTIONS = 200
    THROUGHPUT_MAINTENANCE_SECONDS = 0.02

    def test_535_small_file_burst_keeps_throughput_and_reuses_connections(self):
        import psycopg
        from app import database
        # Size the pool as production does for OPERATION_CONCURRENCY=16.
        dsn = database_dsn(self.path)
        self.enterContext(patch("app.database.settings",
                                SimpleNamespace(database_dsn=dsn, operation_concurrency=16)))
        database.close_pools()
        self.addCleanup(database.close_pools)
        count = stress_file_count(self.THROUGHPUT_FILES)
        jobs = self.seed(count, prefix="535")
        pending = self.seed(prefix="cancel-pending")[0]
        # The harness claims through its own long-lived connections, opened
        # before counting starts, so every counted connection is FrostVault's.
        claimers = [PostgresConnection(self.path) for _ in range(16)]
        for claimer in claimers:
            self.addCleanup(claimer.close)
        opened = []
        opened_lock = threading.Lock()
        class_connect = psycopg.Connection.connect.__func__
        module_connect = psycopg.connect

        def counted_class_connect(cls, *args, **kwargs):
            with opened_lock:
                opened.append(cls)
            return class_connect(cls, *args, **kwargs)

        def counted_module_connect(*args, **kwargs):
            with opened_lock:
                opened.append(psycopg.Connection)
            return module_connect(*args, **kwargs)

        stop = threading.Event()
        maintenance_passes = []
        start = threading.Barrier(19)
        sending, controls_done = threading.Event(), threading.Event()
        first_send_lock = threading.Lock()
        control_times = []
        upload = self.provider.upload

        def held_first_send(*args, **kwargs):
            # Keep one leased upload in the provider boundary until pause and
            # cancel-pending commit. The lease must finish despite the hold.
            with first_send_lock:
                first = not sending.is_set()
                if first:
                    sending.set()
            if first:
                self.assertTrue(controls_done.wait(30))
            return upload(*args, **kwargs)

        def controls():
            from app.main import cancel_pending_uploads, PendingUploadCancelAction
            from app.services import vault_pause
            start.wait(timeout=20)
            self.assertTrue(sending.wait(30))
            try:
                for paused in (True, False):
                    began = time.monotonic()
                    with PostgresConnection(self.path) as c:
                        c.execute("SET LOCAL lock_timeout='10s'")
                        vault_pause.set_paused(c, vault_id=2, paused=paused, user_id=1)
                    control_times.append(time.monotonic() - began)
                    self.assertLess(control_times[-1], 10, "Pause/resume writer starved behind shared upload steps")
                    if paused:
                        with PostgresConnection(self.path) as c:
                            self.assertIsNone(ArchiveCatalog(c).claim_job(
                                job_id=pending, claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(),
                                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                                restore_due_before=storage.now_iso()))
                        began = time.monotonic()
                        result = cancel_pending_uploads(PendingUploadCancelAction(path="cancel-pending"),
                            user={"id": 1}, vault={"id": 2})
                        control_times.append(time.monotonic() - began)
                        self.assertEqual(result["cancelled_count"], 1)
                        self.assertLess(control_times[-1], 10, "Cancel-pending writer starved")
            finally:
                controls_done.set()

        def maintenance():
            start.wait(timeout=20)
            while not stop.is_set():
                maintenance_passes.append(aggregates.process_directory_aggregate_maintenance(vault_id=2))
                stop.wait(self.THROUGHPUT_MAINTENANCE_SECONDS)

        def worker(claimer, batch):
            start.wait(timeout=20)
            for job_id in batch:
                token = uuid.uuid4().hex
                deadline = time.monotonic() + 30
                while True:
                    claimed = ArchiveCatalog(claimer).claim_job(
                        job_id=job_id, claim_token=token, claimed_at=storage.now_iso(),
                        claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                        restore_due_before=storage.now_iso(),
                    )
                    claimer.commit()
                    if claimed is not None:
                        break
                    self.assertLess(time.monotonic(), deadline, "Job did not resume after bounded pause")
                    controls_done.wait(0.01)
                job = claimer.execute("""SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix
                    FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s""", (job_id,)).fetchone()
                claimer.commit()
                self.assertIsNotNone(claimed)
                self.assertTrue(storage.process_job(job))

        with patch.object(psycopg.Connection, "connect", classmethod(counted_class_connect)), \
                patch.object(psycopg, "connect", counted_module_connect), \
                patch.object(storage, "upload_file", side_effect=held_first_send), \
                ThreadPoolExecutor(max_workers=18) as pool:
            upkeep = pool.submit(maintenance)
            control = pool.submit(controls)
            workers = [pool.submit(worker, claimers[i], jobs[i::16]) for i in range(16)]
            start.wait(timeout=20)
            started = time.monotonic()
            try:
                for future in workers:
                    future.result(timeout=4800)
            finally:
                elapsed = time.monotonic() - started
                stop.set()
            upkeep.result(timeout=30)
            control.result(timeout=30)
        self.assertTrue(maintenance_passes)
        self.assertEqual(len(control_times), 3)
        rate = count / elapsed
        stats = database._pool_for(dsn).get_stats()
        print(f"#535 throughput: {count} small files / 16 workers / "
              f"{elapsed:.1f} s / {rate:.1f} files/s / {len(opened)} new PostgreSQL connections "
              f"/ {stats.get('requests_num', 0)} pooled checkouts / pause,cancel,resume {control_times}")
        self.assert_completed(count, cancelled_count=1)
        self.assertGreaterEqual(rate, self.THROUGHPUT_MIN_FILES_PER_SECOND)
        self.assertLessEqual(len(opened), self.THROUGHPUT_MAX_NEW_CONNECTIONS)

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
                observed_at="2026-07-20", scan_id="old", origin="upload",
            )
            catalog.mark_version_verified(previous, plaintext_sha256=hashlib.sha256(b"old").hexdigest(), verified_at="2026-07-20")
            owner = connection.execute("SELECT vault_file_id FROM archive_versions WHERE id=%s", (previous,)).fetchone()["vault_file_id"]
            connection.execute("INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,requested_at,updated_at,upload_plaintext_sha256) VALUES (2,%s,%s,%s,'upload','completed','2026-07-20','2026-07-20',%s)", (owner,previous,path,hashlib.sha256(b"old").hexdigest()))
        self.provider.objects[(key, "previous-only-copy")] = b"old"
        original_verify = ArchiveCatalog.mark_version_verified
        original_purge = ArchiveCatalog.mark_versions_purged
        attempts = {"verify": 0, "cleanup": 0}

        def verify_once(catalog, *args, **kwargs):
            original_verify(catalog, *args, **kwargs)
            attempts["verify"] += 1
            if attempts["verify"] == 1:
                catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced verification abort' USING ERRCODE='40001'; END $$")

        def cleanup_once(catalog, *args, **kwargs):
            original_purge(catalog, *args, **kwargs)
            attempts["cleanup"] += 1
            if attempts["cleanup"] == 1:
                # Even a verified version is not a completed Job at this seam.
                state = catalog.connection.execute("SELECT status FROM jobs WHERE id=%s", (jobs[0],)).fetchone()["status"]
                self.assertEqual(state, "uploading")
                catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced cleanup abort' USING ERRCODE='40P01'; END $$")

        with patch.object(ArchiveCatalog, "mark_version_verified", verify_once), patch.object(
            ArchiveCatalog, "mark_versions_purged", cleanup_once
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
