"""Real PostgreSQL 16 guarded-delete contracts; TEST_POSTGRES_URL is mandatory.

No app startup, Docker, provider, filesystem content or production DB access.
"""
import base64
import hashlib
import json
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
import uuid

from app.services import operational_retention as retention
from app.services import upload_provenance as provenance
from app.services.snapshot_cleanup import _has_lineage
from app.services.upload_snapshot import DEFAULT_PART_SIZE, UploadSnapshot
from tests.postgres import PostgresConnection, run_alembic
from tests.test_operational_retention import NOW

OLD = (NOW - timedelta(days=120)).isoformat()
CUTOFF = (NOW - timedelta(days=30)).isoformat()
RECENT = (NOW - timedelta(days=2)).isoformat()


class OperationalRetentionPostgresTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "retention"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.file_id = str(uuid.uuid4())
        with self.db() as c:
            self.assertTrue(c.execute("SHOW server_version_num").fetchone()["server_version_num"].startswith("16"))
            c.execute("INSERT INTO owner(id,display_name,password_hash) VALUES (1,'Owner','hash')")
            self.vault_id = c.execute("""INSERT INTO vaults(slug,name,source_root,s3_bucket,s3_prefix)
                VALUES ('retention','Retention','/not-accessed','bucket','docs/') RETURNING id""").fetchone()["id"]
            c.execute("INSERT INTO vault_files(id,vault_id,status,created_at) VALUES (%s,%s,'active',%s)", (self.file_id, self.vault_id, OLD))

    def db(self):
        return PostgresConnection(self.path)

    def tick(self, **kwargs):
        return retention.maintain_operational_history(self.db, now=NOW, **kwargs)

    def job(self, *, status="cancelled", snapshot=None, digest=None, reconcile=False, version=None, stamp=OLD):
        with self.db() as c:
            return c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                requested_at,updated_at,upload_snapshot_json,upload_plaintext_sha256,upload_reconcile_only,archive_version_id)
                VALUES (%s,%s,'a.txt','upload',%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (self.vault_id, self.file_id, status, stamp, stamp, snapshot, digest, reconcile, version)).fetchone()["id"]

    def notification(self, *, read=OLD, resolved=None, problem=None, created=OLD, job=None):
        with self.db() as c:
            return c.execute("""INSERT INTO notifications(vault_id,job_id,event,title,created_at,
                read_at,problem_status,resolved_at) VALUES (%s,%s,'test','Test',%s,%s,%s,%s) RETURNING id""",
                (self.vault_id, job, created, read, problem, resolved)).fetchone()["id"]

    def ids(self, table):
        # Fixed test-owned identifiers, never runtime schema discovery.
        from psycopg import sql
        with self.db() as c:
            return {r["id"] for r in c.execute(sql.SQL("SELECT id FROM {}").format(sql.Identifier(table))).fetchall()}

    def intent(self, job, *, settled=False, started=True, version=None, snapshot="{}"):
        identity = str(uuid.uuid4())
        with self.db() as c:
            c.execute("""INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,
                target_json,snapshot_json,requested,inventory_json,send_started,settled,archive_version_id,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'s3_checksum',%s,%s,%s,%s,%s)""",
                (identity,str(uuid.uuid4()),self.vault_id,self.file_id,job,
                 json.dumps({"bucket":"bucket","provider":"provider","key":"docs/a.txt"}),
                 snapshot,'{"versions":[],"markers":[]}',started,settled,version,OLD))
            c.execute("INSERT INTO upload_intent_jobs(job_id,intent_id) VALUES (%s,%s)", (job,identity))
        return identity

    def test_notification_age_is_latest_final_state_with_strict_boundary(self):
        eligible = {self.notification(), self.notification(problem="resolved", resolved=OLD)}
        kept = {
            self.notification(read=None),
            self.notification(problem="open"),
            self.notification(problem="resolved", resolved=None),
            self.notification(problem="resolved", resolved=RECENT),
            self.notification(read=RECENT),
            self.notification(read=CUTOFF),
            self.notification(problem="resolved", resolved=CUTOFF),
            self.notification(created=RECENT),
            # Same instant as the cutoff with a different textual offset.
            self.notification(read="2026-10-13T14:00:00+02:00"),
        }
        result = self.tick()
        self.assertEqual(result["notifications"]["deleted"], len(eligible))
        self.assertEqual(self.ids("notifications"), kept)

    def test_delivery_pruning_is_explicit_bounded_and_never_drops_pending(self):
        closed = self.notification()
        pending = self.notification(problem="resolved", resolved=OLD)
        with self.db() as c:
            for i in range(3):
                c.execute("""INSERT INTO notification_deliveries(notification_id,channel,status,updated_at)
                    VALUES (%s,'email','delivered',%s)""", (closed,OLD))
            c.execute("""INSERT INTO notification_deliveries(notification_id,channel,status,updated_at)
                VALUES (%s,'email','pending',%s)""", (pending,OLD))
        first = self.tick(batch_size=1)
        self.assertEqual(first["deliveries"]["deleted"], 1)
        self.assertIn(closed,self.ids("notifications"), "No unbounded FK cascade of delivery rows")
        for _ in range(5):
            self.tick(batch_size=1)
        self.assertEqual(self.ids("notifications"), {pending})

    def test_locked_notification_cannot_lose_delivery_during_reopen(self):
        notification = self.notification(problem="resolved", resolved=OLD)
        with self.db() as c:
            delivery = c.execute("""INSERT INTO notification_deliveries(notification_id,channel,status,updated_at)
                VALUES (%s,'email','delivered',%s) RETURNING id""", (notification,OLD)).fetchone()["id"]
        with self.db() as reopening:
            reopening.execute("SELECT id FROM notifications WHERE id=%s FOR UPDATE", (notification,))
            self.tick()
            self.assertIn(delivery,self.ids("notification_deliveries"))
            reopening.execute("""UPDATE notifications SET problem_status='open',read_at=NULL,resolved_at=NULL
                WHERE id=%s""", (notification,))
            reopening.execute("UPDATE notification_deliveries SET status='pending',updated_at=%s WHERE id=%s", (RECENT,delivery))
        self.tick()
        self.assertIn(notification,self.ids("notifications"))
        self.assertIn(delivery,self.ids("notification_deliveries"))

    def test_audit_has_independent_90_day_boundary_and_preserves_actor(self):
        expected = {}
        with self.db() as c:
            for actor in ("owner","emergency","system"):
                for stamp in (OLD, (NOW-timedelta(days=90)).isoformat(), (NOW-timedelta(days=45)).isoformat()):
                    row = c.execute("INSERT INTO audit_events(created_at,event,actor) VALUES (%s,'sensitive-action',%s) RETURNING id", (stamp,actor)).fetchone()
                    if stamp != OLD:
                        expected[row["id"]] = actor
        self.tick()
        with self.db() as c:
            self.assertEqual({r["id"]:r["actor"] for r in c.execute("SELECT id,actor FROM audit_events")}, expected)
        self.tick(audit_days=40)
        self.assertFalse(self.ids("audit_events"))

    def test_only_old_never_prepared_cancelled_uploads_can_expire(self):
        eligible = self.job()
        kept = {self.job(status="completed"), self.job(status="failed"), self.job(status="queued"),
                self.job(snapshot="{}"), self.job(digest="a"*64), self.job(reconcile=True),
                self.job(stamp=CUTOFF), self.job(stamp=RECENT)}
        self.tick()
        self.assertNotIn(eligible,self.ids("jobs"))
        self.assertEqual(self.ids("jobs"), kept)

    def test_live_and_closed_references_prevent_even_pristine_job_deletion(self):
        notification_job, audit_job, inventory_job, intent_job = [self.job() for _ in range(4)]
        self.notification(job=notification_job, read=None)
        self.intent(intent_job, settled=True, started=False)
        with self.db() as c:
            c.execute("INSERT INTO audit_events(created_at,event,actor,job_id) VALUES (%s,'action','owner',%s)", (RECENT,audit_job))
            c.execute("""INSERT INTO snapshot_cleanup_inventories(job_id,target_json,inventory_json,created_at,completed_at)
                VALUES (%s,'{}','{}',%s,%s)""", (inventory_job,OLD,OLD))
        self.tick()
        self.assertEqual(self.ids("jobs"), {notification_job,audit_job,inventory_job,intent_job})

    def test_foreign_keys_still_protect_evidence_from_direct_deletion(self):
        import psycopg
        job = self.job()
        intent = self.intent(job,settled=True,started=False)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.db() as c:
                c.execute("DELETE FROM jobs WHERE id=%s", (job,))
        self.tick()
        self.assertIn(job,self.ids("jobs"))
        with self.db() as c:
            self.assertIsNotNone(c.execute("SELECT id FROM upload_intents WHERE id=%s", (intent,)).fetchone())

    def test_cancelled_ambiguous_intent_and_late_receipt_are_never_collected(self):
        job = self.job()
        intent = self.intent(job)
        with self.db() as c:
            c.execute("INSERT INTO upload_multiparts(intent_id,upload_id,created_at) VALUES (%s,'owned-upload',%s)", (intent,OLD))
        self.tick()
        provenance.append_receipt(self.db, intent, {"VersionId":"late-version"}, NOW.isoformat())
        self.tick()
        with self.db() as c:
            self.assertEqual(c.execute("SELECT response_json FROM upload_receipts WHERE intent_id=%s", (intent,)).fetchone()["response_json"], '{"VersionId":"late-version"}')
            self.assertEqual(c.execute("SELECT state FROM upload_multiparts WHERE intent_id=%s", (intent,)).fetchone()["state"], "open")
        self.assertEqual(self.ids("jobs"), {job})

    def test_keeper_settled_lineage_completed_proof_and_purged_versions_survive(self):
        digest = hashlib.sha256(b"a").hexdigest()
        encoded = base64.b64encode(bytes.fromhex(digest)).decode()
        snapshot = UploadSnapshot(1,digest,"FULL_OBJECT",encoded,DEFAULT_PART_SIZE,(encoded,)).as_json()
        keeper, purged = str(uuid.uuid4()), str(uuid.uuid4())
        with self.db() as c:
            for number, identity, availability in ((1,purged,"purged"),(2,keeper,"available")):
                c.execute("""INSERT INTO archive_versions(id,vault_file_id,vault_id,version_number,object_key,
                    provider_version_id,size,discovered_at,origin,integrity,availability,plaintext_sha256,upload_snapshot_json)
                    VALUES (%s,%s,%s,%s,'docs/a.txt',%s,1,%s,'upload','verified',%s,%s,%s)""",
                    (identity,self.file_id,self.vault_id,number,identity,OLD,availability,digest,snapshot))
        job = self.job(status="completed", version=keeper, snapshot=snapshot, digest=digest)
        intent = self.intent(job,settled=True,version=keeper,snapshot=snapshot)
        provenance.append_receipt(self.db,intent,{"VersionId":keeper,"ChecksumSHA256":encoded,"ChecksumType":"FULL_OBJECT"},OLD)
        with self.db() as c:
            c.execute("""INSERT INTO snapshot_cleanup_inventories(job_id,keeper_id,target_json,inventory_json,created_at,completed_at)
                VALUES (%s,%s,'{}','{}',%s,%s)""", (job,keeper,OLD,OLD))
            c.execute("""INSERT INTO snapshot_cleanup_proofs(vault_file_id,bucket,provider,object_key,version_id,
                keeper_id,sha256,size,method,proved_at,deleted_at)
                VALUES (%s,'bucket','provider','docs/a.txt',%s,%s,%s,1,'independent_lineage',%s,%s)""",
                (self.file_id,purged,keeper,digest,OLD,OLD))
            c.execute("""INSERT INTO local_copies(vault_file_id,presence,file_type,size,matched_archive_version_id,observed_at)
                VALUES (%s,'present','regular',1,%s,%s)""", (self.file_id,keeper,OLD))
            version = c.execute("SELECT * FROM archive_versions WHERE id=%s", (keeper,)).fetchone()
            self.assertTrue(_has_lineage(c,version,self.file_id,bucket="bucket",provider="provider"))
        self.tick()
        with self.db() as c:
            version = c.execute("SELECT * FROM archive_versions WHERE id=%s", (keeper,)).fetchone()
            self.assertTrue(_has_lineage(c,version,self.file_id,bucket="bucket",provider="provider"), "Settled intent remains independent cleanup authority")
            for table in ("snapshot_cleanup_inventories","snapshot_cleanup_proofs","upload_receipts","local_copies"):
                from psycopg import sql
                self.assertEqual(c.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"],1)
        self.assertEqual(self.ids("archive_versions"), {keeper,purged})

    def test_open_episode_blocks_unlinked_cancelled_job_and_history_is_retained(self):
        original, candidate = self.job(), self.job()
        intent = self.intent(original,settled=True,started=False)
        episode = str(uuid.uuid4())
        with self.db() as c:
            c.execute("""INSERT INTO upload_remediations(id,target_key,vault_id,vault_file_id,intent_id,
                target_json,requested_mode,size,state,due_at,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,'{}','s3_checksum',1,'cancelled',%s,%s,%s)""",
                (episode,"intent:"+intent,self.vault_id,self.file_id,intent,OLD,OLD,OLD))
        self.tick()
        self.assertEqual(self.ids("jobs"), {original,candidate})
        self.assertEqual(self.ids("upload_remediations"), {episode})

    def test_reference_exclusions_advance_cursor_and_wrap_rechecks_old_rows(self):
        blocked, eligible = self.job(), self.job()
        with self.db() as c:
            c.execute("INSERT INTO audit_events(created_at,event,actor,job_id) VALUES (%s,'action','system',%s)", (RECENT,blocked))
        self.tick(batch_size=1)
        self.assertEqual(self.ids("jobs"), {blocked,eligible})
        self.tick(batch_size=1)
        self.assertEqual(self.ids("jobs"), {blocked})
        self.tick(audit_days=1,batch_size=1)
        self.assertFalse(self.ids("jobs"), "A wrapped cursor revisits newly unreferenced history")

    def test_locked_page_and_late_creation_are_revisited_without_blocking(self):
        first = self.job()
        with self.db() as locked:
            locked.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (first,))
            result = self.tick(batch_size=1)
            self.assertEqual(result["jobs"]["deleted"],0)
        later = self.job()
        self.tick(batch_size=1)
        self.assertNotIn(first,self.ids("jobs"))
        self.assertIn(later,self.ids("jobs"))
        self.tick(batch_size=1)
        self.assertFalse(self.ids("jobs"))


if __name__ == "__main__":
    unittest.main()
