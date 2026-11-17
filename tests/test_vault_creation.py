from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import inspect
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings

from app.services import source_layout
from app.services import vaults as vaults_service
from tests.fake_bucket_s3 import fake_vault_buckets
from tests.owner_fixtures import insert_owner
from tests.test_database import run_alembic


class VaultCreationServiceTestCase(unittest.TestCase):
    """Direct-import tests for app.services.vaults.create_vault."""

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

        with PostgresConnection(str(self.database_path)) as connection:
            insert_owner(connection, display_name="Alice")

        self.settings = replace(
            Settings(),
            database_dsn=database_dsn(str(self.database_path)),
        )
        for target in ("app.database.settings", "app.services.vaults.settings"):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.s3 = fake_vault_buckets(self)

    def _vaults(self) -> list[dict]:
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute("SELECT * FROM vaults").fetchall()

    def test_creates_vault_and_directory_atomically(self) -> None:
        vault = vaults_service.create_vault("My Archive")

        self.assertEqual(vault["name"], "My Archive")
        self.assertNotIn("slug", vault)
        self.assertEqual(vault["lifecycle"], "active")
        self.assertTrue(vault["uuid"])
        self.assertEqual(len(vault["uuid"]), 36)

        # Its own Vault Bucket (#735), objects at the root; the name only
        # seeds the bucket name, the uuid tags it.
        self.assertEqual(vault["s3_prefix"], "")
        self.assertEqual(vault["s3_key_layout"], "bucket_root")
        self.assertRegex(vault["s3_bucket"], r"^frostvault-my-archive-[a-z0-9]{8}$")
        self.assertEqual(vault["s3_region"], self.settings.aws_region)
        self.assertEqual(vault["bucket"]["state"], "ready")
        self.assertIn({"Key": "frostvault:vault", "Value": vault["uuid"]},
                      self.s3.buckets[vault["s3_bucket"]]["tags"])
        self.assertNotIn("rclone_remote", vault)
        self.assertEqual(
            Path(vault["source_root"]), self.managed_root / vault["uuid"]
        )
        self.assertTrue(Path(vault["source_root"]).is_dir())

    def test_renaming_the_label_never_changes_the_storage_identity(self) -> None:
        # The name is only a label: two Vaults may share it (#593).
        first = vaults_service.create_vault("Docs")
        second = vaults_service.create_vault("Docs")

        self.assertNotEqual(first["uuid"], second["uuid"])
        self.assertNotEqual(first["source_root"], second["source_root"])
        self.assertNotEqual(first["s3_bucket"], second["s3_bucket"])

    def test_the_service_never_accepts_caller_chosen_storage_identity(self) -> None:
        # Only labels and constrained adoption coordinates are allowed.
        # Absolute source_root, S3 identity and crypt secrets
        # are absent from the signature (issue #150 seam 9).
        signature = inspect.signature(vaults_service.create_vault)
        self.assertEqual(
            list(signature.parameters),
            [
                "name",
                "creation_mode",
                "volume_alias",
                "relative_path",
                # #736 seam: a Retention Lock spec, not exposed by the API.
                "retention_lock",
                "client_for",
            ],
        )
        forbidden = {
            "source_root",
            "s3_bucket",
            "s3_prefix",
            "rclone_remote",
            "crypt_password",
            "password",
            "password2",
        }
        self.assertTrue(forbidden.isdisjoint(signature.parameters))

    def test_every_vault_gets_its_own_vault_target_and_bucket(self) -> None:
        vaults_service.create_vault("Docs")
        vaults_service.create_vault("Photos")
        with PostgresConnection(str(self.database_path)) as connection:
            targets = connection.execute(
                "SELECT bucket, region, key_layout FROM vault_targets ORDER BY id").fetchall()
            columns = {row["column_name"] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='vaults'"
            ).fetchall()}
        self.assertEqual(len(targets), 2)
        self.assertEqual({row["key_layout"] for row in targets}, {"bucket_root"})
        self.assertEqual(sorted(row["bucket"] for row in targets), sorted(self.s3.buckets))
        self.assertTrue({"slug", "s3_bucket", "s3_prefix", "enabled"}.isdisjoint(columns))

    def test_blank_name_is_rejected(self) -> None:
        with self.assertRaises(vaults_service.InvalidVaultName):
            vaults_service.create_vault("   ")
        self.assertEqual(self._vaults(), [])

    def test_creation_is_unavailable_without_a_region_or_credentials(self) -> None:
        unconfigured = replace(self.settings, aws_region="")
        with patch("app.services.vaults.settings", unconfigured):
            with self.assertRaises(vaults_service.VaultProvisioningUnavailable):
                vaults_service.create_vault("Docs")
        with patch("app.services.vault_buckets._default_client",
                   side_effect=RuntimeError("AWS credentials are not configured")):
            with self.assertRaises(vaults_service.VaultProvisioningUnavailable) as caught:
                vaults_service.create_vault("Docs")
        self.assertIn("AWS credentials", str(caught.exception))
        self.assertEqual(self._vaults(), [])
        self.assertEqual(self.s3.calls, [])

    def test_directory_creation_failure_rolls_back_the_database_rows(self) -> None:
        # Simulate the local directory step failing (e.g. a stale mount
        # collision or a filesystem error) after the DB rows are staged but
        # before the transaction commits: nothing must survive.
        with patch.object(vaults_service.os, "makedirs", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                vaults_service.create_vault("Docs")

        self.assertEqual(self._vaults(), [])
        self.assertEqual(list(self.managed_root.iterdir()), [])

    def test_concurrent_creation_never_collides_in_db_or_filesystem(self) -> None:
        def _create(index: int) -> dict:
            return vaults_service.create_vault(f"Vault {index}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_create, range(8)))

        uuids = {vault["uuid"] for vault in results}
        source_roots = {vault["source_root"] for vault in results}
        buckets = {vault["s3_bucket"] for vault in results}
        self.assertEqual(len(uuids), 8)
        self.assertEqual(len(source_roots), 8)
        self.assertEqual(len(buckets), 8)
        self.assertEqual({vault["lifecycle"] for vault in results}, {"active"})

        created_dirs = {p.name for p in self.managed_root.iterdir() if p.is_dir()}
        self.assertEqual(created_dirs, uuids)
        self.assertEqual(len(self._vaults()), 8)


if __name__ == "__main__":
    unittest.main()
