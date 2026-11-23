from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.services import source_layout
from tests.fake_bucket_s3 import fake_vault_buckets
from tests.fixtures import FrostVaultTestCase, override_settings
from tests.spa_fixture import write_spa_dist


class VaultCreationHttpTestCase(FrostVaultTestCase):
    """Shared harness: a migrated PostgreSQL database with the single owner
    (#412), authenticated through an already-established session as after
    an OIDC login."""

    owner = {"display_name": "Alice"}

    def setUp(self) -> None:
        super().setUp()
        self.sources_root = self.tmp_path / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        self.managed_root = source_layout.ensure_managed_directory()

        self.enterContext(override_settings(
            frontend_dist_dir=str(write_spa_dist(self.tmp_path)),
        ))

        self.s3 = fake_vault_buckets(self)
        self.client = self.http_client()

    def _csrf(self) -> str:
        return self.client.get("/api/me").json()["csrf_token"]

    def _create(self, name: str, csrf: str | None = None):
        payload: dict[str, object] = {
            "name": name,
        }
        headers = {"X-CSRF-Token": csrf if csrf is not None else self._csrf()}
        return self.client.post("/api/vaults", json=payload, headers=headers)


class VaultCreationTests(VaultCreationHttpTestCase):
    def test_unauthenticated_vault_create_page_serves_spa(self) -> None:
        """HTML is the SPA shell; auth is enforced by the JSON create API."""
        response = self.client.get("/vaults/new", follow_redirects=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('id="root"', response.text)

        denied = self.client.post(
            "/api/vaults",
            json={"name": "My Archive"},
            headers={"X-CSRF-Token": "missing"},
        )
        self.assertEqual(denied.status_code, 401, denied.text)

    def test_vault_create_api_works_without_html_form_markup(self) -> None:
        self.sign_in()

        no_vault = self.client.get("/")
        self.assertEqual(no_vault.status_code, 200)
        self.assertIn('id="root"', no_vault.text)

        create_page = self.client.get("/vaults/new")
        self.assertEqual(create_page.status_code, 200)
        self.assertIn('id="root"', create_page.text)

        created = self._create("My Archive")
        self.assertEqual(created.status_code, 201, created.text)
        selected = self.client.post(
            "/api/vaults/select",
            json={"vault_id": created.json()["id"]},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(selected.status_code, 200, selected.text)

        archive = self.client.get("/")
        self.assertEqual(archive.status_code, 200)
        self.assertIn('id="root"', archive.text)

    def test_create_and_select_flow_opens_the_new_archive(self) -> None:
        self.sign_in()
        response = self._create("My Archive")
        self.assertEqual(response.status_code, 201, response.text)

        selected = self.client.post(
            "/api/vaults/select",
            json={"vault_id": response.json()["id"]},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        archive = self.client.get("/")
        self.assertEqual(archive.status_code, 200)
        self.assertIn('id="root"', archive.text)
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["vault"]["name"], "My Archive")
        self.assertNotIn("slug", me.json()["vault"])

    def test_the_owner_can_create_a_vault(self) -> None:
        self.sign_in()

        response = self._create("My Archive")

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["name"], "My Archive")
        self.assertNotIn("role", body)
        self.assertEqual(len(body["uuid"]), 36)

        # It is immediately usable/selectable.
        listed = self.client.get("/api/vaults").json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], body["id"])

        # The generated directory exists on disk under managed/<uuid>.
        created_dirs = [p.name for p in self.managed_root.iterdir() if p.is_dir()]
        self.assertEqual(created_dirs, [body["uuid"]])

    def test_plaintext_creation_has_no_mode_or_recovery_material_and_needs_no_master_key(self) -> None:
        self.sign_in()
        with override_settings(archive_master_key=""):
            response = self._create("Readable")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(set(response.json()),
                         {"id", "uuid", "name", "creation_mode", "lifecycle", "bucket"})
        self.assertEqual(response.json()["lifecycle"], "active")
        self.assertEqual(response.json()["bucket"]["state"], "ready")
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM vaults WHERE id=%s", (response.json()["id"],)).fetchone()
        self.assertEqual(str(row["uuid"]), response.json()["uuid"])
        self.assertNotIn("s3_prefix", row)
        self.assertFalse(any("crypt" in key or "custody" in key for key in row))

    def test_retired_encryption_options_and_recovery_routes_are_not_available(self) -> None:
        self.sign_in()
        headers = {"X-CSRF-Token": self._csrf()}
        for mode in ("plain", "crypt"):
            response = self.client.post("/api/vaults", json={"name": "Retired", "encryption_mode": mode}, headers=headers)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/vaults").json()["items"], [])
        for route in ("confirm", "export"):
            response = self.client.post(f"/api/vault/recovery/{route}", json={}, headers=headers)
            self.assertEqual(response.status_code, 404, response.text)

    def test_creation_requires_authentication(self) -> None:
        response = self.client.post("/api/vaults", json={"name": "Docs"})

        self.assertEqual(response.status_code, 401)

    def test_creation_requires_a_valid_csrf_token(self) -> None:
        self.sign_in()

        response = self._create("Docs", csrf="not-the-token")

        self.assertEqual(response.status_code, 403, response.text)

    def test_creation_needs_no_history_choice_and_rejects_retired_option(self) -> None:
        self.sign_in()
        created = self.client.post(
            "/api/vaults", json={"name": "Docs"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn("cloud_history_policy", created.json())
        rejected = self.client.post(
            "/api/vaults", json={"name": "Retired", "cloud_history_policy": "current_snapshot"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)

    def test_caller_supplied_storage_fields_are_rejected_outright(self) -> None:
        self.sign_in()
        headers = {"X-CSRF-Token": self._csrf()}

        response = self.client.post(
            "/api/vaults",
            json={
                "name": "Docs",
                "source_root": "/sources/whatever-i-want",
                "s3_bucket": "attacker-bucket",
                "s3_prefix": "not/my/prefix",

            },
            headers=headers,
        )

        self.assertEqual(response.status_code, 422, response.text)
        # Nothing was created as a side effect of the rejected request.
        self.assertEqual(self.client.get("/api/vaults").json()["items"], [])

    def test_a_vault_has_no_slug_and_a_name_is_only_a_label(self) -> None:
        self.sign_in()
        self.assertEqual(self._create("Docs").status_code, 201)
        second = self._create("Docs")
        self.assertEqual(second.status_code, 201, second.text)
        rejected = self.client.post(
            "/api/vaults", json={"name": "Docs", "slug": "docs"},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        listed = self.client.get("/api/vaults").json()["items"]
        self.assertEqual({item["lifecycle"] for item in listed}, {"active"})
        self.assertTrue(all("slug" not in item for item in listed))

    def test_creation_errors_are_localized_for_the_owner_locale(self) -> None:
        self.sign_in()
        self.client.cookies.set("frostvault_locale", "it")

        response = self._create("   ")

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["message_key"], "api.vault_name_invalid")
        self.assertEqual(response.json()["detail"], "Inserisci un nome per il Vault.")

    def test_no_new_user_is_ever_provisioned_by_creating_a_vault(self) -> None:
        self.sign_in()
        with self.database.connect() as connection:
            before = connection.execute("SELECT COUNT(*) AS total FROM owner").fetchone()[
                "total"
            ]

        response = self._create("Docs")

        self.assertEqual(response.status_code, 201, response.text)
        with self.database.connect() as connection:
            after = connection.execute("SELECT COUNT(*) AS total FROM owner").fetchone()[
                "total"
            ]
        self.assertEqual(before, after)

    def test_concurrent_creation_requests_never_collide(self) -> None:
        self.sign_in()
        csrf = self._csrf()

        def _create(index: int):
            return self.client.post(
                "/api/vaults",
                json={
                    "name": f"Vault {index}",
                        },
                headers={"X-CSRF-Token": csrf},
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(_create, range(6)))

        for response in responses:
            self.assertEqual(response.status_code, 201, response.text)
        uuids = {response.json()["uuid"] for response in responses}
        self.assertEqual(len(uuids), 6)
        created_dirs = {p.name for p in self.managed_root.iterdir() if p.is_dir()}
        self.assertEqual(created_dirs, uuids)


class VaultBucketCreationHttpTests(VaultCreationHttpTestCase):
    """#735: the Vault is active only once its own bucket is verified."""

    def test_a_failed_bucket_keeps_the_vault_provisioning_and_the_owner_retries(self) -> None:
        self.sign_in()
        self.s3.fail["put_bucket_versioning"] = "AccessDenied"
        response = self._create("Travel")
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["lifecycle"], "provisioning")
        self.assertEqual((body["bucket"]["state"], body["bucket"]["stage"]), ("failed", "created"))
        self.assertIn("AccessDenied", body["bucket"]["error"])
        self.assertNotIn("provider detail", body["bucket"]["error"])
        # Not selectable, not operational, but listed with its creation state.
        self.assertEqual(self.client.get("/api/vaults").json()["items"], [])
        general = self.client.get("/api/general/vaults").json()["items"]
        self.assertEqual([(item["lifecycle"], item["bucket_provisioning"]["state"]) for item in general],
                         [("provisioning", "failed")])
        selected = self.client.post("/api/vaults/select", json={"vault_id": body["id"]},
                                    headers={"X-CSRF-Token": self._csrf()})
        self.assertNotEqual(selected.status_code, 200, selected.text)
        status = self.client.get(f"/api/vaults/{body['id']}/bucket")
        self.assertEqual(status.json()["state"], "failed")

        retried = self.client.post(f"/api/vaults/{body['id']}/bucket/retry",
                                   headers={"X-CSRF-Token": self._csrf()})
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual((retried.json()["state"], retried.json()["error"]), ("ready", None))
        self.assertEqual([item["id"] for item in self.client.get("/api/vaults").json()["items"]],
                         [body["id"]])
        self.assertEqual(list(self.s3.buckets), [body["bucket"]["bucket"]])

    def _reauthenticate(self, at: datetime | None = None) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET reauth_at=%s", (at or datetime.now(timezone.utc),))

    def test_the_owner_cancels_a_creation_after_reauthentication(self) -> None:
        """#735 S7: the Vault leaves the catalog; a created bucket is only named."""
        self.sign_in()
        self.s3.fail["put_bucket_versioning"] = "AccessDenied"
        body = self._create("Travel").json()
        url = f"/api/vaults/{body['id']}/creation/cancel"
        self._reauthenticate(datetime(2000, 1, 1, tzinfo=timezone.utc))
        refused = self.client.post(url, headers={"X-CSRF-Token": self._csrf()})
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "reauth_required"))
        self._reauthenticate()
        cancelled = self.client.post(url, headers={"X-CSRF-Token": self._csrf()})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json(), {
            "vault_id": body["id"], "vault_name": "Travel", "bucket": body["bucket"]["bucket"],
            "region": "eu-south-1", "bucket_left": "created"})
        # Not deleted: FrostVault has no s3:DeleteBucket.
        self.assertEqual(list(self.s3.buckets), [body["bucket"]["bucket"]])
        self.assertEqual(self.client.get("/api/general/vaults").json()["items"], [])
        self.assertFalse((self.managed_root / body["uuid"]).exists())
        with self.database.connect() as connection:
            audit = connection.execute(
                "SELECT actor, vault_id, detail FROM audit_events "
                "WHERE event='vault.creation_cancelled'").fetchall()
        self.assertEqual([(row["actor"], row["vault_id"]) for row in audit], [("owner", body["id"])])
        self.assertEqual((audit[0]["detail"]["bucket"], audit[0]["detail"]["bucket_left"]),
                         (body["bucket"]["bucket"], "created"))
        again = self.client.post(url, headers={"X-CSRF-Token": self._csrf()})
        self.assertEqual(again.status_code, 404, again.text)

    def test_an_active_vault_cannot_be_cancelled(self) -> None:
        self.sign_in()
        self._reauthenticate()
        body = self._create("Photos").json()
        self.assertEqual(body["lifecycle"], "active")
        refused = self.client.post(f"/api/vaults/{body['id']}/creation/cancel",
                                   headers={"X-CSRF-Token": self._csrf()})
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["message_key"], "api.vault_creation_cancel.not_provisioning")

    def test_bucket_endpoints_need_a_session_and_csrf(self) -> None:
        self.assertEqual(self.client.get("/api/vaults/1/bucket").status_code, 401)
        self.sign_in()
        self.assertEqual(self.client.get("/api/vaults/999/bucket").status_code, 404)
        refused = self.client.post("/api/vaults/999/bucket/retry", headers={"X-CSRF-Token": "bad"})
        self.assertIn(refused.status_code, {401, 403})


class UnconfiguredVaultCreationTests(VaultCreationHttpTestCase):
    settings_overrides = {"aws_region": ""}

    def test_creation_fails_clearly_when_the_server_has_no_default_storage(self) -> None:
        self.sign_in()

        response = self._create("Docs")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["message_key"], "api.vault_provisioning_unavailable")
        self.assertEqual(self.client.get("/api/vaults").json()["items"], [])


if __name__ == "__main__":
    unittest.main()
