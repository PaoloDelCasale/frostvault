"""HTTP coverage for current snapshot recovery, exact pins, estimates and approval."""

from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.services.local_copies import LocalCopies
from app.storage import process_jobs_once
from tests.catalog_support import fingerprint, protect
from tests.fixtures import FrostVaultTestCase, seed_vault
from tests import catalog_probe


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class RecoveryHttpTests(FrostVaultTestCase):
    PASSWORD = "correct-horse-battery"
    owner = {"password": PASSWORD}
    settings_overrides = {
        "restore_high_impact_gib": 100,
        "restore_high_impact_eur": 10.0,
        "restore_approval_hold_seconds": 3600,
        "restore_tier": "Bulk",
        "restore_days": 3,
    }

    def setUp(self) -> None:
        super().setUp()
        self.source = self.tmp_path / "source"
        self.source.mkdir()

        with self.database.connect() as connection:
            self.vault_id = seed_vault(connection, source_root=str(self.source))["id"]
            catalog = ArchiveCatalog(connection)
            local_copies = LocalCopies(connection)
            payload = b"http-recover"
            self.file_id = local_copies.observe_local_copy(
                vault_id=self.vault_id,
                path="docs/note.txt",
                file_type="regular",
                size=len(payload),
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            fingerprint(catalog, self.vault_id, "docs/note.txt", _sha(payload))
            self.version_id = protect(catalog, self.vault_id, "docs/note.txt", _sha(payload))
            connection.execute("UPDATE archive_versions SET storage_class='GLACIER' WHERE id=%s",
                               (self.version_id,))
            local_copies.mark_local_copy_missing(
                self.file_id, observed_at="2026-07-21T11:00:00+00:00"
            )

        self.client = self.http_client(follow_redirects=False)

    def test_versions_and_estimate_expose_irreversible_restore_boundary(self) -> None:
        self.sign_in()
        versions = self.client.get(
            f"/api/vaults/{self.vault_id}/files/versions",
            params={"path": "docs/note.txt"},
        )
        self.assertEqual(versions.status_code, 200, versions.text)
        body = versions.json()
        self.assertEqual(body["recoverable_count"], 1)
        self.assertEqual(body["default_archive_version_id"], self.version_id)

        estimate = self.client.post(
            f"/api/vaults/{self.vault_id}/recover/estimate",
            headers=self.signed_in.csrf_headers,
            json={
                "path": "docs/note.txt",
                "archive_version_id": self.version_id,
                "restore_tier": "Bulk",
                "restore_days": 3,
            },
        )
        self.assertEqual(estimate.status_code, 200, estimate.text)
        payload = estimate.json()
        self.assertTrue(payload["requires_restore"])
        self.assertTrue(payload["restore_object_irreversible"])
        self.assertEqual(payload["estimate"]["tier"], "Bulk")
        self.assertNotIn("price_book_id", payload["estimate"])
        self.assertEqual(payload["estimate"]["region"], payload["estimate"]["assumptions"]["region"])

    def test_superseded_copies_are_preserved_but_cannot_be_chosen_as_history(self) -> None:
        with self.database.connect() as connection:
            catalog = ArchiveCatalog(connection)
            generation = catalog_probe.get_file_by_path(catalog, self.vault_id, "docs/note.txt")["generation"]
            newest = catalog.begin_replacement(
                self.file_id, object_key="vaults/x/docs/note.txt", provider_version_id="v2", size=5,
                storage_class="STANDARD", created_at="2026-11-03T00:00:00+00:00")
            catalog.promote_replacement(
                self.file_id, archive_version_id=newest, plaintext_sha256=_sha(b"newer"),
                verified_at="2026-11-03T00:00:00+00:00", local_generation=generation)
        self.sign_in()
        response = self.client.get(f"/api/vaults/{self.vault_id}/files/versions", params={"path": "docs/note.txt"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()["items"]], [newest])
        for endpoint in (f"/api/vaults/{self.vault_id}/recover/estimate", f"/api/vaults/{self.vault_id}/recover"):
            rejected = self.client.post(endpoint, headers=self.signed_in.csrf_headers, json={
                "path": "docs/note.txt", "archive_version_id": self.version_id,
            })
            self.assertEqual(rejected.status_code, 409, rejected.text)
        queued = self.client.post(f"/api/vaults/{self.vault_id}/recover", headers=self.signed_in.csrf_headers, json={"path": "docs/note.txt"})
        self.assertEqual(queued.status_code, 202, queued.text)
        with self.database.connect() as connection:
            # No implicit cloud purge on read/recovery: the superseded copy waits
            # for its exact cleanup.
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM archive_versions WHERE id=%s", (self.version_id,)).fetchone())
            self.assertEqual(str(connection.execute("SELECT archive_version_id FROM jobs").fetchone()["archive_version_id"]), newest)

    def test_selection_recovery_preserves_csrf_vault_scope_and_bulk_decision(self) -> None:
        selection = {"selection": [{"path": "docs", "is_directory": True}, {"path": "docs/note.txt"}]}
        self.assertIn(self.client.post(f"/api/vaults/{self.vault_id}/recover", json=selection).status_code, {401, 403})
        self.sign_in()
        self.assertEqual(self.client.post(f"/api/vaults/{self.vault_id}/recover", json=selection).status_code, 403)
        with self.database.connect() as connection:
            connection.execute("UPDATE archive_versions SET size=%s", (100 * 1024**3,))
        for endpoint in (f"/api/vaults/{self.vault_id}/recover/estimate", f"/api/vaults/{self.vault_id}/recover"):
            self.assertEqual(self.client.post(endpoint.replace(f"/vaults/{self.vault_id}/", "/vaults/999999/"), headers=self.signed_in.csrf_headers, json=selection).status_code, 404)
            self.assertEqual(self.client.post(endpoint, headers=self.signed_in.csrf_headers, json={"selection": [{"path": "../other"}]}).status_code, 422)
            self.assertEqual(self.client.post(endpoint, headers=self.signed_in.csrf_headers, json={**selection, "path": "docs/note.txt"}).status_code, 422)
        estimate = self.client.post(f"/api/vaults/{self.vault_id}/recover/estimate", headers=self.signed_in.csrf_headers, json=selection)
        self.assertEqual(estimate.status_code, 200, estimate.text)
        self.assertTrue(estimate.json()["high_impact"])
        queued = self.client.post(f"/api/vaults/{self.vault_id}/recover", headers=self.signed_in.csrf_headers, json=selection)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()["item_count"], 1, "folder/file overlap deduplicated")
        self.assertTrue(queued.json()["approval_required"])
        approved = self.client.post(f"/api/vaults/{self.vault_id}/recover/approve", headers=self.signed_in.csrf_headers, json={"group_id": queued.json()["group_id"]})
        self.assertEqual(approved.status_code, 202, approved.text)
        self.assertEqual(approved.json()["job_ids"], queued.json()["job_ids"])

    def test_owner_approves_high_impact_restore(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE archive_versions SET size=%s WHERE id=%s",
                (100 * 1024**3, self.version_id),
            )
        self.sign_in()
        queued = self.client.post(
            f"/api/vaults/{self.vault_id}/recover",
            headers=self.signed_in.csrf_headers,
            json={"path": "docs/note.txt", "archive_version_id": self.version_id},
        )
        self.assertEqual(queued.status_code, 202, queued.text)
        group_id = queued.json()["group_id"]
        with (
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client"),
        ):
            process_jobs_once()
        approved = self.client.post(
            f"/api/vaults/{self.vault_id}/recover/approve",
            headers=self.signed_in.csrf_headers,
            json={"group_id": group_id},
        )
        self.assertEqual(approved.status_code, 202, approved.text)
        self.assertEqual(approved.json()["message"], "High-impact restore approved")


if __name__ == "__main__":
    unittest.main()
