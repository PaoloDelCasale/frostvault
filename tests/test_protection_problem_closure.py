"""Regressions for concurrent protection monitoring and superseded failures (#549/#554).

Rebuilt model (ADR-0035): the failed upload is a Job on its ``files`` row,
protection is the explicit Current/matched pair on that row, cloud work is an
``upload_attempts`` row (with receipts), and the pending timer is
``files.pending_since``.
"""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import MagicMock, patch

from app import main, storage
from app.catalog import ArchiveCatalog
from app.services import notifications as inbox
from app.services import protection_notifications as monitor
from app.services.cost_estimates import estimate_restore_cost, list_prices
from tests.catalog_support import fingerprint, protect as protect_current, sha256_hex
from tests.owner_fixtures import insert_owner, insert_vault
from tests.postgres import PostgresConnection, run_alembic
from tests.test_protection_notifications import NOW, ONLINE


class NotificationMonitorRaceUnitTests(unittest.TestCase):
    def test_candidate_whose_timer_was_deleted_is_not_counted_as_pending(self):
        from app.services.operation_policies import OperationPolicy
        c = MagicMock()
        candidate = {"id": "uploaded", "path": "a.txt", "mtime_ns": 1,
                     "observed_at": (NOW-timedelta(days=2)).isoformat()}
        def execute(query, params=None):
            result = MagicMock()
            result.fetchone.return_value = None
            result.fetchall.return_value = [candidate] if "SELECT vf.id, fp.path" in query else []
            # Later protection_pending read is empty: successful publication
            # deleted the timer between the monitor's READ COMMITTED statements.
            return result
        c.execute.side_effect = execute
        with patch.object(monitor, "get_policy", return_value=OperationPolicy()), \
             patch("app.services.source_layout.vault_local_access", return_value=ONLINE), \
             patch.object(inbox, "set_problem") as problem:
            monitor.monitor_vault(c, {"id": 1, "source_root": "/unused"}, now=NOW)
        delay = problem.call_args.kwargs
        self.assertEqual(delay["event"], "protection_delayed")
        self.assertFalse(delay["active"])
        self.assertEqual(delay["params"]["pending"], 0)


class RecoveryApprovalCostTests(unittest.TestCase):
    def test_subcent_cost_is_localized_without_losing_positive_value(self):
        for value in ("0.000000000001", "0.004", "0.0099999999999999999"):
            row = {"event": "recovery_approval", "body_key": "notification.recovery_approval.body",
                   "message_params": {"cost": value, "count": 1}, "body": ""}
            for locale, expected in (("en", "< €0.01"), ("it", "< 0,01 €")):
                with self.subTest(value=value, locale=locale):
                    body = inbox._localized_notification_text(row, key_field="body_key", text_field="body", locale=locale)
                    self.assertIn(expected, body)
                    self.assertNotIn("0.00", body)

    def test_zero_cent_and_ordinary_costs_remain_distinct(self):
        for value, en, it in (("0", "€0.00", "0,00 €"), ("0.01", "€0.01", "0,01 €"),
                              ("1234.56", "€1,234.56", "1.234,56 €")):
            row = {"event": "recovery_approval", "body_key": "notification.recovery_approval.body",
                   "message_params": {"cost": value, "count": 1}, "body": ""}
            for locale, expected in (("en", en), ("it", it)):
                with self.subTest(value=value, locale=locale):
                    body = inbox._localized_notification_text(row, key_field="body_key", text_field="body", locale=locale)
                    self.assertIn(expected, body)

    def test_small_restore_estimate_and_request_total_do_not_round_to_zero(self):
        estimate = estimate_restore_cost(list_prices("eu-west-1"), size_bytes=1,
                                         storage_class="DEEP_ARCHIVE", tier="Standard")
        self.assertGreater(estimate.estimated_cost_eur, 0)
        impact = main.recovery_request_impact([{"bytes": 1, "storage_class": "DEEP_ARCHIVE"}], tier="Standard")
        self.assertGreater(impact["estimated_cost_eur"], 0)


class ProtectionProblemClosureTests(unittest.TestCase):
    DIGEST = sha256_hex(b"a")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "closure.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with self.connect() as c:
            insert_owner(c)
            self.vault_id = insert_vault(c)["id"]
            ArchiveCatalog(c).observe_local_copy(vault_id=self.vault_id, path="a.txt", file_type="regular",
                                                  size=1, mtime_ns=1, observed_at=(NOW-timedelta(hours=25)).isoformat())
            self.file_id = str(self.file_of(c, "a.txt"))
            self.job_id = self.failed_job(c, self.file_id, at=NOW-timedelta(hours=1),
                                          message_key="job.source_changed_stalled")
            inbox.enqueue_job_terminal_notification(c, job_id=self.job_id)

    def connect(self):
        return PostgresConnection(str(self.path))

    @contextmanager
    def db(self):
        with self.connect() as c:
            yield c

    def file_of(self, c, path):
        return c.execute("SELECT id FROM files WHERE vault_id=%s AND path=%s",
                         (self.vault_id, path)).fetchone()["id"]

    def failed_job(self, c, file_id, *, at=NOW, message_key=None):
        return c.execute("""INSERT INTO jobs(vault_id,file_id,action,status,origin,message_key,requested_at,updated_at)
            VALUES (%s,%s,'upload','failed','manual',%s,%s,%s) RETURNING id""",
            (self.vault_id, file_id, message_key, at.isoformat(), at.isoformat())).fetchone()["id"]

    def protect(self, c, *, path="a.txt", completed=True, action="upload"):
        catalog = ArchiveCatalog(c)
        if path != "a.txt":
            catalog.observe_local_copy(vault_id=self.vault_id, path=path, file_type="regular", size=1,
                                       mtime_ns=1, observed_at=NOW.isoformat())
        fingerprint(catalog, self.vault_id, path, self.DIGEST)
        version = protect_current(catalog, self.vault_id, path, self.DIGEST,
                                  provider_version_id="provider-"+path)
        job_id = c.execute("""INSERT INTO jobs(vault_id,file_id,action,status,origin,requested_at,updated_at,archive_version_id)
            VALUES (%s,%s,%s,%s,'manual',%s,%s,%s) RETURNING id""",
            (self.vault_id, self.file_of(c, path), action, "completed" if completed else "failed",
             NOW.isoformat(), NOW.isoformat(), version)).fetchone()["id"]
        return job_id, version

    def attempt(self, c, *, sent):
        """A send attempt the failed Job opened (cloud work when it was sent)."""
        snapshot = c.execute("""INSERT INTO upload_snapshots(vault_id,file_id,path,path_since,source_root,provider,
                local_generation,size,mtime_ns,plaintext_sha256,part_size,part_sha256,verification,closed_at)
            SELECT vault_id,id,path,path_since,'/sources/managed/docs','s3',0,1,1,%s,8388608,ARRAY[%s::bytea],'read_back',NULL
            FROM files WHERE id=%s RETURNING id""",
            (bytes.fromhex(self.DIGEST), bytes.fromhex(self.DIGEST), self.file_id)).fetchone()["id"]
        attempt = c.execute("""INSERT INTO upload_attempts(snapshot_id,file_id,job_id,inventory_versions,inventory_markers)
            VALUES (%s,%s,%s,'{}','{}') RETURNING id""", (snapshot, self.file_id, self.job_id)).fetchone()["id"]
        if sent:
            c.execute("UPDATE upload_attempts SET send_started_at=%s WHERE id=%s", (NOW.isoformat(), attempt))
        return attempt

    def state(self, c):
        inbox.reconcile_problem_states(c)
        return c.execute("SELECT problem_status FROM notifications WHERE job_id=%s", (self.job_id,)).fetchone()["problem_status"]

    def test_successful_protection_resolves_unsent_failure_and_backlog_not_history(self):
        with self.connect() as c:
            self.protect(c)
            self.assertEqual(self.state(c), "resolved")
            self.assertIsNotNone(c.execute("SELECT read_at FROM notifications WHERE job_id=%s", (self.job_id,)).fetchone()["read_at"])
            self.assertEqual(c.execute("SELECT status FROM jobs WHERE id=%s", (self.job_id,)).fetchone()["status"], "failed")
            self.assertEqual(inbox.count_open_problems(c), 0)
        with patch.object(storage, "db", self.db):
            self.assertEqual(storage.observe_failed_job_backlog(), (0, 0.0))

    def test_unrelated_success_or_failed_replacement_never_closes_problem(self):
        with self.connect() as c:
            self.protect(c, path="other.txt")
            self.assertEqual(self.state(c), "open")
            self.protect(c, completed=False)
            self.assertEqual(self.state(c), "open")

    def test_current_unprotected_content_or_unavailable_version_never_closes_problem(self):
        other = sha256_hex(b"b")
        for mutation in ("UPDATE files SET matched_version_id=NULL, local_sha256=NULL",
                         "UPDATE files SET matched_version_id=NULL",
                         f"UPDATE files SET matched_version_id=NULL, local_sha256='\\x{other}'",
                         "UPDATE files SET matched_version_id=NULL, held_version_id=current_version_id",
                         "UPDATE archive_versions SET availability='missing'",
                         "UPDATE archive_versions SET integrity='unverified'"):
            with self.subTest(mutation=mutation), self.connect() as c:
                c.execute("SAVEPOINT scenario")
                self.protect(c)
                c.execute(mutation)
                self.assertEqual(self.state(c), "open")
                c.execute("ROLLBACK TO SAVEPOINT scenario")

    def test_completion_must_be_after_failure_and_target_current_snapshot(self):
        with self.connect() as c:
            successor, version = self.protect(c)
            c.execute("UPDATE jobs SET updated_at='2000-01-01T00:00:00Z' WHERE id=%s", (successor,))
            self.assertEqual(self.state(c), "open")
            c.execute("UPDATE jobs SET updated_at=%s,archive_version_id=NULL WHERE id=%s", (NOW.isoformat(),successor))
            self.assertEqual(self.state(c), "open")
            c.execute("UPDATE jobs SET archive_version_id=%s WHERE id=%s", (version,successor))
            self.assertEqual(self.state(c), "resolved")

    def test_materialized_failure_stays_open_even_with_new_protected_content(self):
        with self.connect() as c:
            _, version = self.protect(c)
            c.execute("UPDATE jobs SET archive_version_id=%s WHERE id=%s", (version,self.job_id))
            self.assertEqual(self.state(c), "open")

    def test_send_started_or_receipt_requires_remediation(self):
        for sent, receipt in ((True, False), (False, True)):
            with self.subTest(sent=sent, receipt=receipt), self.connect() as c:
                c.execute("SAVEPOINT scenario")
                self.protect(c)
                # Not bound to a remediation episode: a started send or a
                # receipt is a cloud fact, not permission to hide the failure.
                attempt = self.attempt(c, sent=sent)
                if receipt:
                    c.execute("INSERT INTO upload_receipts(attempt_id,receipt_sha256,response) VALUES (%s,%s,'{}')",
                              (attempt, bytes.fromhex(self.DIGEST)))
                self.assertEqual(self.state(c), "open")
                c.execute("ROLLBACK TO SAVEPOINT scenario")

    def test_unsent_attempt_can_be_superseded_but_reconcile_only_or_remediation_cannot(self):
        with self.connect() as c:
            self.protect(c)
            self.attempt(c, sent=False)
            c.execute("INSERT INTO job_uploads(job_id,verification_mode,reconcile_only) VALUES (%s,'read_back',TRUE)",
                      (self.job_id,))
            self.assertEqual(self.state(c), "open")
            c.execute("UPDATE job_uploads SET reconcile_only=FALSE WHERE job_id=%s", (self.job_id,))
            remediation = c.execute("""INSERT INTO upload_remediations(vault_id,file_id,due_at)
                VALUES (%s,%s,%s) RETURNING id""", (self.vault_id,self.file_id,NOW.isoformat())).fetchone()["id"]
            c.execute("INSERT INTO upload_remediation_jobs(job_id,remediation_id) VALUES (%s,%s)",
                      (self.job_id,remediation))
            self.assertEqual(self.state(c), "open")
            c.execute("DELETE FROM upload_remediation_jobs WHERE job_id=%s", (self.job_id,))
            self.assertEqual(self.state(c), "resolved")

    def test_later_local_edit_does_not_resurrect_closed_historical_backlog(self):
        with self.connect() as c:
            self.protect(c)
            self.assertEqual(self.state(c), "resolved")
            c.execute("UPDATE files SET matched_version_id=NULL,local_sha256=NULL")
            self.assertEqual(self.state(c), "resolved")
        with patch.object(storage, "db", self.db):
            self.assertEqual(storage.observe_failed_job_backlog(), (0, 0.0))

    def test_successful_recovery_is_not_successful_upload_protection(self):
        with self.connect() as c:
            self.protect(c, action="recover")
            self.assertEqual(self.state(c), "open")

    def test_group_closes_only_when_all_its_failed_files_are_superseded(self):
        with self.connect() as c, patch.object(inbox, "now_iso", return_value=NOW.isoformat()):
            c.execute("UPDATE jobs SET updated_at=%s WHERE id=%s", (NOW.isoformat(),self.job_id))
            for _ in range(inbox.GROUP_THRESHOLD + 2):
                failed = self.failed_job(c, self.file_id)
                inbox.enqueue_job_terminal_notification(c, job_id=failed)
            ArchiveCatalog(c).observe_local_copy(vault_id=self.vault_id, path="b.txt", file_type="regular",
                                                  size=1, mtime_ns=1, observed_at=NOW.isoformat())
            failed = self.failed_job(c, self.file_of(c, "b.txt"))
            inbox.enqueue_job_terminal_notification(c, job_id=failed)
            group = c.execute("SELECT id FROM notifications WHERE event='job_failed_group'").fetchone()
            self.assertIsNotNone(group)
            self.assertEqual(self.state(c), "open")
            self.protect(c)
            self.assertEqual(self.state(c), "resolved")
            self.assertEqual(inbox.count_open_problems(c), 1)
            self.protect(c, path="b.txt")
            self.assertEqual(inbox.count_open_problems(c), 0)
            c.execute("UPDATE files SET matched_version_id=NULL")
            # A delayed outbox replay for a formerly folded failure must keep
            # its durable group closure even when it gains its own history row.
            inbox.enqueue_job_terminal_notification(c, job_id=failed)
            self.assertEqual(c.execute("SELECT problem_status FROM notifications WHERE job_id=%s", (failed,)).fetchone()["problem_status"], "resolved")
        with patch.object(storage, "db", self.db):
            self.assertEqual(storage.observe_failed_job_backlog(), (0, 0.0))

    def test_late_outbox_materializes_resolved_problem_and_does_not_reopen(self):
        with self.connect() as c:
            c.execute("DELETE FROM notifications")
            self.protect(c)
            inbox.enqueue_job_terminal_notification(c, job_id=self.job_id)
            self.assertEqual(c.execute("SELECT problem_status FROM notifications WHERE job_id=%s", (self.job_id,)).fetchone()["problem_status"], "resolved")
            inbox.enqueue_job_terminal_notification(c, job_id=self.job_id)
            self.assertEqual(self.state(c), "resolved")

    def test_approval_persists_unrounded_cost_and_english_fallback(self):
        with self.connect() as c:
            ArchiveCatalog(c).observe_local_copy(vault_id=self.vault_id, path="r.txt", file_type="regular",
                                                  size=1, mtime_ns=1, observed_at=NOW.isoformat())
            group = str(uuid.uuid4())
            c.execute("INSERT INTO job_groups(id,vault_id) VALUES (%s,%s)", (group,self.vault_id))
            c.execute("INSERT INTO restore_requests(group_id,estimated_cost_eur,estimated_hours) VALUES (%s,%s,1)",
                      (group,Decimal("0.000000001")))
            recover = c.execute("""INSERT INTO jobs(vault_id,file_id,action,status,origin,group_id)
                VALUES (%s,%s,'recover','pending_approval','manual',%s) RETURNING id""",
                (self.vault_id,self.file_of(c, "r.txt"),group)).fetchone()["id"]
            inbox.enqueue_recovery_approval_best_effort(c, job_ids=[recover])
            note = c.execute("SELECT * FROM notifications WHERE event='recovery_approval'").fetchone()
            params = note["message_params"]
            params = params if isinstance(params, dict) else json.loads(params)
            self.assertGreater(Decimal(params["cost"]), 0)
            self.assertIn("< €0.01", inbox._row_notification(note)["body"])
            self.assertIn("< 0,01 €", inbox._row_notification(note, locale="it")["body"])

    def test_upload_clears_pending_between_candidate_and_timer_writes(self):
        # Seed the durable timer. A second committed transaction then performs
        # the successful protection publication between the monitor's
        # candidate read and its timer write (READ COMMITTED statements).
        with self.connect() as c:
            c.execute("UPDATE files SET pending_since=%s WHERE id=%s",
                      ((NOW-timedelta(hours=25)).isoformat(), self.file_id))
        reached = []
        fixture = self
        class Interleaved:
            def __init__(self, c):
                self.c = c
            def execute(self, query, params=None):
                # The monitor's read of the durable timers (#622).
                if "AS wanted" in query:
                    with fixture.connect() as upload:
                        fixture.protect(upload)
                        upload.execute("UPDATE files SET pending_since=NULL WHERE id=%s", (fixture.file_id,))
                    reached.append(True)
                return self.c.execute(query, params)
        with self.connect() as c, patch("app.services.source_layout.vault_local_access", return_value=ONLINE):
            vault = c.execute("SELECT * FROM vaults WHERE id=%s", (self.vault_id,)).fetchone()
            monitor.monitor_vault(Interleaved(c), vault, now=NOW)
            self.assertEqual(reached, [True])
            self.assertEqual(c.execute("SELECT count(*) n FROM files WHERE pending_since IS NOT NULL").fetchone()["n"], 0)
            self.assertEqual(c.execute("SELECT count(*) n FROM notifications WHERE event='protection_delayed' AND problem_status='open'").fetchone()["n"], 0)
            # The next cycle must run, not reopen the obsolete timer or a delay.
            monitor.monitor_vault(c, vault, now=NOW+timedelta(minutes=1))
            self.assertEqual(c.execute("SELECT count(*) n FROM files WHERE pending_since IS NOT NULL").fetchone()["n"], 0)
