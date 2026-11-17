"""Retention Lock deletion paths on a real catalog (#736, ADR-0050).

A disposable PostgreSQL 16 database (``TEST_POSTGRES_URL``) and a Vault whose
Vault Bucket has a 30-day Governance default retention. The S3 transport is
the synthetic ``VersionedProvider`` extended with Object Lock: a version is
locked for 30 days from when it was written, a DELETE of a locked version is
refused with AccessDenied (FrostVault has no ``s3:BypassGovernanceRetention``)
and ``GetObjectRetention`` reports its date.

Covered: only a locked Vault can hand a version over; a superseded Current
Snapshot is handed over, not deleted, the upload completes and the sweep
deletes it by exact VersionId once its date has passed; the audit knows the
retained versions and reports a Delete Marker as a Problem; Delayed Cloud
Deletion defers a locked Current «deletable from» its date and deletes it
then; a Vault Deletion with only locked versions left waits as pending, not
failed, and removes the Vault once they are deleted.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg
from botocore.exceptions import ClientError

from app import storage
from app.services import protection_stop, retention_lock, vault_deletion
from app.services import notifications as inbox
from app.services import bucket_metrics
from app.services.vault_buckets import TAG_VAULT
from app.services.vaults import VAULT_ROW_SQL
from tests import test_cloud_purge
from tests import test_upload_worker as fixtures
from tests.catalog_support import seed_vault
from tests.test_upload_worker import A, B
from tests.upload_provenance_provider import VersionedProvider

BUCKET = "frostvault-docs-ab12cd34"
DAYS = 30
PAST = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()


def locked_seed(connection, *, vault_id: int = 2, source_root: str = "/source", name: str = "Docs") -> int:
    """The Vault of the upload fixture, in a Vault Bucket with a Retention Lock."""
    target = connection.execute(
        """INSERT INTO vault_targets(provider, bucket, region, key_layout, retention_lock_days)
           VALUES ('s3', %s, 'eu-south-1', 'bucket_root', %s) RETURNING id""",
        (BUCKET, DAYS)).fetchone()
    connection.execute("INSERT INTO vaults(id, target_id, name, source_root) VALUES (%s, %s, %s, %s)",
                       (vault_id, target["id"], name, source_root))
    return vault_id


class LockedProvider(VersionedProvider):
    """Versions locked ``days`` from when written; no bypass."""

    days = DAYS

    def __init__(self):
        super().__init__()
        self.written: dict[tuple[str, str], datetime] = {}
        self.clock = lambda: datetime.now(timezone.utc)
        self.retention_reads = 0
        self.bucket_tags: dict[str, str] = {}
        self.deleted_buckets: list[str] = []

    def get_bucket_tagging(self, *, Bucket):
        return {"TagSet": [{"Key": k, "Value": v} for k, v in self.bucket_tags.items()]}

    def delete_bucket(self, *, Bucket):
        if self.objects or self.markers:
            raise ClientError({"Error": {"Code": "BucketNotEmpty", "Message": "provider text"}},
                              "DeleteBucket")
        self.deleted_buckets.append(Bucket)
        return {}

    def add(self, key, payload, metadata=None, *, version=None):
        version = super().add(key, payload, metadata, version=version)
        self.written[key, version] = self.clock()
        return version

    def until(self, key, version) -> datetime:
        return self.written[key, version] + timedelta(days=self.days)

    def locked(self, key, version) -> bool:
        return (key, version) in self.objects and self.until(key, version) > self.clock()

    def age(self, key, version, days: int) -> None:
        """The version was written ``days`` ago."""
        self.written[key, version] = self.clock() - timedelta(days=days)

    def list_object_versions(self, *, Bucket, Prefix, **kwargs):
        response = super().list_object_versions(Bucket=Bucket, Prefix=Prefix, **kwargs)
        for item in response["Versions"]:
            item["LastModified"] = self.written.get((item["Key"], item["VersionId"]), item["LastModified"])
        return response

    def delete_object(self, *, Bucket, Key, VersionId):
        if self.locked(Key, VersionId):
            self.delete_requests += 1
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "provider text"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, "DeleteObject")
        return super().delete_object(Bucket=Bucket, Key=Key, VersionId=VersionId)

    def delete_objects(self, *, Bucket, Delete):
        refused = [o for o in Delete["Objects"] if self.locked(o["Key"], o["VersionId"])]
        allowed = [o for o in Delete["Objects"] if o not in refused]
        response = super().delete_objects(Bucket=Bucket, Delete={**Delete, "Objects": allowed}) \
            if allowed else {}
        for item in allowed:
            self.markers.pop((item["Key"], item["VersionId"]), None)
        if refused:
            response = {**(response or {}), "Errors": [
                {"Key": o["Key"], "VersionId": o["VersionId"], "Code": "AccessDenied",
                 "Message": "provider text"} for o in refused]}
        return response

    def get_object_retention(self, *, Bucket, Key, VersionId):
        self.retention_reads += 1
        if (Key, VersionId) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchVersion", "Message": "provider text"},
                               "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObjectRetention")
        return {"Retention": {"Mode": "GOVERNANCE", "RetainUntilDate": self.until(Key, VersionId)}}


class RetentionLockFixture(unittest.TestCase):
    db = fixtures.UploadWorkerTests.db
    observe = fixtures.UploadWorkerTests.observe
    write = fixtures.UploadWorkerTests.write
    queue = fixtures.UploadWorkerTests.queue
    claim = fixtures.UploadWorkerTests.claim
    run_job = fixtures.UploadWorkerTests.run_job
    job = fixtures.UploadWorkerTests.job
    file_row = fixtures.UploadWorkerTests.file_row
    rows = fixtures.UploadWorkerTests.rows
    version = fixtures.UploadWorkerTests.version
    assert_current = fixtures.UploadWorkerTests.assert_current
    cloud_only = test_cloud_purge.CloudPurgeTests.cloud_only
    schedule = test_cloud_purge.CloudPurgeTests.schedule
    claim_group = test_cloud_purge.CloudPurgeTests.claim_group
    run_purge = test_cloud_purge.CloudPurgeTests.run_purge

    def setUp(self) -> None:
        with patch("tests.test_upload_worker.VersionedProvider", LockedProvider), \
                patch("tests.test_upload_worker.seed_vault", locked_seed):
            fixtures.UploadWorkerTests.setUp(self)
        # A Vault Bucket: the key is the relative path.
        self.key = "report.txt"
        # Its tag names this Vault (#739).
        self.provider.bucket_tags = {TAG_VAULT: str(self.vault()["uuid"])}

    def upload(self, payload: bytes) -> dict:
        self.write(payload)
        job_id = self.queue()
        self.run_job(job_id)
        return self.assert_current(job_id, payload)

    def vault(self) -> dict:
        with self.db() as connection:
            return connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()

    def retained(self) -> list[dict]:
        return self.rows("SELECT * FROM retained_versions ORDER BY retain_until")

    def open_problems(self) -> list[str]:
        with self.db() as connection:
            inbox.reconcile_problem_states(connection)
            return [r["event"] for r in connection.execute(
                "SELECT event FROM notifications WHERE problem_status='open'").fetchall()]


class RetainedVersionTests(RetentionLockFixture):
    def test_only_a_vault_with_a_retention_lock_hands_a_version_over(self) -> None:
        with self.db() as connection:
            seed_vault(connection, vault_id=3, source_root=str(self.root / "other"))
            with self.assertRaises(psycopg.errors.RestrictViolation):
                retention_lock.hand_over(connection, vault_id=3, object_key="a.txt", version_id="v1",
                                         size=1, storage_class="STANDARD",
                                         retain_until=datetime.now(timezone.utc))
        later = datetime.now(timezone.utc) + timedelta(days=3)
        with self.db() as connection:
            retention_lock.hand_over(connection, vault_id=2, object_key="a.txt", version_id="v1",
                                     size=1, storage_class="STANDARD", retain_until=later)
            # Handed over again: the later date wins, never an earlier one.
            retention_lock.hand_over(connection, vault_id=2, object_key="a.txt", version_id="v1",
                                     size=1, storage_class="STANDARD",
                                     retain_until=later - timedelta(days=1))
        with self.db() as connection, self.assertRaises(psycopg.errors.RestrictViolation):
            connection.execute("UPDATE retained_versions SET object_key='b.txt'")
        [row] = self.retained()
        self.assertEqual(row["retain_until"], later)

    def test_a_superseded_version_waits_for_its_date_then_is_deleted_by_exact_version(self) -> None:
        first = self.upload(A)
        second = self.upload(B)
        old = (self.key, first["provider_version_id"])
        # Not deleted: still locked, and FrostVault has no bypass.
        self.assertIn(old, self.provider.objects)
        self.assertNotIn(old, self.provider.deleted)
        self.assertEqual(self.provider.delete_requests, 0, "the estimate kept the DELETE from being sent")
        # The upload completed, and the catalog keeps only the new Current.
        self.assertEqual([r["id"] for r in self.rows(
            "SELECT id FROM archive_versions WHERE file_id=%s", self.file_id)], [second["id"]])
        self.assertEqual(self.rows("SELECT id FROM cleanup_batches"), [])
        [row] = self.retained()
        self.assertEqual((row["object_key"], row["provider_version_id"], row["size"]),
                         (self.key, first["provider_version_id"], len(A)))
        self.assertEqual(row["retain_until"], first["created_at"] + timedelta(days=DAYS))
        with self.db() as connection:
            state = retention_lock.status(connection, 2)
        self.assertEqual((state["days"], state["mode"], state["retained"]["count"]), (DAYS, "GOVERNANCE", 1))

        # The sweep before the date: nothing is due.
        self.assertEqual(retention_lock.run_due(self.db, lambda: self.provider), {})
        # After it: deleted by exact VersionId, the row goes.
        later = datetime.now(timezone.utc) + timedelta(days=DAYS + 1)
        self.provider.clock = lambda: later
        self.assertEqual(retention_lock.run_due(self.db, lambda: self.provider, now=later), {"deleted": 1})
        self.assertIn(old, self.provider.deleted)
        self.assertNotIn(old, self.provider.objects)
        self.assertIn((self.key, second["provider_version_id"]), self.provider.objects)
        self.assertEqual(self.retained(), [])
        self.assertEqual([r["event"] for r in self.rows(
            "SELECT event FROM audit_events WHERE event LIKE 'retention_lock.%%'")],
            ["retention_lock.retained_deleted"])

    def test_a_refusal_the_estimate_missed_moves_the_row_to_the_observed_date(self) -> None:
        first = self.upload(A)
        self.upload(B)
        [row] = self.retained()
        # Someone extended the version's retention outside FrostVault.
        self.provider.days = DAYS * 3
        later = datetime.now(timezone.utc) + timedelta(days=DAYS + 1)
        self.provider.clock = lambda: later
        self.assertEqual(retention_lock.run_due(self.db, lambda: self.provider, now=later), {"deferred": 1})
        [moved] = self.retained()
        self.assertEqual(moved["retain_until"], self.provider.until(self.key, first["provider_version_id"]))
        self.assertGreater(moved["retain_until"], row["retain_until"])
        self.assertIn((self.key, first["provider_version_id"]), self.provider.objects)

    def test_the_audit_knows_retained_versions_and_reports_a_delete_marker(self) -> None:
        from app.services.catalog_audit import audit_vault_catalog

        self.upload(A)
        self.upload(B)
        with self.db() as connection:
            report = audit_vault_catalog(connection, self.vault(), self.provider)
        self.assertEqual((report["missing_in_catalog"], report["missing_in_cloud"]), (0, 0))
        self.assertNotIn(retention_lock.DELETE_MARKER_PROBLEM, self.open_problems())

        # A DELETE without VersionId by someone else: a marker hides the file.
        self.provider.markers[self.key, "marker-1"] = True
        with self.db() as connection:
            audit_vault_catalog(connection, self.vault(), self.provider)
        self.assertIn(retention_lock.DELETE_MARKER_PROBLEM, self.open_problems())
        self.provider.markers.clear()
        with self.db() as connection:
            audit_vault_catalog(connection, self.vault(), self.provider)
        self.assertNotIn(retention_lock.DELETE_MARKER_PROBLEM, self.open_problems())


class DeferredCloudDeletionTests(RetentionLockFixture):
    def test_a_locked_current_waits_deletable_from_its_date_then_is_deleted(self) -> None:
        current = self.cloud_only()
        with self.db() as connection:
            from app.services import cloud_deletion
            preview = cloud_deletion.preview_selection(connection, vault_id=2, paths=["report.txt"])
        lock = preview.as_dict()["retention_lock"]
        self.assertEqual((lock["days"], lock["locked_count"], lock["locked_bytes"]), (DAYS, 1, len(A)))
        scheduled = self.schedule()
        [job_id] = scheduled.job_ids
        self.run_purge(job_id)
        job = self.job(job_id)
        until = current["created_at"] + timedelta(days=DAYS)
        # Deferred, not failed: «deletable from» its date, nothing sent to S3.
        self.assertEqual((job["status"], job["message_key"]), ("pending_delay", "job.cloud_purge_deferred"))
        self.assertEqual(job["message_params"], {"until": until.date().isoformat()})
        self.assertEqual(job["run_after"], until)
        self.assertEqual(self.provider.deleted, [])
        self.assertEqual(self.file_row()["current_version_id"], current["id"])
        self.assertIsNone(self.claim_group(job_id), "nothing runs before the date")
        self.assertEqual([r["event"] for r in self.rows(
            "SELECT event FROM audit_events WHERE event LIKE 'cloud_deletion.%%' ORDER BY id")],
            ["cloud_deletion.requested", "cloud_deletion.deferred"])

        # The lock ended: the same consent deletes the exact Current.
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET created_at=created_at - interval '31 days' WHERE id=%s",
                               (current["id"],))
            connection.execute("UPDATE jobs SET run_after=now() - interval '1 second' WHERE id=%s", (job_id,))
        self.provider.age(self.key, current["provider_version_id"], DAYS + 1)
        self.run_purge(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed", self.job(job_id)["message"])
        self.assertEqual(self.provider.deleted, [(self.key, current["provider_version_id"])])

    def test_a_refusal_the_estimate_missed_is_deferred_to_the_observed_date(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        [job_id] = scheduled.job_ids
        # The estimate says ended, but the version's retention was extended.
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET created_at=created_at - interval '31 days' WHERE id=%s",
                               (current["id"],))
        self.provider.age(self.key, current["provider_version_id"], DAYS + 1)
        self.provider.days = DAYS * 3
        self.run_purge(job_id)
        job = self.job(job_id)
        observed = self.provider.until(self.key, current["provider_version_id"])
        self.assertEqual(job["status"], "pending_delay", job["message"])
        self.assertEqual(job["message_params"], {"until": observed.date().isoformat()})
        self.assertEqual(job["run_after"], observed)
        self.assertEqual(self.provider.retention_reads, 1)
        self.assertIn((self.key, current["provider_version_id"]), self.provider.objects)


class DeferredVaultDeletionTests(RetentionLockFixture):
    def test_only_locked_versions_left_wait_as_pending_then_the_vault_goes(self) -> None:
        current = self.upload(A)
        with self.db() as connection:
            protection_stop.stop_protection(connection, vault_id=2, actor="owner",
                                            cloud_disposition="keep", confirmation="Docs",
                                            reason="archive it")
            target = vault_deletion.deletion_target(connection, 2)
        self.assertEqual(target["retention_lock_days"], DAYS)
        summary = vault_deletion.preview(self.provider, target)
        self.assertEqual((summary["retention_lock"]["locked_count"],
                          summary["retention_lock"]["locked_bytes"]), (1, len(A)))
        with self.db() as connection:
            vault_deletion.request_deletion(connection, vault_id=2, actor="owner", confirmation="Docs",
                                            reason="test vault no longer needed", preview_summary=summary,
                                            requested_at=PAST)
        self.assertEqual(vault_deletion.run_due(self.db, lambda: self.provider), 0)
        [row] = self.rows("SELECT * FROM vault_deletions WHERE vault_id=2")
        until = self.provider.until(self.key, current["provider_version_id"])
        self.assertEqual((row["status"], row["retained_count"], row["retained_until"]), ("pending", 1, until))
        self.assertGreaterEqual(row["run_after"], datetime.now(timezone.utc) + timedelta(hours=23))
        self.assertNotIn(vault_deletion.PROBLEM_EVENT, self.open_problems())
        self.assertEqual(self.provider.deleted, [])
        with self.db() as connection:
            state = vault_deletion.status(connection, 2)
        self.assertEqual((state["request"]["retained_count"], state["request"]["cancellable"],
                          state["request"]["retryable"]), (1, True, False))
        self.assertEqual([r["event"] for r in self.rows(
            "SELECT event FROM audit_events WHERE event LIKE 'folder_protection.vault_deletion_%%' ORDER BY id")][-1],
            "folder_protection.vault_deletion_deferred")

        # The lock ended: the next run deletes it and the Vault leaves the catalog.
        self.provider.age(self.key, current["provider_version_id"], DAYS + 1)
        with self.db() as connection:
            connection.execute("UPDATE vault_deletions SET run_after=now() - interval '1 second' WHERE vault_id=2")
        self.assertEqual(vault_deletion.run_due(self.db, lambda: self.provider), 1)
        self.assertEqual(self.rows("SELECT id FROM vaults WHERE id=2"), [])
        self.assertIn((self.key, current["provider_version_id"]), self.provider.deleted)


class VaultBucketDeletionTests(RetentionLockFixture):
    """#739: the empty Vault Bucket is deleted only on request, tagged with its Vault."""

    def request(self, *, delete_bucket: bool) -> None:
        with self.db() as connection:
            protection_stop.stop_protection(connection, vault_id=2, actor="owner",
                                            cloud_disposition="keep", confirmation="Docs",
                                            reason="archive it")
            target = vault_deletion.deletion_target(connection, 2)
        summary = vault_deletion.preview(self.provider, target)
        with self.db() as connection:
            vault_deletion.request_deletion(connection, vault_id=2, actor="owner", confirmation="Docs",
                                            reason="test vault no longer needed", preview_summary=summary,
                                            requested_at=PAST, delete_bucket=delete_bucket)

    def due_now(self) -> None:
        with self.db() as connection:
            connection.execute("UPDATE vault_deletions SET run_after=now() - interval '1 second' WHERE vault_id=2")

    def deleted_audit(self) -> dict:
        [audit] = self.rows("SELECT detail FROM audit_events WHERE event='folder_protection.vault_deleted'")
        return audit["detail"]

    def test_a_bucket_tagged_with_another_vault_is_never_touched(self) -> None:
        current = self.upload(A)
        self.provider.age(self.key, current["provider_version_id"], DAYS + 1)
        self.provider.bucket_tags = {TAG_VAULT: "00000000-0000-4000-8000-0000000000aa"}
        self.request(delete_bucket=True)
        self.assertEqual(vault_deletion.run_due(self.db, lambda: self.provider), 0)
        [row] = self.rows("SELECT * FROM vault_deletions WHERE vault_id=2")
        self.assertEqual((row["status"], row["failure"]["reason"]), ("failed", "bucket_identity"))
        self.assertEqual((self.provider.deleted, self.provider.deleted_buckets), ([], []))
        self.assertIn((self.key, current["provider_version_id"]), self.provider.objects)

    def test_a_bucket_with_locked_versions_is_not_deleted_until_they_are_gone(self) -> None:
        current = self.upload(A)
        self.request(delete_bucket=True)
        self.assertEqual(vault_deletion.run_due(self.db, lambda: self.provider), 0)
        [row] = self.rows("SELECT * FROM vault_deletions WHERE vault_id=2")
        self.assertEqual((row["status"], row["delete_bucket"], row["bucket_deleted_at"]), ("pending", True, None))
        self.assertEqual(self.provider.deleted_buckets, [], "not empty: never deleted")
        with self.db() as connection:
            self.assertEqual(vault_deletion.status(connection, 2)["request"]["delete_bucket"], True)

        self.provider.age(self.key, current["provider_version_id"], DAYS + 1)
        self.due_now()
        self.assertEqual(vault_deletion.run_due(self.db, lambda: self.provider), 1)
        self.assertEqual(self.provider.deleted_buckets, [BUCKET])
        self.assertEqual(self.deleted_audit()["bucket"], "deleted")
        self.assertEqual(self.rows("SELECT id FROM vaults WHERE id=2"), [])

    def test_an_unticked_bucket_stays_and_the_audit_says_so(self) -> None:
        current = self.upload(A)
        self.provider.age(self.key, current["provider_version_id"], DAYS + 1)
        self.request(delete_bucket=False)
        with self.db() as connection:
            self.assertEqual(vault_deletion.status(connection, 2)["request"]["delete_bucket"], False)
        self.assertEqual(vault_deletion.run_due(self.db, lambda: self.provider), 1)
        self.assertEqual(self.provider.deleted_buckets, [])
        self.assertEqual(self.deleted_audit()["bucket"], "kept")

    def test_a_deleted_bucket_is_never_read_again(self) -> None:
        self.request(delete_bucket=True)
        now = datetime.now(timezone.utc)
        with self.db() as connection:
            self.assertIn(2, [v["id"] for v in bucket_metrics.due_vaults(connection, now=now)])
            connection.execute("UPDATE vault_deletions SET bucket_deleted_at=now() WHERE vault_id=2")
            self.assertNotIn(2, [v["id"] for v in bucket_metrics.due_vaults(connection, now=now)])
        with self.db() as connection, self.assertRaises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE vault_deletions SET delete_bucket=false WHERE vault_id=2")
        # A run that stopped after DeleteBucket only removes the catalog.
        self.due_now()
        self.provider.bucket_tags = {}
        self.assertEqual(vault_deletion.run_due(self.db, lambda: self.provider), 1)
        self.assertEqual(self.provider.deleted, [])
        self.assertEqual(self.deleted_audit()["bucket"], "deleted")


class RetentionLockApiTests(RetentionLockFixture):
    def test_the_status_endpoint_and_the_vault_inventory_name_the_lock(self) -> None:
        from app import main
        from app.services.vaults import list_vault_inventory

        state = main.vault_retention_lock(2, {})
        self.assertEqual((state["vault_id"], state["days"], state["mode"], state["retained"]["count"]),
                         (2, DAYS, "GOVERNANCE", 0))
        with self.db() as connection:
            seed_vault(connection, vault_id=3, source_root=str(self.root / "other"))
        self.assertEqual(main.vault_retention_lock(3, {}),
                         {"vault_id": 3, "days": None, "mode": None, "retained": None})
        with self.db() as connection:
            items = {item["id"]: item for item in list_vault_inventory(connection)}
        self.assertEqual((items[2]["retention_lock_days"], items[3]["retention_lock_days"]), (DAYS, None))


if __name__ == "__main__":
    unittest.main()
