"""What the browse and overview routes answer, key by key (ADR-0056, #805).

Every JSON operation of ``app/routes/browse.py`` is called once on a seeded
installation. The shared client checks each answer against the model the route
declares; the key sets below pin what a route answers today, so a model can
only follow the answer and never the other way round.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.routing import APIRoute

from app.catalog import ArchiveCatalog
from app.services.local_copies import LocalCopies
from app.routes import browse
from app.services import directory_aggregates as aggregates
from app.services import notifications
from app.services.metadata_backups import record_backup_run
from tests import catalog_probe
from tests.catalog_support import fingerprint
from tests.fixtures import FrostVaultTestCase, _response_contract, override_settings, seed_vault
from tests.test_metadata_backups import RecordingObjectStore

T = "2026-11-03T10:00:00+00:00"

FILE_KEYS = {
    "type", "id", "name", "path", "local_exists", "local_size", "modified_at", "local_file_type",
    "cloud_exists", "cloud_size", "storage_class", "integrity", "availability",
    "current_version_id", "pending_version_id", "recoverable", "upload_verification_requested",
    "verification_method", "verification_fallback_reason", "verification_egress_bytes",
    "state", "last_known_state", "last_known_at", "previous_snapshot_protected",
    "cloud_only_conflict", "in_excluded_folder", "reconciliation_block", "cleanup",
    "upload_eligible", "recovery", "recover_eligible", "cleanup_eligible",
}
NOTIFICATION_KEYS = {
    "id", "vault_id", "job_id", "event", "title", "body", "title_key", "body_key",
    "message_params", "in_app_enabled", "dedupe_key", "created_at", "read", "read_at",
    "problem_status", "resolved_at",
}
STAGE_KEYS = {"files", "bytes", "transferred_bytes"}
SUMMARY_KEYS = {
    "stability_wait", "queued", "uploading", "verifying", "bulk_admission",
    "awaiting_admission_files", "next_stable_at", "throughput", "expected_finish", "stopped_since",
}
VERIFICATION_KEYS = {"last_verified", "latest", "pending_run_id", "automatic", "next_verification_at"}
RUN_KEYS = {
    "id", "created_at", "finished_at", "reason", "backend", "status", "digest_sha256",
    "database_sha256", "local_path", "s3_key", "s3_bucket", "size_bytes", "error_message",
    "verified_at",
}
KIT_KEYS = {
    "configured", "fingerprint", "system_bucket", "awaiting_backup", "custody_acknowledged",
    "acknowledged_at", "metadata_restore",
}


class BrowseResponsesTests(FrostVaultTestCase):
    owner = {"password": "correct horse battery"}

    def setUp(self) -> None:
        super().setUp()
        backups = self.tmp_path / "backups"
        backups.mkdir()
        self.enterContext(override_settings(
            archive_master_key=Fernet.generate_key().decode("ascii"),
            metadata_backup_dir=str(backups), metadata_backup_bucket="test-bucket",
            metadata_backup_s3_prefix="system/backups/",
        ))
        self.enterContext(patch(
            "app.services.metadata_backups.default_object_store",
            return_value=RecordingObjectStore(),
        ))
        aggregates.set_maintenance_scheduling_enabled(False)
        self.addCleanup(aggregates.set_maintenance_scheduling_enabled, True)
        with self.database.connect() as connection:
            seed_vault(connection, id=9, name="Files", source_root="/source")
            catalog = ArchiveCatalog(connection)
            self.seed(catalog, "photos/protected.jpg", archive=True)
            self.seed(catalog, "photos/cloud.jpg", archive=True, local=False)
            self.seed(catalog, "note.txt")
            aggregates.rebuild_vault_directory_aggregates(connection, 9)
            notifications.enqueue_notification(
                connection, vault_id=9, event="upload_verified", title="Upload verified", body="ok")
            notifications.set_problem(
                connection, key="blocked:9:source", event="protection_blocked", vault_id=9,
                active=True, params={"component": "source"})
        self.client = self.http_client()
        self.sign_in(send_csrf_header=True, reauthenticated_until="2099-01-01T00:00:00+00:00")

    @staticmethod
    def seed(catalog: ArchiveCatalog, path: str, *, archive: bool = False, local: bool = True) -> None:
        """A Vault File at ``path``; ``archive`` gives it a verified Current."""
        local_copies = LocalCopies(catalog.connection)
        file_id = local_copies.observe_local_copy(
            vault_id=9, path=path, file_type="regular", size=12, mtime_ns=1, observed_at=T)
        if archive:
            fingerprint(catalog, 9, path, "a" * 64)
            version = catalog.begin_replacement(
                file_id, object_key=f"vaults/x/{path}", provider_version_id=path, size=12,
                storage_class="STANDARD", created_at=T)
            catalog.promote_replacement(
                file_id, archive_version_id=version, plaintext_sha256="a" * 64, verified_at=T,
                local_generation=catalog_probe.get_file_by_path(catalog, 9, path)["generation"])
        if not local:
            local_copies.mark_local_copy_missing(file_id, observed_at=T)

    def answer(self, method: str, url: str, **options: Any) -> Any:
        response = self.client.request(method, url, **options)
        self.assertEqual(response.status_code, 200, f"{method} {url}: {response.text}")
        self.assertEqual(response.headers["content-type"], "application/json")
        return response.json()

    def test_every_json_route_declares_the_model_of_its_answer(self) -> None:
        raw = {"/api/recovery-kit/download", "/api/general/metadata-backups/download/{run_id}"}
        for route in browse.router.routes:
            assert isinstance(route, APIRoute)
            with self.subTest(route.path):
                self.assertEqual(_response_contract(route) is None, route.path in raw)

    def test_file_listing_detail_and_search(self) -> None:
        listing_keys = {"items", "total", "page", "directory", "mode", "aggregate_status"}
        root = self.answer("GET", "/api/vaults/9/files")
        self.assertEqual(set(root), listing_keys | {"manual_upload"})
        self.assertEqual((root["total"], root["page"], root["directory"], root["mode"]),
                         (2, 1, "", "browse"))
        folder, file = root["items"]
        self.assertEqual(set(folder), {
            "type", "name", "path", "item_count", "total_size", "local_size", "cloud_size",
            "storage_class", "storage_class_count", "state", "state_counts",
            "last_known_states", "last_known_at", "available_actions",
        })
        self.assertEqual((folder["type"], folder["path"], folder["item_count"]),
                         ("directory", "photos", 2))
        self.assertEqual(set(file), FILE_KEYS)
        self.assertEqual((file["type"], file["path"], file["state"], file["local_exists"]),
                         ("file", "note.txt", "pending", 1))

        inside = self.answer("GET", "/api/vaults/9/files?directory=photos&state=cloud_only")
        self.assertEqual([item["path"] for item in inside["items"]], ["photos/cloud.jpg"])
        self.assertEqual(set(inside["items"][0]), FILE_KEYS)
        self.assertEqual(inside["items"][0]["cloud_exists"], 1)

        found = self.answer("GET", "/api/vaults/9/files?q=jpg")
        self.assertEqual(set(found), listing_keys | {"manual_upload"})
        self.assertEqual((found["mode"], found["total"]), ("search", 2))

        detail = self.answer("GET", "/api/vaults/9/files/detail?path=photos/protected.jpg")
        self.assertEqual(set(detail), FILE_KEYS)
        self.assertEqual((detail["state"], detail["recoverable"]), ("protected", True))

        everywhere = self.answer("GET", "/api/files/search?q=jpg")
        self.assertEqual(set(everywhere), listing_keys)
        self.assertEqual(everywhere["total"], 2)
        self.assertEqual(set(everywhere["items"][0]), FILE_KEYS | {"vault_id", "vault_name"})
        self.assertEqual(set(self.answer("GET", "/api/files/search")), listing_keys)

    def test_vault_statistics(self) -> None:
        stats = self.answer("GET", "/api/vaults/9/stats")
        self.assertEqual(set(stats), {
            "states", "protection_state", "last_known_states", "storage", "active_jobs",
            "manual_upload", "runtime", "source_volume",
        })
        self.assertEqual(sum(stats["states"].values()), 3)
        self.assertEqual(set(stats["storage"]), {"local_bytes", "cloud_bytes"})
        self.assertEqual(set(stats["source_volume"]), {"alias", "health", "local_operations_allowed"})
        self.assertLessEqual({
            "scanning", "last_scan", "last_error", "last_error_source_cause", "scan_queued",
            "scan_queued_at", "scan_waiting_for", "scan_waiting_since", "server_priority_enabled",
            "server_priority_throttled", "server_priority_throttled_since",
            "source_device_read_limit", "source_devices",
        }, set(stats["runtime"]))

    def test_upload_activity(self) -> None:
        activity = self.answer("GET", "/api/upload-activity")
        self.assertEqual(set(activity), {"observed_at", "global", "vaults", "server_priority"})
        self.assertEqual(set(activity["server_priority"]), {"throttled", "since"})
        self.assertEqual(set(activity["global"]), SUMMARY_KEYS)
        (vault,) = activity["vaults"]
        self.assertEqual(set(vault), SUMMARY_KEYS | {"vault_id", "vault_name", "paused"})
        for summary in (activity["global"], vault):
            for stage in ("stability_wait", "queued", "uploading", "verifying"):
                self.assertEqual(set(summary[stage]), STAGE_KEYS)
            self.assertEqual(set(summary["bulk_admission"]),
                             {"requests", "selected_files", "processed_files", "admitted_files"})
            self.assertEqual(set(summary["throughput"]), {
                "sample_seconds", "instant_seconds", "average_seconds", "history_seconds",
                "upload", "verification", "history",
            })
            self.assertEqual(set(summary["throughput"]["upload"]),
                             {"bytes_per_second", "files_per_second", "average_bytes_per_second"})
            self.assertEqual(set(summary["throughput"]["verification"]),
                             {"bytes_per_second", "average_bytes_per_second"})

    def test_overview(self) -> None:
        overview = self.answer("GET", "/api/general")
        self.assertEqual(set(overview), {
            "vaults", "open_problem_count", "month", "monthly_estimate", "previous_month_delta",
            "currency", "pricing_effective_at", "email_configured", "runs_as_root",
            "dac_override", "cloud_preflight", "last_verified_backup",
            "metadata_backup_verification",
        })
        self.assertEqual([set(vault) for vault in overview["vaults"]], [
            {"id", "uuid", "name", "lifecycle", "monthly_estimate", "open_problem_count"}])
        self.assertEqual(overview["open_problem_count"], 1)
        self.assertEqual(overview["cloud_preflight"],
                         {"status": "unknown", "remediation": None, "next_probe_at": None})
        self.assertEqual(set(overview["metadata_backup_verification"]), VERIFICATION_KEYS)

    def test_the_gate_answers_its_whole_stored_row_in_the_overview(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO cloud_preflight_state(id, status, remediation, failure_count, next_probe_at)"
                " VALUES (1, 'blocked', 'S3 access was refused', 3, '2000-01-01T00:00:00+00:00')")
        gate = self.answer("GET", "/api/general")["cloud_preflight"]
        self.assertLess({"status", "remediation", "next_probe_at"}, set(gate))
        self.assertEqual((gate["status"], gate["failure_count"]), ("blocked", 3))

    def test_problems_and_notifications(self) -> None:
        problems = self.answer("GET", "/api/problems")
        # Not the notification list: no unread count, no paging flag, no email state.
        self.assertEqual(set(problems), {"items", "open_problem_count"})
        self.assertEqual(problems["open_problem_count"], 1)
        (problem,) = problems["items"]
        self.assertEqual(set(problem), NOTIFICATION_KEYS | {"resolving"})
        self.assertEqual((problem["event"], problem["problem_status"], problem["resolving"]),
                         ("protection_blocked", "open", None))

        listed = self.answer("GET", "/api/notifications?limit=1")
        self.assertEqual(set(listed), {
            "items", "unread_count", "has_more", "open_problem_count", "email_configured"})
        self.assertEqual((listed["unread_count"], listed["has_more"], len(listed["items"])),
                         (2, True, 1))
        # ``user_id`` is not answered: there is one Owner.
        self.assertEqual(set(listed["items"][0]), NOTIFICATION_KEYS)

        read = self.answer("POST", "/api/notifications/read",
                           json={"notification_id": listed["items"][0]["id"]})
        self.assertEqual(set(read), NOTIFICATION_KEYS)
        self.assertTrue(read["read"])
        self.assertEqual(self.answer("POST", "/api/notifications/read-all"),
                         {"marked_count": 1, "unread_count": 0})

    def test_a_running_scan_is_named_on_the_problem_it_can_close(self) -> None:
        with patch.dict("app.routes.browse.runtime_status", {9: {"scanning": True}}):
            (problem,) = self.answer("GET", "/api/problems")["items"]
        self.assertEqual(problem["resolving"], "scan")

    def test_recovery_kit_status_and_custody(self) -> None:
        waiting = self.answer("GET", "/api/recovery-kit")
        self.assertEqual(set(waiting), KIT_KEYS)
        # No backup has reached the System Bucket yet: the kit is not offered.
        self.assertEqual((waiting["configured"], waiting["fingerprint"], waiting["awaiting_backup"],
                          waiting["metadata_restore"]), (False, None, True, None))
        self.assertEqual(set(waiting["system_bucket"]), {"name", "region", "origin", "ready"})

        with self.database.connect() as connection:
            record_backup_run(connection, reason="manual", backend="postgresql", status="succeeded",
                              s3_key="system/backups/a.bak.enc", s3_bucket="test-bucket")
            connection.execute(
                "INSERT INTO metadata_restore(id, backup_created_at, recorded_at) VALUES (1, %s, %s)",
                (T, T))
        status = self.answer("GET", "/api/recovery-kit")
        self.assertEqual(set(status), KIT_KEYS)
        self.assertEqual((status["configured"], status["awaiting_backup"],
                          status["custody_acknowledged"], status["acknowledged_at"]),
                         (True, False, False, None))
        self.assertEqual(status["metadata_restore"], {"backup_created_at": T, "updated_files": 0})
        with self.assertLogs("app.audit", level="WARNING"):
            kept = self.answer("POST", "/api/recovery-kit/custody",
                               json={"fingerprint": status["fingerprint"]})
        self.assertEqual(set(kept), KIT_KEYS)
        self.assertTrue(kept["custody_acknowledged"])
        self.assertIsInstance(kept["acknowledged_at"], str)

    def test_metadata_backups(self) -> None:
        empty = self.answer("GET", "/api/general/metadata-backups")
        self.assertEqual(set(empty), {"status", "runs"})
        status_keys = {"last_status", "last_run", "succeeded_count", "failed_count",
                       "verification", "system_bucket", "off_host"}
        self.assertEqual(set(empty["status"]), status_keys)
        self.assertEqual((empty["status"]["last_status"], empty["status"]["last_run"], empty["runs"]),
                         ("never", None, []))
        self.assertEqual(set(empty["status"]["verification"]), VERIFICATION_KEYS)

        with self.assertLogs("app.audit", level="WARNING"):
            ran = self.answer("POST", "/api/general/metadata-backups/run",
                              json={"reason": "operator requested backup"})
        self.assertEqual(set(ran), {
            "ok", "reason", "path", "digest_sha256", "database_sha256", "backend", "s3_key",
            "s3_bucket", "size_bytes", "filename", "created_at",
        })
        listed = self.answer("GET", "/api/general/metadata-backups")
        self.assertEqual(set(listed["status"]), status_keys)
        (run,) = listed["runs"]
        self.assertEqual(set(run), RUN_KEYS)
        self.assertEqual(listed["status"]["last_run"], run)
        self.assertEqual(set(listed["status"]["verification"]["latest"]),
                         {"id", "created_at", "verified_at"})
