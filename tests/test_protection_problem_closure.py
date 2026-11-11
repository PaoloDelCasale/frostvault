"""Regressions for concurrent protection monitoring and superseded failures (#549/#554)."""
from contextlib import contextmanager
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
from tests.postgres import PostgresConnection
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
    def setUp(self):
        from tests.test_notifications import NotificationDeliveryTests
        NotificationDeliveryTests.setUp(self)
        with self.connect() as c:
            c.execute("UPDATE jobs SET status='failed',message_key='job.source_changed_stalled' WHERE id=%s", (self.job_id,))
            self.file_id = c.execute("SELECT vault_file_id FROM jobs WHERE id=%s", (self.job_id,)).fetchone()["vault_file_id"]
            c.execute("INSERT INTO file_paths(vault_file_id,vault_id,path,valid_from) VALUES (%s,%s,'a.txt',%s)",
                      (self.file_id,self.vault_id,(NOW-timedelta(days=1)).isoformat()))
            ArchiveCatalog(c).observe_local_copy(vault_id=self.vault_id, path="a.txt", file_type="regular",
                                                  size=1, mtime_ns=1, observed_at=(NOW-timedelta(hours=25)).isoformat())
            inbox.enqueue_job_terminal_notification(c, job_id=self.job_id)

    def connect(self):
        return PostgresConnection(str(self.path))

    @contextmanager
    def db(self):
        with self.connect() as c:
            yield c

    def protect(self, c, *, path="a.txt", completed=True):
        catalog = ArchiveCatalog(c)
        if path != "a.txt":
            catalog.observe_local_copy(vault_id=self.vault_id, path=path, file_type="regular", size=1,
                                       mtime_ns=1, observed_at=NOW.isoformat())
        version = catalog.record_archive_version(vault_id=self.vault_id, path=path,
            object_key="docs/"+path, provider_version_id="provider-"+path, size=1,
            storage_class="STANDARD", etag="e", uploaded_at=NOW.isoformat(),
            observed_at=NOW.isoformat(), scan_id="test")
        catalog.mark_version_verified(version, plaintext_sha256="a"*64, verified_at=NOW.isoformat())
        catalog.set_local_fingerprint(vault_id=self.vault_id, path=path, plaintext_sha256="a"*64,
                                      matched_archive_version_id=version)
        file_id = c.execute("SELECT vault_file_id FROM archive_versions WHERE id=%s", (version,)).fetchone()["vault_file_id"]
        job_id = c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at,archive_version_id)
            VALUES (%s,%s,%s,'upload',%s,%s,%s,%s) RETURNING id""",
            (self.vault_id,file_id,path,"completed" if completed else "failed",NOW.isoformat(),NOW.isoformat(),version)).fetchone()["id"]
        return job_id, version

    def state(self, c):
        inbox.reconcile_problem_states(c)
        return c.execute("SELECT problem_status FROM notifications WHERE job_id=%s", (self.job_id,)).fetchone()["problem_status"]

    def test_successful_protection_resolves_unsent_failure_and_backlog_not_history(self):
        with self.connect() as c:
            self.protect(c)
            self.assertEqual(self.state(c), "resolved")
            self.assertIsNotNone(c.execute("SELECT read_at FROM notifications WHERE job_id=%s", (self.job_id,)).fetchone()["read_at"])
            self.assertEqual(c.execute("SELECT status FROM jobs WHERE id=%s", (self.job_id,)).fetchone()["status"], "failed")
            self.assertEqual(inbox.count_open_problems(c, user_id=self.user_id), 0)
        with patch.object(storage, "db", self.db):
            self.assertEqual(storage.observe_failed_job_backlog(), (0, 0.0))

    def test_unrelated_success_or_failed_replacement_never_closes_problem(self):
        with self.connect() as c:
            self.protect(c, path="other.txt")
            self.assertEqual(self.state(c), "open")
            self.protect(c, completed=False)
            self.assertEqual(self.state(c), "open")

    def test_current_unprotected_content_or_unavailable_version_never_closes_problem(self):
        for mutation in ("UPDATE local_copies SET plaintext_sha256=NULL",
                         "UPDATE local_copies SET matched_archive_version_id=NULL",
                         "UPDATE local_copies SET plaintext_sha256=repeat('b',64)",
                         "UPDATE local_copies SET cloud_only_version_id=matched_archive_version_id",
                         "UPDATE archive_versions SET availability='missing'",
                         "UPDATE archive_versions SET integrity='unverified'"):
            with self.subTest(mutation=mutation), self.connect() as c:
                c.execute("SAVEPOINT scenario")
                self.protect(c)
                c.execute(mutation)
                self.assertEqual(self.state(c), "open")
                c.execute("ROLLBACK TO SAVEPOINT scenario")

    def test_materialized_failure_stays_open_even_with_new_protected_content(self):
        with self.connect() as c:
            _, version = self.protect(c)
            c.execute("UPDATE jobs SET archive_version_id=%s WHERE id=%s", (version,self.job_id))
            self.assertEqual(self.state(c), "open")

    def test_send_started_or_receipt_even_on_unbound_intent_requires_remediation(self):
        for sent, receipt in ((True, False), (False, True)):
            with self.subTest(sent=sent, receipt=receipt), self.connect() as c:
                c.execute("SAVEPOINT scenario")
                self.protect(c)
                # No upload_intent_jobs binding: old per-attempt intents remain
                # historical cloud facts, not permission to hide the failure.
                c.execute("""INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,
                    target_json,snapshot_json,requested,inventory_json,send_started,created_at)
                    VALUES ('intent','token',%s,%s,%s,'{}','{}','read_back','{}',%s,%s)""",
                    (self.vault_id,self.file_id,self.job_id,sent,NOW.isoformat()))
                if receipt:
                    c.execute("INSERT INTO upload_receipts VALUES ('intent','hash','{}',%s)", (NOW.isoformat(),))
                self.assertEqual(self.state(c), "open")
                c.execute("ROLLBACK TO SAVEPOINT scenario")

    def test_unsent_intent_can_be_superseded_but_reconcile_only_or_discovery_cannot(self):
        with self.connect() as c:
            self.protect(c)
            c.execute("""INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,
                target_json,snapshot_json,requested,inventory_json,send_started,created_at)
                VALUES ('intent','token',%s,%s,%s,'{}','{}','read_back','{}',FALSE,%s)""",
                (self.vault_id,self.file_id,self.job_id,NOW.isoformat()))
            c.execute("UPDATE jobs SET upload_reconcile_only=TRUE WHERE id=%s", (self.job_id,))
            self.assertEqual(self.state(c), "open")
            c.execute("UPDATE jobs SET upload_reconcile_only=FALSE WHERE id=%s", (self.job_id,))
            c.execute("INSERT INTO upload_remediation_discovery(job_id,vault_id,discovered_at) VALUES (%s,%s,%s)",
                      (self.job_id,self.vault_id,NOW.isoformat()))
            self.assertEqual(self.state(c), "open")
            c.execute("DELETE FROM upload_remediation_discovery WHERE job_id=%s", (self.job_id,))
            self.assertEqual(self.state(c), "resolved")

    def test_later_local_edit_does_not_resurrect_closed_historical_backlog(self):
        with self.connect() as c:
            self.protect(c)
            self.assertEqual(self.state(c), "resolved")
            c.execute("UPDATE local_copies SET matched_archive_version_id=NULL,plaintext_sha256=NULL")
            self.assertEqual(self.state(c), "resolved")
        with patch.object(storage, "db", self.db):
            self.assertEqual(storage.observe_failed_job_backlog(), (0, 0.0))

    def test_successful_recovery_is_not_successful_upload_protection(self):
        with self.connect() as c:
            successor, _ = self.protect(c)
            c.execute("UPDATE jobs SET action='recover' WHERE id=%s", (successor,))
            self.assertEqual(self.state(c), "open")

    def test_group_closes_only_when_all_its_failed_files_are_superseded(self):
        with self.connect() as c, patch.object(inbox, "now_iso", return_value=NOW.isoformat()):
            c.execute("UPDATE jobs SET updated_at=%s WHERE id=%s", (NOW.isoformat(),self.job_id))
            for _ in range(inbox.GROUP_THRESHOLD + 2):
                failed = c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at)
                    VALUES (%s,%s,'a.txt','upload','failed',%s,%s) RETURNING id""",
                    (self.vault_id,self.file_id,NOW.isoformat(),NOW.isoformat())).fetchone()["id"]
                inbox.enqueue_job_terminal_notification(c, job_id=failed)
            group = c.execute("SELECT id FROM notifications WHERE event='job_failed_group'").fetchone()
            self.assertIsNotNone(group)
            self.assertEqual(self.state(c), "open")
            self.protect(c)
            self.assertEqual(self.state(c), "resolved")
            self.assertEqual(inbox.count_open_problems(c, user_id=self.user_id), 0)
            c.execute("UPDATE local_copies SET matched_archive_version_id=NULL")
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
            c.execute("UPDATE jobs SET status='pending_approval',action='recover',group_id='approval',estimated_cost_eur=%s WHERE id=%s",
                      (Decimal("0.000000001"),self.job_id))
            inbox.enqueue_recovery_approval_best_effort(c, job_ids=[self.job_id])
            note = c.execute("SELECT * FROM notifications WHERE event='recovery_approval'").fetchone()
            self.assertGreater(Decimal(json.loads(note["message_params"])["cost"]), 0)
            self.assertIn("< €0.01", note["body"])
            self.assertIn("< 0,01 €", inbox._row_notification(note, locale="it")["body"])

    def test_upload_deletes_pending_between_candidate_and_age_reads(self):
        # Seed the durable timer so monitor's ON CONFLICT path takes no write lock
        # on it. A second committed transaction then performs the successful
        # protection publication + timer DELETE between the two READ COMMITTED reads.
        with self.connect() as c:
            c.execute("INSERT INTO protection_pending VALUES (%s,%s)", (self.file_id,(NOW-timedelta(hours=25)).isoformat()))
        reached = []
        fixture = self
        class Interleaved:
            def __init__(self, c):
                self.c = c
            def execute(self, query, params=None):
                if "SELECT p.* FROM protection_pending" in query:
                    with fixture.connect() as upload:
                        fixture.protect(upload)
                        upload.execute("DELETE FROM protection_pending WHERE vault_file_id=%s", (fixture.file_id,))
                    reached.append(True)
                return self.c.execute(query, params)
        with self.connect() as c, patch("app.services.source_layout.vault_local_access", return_value=ONLINE):
            vault = c.execute("SELECT * FROM vaults WHERE id=%s", (self.vault_id,)).fetchone()
            monitor.monitor_vault(Interleaved(c), vault, now=NOW)
            self.assertEqual(reached, [True])
            self.assertEqual(c.execute("SELECT count(*) n FROM protection_pending").fetchone()["n"], 0)
            self.assertEqual(c.execute("SELECT count(*) n FROM notifications WHERE event='protection_delayed' AND problem_status='open'").fetchone()["n"], 0)
            # The next cycle must run, not reopen the obsolete timer or a delay.
            monitor.monitor_vault(c, vault, now=NOW+timedelta(minutes=1))
            self.assertEqual(c.execute("SELECT count(*) n FROM protection_pending").fetchone()["n"], 0)
