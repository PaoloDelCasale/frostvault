"""#726 Automatic Duplicate Cleanup (ADR-0048).

Real admission, group claim and worker on a disposable PostgreSQL 16 database
(fixtures of ``tests.test_cloud_deletion_selection``). The cleanup is off by
default; once on, a daily run schedules the Cloud Duplicates whose Local Copy
was last seen at least ``grace_days`` ago as one automatic Delayed Cloud
Deletion, or opens a Vault Problem when they exceed the cap. At execution an
automatic group drops only the members that stopped qualifying; a manual
group stays all-or-nothing.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, storage
from app.catalog import ArchiveCatalog
from app.config import settings
from app.services import cloud_deletion, duplicate_cleanup, history_retention
from app.services.cloud_deletion import load_vault_health, observation_is_unconfirmed
from app.sessions import create_session, csrf_token_for
from tests.owner_fixtures import insert_owner
from tests.postgres import database_dsn
from tests.test_cloud_deletion_selection import SelectionFixture

PHOTO = b"photo-bytes-726"
VIDEO = b"video-bytes-726, a little longer"
ORIGINAL = b"only-in-the-cloud-726"
KEY = duplicate_cleanup.problem_key(2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class DuplicateCleanupFixture(SelectionFixture):

    def age(self, *file_ids: str, days: float = 30) -> None:
        """Pretend the Local Copy was last seen ``days`` ago."""
        with self.db() as connection:
            connection.execute(
                "UPDATE files SET local_seen_at=now() - make_interval(secs => %s) WHERE id = ANY(%s::uuid[])",
                (days * 86400, list(file_ids)))

    def configure(self, **values) -> dict:
        with self.db() as connection:
            return duplicate_cleanup.update_settings(
                connection, vault_id=2, actor="owner", values={"enabled": True, **values})

    def run_cleanup(self, now: datetime | None = None) -> dict | None:
        with self.db() as connection:
            return duplicate_cleanup.run_vault(connection, 2, now=now or utcnow())

    def due(self, now: datetime | None = None) -> list[int]:
        with self.db() as connection:
            return duplicate_cleanup.due_vaults(connection, now=now or utcnow())

    def problem(self) -> dict | None:
        rows = self.rows("SELECT problem_status, message_params FROM notifications WHERE dedupe_key=%s", KEY)
        return rows[0] if rows else None

    def moved(self, name: str, payload: bytes) -> str:
        """A file moved by an app: protected at new/, Cloud-only and aged at old/."""
        self.put(f"new/{name}", payload)
        file_id = self.cloud_only_at(f"old/{name}", payload)
        self.age(file_id)
        return file_id

    def current(self, file_id: str) -> dict:
        return self.rows(
            """SELECT v.* FROM files f JOIN archive_versions v ON v.id=f.current_version_id
               WHERE f.id=%s""", file_id)[0]

    def drain(self, group_id: str) -> None:
        """Run the group until no member is waiting (a revoked lead re-queues the rest)."""
        for _ in range(4):
            waiting = self.rows(
                """SELECT id FROM jobs WHERE group_id=%s AND status IN ('pending_delay','queued')
                   ORDER BY id LIMIT 1""", group_id)
            if not waiting:
                return
            self.run_purge(int(waiting[0]["id"]))
        self.fail("the group never finished")

    def status(self, now: datetime | None = None) -> dict:
        with self.db() as connection:
            return duplicate_cleanup.status(connection, 2, now=now)

    def seen_at(self, file_id: str) -> datetime:
        return self.rows("SELECT local_seen_at FROM files WHERE id=%s", file_id)[0]["local_seen_at"]

    def path_statuses(self, group_id: str) -> dict[str, str]:
        return {r["path"]: r["status"] for r in self.rows(
            """SELECT f.path, j.status FROM jobs j JOIN files f ON f.id=j.file_id
               WHERE j.group_id=%s""", group_id)}


class SelectionTests(DuplicateCleanupFixture):

    def test_it_is_off_by_default_and_never_runs_until_enabled(self) -> None:
        self.moved("photo.jpg", PHOTO)
        with self.db() as connection:
            status = duplicate_cleanup.status(connection, 2)
        self.assertEqual((status["enabled"], status["grace_days"], status["cap_files"], status["cap_bytes"]),
                         (False, 7, 5000, 50 * 1024 ** 3))
        self.assertEqual(self.due(), [])
        self.assertIsNone(self.run_cleanup())
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)

    def test_only_aged_cloud_duplicates_outside_other_work_are_scheduled(self) -> None:
        self.moved("photo.jpg", PHOTO)
        original = self.cloud_only_at("old/original.jpg", ORIGINAL)  # no copy on the server
        self.age(original)
        young = self.cloud_only_at("young/photo.jpg", PHOTO)          # seen 1 day ago
        self.age(young, days=1)
        unseen = self.cloud_only_at("unseen/photo.jpg", PHOTO)        # never seen: not a candidate
        with self.db() as connection:
            connection.execute("UPDATE files SET local_seen_at=NULL WHERE id=%s", (unseen,))
        pending = self.moved("video.mp4", VIDEO)                      # already in a manual deletion
        manual = self.schedule(paths=["old/video.mp4"])
        self.configure()

        outcome = self.run_cleanup()
        self.assertEqual((outcome["outcome"], outcome["file_count"], outcome["byte_count"]),
                         ("scheduled", 1, len(PHOTO)))
        group = outcome["group_id"]
        self.assertEqual(self.scheduled_paths(group), ["old/photo.jpg"])
        [request] = self.rows("SELECT automatic, stop_protection, reason FROM cloud_purge_requests WHERE group_id=%s", group)
        self.assertEqual((request["automatic"], request["stop_protection"]), (True, False))
        self.assertTrue(request["reason"])
        self.assertEqual({r["origin"] for r in self.rows("SELECT origin FROM jobs WHERE group_id=%s", group)},
                         {"automatic"})
        run_after = self.rows("SELECT min(run_after) AS t FROM jobs WHERE group_id=%s", group)[0]["t"]
        self.assertGreaterEqual(run_after - utcnow(), timedelta(days=7) - timedelta(minutes=1))
        [event] = self.rows("""SELECT actor, detail FROM audit_events WHERE event='cloud_deletion.requested'
                               AND detail->>'group_id'=%s""", group)
        self.assertEqual((event["actor"], event["detail"]["automatic"]), ("system", True))
        [notice] = self.rows("SELECT event, message_params FROM notifications WHERE dedupe_key=%s",
                             f"{duplicate_cleanup.NOTICE_EVENT}:{group}")
        self.assertEqual((notice["message_params"]["count"], notice["message_params"]["byte_count"]),
                         (1, len(PHOTO)))
        self.assertEqual(self.scheduled_paths(manual.group_id), ["old/video.mp4"])
        self.assertTrue(pending)

        # The young duplicate qualifies once its wait is over, at the next run.
        self.age(young, days=8)
        later = self.run_cleanup(utcnow() + timedelta(days=1, seconds=1))
        self.assertEqual(self.scheduled_paths(later["group_id"]), ["young/photo.jpg"])

    def test_it_runs_once_a_day_and_the_last_run_survives_a_restart(self) -> None:
        self.moved("photo.jpg", PHOTO)
        self.configure()
        now = utcnow()
        self.assertEqual(self.due(now), [2])
        self.assertEqual(self.run_cleanup(now)["outcome"], "scheduled")
        self.assertEqual(self.due(now + timedelta(hours=23)), [])
        self.assertIsNone(self.run_cleanup(now + timedelta(hours=23)), "a restart must not run it again early")
        with self.db() as connection:
            status = duplicate_cleanup.status(connection, 2)
        self.assertEqual(datetime.fromisoformat(status["next_run_at"]), now + timedelta(days=1))
        self.assertEqual(len(status["pending"]), 1)
        self.assertTrue(status["pending"][0]["cancellable"])
        nothing = self.run_cleanup(now + timedelta(days=1))
        self.assertEqual(nothing["outcome"], "none", "already scheduled files are not scheduled twice")
        self.assertEqual(self.count("SELECT DISTINCT group_id FROM jobs WHERE action='cloud-purge'"), 1)

    def test_paused_and_stopped_vaults_are_skipped(self) -> None:
        self.moved("photo.jpg", PHOTO)
        self.configure()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET paused=true WHERE id=2")
        self.assertEqual(self.due(), [])
        self.assertIsNone(self.run_cleanup())
        with self.db() as connection:
            connection.execute("UPDATE vaults SET paused=false WHERE id=2")
        self.stop("keep")
        self.assertEqual(self.due(), [])
        self.assertIsNone(self.run_cleanup())
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)
        with self.assertRaises(duplicate_cleanup.DuplicateCleanupError) as refused:
            self.configure()
        self.assertEqual(refused.exception.code, "stopped")


class CapTests(DuplicateCleanupFixture):

    def test_over_the_cap_schedules_nothing_and_opens_a_problem_until_under(self) -> None:
        self.moved("photo.jpg", PHOTO)
        self.moved("video.mp4", VIDEO)
        self.configure(cap_files=1)
        now = utcnow()
        capped = self.run_cleanup(now)
        self.assertEqual((capped["outcome"], capped["file_count"]), ("capped", 2))
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)
        problem = self.problem()
        self.assertEqual(problem["problem_status"], "open")
        self.assertEqual((problem["message_params"]["count"], problem["message_params"]["cap_files"]), (2, 1))
        self.assertTrue(problem["message_params"]["duplicate_cleanup"])

        self.configure(cap_files=10, cap_bytes=len(PHOTO))  # the bytes cap holds too
        self.assertEqual(self.run_cleanup(now + timedelta(days=1))["outcome"], "capped")
        self.assertEqual(self.problem()["problem_status"], "open")

        self.configure(cap_bytes=10 * 1024 ** 2)
        self.assertEqual(self.run_cleanup(now + timedelta(days=2))["outcome"], "scheduled")
        self.assertEqual(self.problem()["problem_status"], "resolved")

    def test_turning_it_off_closes_the_problem(self) -> None:
        self.moved("photo.jpg", PHOTO)
        self.moved("video.mp4", VIDEO)
        self.configure(cap_files=1)
        self.assertEqual(self.run_cleanup()["outcome"], "capped")
        with self.db() as connection:
            duplicate_cleanup.update_settings(connection, vault_id=2, actor="owner", values={"enabled": False})
        self.assertEqual(self.problem()["problem_status"], "resolved")
        self.assertEqual(self.due(utcnow() + timedelta(days=2)), [])

    def test_invalid_settings_are_refused(self) -> None:
        for values in ({"grace_days": 0}, {"grace_days": 91}, {"cap_files": 0},
                       {"cap_bytes": 0}, {"enabled": "yes"}, {"surprise": 1}):
            with self.subTest(values=values), self.assertRaises(duplicate_cleanup.DuplicateCleanupError):
                self.configure(**values)


class ExecutionTests(DuplicateCleanupFixture):
    """Execution re-checks: per member for automatic groups only."""

    def automatic_group(self) -> tuple[str, dict, dict]:
        photo, video = self.moved("photo.jpg", PHOTO), self.moved("video.mp4", VIDEO)
        self.configure()
        outcome = self.run_cleanup(utcnow() - timedelta(days=8))  # its seven days have passed
        self.assertEqual(outcome["file_count"], 2)
        return outcome["group_id"], self.current(photo), self.current(video)

    def assert_deleted_version(self, path: str, version: dict, deleted: bool) -> None:
        key = (f"vaults/{self.uuid}/{path}", version["provider_version_id"])
        self.assertEqual(key in self.provider.deleted, deleted, path)

    def test_a_member_that_is_no_longer_a_duplicate_is_skipped_alone(self) -> None:
        group, photo, video = self.automatic_group()
        # The other copy of the photo disappears: old/photo.jpg is its only one now.
        self.lose("new/photo.jpg", self.rows("SELECT id FROM files WHERE path='new/photo.jpg'")[0]["id"])
        self.drain(group)
        self.assertEqual(self.path_statuses(group), {"old/photo.jpg": "cancelled", "old/video.mp4": "completed"})
        self.assert_deleted_version("old/photo.jpg", photo, False)
        self.assert_deleted_version("old/video.mp4", video, True)
        [skipped] = self.rows("SELECT message FROM jobs j JOIN files f ON f.id=j.file_id "
                              "WHERE j.group_id=%s AND f.path='old/photo.jpg'", group)
        self.assertIn("No longer a Cloud Duplicate", skipped["message"])
        [audit] = self.rows("SELECT actor, detail FROM audit_events WHERE event='cloud_deletion.files_revoked'")
        self.assertEqual((audit["actor"], audit["detail"]["revoked"], audit["detail"]["paths"]),
                         ("system", 1, ["old/photo.jpg"]))
        self.assertEqual(self.rows("SELECT id FROM cleanup_batches"), [], "no grant is left open")

    def test_a_present_entry_skips_only_that_member_of_an_automatic_group(self) -> None:
        group, photo, video = self.automatic_group()
        (self.source / "old/video.mp4").write_bytes(VIDEO)  # back on disk, not yet observed
        self.drain(group)
        self.assertEqual(self.path_statuses(group), {"old/photo.jpg": "completed", "old/video.mp4": "cancelled"})
        self.assert_deleted_version("old/photo.jpg", photo, True)
        self.assert_deleted_version("old/video.mp4", video, False)

    def test_a_manual_group_stays_all_or_nothing(self) -> None:
        photo_id, video_id = self.moved("photo.jpg", PHOTO), self.moved("video.mp4", VIDEO)
        photo, video = self.current(photo_id), self.current(video_id)
        manual = self.schedule(paths=[], directories=["old"], duplicates_only=True)
        (self.source / "old/video.mp4").write_bytes(VIDEO)
        self.run_purge(manual.job_ids[0])
        self.assertEqual(self.statuses(manual.group_id), ["cancelled", "cancelled"])
        self.assertEqual(self.provider.deleted, [])
        self.assertTrue(photo and video)

    def test_the_totals_count_only_completed_automatic_members(self) -> None:
        group, _, _ = self.automatic_group()
        self.lose("new/photo.jpg", self.rows("SELECT id FROM files WHERE path='new/photo.jpg'")[0]["id"])
        self.drain(group)  # the photo is revoked, the video deleted
        self.assertEqual(self.path_statuses(group), {"old/photo.jpg": "cancelled", "old/video.mp4": "completed"})
        manual_id = self.moved("manual.bin", b"manual-726-bytes")
        manual = self.schedule(paths=["old/manual.bin"])
        self.drain(manual.group_id)
        self.assertEqual(self.statuses(manual.group_id), ["completed"])
        self.assertTrue(manual_id)
        totals = self.status()["totals"]
        [finished] = self.rows("""SELECT j.finished_at FROM jobs j JOIN files f ON f.id=j.file_id
                                  WHERE j.group_id=%s AND f.path='old/video.mp4'""", group)
        self.assertEqual(totals, {"deleted_count": 1, "deleted_bytes": len(VIDEO),
                                  "last_deleted_at": finished["finished_at"].isoformat()})

    def test_the_totals_survive_the_history_retention_of_their_jobs(self) -> None:
        group, _, _ = self.automatic_group()
        self.drain(group)
        self.assertEqual(self.statuses(group), ["completed", "completed"])
        before = self.status()["totals"]
        self.assertEqual((before["deleted_count"], before["deleted_bytes"]), (2, len(PHOTO) + len(VIDEO)))
        with self.db() as connection:  # the terminal notices were delivered and read
            connection.execute("DELETE FROM job_terminal_notifications")
            connection.execute("UPDATE notifications SET read_at=now() WHERE read_at IS NULL")
        history_retention.collect(self.db, now=utcnow() + timedelta(days=400))
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE group_id=%s", group), 0,
                         "the history retention collected the completed Jobs")
        self.assertEqual(self.status()["totals"], before)

    def test_a_manual_deletion_never_adds_to_the_totals(self) -> None:
        self.moved("manual.bin", b"manual-729-bytes")
        manual = self.schedule(paths=["old/manual.bin"])
        self.drain(manual.group_id)
        self.assertEqual(self.statuses(manual.group_id), ["completed"])
        self.assertIsNone(self.status()["totals"], "never recorded: no invented zero")

    def test_an_observed_reappearance_revokes_only_that_automatic_member(self) -> None:
        group, _, _ = self.automatic_group()
        target = self.source / "old/video.mp4"
        target.write_bytes(VIDEO)
        stat = target.stat()
        with self.db() as connection:
            ArchiveCatalog(connection).observe_local_copies_batch(
                vault_id=2, entries=[("old/video.mp4", "regular", stat.st_size, stat.st_mtime_ns,
                                      storage.now_iso())])
        self.assertEqual(self.path_statuses(group), {"old/photo.jpg": "pending_delay", "old/video.mp4": "cancelled"})


def first_run_from(next_run: datetime, at: datetime) -> datetime:
    """The first daily run at or after ``at``."""
    run = next_run
    while run < at:
        run += timedelta(days=1)
    return run


class CountTests(DuplicateCleanupFixture):
    """#729: the duplicates now, those still waiting, and the totals."""

    DELAY = timedelta(seconds=cloud_deletion.DELETION_DELAY_SECONDS)

    def library(self) -> tuple[str, str]:
        """A ready and a waiting duplicate, plus files that are not counted."""
        ready = self.moved("photo.jpg", PHOTO)                       # seen 30 days ago
        waiting = self.cloud_only_at("young/photo.jpg", PHOTO)       # seen 1 day ago
        self.age(waiting, days=1)
        unseen = self.cloud_only_at("unseen/photo.jpg", PHOTO)       # never seen: current only
        with self.db() as connection:
            connection.execute("UPDATE files SET local_seen_at=NULL WHERE id=%s", (unseen,))
        self.age(self.cloud_only_at("old/original.jpg", ORIGINAL))  # no other copy: not a duplicate
        self.moved("video.mp4", VIDEO)
        self.schedule(paths=["old/video.mp4"])                        # already in a manual deletion
        return ready, waiting

    def test_the_status_counts_the_duplicates_now_waiting_and_ready_live(self) -> None:
        _, waiting = self.library()
        self.configure()
        now = utcnow()
        status = self.status(now)
        self.assertEqual(status["current"], {"count": 3, "bytes": 3 * len(PHOTO)})
        self.assertEqual(status["unseen"], {"count": 1, "bytes": len(PHOTO)})
        manual = self.preview(listing=cloud_deletion.ListingFilter(), duplicates_only=True)
        self.assertEqual((status["current"]["count"], status["current"]["bytes"]),
                         (manual.duplicate_count, manual.duplicate_bytes),
                         "the same count as the manual «Only Cloud Duplicates» preview of the whole Vault")
        self.assertEqual(status["ready"], {"count": 1, "bytes": len(PHOTO)})
        eligible = self.seen_at(waiting) + timedelta(days=7)
        self.assertEqual(status["waiting"], {
            "count": 1, "bytes": len(PHOTO), "first_eligible_at": eligible.isoformat(),
            # Never run yet: the first run is at the next poll, then daily.
            "earliest_deletion_at": (first_run_from(now, eligible) + self.DELAY).isoformat()})
        self.assertIsNone(status["totals"], "nothing deleted automatically yet")

        outcome = self.run_cleanup(now)
        self.assertEqual((outcome["outcome"], outcome["file_count"]), ("scheduled", 1))
        self.assertEqual((outcome["waiting_count"], outcome["waiting_bytes"], outcome["unseen_count"]),
                         (1, len(PHOTO), 1))
        after = self.status(now)
        self.assertEqual(after["current"]["count"], 2, "a scheduled duplicate is no longer counted")
        self.assertEqual(after["ready"]["count"], 0)
        next_run = now + timedelta(days=1)
        self.assertEqual(after["waiting"]["earliest_deletion_at"],
                         (first_run_from(next_run, eligible) + self.DELAY).isoformat(),
                         "aligned with the next daily run")

    def test_a_run_with_only_waiting_duplicates_records_how_many_wait(self) -> None:
        self.configure()
        nothing = self.run_cleanup()
        self.assertEqual((nothing["outcome"], nothing["waiting_count"], nothing["waiting_bytes"],
                          nothing["unseen_count"]), ("none", 0, 0, 0))
        young = self.cloud_only_at("young/photo.jpg", PHOTO)
        self.put("new/photo.jpg", PHOTO)
        self.age(young, days=1)
        waiting = self.run_cleanup(utcnow() + timedelta(days=1, seconds=1))
        self.assertEqual((waiting["outcome"], waiting["file_count"], waiting["waiting_count"],
                          waiting["waiting_bytes"]), ("none", 0, 1, len(PHOTO)))
        self.assertEqual(self.status()["last_outcome"]["waiting_count"], 1)

    def test_while_off_the_status_still_counts_the_duplicates_now(self) -> None:
        self.library()
        status = self.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["current"]["count"], 3)
        self.assertEqual(status["waiting"]["count"], 1)
        self.assertIsNone(status["waiting"]["earliest_deletion_at"], "no run is planned while off")
        self.assertIsNone(status["next_run_at"])

    def test_nothing_is_counted_when_it_cannot_be_proven(self) -> None:
        self.moved("photo.jpg", PHOTO)
        self.stop("keep")
        status = self.status()
        self.assertEqual((status["current"], status["waiting"], status["ready"], status["unseen"]),
                         (None, None, None, None))
        self.assertIsNone(status["totals"])

    def test_an_unavailable_volume_counts_nothing_even_without_candidates(self) -> None:
        """No Cloud Duplicate is left to check, yet the root must still be
        verified: an unavailable Source Volume is «unknown», never zero."""
        self.age(self.cloud_only_at("old/original.jpg", ORIGINAL))  # Cloud-only, not a duplicate
        self.configure()
        self.assertEqual(self.status()["current"], {"count": 0, "bytes": 0})
        with self.db() as connection:
            self.assertFalse(observation_is_unconfirmed(load_vault_health(connection, 2)),
                             "the previous health is still healthy")
        with patch("app.services.source_layout.vault_local_access",
                   return_value=SimpleNamespace(local_operations_allowed=False)):
            status = self.status()
            outcome = self.run_cleanup()
        self.assertEqual((status["current"], status["waiting"], status["ready"], status["unseen"]),
                         (None, None, None, None))
        self.assertEqual(outcome["outcome"], "skipped", "a run proves nothing either")

    def test_duplicates_never_seen_locally_are_counted_apart_and_never_scheduled(self) -> None:
        self.put("new/photo.jpg", PHOTO)
        unseen = self.cloud_only_at("old/photo.jpg", PHOTO)
        with self.db() as connection:
            connection.execute("UPDATE files SET local_seen_at=NULL WHERE id=%s", (unseen,))
        self.configure()
        status = self.status()
        self.assertEqual(status["current"], {"count": 1, "bytes": len(PHOTO)})
        self.assertEqual(status["unseen"], {"count": 1, "bytes": len(PHOTO)})
        self.assertEqual((status["ready"]["count"], status["waiting"]["count"]), (0, 0))
        self.assertIsNone(status["waiting"]["first_eligible_at"])
        outcome = self.run_cleanup()
        self.assertEqual((outcome["outcome"], outcome["file_count"], outcome["waiting_count"],
                          outcome["unseen_count"]), ("none", 0, 0, 1))
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)
        self.assertEqual(self.status()["last_outcome"]["unseen_count"], 1)


class HttpTests(DuplicateCleanupFixture):

    def setUp(self):
        super().setUp()
        configured = replace(settings, database_dsn=database_dsn(self.db_path), cookie_secure=False)
        for target in ("app.main.settings", "app.database.settings", "app.sessions.settings"):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))
        with self.db() as connection:
            insert_owner(connection)
            token = create_session(connection, auth_method="oidc")
            csrf = csrf_token_for(connection, token)
        self.client.cookies.set(settings.session_cookie_name, token)
        self.headers = {"X-CSRF-Token": csrf}
        self.url = "/api/vaults/2/duplicate-cleanup"

    def reauth(self, at: datetime | None = None) -> None:
        with self.db() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (at or datetime.now(timezone.utc),))

    def test_turning_it_on_or_changing_it_needs_a_fresh_authentication(self) -> None:
        status = self.client.get(self.url)
        self.assertEqual(status.status_code, 200, status.text)
        self.assertFalse(status.json()["enabled"])
        self.assertEqual(status.json()["current"], {"count": 0, "bytes": 0})
        self.assertIsNone(status.json()["totals"])
        self.assertEqual(status.json()["unseen"], {"count": 0, "bytes": 0})
        self.assertIn("waiting", status.json())
        self.assertIn("ready", status.json())
        self.reauth(datetime(2000, 1, 1, tzinfo=timezone.utc))
        for body in ({"enabled": True}, {"grace_days": 14}):
            with self.subTest(body=body):
                refused = self.client.put(self.url, json=body, headers=self.headers)
                self.assertEqual(refused.status_code, 403, refused.text)
                self.assertEqual(refused.json()["error"], "reauth_required")
        self.reauth()
        on = self.client.put(self.url, json={"enabled": True, "grace_days": 14, "cap_bytes": 1024},
                             headers=self.headers)
        self.assertEqual(on.status_code, 200, on.text)
        self.assertEqual((on.json()["enabled"], on.json()["grace_days"], on.json()["cap_bytes"]), (True, 14, 1024))
        invalid = self.client.put(self.url, json={"grace_days": 91}, headers=self.headers)
        self.assertEqual(invalid.status_code, 422, invalid.text)
        [audit] = self.rows("SELECT actor, detail FROM audit_events WHERE event='duplicate_cleanup.settings_updated'")
        self.assertEqual((audit["actor"], audit["detail"]["after"]["grace_days"]), ("owner", 14))

    def test_turning_it_off_never_needs_a_fresh_authentication(self) -> None:
        self.configure()
        self.reauth(datetime(2000, 1, 1, tzinfo=timezone.utc))
        off = self.client.put(self.url, json={"enabled": False}, headers=self.headers)
        self.assertEqual(off.status_code, 200, off.text)
        self.assertFalse(off.json()["enabled"])
        missing = self.client.get("/api/vaults/999/duplicate-cleanup")
        self.assertEqual(missing.status_code, 404, missing.text)


if __name__ == "__main__":
    unittest.main()
