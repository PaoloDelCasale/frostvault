"""#464 core acceptance through real admission/claim/process_job on disposable PG16.

TEST_POSTGRES_URL is mandatory; never skipped locally as database evidence.
Provider persists actual version bytes/Metadata across worker invocations.

Ported to the rebuilt model (#598, ADR-0035): upload attempts and receipts
replace intents, the key is ``vaults/<uuid>/<path>`` and a keeper is always a
version landed by an earlier upload attempt. Objects no attempt of the file
wrote are never adopted by reading equal bytes (#591), so the "historical
orphan" cases now assert that they block and are retained. Removed, with the
case that covers the same property on the new model:

- ``test_completed_inventory_is_not_reused_for_a_later_scan_remediation``:
  cleanup inventories no longer exist; an unknown object after completion is
  ``test_upload_cleanup.test_unknown_object_with_equal_bytes_is_never_adopted_or_deleted``.
- ``test_cold_unreadable_historical_content_retains_both_with_safe_remedy``:
  no historical GET is ever made; the unknown object blocks before reading
  (``test_historical_different_or_unreadable_preserves_keeper_with_actionable_resume``).
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.main import queue_jobs
from app.services import upload_provenance as provenance
from app.services.vault_relocation import enroll_vault_root_identity
from app.services.vaults import VAULT_ROW_SQL
from app.services.upload_snapshot import UploadSnapshot
from tests.catalog_support import seed_vault
from tests.owner_fixtures import insert_owner
from tests.postgres import PostgresConnection, database_dsn, run_alembic
from tests.upload_provenance_provider import VersionedProvider
from tests.upload_support import claimed_job_row, job_row

A = b"immutable content A"
B = b"immutable content B"


def prepare_unsent(case, job_id):
    """Crash fixture: a committed prepared-but-unsent attempt.

    UploadExecution commits prepare+send-started in one unit, so a failure in
    mark_send_started never leaves this state behind; a DB retry between the
    two units can. Keep exercising its refresh and closure with real facts.
    """
    snapshot, _ = storage.prepare_upload_snapshot(case.file, lambda: None)
    with PostgresConnection(case.db_path) as connection:
        job = job_row(connection, job_id)
        pinned = provenance.target(connection, job,
            key=f"vaults/{job['vault_uuid']}/{job['path']}",
            provider=provenance.provider_identity(case.provider))
        detail = connection.execute(
            "SELECT ju.verification_mode, f.generation FROM job_uploads ju JOIN files f ON f.id=%s "
            "WHERE ju.job_id=%s", (job["file_id"], job_id)).fetchone()
        return provenance.prepare(connection, job, pinned=pinned, snapshot=snapshot,
            requested=detail["verification_mode"], local_generation=detail["generation"],
            before={"versions": [], "markers": []}, timestamp=storage.now_iso())


class UploadProvenanceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "report.txt"
        self.file.write_bytes(A)
        self.db_path = self.root / "catalog"
        result = run_alembic(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.provider = VersionedProvider()
        with PostgresConnection(self.db_path) as connection:
            self.assertTrue(connection.execute("SHOW server_version_num").fetchone()["server_version_num"].startswith("16"))
            insert_owner(connection)
            seed_vault(connection, vault_id=2, source_root=str(self.source))
            enroll_vault_root_identity(connection, 2, str(self.source))
            self.file_id = ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="report.txt", file_type="regular", size=len(A), mtime_ns=self.file.stat().st_mtime_ns, observed_at=storage.now_iso())
            self.uuid = connection.execute("SELECT uuid FROM vaults WHERE id=2").fetchone()["uuid"]
        self.key = f"vaults/{self.uuid}/report.txt"
        self.enterContext(patch("app.database.settings", SimpleNamespace(database_dsn=database_dsn(self.db_path))))
        self.enterContext(patch("app.storage.validate_cloud_vault"))
        self.enterContext(patch("app.storage.s3_client", return_value=self.provider))
        self.enterContext(patch("app.storage.upload_file", side_effect=self.provider.upload))

    def queue(self):
        queued = queue_jobs("report.txt", "upload", 2, 1)
        self.assertEqual(queued["item_count"], 1)
        with PostgresConnection(self.db_path) as connection:
            return connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]

    def run_job(self, job_id):
        with PostgresConnection(self.db_path) as connection:
            claimed = ArchiveCatalog(connection).claim_job(job_id=job_id, claim_token=str(uuid.uuid4()), claimed_at=storage.now_iso(), claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(), restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed)
            job = claimed_job_row(connection, job_id, claimed)
        return storage.process_job(job)

    def row(self, job_id):
        with PostgresConnection(self.db_path) as connection:
            return connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def resume(self):
        self.provider.hook = lambda *args, **kwargs: None
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()
            result = provenance.schedule_resume(connection, vault=vault, path="report.txt", user_id=1, timestamp=storage.now_iso())
        self.run_job(result["job_id"])
        return result["job_id"]

    def http_client(self):
        from fastapi.testclient import TestClient
        from app.main import app, current_user, current_vault
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[current_user] = lambda: {"id":1}
        app.dependency_overrides[current_vault] = lambda: vault
        def restore():
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
        self.addCleanup(restore)
        client = TestClient(app)  # No lifespan/background services or native server.
        self.addCleanup(client.close)
        return client

    def assert_keeper(self, job_id):
        row = self.row(job_id)
        self.assertEqual(row["status"], "completed", row["message"])
        with PostgresConnection(self.db_path) as connection:
            version = connection.execute("SELECT * FROM archive_versions WHERE id=%s", (row["archive_version_id"],)).fetchone()
        self.assertEqual((version["integrity"], version["availability"]), ("verified", "available"))
        self.assertIn((version["object_key"], version["provider_version_id"]), self.provider.objects)
        return version

    def test_upload_rename_free_and_recover_preserves_archived_timestamp(self):
        original = 1_597_614_120_123_456_789
        os.utime(self.file, ns=(original, original))
        self.observe_content(A, mtime_ns=original)
        job_id = self.queue()
        self.run_job(job_id)
        self.assert_keeper(job_id)
        renamed = self.source / "renamed.txt"
        self.file.rename(renamed)
        # A later Local Copy observation is not the archived timestamp.
        os.utime(renamed, ns=(original + 1000, original + 1000))
        with PostgresConnection(self.db_path) as connection:
            catalog = ArchiveCatalog(connection)
            timestamp = storage.now_iso()
            catalog.rename_file(self.file_id, new_path="renamed.txt", changed_at=timestamp, vault_id=2)
            catalog.observe_local_copy(vault_id=2, path="renamed.txt", file_type="regular",
                size=len(A), mtime_ns=renamed.stat().st_mtime_ns, observed_at=timestamp)
            generation = connection.execute("SELECT generation FROM files WHERE id=%s", (self.file_id,)).fetchone()["generation"]
            self.assertTrue(catalog.publish_local_fingerprint(self.file_id, generation=generation,
                plaintext_sha256=hashlib.sha256(A).hexdigest(), size=len(A),
                mtime_ns=renamed.stat().st_mtime_ns, match_current=True))
        def process(action):
            queue_jobs("renamed.txt", action, 2, 1)
            with PostgresConnection(self.db_path) as connection:
                queued = connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
            self.run_job(queued)
            row = self.row(queued)
            self.assertEqual(row["status"], "completed", row["message"])
            return row
        renamed_job = process("rename")
        version = self.assert_keeper(renamed_job["id"])
        head = self.provider.head_object(Bucket="bucket", Key=version["object_key"], VersionId=version["provider_version_id"])
        self.assertEqual(head["Metadata"]["frostvault-mtime-ns"], str(original))
        process("free-space")
        self.assertFalse(renamed.exists())
        process("recover")
        self.assertEqual(renamed.read_bytes(), A)
        self.assertEqual(renamed.stat().st_mtime_ns, original)

    def test_module_run_accepts_a_claimed_job_and_returns_its_durable_outcome(self):
        from app.services.upload_execution import UploadExecution
        job_id = self.queue()
        with PostgresConnection(self.db_path) as connection:
            claimed = ArchiveCatalog(connection).claim_job(job_id=job_id,
                claim_token=str(uuid.uuid4()), claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed)
            job = claimed_job_row(connection, job_id, claimed)
        self.assertIsNone(storage._claim_token_for(job_id))
        outcome = UploadExecution(storage).run(job)
        version = self.assert_keeper(job_id)
        self.assertEqual((outcome.job_id, outcome.status, str(outcome.archive_version_id)),
                         (job_id, "completed", str(version["id"])))
        self.assertIsNone(storage._claim_token_for(job_id), "The caller's context is restored")
        self.assertEqual(self.provider.puts, 1)

    def test_committed_intent_send_snapshot_and_token_precede_provider_mutation(self):
        def inspect(stage, **kwargs):
            if stage == "before_put":
                with PostgresConnection(self.db_path) as connection:
                    attempt = connection.execute("SELECT * FROM upload_attempts").fetchone()
                    snapshot = connection.execute("SELECT * FROM upload_snapshots WHERE id=%s",
                                                  (attempt["snapshot_id"],)).fetchone()
                self.assertIsNotNone(attempt["send_started_at"])
                self.assertIsNone(attempt["settled_at"])
                self.assertEqual(bytes(snapshot["plaintext_sha256"]).hex(), kwargs["snapshot"].plaintext_sha256)
                self.assertEqual(kwargs["metadata"][provenance.TOKEN_KEY], str(attempt["token"]))
                self.assertEqual(kwargs["metadata"]["frostvault-mtime-ns"], str(self.file.stat().st_mtime_ns))
                self.assertEqual(snapshot["mtime_ns"], self.file.stat().st_mtime_ns)
        self.provider.hook = inspect
        job = self.queue()
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.provider.gets, 0)

    def test_definitively_refused_put_waits_and_resumes_after_credentials_recover(self):
        from botocore.exceptions import ClientError
        refused = ClientError({'Error':{'Code':'InvalidAccessKeyId'},'ResponseMetadata':{'HTTPStatusCode':403}},'PutObject')
        # The SDK seam separately proves this fact is set only after joining I/O
        # with no receipt. Replay its contract through real admission/DB/worker.
        refused.upload_no_write_operation = 'PutObject'
        job = self.queue()
        record = provenance.record_no_write
        attempts = []
        def aborted_publication(connection,*args,**kwargs):
            record(connection,*args,**kwargs)
            attempts.append(1)
            if len(attempts) == 1:
                connection.execute("DO $$ BEGIN RAISE EXCEPTION 'forced refusal publication abort' USING ERRCODE='40001'; END $$")
        with patch('app.storage.upload_file',side_effect=refused) as send, patch.object(
            provenance,'record_no_write',side_effect=aborted_publication
        ):
            self.assertFalse(self.run_job(job))
            send.assert_called_once()
        self.assertEqual(len(attempts),2,'Replay only the refused-result DB publication, never the PUT')
        first = self.row(job)
        self.assertEqual((first['status'],first['retry_count']),('queued',0))
        with PostgresConnection(self.db_path) as connection:
            refused_attempt = connection.execute('SELECT * FROM upload_attempts WHERE job_id=%s', (job,)).fetchone()
            self.assertEqual((refused_attempt['outcome'], refused_attempt['no_write_operation']), ('no_write', 'PutObject'))
            self.assertIsNone(provenance.receipt(connection, str(refused_attempt['id'])))
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.provider.puts,1,'One successful object, no blind replay of ambiguous PUT')
        with PostgresConnection(self.db_path) as connection:
            rows = connection.execute('SELECT * FROM upload_attempts WHERE job_id=%s ORDER BY created_at', (job,)).fetchall()
        # The same snapshot is sent again by a new attempt with its own token.
        self.assertEqual([row['outcome'] for row in rows], ['no_write', 'landed'])
        self.assertEqual(rows[0]['snapshot_id'], rows[1]['snapshot_id'])
        self.assertNotEqual(rows[0]['token'], rows[1]['token'])

    def test_auth_failure_after_provider_write_keeps_intent_and_reconciles_without_put(self):
        from botocore.exceptions import ClientError
        def refused_head(stage,**kwargs):
            if stage == 'head':
                raise ClientError({'Error':{'Code':'AccessDenied'},'ResponseMetadata':{'HTTPStatusCode':403}},'HeadObject')
        self.provider.hook = refused_head
        job = self.queue()
        self.assertFalse(self.run_job(job))
        self.assertEqual(self.row(job)['status'],'queued')
        self.assertEqual(self.provider.puts,1)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection,job)
            self.assertTrue(intent.send_started)
            self.assertEqual(connection.execute('SELECT count(*) n FROM upload_receipts').fetchone()['n'],0)
        self.provider.hook = lambda *args,**kwargs: None
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.provider.puts,1)

    def test_prewrite_commit_failure_never_sends(self):
        job = self.queue()
        def fail(connection, *args, **kwargs):
            raise RuntimeError("prewrite commit unavailable")
        with patch.object(provenance, "mark_send_started", side_effect=fail):
            self.run_job(job)
        self.assertEqual(self.provider.puts, 0)
        self.assertEqual(self.provider.objects, {})
        with PostgresConnection(self.db_path) as connection:
            self.assertIsNone(provenance.load_for_job(connection, job),
                "Prepare and send-started roll back together before any PUT")

    def test_callback_and_return_append_only_one_positively_committed_receipt(self):
        job = self.queue()
        original = provenance.append_receipt
        with patch.object(provenance, "append_receipt", wraps=original) as append:
            self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(append.call_count, 1)
        self.assertEqual(self.provider.puts, 1)

    def test_unknown_receipt_commit_retries_the_retained_ack_not_the_send(self):
        import psycopg
        original = provenance.append_receipt
        appends = []
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            appends.append(1)
            if len(appends) == 1:
                raise psycopg.OperationalError("connection lost after receipt commit")
        def send(job, *, source, key):
            # Same retained-response contract as the SDK Adapter: an uncertain
            # callback commit exposes the response for the caller's DB fallback.
            self.provider.puts += 1
            version = self.provider.add(key, source.read_bytes(), job["_upload_metadata"])
            raw = self.provider.head_object(Bucket=job["s3_bucket"], Key=key, VersionId=version)
            try:
                job["_on_upload_acknowledged"](raw)
            except BaseException as exc:
                exc.upload_response = raw
                raise
            return raw
        job = self.queue()
        with patch.object(provenance, "append_receipt", side_effect=uncertain), \
                patch.object(storage, "upload_s3_file", side_effect=send):
            self.run_job(job)
        # The operation failed, but the ack survived the fallback after an
        # unknown commit; a later Job reconciles this one exact provider write.
        self.assertEqual(len(appends), 2)
        self.assertEqual(self.provider.puts, 1)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertIsNotNone(provenance.receipt(connection, intent.id))
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_receipts").fetchone()["n"], 1)
        self.assertEqual(self.row(job)["status"], "retrying")
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE jobs SET run_after=NULL WHERE id=%s", (job,))
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.provider.puts, 1)

    def test_provider_accepted_lost_response_reconciles_token_without_second_put(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("lost response")) if stage == "accepted" else None
        original = self.queue()
        self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM upload_receipts").fetchone()["n"], 0)
        resumed = self.resume()
        self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertEqual(self.row(original)["status"], "failed", "#459 owns historical resolution")

    def test_ack_survives_error_before_flush_head_linkage_and_commit(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("post raw ack fault")) if stage == "acknowledged" else None
        original = self.queue()
        self.run_job(original)
        self.assertIsNone(self.row(original)["archive_version_id"])
        with PostgresConnection(self.db_path) as connection:
            receipt = provenance.receipt(connection, connection.execute("SELECT id FROM upload_attempts").fetchone()["id"])
            self.assertIn((self.key, receipt["VersionId"]), self.provider.objects)
        # Source B and changed requested mode must not redefine A's expected proof.
        self.observe_content(B)
        from app.services.operation_policies import OperationPolicy, set_policy
        with PostgresConnection(self.db_path) as connection:
            set_policy(connection, 2, OperationPolicy(upload_verification="read_back"))
        resumed = self.resume()
        keeper = self.assert_keeper(resumed)
        self.assertEqual(bytes(keeper["plaintext_sha256"]), hashlib.sha256(A).digest())
        self.assertEqual(keeper["verification_method"], "s3_checksum")
        with PostgresConnection(self.db_path) as connection:
            local = connection.execute("SELECT * FROM files WHERE id=%s", (self.file_id,)).fetchone()
        self.assertNotEqual(local["matched_version_id"], keeper["id"])
        self.assertEqual(self.provider.puts, 1)

    def test_missing_individual_file_does_not_prevent_materialized_resume(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue()
        self.run_job(original)
        self.file.unlink()
        resumed = self.resume()
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)

    def test_explicit_reconcile_http_admission_is_independent_of_protected_upload_409(self):
        # A send that landed but whose Job failed terminally (retry budget
        # exhausted) leaves an exact reconciliation; an ordinary upload is 409.
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("lost response")) if stage == "accepted" else None
        original = self.queue()
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE jobs SET retry_count=%s WHERE id=%s", (storage.UPLOAD_RETRY_MAX_ATTEMPTS, original))
        self.run_job(original)
        self.assertEqual(self.row(original)["status"],"failed")
        self.provider.hook = lambda *args, **kwargs: None
        client = self.http_client()
        with patch("app.main._allowed_hosts",return_value=[]):
            ordinary = client.post("/api/upload?vault_id=2",json={"path":"report.txt"})
            self.assertEqual(ordinary.status_code,409,ordinary.text)
            resumed = client.post("/api/upload/reconcile?vault_id=2",json={"path":"report.txt"})
        self.assertEqual(resumed.status_code,202,resumed.text)
        job_id = resumed.json()["job_id"]
        with PostgresConnection(self.db_path) as connection:
            self.assertTrue(connection.execute("SELECT reconcile_only FROM job_uploads WHERE job_id=%s", (job_id,)).fetchone()["reconcile_only"])
        self.run_job(job_id)
        self.assert_keeper(job_id)
        self.assertEqual(self.provider.puts,1)
        self.assertEqual(self.row(original)["status"],"failed","No generic history resolution")

    def seed_keeper(self):
        """A verified Current of A landed by an earlier upload Job of this file."""
        job = self.queue()
        self.run_job(job)
        keeper = self.assert_keeper(job)
        self.provider.puts = 0
        return str(keeper["id"]), keeper["provider_version_id"]

    def observe_content(self, payload, *, mtime_ns=None):
        self.file.write_bytes(payload)
        if mtime_ns is not None:
            os.utime(self.file, ns=(mtime_ns, mtime_ns))
        with PostgresConnection(self.db_path) as connection:
            ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="report.txt", file_type="regular", size=len(payload), mtime_ns=self.file.stat().st_mtime_ns, observed_at=storage.now_iso())

    def test_linked_resume_cannot_ignore_contradictory_receipt_after_publication_crash(self):
        keeper, keeper_version = self.seed_keeper()
        self.observe_content(B)

        def corrupt_ack(client, **kwargs):
            acknowledged = kwargs["on_acknowledged"]
            def contradictory(response):
                response = {**response, "ChecksumSHA256": base64.b64encode(hashlib.sha256(A).digest()).decode()}
                acknowledged(response)
            kwargs["on_acknowledged"] = contradictory
            return self.provider.upload(client, **kwargs)

        original = self.queue()
        with patch("app.storage.upload_file", side_effect=corrupt_ack):
            self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        with PostgresConnection(self.db_path) as connection:
            # The receipt contradicting the frozen snapshot blocks reconciliation.
            self.assertEqual(provenance.describe(connection, vault_id=2, path="report.txt")["reason"], "ambiguous_receipt")
            current = connection.execute("SELECT current_version_id FROM files WHERE id=%s", (self.file_id,)).fetchone()
            versions = {str(row["id"]): row for row in connection.execute("SELECT * FROM archive_versions").fetchall()}
        self.assertEqual(str(current["current_version_id"]), keeper)
        self.assertEqual((versions[keeper]["integrity"], versions[keeper]["availability"]), ("verified", "available"))
        self.assertTrue(all(row["integrity"] != "verified" for vid, row in versions.items() if vid != keeper))
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 0, 0))
        self.assertEqual(len(self.provider.objects), 2)

    def test_scan_before_publication_does_not_demote_the_exact_new_app_write(self):
        keeper, keeper_version = self.seed_keeper()
        self.observe_content(B)
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("lost response before publication")) if stage == "accepted" else None
        original = self.queue(); self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        self.assertIsNone(self.row(original)["archive_version_id"])
        self.provider.hook = lambda *args, **kwargs: None
        self.scan()
        self.assertEqual(self.provider.deletes, 0)
        resumed = self.resume()
        current = self.assert_keeper(resumed)
        self.assertNotEqual(str(current["id"]), keeper)
        self.assertEqual(bytes(current["plaintext_sha256"]), hashlib.sha256(B).digest())
        self.scan()
        with PostgresConnection(self.db_path) as connection:
            versions = connection.execute("SELECT id FROM archive_versions").fetchall()
        # The exact new app write became Current; the superseded keeper was
        # deleted by its exact VersionId, never demoted by the scan.
        self.assertEqual([str(row["id"]) for row in versions], [str(current["id"])])
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 0, 1))
        self.assertEqual(self.provider.deleted, [(self.key, keeper_version)])

    def test_provider_namespace_change_retains_all_unpublished_copies(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue(); self.run_job(original)
        before = dict(self.provider.objects)
        self.provider.meta.endpoint_url = "https://another-disposable.invalid"
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(self.provider.objects, before)
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 0, 0))

    def test_pending_send_blocks_move_admission_and_a_stale_move_worker(self):
        from app.services import vault_moves
        self.seed_keeper()
        self.observe_content(B)
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue(); self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ('completed','failed','cancelled')").fetchone()["n"], 0)
            rows = vault_moves.candidates(connection, 2)
            self.assertTrue(rows[0]["busy"])
            self.assertEqual(vault_moves.eligibility(rows[0], "GLACIER_IR"), "pending_work")
            with patch.object(vault_moves, "load_vault_plan", return_value=SimpleNamespace(transitions=[SimpleNamespace(storage_class="GLACIER_IR")])):
                with self.assertRaises(vault_moves.MoveBlocked):
                    vault_moves.schedule(connection, vault_id=2, target="GLACIER_IR", user_id=1, acknowledged=True, timestamp=storage.now_iso())
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 0, 0))

    def test_cancel_after_raw_ack_keeps_receipt_and_never_resurrects(self):
        job = self.queue()
        def cancel(stage, **kwargs):
            if stage == "acknowledged":
                storage.cancel_jobs([job])
        self.provider.hook = cancel
        self.run_job(job)
        self.assertEqual(self.row(job)["status"], "cancelled")
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertIsNotNone(provenance.receipt(connection, intent.id))
            self.assertFalse(provenance.describe(connection, vault_id=2, path="report.txt")["can_resume"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertIsNone(self.row(job)["archive_version_id"])

    def test_receipt_append_after_lease_loss_is_fact_only(self):
        job = self.queue()
        def takeover(stage, **kwargs):
            if stage == "accepted":
                with PostgresConnection(self.db_path) as connection:
                    connection.execute("UPDATE job_leases SET token=gen_random_uuid() WHERE job_id=%s", (job,))
        self.provider.hook = takeover
        self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertIsNotNone(provenance.receipt(connection, intent.id))
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM archive_versions").fetchone()["n"], 0)
        self.assertIsNone(self.row(job)["archive_version_id"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))

    def test_send_started_zero_match_without_absence_proof_remains_reconcile_only(self):
        # #484: only complete S3 reads that exclude an in-flight multipart prove
        # absence. See test_upload_ambiguous_send_recovery for the recovery path.
        self.provider.multiparts.append((self.key, "in-flight"))
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("before provider request")) if stage == "before_put" else None
        job = self.queue()
        self.run_job(job)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, resumed)
            self.assertTrue(intent.send_started)
            self.assertIsNone(intent.outcome)
            self.assertEqual(provenance.describe(connection, vault_id=2, path="report.txt")["reason"], "ambiguous_send")

    def test_multiple_tokens_never_choose_latest_or_delete(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("lost response")) if stage == "accepted" else None
        job = self.queue()
        self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
        self.provider.add(self.key, A, {provenance.TOKEN_KEY: intent.token}, version="second-token")
        before = dict(self.provider.objects)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(self.provider.objects, before)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))

    def test_historical_duplicate_adopted_without_promoting_current_snapshot(self):
        # v0.6 (#591): an object no landed attempt of this file wrote is never
        # adopted by reading equal bytes nor deleted; completion is blocked.
        self.provider.add(self.key, A, version="old-orphan")
        job = self.queue()
        self.run_job(job)
        self.assertNotEqual(self.row(job)["status"], "completed")
        self.assertEqual((self.provider.gets, self.provider.deletes), (0, 0))
        self.assertIn((self.key, "old-orphan"), self.provider.objects)
        self.assertEqual(len(self.provider.objects), 2)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(provenance.describe(connection, vault_id=2, path="report.txt")["reason"], "inventory_changed")

    def test_historical_different_or_unreadable_preserves_keeper_with_actionable_resume(self):
        self.provider.add(self.key, B, version="different-orphan")
        job = self.queue()
        self.run_job(job)
        self.assertEqual(self.row(job)["status"], "failed")
        with PostgresConnection(self.db_path) as connection:
            state = provenance.describe(connection, vault_id=2, path="report.txt")
            versions = connection.execute("SELECT * FROM archive_versions").fetchall()
        self.assertEqual(state["reason"], "inventory_changed")
        self.assertTrue(state["can_resume"])
        self.assertEqual(len(versions), 1)
        self.assertEqual(len(self.provider.objects), 2)
        self.assertEqual((self.provider.gets, self.provider.deletes), (0, 0))
        # Normal admission is still 409: exact resume is independently available.
        with self.assertRaises(Exception):
            self.queue()

    def test_process_death_after_acceptance_reconciles_persistent_provider_state(self):
        job = self.queue()
        state = self.root / "provider-state.json"
        result = subprocess.run([sys.executable, "-m", "tests.upload_provenance_crash_worker"], input=json.dumps({"dsn": database_dsn(self.db_path), "job_id": job, "state_path": str(state)}), text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 31, result.stderr)
        self.provider.load(state)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertTrue(intent.send_started)
            self.assertIsNone(provenance.receipt(connection, intent.id))
            connection.execute("UPDATE job_leases SET claimed_at='1999-12-31T00:00:00+00:00', expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=%s", (job,))
        storage.reconcile_interrupted_jobs()
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))

    def test_incompatible_receipts_append_never_overwrite_or_publish(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        job = self.queue(); self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            original = provenance.receipt(connection, intent.id)
        provenance.append_receipt(storage.db, intent.id, original, storage.now_iso())
        provenance.append_receipt(storage.db, intent.id, {"VersionId":"incompatible"}, storage.now_iso())
        before = dict(self.provider.objects)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(self.provider.objects, before)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM upload_receipts").fetchone()["n"], 2)
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))

    def test_committed_historical_proof_avoids_get_after_delete_failure(self):
        # The exact cleanup of a superseded landed version: a failed DELETE is
        # retried by the next Job without any GET or second PUT.
        keeper, keeper_version = self.seed_keeper()
        self.observe_content(B)
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("injected deletion fault")) if stage == "delete" else None
        job = self.queue(); self.run_job(job)
        self.assertNotEqual(self.row(job)["status"], "completed")
        self.provider.hook = lambda *args, **kwargs: None
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE jobs SET run_after=NULL WHERE id=%s", (job,))
        if self.row(job)["status"] == "failed":
            job = self.resume()
        else:
            self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 0))
        self.assertEqual(self.provider.deleted, [(self.key, keeper_version)])

    def test_delete_database_gap_reconciles_absence_without_get_put_or_second_delete(self):
        keeper, keeper_version = self.seed_keeper()
        self.observe_content(B)
        def die(stage, **kwargs):
            if stage == "after_delete":
                raise KeyboardInterrupt("process death after delete")
        self.provider.hook = die
        job = self.queue()
        with self.assertRaises(KeyboardInterrupt): self.run_job(job)
        self.provider.hook = lambda *args, **kwargs: None
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE job_leases SET expires_at=claimed_at + interval '1 microsecond' WHERE job_id=%s", (job,))
        storage.reconcile_interrupted_jobs()
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,0,1))

    def scan(self):
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()
        storage.scan_cloud(vault, storage.now_iso())

    def test_scan_catalog_membership_never_launders_different_content_for_upload(self):
        keeper, keeper_version = self.seed_keeper()
        self.provider.add(self.key, B, version="different-discovered")
        self.scan(); self.scan()
        self.assertEqual(self.provider.deletes, 0)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(str(connection.execute("SELECT current_version_id FROM files WHERE id=%s", (self.file_id,)).fetchone()["current_version_id"]), keeper)
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM archive_versions").fetchone()["n"], 1)
        self.observe_content(b"immutable content C")
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"], "failed")
        self.assertIn((self.key, keeper_version), self.provider.objects)
        self.assertIn((self.key, "different-discovered"), self.provider.objects)
        self.assertEqual(self.provider.deletes, 0)

    def test_scan_catalog_membership_never_launders_different_content_for_rename(self):
        keeper, keeper_version = self.seed_keeper()
        self.provider.add(self.key, B, version="different-discovered")
        self.scan()
        renamed = self.source / "renamed.txt"
        self.file.rename(renamed)
        with PostgresConnection(self.db_path) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.rename_file(self.file_id, new_path="renamed.txt", changed_at=storage.now_iso(), vault_id=2)
            catalog.observe_local_copy(vault_id=2, path="renamed.txt", file_type="regular", size=len(A),
                mtime_ns=renamed.stat().st_mtime_ns, observed_at=storage.now_iso())
        queued = queue_jobs("renamed.txt","rename",2,1)
        self.assertEqual(queued["item_count"],1)
        with PostgresConnection(self.db_path) as connection:
            job = connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
        self.run_job(job)
        self.assertEqual(self.row(job)["status"],"failed")
        self.assertIn((self.key, keeper_version),self.provider.objects)
        self.assertIn((self.key,"different-discovered"),self.provider.objects)
        self.assertEqual(self.provider.deletes,0)

    def test_source_root_takeover_during_duplicate_get_blocks_all_cleanup(self):
        self.provider.add(self.key, A, version="old-orphan")
        def replace_root(stage, **kwargs):
            if stage == "get":
                self.source.rename(self.root / "original-source")
                self.source.mkdir()
        self.provider.hook = replace_root
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"], "failed")
        self.assertEqual(self.provider.deletes, 0)
        self.assertIn((self.key,"old-orphan"), self.provider.objects)
        self.assertEqual(len(self.provider.objects), 2)

    def test_observable_external_writer_after_get_never_becomes_a_delete_target(self):
        # An external write appearing while the cleanup lists the key stops it
        # before any DELETE; nothing it did not freeze is ever deleted.
        keeper, keeper_version = self.seed_keeper()
        self.observe_content(B)
        listings = []
        def external(stage, **kwargs):
            if stage == "list" and self.provider.puts and not listings:
                listings.append(1)
                self.provider.add(self.key, A, version="new-external")
        self.provider.hook = external
        job = self.queue(); self.run_job(job)
        self.assertNotEqual(self.row(job)["status"],"completed")
        self.assertEqual(self.provider.deletes,0)
        self.assertIn((self.key, keeper_version),self.provider.objects)
        self.assertIn((self.key,"new-external"),self.provider.objects)

    def test_reused_old_path_never_deletes_another_files_sole_keeper_even_if_identical(self):
        keeper, keeper_version = self.seed_keeper()
        renamed = self.source / "renamed.txt"
        self.file.rename(renamed)
        with PostgresConnection(self.db_path) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.rename_file(self.file_id, new_path="renamed.txt", changed_at=storage.now_iso(), vault_id=2)
            catalog.observe_local_copy(vault_id=2, path="renamed.txt", file_type="regular", size=len(A),
                mtime_ns=renamed.stat().st_mtime_ns, observed_at=storage.now_iso())
        # Another file now lives at the old path with identical bytes and its
        # own landed keeper at the same key.
        self.file.write_bytes(A)
        with PostgresConnection(self.db_path) as connection:
            foreign = ArchiveCatalog(connection).observe_local_copy(vault_id=2,path="report.txt",file_type="regular",size=len(A),mtime_ns=self.file.stat().st_mtime_ns,observed_at=storage.now_iso())
        self.assertNotEqual(str(foreign), str(self.file_id))
        foreign_job = self.queue_path("report.txt")
        self.run_job(foreign_job)
        # The renamed file's keeper still sits at the old key: the foreign
        # upload never adopts or deletes it, and its own write is retained.
        self.assertNotEqual(self.row(foreign_job)["status"], "completed")
        self.assertEqual(self.provider.deletes, 0)
        foreign_writes = set(self.provider.objects) - {(self.key, keeper_version)}
        self.assertEqual(len(foreign_writes), 1)
        queued = queue_jobs("renamed.txt","rename",2,1)
        self.assertEqual(queued["item_count"],1)
        with PostgresConnection(self.db_path) as connection:
            job = connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
        self.run_job(job)
        # Whatever the rename does, another file's write is never deleted.
        self.assertTrue(foreign_writes <= set(self.provider.objects))
        self.assertTrue(foreign_writes.isdisjoint(self.provider.deleted))

    def test_marker_never_bypasses_replacement_consent(self):
        self.provider.markers[self.key,"marker"] = True
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"],"failed")
        self.assertEqual(self.provider.deletes,0)
        self.assertIn((self.key,"marker"),self.provider.markers)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(provenance.describe(connection,vault_id=2,path="report.txt")["reason"],"markers_present")

    def test_stop_protection_and_incompatible_deletion_exclude_resume(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue(); self.run_job(original)
        self.provider.hook = lambda *args, **kwargs: None
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=2")
        with PostgresConnection(self.db_path) as connection:
            with self.assertRaises(provenance.UploadBlocked):
                provenance.schedule_resume(connection,vault=vault,path="report.txt",user_id=1,timestamp=storage.now_iso())
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE vaults SET lifecycle='active' WHERE id=2")
            group = connection.execute("INSERT INTO job_groups(vault_id) VALUES (2) RETURNING id").fetchone()["id"]
            connection.execute("INSERT INTO jobs(vault_id,file_id,group_id,action,status,run_after) VALUES (2,%s,%s,'cloud-purge','pending_delay',now() + interval '1 day')", (self.file_id, group))
        with PostgresConnection(self.db_path) as connection:
            self.assertFalse(provenance.describe(connection,vault_id=2,path="report.txt")["can_resume"])
            with self.assertRaises(provenance.UploadBlocked):
                provenance.schedule_resume(connection,vault=vault,path="report.txt",user_id=1,timestamp=storage.now_iso())
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))

    def test_cloud_only_conflict_after_ack_cannot_publish_or_cleanup(self):
        keeper, keeper_version = self.seed_keeper()
        self.observe_content(B)
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        job = self.queue(); self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE files SET held_version_id=%s WHERE id=%s", (keeper,self.file_id))
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()
            with self.assertRaises(provenance.UploadBlocked):
                provenance.schedule_resume(connection,vault=vault,path="report.txt",user_id=1,timestamp=storage.now_iso())
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))
        self.assertIn((self.key,keeper_version),self.provider.objects)
        self.assertIsNone(self.row(job)["archive_version_id"])

    def test_cancel_during_historical_get_preserves_all_copies(self):
        # The cleanup's listing is where a historical GET used to be: a cancel
        # there stops the cleanup before any DELETE and keeps every copy.
        keeper, keeper_version = self.seed_keeper()
        self.observe_content(B)
        job = self.queue()
        listings = []
        def cancel(stage, **kwargs):
            if stage == "list" and self.provider.puts and not listings:
                listings.append(1)
                storage.cancel_jobs([job])
        self.provider.hook = cancel
        self.run_job(job)
        self.assertEqual(self.row(job)["status"], "cancelled")
        self.assertEqual(self.provider.deletes, 0)
        self.assertEqual(len(self.provider.objects), 2)

    def test_proof_metadata_integrity_and_local_CAS_retry_as_one_DB_unit(self):
        # Verification evidence, integrity, Current and the Local Copy CAS
        # commit in one fenced unit: a serialization abort replays only that
        # unit, never the filesystem observation or the provider.
        publish = ArchiveCatalog.promote_replacement
        transactions = []
        def promote(catalog, file_id, **kwargs):
            publish(catalog, file_id, **kwargs)
            transactions.append(catalog.connection.execute("SELECT txid_current() AS id").fetchone()["id"])
            row = catalog.connection.execute(
                """SELECT av.verification_method, av.integrity, f.matched_version_id=av.id AS matched
                   FROM archive_versions av JOIN files f ON f.current_version_id=av.id WHERE f.id=%s""",
                (file_id,)).fetchone()
            self.assertEqual(row, {"verification_method": "s3_checksum", "integrity": "verified", "matched": True})
            if len(transactions) == 1:
                catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'proof commit abort' USING ERRCODE='40001'; END $$")
        job = self.queue()
        with patch.object(ArchiveCatalog, "promote_replacement", promote), \
                patch.object(storage, "prepare_upload_snapshot", wraps=storage.prepare_upload_snapshot) as hashed:
            self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(len(transactions), 2)
        self.assertEqual(hashed.call_count, 1, "A DB retry cannot replay filesystem observation")
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 0))

    def test_unavailable_source_volume_suspends_already_written_resume(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue(); self.run_job(original)
        with patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=False,volume_health="missing")):
            resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "retrying")
        self.assertIsNone(self.row(resumed)["archive_version_id"])
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))

    def test_db_transaction_and_commit_retry_never_replays_provider(self):
        for code in ("40P01", "40001"):
            with self.subTest(sqlstate=code):
                # Nontransactional sequence survives the failed COMMIT, unlike a
                # row-based counter which would fail forever after rollback.
                with PostgresConnection(self.db_path) as connection:
                    connection.execute(f"CREATE SEQUENCE fail_{code}")
                    connection.execute(f"""CREATE FUNCTION fail_receipt_{code}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('fail_{code}')=1 THEN RAISE EXCEPTION 'commit fault' USING ERRCODE='{code}'; END IF; RETURN NEW; END $$""")
                    connection.execute(f"CREATE CONSTRAINT TRIGGER receipt_commit_fault AFTER INSERT ON upload_receipts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fail_receipt_{code}()")
                job = self.queue()
                puts_before = self.provider.puts
                self.run_job(job)
                self.assert_keeper(job)
                self.assertEqual(self.provider.puts - puts_before, 1)
                with PostgresConnection(self.db_path) as connection:
                    connection.execute("DROP TRIGGER receipt_commit_fault ON upload_receipts")
                    # A fresh local generation earns ordinary upload admission.
                    self.file.write_bytes(B if code == "40P01" else A)
                    ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="report.txt", file_type="regular", size=self.file.stat().st_size, mtime_ns=self.file.stat().st_mtime_ns, observed_at=storage.now_iso())

    def queue_path(self, path):
        queued = queue_jobs(path, "upload", 2, 1)
        self.assertEqual(queued["item_count"], 1)
        with PostgresConnection(self.db_path) as connection:
            return connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
