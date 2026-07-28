from __future__ import annotations

import inspect
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.database import SQLiteConnection
from app.services import source_layout
from app.services import vaults as vaults_service
from tests.test_database import run_alembic


class VaultCreationServiceTestCase(unittest.TestCase):
    """Direct-import tests for app.services.vaults.create_vault_for_user."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        self.managed_root = source_layout.ensure_managed_directory()

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            vault_s3_bucket="test-bucket",
            vault_rclone_remote="test-remote",
        )
        for target in ("app.database.settings", "app.services.vaults.settings"):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _vaults(self) -> list[dict]:
        with SQLiteConnection(str(self.database_path)) as connection:
            return connection.execute("SELECT * FROM vaults").fetchall()

    def _all_members(self) -> list[dict]:
        with SQLiteConnection(str(self.database_path)) as connection:
            return connection.execute("SELECT * FROM vault_members").fetchall()

    def test_creates_vault_owner_membership_and_directory_atomically(self) -> None:
        vault = vaults_service.create_vault_for_user(1, "My Archive")

        self.assertEqual(vault["name"], "My Archive")
        self.assertEqual(vault["slug"], "my-archive")
        self.assertTrue(vault["uuid"])
        self.assertEqual(len(vault["uuid"]), 36)

        # Storage identity is derived purely from the generated uuid, never
        # from the label.
        self.assertEqual(vault["s3_prefix"], f"vaults/{vault['uuid']}/")
        self.assertEqual(vault["s3_bucket"], "test-bucket")
        self.assertEqual(vault["rclone_remote"], "test-remote")
        self.assertEqual(
            Path(vault["source_root"]), self.managed_root / vault["uuid"]
        )
        self.assertTrue(Path(vault["source_root"]).is_dir())

        members = self._all_members()
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["vault_id"], vault["id"])
        self.assertEqual(members[0]["user_id"], 1)
        self.assertEqual(members[0]["role"], "owner")

    def test_renaming_the_label_never_changes_the_storage_identity(self) -> None:
        first = vaults_service.create_vault_for_user(1, "Docs", slug="docs")
        second = vaults_service.create_vault_for_user(1, "Docs", slug="docs-2")

        self.assertNotEqual(first["uuid"], second["uuid"])
        self.assertNotEqual(first["source_root"], second["source_root"])
        self.assertNotEqual(first["s3_prefix"], second["s3_prefix"])

    def test_the_service_accepts_only_labels_and_encryption_mode(self) -> None:
        # No parameter exists through which a caller could supply a storage
        # root, bucket, prefix, rclone remote, or crypt secret: this is
        # enforced by the function signature itself, not by input filtering.
        signature = inspect.signature(vaults_service.create_vault_for_user)
        self.assertEqual(
            list(signature.parameters),
            ["user_id", "name", "slug", "encryption_mode"],
        )

    def test_duplicate_slug_is_rejected_and_leaves_nothing_partial_behind(self) -> None:
        vaults_service.create_vault_for_user(1, "Docs", slug="docs")

        with self.assertRaises(vaults_service.VaultSlugTaken):
            vaults_service.create_vault_for_user(1, "Docs Again", slug="docs")

        self.assertEqual(len(self._vaults()), 1)
        created_dirs = [p for p in self.managed_root.iterdir() if p.is_dir()]
        self.assertEqual(len(created_dirs), 1)

    def test_blank_name_is_rejected(self) -> None:
        with self.assertRaises(vaults_service.InvalidVaultName):
            vaults_service.create_vault_for_user(1, "   ")
        self.assertEqual(self._vaults(), [])

    def test_a_name_with_no_alphanumeric_characters_is_rejected(self) -> None:
        with self.assertRaises(vaults_service.InvalidVaultName):
            vaults_service.create_vault_for_user(1, "!!!")
        self.assertEqual(self._vaults(), [])

    def test_creation_is_unavailable_without_a_configured_bucket_and_remote(self) -> None:
        unconfigured = replace(self.settings, vault_s3_bucket="", vault_rclone_remote="")
        with patch("app.services.vaults.settings", unconfigured):
            with self.assertRaises(vaults_service.VaultProvisioningUnavailable):
                vaults_service.create_vault_for_user(1, "Docs")
        self.assertEqual(self._vaults(), [])

    def test_directory_creation_failure_rolls_back_the_database_rows(self) -> None:
        # Simulate the local directory step failing (e.g. a stale mount
        # collision or a filesystem error) after the DB rows are staged but
        # before the transaction commits: nothing must survive.
        with patch.object(vaults_service.os, "makedirs", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                vaults_service.create_vault_for_user(1, "Docs")

        self.assertEqual(self._vaults(), [])
        self.assertEqual(self._all_members(), [])
        self.assertEqual(list(self.managed_root.iterdir()), [])

    def test_concurrent_creation_never_collides_in_db_or_filesystem(self) -> None:
        def _create(index: int) -> dict:
            return vaults_service.create_vault_for_user(1, f"Vault {index}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_create, range(8)))

        uuids = {vault["uuid"] for vault in results}
        source_roots = {vault["source_root"] for vault in results}
        s3_prefixes = {vault["s3_prefix"] for vault in results}
        self.assertEqual(len(uuids), 8)
        self.assertEqual(len(source_roots), 8)
        self.assertEqual(len(s3_prefixes), 8)

        created_dirs = {p.name for p in self.managed_root.iterdir() if p.is_dir()}
        self.assertEqual(created_dirs, uuids)
        self.assertEqual(len(self._vaults()), 8)
        self.assertEqual(len(self._all_members()), 8)


if __name__ == "__main__":
    unittest.main()
