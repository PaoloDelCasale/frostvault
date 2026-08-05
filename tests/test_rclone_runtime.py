"""Focused runtime-config custody coverage for issue #201."""
from __future__ import annotations

import asyncio
import multiprocessing
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


def _concurrent_cleanup_worker(
    temp_root: str,
    master_key: str,
    barrier: multiprocessing.synchronize.Barrier,
    queue: multiprocessing.queues.Queue,
) -> None:
    """Run one cleanup process without returning any config-derived content."""
    from app.services import rclone_runtime as worker_runtime

    worker_runtime.settings = SimpleNamespace(archive_master_key=master_key)
    worker_runtime.tempfile.gettempdir = lambda: temp_root
    try:
        barrier.wait(timeout=15)
        result = worker_runtime.cleanup_runtime_configs()
        queue.put(
            (
                "ok",
                result.removed,
                result.skipped_active,
                result.skipped_foreign,
                result.skipped_unsafe,
                result.skipped_raced,
            )
        )
    except BaseException as exc:  # pragma: no cover - assertion reports only type.
        queue.put(("error", type(exc).__name__))


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

    def _abandon_fallback(self):
        fallback = rclone_runtime._create_protected_runtime_config(
            "[vault]\ntype = crypt\n"
        )
        os.close(fallback.descriptor)
        self.addCleanup(self._make_run_writable, fallback.run_path)
        return fallback

    @staticmethod
    def _make_run_writable(path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        except OSError:
            pass

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

    def test_fallback_config_is_private_and_is_removed_normally(self) -> None:
        with patch(
            "app.services.rclone_runtime._anonymous_runtime_config",
            return_value=None,
        ):
            with rclone_runtime.vault_rclone_config(self.vault) as runtime:
                runtime_path = runtime.path
                run_directory = runtime_path.parent
                self.assertEqual(run_directory.parent, self.runtime_directory)
                self.assertEqual(
                    stat.S_IMODE(self.runtime_directory.stat().st_mode), 0o700
                )
                self.assertEqual(stat.S_IMODE(run_directory.stat().st_mode), 0o500)
                self.assertEqual(stat.S_IMODE(runtime_path.stat().st_mode), 0o600)
                self.assertTrue(runtime_path.is_file())
            self.assertFalse(runtime_path.exists())
            self.assertFalse(run_directory.exists())
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
        self.assertFalse(runtime_path.parent.exists())

    @unittest.skipUnless(shutil.which("rclone"), "rclone binary is required")
    def test_sealed_fallback_config_is_accepted_by_rclone(self) -> None:
        with patch(
            "app.services.rclone_runtime._anonymous_runtime_config",
            return_value=None,
        ):
            with rclone_runtime.vault_rclone_config(self.vault) as runtime:
                completed = subprocess.run(
                    ["rclone", "--config", str(runtime.path), "listremotes"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("vault:", completed.stdout)

    def test_cleanup_removes_signed_abnormal_residue(self) -> None:
        residue = self._abandon_fallback()

        result = rclone_runtime.cleanup_runtime_configs()

        self.assertEqual(result.removed, 1)
        self.assertEqual(result.skipped_active, 0)
        self.assertEqual(result.skipped_foreign, 0)
        self.assertEqual(result.skipped_unsafe, 0)
        self.assertEqual(result.skipped_raced, 0)
        self.assertFalse(residue.run_path.exists())

    def test_cleanup_never_deletes_an_active_locked_config(self) -> None:
        fallback = rclone_runtime._create_protected_runtime_config(
            "[vault]\ntype = crypt\n"
        )
        try:
            result = rclone_runtime.cleanup_runtime_configs()
            self.assertEqual(result.removed, 0)
            self.assertEqual(result.skipped_active, 1)
            self.assertTrue(fallback.config_path.is_file())
            self.assertTrue(fallback.run_path.is_dir())
        finally:
            os.close(fallback.descriptor)
        result = rclone_runtime.cleanup_runtime_configs()
        self.assertEqual(result.removed, 1)
        self.assertFalse(fallback.run_path.exists())

    def test_cleanup_never_deletes_matching_foreign_file_or_symlink(self) -> None:
        directory = rclone_runtime.runtime_config_directory()
        foreign_file = directory / f"run-101-{'a' * 32}"
        foreign_file.write_text("foreign", encoding="utf-8")
        foreign_file.chmod(0o600)
        target = directory / "foreign-target"
        target.write_text("target", encoding="utf-8")
        foreign_link = directory / f"run-102-{'b' * 32}"
        foreign_link.symlink_to(target.name)

        result = rclone_runtime.cleanup_runtime_configs()

        self.assertEqual(result.removed, 0)
        self.assertGreaterEqual(result.skipped_foreign, 3)
        self.assertTrue(foreign_file.is_file())
        self.assertTrue(foreign_link.is_symlink())
        self.assertTrue(target.is_file())

    def test_cleanup_leaves_mode_mismatched_signed_residue(self) -> None:
        residue = self._abandon_fallback()
        residue.config_path.chmod(0o640)

        result = rclone_runtime.cleanup_runtime_configs()

        self.assertEqual(result.removed, 0)
        self.assertEqual(result.skipped_foreign, 1)
        self.assertTrue(residue.config_path.is_file())
        residue.config_path.chmod(0o600)
        self.assertEqual(rclone_runtime.cleanup_runtime_configs().removed, 1)

    def test_cleanup_never_deletes_a_symlink_inside_a_signed_run(self) -> None:
        residue = self._abandon_fallback()
        residue.run_path.chmod(0o700)
        target = self.runtime_directory / "foreign-symlink-target"
        target.write_text("foreign", encoding="utf-8")
        residue.config_path.unlink()
        residue.config_path.symlink_to(target)
        residue.run_path.chmod(0o500)

        result = rclone_runtime.cleanup_runtime_configs()

        self.assertEqual(result.removed, 0)
        self.assertGreaterEqual(result.skipped_foreign, 1)
        self.assertTrue(residue.config_path.is_symlink())
        self.assertTrue(target.is_file())

    def test_cleanup_leaves_owner_mismatched_signed_residue_when_testable(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("changing ownership requires a root test runner")
        residue = self._abandon_fallback()
        os.chown(residue.config_path, 1, os.getegid())
        try:
            result = rclone_runtime.cleanup_runtime_configs()
            self.assertEqual(result.removed, 0)
            self.assertEqual(result.skipped_foreign, 1)
            self.assertTrue(residue.config_path.is_file())
        finally:
            os.chown(residue.config_path, os.geteuid(), os.getegid())
        self.assertEqual(rclone_runtime.cleanup_runtime_configs().removed, 1)

    def test_cleanup_detects_pathname_replacement_before_unlink(self) -> None:
        residue = self._abandon_fallback()
        original = rclone_runtime._entry_matches_descriptor
        replaced = False
        config_checks = 0

        def replace_before_final_check(
            directory_fd: int,
            name: str,
            descriptor: int,
            *,
            directory: bool,
            mode: int,
        ) -> bool:
            nonlocal config_checks, replaced
            if name == rclone_runtime.RUNTIME_CONFIG_FILENAME:
                config_checks += 1
            if (
                not replaced
                and name == rclone_runtime.RUNTIME_CONFIG_FILENAME
                and config_checks == 2
            ):
                os.fchmod(directory_fd, 0o700)
                replacement = residue.run_path / "replacement"
                replacement.write_text("foreign replacement", encoding="utf-8")
                replacement.chmod(0o600)
                os.replace(replacement, residue.config_path)
                replaced = True
            return original(
                directory_fd,
                name,
                descriptor,
                directory=directory,
                mode=mode,
            )

        with patch(
            "app.services.rclone_runtime._entry_matches_descriptor",
            side_effect=replace_before_final_check,
        ):
            result = rclone_runtime.cleanup_runtime_configs()

        self.assertTrue(replaced)
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.skipped_raced, 1)
        self.assertTrue(residue.config_path.is_file())
        self.assertTrue(residue.run_path.is_dir())

    def test_concurrent_cleanup_processes_remove_one_residue_once(self) -> None:
        residue = self._abandon_fallback()
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        queue = context.Queue()
        workers = [
            context.Process(
                target=_concurrent_cleanup_worker,
                args=(str(self.temp_root), self.master_key, barrier, queue),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
            self.assertEqual(worker.exitcode, 0)
        outcomes = [queue.get(timeout=5) for _ in workers]

        self.assertTrue(all(outcome[0] == "ok" for outcome in outcomes))
        self.assertEqual(sum(outcome[1] for outcome in outcomes), 1)
        self.assertFalse(residue.run_path.exists())

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

    def test_database_initialization_runs_runtime_residue_cleanup(self) -> None:
        database_path = self._startable_database()
        residue = self._abandon_fallback()
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
        self.assertFalse(residue.run_path.exists())

    def test_database_reports_deferred_cleanup_without_runtime_contents(self) -> None:
        database_path = self._startable_database()
        foreign = self.runtime_directory / f"run-111-{'c' * 32}"
        rclone_runtime.runtime_config_directory()
        foreign.write_text("runtime-content-must-not-appear", encoding="utf-8")
        foreign.chmod(0o600)
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(database_path),
        )

        with patch("app.database.settings", database_settings):
            with self.assertLogs("app.database", level="WARNING") as captured:
                initialize_database()

        messages = "\n".join(captured.output)
        self.assertIn("untrusted=1", messages)
        self.assertNotIn("runtime-content-must-not-appear", messages)
        self.assertTrue(foreign.is_file())


if __name__ == "__main__":
    unittest.main()
