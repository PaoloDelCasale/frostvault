"""What the Vault routes answer, key by key (ADR-0056, #805).

Every JSON operation of ``app/routes/vaults.py`` (``router`` and
``inventory_router``) is called on a real installation. The shared client
checks each answer against the model the route declares; the key sets below
pin what a route answers today, so a model can only follow the answer and
never the other way round. Each family reuses the fixture base of the module
that owns its behaviour.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.routing import APIRoute

from app.routes import vaults
from app.services import billed_cost, source_identity, source_layout, system_bucket
from app.services.system_bucket import SystemBucket
from tests.test_billed_cost import FakeExportS3, csv_text, delivered_export, storage_row
from tests.test_bucket_metrics import CatalogFixture, FakeCloudWatch, bucket_points
from tests.test_cloud_deletion import CloudDeletionFixture
from tests.test_mirror_cleanup import MirrorCleanupFixture
from tests.test_vault_creation_http import VaultCreationHttpTestCase
from tests.test_vault_deletion import VaultDeletionFixture
from tests.fixtures import FrostVaultTestCase, _response_contract, seed_vault

BUCKET_KEYS = {
    "vault_id", "bucket", "region", "dedicated", "state", "stage", "attempts", "last_attempt_at",
    "error", "retention_lock_days", "active_at", "active_in_seconds",
}
GENERAL_VAULT_KEYS = {
    "id", "uuid", "name", "source_root", "s3_bucket", "s3_region", "s3_prefix", "lifecycle",
    "paused", "root_released_at", "dedicated_bucket", "retention_lock_days",
    "vault_deletion_status", "bucket_provisioning",
}
STOP_KEYS = {
    "vault_id", "vault_name", "stopped", "root_released", "cloud_disposition", "group_id",
    "cancellable", "pending_until", "local_files_blocking_cloud_delete", "jobs",
}
DELETION_KEYS = {"vault_id", "vault_name", "lifecycle", "deletable", "local_files", "request"}
REQUEST_KEYS = {
    "status", "requested_at", "pending_until", "reason", "attempts", "failed_at", "failure",
    "preview", "cancellable", "retryable", "retained_count", "retained_until", "next_attempt_at",
    "delete_bucket",
}
PREVIEW_KEYS = {
    "prefix", "object_count", "version_count", "delete_marker_count", "multipart_upload_count",
    "byte_count", "by_storage_class", "early_deletion_cost", "currency", "pricing_effective_at",
    "region", "executes_at", "delay_seconds", "early_deletion_version_count",
    "early_deletion_byte_count", "savings_from", "early_deletion_estimated_count", "retention_lock",
}
DELETION_CLASS_KEYS = {
    "storage_class", "version_count", "byte_count", "early_deletion_cost",
    "early_deletion_version_count", "minimum_days", "early_deletion_byte_count",
}
CLEANUP_KEYS = {
    "enabled", "grace_days", "grace_days_min", "grace_days_max", "cap_files", "cap_bytes",
    "beyond_minimum", "last_run_at",
    "next_run_at", "last_outcome", "pending", "current", "waiting", "ready", "unseen", "totals",
    "held", "minimum_storage", "retention_lock",
}
STATUS_FIELDS = {
    "state", "bucket", "prefix", "checked_at", "next_check_at", "read_at", "exported_at",
    "data_through", "currency", "stale", "error_code", "error_detail",
}
CATEGORIES = {"storage", "requests", "transitions", "retrievals", "data_transfer", "other", "credits"}


class RouteContractTests(unittest.TestCase):
    def test_every_route_declares_the_model_of_its_answer(self) -> None:
        for route in [*vaults.router.routes, *vaults.inventory_router.routes]:
            assert isinstance(route, APIRoute)
            with self.subTest(route.path):
                self.assertIsNotNone(_response_contract(route))


class CreationResponsesTests(VaultCreationHttpTestCase):
    """Creation, bucket, Retention Lock, pause and the inventory."""

    def setUp(self) -> None:
        super().setUp()
        self.headers = self.sign_in().csrf_headers

    def answer(self, method: str, url: str, status: int = 200, **options: Any) -> Any:
        response = self.client.request(method, url, headers=self.headers, **options)
        self.assertEqual(response.status_code, status, f"{method} {url}: {response.text}")
        self.assertEqual(response.headers["content-type"], "application/json")
        return response.json()

    def reauthenticate(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (datetime.now(timezone.utc),))

    def test_an_active_vault_with_a_retention_lock(self) -> None:
        created = self.answer("POST", "/api/vaults", 201,
                              json={"name": "Photos", "retention_lock_days": 30})
        self.assertEqual(set(created), {"id", "uuid", "name", "creation_mode", "lifecycle", "bucket"})
        self.assertEqual((created["creation_mode"], created["lifecycle"]), ("empty", "active"))
        self.assertEqual(set(created["bucket"]), BUCKET_KEYS)
        self.assertEqual((created["bucket"]["state"], created["bucket"]["retention_lock_days"]),
                         ("ready", 30))
        vault_id = created["id"]

        listed = self.answer("GET", "/api/vaults")
        self.assertEqual(set(listed), {"items"})
        self.assertEqual([set(item) for item in listed["items"]],
                         [{"id", "uuid", "name", "paused", "lifecycle"}])

        self.assertEqual(set(self.answer("GET", f"/api/vaults/{vault_id}/bucket")), BUCKET_KEYS)
        locked = self.answer("GET", f"/api/vaults/{vault_id}/retention-lock")
        self.assertEqual(set(locked), {"vault_id", "days", "mode", "retained"})
        self.assertEqual((locked["days"], locked["mode"]), (30, "GOVERNANCE"))
        self.assertEqual(set(locked["retained"]), {
            "count", "bytes", "deletable_from", "deletable_until", "failed_count",
            "failed_attempts", "last_error", "last_attempt_at", "next_attempt_at"})

        for action, paused in (("pause", True), ("resume", False)):
            answer = self.answer("POST", f"/api/vaults/{vault_id}/{action}")
            self.assertEqual(set(answer), {"id", "paused", "message", "message_key"})
            self.assertEqual(answer["paused"], paused)

        (general,) = self.answer("GET", "/api/general/vaults")["items"]
        self.assertEqual(set(general), GENERAL_VAULT_KEYS)
        self.assertEqual((general["dedicated_bucket"], general["retention_lock_days"],
                          general["root_released_at"], general["bucket_provisioning"]),
                         (True, 30, None, None))

    def test_a_vault_without_a_retention_lock_answers_nulls(self) -> None:
        created = self.answer("POST", "/api/vaults", 201, json={"name": "Docs"})
        locked = self.answer("GET", f"/api/vaults/{created['id']}/retention-lock")
        self.assertEqual(locked, {"vault_id": created["id"], "days": None, "mode": None,
                                  "retained": None})

    def test_a_failed_creation_is_retried_or_cancelled(self) -> None:
        self.s3.fail["put_bucket_versioning"] = "AccessDenied"
        failed = self.answer("POST", "/api/vaults", 201, json={"name": "Travel"})
        self.assertEqual(set(failed["bucket"]), BUCKET_KEYS)
        self.assertEqual((failed["lifecycle"], failed["bucket"]["state"], failed["bucket"]["stage"]),
                         ("provisioning", "failed", "created"))
        self.assertIsInstance(failed["bucket"]["error"], str)
        (general,) = self.answer("GET", "/api/general/vaults")["items"]
        self.assertEqual(set(general), GENERAL_VAULT_KEYS)
        self.assertEqual(set(general["bucket_provisioning"]), BUCKET_KEYS)

        retried = self.answer("POST", f"/api/vaults/{failed['id']}/bucket/retry")
        self.assertEqual(set(retried), BUCKET_KEYS)
        self.assertEqual((retried["state"], retried["error"]), ("ready", None))

        self.s3.fail["put_bucket_versioning"] = "AccessDenied"
        other = self.answer("POST", "/api/vaults", 201, json={"name": "Music"})
        self.reauthenticate()
        cancelled = self.answer("POST", f"/api/vaults/{other['id']}/creation/cancel")
        self.assertEqual(cancelled, {"vault_id": other["id"], "vault_name": "Music",
                                     "bucket": other["bucket"]["bucket"], "region": "eu-south-1",
                                     "bucket_left": "created"})


class StopProtectionResponsesTests(CloudDeletionFixture):
    owner = {}

    def setUp(self) -> None:
        super().setUp()
        self.cloud_only()
        self.client = self.http_client()
        self.headers = self.sign_in().csrf_headers
        with self.db() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (datetime.now(timezone.utc),))

    def test_status_stop_with_deletion_and_its_cancellation(self) -> None:
        url = "/api/vaults/2/stop-protection"
        before = self.client.get(url)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(set(before.json()), STOP_KEYS)
        self.assertEqual((before.json()["stopped"], before.json()["jobs"]), (False, []))

        stopped = self.client.post(url, headers=self.headers, json={
            "cloud_disposition": "delete", "confirmation": "Docs", "reason": "stop"})
        self.assertEqual(stopped.status_code, 202, stopped.text)
        self.assertEqual(set(stopped.json()), STOP_KEYS)
        self.assertTrue(stopped.json()["cancellable"])
        self.assertIsInstance(stopped.json()["group_id"], str)
        self.assertEqual([set(job) for job in stopped.json()["jobs"]],
                         [{"id", "status", "pending_until"}])

        cancelled = self.client.post(url + "/cancel-deletion", headers=self.headers)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(set(cancelled.json()), STOP_KEYS)
        self.assertEqual(cancelled.json()["cloud_disposition"], "keep")


class VaultDeletionResponsesTests(VaultDeletionFixture):
    owner = {}

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch("app.routes.vaults.s3_client", return_value=self.provider))
        self.client = self.http_client()
        self.headers = self.sign_in().csrf_headers
        with self.db() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (datetime.now(timezone.utc),))

    def answer(self, method: str, url: str, status: int = 200, **options: Any) -> Any:
        response = self.client.request(method, url, headers=self.headers, **options)
        self.assertEqual(response.status_code, status, f"{method} {url}: {response.text}")
        return response.json()

    def test_preview_request_cancel_and_retry(self) -> None:
        url = "/api/vaults/2/vault-deletion"
        active = self.answer("GET", url)
        self.assertEqual(set(active), DELETION_KEYS)
        self.assertEqual((active["lifecycle"], active["deletable"], active["request"]),
                         ("active", False, None))
        self.stop()

        preview = self.answer("GET", url + "/preview")
        self.assertEqual(set(preview), PREVIEW_KEYS)
        self.assertEqual({frozenset(entry) for entry in preview["by_storage_class"]},
                         {frozenset(DELETION_CLASS_KEYS)})

        scheduled = self.answer("POST", url, 202,
                                json={"confirmation": "Docs", "reason": "test vault no longer needed"})
        self.assertEqual(set(scheduled), DELETION_KEYS)
        self.assertEqual(set(scheduled["request"]), REQUEST_KEYS)
        self.assertEqual(set(scheduled["request"]["preview"]), PREVIEW_KEYS)
        self.assertEqual((scheduled["request"]["status"], scheduled["request"]["failure"],
                          scheduled["request"]["delete_bucket"]), ("pending", None, False))

        (general,) = self.client.get("/api/general/vaults").json()["items"]
        self.assertEqual(set(general), GENERAL_VAULT_KEYS)
        self.assertEqual((general["lifecycle"], general["vault_deletion_status"]),
                         ("tombstone", "pending"))
        self.assertIsInstance(general["root_released_at"], str)

        self.assertEqual(self.answer("POST", url + "/cancel")["request"], None)

        self.request()
        self.provider.delete_objects = lambda **kwargs: {"Errors": [{"Code": "AccessDenied"}]}
        self.assertEqual(self.run_due(), 0)
        failed = self.answer("GET", url)["request"]
        self.assertEqual(set(failed), REQUEST_KEYS)
        self.assertEqual((failed["status"], failed["failure"]["reason"], failed["retryable"]),
                         ("failed", "provider_refused", True))
        self.assertIsInstance(failed["failed_at"], str)
        retried = self.answer("POST", url + "/retry")
        self.assertEqual(set(retried["request"]), REQUEST_KEYS)
        self.assertEqual(retried["request"]["status"], "pending")


class CleanupResponsesTests(MirrorCleanupFixture):
    owner = {}

    def setUp(self) -> None:
        super().setUp()
        self.client = self.http_client()
        self.headers = self.sign_in().csrf_headers
        with self.db() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (datetime.now(timezone.utc),))

    def answer(self, method: str, url: str, **options: Any) -> Any:
        response = self.client.request(method, url, headers=self.headers, **options)
        self.assertEqual(response.status_code, 200, f"{method} {url}: {response.text}")
        return response.json()

    def test_duplicate_and_mirror_cleanup(self) -> None:
        self.moved("photo.jpg", b"photo-bytes-805")
        for url, keys in (("/api/vaults/2/duplicate-cleanup", CLEANUP_KEYS),
                          ("/api/vaults/2/mirror-cleanup", CLEANUP_KEYS | {"folders", "cloud_life"})):
            with self.subTest(url):
                off = self.answer("GET", url)
                self.assertEqual(set(off), keys)
                self.assertEqual((off["enabled"], off["totals"], off["last_outcome"]), (False, None, None))
                on = self.answer("PUT", url, json={"enabled": True, "beyond_minimum": True})
                self.assertEqual(set(on), keys)
                self.assertTrue(on["enabled"])
                self.assertEqual(set(on["current"]), {"count", "bytes"})
        mirror = self.answer("GET", "/api/vaults/2/mirror-cleanup")
        self.assertEqual(set(mirror["cloud_life"]), {
            "days_after_disappearance", "storage_class", "class_from_day", "minimum_days",
            "minimum_until_day", "beyond_minimum"})

    def test_minimum_storage(self) -> None:
        self.moved("photo.jpg", b"photo-bytes-805")
        summary = self.answer("GET", "/api/vaults/2/minimum-storage")
        self.assertEqual(set(summary), {
            "vault_id", "by_storage_class", "remaining_cost", "releases", "estimated_count",
            "currency", "pricing_effective_at", "region", "disclaimer", "evaluated_at"})
        self.assertIsInstance(summary["disclaimer"], str)
        (standard,) = summary["by_storage_class"]
        self.assertEqual(set(standard), {
            "storage_class", "minimum_days", "within", "beyond", "remaining_cost", "estimated_count"})
        self.assertEqual(set(standard["within"]), {"count", "bytes"})


class CloudUsageResponsesTests(CatalogFixture):
    owner = {}

    def test_measured_and_shared_bucket_usage(self) -> None:
        now = datetime.now(timezone.utc)
        self.run_due({"eu-south-1": FakeCloudWatch(bucket_points(day=now.date() - timedelta(days=1))),
                      "eu-west-1": FakeCloudWatch()}, now=now)
        client = self.http_client()
        self.sign_in(client)
        usage = client.get("/api/vaults/2/cloud-usage")
        self.assertEqual(usage.status_code, 200, usage.text)
        body = usage.json()
        self.assertEqual(set(body), {"vault_id", "bucket", "region", "dedicated_bucket",
                                     "estimate", "measured"})
        self.assertEqual(set(body["estimate"]), {
            "versions", "bytes", "monthly_cost", "currency", "region", "pricing_effective_at",
            "by_storage_class"})
        measured = body["measured"]
        self.assertEqual(set(measured), {
            "status", "last_read", "next_read_after", "history", "day", "read_at", "object_count",
            "bytes", "overhead_bytes", "by_storage_class"})
        self.assertEqual(measured["status"], "available")
        self.assertEqual(set(measured["last_read"]), {"attempted_at", "outcome", "error_code"})
        self.assertEqual(set(measured["history"][0]), {"day", "bytes", "overhead_bytes", "object_count"})
        self.assertEqual({frozenset(entry) for entry in measured["by_storage_class"]},
                         {frozenset({"storage_class", "bytes", "overhead_bytes", "overhead"})})
        overhead = [item for entry in measured["by_storage_class"] for item in entry["overhead"]]
        self.assertEqual({frozenset(item) for item in overhead},
                         {frozenset({"storage_type", "kind", "billed_as", "bytes"})})
        shared = client.get("/api/vaults/3/cloud-usage").json()
        self.assertEqual(shared["measured"], {"status": "shared_bucket"})

    def test_billing_without_a_system_bucket(self) -> None:
        client = self.http_client()
        self.sign_in(client)
        export = client.get("/api/billing-export")
        self.assertEqual(export.status_code, 200, export.text)
        self.assertEqual(set(export.json()), STATUS_FIELDS | {"periods"})
        self.assertEqual((export.json()["state"], export.json()["periods"]), ("unavailable", []))
        billed = client.get("/api/vaults/2/billed-cost")
        self.assertEqual(billed.status_code, 200, billed.text)
        self.assertEqual(set(billed.json()), STATUS_FIELDS | {"vault_id", "vault_bucket", "shared",
                                                              "periods"})


class BilledCostResponsesTests(FrostVaultTestCase):
    owner = {}

    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            seed_vault(connection, id=1, name="Foto", source_root="/sources/foto",
                       bucket="frostvault-foto-ab12cd34", target={"id": 1, "key_layout": "bucket_root"})
            seed_vault(connection, id=2, name="Documenti", source_root="/sources/documenti",
                       bucket="frostvault-documenti-x9y8z7w6",
                       target={"id": 2, "key_layout": "bucket_root"})
            seed_vault(connection, id=3, name="Vecchio", source_root="/sources/vecchio",
                       bucket="paolo-archive", target={"id": 3})
        location = SystemBucket("frostvault-system-k3x9q0ab", "eu-south-1", "created")
        self.enterContext(patch.object(system_bucket, "location", return_value=location))
        now = datetime.now(timezone.utc)
        s3 = FakeExportS3()
        month = now.strftime("%Y-%m")
        delivered_export(s3, month, modified=now - timedelta(hours=2),
                         text=csv_text([storage_row(cost="1.5"),
                                        storage_row("frostvault-system-k3x9q0ab", cost="0.25"),
                                        storage_row("paolo-archive", cost="3")]))
        self.assertTrue(billed_cost.run_due(store=billed_cost.DatabaseStore(self.database.connect),
                                            client_for=lambda region: s3, now=now))

    def test_an_export_read_by_bucket_and_by_vault(self) -> None:
        client = self.http_client()
        self.sign_in(client)
        export = client.get("/api/billing-export")
        self.assertEqual(export.status_code, 200, export.text)
        body = export.json()
        self.assertEqual(set(body), STATUS_FIELDS | {"periods"})
        self.assertEqual(body["state"], "ok")
        (period,) = body["periods"]
        self.assertEqual(set(period), {"period", "total", "vaults_total", "vaults", "installation"})
        self.assertEqual([row["name"] for row in period["vaults"]], ["Foto"])
        self.assertEqual({row["role"] for row in period["installation"]}, {"system", "shared"})
        for row in period["vaults"]:
            self.assertEqual(set(row), {"vault_id", "name", "bucket", "total", "categories"})
            self.assertEqual(set(row["categories"]), CATEGORIES)
        for row in period["installation"]:
            self.assertEqual(set(row), {"role", "bucket", "total", "categories"})

        billed = client.get("/api/vaults/1/billed-cost")
        self.assertEqual(billed.status_code, 200, billed.text)
        vault = billed.json()
        self.assertEqual(set(vault), STATUS_FIELDS | {"vault_id", "vault_bucket", "shared", "periods"})
        (own,) = vault["periods"]
        self.assertEqual(set(own), {"period", "total", "categories"})
        self.assertEqual(set(own["categories"]), CATEGORIES)
        shared = client.get("/api/vaults/3/billed-cost").json()
        self.assertEqual((shared["shared"], shared["periods"]), (True, []))


class SourceVolumeResponsesTests(FrostVaultTestCase):
    owner = {}

    def setUp(self) -> None:
        super().setUp()
        self.sources = self.tmp_path / "sources"
        self.photos = self.sources / "photos"
        for folder in (self.sources / "managed", self.photos / "family", self.photos / "trips"):
            folder.mkdir(parents=True)
        source_layout.override_sources_root(self.sources)
        self.addCleanup(source_layout.reset_sources_root_override)
        mounts = {self.sources, self.photos}
        for patcher in (
            patch.object(source_layout, "path_is_mount", side_effect=lambda path: Path(path) in mounts),
            patch.object(source_layout, "path_is_writable", return_value=True),
            patch.object(source_identity, "fingerprint_for_mount", return_value="mount"),
            patch.object(source_identity, "validate_root_mount_boundary"),
        ):
            self.enterContext(patcher)
        self.own = "11111111-1111-1111-1111-111111111111"
        with self.database.connect() as connection:
            seed_vault(connection, id=7, name="Family", source_root=str(self.photos / "family"))
            seed_vault(connection, id=8, uuid=self.own, name="Managed",
                       source_root=str(self.sources / "managed" / self.own),
                       lifecycle="reconnect_required")
        (self.sources / "managed" / self.own).mkdir()
        self.client = self.http_client()
        self.headers = self.sign_in(self.client).csrf_headers

    def test_inventory_and_folder_browser(self) -> None:
        inventory = self.client.get("/api/general/source-volumes")
        self.assertEqual(inventory.status_code, 200, inventory.text)
        self.assertEqual(set(inventory.json()), {"items"})
        (photos,) = [item for item in inventory.json()["items"] if item["alias"] == "photos"]
        self.assertEqual(set(photos), {
            "alias", "path", "access", "health", "vault_count", "diagnostic", "diagnostic_code"})

        browsed = self.client.get("/api/source-volumes/photos/browse")
        self.assertEqual(browsed.status_code, 200, browsed.text)
        self.assertEqual(set(browsed.json()), {"volume_alias", "relative_path", "items"})
        family, trips = browsed.json()["items"]
        for entry in (family, trips):
            self.assertEqual(set(entry), {"name", "relative_path", "navigable", "selectable",
                                          "occupation"})
        self.assertEqual(family["occupation"], {"kind": "vault", "vault_name": "Family"})
        self.assertIsNone(trips["occupation"])

    def test_reconnect(self) -> None:
        reconnected = self.client.post("/api/vaults/8/reconnect", headers=self.headers, json={
            "volume_alias": "managed", "relative_path": self.own, "reason": "restore"})
        self.assertEqual(reconnected.status_code, 200, reconnected.text)
        self.assertEqual(set(reconnected.json()), {
            "vault_id", "source_root", "reconnect_state", "full_scan_required"})
        self.assertEqual(reconnected.json()["reconnect_state"], "reconnect_required")
