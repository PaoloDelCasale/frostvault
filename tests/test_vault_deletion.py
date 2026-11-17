"""Vault Deletion of a Vault Tombstone (#717, ADR-0047).

Real catalog on a disposable PostgreSQL 16 database. The Tombstone is produced
by a real upload Job and a real Stop Protection with retained cloud content;
the S3 transport is the synthetic ``VersionedProvider`` with exact VersionIds,
delete markers and paginated listings.

Covered: the live preview of the whole prefix (versions, markers, bytes per
class, early-deletion cost) and nothing outside it; only a Tombstone can be
deleted; the seven-day delay, cancellation and one request per Vault; the
elapsed deletion removes every version and marker under the prefix, catalogued
or not, then the Vault and all its catalog rows, keeping the audit record and
the local files; a refused delete or leftovers keep the Tombstone with an open
Problem that survives the stopped-Vault rule (#715) until a retry, which runs
at once; a key outside the prefix stops the run before any delete; the HTTP
surface requires a fresh reauthentication and a successful listing.
"""
from __future__ import annotations

import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app import main, storage
from app.config import settings
from app.services import cloud_deletion, protection_stop, vault_deletion
from app.services import notifications as inbox
from app.services.vaults import list_vault_inventory
from app.sessions import create_session, csrf_token_for
from tests import test_upload_worker as fixtures
from tests.owner_fixtures import insert_owner
from tests.postgres import database_dsn
from tests.test_upload_worker import A
from tests.upload_provenance_provider import VersionedProvider

PAST = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
OTHER_UUID = "00000000-0000-4000-8000-0000000000aa"
STRAY = 1024 * 1024


class PagedProvider(VersionedProvider):
    """Versioned listings in small pages; a batch delete also removes markers."""

    page_size = 2

    def list_object_versions(self, *, Bucket, Prefix, KeyMarker="", VersionIdMarker="", **kwargs):
        self.hook("list", key=Prefix)
        entries = sorted(
            [(k, v, "version") for k, v in self.objects if k.startswith(Prefix)]
            + [(k, v, "marker") for k, v in self.markers if k.startswith(Prefix)])
        if KeyMarker:
            entries = [e for e in entries if (e[0], e[1]) > (KeyMarker, VersionIdMarker)]
        page, rest = entries[:self.page_size], entries[self.page_size:]
        response = {
            "Versions": [{"Key": k, "VersionId": v, "Size": len(self.objects[k, v][0]),
                          "StorageClass": self.classes.get((k, v), "STANDARD"),
                          "LastModified": self.modified.get((k, v), datetime(2026, 11, 1, tzinfo=timezone.utc))}
                         for k, v, kind in page if kind == "version"],
            "DeleteMarkers": [{"Key": k, "VersionId": v} for k, v, kind in page if kind == "marker"],
            "IsTruncated": bool(rest)}
        if rest:
            response.update(NextKeyMarker=page[-1][0], NextVersionIdMarker=page[-1][1])
        return response

    def delete_objects(self, *, Bucket, Delete):
        response = super().delete_objects(Bucket=Bucket, Delete=Delete)
        for item in Delete["Objects"]:
            self.markers.pop((item["Key"], item["VersionId"]), None)
        return response


class VaultDeletionFixture(unittest.TestCase):
    setUp_upload = fixtures.UploadWorkerTests.setUp
    db = fixtures.UploadWorkerTests.db
    observe = fixtures.UploadWorkerTests.observe
    write = fixtures.UploadWorkerTests.write
    queue = fixtures.UploadWorkerTests.queue
    claim = fixtures.UploadWorkerTests.claim
    run_job = fixtures.UploadWorkerTests.run_job
    job = fixtures.UploadWorkerTests.job
    rows = fixtures.UploadWorkerTests.rows

    def setUp(self) -> None:
        with patch("tests.test_upload_worker.VersionedProvider", PagedProvider):
            self.setUp_upload()
        self.provider.modified = {}
        self.prefix = f"vaults/{self.uuid}/"
        # The Vault's own upload, then content the catalog never knew.
        self.write(A)
        self.assertEqual(self.job(job_id := self.queue())["status"], "queued")
        self.assertTrue(self.run_job(job_id) is not None)
        self.stray = self.provider.add(self.prefix + "stray/old.bin", b"x" * STRAY)
        self.provider.classes[self.prefix + "stray/old.bin", self.stray] = "DEEP_ARCHIVE"
        self.provider.modified[self.prefix + "stray/old.bin", self.stray] = datetime.now(timezone.utc)
        self.provider.markers[self.prefix + "gone.txt", "marker-1"] = True
        # Another Vault's content and a look-alike prefix are never touched.
        self.foreign = {(f"vaults/{OTHER_UUID}/immich.jpg", self.provider.add(f"vaults/{OTHER_UUID}/immich.jpg", b"i")),
                        (f"vaults/{self.uuid}x/near.bin", self.provider.add(f"vaults/{self.uuid}x/near.bin", b"n"))}

    # -- helpers -------------------------------------------------------------

    def stop(self) -> None:
        with self.db() as connection:
            protection_stop.stop_protection(
                connection, vault_id=2, actor="owner", cloud_disposition="keep",
                confirmation="Docs", reason="archive it")
            vault = connection.execute("SELECT lifecycle FROM vaults WHERE id=2").fetchone()
        self.assertEqual(vault["lifecycle"], "tombstone")

    def target(self) -> dict:
        with self.db() as connection:
            return vault_deletion.deletion_target(connection, 2)

    def preview(self) -> dict:
        return vault_deletion.preview(self.provider, self.target())

    def request(self, *, requested_at: str = PAST, confirmation: str = "Docs") -> dict:
        summary = self.preview()
        with self.db() as connection:
            return vault_deletion.request_deletion(
                connection, vault_id=2, actor="owner", confirmation=confirmation,
                reason="test vault no longer needed", preview_summary=summary,
                requested_at=requested_at)

    def run_due(self) -> int:
        return vault_deletion.run_due(self.db, lambda: self.provider)

    def under_prefix(self) -> list:
        return sorted([k for k in self.provider.objects if k[0].startswith(self.prefix)]
                      + [k for k in self.provider.markers if k[0].startswith(self.prefix)])

    def deletion(self) -> dict | None:
        found = self.rows("SELECT * FROM vault_deletions WHERE vault_id=2")
        return found[0] if found else None

    def open_problems(self) -> list[dict]:
        with self.db() as connection:
            inbox.reconcile_problem_states(connection)
            return connection.execute(
                "SELECT event, vault_id FROM notifications WHERE problem_status='open'").fetchall()

    def assert_foreign_untouched(self) -> None:
        for key in self.foreign:
            self.assertIn(key, self.provider.objects)
        self.assertFalse([d for d in self.provider.deleted if not d[0].startswith(self.prefix)])


class VaultDeletionTests(VaultDeletionFixture):
    # -- preview -------------------------------------------------------------

    def test_preview_lists_the_whole_prefix_and_nothing_else(self) -> None:
        self.stop()
        summary = self.preview()
        self.assertEqual(summary["prefix"], self.prefix)
        self.assertEqual((summary["object_count"], summary["version_count"],
                          summary["delete_marker_count"], summary["byte_count"]),
                         (3, 2, 1, len(A) + STRAY))
        classes = {c["storage_class"]: c for c in summary["by_storage_class"]}
        self.assertEqual(set(classes), {"STANDARD", "DEEP_ARCHIVE"})
        self.assertEqual(classes["STANDARD"]["early_deletion_cost"], 0)
        # Written now: 173 of its 180 minimum days remain at the earliest deletion.
        self.assertEqual((classes["DEEP_ARCHIVE"]["minimum_days"],
                          classes["DEEP_ARCHIVE"]["early_deletion_version_count"]), (180, 1))
        self.assertGreater(classes["DEEP_ARCHIVE"]["early_deletion_cost"], 0)
        self.assertEqual(summary["early_deletion_cost"], classes["DEEP_ARCHIVE"]["early_deletion_cost"])
        self.assertEqual(self.provider.deleted, [])

    def test_the_early_deletion_counts_from_the_class_entry_not_the_upload(self) -> None:
        """#731: uploaded 200 days ago, in Deep Archive only for 10 days: still
        within its minimum, though LastModified says otherwise."""
        self.stop()
        now = datetime.now(timezone.utc)
        [version] = self.rows("""SELECT av.object_key, av.provider_version_id FROM archive_versions av
                                 JOIN files f ON f.id=av.file_id WHERE f.vault_id=2""")
        key = (version["object_key"], version["provider_version_id"])
        self.provider.classes[key] = "DEEP_ARCHIVE"
        self.provider.modified[key] = now - timedelta(days=200)
        with self.db() as connection:
            connection.execute(
                """UPDATE archive_versions SET storage_class='DEEP_ARCHIVE', created_at=%s,
                       storage_class_since=%s WHERE object_key=%s AND provider_version_id=%s""",
                (now - timedelta(days=200), now - timedelta(days=10), *key))

        without_catalog = self.preview()
        self.assertEqual(without_catalog["early_deletion_version_count"], 1, "only the stray, from LastModified")

        summary = vault_deletion.preview(self.provider, self.target(), database=self.db, now=now)
        deep = {c["storage_class"]: c for c in summary["by_storage_class"]}["DEEP_ARCHIVE"]
        self.assertEqual((deep["early_deletion_version_count"], deep["early_deletion_byte_count"]),
                         (2, len(A) + STRAY))
        self.assertEqual((summary["early_deletion_version_count"], summary["early_deletion_byte_count"]),
                         (2, len(A) + STRAY))
        self.assertEqual(summary["early_deletion_estimated_count"], 1, "the stray's entry is estimated")
        stray_written = self.provider.modified[self.prefix + "stray/old.bin", self.stray]
        self.assertEqual(summary["savings_from"], (stray_written + timedelta(days=180)).isoformat())
        self.assertGreater(summary["early_deletion_cost"], without_catalog["early_deletion_cost"])

        # A class the catalog did not record is estimated from the Storage Plan.
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET storage_class='STANDARD' WHERE object_key=%s", (key[0],))
            connection.execute(
                """UPDATE vaults SET storage_plan='{"transitions": [{"days": 30, "storage_class": "DEEP_ARCHIVE"}]}'::jsonb
                   WHERE id=2""")
        planned = vault_deletion.preview(self.provider, self.target(), database=self.db, now=now)
        self.assertEqual((planned["early_deletion_version_count"], planned["early_deletion_estimated_count"]), (2, 2),
                         "entered 169 days ago (200 - 30 - 1): 4 days remain after the delay")

    def test_a_failed_listing_gives_no_preview(self) -> None:
        self.stop()

        def refuse(stage, **kwargs):
            if stage == "list":
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "secret"}},
                                  "ListObjectVersions")
        self.provider.hook = refuse
        with self.assertRaises(vault_deletion.VaultDeletionListingError):
            self.preview()

    # -- consent -------------------------------------------------------------

    def test_only_a_tombstone_can_be_deleted(self) -> None:
        with self.db() as connection, self.assertRaises(vault_deletion.VaultDeletionError) as refused:
            vault_deletion.deletion_target(connection, 2)
        self.assertEqual(refused.exception.code, "not_tombstone")
        with self.db() as connection, self.assertRaises(vault_deletion.VaultDeletionError):
            vault_deletion.request_deletion(
                connection, vault_id=2, actor="owner", confirmation="Docs", reason="x",
                preview_summary={"prefix": self.prefix}, requested_at=PAST)
        # The schema refuses it too, whatever the caller.
        with self.db() as connection, self.assertRaises(psycopg.errors.RestrictViolation):
            connection.execute(
                """INSERT INTO vault_deletions(vault_id, status, actor, reason, requested_at,
                                               run_after, preview)
                   VALUES (2, 'pending', 'owner', 'x', now(), now() + interval '7 days', '{}')""")

    def test_the_request_waits_seven_days_and_can_be_cancelled(self) -> None:
        self.stop()
        with self.assertRaises(cloud_deletion.ConfirmationRequired):
            self.request(confirmation="docs")
        state = self.request(requested_at=storage.now_iso())
        self.assertEqual((state["request"]["status"], state["request"]["cancellable"]), ("pending", True))
        row = self.deletion()
        self.assertEqual((row["run_after"] - row["requested_at"]).total_seconds(),
                         cloud_deletion.DELETION_DELAY_SECONDS)
        with self.assertRaises(vault_deletion.VaultDeletionError) as again:
            self.request()
        self.assertEqual(again.exception.code, "already_requested")
        self.assertEqual(self.run_due(), 0)
        self.assertEqual(self.provider.deleted, [], "nothing runs before the delay")
        # The schema refuses a shorter delay.
        with self.db() as connection, self.assertRaises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE vault_deletions SET run_after=requested_at WHERE vault_id=2")
        with self.db() as connection:
            state = vault_deletion.cancel(connection, vault_id=2, actor="owner")
        self.assertIsNone(state["request"])
        self.assertIsNone(self.deletion())
        self.assertEqual(self.run_due(), 0)
        self.assertEqual(self.provider.deleted, [])
        events = [r["event"] for r in self.rows(
            "SELECT event FROM audit_events WHERE event LIKE 'folder_protection.vault_deletion%%' ORDER BY id")]
        self.assertEqual(events, ["folder_protection.vault_deletion_requested",
                                  "folder_protection.vault_deletion_cancelled"])

    # -- execution -----------------------------------------------------------

    def test_the_elapsed_deletion_removes_the_prefix_then_the_vault(self) -> None:
        with self.db() as connection:
            inbox.set_problem(connection, key="delay:2", event="protection_delayed", vault_id=2,
                              active=True, params={"pending": 1, "stalled": 0, "overdue": 0, "unstable": 0})
        self.stop()
        self.request()
        self.assertEqual(self.run_due(), 1)
        self.assertEqual(self.under_prefix(), [])
        self.assert_foreign_untouched()
        self.assertIn((self.prefix + "gone.txt", "marker-1"), self.provider.deleted)
        for table in ("vaults WHERE id=2", "files WHERE vault_id=2", "jobs WHERE vault_id=2",
                      "archive_versions", "upload_attempts", "upload_snapshots",
                      "notifications WHERE vault_id=2", "vault_deletions"):
            self.assertEqual(self.rows(f"SELECT 1 FROM {table}"), [], table)
        with self.db() as connection:
            self.assertEqual([v["id"] for v in list_vault_inventory(connection)], [])
        [audit] = self.rows("SELECT * FROM audit_events WHERE event='folder_protection.vault_deleted'")
        self.assertEqual((audit["actor"], audit["vault_id"], audit["outcome"]), ("owner", 2, "success"))
        detail = audit["detail"]
        self.assertEqual((detail["vault_uuid"], detail["vault_name"], detail["deleted_versions"],
                          detail["deleted_markers"], detail["deleted_bytes"], detail["files"]),
                         (str(self.uuid), "Docs", 2, 1, len(A) + STRAY, 1))
        self.assertTrue(self.file.exists(), "a Local Copy is never touched")
        self.assertEqual(self.file.read_bytes(), A)

    def test_a_refused_delete_keeps_the_tombstone_with_an_open_problem_and_retry_runs_at_once(self) -> None:
        self.stop()
        self.request()
        original = self.provider.delete_objects
        self.provider.delete_objects = lambda **kwargs: {"Errors": [{"Code": "AccessDenied"}]}
        self.assertEqual(self.run_due(), 0)
        row = self.deletion()
        self.assertEqual((row["status"], row["failure"]["reason"]), ("failed", "provider_refused"))
        self.assertEqual(self.rows("SELECT lifecycle FROM vaults WHERE id=2")[0]["lifecycle"], "tombstone")
        self.assertEqual(self.open_problems(), [{"event": "vault_deletion_failed", "vault_id": 2}],
                         "the failure survives the stopped-Vault rule (#715)")
        self.assertEqual(self.run_due(), 0, "a failed deletion waits for the Owner")
        self.provider.delete_objects = original
        with self.db() as connection:
            state = vault_deletion.retry(connection, vault_id=2, actor="owner")
        self.assertEqual(state["request"]["status"], "pending")
        self.assertEqual(self.open_problems(), [])
        self.assertEqual(self.run_due(), 1, "a retry needs no new delay")
        self.assertEqual(self.under_prefix(), [])
        self.assertEqual(self.rows("SELECT 1 FROM vaults WHERE id=2"), [])

    def test_leftovers_after_the_deletion_fail_without_removing_the_catalog(self) -> None:
        self.stop()
        self.request()

        def rewrite(stage, **kwargs):
            if stage == "after_delete_objects":
                self.provider.hook = lambda stage, **kwargs: None
                self.provider.add(self.prefix + "late.bin", b"late")
        self.provider.hook = rewrite
        self.assertEqual(self.run_due(), 0)
        row = self.deletion()
        self.assertEqual((row["status"], row["failure"]["reason"], row["failure"]["leftover_versions"]),
                         ("failed", "leftovers", 1))
        self.assertEqual(len(self.rows("SELECT 1 FROM files WHERE vault_id=2")), 1)
        with self.db() as connection:
            vault_deletion.cancel(connection, vault_id=2, actor="owner")
        self.assertEqual(self.open_problems(), [])
        self.assertEqual(self.rows("SELECT lifecycle FROM vaults WHERE id=2")[0]["lifecycle"], "tombstone")

    def test_a_key_outside_the_prefix_stops_the_run_before_any_delete(self) -> None:
        self.stop()
        self.request()
        listing = self.provider.list_object_versions

        def leaking(**kwargs):
            page = listing(**kwargs)
            page["Versions"] = page["Versions"] + [{"Key": f"vaults/{OTHER_UUID}/immich.jpg",
                                                   "VersionId": "x", "Size": 1}]
            return page
        self.provider.list_object_versions = leaking
        self.assertEqual(self.run_due(), 0)
        self.assertEqual(self.provider.deleted, [])
        self.assertEqual(self.deletion()["failure"]["reason"], "outside_prefix")
        self.assert_foreign_untouched()

    def test_a_running_deletion_cannot_be_cancelled_and_a_stale_one_is_claimed_again(self) -> None:
        self.stop()
        self.request()
        with self.db() as connection:
            self.assertEqual(vault_deletion.claim_due(connection, now=datetime.now(timezone.utc)), [2])
        with self.db() as connection, self.assertRaises(vault_deletion.VaultDeletionError) as refused:
            vault_deletion.cancel(connection, vault_id=2, actor="owner")
        self.assertEqual(refused.exception.code, "running")
        with self.db() as connection:
            self.assertEqual(vault_deletion.claim_due(connection, now=datetime.now(timezone.utc)), [],
                             "a live run is not claimed twice")
            connection.execute(
                "UPDATE vault_deletions SET heartbeat_at=now() - interval '1 hour' WHERE vault_id=2")
        self.assertEqual(self.run_due(), 1)
        self.assertEqual(self.rows("SELECT attempts FROM vault_deletions"), [])


class VaultDeletionHttpTests(VaultDeletionFixture):
    def setUp(self) -> None:
        super().setUp()
        configured = replace(settings, database_dsn=database_dsn(self.db_path), cookie_secure=False)
        for target in ("app.main.settings", "app.database.settings", "app.sessions.settings"):
            self.enterContext(patch(target, configured))
        self.enterContext(patch("app.main.s3_client", return_value=self.provider))
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))
        with self.db() as connection:
            insert_owner(connection)
            token = create_session(connection, auth_method="oidc")
            csrf = csrf_token_for(connection, token)
        self.client.cookies.set(settings.session_cookie_name, token)
        self.headers = {"X-CSRF-Token": csrf}
        self.payload = {"confirmation": "Docs", "reason": "test vault no longer needed"}

    def reauth(self, recent: bool) -> None:
        with self.db() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (
                datetime.now(timezone.utc).isoformat() if recent else "2000-01-01T00:00:00+00:00",))

    def test_http_requires_reauth_a_tombstone_and_a_listing(self) -> None:
        url = "/api/vaults/2/vault-deletion"
        self.reauth(True)
        active = self.client.post(url, json=self.payload, headers=self.headers)
        self.assertEqual((active.status_code, active.json()["message_key"]),
                         (409, "api.vault_deletion.not_tombstone"))
        self.stop()
        self.reauth(False)
        denied = self.client.post(url, json=self.payload, headers=self.headers)
        self.assertEqual((denied.status_code, denied.json()), (403, {"error": "reauth_required"}))
        self.reauth(True)
        preview = self.client.get(url + "/preview")
        self.assertEqual((preview.status_code, preview.json()["version_count"]), (200, 2))
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("down")) \
            if stage == "list" else None
        failed = self.client.post(url, json=self.payload, headers=self.headers)
        self.assertEqual((failed.status_code, failed.json()["message_key"]),
                         (502, "api.vault_deletion.listing_failed"))
        self.assertIsNone(self.deletion(), "no listing, no confirmation")
        self.provider.hook = lambda stage, **kwargs: None
        wrong = self.client.post(url, json={**self.payload, "confirmation": "docs"}, headers=self.headers)
        self.assertEqual(wrong.status_code, 422, wrong.text)
        scheduled = self.client.post(url, json=self.payload, headers=self.headers)
        self.assertEqual(scheduled.status_code, 202, scheduled.text)
        self.assertEqual(scheduled.json()["request"]["status"], "pending")
        self.assertEqual(scheduled.json()["request"]["preview"]["version_count"], 2)
        listed = self.client.get("/api/general/vaults").json()["items"]
        self.assertEqual([v["vault_deletion_status"] for v in listed], ["pending"])
        self.reauth(False)  # Cancellation never requires step-up.
        cancelled = self.client.post(url + "/cancel", headers=self.headers)
        self.assertEqual((cancelled.status_code, cancelled.json()["request"]), (200, None))
        self.assertEqual(self.client.get("/api/vaults/99/vault-deletion").status_code, 404)


if __name__ == "__main__":
    unittest.main()
