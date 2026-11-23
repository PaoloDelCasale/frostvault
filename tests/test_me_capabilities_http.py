"""GET /api/me vault capability seams (issue #59).

The SPA reads the selected Vault from /api/me without calling the expensive
``/api/stats`` endpoint. The single owner (#412) has no Vault role: every
Vault is theirs, and the Vault block carries no capability flags (#791).
"""

from __future__ import annotations

import unittest

from tests.fixtures import FrostVaultTestCase, override_settings, seed_vault
from tests.spa_fixture import write_spa_dist


class MeCapabilitiesHttpTests(FrostVaultTestCase):
    PASSWORD = "correct horse battery staple"
    owner = {"password": PASSWORD}

    def setUp(self) -> None:
        super().setUp()
        self.dist_dir = write_spa_dist(self.tmp_path)
        self.enterContext(override_settings(frontend_dist_dir=str(self.dist_dir)))
        self.client = self.http_client()

    def _create_vault(self, **values) -> dict:
        with self.database.connect() as connection:
            return seed_vault(connection, name="Docs Archive", **values)

    def _me(self) -> dict:
        self.sign_in()
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_owner_me_reports_the_selected_vault(self) -> None:
        """Seam 1: the owner sees its Vault without any role field."""
        created = self._create_vault()
        vault = self._me()["vault"]

        self.assertEqual(vault["id"], created["id"])
        self.assertEqual(vault["uuid"], str(created["uuid"]))
        self.assertEqual(vault["name"], "Docs Archive")
        for removed in ("role", "can_operate", "is_vault_owner",
                        "delete_enabled", "cloud_deletion_enabled"):
            self.assertNotIn(removed, vault)

    def test_owner_without_vault_gets_null_vault_block(self) -> None:
        """Seam 6: before the first Vault exists → a null vault block."""
        payload = self._me()

        self.assertIsNone(payload["vault"])
        self.assertIsNone(payload["stopped_vault"])
        self.assertEqual(payload["display_name"], "Owner")

    def test_stopped_vault_is_reported_when_no_vault_is_protected(self) -> None:
        stopping = self._create_vault(lifecycle="stopping")
        payload = self._me()

        self.assertIsNone(payload["vault"])
        stopped = payload["stopped_vault"]
        self.assertEqual(stopped["id"], stopping["id"])
        self.assertEqual(stopped["uuid"], str(stopping["uuid"]))
        self.assertEqual(stopped["lifecycle"], "stopping")
        self.assertFalse(stopped["root_released"])

    def test_released_vault_reports_when_its_root_was_released(self) -> None:
        released = self._create_vault(lifecycle="stopping")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE vaults SET lifecycle='tombstone', root_released_at=%s WHERE id=%s",
                ("2026-11-01T10:20:30.123456+00:00", released["id"]),
            )
        self.assertEqual(self._me()["stopped_vault"], {
            "id": released["id"],
            "uuid": str(released["uuid"]),
            "name": "Docs Archive",
            "lifecycle": "tombstone",
            "root_released_at": "2026-11-01T10:20:30.123456Z",
            "root_released": True,
        })

    def test_me_reports_the_owner_identity(self) -> None:
        """Seam 7: identity and session fields, without multi-user flags."""
        self._create_vault()
        payload = self._me()

        self.assertEqual(payload["id"], 1)
        self.assertNotIn("username", payload)
        self.assertEqual(payload["display_name"], "Owner")
        self.assertNotIn("is_admin", payload)
        self.assertNotIn("active", payload)
        self.assertEqual(payload["session_version"], 1)
        self.assertIsInstance(payload["csrf_token"], str)
        self.assertTrue(payload["csrf_token"])
        self.assertEqual(payload["auth_method"], "oidc")
        self.assertEqual(payload["locale"], "en")
        self.assertEqual(payload["locales"], ["en", "it"])

if __name__ == "__main__":
    unittest.main()
