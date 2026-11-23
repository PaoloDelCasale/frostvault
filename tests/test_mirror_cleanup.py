"""#771 Mirror Cleanup (ADR-0052).

Real admission, group claim and worker on a disposable PostgreSQL 16 database
(fixtures of ``tests.test_duplicate_cleanup``). The cleanup is off by default;
once on, a daily run schedules the Cloud-only files whose Local Copy was last
seen at least ``grace_days`` ago, optionally only under chosen folders, as one
automatic Delayed Cloud Deletion, or opens a Vault Problem above the cap. At
execution a member back on the server or changed is skipped alone.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import duplicate_cleanup, mirror_cleanup
from app.services.lifecycle_profiles import GUIDED_PROFILES
from app.services.lifecycle_policies import set_vault_plan
from tests.test_duplicate_cleanup import DuplicateCleanupFixture

BACKUP = b"ab_20261101.tar.zst bytes"
OTHER = b"ab_20261108.tar.zst, a little longer"
PHOTO = b"photo-bytes-771"
KEY = mirror_cleanup.problem_key(2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class MirrorCleanupFixture(DuplicateCleanupFixture):

    def gone(self, path: str, payload: bytes, *, days: float = 30) -> str:
        """A backup uploaded, then deleted from the server ``days`` ago."""
        file_id = self.cloud_only_at(path, payload)
        self.age(file_id, days=days)
        return file_id

    def mirror(self, **values) -> dict:
        with self.db() as connection:
            return mirror_cleanup.update_settings(
                connection, vault_id=2, actor="owner", values={"enabled": True, **values})

    def run_mirror(self, now: datetime | None = None) -> dict | None:
        with self.db() as connection:
            return mirror_cleanup.run_vault(connection, 2, now=now or utcnow())

    def mirror_due(self, now: datetime | None = None) -> list[int]:
        with self.db() as connection:
            return mirror_cleanup.due_vaults(connection, now=now or utcnow())

    def mirror_status(self, now: datetime | None = None) -> dict:
        with self.db() as connection:
            return mirror_cleanup.status(connection, 2, now=now)

    def mirror_problem(self) -> dict | None:
        rows = self.rows("SELECT problem_status, message_params FROM notifications WHERE dedupe_key=%s", KEY)
        return rows[0] if rows else None


class SelectionTests(MirrorCleanupFixture):

    def test_it_is_off_by_default_and_never_runs_until_enabled(self) -> None:
        self.gone("backup/ab_1/a.tar.zst", BACKUP)
        status = self.mirror_status()
        self.assertEqual((status["enabled"], status["grace_days"], status["cap_files"], status["cap_bytes"],
                          status["folders"], status["beyond_minimum"]),
                         (False, 7, 5000, 50 * 1024 ** 3, [], False))
        self.assertEqual((status["grace_days_min"], status["grace_days_max"]), (1, 180))
        self.assertEqual(self.mirror_due(), [])
        self.assertIsNone(self.run_mirror())
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)

    def test_aged_cloud_only_files_are_scheduled_even_the_only_copy(self) -> None:
        only = self.gone("backup/ab_1/a.tar.zst", BACKUP)                 # the only copy
        self.put("new/photo.jpg", PHOTO)
        self.gone("old/photo.jpg", PHOTO)                                  # a Cloud Duplicate
        young = self.gone("backup/ab_2/a.tar.zst", OTHER, days=1)          # still waiting
        unseen = self.cloud_only_at("backup/unseen.bin", b"never-seen-771")
        with self.db() as connection:
            connection.execute("UPDATE files SET local_seen_at=NULL WHERE id=%s", (unseen,))
        self.put("backup/ab_3/present.tar.zst", b"still here")             # present: never
        self.mirror()

        outcome = self.run_mirror()
        self.assertEqual((outcome["outcome"], outcome["file_count"], outcome["byte_count"]),
                         ("scheduled", 2, len(BACKUP) + len(PHOTO)))
        self.assertEqual((outcome["waiting_count"], outcome["unseen_count"]), (1, 1))
        group = outcome["group_id"]
        self.assertEqual(self.scheduled_paths(group), ["backup/ab_1/a.tar.zst", "old/photo.jpg"])
        [request] = self.rows("""SELECT automatic, mirror_cleanup, stop_protection, reason
                                 FROM cloud_purge_requests WHERE group_id=%s""", group)
        self.assertEqual((request["automatic"], request["mirror_cleanup"], request["stop_protection"]),
                         (True, True, False))
        self.assertTrue(request["reason"])
        run_after = self.rows("SELECT min(run_after) AS t FROM jobs WHERE group_id=%s", group)[0]["t"]
        self.assertGreaterEqual(run_after - utcnow(), timedelta(days=7) - timedelta(minutes=1))
        [event] = self.rows("""SELECT actor, detail FROM audit_events WHERE event='cloud_deletion.requested'
                               AND detail->>'group_id'=%s""", group)
        self.assertEqual((event["actor"], event["detail"]["automatic"], event["detail"]["mirror_cleanup"]),
                         ("system", True, True))
        [notice] = self.rows("SELECT message_params FROM notifications WHERE dedupe_key=%s",
                             f"{mirror_cleanup.NOTICE_EVENT}:{group}")
        self.assertEqual((notice["message_params"]["count"], notice["message_params"]["mirror_cleanup"]), (2, True))
        status = self.mirror_status()
        self.assertEqual([p["group_id"] for p in status["pending"]], [group])
        self.assertEqual(self.status()["pending"], [], "not listed as a duplicate cleanup")
        self.assertTrue(only and young)

    def test_the_scope_limits_the_candidates_to_the_chosen_folders(self) -> None:
        self.gone("backup/ab_1/a.tar.zst", BACKUP)
        self.gone("backupx/b.tar.zst", OTHER)          # a sibling with the same prefix
        self.gone("photos/original.jpg", PHOTO)
        self.mirror(folders=["backup/", "./backup//ab_1"])
        self.assertEqual(self.mirror_status()["folders"], ["backup"], "normalized, nested folders dropped")
        outcome = self.run_mirror()
        self.assertEqual(self.scheduled_paths(outcome["group_id"]), ["backup/ab_1/a.tar.zst"])
        self.assertEqual(self.mirror_status()["current"]["count"], 0)

    def test_invalid_folders_are_refused(self) -> None:
        for folders in (["../etc"], ["backup/../x"], ["backup/*"], ["a?b"], [""], ["/"], ["/backup"], "backup",
                        [1], ["a\nb"], [f"f{i}" for i in range(101)]):
            with self.subTest(folders=folders), self.assertRaises(mirror_cleanup.MirrorCleanupError) as error:
                self.mirror(folders=folders)
            self.assertEqual(error.exception.reason, "folders")

    def test_it_runs_once_a_day_and_never_schedules_twice(self) -> None:
        self.gone("backup/a.tar.zst", BACKUP)
        self.mirror()
        now = utcnow()
        self.assertEqual(self.mirror_due(now), [2])
        self.assertEqual(self.run_mirror(now)["outcome"], "scheduled")
        self.assertEqual(self.mirror_due(now + timedelta(hours=23)), [])
        self.assertIsNone(self.run_mirror(now + timedelta(hours=23)))
        self.assertEqual(datetime.fromisoformat(self.mirror_status()["next_run_at"]), now + timedelta(days=1))
        self.assertEqual(self.run_mirror(now + timedelta(days=1))["outcome"], "none")
        self.assertEqual(self.count("SELECT DISTINCT group_id FROM jobs WHERE action='cloud-purge'"), 1)

    def test_the_two_cleanups_never_schedule_the_same_file(self) -> None:
        self.moved("photo.jpg", PHOTO)                     # a Cloud Duplicate, aged
        self.gone("backup/a.tar.zst", BACKUP)
        self.configure()                                   # the duplicate cleanup
        self.mirror()
        duplicates = self.run_cleanup()
        self.assertEqual(self.scheduled_paths(duplicates["group_id"]), ["old/photo.jpg"])
        mirrored = self.run_mirror()
        self.assertEqual(self.scheduled_paths(mirrored["group_id"]), ["backup/a.tar.zst"])
        # And the other way round, from a fresh pair.
        self.moved("video.mp4", b"video-771")
        later = utcnow() + timedelta(days=1, seconds=1)
        self.assertEqual(self.scheduled_paths(self.run_mirror(later)["group_id"]), ["old/video.mp4"])
        self.assertEqual(self.run_cleanup(later)["outcome"], "none")
        self.assertEqual(self.count("""SELECT file_id FROM jobs WHERE action='cloud-purge'
                                       GROUP BY file_id HAVING count(*) > 1"""), 0)

    def test_files_in_a_manual_deletion_are_skipped(self) -> None:
        self.gone("backup/a.tar.zst", BACKUP)
        self.gone("backup/b.tar.zst", OTHER)
        manual = self.schedule(paths=["backup/a.tar.zst"])
        self.mirror()
        outcome = self.run_mirror()
        self.assertEqual(self.scheduled_paths(outcome["group_id"]), ["backup/b.tar.zst"])
        self.assertEqual(self.scheduled_paths(manual.group_id), ["backup/a.tar.zst"])

    def test_paused_and_stopped_vaults_are_skipped(self) -> None:
        self.gone("backup/a.tar.zst", BACKUP)
        self.mirror()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET paused=true WHERE id=2")
        self.assertEqual(self.mirror_due(), [])
        self.assertIsNone(self.run_mirror())
        with self.db() as connection:
            connection.execute("UPDATE vaults SET paused=false WHERE id=2")
        self.stop("keep")
        self.assertIsNone(self.run_mirror())
        with self.assertRaises(mirror_cleanup.MirrorCleanupError) as refused:
            self.mirror()
        self.assertEqual(refused.exception.reason, "stopped")

    def test_an_unavailable_volume_schedules_nothing(self) -> None:
        self.gone("backup/a.tar.zst", BACKUP)
        self.mirror()
        from types import SimpleNamespace
        with patch("app.services.source_layout.vault_local_access",
                   return_value=SimpleNamespace(local_operations_allowed=False)):
            status = self.mirror_status()
            outcome = self.run_mirror()
        self.assertEqual((status["current"], status["ready"]), (None, None))
        self.assertEqual(outcome["outcome"], "skipped")


class CapTests(MirrorCleanupFixture):

    def test_over_the_cap_schedules_nothing_and_opens_a_problem_until_under(self) -> None:
        self.gone("backup/a.tar.zst", BACKUP)
        self.gone("backup/b.tar.zst", OTHER)
        self.mirror(cap_files=1)
        now = utcnow()
        capped = self.run_mirror(now)
        self.assertEqual((capped["outcome"], capped["file_count"]), ("capped", 2))
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)
        problem = self.mirror_problem()
        self.assertEqual(problem["problem_status"], "open")
        self.assertTrue(problem["message_params"]["mirror_cleanup"])
        self.mirror(cap_files=10, cap_bytes=len(BACKUP))
        self.assertEqual(self.run_mirror(now + timedelta(days=1))["outcome"], "capped")
        self.mirror(cap_bytes=10 * 1024 ** 2)
        self.assertEqual(self.run_mirror(now + timedelta(days=2))["outcome"], "scheduled")
        self.assertEqual(self.mirror_problem()["problem_status"], "resolved")

    def test_turning_it_off_closes_the_problem(self) -> None:
        self.gone("backup/a.tar.zst", BACKUP)
        self.gone("backup/b.tar.zst", OTHER)
        self.mirror(cap_files=1)
        self.assertEqual(self.run_mirror()["outcome"], "capped")
        with self.db() as connection:
            mirror_cleanup.update_settings(connection, vault_id=2, actor="owner", values={"enabled": False})
        self.assertEqual(self.mirror_problem()["problem_status"], "resolved")
        self.assertEqual(self.mirror_due(utcnow() + timedelta(days=2)), [])

    def test_invalid_settings_are_refused(self) -> None:
        for values in ({"grace_days": 0}, {"grace_days": 181}, {"cap_files": 0}, {"cap_bytes": 0},
                       {"enabled": "yes"}, {"beyond_minimum": 1}, {"surprise": 1}):
            with self.subTest(values=values), self.assertRaises(mirror_cleanup.MirrorCleanupError):
                self.mirror(**values)
        self.assertEqual(self.mirror(grace_days=180)["grace_days"], 180)


class ExecutionTests(MirrorCleanupFixture):

    def mirror_group(self) -> tuple[str, dict, dict]:
        a, b = self.gone("backup/a.tar.zst", BACKUP), self.gone("backup/b.tar.zst", OTHER)
        self.mirror()
        outcome = self.run_mirror(utcnow() - timedelta(days=8))  # its seven days have passed
        self.assertEqual(outcome["file_count"], 2)
        return outcome["group_id"], self.current(a), self.current(b)

    def deleted(self, path: str, version: dict) -> bool:
        return (f"vaults/{self.uuid}/{path}", version["provider_version_id"]) in self.provider.deleted

    def test_the_only_copy_is_deleted_and_counted_apart_from_duplicates(self) -> None:
        group, a, b = self.mirror_group()
        self.drain(group)
        self.assertEqual(self.path_statuses(group), {"backup/a.tar.zst": "completed",
                                                     "backup/b.tar.zst": "completed"})
        self.assertTrue(self.deleted("backup/a.tar.zst", a) and self.deleted("backup/b.tar.zst", b))
        totals = self.mirror_status()["totals"]
        self.assertEqual((totals["deleted_count"], totals["deleted_bytes"]), (2, len(BACKUP) + len(OTHER)))
        self.assertIsNone(self.status()["totals"], "the duplicate cleanup's totals are untouched")

    def test_a_member_back_on_the_server_is_skipped_alone(self) -> None:
        group, a, b = self.mirror_group()
        (self.source / "backup/b.tar.zst").write_bytes(OTHER)  # back on disk, not yet observed
        self.drain(group)
        self.assertEqual(self.path_statuses(group), {"backup/a.tar.zst": "completed",
                                                     "backup/b.tar.zst": "cancelled"})
        self.assertTrue(self.deleted("backup/a.tar.zst", a))
        self.assertFalse(self.deleted("backup/b.tar.zst", b))
        self.assertEqual(self.rows("SELECT id FROM cleanup_batches"), [], "no grant is left open")

    def test_an_observed_reappearance_revokes_only_that_member(self) -> None:
        group, _, _ = self.mirror_group()
        target = self.source / "backup/b.tar.zst"
        target.write_bytes(OTHER)
        stat = target.stat()
        with self.db() as connection:
            ArchiveCatalog(connection).observe_local_copies_batch(
                vault_id=2, entries=[("backup/b.tar.zst", "regular", stat.st_size, stat.st_mtime_ns,
                                      storage.now_iso())])
        self.assertEqual(self.path_statuses(group), {"backup/a.tar.zst": "pending_delay",
                                                     "backup/b.tar.zst": "cancelled"})

    def test_a_changed_member_is_skipped_alone(self) -> None:
        group, a, b = self.mirror_group()
        with self.db() as connection:  # b's Current is no longer the consented one
            connection.execute(
                "UPDATE jobs SET archive_version_id=NULL WHERE group_id=%s AND file_id=%s",
                (group, str(b["file_id"])))
        self.drain(group)
        self.assertEqual(self.path_statuses(group), {"backup/a.tar.zst": "completed",
                                                     "backup/b.tar.zst": "cancelled"})
        self.assertFalse(self.deleted("backup/b.tar.zst", b))
        self.assertTrue(a)


class MinimumStorageTests(MirrorCleanupFixture):

    def in_class(self, file_id: str, storage_class: str, *, days: float) -> None:
        with self.db() as connection:
            connection.execute(
                """UPDATE archive_versions SET storage_class=%s,
                       storage_class_since=now() - make_interval(secs => %s)
                   WHERE id=(SELECT current_version_id FROM files WHERE id=%s)""",
                (storage_class, days * 86400, file_id))

    def test_beyond_minimum_holds_back_the_files_still_within_it(self) -> None:
        self.in_class(self.gone("backup/a.tar.zst", BACKUP), "GLACIER_IR", days=20)
        self.in_class(self.gone("backup/b.tar.zst", OTHER), "GLACIER_IR", days=200)
        self.mirror(beyond_minimum=True)
        status = self.mirror_status()
        self.assertEqual((status["held"], status["ready"]),
                         ({"count": 1, "bytes": len(BACKUP)}, {"count": 1, "bytes": len(OTHER)}))
        outcome = self.run_mirror()
        self.assertEqual((outcome["file_count"], outcome["held_count"]), (1, 1))
        self.assertEqual(self.scheduled_paths(outcome["group_id"]), ["backup/b.tar.zst"])

    def test_off_it_schedules_them_and_says_what_they_still_bill(self) -> None:
        self.in_class(self.gone("backup/a.tar.zst", BACKUP), "GLACIER_IR", days=20)
        self.mirror()
        status = self.mirror_status()
        self.assertEqual((status["held"], status["minimum_storage"]["count"]), (None, 1))
        outcome = self.run_mirror()
        self.assertEqual((outcome["file_count"], outcome["minimum_count"]), (1, 1))

    def test_the_cloud_life_is_compared_with_the_minimum_of_the_plan(self) -> None:
        with self.db() as connection:
            set_vault_plan(connection, 2, GUIDED_PROFILES["recent_backups"])
        self.mirror(grace_days=7)
        self.assertEqual(self.mirror_status()["cloud_life"], {
            "days_after_disappearance": 14, "storage_class": "GLACIER_IR", "class_from_day": 0,
            "minimum_days": 90, "minimum_until_day": 90, "beyond_minimum": False})
        self.mirror(grace_days=150)
        life = self.mirror_status()["cloud_life"]
        self.assertEqual((life["days_after_disappearance"], life["beyond_minimum"]), (157, True))
        with self.db() as connection:
            set_vault_plan(connection, 2, GUIDED_PROFILES["standard_only"])
        life = self.mirror_status()["cloud_life"]
        self.assertEqual((life["storage_class"], life["minimum_days"], life["minimum_until_day"],
                          life["beyond_minimum"]), ("STANDARD", 0, None, False))


class ReauthRuleTests(unittest.TestCase):
    """Which changes could delete more (#771)."""

    BASE = {"enabled": True, "grace_days": 7, "cap_files": 5000, "cap_bytes": 100,
            "folders": ["backup"], "beyond_minimum": True}

    def test_loosening_and_tightening(self) -> None:
        loose = [{"grace_days": 6}, {"cap_files": 5001}, {"cap_bytes": 101}, {"folders": []},
                 {"folders": ["backup", "photos"]}, {"folders": ["photos"]}, {"beyond_minimum": False}]
        tight = [{"enabled": False}, {"grace_days": 8}, {"cap_files": 1}, {"cap_bytes": 1},
                 {"folders": ["backup/ab_1"]}, {"folders": ["backup"]}]
        for change in loose:
            with self.subTest(change=change):
                self.assertTrue(mirror_cleanup.loosens(self.BASE, {**self.BASE, **change}))
        for change in tight:
            with self.subTest(change=change):
                self.assertFalse(mirror_cleanup.loosens(self.BASE, {**self.BASE, **change}))
        off = {**self.BASE, "enabled": False}
        self.assertTrue(mirror_cleanup.loosens(off, self.BASE), "turning it on")
        self.assertFalse(mirror_cleanup.loosens({**self.BASE, "folders": []},
                                                {**self.BASE, "folders": ["x"]}))

    def test_folders_are_normalized(self) -> None:
        self.assertEqual(mirror_cleanup.normalize_folders([" a//b/ ", "a/b/c", "./c", "a/b"]), ["a/b", "c"])
        self.assertEqual(mirror_cleanup.normalize_folders([]), [])


class HttpTests(MirrorCleanupFixture):

    owner = {}

    def setUp(self):
        super().setUp()
        self.client = self.http_client()
        self.headers = self.sign_in().csrf_headers
        self.url = "/api/vaults/2/mirror-cleanup"

    def reauth(self, at: datetime | None = None) -> None:
        with self.db() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (at or datetime.now(timezone.utc),))

    def stale(self) -> None:
        self.reauth(datetime(2000, 1, 1, tzinfo=timezone.utc))

    def send(self, body: dict):
        return self.client.put(self.url, json=body, headers=self.headers)

    def test_turning_it_on_or_loosening_it_needs_a_fresh_authentication(self) -> None:
        status = self.client.get(self.url)
        self.assertEqual(status.status_code, 200, status.text)
        body = status.json()
        self.assertFalse(body["enabled"])
        self.assertEqual((body["current"], body["totals"], body["folders"]), ({"count": 0, "bytes": 0}, None, []))
        self.assertIn("cloud_life", body)
        self.stale()
        for change in ({"enabled": True}, {"grace_days": 3}, {"cap_files": 6000}, {}):
            with self.subTest(change=change):
                refused = self.send(change)
                self.assertEqual(refused.status_code, 403, refused.text)
                self.assertEqual(refused.json()["code"], "reauth_required")
        self.reauth()
        on = self.send({"enabled": True, "grace_days": 21, "folders": ["Backup/"], "beyond_minimum": True})
        self.assertEqual(on.status_code, 200, on.text)
        self.assertEqual((on.json()["enabled"], on.json()["grace_days"], on.json()["folders"]),
                         (True, 21, ["Backup"]))
        self.assertEqual(self.send({"grace_days": 181}).status_code, 422)
        invalid = self.send({"folders": ["../x"]})
        self.assertEqual(invalid.status_code, 422, invalid.text)
        [audit] = self.rows("SELECT actor, detail FROM audit_events WHERE event='mirror_cleanup.settings_updated'")
        self.assertEqual((audit["actor"], audit["detail"]["after"]["folders"]), ("owner", ["Backup"]))

        self.stale()
        for change in ({"grace_days": 30}, {"cap_files": 10}, {"folders": ["Backup/ab_1"]}, {"enabled": False}):
            with self.subTest(change=change):
                accepted = self.send(change)
                self.assertEqual(accepted.status_code, 200, accepted.text)
        for change in ({"enabled": True}, {"folders": []}, {"beyond_minimum": False}, {"grace_days": 29}):
            with self.subTest(change=change):
                self.assertEqual(self.send(change).status_code, 403)
        self.assertEqual(self.client.get("/api/vaults/999/mirror-cleanup").status_code, 404)
        duplicate = self.client.get("/api/vaults/2/duplicate-cleanup").json()
        self.assertFalse(duplicate["enabled"], "a separate setting")

    def test_the_duplicate_settings_are_untouched(self) -> None:
        self.reauth()
        self.assertEqual(self.send({"enabled": True}).status_code, 200)
        with self.db() as connection:
            self.assertFalse(duplicate_cleanup.status(connection, 2)["enabled"])


if __name__ == "__main__":
    unittest.main()
