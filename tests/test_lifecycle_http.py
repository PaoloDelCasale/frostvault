from unittest.mock import patch

from app import main
from tests.fixtures import FrostVaultTestCase, seed_vault


class VaultLifecycleHttpTests(FrostVaultTestCase):
    def setUp(self):
        super().setUp()
        with self.database.connect() as connection:
            seed_vault(connection, id=1, source_root="/source")
        self.client = self.http_client()
        self.sign_in()

    def test_owner_can_read_and_save_every_plan_without_reauthentication(self):
        body = self.client.get("/api/vault/lifecycle").json()
        self.assertEqual(body["profile"], {"transitions": []})
        self.assertEqual(len(body["guided_profiles"]), 5)
        self.assertNotIn("policies", body)
        with patch("app.routes.storage_plan.s3_client"), patch("app.routes.storage_plan.sync_lifecycle_rules_for_bucket") as sync:
            for name, profile in body["guided_profiles"].items():
                result = self.client.put("/api/vault/lifecycle", headers=self.signed_in.csrf_headers, json={"guided_profile": name})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["profile"], profile)
                self.assertFalse(result.json()["sync_pending"])
            self.assertEqual(sync.call_count, 5)

    def test_custom_early_archive_and_source_residence_validation(self):
        with patch("app.routes.storage_plan.s3_client"), patch("app.routes.storage_plan.sync_lifecycle_rules_for_bucket"):
            for steps, expected in [([(1, "DEEP_ARCHIVE")], 200), ([(4, "GLACIER_IR"), (93, "DEEP_ARCHIVE")], 400), ([(4, "GLACIER_IR"), (94, "DEEP_ARCHIVE")], 200), ([(29, "STANDARD_IA")], 400), ([(1.5, "GLACIER")], 422), ([(True, "GLACIER")], 422)]:
                response = self.client.put("/api/vault/lifecycle", headers=self.signed_in.csrf_headers, json={"profile": {"transitions": [{"days": d, "storage_class": c} for d, c in steps]}})
                self.assertEqual(response.status_code, expected, response.text)

    def test_removed_policy_options_are_rejected_without_sync_or_mutation(self):
        before = self.client.get("/api/vault/lifecycle").json()
        payloads = [{}, {"guided_profile": "ia_after_30"}, {"guided_profile": "standard_only", "profile": {"transitions": []}},
                    *[{"profile": {"transitions": [], key: []}} for key in ["expiration_days", "noncurrent_expiration_days", "noncurrent_transitions"]],
                    {"guided_profile": "standard_only", "folder_path": "docs"}]
        with patch("app.routes.storage_plan.sync_lifecycle_rules_for_bucket") as sync:
            for payload in payloads:
                response = self.client.put("/api/vault/lifecycle", headers=self.signed_in.csrf_headers, json=payload)
                self.assertIn(response.status_code, [400, 422], response.text)
            sync.assert_not_called()
        self.assertEqual(self.client.get("/api/vault/lifecycle").json(), before)

    def test_sync_failure_keeps_durable_plan_for_retry_and_reports_pending(self):
        with patch("app.routes.storage_plan.s3_client", side_effect=RuntimeError("offline")):
            result = self.client.put("/api/vault/lifecycle", headers=self.signed_in.csrf_headers, json={"guided_profile": "archive_tiered"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["sync_pending"])
        self.assertEqual(self.client.get("/api/vault/lifecycle").json()["profile"], result.json()["profile"])

    def test_csrf_required_and_retired_endpoints_absent(self):
        result = self.client.put("/api/vault/lifecycle", json={"guided_profile": "standard_only"})
        self.assertEqual(result.status_code, 403)
        paths = main.app.openapi()["paths"]
        for path in ["/api/vault/lifecycle/default", "/api/vault/lifecycle/folder-overrides", "/api/lifecycle-pin", "/api/storage-class", "/api/storage-classes"]:
            self.assertNotIn(path, paths)
            response = self.client.put(path, headers=self.signed_in.csrf_headers, json={})
            self.assertIn(response.status_code, [404, 405])
