"""What the Vault settings routes answer, key by key (ADR-0056, #805).

Every JSON operation of ``app/routes/policy.py`` and ``app/routes/storage_plan.py``
is called with a success: the Operation Policy and its exclusions, the Excluded
Folders read from disk, the storage estimate, the Storage Plan and the Vault
move. The shared client checks each answer against the model the route
declares; the key sets below pin what a route answers today, so a model can
only follow the answer and never the other way round.
"""
from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from fastapi.routing import APIRoute

from app.routes import policy, storage_plan
from app.services.source_layout import VaultLocalAccess
from app.services.vault_exclusions import DiskReadError
from app.services.vault_relocation import ROOT_IDENTITY_VERSION, root_identity
from tests.fixtures import FrostVaultTestCase, _response_contract, seed_vault
from tests.test_rename_sync import CopyWorkerFixture

ONLINE = VaultLocalAccess(True, True, "photos", "ok")
POLICY_KEYS = {
    "auto_upload", "local_release_after_days", "stability_seconds", "include_globs",
    "exclude_globs", "upload_verification", "global_upload_verification",
    "effective_upload_verification", "fixed_exclude_globs",
}
ITEM_KEYS = {
    "path", "is_directory", "own_rule", "own_rule_present", "excluded", "rules", "fixed",
    "not_included",
}
PREVIEW_KEYS = ITEM_KEYS | {"includable", "files", "bytes", "complete", "estimate", "folder_rule"}
ESTIMATE_KEYS = {
    "kind", "size_bytes", "storage_class", "tier", "estimated_cost_eur", "estimated_hours",
    "currency", "region", "pricing_effective_at", "assumptions",
}
LISTING_KEYS = {"directory", "excluded_by", "folders", "entries", "total_entries", "truncated"}
MOVE_ESTIMATE_KEYS = {
    "target_class", "eligible_count", "total_bytes", "skipped", "minimum_days",
    "estimated_monthly_eur", "estimated_retrieval_eur", "pricing_effective_at", "region",
    "assumptions",
}
PLAN = {"transitions": [{"days": 30, "storage_class": "STANDARD_IA"},
                        {"days": 300, "storage_class": "DEEP_ARCHIVE"}]}


class RouteContractTests(unittest.TestCase):
    def test_every_json_route_declares_the_model_of_its_answer(self) -> None:
        for router in (policy.router, storage_plan.router):
            for route in router.routes:
                assert isinstance(route, APIRoute)
                with self.subTest(route.path, methods=sorted(route.methods)):
                    self.assertIsNotNone(_response_contract(route))


class AnswerMixin:
    client: Any

    def answer(self, method: str, url: str, **options: Any) -> Any:
        response = self.client.request(method, url, **options)
        self.assertEqual(response.status_code, 200, f"{method} {url}: {response.text}")
        self.assertEqual(response.headers["content-type"], "application/json")
        return response.json()


class OperationPolicyResponsesTests(AnswerMixin, FrostVaultTestCase):
    """A Vault on a real folder with ``library/a.jpg``, ``upload/u1/b.jpg``, ``upload/c.jpg``."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp_path / "immich"
        for folder in ("library", "upload/u1"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "library/a.jpg").write_bytes(b"a" * 10)
        (self.root / "upload/u1/b.jpg").write_bytes(b"b" * 20)
        (self.root / "upload/c.jpg").write_bytes(b"c" * 30)
        with self.database.connect() as connection:
            self.vault_id = seed_vault(
                connection, source_root=str(self.root), root_identity_version=ROOT_IDENTITY_VERSION,
                root_identity=root_identity(str(self.root)),
            )["id"]
        self.enterContext(
            patch("app.services.vault_exclusions.vault_local_access", return_value=ONLINE))
        self.enterContext(patch("app.routes.policy.request_full_scan"))
        self.client = self.http_client()
        self.headers = self.sign_in().csrf_headers
        self.base = f"/api/vaults/{self.vault_id}"

    def test_policy_read_saved_and_edited(self) -> None:
        read = self.answer("GET", f"{self.base}/operation-policy")
        self.assertEqual(set(read), POLICY_KEYS)
        self.assertIsNone(read["upload_verification"])
        saved = self.answer("PUT", f"{self.base}/operation-policy", headers=self.headers, json={
            "local_release_after_days": 90, "upload_verification": "read_back",
            "include_globs": ["**/*.jpg"], "exclude_globs": ["*.tmp"]})
        self.assertEqual(set(saved), POLICY_KEYS)
        self.assertEqual((saved["local_release_after_days"], saved["effective_upload_verification"]),
                         (90, "read_back"))
        edited = self.answer("POST", f"{self.base}/operation-policy/globs", headers=self.headers,
                             json={"field": "exclude_globs", "action": "remove", "glob": "*.tmp"})
        self.assertEqual(set(edited), POLICY_KEYS)
        preview = self.answer("POST", f"{self.base}/operation-policy/preview-globs", headers=self.headers,
                              json={"paths": ["a.jpg", "b.tmp"], "exclude_globs": ["*.tmp"]})
        self.assertEqual(preview, {"included": ["a.jpg"], "excluded": ["b.tmp"]})

    def test_an_item_excluded_previewed_and_included_again(self) -> None:
        params = {"path": "upload", "is_directory": "true"}
        status = self.answer("GET", f"{self.base}/exclusions/item", params=params)
        self.assertEqual(set(status), ITEM_KEYS)
        self.assertFalse(status["own_rule_present"])
        not_includable = self.answer("GET", f"{self.base}/exclusions/inclusion-preview", params=params)
        self.assertEqual(set(not_includable), PREVIEW_KEYS)
        self.assertEqual([not_includable[key] for key in ("files", "bytes", "complete", "estimate")],
                         [None, None, None, None])
        excluded = self.answer("POST", f"{self.base}/exclusions/exclude", headers=self.headers,
                               json={"path": "upload", "is_directory": True})
        self.assertEqual(set(excluded), POLICY_KEYS)
        measured = self.answer("GET", f"{self.base}/exclusions/inclusion-preview", params=params)
        self.assertEqual(set(measured), PREVIEW_KEYS)
        self.assertEqual((measured["files"], measured["bytes"], measured["complete"]), (2, 50, True))
        self.assertEqual(set(measured["estimate"]), ESTIMATE_KEYS)
        with patch("app.services.vault_exclusions.measure_upload",
                   side_effect=DiskReadError("unreadable")):
            unknown = self.answer("GET", f"{self.base}/exclusions/inclusion-preview", params=params)
        self.assertEqual(set(unknown), PREVIEW_KEYS)
        self.assertTrue(unknown["includable"])
        self.assertIsNone(unknown["estimate"])
        included = self.answer("POST", f"{self.base}/exclusions/include", headers=self.headers,
                               json={"path": "upload", "is_directory": True})
        self.assertEqual(set(included), POLICY_KEYS)

    def test_excluded_folders_outside_and_inside(self) -> None:
        empty = self.answer("GET", f"{self.base}/excluded-folders")
        self.assertEqual(empty, {"directory": "", "excluded_by": None, "folders": [], "entries": [],
                                 "total_entries": 0, "truncated": False})
        self.answer("POST", f"{self.base}/exclusions/exclude", headers=self.headers,
                    json={"path": "upload", "is_directory": True})
        root = self.answer("GET", f"{self.base}/excluded-folders")
        self.assertEqual(set(root), LISTING_KEYS)
        self.assertEqual(root["folders"], [{"name": "upload", "path": "upload", "rule": "upload/**"}])
        inside = self.answer("GET", f"{self.base}/excluded-folders", params={"directory": "upload"})
        self.assertEqual(set(inside), LISTING_KEYS)
        self.assertEqual([set(entry) for entry in inside["entries"]],
                         [{"name", "path", "type", "size", "modified_at"}] * 2)
        self.assertEqual([entry["size"] for entry in inside["entries"]], [None, 30])

    def test_storage_estimate_of_a_size_and_of_a_vault(self) -> None:
        url = "/api/general/cost-estimates/storage"
        sized = self.answer("POST", url, headers=self.headers, json={"size_bytes": 1024})
        self.assertEqual(set(sized), ESTIMATE_KEYS)
        self.assertEqual((sized["kind"], sized["tier"], sized["estimated_hours"]), ("storage_month", None, None))
        empty = self.answer("POST", url, headers=self.headers, json={"vault_id": self.vault_id})
        self.assertEqual((empty["size_bytes"], empty["estimated_cost_eur"]), (0, 0.0))

    def test_storage_plan_read_and_saved(self) -> None:
        read = self.answer("GET", f"{self.base}/lifecycle")
        self.assertEqual(set(read), {"profile", "guided_profiles"})
        self.assertEqual(set(read["guided_profiles"]["economical"]), {"transitions"})
        self.assertEqual(set(read["guided_profiles"]["economical"]["transitions"][0]), {"days", "storage_class"})
        with patch("app.routes.storage_plan.s3_client"), \
                patch("app.routes.storage_plan.sync_lifecycle_rules_for_bucket"):
            saved = self.answer("PUT", f"{self.base}/lifecycle", headers=self.headers,
                                json={"guided_profile": "economical"})
        self.assertEqual(set(saved), {"message", "sync_pending", "warnings", "profile", "guided_profiles"})
        self.assertEqual(saved["profile"], read["guided_profiles"]["economical"])


class VaultMoveResponsesTests(AnswerMixin, CopyWorkerFixture):
    """Vault 2 with one protected file and a two-step Storage Plan."""

    owner = {}

    def setUp(self) -> None:
        super().setUp()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET storage_plan=%s::jsonb WHERE id=2", (json.dumps(PLAN),))
        self.protected()
        self.client = self.http_client()
        self.headers = self.sign_in().csrf_headers

    def test_move_preview_and_schedule(self) -> None:
        preview = self.answer("GET", "/api/vaults/2/move")
        self.assertEqual(set(preview), {"vault_id", "targets"})
        self.assertEqual([set(target) for target in preview["targets"]], [MOVE_ESTIMATE_KEYS] * 2)
        moved = self.answer("POST", "/api/vaults/2/move", headers=self.headers,
                            json={"target_class": "STANDARD_IA", "acknowledge_costs": True})
        self.assertEqual(set(moved), MOVE_ESTIMATE_KEYS | {"group_id", "job_ids", "item_count"})
        self.assertEqual(moved["item_count"], 1)


if __name__ == "__main__":
    unittest.main()
