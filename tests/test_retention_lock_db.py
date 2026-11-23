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

#749: the sweep sends one DeleteObjects per batch with no connection held,
skips a key being written, shows and retries failures and opens a Problem
after three; an unreadable retention waits an hour with a Problem; a refusal
within the clock skew is the lock; the audit and a restore Reconnect adopt
superseded locked versions no table names (dated by LastModified, #743); a
new Vault Deletion run forgets the previous locked versions; partly deferred,
crashed and automatic groups; a legacy Vault makes no extra S3 call.
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
from app.services.vault_paths import TAG_VAULT
from app.services.vaults import VAULT_ROW_SQL
from tests import test_cloud_purge, test_duplicate_cleanup
from tests import test_upload_worker as worker
from tests.fixtures import seed_vault
from tests.test_upload_worker import A, B
from tests.upload_provenance_provider import VersionedProvider

BUCKET = "frostvault-docs-ab12cd34"
DAYS = 30
PAST = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()


def locked_seed(connection, **columns) -> dict:
    """The Vault of the upload fixture, in a Vault Bucket with a Retention Lock."""
    return seed_vault(connection, bucket=BUCKET,
                      target={"key_layout": "bucket_root", "retention_lock_days": DAYS}, **columns)


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
        # #749: S3 refuses GetObjectRetention; DeleteObjects errors by key;
        # the size of every DeleteObjects request.
        self.retention_denied = False
        self.delete_errors: dict[str, str] = {}
        self.batches: list[int] = []

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
        self.batches.append(len(Delete["Objects"]))
        failing = [o for o in Delete["Objects"] if o["Key"] in self.delete_errors]
        refused = [o for o in Delete["Objects"]
                   if o not in failing and self.locked(o["Key"], o["VersionId"])]
        allowed = [o for o in Delete["Objects"] if o not in refused and o not in failing]
        response = super().delete_objects(Bucket=Bucket, Delete={**Delete, "Objects": allowed}) \
            if allowed else {}
        for item in allowed:
            self.markers.pop((item["Key"], item["VersionId"]), None)
        if refused or failing:
            response = {**(response or {}), "Errors": [
                {"Key": o["Key"], "VersionId": o["VersionId"], "Code": "AccessDenied",
                 "Message": "provider text"} for o in refused] + [
                {"Key": o["Key"], "VersionId": o["VersionId"], "Code": self.delete_errors[o["Key"]],
                 "Message": "provider text"} for o in failing]}
        return response

    def get_object_retention(self, *, Bucket, Key, VersionId=None):
        self.retention_reads += 1
        if self.retention_denied:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "provider text"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObjectRetention")
        if VersionId is None:  # the permission probe of an absent key (#749)
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "provider text"},
                               "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObjectRetention")
        if (Key, VersionId) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchVersion", "Message": "provider text"},
                               "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObjectRetention")
        return {"Retention": {"Mode": "GOVERNANCE", "RetainUntilDate": self.until(Key, VersionId)}}


class RetentionLockFixture(worker.UploadWorkerCase):
    cloud_only = test_cloud_purge.CloudPurgeTests.cloud_only
    schedule = test_cloud_purge.CloudPurgeTests.schedule
    claim_group = test_cloud_purge.CloudPurgeTests.claim_group
    run_purge = test_cloud_purge.CloudPurgeTests.run_purge

    def setUp(self) -> None:
        with patch("tests.test_upload_worker.VersionedProvider", LockedProvider), \
                patch("tests.test_upload_worker.seed_vault", locked_seed):
            super().setUp()
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
            seed_vault(connection, id=3, source_root=str(self.root / "other"))
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
        from app.routes.vaults import vault_retention_lock
        from app.services.vaults import list_vault_inventory

        state = vault_retention_lock(2, {})
        self.assertEqual((state["vault_id"], state["days"], state["mode"], state["retained"]["count"]),
                         (2, DAYS, "GOVERNANCE", 0))
        with self.db() as connection:
            seed_vault(connection, id=3, source_root=str(self.root / "other"))
        self.assertEqual(vault_retention_lock(3, {}),
                         {"vault_id": 3, "days": None, "mode": None, "retained": None})
        with self.db() as connection:
            items = {item["id"]: item for item in list_vault_inventory(connection)}
        self.assertEqual((items[2]["retention_lock_days"], items[3]["retention_lock_days"]), (DAYS, None))



def open_attempt(connection, file_id) -> None:
    """An unsettled upload attempt of ``file_id``: a write in progress at its key."""
    snapshot = connection.execute(
        "SELECT id FROM upload_snapshots WHERE file_id=%s AND closed_at IS NULL", (file_id,)).fetchone()
    if snapshot is None:
        digest = b"\x01" * 32
        snapshot = connection.execute(
            """INSERT INTO upload_snapshots(vault_id, file_id, path, path_since, source_root, provider,
                   local_generation, size, mtime_ns, plaintext_sha256, part_size, part_sha256, verification)
               SELECT vault_id, id, path, path_since, '/source', 's3', 0, 1, 1, %s, 8388608,
                      ARRAY[%s::bytea], 's3_checksum'
               FROM files WHERE id=%s RETURNING id""", (digest, digest, file_id)).fetchone()
    connection.execute(
        """INSERT INTO upload_attempts(snapshot_id, file_id, inventory_versions, inventory_markers)
           VALUES (%s, %s, '{}', '{}')""", (snapshot["id"], file_id))


class SweepTests(RetentionLockFixture):
    """#749 S2/S3/S4: batches, failures and unreadable retentions of the sweep."""

    def superseded(self, count: int = 1) -> list[dict]:
        """``count`` superseded versions of report.txt, handed over to the sweep."""
        versions = [self.upload(A)]
        for n in range(count):
            versions.append(self.upload(B + str(n).encode()))
        self.assertEqual(len(self.retained()), count)
        return versions[:-1]

    def after_the_lock(self) -> datetime:
        later = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=DAYS + 1)
        self.provider.clock = lambda: later
        return later

    def sweep(self, at: datetime) -> dict[str, int]:
        return retention_lock.run_due(self.db, lambda: self.provider, now=at)

    def test_due_versions_go_in_one_request_with_no_connection_held(self) -> None:
        old = self.superseded(2)
        later = self.after_the_lock()
        send = self.provider.delete_objects
        free = []

        def delete_objects(**kwargs):
            # No sweep transaction holds the rows while S3 answers.
            with self.db() as connection:
                connection.execute("SELECT id FROM retained_versions FOR UPDATE NOWAIT").fetchall()
            free.append(True)
            return send(**kwargs)
        self.provider.delete_objects = delete_objects
        self.assertEqual(self.sweep(later), {"deleted": 2})
        self.assertEqual((self.provider.batches, free), ([2], [True]))
        for version in old:
            self.assertIn((self.key, version["provider_version_id"]), self.provider.deleted)
        self.assertEqual(self.retained(), [])

    def test_batches_follow_each_other_until_nothing_is_due(self) -> None:
        self.superseded(3)
        later = self.after_the_lock()
        self.assertEqual(retention_lock.run_due(self.db, lambda: self.provider, now=later, limit=2),
                         {"deleted": 3})
        self.assertEqual(self.provider.batches, [2, 1])

    def test_a_key_with_an_open_upload_attempt_waits(self) -> None:
        [old] = self.superseded()
        later = self.after_the_lock()
        with self.db() as connection:
            open_attempt(connection, self.file_id)
        self.assertEqual(self.sweep(later), {})
        self.assertEqual(self.provider.batches, [])
        with self.db() as connection:
            connection.execute("UPDATE upload_attempts SET settled_at=now(), outcome='unsent' "
                               "WHERE settled_at IS NULL")
        self.assertEqual(self.sweep(later), {"deleted": 1})
        self.assertIn((self.key, old["provider_version_id"]), self.provider.deleted)

    def test_failures_are_visible_retried_and_a_problem_after_three(self) -> None:
        [old] = self.superseded()
        start = self.after_the_lock()
        self.provider.delete_errors[self.key] = "InternalError"
        for attempt in range(1, 4):
            at = start + timedelta(hours=attempt - 1)
            self.assertEqual(self.sweep(at), {"failed": 1}, attempt)
            [row] = self.retained()
            # Transient: retried an hour later, never before.
            self.assertEqual((row["failures"], row["last_error"], row["next_attempt_at"]),
                             (attempt, "InternalError", at + timedelta(hours=1)))
            self.assertEqual(self.sweep(at + timedelta(minutes=30)), {})
            with self.db() as connection:
                state = retention_lock.status(connection, 2)["retained"]
            self.assertEqual((state["failed_count"], state["failed_attempts"], state["last_error"]),
                             (1, attempt, "InternalError"))
            opened = retention_lock.SWEEP_FAILED_PROBLEM in self.open_problems()
            self.assertEqual(opened, attempt >= retention_lock.FAILURES_BEFORE_PROBLEM, attempt)

        # Any other error waits a day.
        self.provider.delete_errors[self.key] = "MethodNotAllowed"
        at = start + timedelta(hours=3)
        self.assertEqual(self.sweep(at), {"failed": 1})
        [row] = self.retained()
        self.assertEqual((row["failures"], row["next_attempt_at"]), (4, at + timedelta(days=1)))

        # S3 deletes it: the row goes and the Problem closes.
        self.provider.delete_errors.clear()
        self.assertEqual(self.sweep(at + timedelta(days=1)), {"deleted": 1})
        self.assertEqual(self.retained(), [])
        self.assertIn((self.key, old["provider_version_id"]), self.provider.deleted)
        self.assertNotIn(retention_lock.SWEEP_FAILED_PROBLEM, self.open_problems())

    def test_an_unreadable_retention_waits_an_hour_with_a_problem(self) -> None:
        [old] = self.superseded()
        later = self.after_the_lock()
        [before] = self.retained()
        self.provider.days = DAYS * 3      # still locked: S3 refuses the DELETE
        self.provider.retention_denied = True
        self.assertEqual(self.sweep(later), {"unreadable": 1})
        [row] = self.retained()
        self.assertEqual((row["retain_until"], row["failures"], row["next_attempt_at"]),
                         (before["retain_until"], 0, later + timedelta(hours=1)))
        self.assertIn(retention_lock.UNREADABLE_PROBLEM, self.open_problems())

        # The permission is back: the next sweep's probe closes the Problem.
        self.provider.retention_denied = False
        self.assertEqual(self.sweep(later), {})
        self.assertNotIn(retention_lock.UNREADABLE_PROBLEM, self.open_problems())
        self.assertEqual(self.sweep(later + timedelta(hours=1)), {"deferred": 1})
        [moved] = self.retained()
        self.assertEqual(moved["retain_until"], self.provider.until(self.key, old["provider_version_id"]))

    def test_a_refusal_just_after_the_date_is_the_lock_retried_in_an_hour(self) -> None:
        [old] = self.superseded()
        until = self.provider.until(self.key, old["provider_version_id"])
        moment = until + timedelta(minutes=2)
        # The bucket's clock is a little behind: S3 still refuses.
        self.provider.clock = lambda: until - timedelta(seconds=1)
        with self.db() as connection:
            connection.execute("UPDATE retained_versions SET retain_until=%s", (until,))
        self.assertEqual(self.sweep(moment), {"deferred": 1})
        [row] = self.retained()
        self.assertEqual((row["retain_until"], row["failures"]), (moment + timedelta(hours=1), 0))


class AdoptionTests(RetentionLockFixture):
    """#749 S1/#743: superseded locked versions no table names any more."""

    def test_the_audit_adopts_a_superseded_version_the_catalog_lost(self) -> None:
        from app.services.catalog_audit import audit_vault_catalog

        first = self.upload(A)
        self.upload(B)
        # The catalog was restored from a backup older than the replacement.
        with self.db() as connection:
            connection.execute("DELETE FROM retained_versions")
        # Versions FrostVault never wrote at a key no Vault File names.
        self.provider.add("stranger.txt", b"x")
        self.provider.add("stranger.txt", b"y")
        with self.db() as connection:
            report = audit_vault_catalog(connection, self.vault(), self.provider)
        self.assertEqual((report["missing_in_catalog"], report["missing_in_cloud"]), (2, 0))
        [row] = self.retained()
        old = (self.key, first["provider_version_id"])
        self.assertEqual((row["object_key"], row["provider_version_id"]), old)
        self.assertEqual(row["retain_until"], self.provider.until(*old))
        self.assertEqual(self.provider.deleted, [], "database writes only")
        [event] = self.rows("SELECT detail FROM audit_events WHERE event='retention_lock.retained_adopted'")
        self.assertEqual(event["detail"]["adopted"], 1)
        # Audited again: adopted once.
        with self.db() as connection:
            audit_vault_catalog(connection, self.vault(), self.provider)
        self.assertEqual(len(self.retained()), 1)

    def test_a_key_with_a_delete_marker_or_an_open_attempt_is_not_adopted(self) -> None:
        from app.services.catalog_audit import audit_vault_catalog

        self.upload(A)
        self.upload(B)
        with self.db() as connection:
            connection.execute("DELETE FROM retained_versions")
            open_attempt(connection, self.file_id)
            audit_vault_catalog(connection, self.vault(), self.provider)
        self.assertEqual(self.retained(), [])
        with self.db() as connection:
            connection.execute("UPDATE upload_attempts SET settled_at=now(), outcome='unsent' "
                               "WHERE settled_at IS NULL")
        self.provider.markers[self.key, "marker-1"] = True
        with self.db() as connection:
            audit_vault_catalog(connection, self.vault(), self.provider)
        self.assertEqual(self.retained(), [])

    def test_a_restore_reconnect_adopts_and_dates_what_the_cloud_gained(self) -> None:
        from app.services.recovery_kit import record_metadata_restore

        first = self.upload(A)
        old = first["provider_version_id"]
        # After the backup: report.txt was replaced twice in the cloud.
        middle = self.provider.add(self.key, b"middle bytes")
        newest = self.provider.add(self.key, B)
        self.provider.age(self.key, old, 9)
        self.provider.age(self.key, middle, 6)
        self.provider.age(self.key, newest, 3)
        with self.db() as connection:
            record_metadata_restore(connection, (datetime.now(timezone.utc) - timedelta(days=8)).isoformat())
        storage.scan_cloud(self.vault(), storage.now_iso(), reconnect=True)

        current = self.version(self.file_row()["current_version_id"])
        self.assertEqual(current["provider_version_id"], newest)
        # #743: dated by its LastModified, Class Entry left unrecorded.
        self.assertEqual(current["created_at"], self.provider.written[self.key, newest])
        self.assertIsNone(current["storage_class_since"])
        # S1: the version written and superseded after the backup is retained.
        [row] = self.retained()
        self.assertEqual((row["object_key"], row["provider_version_id"], row["retain_until"]),
                         (self.key, middle, self.provider.until(self.key, middle)))
        self.assertEqual(self.provider.deleted, [])


class ClaimClearsRetainedTests(RetentionLockFixture):
    def test_a_new_vault_deletion_run_forgets_the_previous_locked_versions(self) -> None:
        self.upload(A)
        with self.db() as connection:
            protection_stop.stop_protection(connection, vault_id=2, actor="owner",
                                            cloud_disposition="keep", confirmation="Docs",
                                            reason="archive it")
            target = vault_deletion.deletion_target(connection, 2)
        summary = vault_deletion.preview(self.provider, target)
        with self.db() as connection:
            vault_deletion.request_deletion(connection, vault_id=2, actor="owner", confirmation="Docs",
                                            reason="test vault no longer needed", preview_summary=summary,
                                            requested_at=PAST)
        self.assertEqual(vault_deletion.run_due(self.db, lambda: self.provider), 0)
        [row] = self.rows("SELECT retained_count FROM vault_deletions WHERE vault_id=2")
        self.assertEqual(row["retained_count"], 1)
        with self.db() as connection:
            claimed = vault_deletion.claim_due(
                connection, now=datetime.now(timezone.utc) + timedelta(days=DAYS + 1))
        self.assertEqual(claimed, [2])
        [row] = self.rows("SELECT status, retained_count, retained_until FROM vault_deletions WHERE vault_id=2")
        self.assertEqual((row["status"], row["retained_count"], row["retained_until"]), ("running", None, None))


class DeferredGroupTests(RetentionLockFixture):
    """#749 S8/S9: groups whose members a Retention Lock defers."""

    second_cloud_only = test_cloud_purge.CloudPurgeTests.second_cloud_only
    stop = test_cloud_purge.CloudPurgeTests.stop
    expire_group = test_cloud_purge.CloudPurgeTests.expire_group

    def unlocked_other(self) -> dict:
        """other.txt, Cloud-only, written long enough ago that its lock ended."""
        file_id = self.second_cloud_only("other.txt")
        [current] = self.rows(
            """SELECT v.* FROM files f JOIN archive_versions v ON v.id=f.current_version_id
               WHERE f.id=%s""", file_id)
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET created_at=created_at - interval '31 days' "
                               "WHERE id=%s", (current["id"],))
        self.provider.age("other.txt", current["provider_version_id"], DAYS + 1)
        return current

    def path_statuses(self, group_id: str) -> dict[str, str]:
        return {r["path"]: r["status"] for r in self.rows(
            """SELECT f.path, j.status FROM jobs j JOIN files f ON f.id=j.file_id
               WHERE j.group_id=%s""", group_id)}

    def due(self, group_id: str) -> None:
        with self.db() as connection:
            connection.execute(
                "UPDATE jobs SET requested_at=%s, run_after=now() - interval '1 hour' "
                "WHERE group_id=%s AND status='pending_delay'", (PAST, group_id))

    def test_a_partly_deferred_stop_deletion_can_still_be_cancelled(self) -> None:
        locked = self.cloud_only()
        other = self.unlocked_other()
        status = self.stop("delete")
        group_id = status["group_id"]
        self.due(group_id)
        self.run_purge(min(j["id"] for j in status["jobs"]))
        self.assertEqual(self.path_statuses(group_id), {"report.txt": "pending_delay", "other.txt": "completed"})
        self.assertIn(("other.txt", other["provider_version_id"]), self.provider.deleted)
        with self.db() as connection:
            status = protection_stop.status(connection, 2)
        self.assertTrue(status["cancellable"], "the deferred member can be cancelled")
        with self.db() as connection:
            status = protection_stop.cancel_deletion(connection, vault_id=2, actor="owner")
        self.assertEqual((status["cloud_disposition"], status["root_released"]), ("keep", True))
        self.assertEqual(self.path_statuses(group_id), {"report.txt": "cancelled", "other.txt": "completed"})
        self.assertIn((self.key, locked["provider_version_id"]), self.provider.objects)

    def test_a_crash_after_the_deletion_keeps_the_deferred_member_waiting(self) -> None:
        locked = self.cloud_only()
        other = self.unlocked_other()
        scheduled = self.schedule(paths=["report.txt", "other.txt"])
        self.due(scheduled.group_id)
        with patch("app.services.cloud_deletion.defer_locked",
                   side_effect=storage._PurgeUnsettled("worker stopped")):
            self.run_purge(scheduled.job_ids[0])
        self.assertEqual(self.path_statuses(scheduled.group_id)["other.txt"], "completed")
        self.expire_group(scheduled.group_id)
        storage.reconcile_interrupted_jobs()
        self.assertEqual(self.path_statuses(scheduled.group_id),
                         {"report.txt": "queued", "other.txt": "completed"})
        [waiting] = self.rows(
            """SELECT j.id FROM jobs j JOIN files f ON f.id=j.file_id
               WHERE j.group_id=%s AND f.path='report.txt'""", scheduled.group_id)
        self.run_purge(int(waiting["id"]))
        statuses = self.path_statuses(scheduled.group_id)
        self.assertEqual(statuses, {"report.txt": "pending_delay", "other.txt": "completed"})
        self.assertIn((self.key, locked["provider_version_id"]), self.provider.objects)
        self.assertEqual(self.provider.deleted, [("other.txt", other["provider_version_id"])])
        self.assertEqual(self.rows("SELECT id FROM cleanup_batches"), [], "no grant is left open")


class AutomaticGroupTests(RetentionLockFixture):
    """An automatic Duplicate Cleanup group with locked members waits, then runs."""

    put = test_duplicate_cleanup.DuplicateCleanupFixture.put
    lose = test_duplicate_cleanup.DuplicateCleanupFixture.lose
    cloud_only_at = test_duplicate_cleanup.DuplicateCleanupFixture.cloud_only_at
    age = test_duplicate_cleanup.DuplicateCleanupFixture.age
    moved = test_duplicate_cleanup.DuplicateCleanupFixture.moved
    configure = test_duplicate_cleanup.DuplicateCleanupFixture.configure
    run_cleanup = test_duplicate_cleanup.DuplicateCleanupFixture.run_cleanup
    current = test_duplicate_cleanup.DuplicateCleanupFixture.current
    path_statuses = test_duplicate_cleanup.DuplicateCleanupFixture.path_statuses

    def test_an_automatic_group_is_deferred_then_deleted(self) -> None:
        photo, video = self.moved("photo.jpg", A), self.moved("video.mp4", B)
        self.configure()
        outcome = self.run_cleanup(datetime.now(timezone.utc) - timedelta(days=8))
        group = outcome["group_id"]
        lead = self.rows("SELECT min(id) AS id FROM jobs WHERE group_id=%s", group)[0]["id"]
        self.run_purge(lead)
        self.assertEqual(self.path_statuses(group),
                         {"old/photo.jpg": "pending_delay", "old/video.mp4": "pending_delay"})
        self.assertEqual(self.provider.deleted, [])
        [request] = self.rows("SELECT automatic FROM cloud_purge_requests WHERE group_id=%s", group)
        self.assertTrue(request["automatic"])

        # The lock ended: the same automatic consent deletes both.
        versions = [self.current(photo), self.current(video)]
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET created_at=created_at - interval '31 days' "
                               "WHERE id = ANY(%s::uuid[])", ([str(v["id"]) for v in versions],))
            connection.execute("UPDATE jobs SET run_after=now() - interval '1 second' WHERE group_id=%s",
                               (group,))
        for version in versions:
            self.provider.age(version["object_key"], version["provider_version_id"], DAYS + 1)
        self.run_purge(lead)
        self.assertEqual(self.path_statuses(group),
                         {"old/photo.jpg": "completed", "old/video.mp4": "completed"})
        self.assertEqual(sorted(self.provider.deleted),
                         sorted((v["object_key"], v["provider_version_id"]) for v in versions))


class LegacyVaultTests(worker.UploadWorkerCase):
    """A shared-bucket Vault without a lock makes no extra S3 call (#749)."""

    upload = RetentionLockFixture.upload
    vault = RetentionLockFixture.vault

    def test_replacement_audit_and_sweep_are_unchanged(self) -> None:
        from app.services.catalog_audit import audit_vault_catalog

        calls: list[str] = []
        previous = self.provider.hook

        def hook(stage, **kwargs):
            calls.append(stage)
            previous(stage, **kwargs)
        self.provider.hook = hook
        first = self.upload(A)
        self.upload(B)
        # The superseded version is deleted at once, by exact VersionId.
        self.assertIn((self.key, first["provider_version_id"]), self.provider.deleted)
        self.assertFalse(hasattr(self.provider, "get_object_retention"), "no retention is ever read")
        self.assertIsNone(self.vault()["s3_retention_lock_days"])
        calls.clear()
        with self.db() as connection:
            report = audit_vault_catalog(connection, self.vault(), self.provider)
        self.assertEqual(report["missing_in_catalog"], 0)
        self.assertEqual(calls, ["list"], "one listing, nothing else")
        self.assertEqual(self.rows("SELECT id FROM retained_versions"), [])
        self.assertEqual(self.rows("SELECT id FROM audit_events WHERE event LIKE 'retention_lock.%%'"), [])

        def no_client():
            raise AssertionError("the sweep opens no S3 client without Retained Versions")
        self.assertEqual(retention_lock.run_due(self.db, no_client), {})


if __name__ == "__main__":
    unittest.main()
