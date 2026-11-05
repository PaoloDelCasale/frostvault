"""PostgreSQL CI: admission, exact replacement, restart and application fencing."""
from contextlib import contextmanager
import base64
import hashlib
import io
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import vault_moves as moves, protection_stop
from app.services.lifecycle_profiles import guided_profile, profile_to_json
from tests.postgres import PostgresConnection, database_dsn
from tests import test_storage_class_change_worker as fixture


class Provider:
    """Strict versioned provider: copies survive simulated process termination."""
    def __init__(self):
        self.data = b"hello world!"
        self.checksum = base64.b64encode(hashlib.sha256(self.data).digest()).decode()
        self.versions = {"s3-v1":dict(VersionId="s3-v1",ContentLength=12,StorageClass="STANDARD",
                                     ChecksumSHA256=self.checksum,Metadata={"original":"kept"})}
        self.current = "s3-v1"
        self.copies = 0
        self.deleted = []
        self.after_copy = None
        self.after_delete = None
        self.corrupt = False

    def head_object(self, **kw):
        return dict(self.versions[kw.get("VersionId") or self.current])

    def get_object(self, **kw):
        assert kw.get("VersionId")
        return {"Body":io.BytesIO(self.data)}

    def copy_object(self, **kw):
        assert kw["CopySource"]["VersionId"] == "s3-v1"
        self.copies += 1
        self.current = f"copy-{self.copies}"
        self.versions[self.current] = dict(VersionId=self.current,ContentLength=12,
            StorageClass=kw["StorageClass"],ChecksumSHA256="corrupt" if self.corrupt else self.checksum,
            Metadata=kw["Metadata"])
        if self.after_copy:
            self.after_copy()
        return {"VersionId":self.current}

    def list_object_versions(self, **kw):
        return {"Versions":[dict(Key=kw["Prefix"],VersionId=v) for v in self.versions]}

    def delete_object(self, **kw):
        assert kw["VersionId"] != self.current
        self.deleted.append(kw["VersionId"])
        self.versions.pop(kw["VersionId"],None)
        if self.after_delete:
            self.after_delete()
        return {}


class VaultMoveTests(unittest.TestCase):
    _job_row = fixture.StorageClassChangeWorkerTests._job_row

    def setUp(self):
        fixture.StorageClassChangeWorkerTests.setUp(self)
        self.provider = Provider()
        with PostgresConnection(str(self.path)) as c:
            c.execute("DELETE FROM jobs")
            c.execute("UPDATE vaults SET storage_plan_json=%s WHERE id=1",
                      (profile_to_json(guided_profile("archive_tiered")),))
            c.execute("UPDATE archive_versions SET plaintext_sha256=%s WHERE id=%s",
                      (hashlib.sha256(self.provider.data).hexdigest(),self.version_id))
        self.settings = SimpleNamespace(database_dsn=database_dsn(str(self.path)))

    @contextmanager
    def worker(self):
        with patch("app.database.settings",self.settings), patch("app.storage.settings",self.settings), \
             patch("app.storage.validate_cloud_vault"), patch("app.storage.s3_client",return_value=self.provider):
            yield

    def schedule(self, target="DEEP_ARCHIVE"):
        with PostgresConnection(str(self.path)) as c:
            result=moves.schedule(c,vault_id=1,target=target,user_id=1,acknowledged=True,timestamp=storage.now_iso())
        self.job_id=result["job_ids"][0]
        return result

    def state(self):
        with PostgresConnection(str(self.path)) as c:
            return c.execute("SELECT * FROM jobs WHERE id=%s",(self.job_id,)).fetchone()

    def run_job(self):
        with self.worker():
            return storage.process_job(dict(self._job_row()))

    def expire(self):
        with PostgresConnection(str(self.path)) as c:
            c.execute("UPDATE jobs SET status='uploading',claim_token='dead',claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id=%s",(self.job_id,))
        with self.worker():
            summary=storage.reconcile_interrupted_jobs()
        self.assertEqual(summary,{"completed":0,"requeued":1,"failed":0})

    def test_move_creates_immutable_verified_snapshot_before_exact_source_removal(self):
        self.schedule()
        self.run_job()
        job=self.state()
        self.assertEqual(job["status"],"completed",job["message"])
        self.assertNotEqual(job["archive_version_id"],self.version_id)
        self.assertEqual(self.provider.deleted,["s3-v1"])
        with PostgresConnection(str(self.path)) as c:
            old=c.execute("SELECT * FROM archive_versions WHERE id=%s",(self.version_id,)).fetchone()
            new=c.execute("SELECT * FROM archive_versions WHERE id=%s",(job["archive_version_id"],)).fetchone()
            self.assertEqual(old["provider_version_id"],"s3-v1")
            self.assertEqual(old["availability"],"purged")
            self.assertEqual(new["integrity"],"verified")
            self.assertEqual(new["storage_class"],"DEEP_ARCHIVE")
            self.assertEqual(new["verified_at"],old["verified_at"])

    def test_corrupt_new_version_never_removes_only_verified_copy(self):
        self.schedule()
        self.provider.corrupt=True
        self.run_job()
        self.assertEqual(self.state()["status"],"failed")
        self.assertEqual(self.provider.deleted,[])
        self.assertIn("s3-v1",self.provider.versions)

    def test_restart_after_copy_before_response_adopts_token_and_does_not_copy_twice(self):
        self.schedule()
        self.provider.after_copy=lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt): self.run_job()
        self.assertEqual(self.provider.deleted,[])
        self.provider.after_copy=None
        self.expire()
        self.run_job()
        self.assertEqual(self.state()["status"],"completed",self.state()["message"])
        self.assertEqual(self.provider.copies,1)
        self.assertEqual(self.provider.deleted,["s3-v1"])

    def test_restart_after_verified_publication_and_delete_before_db_commit(self):
        self.schedule()
        self.provider.after_delete=lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt): self.run_job()
        self.assertNotEqual(self.state()["archive_version_id"],self.version_id)
        self.provider.after_delete=None
        self.expire()
        self.run_job()
        self.assertEqual(self.state()["status"],"completed",self.state()["message"])
        self.assertEqual(self.provider.copies,1)
        self.assertEqual(self.provider.deleted,["s3-v1"])

    def test_cancel_during_copy_retains_old_and_new_versions_without_publication(self):
        self.schedule()
        self.provider.after_copy=lambda: storage.cancel_jobs([self.job_id])
        self.run_job()
        self.assertEqual(self.state()["status"],"cancelled")
        self.assertEqual(self.provider.deleted,[])
        self.assertEqual(len(self.provider.versions),2)
        self.assertEqual(self.state()["archive_version_id"],self.version_id)

    def test_cloud_race_preserves_source_and_external_write(self):
        self.schedule()
        def external_write():
            self.provider.versions["external"]={**self.provider.versions["s3-v1"],"VersionId":"external"}
            self.provider.current="external"
        self.provider.after_copy=external_write
        self.run_job()
        self.assertEqual(self.state()["status"],"failed")
        self.assertEqual(self.provider.deleted,[])
        self.assertEqual(len(self.provider.versions),3)

    def test_worker_rechecks_lifecycle_class_and_never_automatically_restores(self):
        self.schedule()
        self.provider.versions["s3-v1"]["StorageClass"]="GLACIER"
        self.run_job()
        self.assertEqual(self.state()["status"],"failed")
        self.assertIn("Explicit restore",self.state()["message"])
        self.assertEqual(self.provider.copies,0)
        self.assertEqual(self.provider.deleted,[])

    def test_lifecycle_drift_to_equal_or_colder_never_warms(self):
        self.schedule("GLACIER_IR")
        self.provider.versions["s3-v1"]["StorageClass"]="DEEP_ARCHIVE"
        self.run_job()
        self.assertEqual(self.state()["status"],"failed")
        self.assertEqual(self.provider.copies,0)

    def test_standard_plan_and_target_outside_plan_and_missing_ack_rejected(self):
        for kwargs in (dict(target="STANDARD",acknowledged=True),dict(target="DEEP_ARCHIVE",acknowledged=False)):
            with PostgresConnection(str(self.path)) as c,self.assertRaises(moves.MoveBlocked):
                moves.schedule(c,vault_id=1,user_id=1,timestamp=storage.now_iso(),**kwargs)
        with PostgresConnection(str(self.path)) as c:
            c.execute("UPDATE vaults SET storage_plan_json='{\"transitions\":[]}' WHERE id=1")
            self.assertEqual(moves.preview(c,1)["targets"],[])
            with self.assertRaises(moves.MoveBlocked):
                moves.schedule(c,vault_id=1,target="DEEP_ARCHIVE",acknowledged=True,user_id=1,timestamp=storage.now_iso())

    def test_pending_deletion_is_not_selected_and_stop_protection_rejects_move(self):
        result=self.schedule()
        with PostgresConnection(str(self.path)) as c:
            c.execute("UPDATE jobs SET action='cloud-purge',status='pending_delay' WHERE id=%s",(self.job_id,))
            preview=moves.preview(c,1)
            self.assertEqual(preview["targets"][-1]["skipped"],{"pending_work":1})
            with self.assertRaises(moves.MoveBlocked):
                moves.schedule(c,vault_id=1,target="DEEP_ARCHIVE",acknowledged=True,user_id=1,timestamp=storage.now_iso())
            with self.assertRaisesRegex(ValueError,"active Jobs"):
                protection_stop.stop_protection(c,vault_id=1,actor_user_id=1,cloud_disposition="keep",confirmation="Docs",reason="stop")
        self.assertEqual(len(result["job_ids"]),1)

    def test_concurrent_admissions_do_not_duplicate_file_job(self):
        barrier=threading.Barrier(2)
        results=[]
        def admit():
            barrier.wait(timeout=10)
            try:
                with PostgresConnection(str(self.path)) as c:
                    results.append(moves.schedule(c,vault_id=1,target="DEEP_ARCHIVE",acknowledged=True,user_id=1,timestamp=storage.now_iso()))
            except moves.MoveBlocked:
                results.append(None)
        threads=[threading.Thread(target=admit) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_worker_cannot_publish_after_claim_takeover(self):
        self.schedule()
        # Bind a real durable lease; cancellation/takeover wins while CopyObject runs.
        with PostgresConnection(str(self.path)) as c:
            c.execute("UPDATE jobs SET claim_token='original',claim_expires_at='2099-01-01T00:00:00+00:00' WHERE id=%s",(self.job_id,))
        def takeover():
            with PostgresConnection(str(self.path)) as c:
                c.execute("UPDATE jobs SET claim_token='replacement' WHERE id=%s",(self.job_id,))
        self.provider.after_copy=takeover
        self.run_job()
        self.assertEqual(self.state()["claim_token"],"replacement")
        self.assertEqual(self.state()["archive_version_id"],self.version_id)
        self.assertEqual(self.provider.deleted,[])
