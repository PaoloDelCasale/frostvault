"""The HTTP contract of Job admission, fixed before it left ``main.py`` (#796).

What the Owner's browser sees when it asks for a Cost Estimate or queues an
upload, a Recovery or a local release: the amounts, the Archive Version
chosen, the Restore Approval, every refusal's status and text. These tests go
through the routes only, so they hold whatever module the admission lives in.

Amounts are computed here from the bundled AWS list prices, never copied from
the application: a regenerated price list keeps the tests valid, a changed
formula or rounding does not.
"""
from __future__ import annotations

from unittest.mock import patch

from app.services import source_layout
from app.services.component_health import record_component_health
from app.services.cost_estimates import list_prices
from tests.fixtures import FrostVaultTestCase
from tests.test_recovery_verification import _add_cloud_only_version, _prepare_cloud_only_version

GIB = 1024**3
REGION = "eu-south-1"
# Sizes that are not whole GiB, so a rounded amount differs from an exact one.
DEEP_SIZE = 40 * GIB + 123_457
GLACIER_SIZE = 30 * GIB + 7_919
HELD_MESSAGE = (
    "High-impact Glacier restore held for primary-owner approval; "
    "RestoreObject cannot be cancelled after AWS accepts it"
)


def restore_cost(size: int, storage_class: str, tier: str) -> float:
    return size / GIB * list_prices(REGION).restore_rates[storage_class][tier]


class JobAdmissionHttpFixture(FrostVaultTestCase):
    """Vault 2 with a folder of Cloud-only files in three Storage Classes."""

    settings_overrides = dict(
        aws_region=REGION, restore_tier="Standard", restore_days=3,
        restore_high_impact_gib=100, restore_high_impact_eur=10.0,
        restore_approval_hold_seconds=3600,
    )

    def setUp(self) -> None:
        super().setUp()
        # Cancelling raises process-wide cancel flags by Job id.
        self.enterContext(patch("app.services.job_queue.cancelled_jobs", set()))
        self.source, self.deep_a = _prepare_cloud_only_version(
            self.database, self.tmp_path, relative_path="photos/a.jpg", payload=b"x",
            storage_class="DEEP_ARCHIVE",
        )
        self.deep_b = self.add("photos/b.jpg", "DEEP_ARCHIVE")
        self.glacier = self.add("photos/sub/c.jpg", "GLACIER")
        self.standard = self.add("photos/d.jpg", "STANDARD")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE archive_versions SET size=%s WHERE storage_class='DEEP_ARCHIVE'",
                (DEEP_SIZE,))
            connection.execute(
                "UPDATE archive_versions SET size=%s WHERE storage_class='GLACIER'",
                (GLACIER_SIZE,))
        self.client = self.http_client()
        self.headers = self.sign_in().csrf_headers

    def add(self, path: str, storage_class: str) -> str:
        return _add_cloud_only_version(
            self.database, relative_path=path, payload=b"x", storage_class=storage_class,
            provider_version_id=f"s3-{path}", object_key=f"docs/{path}",
        )

    def post(self, url: str, body: dict):
        return self.client.post(url, json=body, headers=self.headers)

    def sql(self, query: str, params: tuple = ()) -> list[dict]:
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def set_restore_state(self, state: str, storage_class: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE archive_versions SET restore_state=%s WHERE storage_class=%s",
                (state, storage_class))

    def jobs(self) -> dict[str, dict]:
        return {row["path"]: row for row in self.sql(
            """
            SELECT f.path, j.status, j.message, j.archive_version_id::text AS version,
                   jr.tier, jr.days, rr.estimated_cost_eur, rr.estimated_hours,
                   rr.approval_deadline, rr.approved_at
            FROM jobs j JOIN files f ON f.id=j.file_id
            LEFT JOIN job_restores jr ON jr.job_id=j.id
            LEFT JOIN restore_requests rr ON rr.group_id=j.group_id
            WHERE j.action='recover'
            """)}


class RecoverEstimateHttpTests(JobAdmissionHttpFixture):
    def test_single_file_estimate_is_the_exact_list_price_with_its_assumptions(self) -> None:
        prices = list_prices(REGION)
        response = self.post("/api/vaults/2/recover/estimate", {"path": "photos/sub/c.jpg"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "path": "photos/sub/c.jpg",
            "archive_version_id": self.glacier,
            "storage_class": "GLACIER",
            "requires_restore": True,
            "restore_object_irreversible": True,
            "high_impact": False,
            "estimate": {
                "tier": "Standard",
                "days": 3,
                "estimated_cost_eur": restore_cost(GLACIER_SIZE, "GLACIER", "Standard"),
                "estimated_hours": 5.0,
                "pricing_note": "Estimate from AWS list prices; not an AWS Billing quote.",
                "region": REGION,
                "pricing_effective_at": prices.effective_at,
                "assumptions": {
                    **prices.assumptions, "restore_days": 3,
                    "restore_object_irreversible": True,
                },
                "restore_object_irreversible": True,
            },
        })

    def test_single_file_estimate_takes_the_requested_tier_and_days(self) -> None:
        body = self.post("/api/vaults/2/recover/estimate", {
            "path": "photos/sub/c.jpg", "restore_tier": "Expedited", "restore_days": 7,
            "archive_version_id": self.glacier,
        }).json()
        self.assertEqual(body["estimate"]["tier"], "Expedited")
        self.assertEqual(body["estimate"]["days"], 7)
        self.assertEqual(body["estimate"]["estimated_cost_eur"],
                         restore_cost(GLACIER_SIZE, "GLACIER", "Expedited"))
        self.assertEqual(body["estimate"]["estimated_hours"], 5 / 60)
        self.assertEqual(body["estimate"]["assumptions"]["restore_days"], 7)

    def test_single_file_tier_invalid_for_its_class_falls_back_to_bulk(self) -> None:
        body = self.post("/api/vaults/2/recover/estimate", {
            "path": "photos/a.jpg", "restore_tier": "Expedited"}).json()
        self.assertEqual(body["estimate"]["tier"], "Bulk")
        self.assertEqual(body["estimate"]["estimated_cost_eur"],
                         restore_cost(DEEP_SIZE, "DEEP_ARCHIVE", "Bulk"))
        self.assertEqual(body["estimate"]["estimated_hours"], 48.0)

    def test_single_file_estimate_without_cloud_restore_has_no_price(self) -> None:
        response = self.post("/api/vaults/2/recover/estimate", {"path": "photos/d.jpg"})
        self.assertEqual(response.json(), {
            "path": "photos/d.jpg", "archive_version_id": self.standard,
            "storage_class": "STANDARD", "requires_restore": False,
            "restore_object_irreversible": False, "high_impact": False, "estimate": None,
        })

    def test_single_file_high_impact_follows_the_thresholds_and_the_restore_state(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE archive_versions SET size=%s WHERE id=%s",
                               (100 * GIB, self.glacier))
        body = self.post("/api/vaults/2/recover/estimate", {"path": "photos/sub/c.jpg"}).json()
        self.assertTrue(body["high_impact"])
        # A restore already requested is still priced, but needs no approval.
        self.set_restore_state("restoring", "GLACIER")
        body = self.post("/api/vaults/2/recover/estimate", {"path": "photos/sub/c.jpg"}).json()
        self.assertFalse(body["high_impact"])
        self.assertTrue(body["requires_restore"])
        self.assertEqual(body["estimate"]["estimated_cost_eur"],
                         restore_cost(100 * GIB, "GLACIER", "Standard"))

    def test_single_file_estimate_refusals(self) -> None:
        stale = self.post("/api/vaults/2/recover/estimate", {
            "path": "photos/a.jpg", "archive_version_id": self.deep_b})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"], "The current snapshot changed; retry recovery")
        missing = self.post("/api/vaults/2/recover/estimate", {"path": "photos/none.jpg"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "No recoverable Archive Version found")
        outside = self.post("/api/vaults/2/recover/estimate", {"path": "../a.jpg"})
        self.assertEqual(outside.status_code, 422)

    def test_directory_estimate_prices_the_whole_request_once(self) -> None:
        response = self.post("/api/vaults/2/recover/estimate", {
            "path": "photos", "is_directory": True, "restore_tier": "Standard",
            "restore_days": 5})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        cost = body["estimate"].pop("estimated_cost_eur")
        self.assertEqual(body, {
            "path": "photos", "archive_version_id": None, "requires_restore": True,
            "restore_object_irreversible": True, "high_impact": True,
            "estimate": {"tier": "Standard", "days": 5, "estimated_hours": 12.0},
        })
        self.assertAlmostEqual(cost, (
            2 * restore_cost(DEEP_SIZE, "DEEP_ARCHIVE", "Standard")
            + restore_cost(GLACIER_SIZE, "GLACIER", "Standard")), places=9)

    def test_directory_estimate_defaults_and_threshold(self) -> None:
        body = self.post("/api/vaults/2/recover/estimate", {
            "path": "photos/sub", "is_directory": True}).json()
        self.assertEqual(body["estimate"], {
            "tier": "Standard", "days": 3, "estimated_hours": 5.0,
            "estimated_cost_eur": restore_cost(GLACIER_SIZE, "GLACIER", "Standard"),
        })
        self.assertFalse(body["high_impact"])

    def test_directory_estimate_charges_only_restores_not_yet_requested(self) -> None:
        self.set_restore_state("restoring", "DEEP_ARCHIVE")
        body = self.post("/api/vaults/2/recover/estimate", {"path": "photos", "is_directory": True}).json()
        # Only the GLACIER file is charged and counted toward the thresholds;
        # the time is the slowest class of the folder, already requested or not.
        self.assertEqual(body["estimate"], {
            "tier": "Standard", "days": 3, "estimated_hours": 12.0,
            "estimated_cost_eur": restore_cost(GLACIER_SIZE, "GLACIER", "Standard"),
        })
        self.assertFalse(body["high_impact"])
        self.set_restore_state("available", "GLACIER")
        body = self.post("/api/vaults/2/recover/estimate", {"path": "photos", "is_directory": True}).json()
        self.assertTrue(body["requires_restore"])
        self.assertEqual(body["estimate"]["estimated_cost_eur"], 0)
        self.assertEqual(body["estimate"]["estimated_hours"], 12.0)
        self.assertFalse(body["high_impact"])

    def test_directory_estimate_without_archived_files_and_invalid_tier(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE archive_versions SET storage_class='STANDARD'")
        response = self.post("/api/vaults/2/recover/estimate", {"path": "photos", "is_directory": True})
        self.assertEqual(response.json(), {
            "path": "photos", "archive_version_id": None, "requires_restore": False,
            "restore_object_irreversible": False, "high_impact": False, "estimate": None,
        })
        invalid = self.post("/api/vaults/2/recover/estimate", {
            "path": "photos", "is_directory": True, "restore_tier": "Expedited"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"], "Choose Standard or Bulk")

    def test_selection_estimate_counts_overlapping_items_once(self) -> None:
        body = self.post("/api/vaults/2/recover/estimate", {"selection": [
            {"path": "photos/sub", "is_directory": True},
            {"path": "photos/sub/c.jpg"},
            {"path": "photos/a.jpg"},
        ]}).json()
        self.assertEqual(body["path"], "")
        self.assertIsNone(body["archive_version_id"])
        self.assertAlmostEqual(body["estimate"]["estimated_cost_eur"], (
            restore_cost(DEEP_SIZE, "DEEP_ARCHIVE", "Standard")
            + restore_cost(GLACIER_SIZE, "GLACIER", "Standard")), places=9)
        self.assertEqual(body["estimate"]["estimated_hours"], 12.0)
        self.assertFalse(body["high_impact"])


class RecoverAdmissionHttpTests(JobAdmissionHttpFixture):
    def test_single_file_recovery_queues_current_with_the_request_estimate(self) -> None:
        response = self.post("/api/vaults/2/recover", {"path": "photos/sub/c.jpg"})
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        cost = restore_cost(GLACIER_SIZE, "GLACIER", "Standard")
        self.assertEqual(body, {
            "group_id": body["group_id"], "job_ids": body["job_ids"], "item_count": 1,
            "total_bytes": GLACIER_SIZE, "archive_version_id": self.glacier,
            "restore_tier": "Standard", "restore_days": 3,
            "estimated_cost_eur": cost, "estimated_hours": 5.0,
            "approval_required": False,
            "message": "Recovery started", "message_key": "api.recovery_started",
        })
        job = self.jobs()["photos/sub/c.jpg"]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["version"], self.glacier)
        self.assertEqual((job["tier"], job["days"]), ("Standard", 3))
        self.assertEqual(job["estimated_cost_eur"], cost)
        self.assertEqual(job["estimated_hours"], 5.0)
        self.assertIsNone(job["approval_deadline"])

    def test_single_file_recovery_takes_tier_days_and_the_pinned_current(self) -> None:
        body = self.post("/api/vaults/2/recover", {
            "path": "photos/sub/c.jpg", "archive_version_id": self.glacier,
            "restore_tier": "Expedited", "restore_days": 9}).json()
        self.assertEqual(body["archive_version_id"], self.glacier)
        self.assertEqual((body["restore_tier"], body["restore_days"]), ("Expedited", 9))
        self.assertEqual(body["estimated_cost_eur"],
                         restore_cost(GLACIER_SIZE, "GLACIER", "Expedited"))
        self.assertEqual(body["estimated_hours"], 5 / 60)
        job = self.jobs()["photos/sub/c.jpg"]
        self.assertEqual((job["tier"], job["days"]), ("Expedited", 9))

    def test_single_file_recovery_of_a_restore_already_requested_keeps_the_rounded_estimate(self) -> None:
        self.set_restore_state("restoring", "GLACIER")
        body = self.post("/api/vaults/2/recover", {"path": "photos/sub/c.jpg"}).json()
        rounded = round(restore_cost(GLACIER_SIZE, "GLACIER", "Standard"), 6)
        self.assertNotEqual(rounded, restore_cost(GLACIER_SIZE, "GLACIER", "Standard"))
        self.assertEqual(body["estimated_cost_eur"], rounded)
        self.assertEqual(body["estimated_hours"], 5.0)
        self.assertFalse(body["approval_required"])
        job = self.jobs()["photos/sub/c.jpg"]
        self.assertEqual(job["estimated_cost_eur"], rounded)
        self.assertEqual(job["status"], "queued")

    def test_single_file_recovery_without_cloud_restore_has_no_estimate(self) -> None:
        body = self.post("/api/vaults/2/recover", {"path": "photos/d.jpg"}).json()
        self.assertEqual(body["archive_version_id"], self.standard)
        self.assertEqual(
            [body[key] for key in ("restore_tier", "restore_days", "estimated_cost_eur",
                                   "estimated_hours", "approval_required")],
            [None, None, None, None, False])
        self.assertEqual(self.sql("SELECT 1 FROM restore_requests"), [])

    def test_single_file_high_impact_recovery_is_held_for_approval(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE archive_versions SET size=%s WHERE id=%s",
                               (100 * GIB, self.glacier))
        body = self.post("/api/vaults/2/recover", {"path": "photos/sub/c.jpg"}).json()
        self.assertTrue(body["approval_required"])
        self.assertEqual(body["estimated_cost_eur"], restore_cost(100 * GIB, "GLACIER", "Standard"))
        job = self.jobs()["photos/sub/c.jpg"]
        self.assertEqual(job["status"], "pending_approval")
        self.assertEqual(job["message"], HELD_MESSAGE)
        self.assertIsNotNone(job["approval_deadline"])
        self.assertIsNone(job["approved_at"])
        self.assertEqual(len(self.sql(
            "SELECT 1 FROM notifications WHERE event='recovery_approval'")), 1)

        approved = self.post("/api/vaults/2/recover/approve", {"group_id": body["group_id"]})
        self.assertEqual(approved.status_code, 202, approved.text)
        self.assertEqual(approved.json(), {
            "group_id": body["group_id"], "job_ids": body["job_ids"],
            "message": "High-impact restore approved"})
        self.assertEqual(self.jobs()["photos/sub/c.jpg"]["status"], "queued")
        again = self.post("/api/vaults/2/recover/approve", {"group_id": body["group_id"]})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["detail"],
                         "No high-impact restore is waiting for approval (it may have expired)")

    def test_directory_recovery_is_one_request_priced_and_held_on_its_total(self) -> None:
        response = self.post("/api/vaults/2/recover", {
            "path": "photos", "is_directory": True, "restore_tier": "Standard",
            "restore_days": 5})
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        cost = (2 * restore_cost(DEEP_SIZE, "DEEP_ARCHIVE", "Standard")
                + restore_cost(GLACIER_SIZE, "GLACIER", "Standard"))
        self.assertEqual(body["item_count"], 4)
        self.assertEqual(body["total_bytes"], 2 * DEEP_SIZE + GLACIER_SIZE + 1)
        self.assertIsNone(body["archive_version_id"])
        self.assertEqual((body["restore_tier"], body["restore_days"]), ("Standard", 5))
        self.assertAlmostEqual(body["estimated_cost_eur"], cost, places=9)
        self.assertEqual(body["estimated_hours"], 12.0)
        self.assertTrue(body["approval_required"])
        jobs = self.jobs()
        self.assertEqual({job["status"] for job in jobs.values()}, {"pending_approval"})
        self.assertEqual({job["message"] for job in jobs.values()}, {HELD_MESSAGE})
        self.assertEqual((jobs["photos/a.jpg"]["tier"], jobs["photos/a.jpg"]["days"]), ("Standard", 5))
        self.assertIsNone(jobs["photos/d.jpg"]["tier"])
        self.assertEqual(len(self.sql(
            "SELECT 1 FROM notifications WHERE event='recovery_approval'")), 1)

    def test_directory_recovery_below_the_thresholds_is_released_at_once(self) -> None:
        body = self.post("/api/vaults/2/recover", {"path": "photos/sub", "is_directory": True}).json()
        self.assertEqual((body["restore_tier"], body["restore_days"]), ("Standard", 3))
        self.assertEqual(body["estimated_cost_eur"], restore_cost(GLACIER_SIZE, "GLACIER", "Standard"))
        self.assertEqual(body["estimated_hours"], 5.0)
        self.assertFalse(body["approval_required"])
        self.assertEqual(self.jobs()["photos/sub/c.jpg"]["status"], "queued")

    def test_directory_recovery_ignores_restores_already_requested(self) -> None:
        self.set_restore_state("restoring", "DEEP_ARCHIVE")
        body = self.post("/api/vaults/2/recover", {"path": "photos", "is_directory": True}).json()
        self.assertEqual(body["item_count"], 4)
        self.assertEqual(body["estimated_cost_eur"], restore_cost(GLACIER_SIZE, "GLACIER", "Standard"))
        self.assertEqual(body["estimated_hours"], 5.0)
        self.assertFalse(body["approval_required"])

    def test_directory_recovery_without_archived_files_echoes_no_tier(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE archive_versions SET storage_class='STANDARD'")
        body = self.post("/api/vaults/2/recover", {
            "path": "photos", "is_directory": True, "restore_tier": "Standard"}).json()
        self.assertEqual(
            [body[key] for key in ("restore_tier", "restore_days", "estimated_cost_eur",
                                   "estimated_hours", "approval_required")],
            [None, None, None, None, False])

    def test_selection_recovery_queues_each_file_once(self) -> None:
        body = self.post("/api/vaults/2/recover", {"selection": [
            {"path": "photos/sub", "is_directory": True},
            {"path": "photos/sub/c.jpg"},
            {"path": "photos/d.jpg"},
        ]}).json()
        self.assertEqual(body["item_count"], 2)
        self.assertEqual(body["estimated_cost_eur"], restore_cost(GLACIER_SIZE, "GLACIER", "Standard"))
        self.assertEqual(set(self.jobs()), {"photos/sub/c.jpg", "photos/d.jpg"})

    def test_recovery_refusals(self) -> None:
        for body, status, detail in (
            ({"path": "photos/a.jpg", "archive_version_id": self.deep_b}, 409,
             "The selected Archive Version is not recoverable"),
            ({"path": "photos", "is_directory": True, "archive_version_id": self.deep_a}, 422,
             "archive_version_id is only valid for single-file recover"),
            ({"path": "photos", "is_directory": True, "restore_tier": "Expedited"}, 422,
             "Choose Standard or Bulk"),
            ({"path": "photos/none.jpg"}, 409, "No files are eligible for this operation"),
            ({"path": "empty", "is_directory": True}, 409,
             "No files are eligible for this operation"),
        ):
            with self.subTest(body=body):
                response = self.post("/api/vaults/2/recover", body)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["detail"], detail)
        self.assertEqual(self.jobs(), {})
        self.assertEqual(self.sql("SELECT 1 FROM restore_requests"), [])

    def test_a_second_request_on_the_same_file_is_refused(self) -> None:
        self.assertEqual(self.post("/api/vaults/2/recover", {"path": "photos/d.jpg"}).status_code, 202)
        again = self.post("/api/vaults/2/recover", {"path": "photos/d.jpg"})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["detail"], "No files are eligible for this operation")

    def test_recovery_into_a_folder_that_may_not_be_written_is_refused(self) -> None:
        (self.source / "photos").mkdir()
        with patch("app.services.write_access._writable", return_value=False):
            response = self.post("/api/vaults/2/recover", {"path": "photos/d.jpg"})
        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertEqual(body["message_key"], "job.destination_not_writable")
        self.assertEqual(body["message_params"]["path"], str(self.source / "photos"))
        self.assertEqual(self.jobs(), {})


class LocalWorkRefusalHttpTests(JobAdmissionHttpFixture):
    ACTIONS = (
        ("/api/vaults/2/recover", {"path": "photos/d.jpg"}),
        ("/api/vaults/2/upload", {"path": "photos/d.jpg"}),
        ("/api/vaults/2/free-space", {"path": "photos/d.jpg"}),
    )

    def test_a_vault_waiting_for_its_reconnect_scan_admits_no_local_work(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=2")
        for url, body in self.ACTIONS:
            with self.subTest(url=url):
                response = self.post(url, body)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"],
                                 "Local work is suspended pending reconnect scan")

    def test_an_unavailable_source_volume_admits_no_local_work(self) -> None:
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.tmp_path / "sources")
        for url, body in self.ACTIONS:
            with self.subTest(url=url):
                response = self.post(url, body)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["detail"],
                                 "Local storage for this vault is unavailable (unavailable)")

    def test_upload_and_local_release_refusals(self) -> None:
        for url in ("/api/vaults/2/upload", "/api/vaults/2/free-space"):
            with self.subTest(url=url):
                response = self.post(url, {"path": "photos/d.jpg"})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"],
                                 "No files are eligible for this operation")

    def test_local_release_is_refused_while_protection_is_unconfirmed(self) -> None:
        with self.database.connect() as connection:
            record_component_health(
                connection, vault_id=2, component="cloud", ok=False, error="S3 unreachable")
        response = self.post("/api/vaults/2/free-space", {"path": "photos/d.jpg"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {
            "detail": ("Local release blocked: the protection of this Vault is not "
                       "confirmed (see its Problems)"),
            "code": "conflict",
            "message_key": "api.local_release_unconfirmed",
            "message_params": {},
        })

    def test_replacing_a_cloud_only_copy_needs_its_conflict(self) -> None:
        response = self.post("/api/vaults/2/files/cloud-only-conflict/replace", {"path": "photos/d.jpg"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"],
                         "No conflict with a cloud-only file at this path")
