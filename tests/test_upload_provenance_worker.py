"""#464 core acceptance through real admission/claim/process_job on disposable PG16.

TEST_POSTGRES_URL is mandatory; never skipped locally as database evidence.
Provider persists actual version bytes/Metadata across worker invocations.
"""
import base64
import hashlib
import json
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
from app.services.upload_snapshot import UploadSnapshot
from tests.postgres import PostgresConnection, database_dsn, run_alembic
from tests.upload_provenance_provider import VersionedProvider

A = b"immutable content A"
B = b"immutable content B"


def prepare_unsent(case, job_id):
    """Legacy/crash fixture: an already committed prepared-but-unsent intent.

    Fresh UploadExecution commits prepare+send-started atomically, so injecting
    a failure in mark_send_started no longer manufactures this historical state.
    Keep exercising its immutability and closure with real persisted facts.
    """
    snapshot, _ = storage.prepare_upload_snapshot(case.file, lambda: None)
    with PostgresConnection(case.db_path) as connection:
        job = connection.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job_id,)).fetchone()
        pinned = provenance.target(connection, job,
            key=storage.expected_cloud_key(job["path"], job["s3_prefix"]),
            provider=provenance.provider_identity(case.provider))
        ArchiveCatalog(connection).set_upload_snapshot(job_id, snapshot_json=snapshot.as_json())
        return provenance.prepare(connection, job, pinned=pinned, snapshot=snapshot,
            requested=job["upload_verification_requested"],
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
            connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
            connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'docs','Docs',%s,'bucket','docs')", (str(self.source),))
            enroll_vault_root_identity(connection, 2, str(self.source))
            ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="report.txt", file_type="regular", size=len(A), mtime_ns=self.file.stat().st_mtime_ns, observed_at=storage.now_iso())
            self.file_id = connection.execute("SELECT vault_file_id FROM file_paths WHERE vault_id=2 AND path='report.txt' AND valid_to IS NULL").fetchone()["vault_file_id"]
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
            claimed = ArchiveCatalog(connection).claim_job(job_id=job_id, claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(), claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(), restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed)
            job = connection.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job_id,)).fetchone()
        return storage.process_job(job)

    def row(self, job_id):
        with PostgresConnection(self.db_path) as connection:
            return connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def resume(self):
        self.provider.hook = lambda *args, **kwargs: None
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            result = provenance.schedule_resume(connection, vault=vault, path="report.txt", user_id=1, timestamp=storage.now_iso())
        self.run_job(result["job_id"])
        return result["job_id"]

    def http_client(self):
        from fastapi.testclient import TestClient
        from app.main import app, current_user, current_vault
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
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

    def test_module_run_accepts_a_claimed_job_and_returns_its_durable_outcome(self):
        from app.services.upload_execution import UploadExecution
        job_id = self.queue()
        with PostgresConnection(self.db_path) as connection:
            claimed = ArchiveCatalog(connection).claim_job(job_id=job_id,
                claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed)
            job = connection.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job_id,)).fetchone()
        self.assertIsNone(storage._claim_token_for(job_id))
        outcome = UploadExecution(storage).run(job)
        version = self.assert_keeper(job_id)
        self.assertEqual((outcome.job_id, outcome.status, outcome.archive_version_id),
                         (job_id, "completed", version["id"]))
        self.assertIsNone(storage._claim_token_for(job_id), "The caller's context is restored")
        self.assertEqual(self.provider.puts, 1)

    def test_committed_intent_send_snapshot_and_token_precede_provider_mutation(self):
        def inspect(stage, **kwargs):
            if stage == "before_put":
                with PostgresConnection(self.db_path) as connection:
                    intent = connection.execute("SELECT * FROM upload_intents").fetchone()
                    self.assertTrue(intent["send_started"])
                    self.assertEqual(intent["snapshot_json"], kwargs["snapshot"].as_json())
                    self.assertEqual(kwargs["metadata"][provenance.TOKEN_KEY], intent["token"])
                    self.assertIsNotNone(connection.execute("SELECT job_id FROM snapshot_cleanup_inventories").fetchone())
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
        def aborted_publication(connection,*args):
            record(connection,*args)
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
            intent = provenance.load_for_job(connection,job)
            self.assertFalse(intent['send_started'])
            self.assertIsNone(provenance.receipt(connection,intent['id']))
            no_write = connection.execute('SELECT response_json FROM upload_receipts WHERE intent_id=%s',(intent['id'],)).fetchone()
            self.assertEqual(json.loads(no_write['response_json']),{'NoWriteOperation':'PutObject'})
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.provider.puts,1,'One successful object, no blind replay of ambiguous PUT')
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(provenance.load_for_job(connection,job)['id'],intent['id'])
            self.assertEqual(connection.execute('SELECT count(*) n FROM upload_receipts WHERE intent_id=%s',(intent['id'],)).fetchone()['n'],2)

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
            self.assertTrue(intent['send_started'])
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
            self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_receipts").fetchone()["n"], 1)
        self.assertEqual(self.row(job)["status"], "retrying")
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE jobs SET retry_after=NULL WHERE id=%s", (job,))
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
            receipt = provenance.receipt(connection, connection.execute("SELECT id FROM upload_intents").fetchone()["id"])
            self.assertIn(("docs/report.txt", receipt["VersionId"]), self.provider.objects)
        # Source B and changed requested mode must not redefine A's expected proof.
        self.file.write_bytes(B)
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE vault_operation_policies SET upload_verification='read_back' WHERE vault_id=2")
        resumed = self.resume()
        keeper = self.assert_keeper(resumed)
        self.assertEqual(keeper["plaintext_sha256"], hashlib.sha256(A).hexdigest())
        with PostgresConnection(self.db_path) as connection:
            local = connection.execute("SELECT * FROM local_copies WHERE vault_file_id=%s", (self.file_id,)).fetchone()
        self.assertNotEqual(local["matched_archive_version_id"], keeper["id"])
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
        self.provider.add("docs/report.txt",A,version="historical-duplicate")
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("delete temporarily unavailable")) if stage == "delete" else None
        original = self.queue()
        # This scenario requires a terminal historical failure, not a still-active
        # transient retry. Exhaust the existing budget before the injected fault.
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
        self.assertTrue(self.row(job_id)["upload_reconcile_only"])
        self.run_job(job_id)
        self.assert_keeper(job_id)
        self.assertEqual(self.provider.puts,1)
        self.assertEqual(self.row(original)["status"],"failed","No generic history resolution")

    def legacy_materialized_failure(self, *, keep_token=False):
        from app.services.operation_policies import OperationPolicy, set_policy
        with PostgresConnection(self.db_path) as connection:
            set_policy(connection,2,OperationPolicy(upload_verification="read_back"))
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("get temporarily unavailable")) if stage == "get" else None
        original = self.queue()
        self.run_job(original)
        self.assertIsNotNone(self.row(original)["archive_version_id"])
        with PostgresConnection(self.db_path) as connection:
            # Construct a genuine populated 0063 fixture: linked version + its
            # independent baseline, no invented/backfilled 0064 provenance.
            connection.execute("UPDATE jobs SET status='failed',retry_after=NULL WHERE id=%s",(original,))
            connection.execute("DELETE FROM upload_receipts")
            connection.execute("DELETE FROM upload_intent_jobs")
            connection.execute("DELETE FROM upload_remediation_facts")
            connection.execute("DELETE FROM upload_intents")
            set_policy(connection,2,OperationPolicy(upload_verification="s3_checksum"))
            state = provenance.describe(connection,vault_id=2,path="report.txt")
            self.assertTrue(state["pending"])
            self.assertTrue(state["can_resume"])
            version = connection.execute("SELECT object_key,provider_version_id FROM archive_versions WHERE id=%s", (self.row(original)["archive_version_id"],)).fetchone()
        if not keep_token:
            # Construct the pre-0064 provider fixture too. Clearing only the DB
            # would leave a new-format app token without its required journal.
            payload, metadata = self.provider.objects[version["object_key"], version["provider_version_id"]]
            self.provider.objects[version["object_key"], version["provider_version_id"]] = (payload, {key: value for key, value in metadata.items() if key != provenance.TOKEN_KEY})
        return original

    def test_failed_linked_pre0064_baseline_resumes_without_file_or_new_put(self):
        original = self.legacy_materialized_failure()
        self.file.unlink()
        resumed = self.resume()
        version = self.assert_keeper(resumed)
        self.assertEqual(version["upload_verification_requested"],"read_back")
        self.assertEqual(version["verification_method"],"read_back")
        self.assertEqual((self.provider.puts,self.provider.gets),(1,2))
        self.assertEqual(self.row(original)["status"],"failed")
        with PostgresConnection(self.db_path) as connection:
            self.assertIsNone(connection.execute("SELECT id FROM upload_intents").fetchone(),"Legacy resume is not provenance backfill")

    def test_new_format_token_without_its_journal_is_not_legacy_authority(self):
        original = self.legacy_materialized_failure(keep_token=True)
        before = dict(self.provider.objects)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(self.provider.objects, before)
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 1, 0))
        with PostgresConnection(self.db_path) as connection:
            self.assertNotEqual(connection.execute("SELECT integrity FROM archive_versions WHERE id=%s", (self.row(original)["archive_version_id"],)).fetchone()["integrity"], "verified")

    def observe_content(self, payload):
        self.file.write_bytes(payload)
        with PostgresConnection(self.db_path) as connection:
            ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="report.txt", file_type="regular", size=len(payload), mtime_ns=self.file.stat().st_mtime_ns, observed_at=storage.now_iso())

    def test_linked_resume_cannot_ignore_contradictory_receipt_after_publication_crash(self):
        keeper = self.seed_legacy_keeper()
        self.observe_content(B)
        publish = storage._publish_upload_receipt

        def corrupt_ack(client, **kwargs):
            acknowledged = kwargs["on_acknowledged"]
            def contradictory(response):
                response["ChecksumSHA256"] = base64.b64encode(hashlib.sha256(A).digest()).decode()
                acknowledged(response)
            kwargs["on_acknowledged"] = contradictory
            return self.provider.upload(client, **kwargs)

        def crash_after_publish(*args, **kwargs):
            publish(*args, **kwargs)
            raise RuntimeError("crash after publication before proof evaluation")

        original = self.queue()
        with patch("app.storage.upload_file", side_effect=corrupt_ack), patch("app.storage._publish_upload_receipt", side_effect=crash_after_publish):
            self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        linked = self.row(original)["archive_version_id"]
        self.assertIsNotNone(linked)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, resumed)
            self.assertNotEqual(provenance.receipt(connection, intent["id"])["ChecksumSHA256"], UploadSnapshot.from_json(intent["snapshot_json"]).checksum_sha256)
            self.assertEqual(provenance.describe(connection, vault_id=2, path="report.txt")["reason"], "proof_mismatch")
            versions = {row["id"]: row for row in ArchiveCatalog(connection).list_versions(2, "report.txt")}
        self.assertNotEqual(versions[linked]["integrity"], "verified")
        self.assertEqual((versions[keeper]["integrity"], versions[keeper]["availability"]), ("verified", "available"))
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 0, 0))
        self.assertEqual(len(self.provider.objects), 2)

    def test_scan_before_publication_does_not_demote_the_exact_new_app_write(self):
        keeper = self.seed_legacy_keeper()
        self.observe_content(B)
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("lost response before publication")) if stage == "accepted" else None
        original = self.queue(); self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        self.assertIsNone(self.row(original)["archive_version_id"])
        self.provider.hook = lambda *args, **kwargs: None
        self.scan()
        with PostgresConnection(self.db_path) as connection:
            discovered = connection.execute("SELECT * FROM archive_versions WHERE provider_version_id<>'legacy-keeper'").fetchone()
        self.assertEqual(discovered["origin"], "discovered")
        self.assertLess(discovered["version_number"], 0)
        self.assertEqual(self.provider.deletes, 0)
        resumed = self.resume()
        current = self.assert_keeper(resumed)
        self.assertEqual(current["id"], discovered["id"])
        self.assertEqual(current["origin"], "upload")
        self.assertGreater(current["version_number"], 1)
        self.scan()
        with PostgresConnection(self.db_path) as connection:
            versions = ArchiveCatalog(connection).list_versions(2, "report.txt")
        self.assertEqual(versions[0]["id"], current["id"])
        self.assertEqual(next(row for row in versions if row["id"] == keeper)["availability"], "purged")
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 0, 1))
        self.assertEqual(self.provider.deleted, [("docs/report.txt", "legacy-keeper")])

    def test_provider_namespace_change_retains_all_unpublished_copies(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue(); self.run_job(original)
        before = dict(self.provider.objects)
        self.provider.meta.endpoint_url = "https://another-disposable.invalid"
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(self.provider.objects, before)
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 0, 0))

    def test_completed_inventory_is_not_reused_for_a_later_scan_remediation(self):
        original = self.queue(); self.run_job(original)
        keeper = self.assert_keeper(original)
        with PostgresConnection(self.db_path) as connection:
            old = connection.execute("SELECT * FROM snapshot_cleanup_inventories WHERE job_id=%s", (original,)).fetchone()
        self.assertIsNotNone(old["completed_at"])
        self.provider.add("docs/report.txt", A, version="duplicate-after-completion")
        self.scan()
        self.assertEqual(self.provider.deletes, 0)
        resumed = self.resume()
        self.assertEqual(self.assert_keeper(resumed)["id"], keeper["id"])
        with PostgresConnection(self.db_path) as connection:
            new = connection.execute("SELECT * FROM snapshot_cleanup_inventories WHERE job_id=%s", (resumed,)).fetchone()
            unchanged = connection.execute("SELECT * FROM snapshot_cleanup_inventories WHERE job_id=%s", (original,)).fetchone()
        self.assertEqual(unchanged, old)
        self.assertNotEqual(new["job_id"], old["job_id"])
        self.assertIsNotNone(new["completed_at"])
        self.assertIn("duplicate-after-completion", json.loads(new["inventory_json"])["docs/report.txt"]["versions"])
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (1, 1, 1))

    def test_pending_send_blocks_move_admission_and_a_stale_move_worker(self):
        from app.services import vault_moves
        keeper = self.seed_legacy_keeper()
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
            # Simulate an already-admitted stale Move. The worker must re-fence
            # pending sends independently of the dedicated admission endpoint.
            ids, _, _ = ArchiveCatalog(connection).queue_jobs(vault_id=2, path="report.txt", action="storage-class", archive_version_id=keeper, requested_by=1, requested_at=storage.now_iso(), group_id="stale-move", is_directory=False, target_storage_class="GLACIER_IR")
            self.assertEqual(len(ids), 1)
            connection.execute("""INSERT INTO vault_class_moves(job_id,token,source_archive_id,source_version_id,object_key,size,sha256)
                VALUES (%s,'stale-move-token',%s,'legacy-keeper','docs/report.txt',%s,%s)""", (ids[0], keeper, len(A), hashlib.sha256(A).hexdigest()))
        before = dict(self.provider.objects)
        self.provider.hook = lambda *args, **kwargs: None
        with patch.object(vault_moves, "load_vault_plan", return_value=SimpleNamespace(transitions=[SimpleNamespace(storage_class="GLACIER_IR")])):
            self.run_job(ids[0])
        self.assertEqual(self.row(ids[0])["status"], "failed")
        self.assertIn("Reconcile the pending upload before another cloud mutation", self.row(ids[0])["message"])
        self.assertEqual(self.provider.objects, before)
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
            self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
            self.assertFalse(provenance.describe(connection, vault_id=2, path="report.txt")["can_resume"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertIsNone(self.row(job)["archive_version_id"])

    def test_receipt_append_after_lease_loss_is_fact_only(self):
        job = self.queue()
        def takeover(stage, **kwargs):
            if stage == "accepted":
                with PostgresConnection(self.db_path) as connection:
                    connection.execute("UPDATE jobs SET claim_token='new-owner' WHERE id=%s", (job,))
        self.provider.hook = takeover
        self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            self.assertIsNotNone(provenance.receipt(connection, intent["id"]))
            self.assertIsNone(intent["archive_version_id"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))

    def test_send_started_zero_match_without_absence_proof_remains_reconcile_only(self):
        # #484: only complete S3 reads that exclude an in-flight multipart prove
        # absence. See test_upload_ambiguous_send_recovery for the recovery path.
        self.provider.multiparts.append(("docs/report.txt", "in-flight"))
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("before provider request")) if stage == "before_put" else None
        job = self.queue()
        self.run_job(job)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, resumed)
            self.assertTrue(intent["send_started"])
            self.assertEqual(intent["blocked_reason"], "ambiguous_send")

    def test_multiple_tokens_never_choose_latest_or_delete(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("lost response")) if stage == "accepted" else None
        job = self.queue()
        self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
        self.provider.add("docs/report.txt", A, {provenance.TOKEN_KEY: intent["token"]}, version="second-token")
        before = dict(self.provider.objects)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(self.provider.objects, before)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))

    def test_historical_duplicate_adopted_without_promoting_current_snapshot(self):
        self.provider.add("docs/report.txt", A, version="old-orphan")
        job = self.queue()
        self.run_job(job)
        keeper = self.assert_keeper(job)
        self.assertEqual(self.provider.deleted, [("docs/report.txt", "old-orphan")])
        self.assertEqual(self.provider.gets, 1)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM archive_versions").fetchone()["n"], 1)
            proof = connection.execute("SELECT * FROM snapshot_cleanup_proofs").fetchone()
            self.assertEqual(proof["keeper_id"], keeper["id"])
            self.assertEqual(proof["method"], "exact_get_duplicate")
            self.assertIsNotNone(proof["deleted_at"])

    def test_historical_different_or_unreadable_preserves_keeper_with_actionable_resume(self):
        self.provider.add("docs/report.txt", B, version="different-orphan")
        job = self.queue()
        self.run_job(job)
        self.assertEqual(self.row(job)["status"], "failed")
        with PostgresConnection(self.db_path) as connection:
            keeper = connection.execute("SELECT * FROM archive_versions WHERE id=%s", (self.row(job)["archive_version_id"],)).fetchone()
            state = provenance.describe(connection, vault_id=2, path="report.txt")
        self.assertEqual(keeper["integrity"], "verified")
        self.assertEqual(state["reason"], "orphan_different")
        self.assertTrue(state["can_resume"])
        self.assertEqual(len(self.provider.objects), 2)
        self.assertEqual(self.provider.deletes, 0)
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
            self.assertTrue(intent["send_started"])
            self.assertIsNone(provenance.receipt(connection, intent["id"]))
            connection.execute("UPDATE jobs SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id=%s", (job,))
        storage.reconcile_interrupted_jobs()
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))

    def test_incompatible_receipts_append_never_overwrite_or_publish(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        job = self.queue(); self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            intent = provenance.load_for_job(connection, job)
            original = provenance.receipt(connection, intent["id"])
        provenance.append_receipt(storage.db, intent["id"], original, storage.now_iso())
        provenance.append_receipt(storage.db, intent["id"], {"VersionId":"incompatible"}, storage.now_iso())
        before = dict(self.provider.objects)
        resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(self.provider.objects, before)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM upload_receipts").fetchone()["n"], 2)
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))

    def test_committed_historical_proof_avoids_get_after_delete_failure(self):
        self.provider.add("docs/report.txt", A, version="old-orphan")
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("injected deletion fault")) if stage == "delete" else None
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"], "failed")
        self.assertEqual(self.provider.gets, 1)
        resumed = self.resume()
        self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,1))

    def test_delete_database_gap_reconciles_absence_without_get_put_or_second_delete(self):
        self.provider.add("docs/report.txt", A, version="old-orphan")
        def die(stage, **kwargs):
            if stage == "after_delete":
                raise KeyboardInterrupt("process death after delete")
        self.provider.hook = die
        job = self.queue()
        with self.assertRaises(KeyboardInterrupt): self.run_job(job)
        self.provider.hook = lambda *args, **kwargs: None
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE jobs SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id=%s", (job,))
        storage.reconcile_interrupted_jobs()
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,1))

    def seed_legacy_keeper(self):
        self.provider.add("docs/report.txt", A, version="legacy-keeper")
        with PostgresConnection(self.db_path) as connection:
            catalog = ArchiveCatalog(connection)
            keeper = catalog.record_archive_version(vault_id=2,vault_file_id=self.file_id,path="report.txt",object_key="docs/report.txt",provider_version_id="legacy-keeper",size=len(A),storage_class="STANDARD",etag="not-proof",uploaded_at="2026-11-01",observed_at="2026-11-01",scan_id="seed",origin="upload")
            digest = hashlib.sha256(A).hexdigest()
            catalog.mark_version_verified(keeper,plaintext_sha256=digest,verified_at="2026-11-01")
            catalog.set_local_fingerprint(vault_id=2,path="report.txt",plaintext_sha256=digest,matched_archive_version_id=keeper)
            connection.execute("INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,requested_at,updated_at,upload_plaintext_sha256) VALUES (2,%s,%s,'report.txt','upload','completed','2026-11-01','2026-11-01',%s)", (self.file_id,keeper,digest))
        return keeper

    def scan(self):
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
        storage.scan_cloud(vault, storage.now_iso())

    def test_scan_catalog_membership_never_launders_different_content_for_upload(self):
        keeper = self.seed_legacy_keeper()
        self.provider.add("docs/report.txt", B, version="different-discovered")
        self.scan(); self.scan()
        self.assertEqual(self.provider.deletes, 0)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(ArchiveCatalog(connection).list_versions(2,"report.txt")[0]["id"], keeper)
            self.assertEqual(connection.execute("SELECT integrity FROM archive_versions WHERE provider_version_id='different-discovered'").fetchone()["integrity"], "unverified")
            self.file.write_bytes(b"immutable content C")
            ArchiveCatalog(connection).observe_local_copy(vault_id=2,path="report.txt",file_type="regular",size=self.file.stat().st_size,mtime_ns=self.file.stat().st_mtime_ns,observed_at=storage.now_iso())
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"], "failed")
        self.assertIn(("docs/report.txt","legacy-keeper"), self.provider.objects)
        self.assertIn(("docs/report.txt","different-discovered"), self.provider.objects)
        self.assertEqual(self.provider.deletes, 0)

    def test_scan_catalog_membership_never_launders_different_content_for_rename(self):
        self.seed_legacy_keeper()
        self.provider.add("docs/report.txt", B, version="different-discovered")
        self.scan()
        self.file.rename(self.source / "renamed.txt")
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE file_paths SET valid_to=%s WHERE vault_file_id=%s AND valid_to IS NULL", (storage.now_iso(),self.file_id))
            connection.execute("INSERT INTO file_paths(vault_file_id,vault_id,path,valid_from) VALUES (%s,2,'renamed.txt',%s)", (self.file_id,storage.now_iso()))
        queued = queue_jobs("renamed.txt","rename",2,1)
        self.assertEqual(queued["item_count"],1)
        with PostgresConnection(self.db_path) as connection:
            job = connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
        self.run_job(job)
        self.assertEqual(self.row(job)["status"],"failed")
        self.assertIn(("docs/report.txt","legacy-keeper"),self.provider.objects)
        self.assertIn(("docs/report.txt","different-discovered"),self.provider.objects)
        self.assertEqual(self.provider.deletes,0)

    def test_source_root_takeover_during_duplicate_get_blocks_all_cleanup(self):
        self.provider.add("docs/report.txt", A, version="old-orphan")
        def replace_root(stage, **kwargs):
            if stage == "get":
                self.source.rename(self.root / "original-source")
                self.source.mkdir()
        self.provider.hook = replace_root
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"], "failed")
        self.assertEqual(self.provider.deletes, 0)
        self.assertIn(("docs/report.txt","old-orphan"), self.provider.objects)
        self.assertEqual(len(self.provider.objects), 2)

    def test_observable_external_writer_after_get_never_becomes_a_delete_target(self):
        self.provider.add("docs/report.txt", A, version="old-orphan")
        def external(stage, **kwargs):
            if stage == "get": self.provider.add("docs/report.txt", A, version="new-external")
        self.provider.hook = external
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"],"failed")
        self.assertEqual(self.provider.deletes,0)
        self.assertIn(("docs/report.txt","old-orphan"),self.provider.objects)
        self.assertIn(("docs/report.txt","new-external"),self.provider.objects)

    def test_reused_old_path_never_deletes_another_files_sole_keeper_even_if_identical(self):
        self.seed_legacy_keeper()
        self.file.rename(self.source / "renamed.txt")
        self.file.write_bytes(A)
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE file_paths SET valid_to=%s WHERE vault_file_id=%s AND valid_to IS NULL", (storage.now_iso(),self.file_id))
            connection.execute("INSERT INTO file_paths(vault_file_id,vault_id,path,valid_from) VALUES (%s,2,'renamed.txt',%s)", (self.file_id,storage.now_iso()))
            catalog = ArchiveCatalog(connection)
            foreign = catalog.observe_local_copy(vault_id=2,path="report.txt",file_type="regular",size=len(A),mtime_ns=self.file.stat().st_mtime_ns,observed_at=storage.now_iso())
            av = catalog.record_archive_version(vault_id=2,vault_file_id=foreign,path="report.txt",object_key="docs/report.txt",provider_version_id="another-files-keeper",size=len(A),storage_class="STANDARD",etag="same",uploaded_at=storage.now_iso(),observed_at=storage.now_iso(),scan_id="foreign",origin="upload")
            catalog.mark_version_verified(av,plaintext_sha256=hashlib.sha256(A).hexdigest(),verified_at=storage.now_iso())
        self.provider.add("docs/report.txt",A,version="another-files-keeper")
        queued = queue_jobs("renamed.txt","rename",2,1)
        self.assertEqual(queued["item_count"],1)
        with PostgresConnection(self.db_path) as connection:
            job = connection.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
        before = dict(self.provider.objects)
        self.run_job(job)
        self.assertEqual(self.row(job)["status"],"failed")
        self.assertEqual(self.provider.objects,before)
        self.assertEqual((self.provider.puts,self.provider.deletes), (0,0))

    def test_marker_never_bypasses_replacement_consent(self):
        self.provider.markers["docs/report.txt","marker"] = True
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"],"failed")
        self.assertEqual(self.provider.deletes,0)
        self.assertIn(("docs/report.txt","marker"),self.provider.markers)
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(provenance.describe(connection,vault_id=2,path="report.txt")["reason"],"markers_present")

    def test_unavailable_source_volume_suspends_already_written_resume(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue(); self.run_job(original)
        with patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=False,volume_health="missing")):
            resumed = self.resume()
        self.assertEqual(self.row(resumed)["status"], "retrying")
        self.assertIsNone(self.row(resumed)["archive_version_id"])
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))

    def test_stop_protection_and_incompatible_deletion_exclude_resume(self):
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        original = self.queue(); self.run_job(original)
        self.provider.hook = lambda *args, **kwargs: None
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            connection.execute("UPDATE vaults SET decommission_state='decommissioning' WHERE id=2")
        with PostgresConnection(self.db_path) as connection:
            with self.assertRaises(provenance.UploadBlocked):
                provenance.schedule_resume(connection,vault=vault,path="report.txt",user_id=1,timestamp=storage.now_iso())
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE vaults SET decommission_state='active' WHERE id=2")
            connection.execute("INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at) VALUES (2,%s,'report.txt','cloud-purge','pending_delay',%s,%s)", (self.file_id,storage.now_iso(),storage.now_iso()))
        with PostgresConnection(self.db_path) as connection:
            self.assertFalse(provenance.describe(connection,vault_id=2,path="report.txt")["can_resume"])
            with self.assertRaises(provenance.UploadBlocked):
                provenance.schedule_resume(connection,vault=vault,path="report.txt",user_id=1,timestamp=storage.now_iso())
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))

    def test_cloud_only_conflict_after_ack_cannot_publish_or_cleanup(self):
        keeper = self.seed_legacy_keeper()
        self.file.write_bytes(B)
        with PostgresConnection(self.db_path) as connection:
            ArchiveCatalog(connection).observe_local_copy(vault_id=2,path="report.txt",file_type="regular",size=len(B),mtime_ns=self.file.stat().st_mtime_ns,observed_at=storage.now_iso())
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("after ack")) if stage == "acknowledged" else None
        job = self.queue(); self.run_job(job)
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE local_copies SET cloud_only_version_id=%s WHERE vault_file_id=%s", (keeper,self.file_id))
        with PostgresConnection(self.db_path) as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            with self.assertRaises(provenance.UploadBlocked):
                provenance.schedule_resume(connection,vault=vault,path="report.txt",user_id=1,timestamp=storage.now_iso())
        self.assertEqual((self.provider.puts,self.provider.deletes), (1,0))
        self.assertIn(("docs/report.txt","legacy-keeper"),self.provider.objects)
        self.assertIsNone(self.row(job)["archive_version_id"])

    def test_cold_unreadable_historical_content_retains_both_with_safe_remedy(self):
        from botocore.exceptions import ClientError
        self.provider.add("docs/report.txt",A,version="cold-orphan")
        def unreadable(stage, **kwargs):
            if stage == "get": raise ClientError({"Error":{"Code":"InvalidObjectState"}},"GetObject")
        self.provider.hook = unreadable
        job = self.queue(); self.run_job(job)
        self.assertEqual(self.row(job)["status"],"failed")
        self.assertEqual(len(self.provider.objects),2)
        self.assertEqual(self.provider.deletes,0)
        with PostgresConnection(self.db_path) as connection:
            state = provenance.describe(connection,vault_id=2,path="report.txt")
        self.assertEqual(state["reason"],"proof_unavailable")
        self.assertIn("explicit restore",state["remedy"])

    def test_cancel_during_historical_get_preserves_all_copies(self):
        self.provider.add("docs/report.txt", A, version="old-orphan")
        job = self.queue()
        self.provider.hook = lambda stage, **kwargs: storage.cancel_jobs([job]) if stage == "get" else None
        self.run_job(job)
        self.assertEqual(self.row(job)["status"], "cancelled")
        self.assertEqual(self.provider.deletes, 0)
        self.assertEqual(len(self.provider.objects), 2)

    def test_proof_metadata_integrity_and_local_CAS_retry_as_one_DB_unit(self):
        method = ArchiveCatalog.set_version_verification_method
        verified = ArchiveCatalog.mark_version_verified
        method_transactions, verification_transactions = [], []
        def save_method(catalog, version_id, **kwargs):
            method(catalog, version_id, **kwargs)
            method_transactions.append(catalog.connection.execute("SELECT txid_current() AS id").fetchone()["id"])
        def save_verified(catalog, version_id, **kwargs):
            verified(catalog, version_id, **kwargs)
            verification_transactions.append(catalog.connection.execute("SELECT txid_current() AS id").fetchone()["id"])
            row = catalog.connection.execute("SELECT verification_method,integrity FROM archive_versions WHERE id=%s", (version_id,)).fetchone()
            self.assertEqual(row, {"verification_method": "s3_checksum", "integrity": "verified"})
            if len(verification_transactions) == 1:
                catalog.connection.execute("DO $$ BEGIN RAISE EXCEPTION 'proof commit abort' USING ERRCODE='40001'; END $$")
        inspector = storage._inspect_local_copy_for_upload_snapshot
        job = self.queue()
        with patch.object(ArchiveCatalog, "set_version_verification_method", save_method), \
                patch.object(ArchiveCatalog, "mark_version_verified", save_verified), \
                patch.object(storage, "_inspect_local_copy_for_upload_snapshot", wraps=inspector) as inspect:
            self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(method_transactions, verification_transactions)
        self.assertEqual(len(verification_transactions), 2)
        self.assertEqual(inspect.call_count, 1, "A DB retry cannot replay filesystem observation")
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 0))

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
