"""Each family of Problems closes from the state of its own domain (#804).

One open Problem per family: reconciling leaves it open while its domain still
says so, and resolves it once the domain no longer does. A Problem whose family
has no domain rule stays open; the Problems of a stopped Vault close.
"""
from datetime import datetime, timedelta, timezone

from app.services.local_copies import LocalCopies
from app.services import notifications as inbox
from tests.catalog_support import fingerprint, protect, sha256_hex
from tests.fixtures import FrostVaultTestCase, seed_job, seed_vault

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class ProblemFamilyResolutionTests(FrostVaultTestCase):
    def setUp(self):
        super().setUp()
        with self.database.connect() as c:
            self.vault_id = seed_vault(c)["id"]

    def open_problem(self, c, *, event, key=None, params=None):
        key = key or f"{event}:{self.vault_id}"
        inbox.set_problem(c, key=key, event=event, vault_id=self.vault_id,
                          active=True, params=params, email=False)
        return key

    def status(self, c, key):
        inbox.reconcile_problem_states(c)
        row = c.execute(
            "SELECT problem_status, resolved_at, read_at FROM notifications WHERE dedupe_key=%s",
            (key,)).fetchone()
        if row["problem_status"] == "resolved":
            self.assertIsNotNone(row["resolved_at"])
            self.assertIsNotNone(row["read_at"])
        else:
            self.assertIsNone(row["resolved_at"])
        return row["problem_status"]

    def protected_file(self, c, path="a.txt"):
        """A Vault File with one verified Archive Version; returns (file id, version id)."""
        local_copies = LocalCopies(c)
        local_copies.observe_local_copy(vault_id=self.vault_id, path=path, file_type="regular",
                                   size=1, mtime_ns=1, observed_at=NOW.isoformat())
        digest = sha256_hex(b"a")
        fingerprint(c, self.vault_id, path, digest)
        version = protect(c, self.vault_id, path, digest)
        file_id = c.execute("SELECT id FROM files WHERE vault_id=%s AND path=%s",
                            (self.vault_id, path)).fetchone()["id"]
        return file_id, version

    def observed_file(self, c, path="a.txt"):
        LocalCopies(c).observe_local_copy(vault_id=self.vault_id, path=path, file_type="regular",
                                             size=1, mtime_ns=1, observed_at=NOW.isoformat())

    def failed_upload(self, c, path="a.txt"):
        self.observed_file(c, path)
        job_id = seed_job(c, vault_id=self.vault_id, path=path, status="failed")
        c.execute("UPDATE jobs SET requested_at=%s, updated_at=%s WHERE id=%s",
                  (NOW.isoformat(), NOW.isoformat(), job_id))
        return job_id

    def test_duplicate_cleanup_capped_closes_once_the_cleanup_is_off(self):
        with self.database.connect() as c:
            c.execute("UPDATE vaults SET duplicate_cleanup_enabled=TRUE WHERE id=%s", (self.vault_id,))
            key = self.open_problem(c, event="duplicate_cleanup_capped")
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE vaults SET duplicate_cleanup_enabled=FALSE WHERE id=%s", (self.vault_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_mirror_cleanup_capped_closes_once_the_cleanup_is_off(self):
        with self.database.connect() as c:
            c.execute("UPDATE vaults SET mirror_cleanup_enabled=TRUE WHERE id=%s", (self.vault_id,))
            key = self.open_problem(c, event="mirror_cleanup_capped")
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE vaults SET mirror_cleanup_enabled=FALSE WHERE id=%s", (self.vault_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_vault_bucket_failed_closes_once_the_vault_left_provisioning(self):
        with self.database.connect() as c:
            self.vault_id = seed_vault(c, name="New", source_root="/sources/managed/new",
                                       lifecycle="provisioning")["id"]
            key = self.open_problem(c, event="vault_bucket_failed")
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE vaults SET lifecycle='active' WHERE id=%s", (self.vault_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_catalog_audit_failed_closes_with_the_next_successful_audit(self):
        with self.database.connect() as c:
            c.execute("INSERT INTO vault_component_health(vault_id, component, last_error) "
                      "VALUES (%s, 'audit', 'failed')", (self.vault_id,))
            key = self.open_problem(c, event="catalog_audit_failed")
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE vault_component_health SET last_error=NULL WHERE vault_id=%s",
                      (self.vault_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_file_unreadable_closes_with_a_later_clean_source_observation(self):
        with self.database.connect() as c:
            c.execute("INSERT INTO vault_component_health(vault_id, component, last_error, last_success_at) "
                      "VALUES (%s, 'source', 'unreadable', now() + interval '1 hour')", (self.vault_id,))
            key = self.open_problem(c, event="file_unreadable", params={"path": "a.txt"})
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE vault_component_health SET last_error=NULL WHERE vault_id=%s",
                      (self.vault_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_file_unreadable_closes_once_its_path_was_observed_again(self):
        with self.database.connect() as c:
            self.observed_file(c)
            key = self.open_problem(c, event="file_unreadable", params={"path": "a.txt"})
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE files SET local_observed_at=now() + interval '1 hour' WHERE vault_id=%s",
                      (self.vault_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_upload_reconciliation_closes_once_no_file_under_its_key_is_blocked(self):
        with self.database.connect() as c:
            file_id, _version = self.protected_file(c)
            LocalCopies(c).block_reconciliation(file_id, reason="ambiguous_send", at=NOW.isoformat())
            key = self.open_problem(c, event="upload_reconciliation",
                                    key=f"upload_reconciliation:{file_id}")
            self.assertEqual(self.status(c, key), "open")
            LocalCopies(c).clear_reconciliation_block(file_id)
            self.assertEqual(self.status(c, key), "resolved")

    def test_archive_version_missing_closes_once_the_version_is_located_again(self):
        with self.database.connect() as c:
            _file_id, version = self.protected_file(c)
            c.execute("UPDATE archive_versions SET availability='missing' WHERE id=%s", (version,))
            key = self.open_problem(c, event="archive_version_missing", key=f"missing:{version}")
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE archive_versions SET availability='available' WHERE id=%s", (version,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_cloud_only_conflict_closes_once_the_file_no_longer_holds_that_version(self):
        with self.database.connect() as c:
            file_id, version = self.protected_file(c)
            c.execute("UPDATE files SET held_version_id=%s, matched_version_id=NULL WHERE id=%s",
                      (version, file_id))
            key = self.open_problem(c, event="cloud_only_conflict",
                                    key=inbox.cloud_only_conflict_key(file_id, version))
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE files SET held_version_id=NULL WHERE id=%s", (file_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_job_failed_closes_once_its_file_is_gone_with_no_cloud_work(self):
        with self.database.connect() as c:
            job_id = self.failed_upload(c)
            self.assertEqual(inbox.enqueue_job_terminal_notification(c, job_id=job_id), 1)
            key = f"job:{job_id}:job_failed"
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE files SET local_presence='missing' WHERE vault_id=%s", (self.vault_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_job_failed_group_closes_once_every_folded_failure_is_closed(self):
        with self.database.connect() as c:
            self.failed_upload(c)
            key = self.open_problem(
                c, event="job_failed_group", key=f"job_failed_group:{self.vault_id}",
                params={"count": 1, "since": (NOW - timedelta(minutes=5)).isoformat(),
                        "last": (NOW + timedelta(minutes=5)).isoformat()})
            self.assertEqual(self.status(c, key), "open")
            c.execute("UPDATE files SET local_presence='missing' WHERE vault_id=%s", (self.vault_id,))
            self.assertEqual(self.status(c, key), "resolved")

    def test_a_late_failure_notice_of_a_closed_failure_is_born_resolved(self):
        with self.database.connect() as c:
            job_id = self.failed_upload(c)
            c.execute("UPDATE files SET local_presence='missing' WHERE vault_id=%s", (self.vault_id,))
            inbox.enqueue_job_terminal_notification(c, job_id=job_id)
            row = c.execute("SELECT problem_status FROM notifications WHERE job_id=%s",
                            (job_id,)).fetchone()
            self.assertEqual(row["problem_status"], "resolved")

    def test_a_problem_without_a_domain_rule_stays_open(self):
        with self.database.connect() as c:
            key = self.open_problem(c, event="internal_error", params={"component": "worker"})
            self.assertEqual(self.status(c, key), "open")

    def test_every_open_problem_of_a_stopping_vault_closes(self):
        with self.database.connect() as c:
            c.execute("UPDATE vaults SET duplicate_cleanup_enabled=TRUE WHERE id=%s", (self.vault_id,))
            capped = self.open_problem(c, event="duplicate_cleanup_capped")
            internal = self.open_problem(c, event="internal_error", params={"component": "worker"})
            c.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=%s", (self.vault_id,))
            self.assertEqual(self.status(c, capped), "resolved")
            self.assertEqual(self.status(c, internal), "resolved")

    def test_reconciling_all_families_at_once_closes_only_the_resolved_ones(self):
        with self.database.connect() as c:
            c.execute("UPDATE vaults SET duplicate_cleanup_enabled=TRUE, mirror_cleanup_enabled=FALSE "
                      "WHERE id=%s", (self.vault_id,))
            c.execute("INSERT INTO vault_component_health(vault_id, component, last_error) "
                      "VALUES (%s, 'audit', 'failed')", (self.vault_id,))
            file_id, version = self.protected_file(c)
            job_id = self.failed_upload(c, path="b.txt")
            inbox.enqueue_job_terminal_notification(c, job_id=job_id)
            stays = [
                self.open_problem(c, event="duplicate_cleanup_capped"),
                self.open_problem(c, event="catalog_audit_failed"),
                self.open_problem(c, event="internal_error", params={"component": "worker"}),
                f"job:{job_id}:job_failed",
            ]
            closes = [
                self.open_problem(c, event="mirror_cleanup_capped"),
                self.open_problem(c, event="vault_bucket_failed"),
                self.open_problem(c, event="upload_reconciliation",
                                  key=f"upload_reconciliation:{file_id}"),
                self.open_problem(c, event="archive_version_missing", key=f"missing:{version}"),
                self.open_problem(c, event="cloud_only_conflict",
                                  key=inbox.cloud_only_conflict_key(file_id, version)),
            ]
            for key in stays:
                self.assertEqual(self.status(c, key), "open", key)
            for key in closes:
                self.assertEqual(self.status(c, key), "resolved", key)
