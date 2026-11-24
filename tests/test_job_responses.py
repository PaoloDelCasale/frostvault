"""What the Job and operation routes answer, key by key (ADR-0056, #805).

Every JSON operation of ``app/routes/jobs.py`` is called with a success on a
real catalog: Uploads, a local release, a Recovery with its Restore Approval,
a Cloud Deletion, the Job lists. The shared client checks each answer against
the model the route declares; the key sets below pin what a route answers
today, so a model can only follow the answer and never the other way round.
"""
from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from fastapi.routing import APIRoute

from app import storage
from app.routes import jobs
from app.services.operation_policies import OperationPolicy, queue_auto_uploads, set_policy
from app.services.vaults import VAULT_ROW_SQL
from tests.test_cloud_deletion import CloudDeletionFixture
from tests.fixtures import _response_contract
from tests.test_job_admission_http import JobAdmissionHttpFixture
from tests.test_upload_worker import B

QUEUED_KEYS = {
    "group_id", "job_ids", "item_count", "total_bytes", "archive_version_id", "restore_tier",
    "restore_days", "estimated_cost_eur", "estimated_hours", "message", "message_key",
}
ADMISSION_KEYS = {
    "group_id", "vault_id", "path", "status", "reason", "requested_at", "updated_at", "origin",
    "verification_mode", "selected_count", "processed_count", "admitted_count", "total_bytes",
}
JOB_KEYS = {
    "id", "path", "action", "status", "message", "message_key", "message_params",
    "requested_at", "updated_at", "group_id", "group_path", "total_bytes", "transferred_bytes",
    "archive_version_id", "upload_reconcile_only", "remediation",
}
# The Job list and a group's page also carry the request's columns.
LISTED_JOB_KEYS = JOB_KEYS | {
    "pending_until", "estimated_cost_eur", "estimated_hours", "restore_tier", "restore_days",
    "approved_at",
}
GROUP_KEYS = {
    "id", "path", "action", "status", "message", "message_key", "item_count", "completed_count",
    "failed_count", "resolved_failed_count", "unresolved_failed_count", "cancelled_count",
    "total_bytes", "transferred_bytes", "percent", "updated_at", "requested_at", "pending_until",
    "estimated_cost_eur", "estimated_hours", "restore_tier", "restore_days", "ready_by",
}
REMEDIATION_KEYS = {
    "id", "state", "reason", "remedy", "retry_count", "due_at", "can_resume", "current_job_id",
    "resolved_by_job_id", "resolved_at", "archive_version_id", "original_job_ids",
}
VERSION_KEYS = {
    "id", "role", "object_key", "provider_version_id", "size", "storage_class",
    "plaintext_sha256", "integrity", "availability", "availability_checked_at", "verified_at",
    "restore_state", "restore_expiry", "created_at", "storage_class_since",
    "storage_class_since_estimated", "expected_storage_class", "minimum_storage_until",
    "upload_verification_requested", "verification_method", "verification_fallback_reason",
    "verification_egress_bytes", "recoverable", "not_selectable_reason",
}
VERSIONS_KEYS = {
    "path", "items", "recoverable_count", "default_archive_version_id", "reconciliation",
    "remediations", "supported_restore_tiers", "default_restore_tier", "default_restore_days",
}
PREVIEW_KEYS = {
    "object_count", "version_count", "delete_marker_count", "byte_count", "fingerprint", "mode",
    "duplicates_only", "eligible_count", "eligible_bytes", "duplicate_count", "duplicate_bytes",
    "no_local_copy_count", "no_local_copy_bytes", "skipped", "skipped_count", "minimum_storage",
    "retention_lock",
}
ESTIMATE_KEYS = {
    "path", "archive_version_id", "requires_restore", "restore_object_irreversible",
    "high_impact", "estimate",
}


class RouteContractTests(unittest.TestCase):
    def test_every_json_route_declares_the_model_of_its_answer(self) -> None:
        stream = "/api/vaults/{vault_id}/catalog/events"
        for route in jobs.router.routes:
            assert isinstance(route, APIRoute)
            with self.subTest(route.path):
                self.assertEqual(_response_contract(route) is None, route.path == stream)


class LocalWorkResponsesTests(CloudDeletionFixture):
    """Vault 2 with ``report.txt`` on the synthetic versioned provider."""

    owner = {}

    def setUp(self) -> None:
        super().setUp()
        self.client = self.http_client()
        self.sign_in(send_csrf_header=True,
                     reauthenticated_until=datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat())

    def answer(self, method: str, url: str, status: int = 200, **options: Any) -> Any:
        response = self.client.request(method, url, **options)
        self.assertEqual(response.status_code, status, f"{method} {url}: {response.text}")
        self.assertEqual(response.headers["content-type"], "application/json")
        return response.json()

    def test_upload_lists_and_local_release(self) -> None:
        upload = self.answer("POST", "/api/vaults/2/upload", 202, json={"path": "report.txt"})
        self.assertEqual(set(upload), QUEUED_KEYS)
        [job_id] = upload["job_ids"]
        listed = self.answer("GET", "/api/vaults/2/jobs")
        self.assertEqual(set(listed), {"items", "groups", "locale", "next_groups_before_id"})
        self.assertEqual(set(listed["items"][0]), LISTED_JOB_KEYS)
        self.assertEqual(set(listed["groups"][0]), GROUP_KEYS)
        self.run_job(job_id)
        detail = self.answer("GET", f"/api/vaults/2/jobs/{job_id}")
        self.assertEqual(set(detail), JOB_KEYS)
        history = self.answer("GET", "/api/vaults/2/files/jobs", params={"path": "report.txt"})
        self.assertEqual(set(history), {"items", "next_before_id"})
        self.assertEqual(set(history["items"][0]), JOB_KEYS)
        versions = self.answer("GET", "/api/vaults/2/files/versions", params={"path": "report.txt"})
        self.assertEqual(set(versions), VERSIONS_KEYS)
        self.assertEqual(set(versions["items"][0]), VERSION_KEYS)
        self.assertEqual(set(versions["reconciliation"]), {"pending", "can_resume", "reason", "remedy"})
        estimate = self.answer("POST", "/api/vaults/2/recover/estimate", json={"path": "report.txt"})
        self.assertEqual(set(estimate), ESTIMATE_KEYS | {"storage_class"})
        released = self.answer("POST", "/api/vaults/2/free-space", 202, json={"path": "report.txt"})
        self.assertEqual(set(released), QUEUED_KEYS)
        self.answer("POST", "/api/vaults/2/jobs/cancel", json={
            "group_id": released["group_id"], "action": "free-space"})

    def test_folder_upload_is_admitted_and_its_admission_read(self) -> None:
        request_id = str(uuid.uuid4())
        upload = self.answer("POST", "/api/vaults/2/upload", 202, json={
            "path": "", "is_directory": True, "request_id": request_id})
        self.assertEqual(set(upload), {
            "group_id", "job_ids", "item_count", "total_bytes", "admission", "message", "message_key"})
        self.assertEqual(set(upload["admission"]), ADMISSION_KEYS)
        status = self.answer("GET", f"/api/vaults/2/upload/admissions/{request_id}")
        self.assertEqual(set(status), ADMISSION_KEYS)
        cancelled = self.answer("POST", "/api/vaults/2/jobs/cancel", json={
            "group_id": request_id, "action": "upload"})
        self.assertEqual(set(cancelled), {"message", "cancelled_count", "stopped_admissions"})
        pending = self.answer("POST", "/api/vaults/2/upload/cancel-pending", json={"path": ""})
        self.assertEqual(set(pending), {"message", "cancelled_count", "stopped_admissions"})
        for url in ("/api/upload/retry-failed", "/api/vaults/2/upload/retry-failed"):
            self.assertEqual(set(self.answer("POST", url, 202)), {"item_count"})

    def test_recovery_of_a_cloud_only_file(self) -> None:
        self.cloud_only()
        recovered = self.answer("POST", "/api/vaults/2/recover", 202, json={"path": "report.txt"})
        self.assertEqual(set(recovered), QUEUED_KEYS | {"approval_required"})
        cancelled = self.answer("POST", "/api/vaults/2/jobs/cancel", json={
            "group_id": recovered["group_id"], "action": "recover"})
        self.assertEqual(set(cancelled), {"message", "cancelled_count", "stopped_admissions"})

    def test_cloud_deletion_preview_schedule_and_cancel(self) -> None:
        self.cloud_only()
        self.assertEqual(self.answer("GET", "/api/cloud-deletion"), {"purge_delay_seconds": 604800})
        preview = self.answer("POST", "/api/vaults/2/cloud-deletion/preview", json={"path": "report.txt"})
        self.assertEqual(set(preview), PREVIEW_KEYS)
        self.assertEqual(set(preview["skipped"]), {
            "present_locally", "pending_work", "pending_cleanup", "conflict", "not_verified",
            "no_exact_version"})
        scheduled = self.answer("POST", "/api/vaults/2/cloud-purge", 202, json={
            "path": "report.txt", "confirmation": "Docs", "reason": "deliberate deletion"})
        self.assertEqual(set(scheduled), {
            "group_id", "job_ids", "preview", "pending_until", "status", "message", "message_key"})
        self.assertEqual(set(scheduled["preview"]), PREVIEW_KEYS)
        listed = self.answer("GET", "/api/vaults/2/jobs", params={"action": "cloud-purge"})
        self.assertIsNotNone(listed["groups"][0]["pending_until"])
        cancelled = self.answer("POST", "/api/vaults/2/jobs/cancel", json={
            "group_id": scheduled["group_id"], "action": "cloud-purge"})
        self.assertEqual(set(cancelled), {"message", "cancelled_count"})

    def test_replacing_the_only_cloud_copy(self) -> None:
        self.cloud_only()
        with self.db() as connection:
            set_policy(connection, 2, OperationPolicy(auto_upload=True, stability_seconds=0))
        self.write(B)
        with self.db() as connection:
            vault = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()
        storage.apply_auto_renames(vault)
        with self.db() as connection:
            queue_auto_uploads(connection, vault_id=2, source_root=str(self.source))
        replaced = self.answer("POST", "/api/vaults/2/files/cloud-only-conflict/replace", 202,
                               json={"path": "report.txt"})
        self.assertEqual(set(replaced), QUEUED_KEYS)

    def test_reconciliation_of_an_ambiguous_send(self) -> None:
        self.provider.hook = None
        job_id = self.queue()
        with patch.object(self.provider, "upload", side_effect=ConnectionError("lost")):
            self.run_job(job_id)
        versions = self.answer("GET", "/api/vaults/2/files/versions", params={"path": "report.txt"})
        [remediation] = versions["remediations"]
        self.assertEqual(set(remediation), REMEDIATION_KEYS)
        self.assertEqual(set(self.answer("GET", "/api/vaults/2/jobs")["items"][0]["remediation"]),
                         REMEDIATION_KEYS)
        resumed = self.answer("POST", "/api/vaults/2/upload/reconcile", 202, json={
            "path": "report.txt", "remediation_id": remediation["id"]})
        self.assertEqual(set(resumed), {"job_id", "group_id"})


class RecoveryApprovalResponsesTests(JobAdmissionHttpFixture):
    """Vault 2 with a folder of Cloud-only files in three Storage Classes."""

    def answer(self, method: str, url: str, status: int = 200, **options: Any) -> Any:
        response = self.client.request(method, url, headers=self.headers, **options)
        self.assertEqual(response.status_code, status, f"{method} {url}: {response.text}")
        return response.json()

    def test_folder_estimate_held_recovery_and_its_approval(self) -> None:
        estimate = self.answer("POST", "/api/vaults/2/recover/estimate", json={
            "path": "photos", "is_directory": True})
        self.assertEqual(set(estimate), ESTIMATE_KEYS)
        self.assertEqual(set(estimate["estimate"]), {
            "tier", "days", "estimated_cost_eur", "estimated_hours"})
        file_estimate = self.answer("POST", "/api/vaults/2/recover/estimate", json={
            "path": "photos/sub/c.jpg"})
        self.assertEqual(set(file_estimate["estimate"]), {
            "tier", "days", "estimated_cost_eur", "estimated_hours", "pricing_note", "region",
            "pricing_effective_at", "assumptions", "restore_object_irreversible"})
        held = self.answer("POST", "/api/vaults/2/recover", 202, json={
            "path": "photos", "is_directory": True})
        self.assertEqual(set(held), QUEUED_KEYS | {"approval_required"})
        self.assertTrue(held["approval_required"])
        listed = self.answer("GET", "/api/vaults/2/jobs")
        self.assertEqual(set(listed["groups"][0]), GROUP_KEYS)
        self.assertEqual(set(listed["items"][0]), LISTED_JOB_KEYS)
        group = self.answer("GET", f"/api/vaults/2/job-groups/{listed['groups'][0]['id']}/jobs")
        self.assertEqual(set(group), {"items", "next_before_id"})
        self.assertEqual(set(group["items"][0]), LISTED_JOB_KEYS)
        approved = self.answer("POST", "/api/vaults/2/recover/approve", 202, json={
            "group_id": held["group_id"]})
        self.assertEqual(set(approved), {"group_id", "job_ids", "message"})

    def test_catalog_revision_and_scan(self) -> None:
        revision = self.answer("GET", "/api/vaults/2/catalog/revision")
        self.assertEqual(set(revision), {"vault_id", "revision", "domains", "has_gap", "changed"})
        with patch("app.routes.jobs.queue_scan"):
            scan = self.answer("POST", "/api/vaults/2/scan", 202)
        self.assertEqual(set(scan), {"message", "message_key", "queued"})
