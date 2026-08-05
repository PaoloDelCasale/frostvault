"""Focused runtime-config custody coverage for issue #201."""
from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.database import SQLiteConnection, initialize_database
from app.services import rclone_runtime
from app.services.vault_crypto import CryptSecrets, encrypt_vault_secrets
from tests.test_database import run_alembic


class RuntimeRcloneConfigStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.temp_root = self.root / "tmp"
        self.temp_root.mkdir()
        self.rclone_config = self.root / "rclone.conf"
        self.rclone_config.write_text("[base]\ntype = local\n", encoding="utf-8")

        self.master_key = Fernet.generate_key().decode("ascii")
        self.stored = encrypt_vault_secrets(
            CryptSecrets(
                password="runtime-config-password",
                password2="runtime-config-password2",
            ),
            self.master_key,
        )
        self.vault = {
            "encryption_mode": "crypt",
            "s3_bucket": "archive",
            "s3_prefix": "vaults/runtime",
            "rclone_remote": "base",
            "crypt_password_ciphertext": self.stored.password_ciphertext,
            "crypt_password2_ciphertext": self.stored.password2_ciphertext,
        }
        self.test_settings = SimpleNamespace(
            archive_master_key=self.master_key,
            rclone_config=str(self.rclone_config),
        )
        for target in (
            "app.services.rclone_runtime.settings",
            "app.services.vault_crypto.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        tempdir_patcher = patch(
            "app.services.rclone_runtime.tempfile.gettempdir",
            return_value=str(self.temp_root),
        )
        tempdir_patcher.start()
        self.addCleanup(tempdir_patcher.stop)

    @property
    def runtime_directory(self) -> Path:
        return self.temp_root / rclone_runtime.RUNTIME_DIRECTORY_NAME

    def test_anonymous_config_leaves_no_filesystem_residue(self) -> None:
        with rclone_runtime.vault_rclone_config(self.vault) as runtime:
            runtime_path = runtime.path
            self.assertTrue(runtime_path.is_file())
            self.assertTrue(
                str(runtime_path).startswith(f"/proc/{os.getpid()}/fd/")
            )
            self.assertNotIn("runtime-config-password", runtime.config_text)
            self.assertNotIn("runtime-config-password2", runtime.config_text)
            self.assertNotIn(self.stored.password_ciphertext, runtime.config_text)
            self.assertNotIn(self.stored.password2_ciphertext, runtime.config_text)
            self.assertFalse(self.runtime_directory.exists())
        self.assertFalse(runtime_path.exists())
        self.assertFalse(self.runtime_directory.exists())

    def test_fallback_config_has_private_permissions_and_is_removed_normally(self) -> None:
        with patch(
            "app.services.rclone_runtime._anonymous_runtime_config",
            return_value=None,
        ):
            with rclone_runtime.vault_rclone_config(self.vault) as runtime:
                runtime_path = runtime.path
                self.assertEqual(runtime_path.parent, self.runtime_directory)
                self.assertEqual(
                    stat.S_IMODE(self.runtime_directory.stat().st_mode), 0o700
                )
                self.assertEqual(stat.S_IMODE(runtime_path.stat().st_mode), 0o600)
                self.assertTrue(runtime_path.is_file())
            self.assertFalse(runtime_path.exists())
        self.assertEqual(list(self.runtime_directory.iterdir()), [])

    def test_fallback_config_is_removed_when_cancelled(self) -> None:
        with patch(
            "app.services.rclone_runtime._anonymous_runtime_config",
            return_value=None,
        ):
            with self.assertRaises(asyncio.CancelledError):
                with rclone_runtime.vault_rclone_config(self.vault) as runtime:
                    runtime_path = runtime.path
                    raise asyncio.CancelledError()
        self.assertFalse(runtime_path.exists())
        self.assertEqual(list(self.runtime_directory.iterdir()), [])

    def test_cleanup_removes_only_generated_abnormal_residue(self) -> None:
        directory = rclone_runtime.runtime_config_directory()
        residue = directory / (
            f"{rclone_runtime.RUNTIME_CONFIG_PREFIX}interrupted"
            f"{rclone_runtime.RUNTIME_CONFIG_SUFFIX}"
        )
        residue.write_text("generated-config-residue", encoding="utf-8")
        residue.chmod(0o600)
        unrelated = directory / "operator-note.txt"
        unrelated.write_text("retain", encoding="utf-8")

        self.assertEqual(rclone_runtime.cleanup_runtime_configs(), 1)
        self.assertFalse(residue.exists())
        self.assertTrue(unrelated.exists())

    def test_database_initialization_runs_runtime_residue_cleanup(self) -> None:
        database_path = self.root / "app.db"
        migrated = run_alembic(database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', 'hash', TRUE)
                """
            )

        directory = rclone_runtime.runtime_config_directory()
        residue = directory / (
            f"{rclone_runtime.RUNTIME_CONFIG_PREFIX}restart"
            f"{rclone_runtime.RUNTIME_CONFIG_SUFFIX}"
        )
        residue.write_text("interrupted-runtime-config", encoding="utf-8")
        residue.chmod(0o600)
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(database_path),
        )
        with (
            patch("app.database.settings", database_settings),
            patch(
                "app.database.cleanup_runtime_configs",
                wraps=rclone_runtime.cleanup_runtime_configs,
            ) as cleanup,
        ):
            initialize_database()
        cleanup.assert_called_once_with()
        self.assertFalse(residue.exists())


if __name__ == "__main__":
    unittest.main()
