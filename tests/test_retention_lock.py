"""Retention Lock (#736, ADR-0050): the pure parts, no database.

The estimate and observation of a version's RetainUntilDate, the preview
summaries, the Vault Deletion page loop that never sends a locked version and
explains a refused one, the creation API's choices, the Delete Marker Problem
and its link. Database paths: ``tests.test_retention_lock_db``.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from pydantic import ValidationError

from app.services import retention_lock, vault_deletion
from app.services.notification_links import notification_path

NOW = datetime(2027, 1, 10, 12, 0, tzinfo=timezone.utc)


def client_error(code: str, operation: str = "DeleteObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "provider text"},
                        "ResponseMetadata": {"HTTPStatusCode": 403}}, operation)


class LockedBucket:
    """A versioned bucket with a Governance default retention and no bypass.

    Every version is locked for ``days`` from its LastModified; a DELETE of a
    locked version is refused with AccessDenied, like S3 without
    ``s3:BypassGovernanceRetention``.
    """

    def __init__(self, days: int, now: datetime = NOW) -> None:
        self.days, self.now = days, now
        self.versions: dict[tuple[str, str], datetime] = {}
        self.markers: set[tuple[str, str]] = set()
        self.deleted: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.retention_reads: list[tuple[str, str]] = []

    def add(self, key: str, version: str, written: datetime) -> None:
        self.versions[key, version] = written

    def until(self, key: str, version: str) -> datetime:
        return self.versions[key, version] + timedelta(days=self.days)

    def list_object_versions(self, *, Bucket, Prefix, **kwargs):
        return {"Versions": [{"Key": k, "VersionId": v, "Size": 10, "StorageClass": "STANDARD",
                              "LastModified": written}
                             for (k, v), written in sorted(self.versions.items())],
                "DeleteMarkers": [{"Key": k, "VersionId": v} for k, v in sorted(self.markers)],
                "IsTruncated": False}

    def list_multipart_uploads(self, **kwargs):
        return {"Uploads": [], "IsTruncated": False}

    def delete_objects(self, *, Bucket, Delete):
        errors = []
        for item in Delete["Objects"]:
            identity = (item["Key"], item["VersionId"])
            self.sent.append(identity)
            if identity in self.versions and self.until(*identity) > self.now:
                errors.append({"Key": item["Key"], "VersionId": item["VersionId"],
                               "Code": "AccessDenied", "Message": "provider text"})
                continue
            self.versions.pop(identity, None)
            self.markers.discard(identity)
            self.deleted.append(identity)
        return {"Errors": errors} if errors else {}

    def get_object_retention(self, *, Bucket, Key, VersionId):
        self.retention_reads.append((Key, VersionId))
        if (Key, VersionId) not in self.versions:
            raise client_error("NoSuchVersion", "GetObjectRetention")
        return {"Retention": {"Mode": "GOVERNANCE", "RetainUntilDate": self.until(Key, VersionId)}}


class EstimateTests(unittest.TestCase):
    def test_only_the_offered_durations(self) -> None:
        self.assertEqual(retention_lock.CHOICES, (30, 90, 180, 365))

    def test_the_estimate_is_the_creation_plus_the_default_days(self) -> None:
        created = NOW - timedelta(days=10)
        self.assertEqual(retention_lock.estimated_until(created, 30), created + timedelta(days=30))
        self.assertEqual(retention_lock.locked_at(created, 30, NOW), created + timedelta(days=30))
        self.assertIsNone(retention_lock.locked_at(created, 10, NOW), "ended exactly now")
        self.assertIsNone(retention_lock.locked_at(created, None, NOW), "no Retention Lock")
        # A naive timestamp is UTC; an ISO string is accepted.
        self.assertEqual(retention_lock.estimated_until("2027-01-01T00:00:00", 30),
                         datetime(2027, 1, 31, tzinfo=timezone.utc))

    def test_the_summary_counts_what_is_still_locked_when_the_deletion_runs(self) -> None:
        rows = [{"created_at": NOW - timedelta(days=5), "size": 100},
                {"created_at": NOW - timedelta(days=20), "size": 50},
                {"created_at": NOW - timedelta(days=40), "size": 7}]
        at = NOW + timedelta(days=7)
        summary = retention_lock.summarize(rows, days=30, at=at)
        self.assertEqual(summary, {
            "days": 30, "mode": "GOVERNANCE", "locked_count": 2, "locked_bytes": 150,
            "locked_from": (NOW + timedelta(days=10)).isoformat(),
            "locked_until": (NOW + timedelta(days=25)).isoformat()})
        self.assertIsNone(retention_lock.summarize(rows, days=None, at=at))
        none = retention_lock.summarize([], days=30, at=at)
        self.assertEqual((none["locked_count"], none["locked_from"]), (0, None))


class ObservationTests(unittest.TestCase):
    def test_a_refused_delete_is_explained_by_the_observed_retention(self) -> None:
        bucket = LockedBucket(30)
        bucket.add("a.txt", "v1", NOW - timedelta(days=1))
        until = retention_lock.refused_until(bucket, bucket="b", key="a.txt", version_id="v1",
                                             code="AccessDenied", now=NOW)
        self.assertEqual(until, NOW + timedelta(days=29))

    def test_other_refusals_and_ended_locks_are_not_explained(self) -> None:
        bucket = LockedBucket(30)
        bucket.add("a.txt", "v1", NOW - timedelta(days=31))
        self.assertIsNone(retention_lock.refused_until(
            bucket, bucket="b", key="a.txt", version_id="v1", code="AccessDenied", now=NOW))
        self.assertIsNone(retention_lock.refused_until(
            bucket, bucket="b", key="a.txt", version_id="v1", code="SlowDown", now=NOW))
        self.assertEqual(bucket.retention_reads, [("a.txt", "v1")], "only an AccessDenied is read")
        with self.assertRaises(retention_lock.VersionGone):
            retention_lock.refused_until(bucket, bucket="b", key="gone", version_id="v9",
                                         code="AccessDenied", now=NOW)

    def test_a_version_without_retention_is_not_locked(self) -> None:
        class Unlocked:
            def get_object_retention(self, **kwargs):
                raise client_error("NoSuchObjectLockConfiguration", "GetObjectRetention")

        self.assertIsNone(retention_lock.observed_until(Unlocked(), bucket="b", key="k", version_id="v"))

    def test_a_failed_read_decides_nothing(self) -> None:
        class Broken:
            def get_object_retention(self, **kwargs):
                raise client_error("AccessDenied", "GetObjectRetention")

        with self.assertRaises(ClientError):
            retention_lock.observed_until(Broken(), bucket="b", key="k", version_id="v")


class VaultDeletionPageTests(unittest.TestCase):
    """A deletion of a locked Vault Bucket sends no locked version and fails on none."""

    def setUp(self) -> None:
        self.bucket = LockedBucket(30, now=datetime.now(timezone.utc))
        moment = self.bucket.now
        self.bucket.add("old.txt", "v-old", moment - timedelta(days=40))
        self.bucket.add("new.txt", "v-new", moment - timedelta(days=2))
        self.bucket.markers.add(("gone.txt", "m-1"))
        self.enterContext(patch.object(vault_deletion, "_heartbeat"))

    def run_prefix(self, days: int | None) -> vault_deletion._Progress:
        progress = vault_deletion._Progress()
        vault_deletion._delete_prefix(self.bucket, lambda: None, vault_id=2, bucket="b", prefix="",
                                      progress=progress, retention_lock_days=days)
        return progress

    def test_a_version_still_locked_by_its_estimate_is_never_sent(self) -> None:
        progress = self.run_prefix(30)
        self.assertNotIn(("new.txt", "v-new"), self.bucket.sent)
        self.assertEqual(sorted(self.bucket.deleted), [("gone.txt", "m-1"), ("old.txt", "v-old")])
        self.assertEqual((progress.versions, progress.markers), (1, 1))
        self.assertEqual(set(progress.locked), {("new.txt", "v-new")})
        leftover = vault_deletion.list_prefix(self.bucket, bucket="b", prefix="")
        self.assertTrue(vault_deletion._only_locked(leftover, progress))

    def test_a_refused_version_waits_for_its_observed_retention(self) -> None:
        # The estimate says ended (written 40 days ago) but someone extended
        # the retention outside FrostVault: S3 refuses, the observation explains.
        self.bucket.days = 60
        progress = self.run_prefix(30)
        self.assertIn(("old.txt", "v-old"), self.bucket.sent)
        self.assertEqual(self.bucket.retention_reads, [("old.txt", "v-old")])
        self.assertEqual(progress.locked[("old.txt", "v-old")], self.bucket.until("old.txt", "v-old"))
        self.assertEqual(progress.versions, 0, "nothing counted as deleted")

    def test_without_a_lock_a_refusal_is_a_failure(self) -> None:
        with self.assertRaises(vault_deletion.VaultDeletionError):
            self.run_prefix(None)
        self.assertEqual(self.bucket.retention_reads, [], "no retention is read without a lock")

    def test_a_leftover_that_is_not_locked_is_still_a_failure(self) -> None:
        progress = self.run_prefix(30)
        self.bucket.markers.add(("late.txt", "m-2"))
        leftover = vault_deletion.list_prefix(self.bucket, bucket="b", prefix="")
        self.assertFalse(vault_deletion._only_locked(leftover, progress))
        self.assertFalse(vault_deletion._only_locked(leftover, vault_deletion._Progress()))

    def test_the_preview_counts_the_versions_still_locked_at_the_earliest_execution(self) -> None:
        inventory = vault_deletion.list_prefix(self.bucket, bucket="b", prefix="")
        executes = self.bucket.now + timedelta(days=7)
        summary = vault_deletion.summarize(inventory, executes_at=executes, retention_lock_days=30)
        lock = summary["retention_lock"]
        self.assertEqual((lock["days"], lock["locked_count"], lock["locked_bytes"]), (30, 1, 10))
        self.assertEqual(lock["locked_until"], self.bucket.until("new.txt", "v-new").isoformat())
        self.assertIsNone(vault_deletion.summarize(inventory, executes_at=executes)["retention_lock"])


class CreationApiTests(unittest.TestCase):
    def test_only_the_offered_durations_or_none(self) -> None:
        from app.main import VaultSelfServiceCreate

        self.assertIsNone(VaultSelfServiceCreate(name="Photos").retention_lock_days)
        for days in (30, 90, 180, 365):
            self.assertEqual(VaultSelfServiceCreate(name="Photos", retention_lock_days=days)
                             .retention_lock_days, days)
        for wrong in (0, 45, 3650, "180d"):
            with self.subTest(days=wrong), self.assertRaises(ValidationError):
                VaultSelfServiceCreate(name="Photos", retention_lock_days=wrong)
        # No mode to choose: Governance only, never Compliance.
        with self.assertRaises(ValidationError):
            VaultSelfServiceCreate(name="Photos", retention_lock_days=180, retention_lock_mode="COMPLIANCE")

    def test_the_service_refuses_other_durations_before_anything_is_saved(self) -> None:
        from app.services import vaults
        from app.services.bucket_provisioning import RetentionLock

        with patch.object(vaults.vault_buckets, "client_for_region") as client, \
                self.assertRaises(vaults.InvalidVaultName):
            vaults.create_vault("Photos", retention_lock=RetentionLock(days=45))
        client.assert_not_called()


class DeleteMarkerProblemTests(unittest.TestCase):
    def test_a_marker_on_a_locked_bucket_opens_a_problem_and_none_closes_it(self) -> None:
        vault = {"id": 2, "name": "Photos", "s3_bucket": "frostvault-photos-ab12cd34",
                 "s3_retention_lock_days": 90}
        with patch("app.services.notifications.set_problem") as set_problem:
            self.assertEqual(retention_lock.report_delete_markers(
                SimpleNamespace(), vault, {("b.txt", "m2"), ("a.txt", "m1")}), 2)
            self.assertEqual(retention_lock.report_delete_markers(SimpleNamespace(), vault, set()), 0)
            self.assertEqual(retention_lock.report_delete_markers(
                SimpleNamespace(), {**vault, "s3_retention_lock_days": None}, {("a.txt", "m1")}), 0)
        opened, closed = set_problem.call_args_list
        self.assertEqual(opened.kwargs["event"], retention_lock.DELETE_MARKER_PROBLEM)
        self.assertEqual((opened.kwargs["active"], opened.kwargs["params"]["count"],
                          opened.kwargs["params"]["first_key"]), (True, 2, "a.txt"))
        self.assertFalse(closed.kwargs["active"])
        self.assertEqual(set_problem.call_count, 2, "a Vault without a lock opens nothing")

    def test_the_problem_links_to_the_vault_settings(self) -> None:
        self.assertEqual(notification_path(2, {"retention_lock": True, "first_key": "a.txt"}),
                         "/?vault_id=2&settings=1")

    def test_the_messages_exist_in_both_languages(self) -> None:
        from app.i18n import has_message, translate

        for locale in ("en", "it"):
            for key in ("notification.retention_lock_delete_marker.title",
                        "notification.retention_lock_delete_marker.body",
                        "job.cloud_purge_deferred"):
                self.assertTrue(has_message(key, locale), (key, locale))
        self.assertIn("eliminazione possibile dal 2027-03-01",
                      translate("job.cloud_purge_deferred", "it", until="2027-03-01"))
        self.assertIn("deletable from 2027-03-01",
                      translate("job.cloud_purge_deferred", "en", until="2027-03-01"))

    def test_the_new_problems_exist_in_both_languages_and_link_to_the_settings(self) -> None:
        from app.i18n import has_message

        for locale in ("en", "it"):
            for key in ("notification.retention_lock_sweep_failed.title",
                        "notification.retention_lock_sweep_failed.body_one",
                        "notification.retention_lock_sweep_failed.body_other",
                        "notification.retention_lock_unreadable.title",
                        "notification.retention_lock_unreadable.body"):
                self.assertTrue(has_message(key, locale), (key, locale))
        self.assertEqual(notification_path(2, {"retention_lock": True, "vault": "Docs"}),
                         "/?vault_id=2&settings=1")

    def test_a_deferred_job_shows_its_date_in_the_reader_language(self) -> None:
        from app.i18n import present_job_message

        row = {"message_key": "job.cloud_purge_deferred", "message_params": {"until": "2027-03-01"}}
        self.assertIn("deletable from 1 Mar 2027", present_job_message(row, "en"))
        self.assertIn("eliminazione possibile dal 1 mar 2027", present_job_message(row, "it"))


class SkewAndUnreadableTests(unittest.TestCase):
    """#749 S4/S5: a refusal near the date and a retention S3 refuses to read."""

    def test_a_refusal_just_after_the_date_is_still_the_lock_and_waits_an_hour(self) -> None:
        bucket = LockedBucket(30)
        bucket.add("ended-2m.txt", "v1", NOW - timedelta(days=30, minutes=2))
        bucket.add("ended-6m.txt", "v2", NOW - timedelta(days=30, minutes=6))
        bucket.add("ahead-10m.txt", "v3", NOW - timedelta(days=30) + timedelta(minutes=10))

        def until(key: str, version: str):
            return retention_lock.refused_until(bucket, bucket="b", key=key, version_id=version,
                                                code="AccessDenied", now=NOW)

        # Within the 5 minutes of clock skew the refusal is the lock, retried in an hour.
        self.assertEqual(until("ended-2m.txt", "v1"), NOW + timedelta(hours=1))
        self.assertEqual(until("ahead-10m.txt", "v3"), NOW + timedelta(hours=1))
        # Beyond it the lock does not explain the refusal.
        self.assertIsNone(until("ended-6m.txt", "v2"))

    def test_an_unreadable_retention_defers_an_hour(self) -> None:
        class Denied(LockedBucket):
            def get_object_retention(self, **kwargs):
                raise client_error("AccessDenied", "GetObjectRetention")

        with self.assertRaises(retention_lock.RetentionUnreadable) as caught:
            retention_lock.refused_until(Denied(30), bucket="b", key="a.txt", version_id="v1",
                                         code="AccessDenied", now=NOW)
        self.assertEqual(caught.exception.until, NOW + timedelta(hours=1))

    def test_the_probe_reads_a_missing_key_and_names_a_refusal(self) -> None:
        class Probe:
            def __init__(self, code: str) -> None:
                self.code, self.calls = code, []

            def get_object_retention(self, **kwargs):
                self.calls.append(kwargs)
                raise client_error(self.code, "GetObjectRetention")

        allowed = Probe("NoSuchKey")
        self.assertIsNone(retention_lock.probe_retention_read(allowed, "b"))
        self.assertEqual(allowed.calls, [{"Bucket": "b", "Key": retention_lock.PROBE_KEY}])
        self.assertEqual(retention_lock.probe_retention_read(Probe("AccessDenied"), "b"), "AccessDenied")

    def test_a_vault_deletion_records_an_unreadable_version_an_hour_ahead(self) -> None:
        class Denied(LockedBucket):
            def get_object_retention(self, **kwargs):
                raise client_error("AccessDenied", "GetObjectRetention")

        moment = datetime.now(timezone.utc)
        bucket = Denied(60, now=moment)
        bucket.add("old.txt", "v-old", moment - timedelta(days=40))  # estimate ended, S3 refuses
        progress = vault_deletion._Progress()
        with patch.object(vault_deletion, "_heartbeat"):
            vault_deletion._delete_prefix(bucket, lambda: None, vault_id=2, bucket="b", prefix="",
                                          progress=progress, retention_lock_days=30)
        self.assertTrue(progress.unreadable)
        until = progress.locked[("old.txt", "v-old")]
        self.assertGreaterEqual(until, moment + timedelta(hours=1))
        self.assertLess(until, moment + timedelta(hours=2))


class SweepBatchTests(unittest.TestCase):
    """#749 S2/S3: one DeleteObjects per batch and an outcome for each row."""

    @staticmethod
    def rows(*identities: tuple[str, str]) -> list[dict]:
        return [{"id": f"r{n}", "object_key": key, "provider_version_id": version}
                for n, (key, version) in enumerate(identities, 1)]

    def test_one_request_deletes_the_ended_defers_the_locked_and_accepts_the_gone(self) -> None:
        bucket = LockedBucket(30)
        bucket.add("ended.txt", "v1", NOW - timedelta(days=40))
        bucket.add("locked.txt", "v2", NOW - timedelta(days=1))
        requests = []
        send = bucket.delete_objects

        def delete_objects(**kwargs):
            requests.append(kwargs)
            return send(**kwargs)
        bucket.delete_objects = delete_objects
        result = retention_lock._sweep_bucket(
            bucket, "b", self.rows(("ended.txt", "v1"), ("locked.txt", "v2"), ("gone.txt", "v3")), NOW)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["Delete"]["Quiet"])
        self.assertEqual(result["deleted"], ["r1", "r3"])
        self.assertEqual(result["deferred"], [("r2", bucket.until("locked.txt", "v2"))])
        self.assertEqual((result["unreadable"], result["failed"]), ([], []))

    def test_errors_are_failures_retried_sooner_when_transient(self) -> None:
        bucket = LockedBucket(30)
        bucket.add("ended.txt", "v1", NOW - timedelta(days=40))
        bucket.add("busy.txt", "v2", NOW - timedelta(days=40))
        bucket.delete_objects = lambda **kwargs: {"Errors": [
            {"Key": "ended.txt", "VersionId": "v1", "Code": "AccessDenied"},
            {"Key": "busy.txt", "VersionId": "v2", "Code": "InternalError"}]}
        result = retention_lock._sweep_bucket(
            bucket, "b", self.rows(("ended.txt", "v1"), ("busy.txt", "v2")), NOW)
        # AccessDenied on an ended lock: not explained, a permanent failure.
        self.assertEqual(result["failed"], [("r1", "AccessDenied", False), ("r2", "InternalError", True)])
        self.assertEqual(bucket.retention_reads, [("ended.txt", "v1")])

    def test_a_lost_request_fails_every_row_of_the_batch(self) -> None:
        class Lost(LockedBucket):
            def delete_objects(self, **kwargs):
                raise client_error("SlowDown")

        result = retention_lock._sweep_bucket(
            Lost(30), "b", self.rows(("a.txt", "v1"), ("b.txt", "v2")), NOW)
        self.assertEqual(result["failed"], [("r1", "SlowDown", True), ("r2", "SlowDown", True)])

    def test_an_unreadable_retention_is_its_own_outcome(self) -> None:
        class Denied(LockedBucket):
            def get_object_retention(self, **kwargs):
                raise client_error("AccessDenied", "GetObjectRetention")

        bucket = Denied(30)
        bucket.add("locked.txt", "v1", NOW - timedelta(days=1))
        result = retention_lock._sweep_bucket(bucket, "b", self.rows(("locked.txt", "v1")), NOW)
        self.assertEqual(result["unreadable"], [("r1", NOW + timedelta(hours=1))])
        self.assertEqual(result["failed"], [])


if __name__ == "__main__":
    unittest.main()
