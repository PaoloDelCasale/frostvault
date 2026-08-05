"""Focused runtime-config custody coverage for issue #201."""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
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

    def _legacy_run(self) -> Path:
        """Create an old named fallback hierarchy without secret material."""
        self.runtime_directory.mkdir(mode=0o700)
        self.runtime_directory.chmod(0o700)
        run = self.runtime_directory / f"run-{os.getpid()}-{'a' * 32}"
        run.mkdir(mode=0o700)
        (run / "config.rclone.conf").write_text("obsolete", encoding="utf-8")
        (run / ".frostvault-runtime.json").write_text("obsolete", encoding="utf-8")
        run.chmod(0o500)
        return run

    def test_anonymous_config_is_a_private_memfd_without_filesystem_residue(self) -> None:
        with rclone_runtime.vault_rclone_config(self.vault) as runtime:
            runtime_path = runtime.path
            self.assertTrue(runtime_path.is_file())
            self.assertTrue(
                str(runtime_path).startswith(f"/proc/{os.getpid()}/fd/")
            )
            self.assertEqual(stat.S_IMODE(runtime_path.stat().st_mode), 0o600)
            self.assertFalse(
                any(
                    secret in runtime.config_text
                    for secret in (
                        "runtime-config-password",
                        "runtime-config-password2",
                        self.stored.password_ciphertext,
                        self.stored.password2_ciphertext,
                    )
                ),
                "runtime config must not include plaintext or persisted crypt material",
            )
            self.assertFalse(self.runtime_directory.exists())
        self.assertFalse(runtime_path.exists())
        self.assertFalse(self.runtime_directory.exists())

    def test_anonymous_config_is_removed_when_cancelled(self) -> None:
        with self.assertRaises(asyncio.CancelledError):
            with rclone_runtime.vault_rclone_config(self.vault) as runtime:
                runtime_path = runtime.path
                raise asyncio.CancelledError()
        self.assertFalse(runtime_path.exists())
        self.assertFalse(self.runtime_directory.exists())

    def test_runtime_config_fails_closed_without_anonymous_storage(self) -> None:
        with patch(
            "app.services.rclone_runtime._anonymous_runtime_config",
            return_value=None,
        ):
            with self.assertRaises(rclone_runtime.RuntimeConfigStorageError):
                with rclone_runtime.vault_rclone_config(self.vault):
                    self.fail("a named fallback must never be created")
        self.assertFalse(self.runtime_directory.exists())

    def test_filename_decoding_batches_and_reuses_repeated_keys(self) -> None:
        runtime = rclone_runtime.RuntimeRcloneConfig(
            path=self.root / "runtime.conf",
            remote_name="vault",
            config_text="",
            secrets=None,
        )
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            values = list(command[command.index("vault:") + 1 :])
            calls.append(values)
            return SimpleNamespace(
                returncode=0,
                stdout="\n".join(f"decoded/{value}" for value in values),
                stderr="secret-material-must-not-be-read",
            )

        with patch("app.services.rclone_runtime.subprocess.run", side_effect=fake_run):
            decoded, failed = rclone_runtime.decode_object_relative_paths(
                runtime,
                ["a", "b", "a", "c"],
            )

        self.assertEqual(
            decoded,
            {"a": "decoded/a", "b": "decoded/b", "c": "decoded/c"},
        )
        self.assertEqual(failed, set())
        self.assertEqual(calls, [["a", "b", "c"]])

    def test_filename_decoding_failure_does_not_expose_process_output(self) -> None:
        runtime = rclone_runtime.RuntimeRcloneConfig(
            path=self.root / "runtime.conf",
            remote_name="vault",
            config_text="",
            secrets=None,
        )

        def failed_run(_command, **_kwargs):
            return SimpleNamespace(
                returncode=1,
                stdout="ciphertext-output",
                stderr="password=secret-material",
            )

        with patch("app.services.rclone_runtime.subprocess.run", side_effect=failed_run):
            decoded, failed = rclone_runtime.decode_object_relative_paths(
                runtime,
                ["encrypted-name"],
            )

        self.assertEqual(decoded, {})
        self.assertEqual(failed, {"encrypted-name"})
        with self.assertRaisesRegex(RuntimeError, "filename decoding failed"):
            rclone_runtime.decode_object_relative_path(runtime, "encrypted-name")

    @unittest.skipUnless(shutil.which("rclone"), "rclone binary is required")
    def test_anonymous_memfd_config_is_accepted_by_rclone(self) -> None:
        with rclone_runtime.vault_rclone_config(self.vault) as runtime:
            for _ in range(2):
                completed = subprocess.run(
                    ["rclone", "--config", str(runtime.path), "listremotes"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertIn("vault:", completed.stdout)

    def test_cleanup_reports_legacy_named_residue_without_removing_it(self) -> None:
        run = self._legacy_run()

        result = rclone_runtime.cleanup_runtime_configs()

        self.assertEqual(result.removed, 0)
        self.assertEqual(result.skipped_active, 0)
        self.assertEqual(result.skipped_foreign, 1)
        self.assertEqual(result.skipped_unsafe, 0)
        self.assertEqual(result.skipped_raced, 0)
        self.assertTrue(run.is_dir())
        self.assertTrue((run / "config.rclone.conf").is_file())
        self.assertTrue((run / ".frostvault-runtime.json").is_file())

    def test_cleanup_reports_a_legacy_root_symlink_without_following_it(self) -> None:
        target = self.temp_root / "foreign-target"
        target.mkdir()
        (target / "retain").write_text("foreign", encoding="utf-8")
        self.runtime_directory.symlink_to(target, target_is_directory=True)

        result = rclone_runtime.cleanup_runtime_configs()

        self.assertEqual(result.removed, 0)
        self.assertEqual(result.skipped_foreign, 1)
        self.assertTrue(self.runtime_directory.is_symlink())
        self.assertTrue((target / "retain").is_file())

    def test_cleanup_never_unlinks_replacements_after_legacy_observation(self) -> None:
        """Exercise the old final-check-to-unlink window deterministically.

        The old fallback validated config, manifest, and run-directory inodes,
        then unlinked their names.  Replace that complete hierarchy immediately
        after this implementation observes the legacy root, and make every
        destructive primitive fail the test.  The read-only redesign must leave
        all replacement names intact.
        """
        run = self._legacy_run()
        config = run / "config.rclone.conf"
        manifest = run / ".frostvault-runtime.json"
        original_has_residue = rclone_runtime._legacy_runtime_has_residue
        original_unlink = os.unlink
        original_rmdir = os.rmdir
        replaced = False

        def replace_after_observation(directory_fd: int) -> bool | None:
            nonlocal replaced
            has_residue = original_has_residue(directory_fd)
            self.assertTrue(has_residue)
            run.chmod(0o700)
            original_unlink(config)
            original_unlink(manifest)
            original_rmdir(run)
            replacement = self.runtime_directory / "replacement-run"
            replacement.mkdir(mode=0o700)
            replacement_config_path = replacement / config.name
            replacement_manifest_path = replacement / manifest.name
            replacement_config_path.write_text("foreign replacement", encoding="utf-8")
            replacement_manifest_path.write_text("foreign replacement", encoding="utf-8")
            os.replace(replacement, run)
            replaced = True
            return has_residue

        with (
            patch(
                "app.services.rclone_runtime._legacy_runtime_has_residue",
                side_effect=replace_after_observation,
            ),
            patch(
                "app.services.rclone_runtime.os.unlink",
                side_effect=AssertionError("cleanup must not unlink a name"),
            ),
            patch(
                "app.services.rclone_runtime.os.rmdir",
                side_effect=AssertionError("cleanup must not remove a directory"),
            ),
        ):
            result = rclone_runtime.cleanup_runtime_configs()

        self.assertTrue(replaced)
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.skipped_foreign, 1)
        self.assertTrue(run.is_dir())
        self.assertTrue((run / config.name).is_file())
        self.assertTrue((run / manifest.name).is_file())
        self.assertEqual((run / config.name).read_text(encoding="utf-8"), "foreign replacement")
        self.assertEqual(
            (run / manifest.name).read_text(encoding="utf-8"),
            "foreign replacement",
        )

    def _startable_database(self) -> Path:
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
        return database_path

    def test_database_initialization_reports_but_retains_legacy_residue(self) -> None:
        database_path = self._startable_database()
        run = self._legacy_run()
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
        self.assertTrue(run.is_dir())

    def test_database_reports_retained_residue_without_runtime_contents(self) -> None:
        database_path = self._startable_database()
        run = self._legacy_run()
        (run / "config.rclone.conf").chmod(0o600)
        (run / "config.rclone.conf").write_text(
            "runtime-content-must-not-appear",
            encoding="utf-8",
        )
        run.chmod(0o500)
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(database_path),
        )

        with patch("app.database.settings", database_settings):
            with self.assertLogs("app.database", level="WARNING") as captured:
                initialize_database()

        messages = "\n".join(captured.output)
        self.assertIn("legacy runtime residue retained", messages)
        self.assertIn("untrusted=1", messages)
        self.assertFalse(
            "runtime-content-must-not-appear" in messages,
            "startup logging must not include runtime contents",
        )
        self.assertTrue((run / "config.rclone.conf").is_file())


if __name__ == "__main__":
    unittest.main()
