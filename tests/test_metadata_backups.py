"""Encrypted metadata backups and disaster recovery (issue #15).

Seams under test (confirmed for this issue):
- ``app.services.metadata_backups.create_metadata_backup``
  — encrypted, checksummed DB+config artifact; never embeds the master key
- ``app.services.metadata_backups.list_backup_artifacts`` /
  ``backup_status`` / ``rotate_local_retention``
  — retention-aware listing and operator status
- ``app.services.metadata_backups.store_backup_to_object_store``
  — injectable ObjectStore boundary for encrypted ``system/backups/``
- ``app.services.metadata_backups.verify_restore_isolated``
  — restore into a temporary database without touching live DB or vault S3
- ``app.services.metadata_backups.run_pre_upgrade_backup``
  — failure blocks application-managed schema upgrades
- ``app.services.metadata_backups.default_object_store``
  — public ObjectStore factory for configured off-host backup (BUG-010)
- ``app.storage._verify_latest_metadata_backup_once``
  — scheduled verify must stamp the run bound by local_path/digest (BUG-016)
- Admin HTTP: list / status / manual run / download
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from app import storage
from app.database import SQLiteConnection
from app.services import metadata_backups
from tests.test_database import run_alembic


class CreateMetadataBackupTests(unittest.TestCase):
    """Vertical slice: create an encrypted, digestsable backup without the master key."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "live.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', 'hash', TRUE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/src', 'bucket', 'vaults/abc/', 'remote')
                """
            )

        self.master_key = Fernet.generate_key().decode("ascii")
        self.settings = replace(
            __import__("app.config", fromlist=["settings"]).settings,
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            archive_master_key=self.master_key,
            bootstrap_admin_password="super-secret-bootstrap-password",
            oidc_client_secret="oidc-secret-value",
        )

    def test_create_backup_is_encrypted_checksummed_and_excludes_master_key(self) -> None:
        with patch.object(metadata_backups, "settings", self.settings):
            artifact = metadata_backups.create_metadata_backup(
                reason="manual",
                backup_dir=self.backup_dir,
                master_key=self.master_key,
                config_snapshot=metadata_backups.build_config_snapshot(self.settings),
            )

        self.assertTrue(artifact["path"].exists())
        self.assertEqual(artifact["backend"], "sqlite")
        self.assertEqual(artifact["reason"], "manual")
        self.assertEqual(len(artifact["digest_sha256"]), 64)
        self.assertEqual(
            artifact["digest_sha256"],
            hashlib.sha256(artifact["path"].read_bytes()).hexdigest(),
        )

        ciphertext = artifact["path"].read_bytes()
        self.assertNotIn(self.master_key.encode("ascii"), ciphertext)
        self.assertNotIn(b"super-secret-bootstrap-password", ciphertext)
        self.assertNotIn(b"oidc-secret-value", ciphertext)

        plaintext = Fernet(self.master_key.encode("ascii")).decrypt(ciphertext)
        payload = metadata_backups.unpack_backup_payload(plaintext)
        self.assertEqual(payload["manifest"]["backend"], "sqlite")
        self.assertIn("database", payload)
        self.assertNotIn("archive_master_key", payload["config"])
        self.assertNotIn(self.master_key, json.dumps(payload["config"]))
        self.assertNotIn("super-secret-bootstrap-password", json.dumps(payload["config"]))

        restored_db = self.root / "restored.db"
        restored_db.write_bytes(payload["database"])
        with SQLiteConnection(str(restored_db)) as connection:
            users = connection.execute("SELECT username FROM users").fetchall()
            vaults = connection.execute("SELECT slug FROM vaults").fetchall()
        self.assertEqual([row["username"] for row in users], ["admin"])
        self.assertEqual([row["slug"] for row in vaults], ["docs"])


class LocalRetentionRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "live.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.master_key = Fernet.generate_key().decode("ascii")

    def test_retention_keeps_newest_artifacts_only(self) -> None:
        for index in range(4):
            path = self.backup_dir / f"2026010{index}T000000Z-sqlite-scheduled.bak.enc"
            path.write_bytes(b"ciphertext-%d" % index)
            path.with_suffix(path.suffix + ".sha256").write_text(
                hashlib.sha256(path.read_bytes()).hexdigest() + "\n",
                encoding="utf-8",
            )

        removed = metadata_backups.rotate_local_retention(self.backup_dir, keep=2)
        remaining = sorted(p.name for p in self.backup_dir.glob("*.bak.enc"))
        self.assertEqual(len(removed), 2)
        self.assertEqual(
            remaining,
            [
                "20260102T000000Z-sqlite-scheduled.bak.enc",
                "20260103T000000Z-sqlite-scheduled.bak.enc",
            ],
        )
        # Sidecars for removed artifacts are gone; kept ones remain.
        self.assertFalse(
            (self.backup_dir / "20260100T000000Z-sqlite-scheduled.bak.enc.sha256").exists()
        )
        self.assertTrue(
            (self.backup_dir / "20260103T000000Z-sqlite-scheduled.bak.enc.sha256").exists()
        )


class RecordingObjectStore:
    """In-memory ObjectStore double for the S3 system boundary."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key: str, body: bytes) -> None:
        self.objects[key] = body

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))

    def delete_key(self, key: str) -> None:
        self.objects.pop(key, None)


class ObjectStoreBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "live.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.master_key = Fernet.generate_key().decode("ascii")
        self.store = RecordingObjectStore()

    def test_store_backup_uses_system_backups_prefix_only(self) -> None:
        artifact = metadata_backups.create_metadata_backup(
            reason="scheduled",
            backup_dir=self.backup_dir,
            master_key=self.master_key,
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            config_snapshot={"db_backend": "sqlite"},
        )
        stored = metadata_backups.store_backup_to_object_store(
            artifact,
            object_store=self.store,
            prefix="system/backups/",
        )
        self.assertTrue(stored["key"].startswith("system/backups/"))
        self.assertFalse(stored["key"].startswith("vaults/"))
        self.assertIn(stored["key"], self.store.objects)
        self.assertEqual(
            self.store.objects[stored["key"]],
            artifact["path"].read_bytes(),
        )
        digest_key = stored["key"] + ".sha256"
        self.assertEqual(
            self.store.objects[digest_key].decode("utf-8").strip(),
            artifact["digest_sha256"],
        )


class IsolatedRestoreVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "live.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', 'hash', TRUE)
                """
            )
        self.master_key = Fernet.generate_key().decode("ascii")
        self.store = RecordingObjectStore()

    def test_verify_restore_isolated_does_not_touch_live_db_or_vault_objects(self) -> None:
        artifact = metadata_backups.create_metadata_backup(
            reason="manual",
            backup_dir=self.backup_dir,
            master_key=self.master_key,
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            config_snapshot={"db_backend": "sqlite"},
        )
        # Poison the object store with a vault key that must never be touched.
        self.store.objects["vaults/abc/file.bin"] = b"do-not-touch"
        live_before = self.db_path.read_bytes()

        result = metadata_backups.verify_restore_isolated(
            artifact["path"],
            master_key=self.master_key,
            work_dir=self.root / "verify",
            object_store=self.store,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["user_count"], 1)
        self.assertEqual(self.db_path.read_bytes(), live_before)
        self.assertEqual(self.store.objects["vaults/abc/file.bin"], b"do-not-touch")
        self.assertEqual(
            [key for key in self.store.objects if key.startswith("vaults/")],
            ["vaults/abc/file.bin"],
        )


class MetadataVerifyRunBindingTests(unittest.TestCase):
    """BUG-016: scheduled verify must stamp the restore-tested run (REQ-028)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "live.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', 'hash', TRUE)
                """
            )
        self.master_key = Fernet.generate_key().decode("ascii")
        self.settings = replace(
            __import__("app.config", fromlist=["settings"]).settings,
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            archive_master_key=self.master_key,
            metadata_backup_dir=str(self.backup_dir),
            metadata_backup_verify_interval_seconds=60,
        )

    def test_bug_016_verify_binds_run_to_artifact(self) -> None:
        """[BUG-016][Req: REQ-028] verified status binds path/digest to run.

        Seam: ``storage._verify_latest_metadata_backup_once`` — scheduled worker
        entry used by ``background_loop``. Arrange a newest-on-disk orphan that
        is not restore-viable while the newest ``succeeded`` run records a
        different ``local_path``/digest. Verified must attach only after the
        bound artifact is restore-tested (same run→path/digest binding idea as
        ``open_backup_artifact``), not after verifying an unbound directory
        newest file.
        """
        with patch.object(metadata_backups, "settings", self.settings):
            artifact = metadata_backups.create_metadata_backup(
                reason="manual",
                backup_dir=self.backup_dir,
                master_key=self.master_key,
                db_backend="sqlite",
                sqlite_path=str(self.db_path),
                config_snapshot={"db_backend": "sqlite"},
            )

        orphan = self.backup_dir / "zzzz-unbound-orphan.bak.enc"
        orphan.write_bytes(b"not-a-valid-encrypted-backup")
        listed = metadata_backups.list_local_backup_files(self.backup_dir)
        self.assertEqual(listed[0], orphan)
        self.assertIn(artifact["path"], listed)

        with SQLiteConnection(str(self.db_path)) as connection:
            run = metadata_backups.record_backup_run(
                connection,
                reason="manual",
                backend="sqlite",
                status="succeeded",
                digest_sha256=artifact["digest_sha256"],
                local_path=str(artifact["path"]),
                size_bytes=artifact["path"].stat().st_size,
            )
            run_id = run["id"]

        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
        )
        with (
            patch("app.database.settings", database_settings),
            patch("app.storage.settings", self.settings),
            patch.object(metadata_backups, "settings", self.settings),
        ):
            storage._verify_latest_metadata_backup_once()

        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT status, verified_at FROM metadata_backup_runs WHERE id=%s",
                (run_id,),
            ).fetchone()
        self.assertEqual(
            row["status"],
            "verified",
            "verify must stamp the succeeded run whose local_path/digest was restore-tested",
        )
        self.assertIsNotNone(row["verified_at"])


class DefaultObjectStoreFailClosedTests(unittest.TestCase):
    """BUG-010: configured S3 backup failures must not become None success (REQ-022)."""

    def test_bug_010_configured_store_failure_raises(self) -> None:
        """[BUG-010][Req: REQ-022] configured bucket client errors fail closed.

        Seam: ``default_object_store()`` — public factory used by admin/manual
        backup, the scheduled worker, and ``backup_upgrade`` (pre-upgrade gate).
        With ``vault_s3_bucket`` set and ``s3_client`` failing at the AWS
        boundary, must raise ``BackupError`` rather than return ``None`` (which
        aliases failure as “S3 unset” and yields silent local-only success).
        """
        configured = replace(
            __import__("app.config", fromlist=["settings"]).settings,
            vault_s3_bucket="frostvault-backups",
        )
        with (
            patch.object(metadata_backups, "settings", configured),
            patch(
                "app.storage.s3_client",
                side_effect=RuntimeError("AWS credentials are not configured"),
            ),
        ):
            with self.assertRaises(metadata_backups.BackupError) as ctx:
                metadata_backups.default_object_store()
        self.assertIn("unavailable", str(ctx.exception).lower())

    def test_bug_010_unset_bucket_returns_none(self) -> None:
        """Unset bucket remains the only path that returns None (local-only OK)."""
        unset = replace(
            __import__("app.config", fromlist=["settings"]).settings,
            vault_s3_bucket="",
        )
        with patch.object(metadata_backups, "settings", unset):
            self.assertIsNone(metadata_backups.default_object_store())


class PreUpgradeBackupGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "live.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.master_key = Fernet.generate_key().decode("ascii")
        self.store = RecordingObjectStore()

    def test_pre_upgrade_backup_success_allows_upgrade(self) -> None:
        result = metadata_backups.run_pre_upgrade_backup(
            backup_dir=self.backup_dir,
            master_key=self.master_key,
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            config_snapshot={"db_backend": "sqlite"},
            object_store=self.store,
            retention=5,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "pre_upgrade")
        self.assertTrue(Path(result["path"]).exists())
        self.assertTrue(
            any(key.startswith("system/backups/") for key in self.store.objects)
        )

    def test_failed_pre_upgrade_backup_blocks_schema_upgrade(self) -> None:
        with self.assertRaises(metadata_backups.BackupError) as ctx:
            metadata_backups.run_pre_upgrade_backup(
                backup_dir=self.backup_dir,
                master_key="",  # missing master key must fail closed
                db_backend="sqlite",
                sqlite_path=str(self.db_path),
                config_snapshot={"db_backend": "sqlite"},
                object_store=self.store,
            )
        self.assertIn("ARCHIVE_MASTER_KEY", str(ctx.exception))
        self.assertEqual(list(self.backup_dir.glob("*.bak.enc")), [])


class BackupOrchestrationNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "live.db"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', 'hash', TRUE)
                """
            )
            self.admin_id = connection.execute(
                "SELECT id FROM users WHERE username='admin'"
            ).fetchone()["id"]
        self.master_key = Fernet.generate_key().decode("ascii")
        self.store = RecordingObjectStore()

    def test_failed_backup_records_status_and_notifies_admins(self) -> None:
        with SQLiteConnection(str(self.db_path)) as connection:
            with self.assertRaises(metadata_backups.BackupError):
                metadata_backups.run_metadata_backup(
                    connection,
                    reason="scheduled",
                    backup_dir=self.backup_dir,
                    master_key="",
                    db_backend="sqlite",
                    sqlite_path=str(self.db_path),
                    config_snapshot={"db_backend": "sqlite"},
                    object_store=self.store,
                    retention=5,
                )
            status = metadata_backups.backup_status(connection)
            notes = connection.execute(
                "SELECT event, title FROM notifications WHERE user_id=%s",
                (self.admin_id,),
            ).fetchall()

        self.assertEqual(status["last_status"], "failed")
        self.assertTrue(any(row["event"] == "metadata_backup_failed" for row in notes))


if __name__ == "__main__":
    unittest.main()
