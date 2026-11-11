"""Regressions for concurrent protection monitoring and superseded failures (#549/#554)."""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from app import main, storage
from app.catalog import ArchiveCatalog
from app.services import notifications as inbox
from app.services import protection_notifications as monitor
from app.services.cost_estimates import estimate_restore_cost, list_prices
from tests.postgres import PostgresConnection
from tests.test_protection_notifications import NOW, ONLINE


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
