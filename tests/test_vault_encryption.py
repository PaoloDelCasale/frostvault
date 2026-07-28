"""Service-level coverage for vault encryption mode and recovery (issue #6)."""
from __future__ import annotations

import inspect
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.config import Settings
from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services import rclone_runtime, source_layout, vault_recovery
from app.services import vaults as vaults_service
from app.services.vault_crypto import CryptSecrets, decrypt_vault_secrets
from tests.test_database import run_alembic


class VaultEncryptionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.database_path = self.root / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.sources_root = self.root / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        self.managed_root = source_layout.ensure_managed_directory()
        self.base_data = self.root / "cloud-data"
        self.base_data.mkdir()
        self.rclone_config = self.root / "rclone.conf"
        self.rclone_config.write_text(
            "[base]\n"
            "type = local\n"
            "nounc = true\n",
            encoding="utf-8",
        )

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (2, 'bob', 'Bob', 'hash', 0)"
            )

        self.master_key = Fernet.generate_key().decode("ascii")
        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            vault_s3_bucket="test-bucket",
            vault_rclone_remote="plain-remote",
            vault_rclone_base_remote="base",
            archive_master_key=self.master_key,
            rclone_config=str(self.rclone_config),
        )
        for target in (
            "app.database.settings",
            "app.services.vaults.settings",
            "app.services.rclone_runtime.settings",
            "app.services.vault_recovery.settings",
            "app.services.vault_crypto.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _create_crypt(self, name: str = "Secret Archive", slug: str | None = None):
        return vaults_service.create_vault_for_user(
            1, name, slug=slug, encryption_mode="crypt"
        )

    def test_create_accepts_immutable_encryption_mode(self) -> None:
        signature = inspect.signature(vaults_service.create_vault_for_user)
        self.assertIn("encryption_mode", signature.parameters)
        self.assertNotIn("source_root", signature.parameters)


    def test_plain_vault_has_no_stored_crypt_material(self) -> None:
        vault = vaults_service.create_vault_for_user(1, "Plain Docs")
        self.assertEqual(vault["encryption_mode"], "plain")
        self.assertIsNone(vault["crypt_password_ciphertext"])
        self.assertIsNone(vault["crypt_password2_ciphertext"])
        self.assertIsNone(vault["recovery_custody_confirmed_at"])
        self.assertEqual(vault["rclone_remote"], "plain-remote")

    def test_crypt_vaults_get_independent_secrets_encrypted_at_rest(self) -> None:
        first = self._create_crypt("One", slug="one")
        second = self._create_crypt("Two", slug="two")

        self.assertEqual(first["encryption_mode"], "crypt")
        self.assertEqual(second["encryption_mode"], "crypt")
        self.assertIsNone(first["recovery_custody_confirmed_at"])
        self.assertNotEqual(
            first["crypt_password_ciphertext"],
            second["crypt_password_ciphertext"],
        )
        self.assertNotEqual(
            first["crypt_password2_ciphertext"],
            second["crypt_password2_ciphertext"],
        )
        self.assertEqual(first["rclone_remote"], "base")

        first_secrets = decrypt_vault_secrets(
            vault_recovery.stored_secrets_from_vault(first), self.master_key
        )
        second_secrets = decrypt_vault_secrets(
            vault_recovery.stored_secrets_from_vault(second), self.master_key
        )
        self.assertNotEqual(first_secrets.password, second_secrets.password)
        self.assertNotIn(first_secrets.password, first["crypt_password_ciphertext"])

    def test_crypt_creation_requires_master_key_and_base_remote(self) -> None:
        bare = replace(self.settings, archive_master_key="", vault_rclone_base_remote="")
        with patch("app.services.vaults.settings", bare):
            with self.assertRaises(vaults_service.VaultProvisioningUnavailable):
                vaults_service.create_vault_for_user(
                    1, "Nope", encryption_mode="crypt"
                )

    def test_invalid_encryption_mode_is_rejected(self) -> None:
        with self.assertRaises(vaults_service.InvalidVaultName):
            vaults_service.create_vault_for_user(
                1, "Docs", encryption_mode="rot13"
            )

    def test_uploads_blocked_until_recovery_custody_confirmed(self) -> None:
        vault = self._create_crypt()
        with SQLiteConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=vault["id"],
                path="notes.txt",
                file_type="regular",
                size=12,
                mtime_ns=1,
                observed_at="2026-01-01T00:00:00+00:00",
            )
            with self.assertRaises(vault_recovery.RecoveryCustodyRequired):
                catalog.queue_jobs(
                    vault_id=vault["id"],
                    path="notes.txt",
                    action="upload",
                    requested_by=1,
                    requested_at="2026-01-01T00:00:00+00:00",
                    group_id="g1",
                    is_directory=False,
                )

            vault_recovery.confirm_recovery_custody(connection, vault_id=vault["id"])
            job_ids, _, eligible = catalog.queue_jobs(
                vault_id=vault["id"],
                path="notes.txt",
                action="upload",
                requested_by=1,
                requested_at="2026-01-01T00:00:01+00:00",
                group_id="g2",
                is_directory=False,
            )
        self.assertEqual(eligible, 1)
        self.assertEqual(len(job_ids), 1)

    def test_recovery_export_round_trips_through_standalone_rclone(self) -> None:
        if shutil.which("rclone") is None:
            self.skipTest("rclone binary is required for recovery export coverage")

        vault = self._create_crypt()
        export = vault_recovery.build_recovery_export(vault)
        self.assertIn("filename_encryption = standard", export)
        self.assertIn("directory_name_encryption = true", export)
        self.assertIn(f"vaults/{vault['uuid']}/", export)

        secrets = decrypt_vault_secrets(
            vault_recovery.stored_secrets_from_vault(vault), self.master_key
        )
        self.assertNotIn(secrets.password, export)
        # Export must carry rclone-obscured material, never raw DB ciphertext.
        self.assertNotIn(vault["crypt_password_ciphertext"], export)

        export_path = self.root / "recovery.conf"
        export_path.write_text(export, encoding="utf-8")
        # Rewrite the base remote in the export to a local directory for the test.
        rewritten = export.replace(
            f"remote = base:test-bucket/vaults/{vault['uuid']}/",
            f"remote = base:{self.base_data}/",
        )
        # Ensure the export's base section is local for this isolated restore.
        if "[base]" not in rewritten:
            rewritten = (
                "[base]\ntype = local\nnounc = true\n\n" + rewritten
            )
        else:
            rewritten = rewritten.replace(
                "[base]\ntype = local\nnounc = true",
                "[base]\ntype = local\nnounc = true",
            )
        export_path.write_text(rewritten, encoding="utf-8")

        source = self.root / "plain.txt"
        source.write_text("recover-me-exactly", encoding="utf-8")
        remote_name = vault_recovery.RECOVERY_REMOTE_NAME
        completed = subprocess.run(
            [
                "rclone",
                "--config",
                str(export_path),
                "copyto",
                str(source),
                f"{remote_name}:docs/plain.txt",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        underlying = [p for p in self.base_data.rglob("*") if p.is_file()]
        self.assertTrue(underlying)
        for path in underlying:
            self.assertNotIn("plain.txt", path.name)
            self.assertNotIn("docs", path.parts)

        restored = self.root / "restored.txt"
        completed = subprocess.run(
            [
                "rclone",
                "--config",
                str(export_path),
                "copyto",
                f"{remote_name}:docs/plain.txt",
                str(restored),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(restored.read_text(encoding="utf-8"), "recover-me-exactly")

    def test_runtime_config_enables_standard_name_encryption(self) -> None:
        if shutil.which("rclone") is None:
            self.skipTest("rclone binary is required for runtime config coverage")

        vault = self._create_crypt()
        with rclone_runtime.vault_rclone_config(vault) as runtime:
            self.assertIn("filename_encryption = standard", runtime.config_text)
            self.assertIn("directory_name_encryption = true", runtime.config_text)
            encoded = rclone_runtime.encode_object_relative_path(
                runtime, "folder/secret.txt"
            )
        self.assertNotIn("folder", encoded)
        self.assertNotIn("secret.txt", encoded)
        self.assertNotIn(".", encoded.split("/")[-1])

    def test_mode_change_is_not_an_in_place_update(self) -> None:
        vault = vaults_service.create_vault_for_user(1, "Plain")
        with SQLiteConnection(str(self.database_path)) as connection:
            # Application API: creating another vault is the migration path.
            other = vaults_service.create_vault_for_user(
                1, "Crypt", slug="crypt-copy", encryption_mode="crypt"
            )
            row = connection.execute(
                "SELECT encryption_mode FROM vaults WHERE id=%s", (vault["id"],)
            ).fetchone()
        self.assertEqual(row["encryption_mode"], "plain")
        self.assertEqual(other["encryption_mode"], "crypt")
        self.assertNotEqual(vault["uuid"], other["uuid"])


class VaultEncryptionMigrationTests(unittest.TestCase):
    def test_migration_adds_encryption_columns_with_plain_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO vaults(slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                    "VALUES ('legacy', 'Legacy', '/src', 'b', 'p', 'r')"
                )
                row = connection.execute(
                    "SELECT encryption_mode, crypt_password_ciphertext, "
                    "crypt_password2_ciphertext, recovery_custody_confirmed_at "
                    "FROM vaults WHERE slug='legacy'"
                ).fetchone()
            self.assertEqual(row["encryption_mode"], "plain")
            self.assertIsNone(row["crypt_password_ciphertext"])
            self.assertIsNone(row["crypt_password2_ciphertext"])
            self.assertIsNone(row["recovery_custody_confirmed_at"])


if __name__ == "__main__":
    unittest.main()
