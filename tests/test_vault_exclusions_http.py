"""HTTP seams of exclusions in the Vault browser (#754)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.source_layout import VaultLocalAccess
from app.services.vault_relocation import ROOT_IDENTITY_VERSION, root_identity
from tests.fixtures import FrostVaultTestCase, seed_vault

ONLINE = VaultLocalAccess(True, True, "photos", "ok")


class VaultExclusionsHttpTests(FrostVaultTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp_path / "immich"
        for folder in ("library", "upload/u1", "thumbs"):
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
        self.client = self.http_client()
        self.sign_in()

    def audit(self, event: str) -> list[dict]:
        with self.database.connect() as connection:
            return [row["detail"] for row in connection.execute(
                "SELECT detail FROM audit_events WHERE event=%s ORDER BY id", (event,)).fetchall()]

    def policy(self) -> dict:
        response = self.client.get(f"/api/vaults/{self.vault_id}/operation-policy")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def edit(self, field: str, action: str, glob: str):
        return self.client.post(f"/api/vaults/{self.vault_id}/operation-policy/globs", headers=self.signed_in.csrf_headers,
                                json={"field": field, "action": action, "glob": glob})

    def test_the_policy_lists_the_fixed_exclusions_read_only(self) -> None:
        self.assertIn("._*", self.policy()["fixed_exclude_globs"])

    def test_one_glob_is_added_and_removed_against_the_stored_policy(self) -> None:
        put = self.client.put(f"/api/vaults/{self.vault_id}/operation-policy", headers=self.signed_in.csrf_headers,
                              json={"stability_seconds": 900, "exclude_globs": ["*.tmp"]})
        self.assertEqual(put.status_code, 200, put.text)
        added = self.edit("exclude_globs", "add", "thumbs/**")
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(added.json()["exclude_globs"], ["*.tmp", "thumbs/**"])
        self.assertEqual(added.json()["stability_seconds"], 900)
        self.assertEqual(self.edit("include_globs", "add", "**/*.jpg").json()["include_globs"], ["**/*.jpg"])
        removed = self.edit("exclude_globs", "remove", "*.tmp")
        self.assertEqual(removed.json()["exclude_globs"], ["thumbs/**"])
        stale = self.edit("exclude_globs", "remove", "*.tmp")
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.edit("exclude_globs", "add", "[z-a]*").status_code, 422)
        self.assertEqual([detail["glob"] for detail in self.audit("vault_operation_glob_added")],
                         ["thumbs/**", "**/*.jpg"])
        self.assertEqual([detail["glob"] for detail in self.audit("vault_operation_glob_removed")], ["*.tmp"])

    def test_removing_a_folder_rule_one_at_a_time_looks_inside_again(self) -> None:
        with patch("app.routes.policy.request_full_scan") as scan:
            self.edit("exclude_globs", "add", "upload/**")
            scan.assert_not_called()
            self.edit("exclude_globs", "remove", "upload/**")
            scan.assert_called_once()

    def test_a_folder_is_excluded_by_its_own_rule_and_included_again(self) -> None:
        excluded = self.client.post(f"/api/vaults/{self.vault_id}/exclusions/exclude", headers=self.signed_in.csrf_headers,
                                    json={"path": "upload", "is_directory": True})
        self.assertEqual(excluded.status_code, 200, excluded.text)
        self.assertEqual(excluded.json()["exclude_globs"], ["upload/**"])
        status = self.client.get(f"/api/vaults/{self.vault_id}/exclusions/item", params={"path": "upload", "is_directory": "true"}).json()
        self.assertEqual((status["own_rule"], status["own_rule_present"], status["excluded"]), ("upload/**", True, True))
        preview = self.client.get(f"/api/vaults/{self.vault_id}/exclusions/inclusion-preview",
                                  params={"path": "upload", "is_directory": "true"})
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertTrue(body["includable"])
        self.assertTrue(body["folder_rule"])
        self.assertEqual((body["files"], body["bytes"], body["complete"]), (2, 50, True))
        self.assertEqual(body["estimate"]["size_bytes"], 50)
        with patch("app.routes.policy.request_full_scan"):
            included = self.client.post(f"/api/vaults/{self.vault_id}/exclusions/include", headers=self.signed_in.csrf_headers,
                                        json={"path": "upload", "is_directory": True})
        self.assertEqual(included.status_code, 200, included.text)
        self.assertEqual(included.json()["exclude_globs"], [])
        [event] = self.audit("vault_item_excluded")
        self.assertEqual((event["path"], event["glob"], event["is_directory"]), ("upload", "upload/**", True))
        [event] = self.audit("vault_item_included")
        self.assertEqual(event["glob"], "upload/**")

    def test_a_file_gets_its_exact_path_and_glob_names_are_refused(self) -> None:
        response = self.client.post(f"/api/vaults/{self.vault_id}/exclusions/exclude", headers=self.signed_in.csrf_headers,
                                    json={"path": "library/a.jpg"})
        self.assertEqual(response.json()["exclude_globs"], ["library/a.jpg"])
        refused = self.client.post(f"/api/vaults/{self.vault_id}/exclusions/exclude", headers=self.signed_in.csrf_headers,
                                   json={"path": "library/a[1].jpg"})
        self.assertEqual(refused.status_code, 422, refused.text)
        # A root file's rule would have no "/" and match the name in every folder.
        root_file = self.client.post(f"/api/vaults/{self.vault_id}/exclusions/exclude", headers=self.signed_in.csrf_headers,
                                     json={"path": "notes.txt"})
        self.assertEqual(root_file.status_code, 422, root_file.text)
        self.assertEqual(self.client.get(f"/api/vaults/{self.vault_id}/operation-policy").json()["exclude_globs"],
                         ["library/a.jpg"])

    def test_a_broader_rule_is_shown_and_never_removed_from_an_item(self) -> None:
        self.edit("exclude_globs", "add", "**/u1/**")
        preview = self.client.get(f"/api/vaults/{self.vault_id}/exclusions/inclusion-preview",
                                  params={"path": "upload/u1", "is_directory": "true"}).json()
        self.assertFalse(preview["includable"])
        self.assertEqual(preview["rules"], ["**/u1/**"])
        self.assertIsNone(preview["files"])
        refused = self.client.post(f"/api/vaults/{self.vault_id}/exclusions/include", headers=self.signed_in.csrf_headers,
                                   json={"path": "upload/u1", "is_directory": True})
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.policy()["exclude_globs"], ["**/u1/**"])

    def test_the_browser_sees_excluded_folders_and_reads_inside_them_from_disk(self) -> None:
        self.edit("exclude_globs", "add", "upload/**")
        self.edit("exclude_globs", "add", "thumbs/**")
        root = self.client.get(f"/api/vaults/{self.vault_id}/excluded-folders")
        self.assertEqual(root.status_code, 200, root.text)
        self.assertIsNone(root.json()["excluded_by"])
        self.assertEqual([(f["name"], f["rule"]) for f in root.json()["folders"]],
                         [("thumbs", "thumbs/**"), ("upload", "upload/**")])
        inside = self.client.get(f"/api/vaults/{self.vault_id}/excluded-folders", params={"directory": "upload"}).json()
        self.assertEqual(inside["excluded_by"], "upload/**")
        self.assertEqual([(e["name"], e["type"]) for e in inside["entries"]], [("u1", "directory"), ("c.jpg", "file")])
        deeper = self.client.get(f"/api/vaults/{self.vault_id}/excluded-folders", params={"directory": "upload/u1"}).json()
        self.assertEqual([e["path"] for e in deeper["entries"]], ["upload/u1/b.jpg"])
        missing = self.client.get(f"/api/vaults/{self.vault_id}/excluded-folders", params={"directory": "upload/nope"})
        self.assertEqual(missing.status_code, 404, missing.text)
        escape = self.client.get(f"/api/vaults/{self.vault_id}/excluded-folders", params={"directory": "../x"})
        self.assertEqual(escape.status_code, 422, escape.text)
        # Nothing read from disk is catalogued.
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM files").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
