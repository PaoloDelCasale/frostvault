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
  — restore into a temporary database without touching the live DB
- ``app.services.metadata_backups.default_object_store``
  — public ObjectStore factory for configured off-host backup (BUG-010)
- ``app.storage._verify_latest_metadata_backup_once``
  — scheduled verify must stamp the run bound by local_path/digest (BUG-016)
- ``app.services.metadata_backups._verify_postgres_dump_isolated``
  — temp_database verify must prove schema counts (BUG-019 / REQ-031)
- Admin HTTP: list / status / manual run / download
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from app import storage

from app.services import metadata_backups
from tests.fixtures import FrostVaultTestCase, empty_database, override_settings, seed_vault


class _BackupCase(FrostVaultTestCase):
    """A live database, a backup directory and a master key."""

    owner = None

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp_path
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        self.master_key = Fernet.generate_key().decode("ascii")


class CreateMetadataBackupTests(_BackupCase):
    """Vertical slice: create an encrypted, digestsable backup without the master key."""

    owner = {"display_name": "Admin"}

    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            seed_vault(connection, name="Docs", source_root="/src")

        self.enterContext(override_settings(
            archive_master_key=self.master_key,
            bootstrap_admin_password="super-secret-bootstrap-password",
            oidc_client_secret="oidc-secret-value",
        ))

    def test_create_backup_is_encrypted_checksummed_and_excludes_master_key(self) -> None:
        artifact = metadata_backups.create_metadata_backup(
            reason="manual",
            backup_dir=self.backup_dir,
            master_key=self.master_key,
            config_snapshot=metadata_backups.build_config_snapshot(self.settings),
        )

        self.assertTrue(artifact["path"].exists())
        self.assertEqual(artifact["backend"], "postgresql")
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
        self.assertEqual(payload["manifest"]["backend"], "postgresql")
        self.assertIn("database", payload)
        self.assertNotIn("archive_master_key", payload["config"])
        self.assertNotIn(self.master_key, json.dumps(payload["config"]))
        self.assertNotIn("super-secret-bootstrap-password", json.dumps(payload["config"]))

        restored = self.enterContext(empty_database())
        restored.restore_dump(payload["database"])
        with restored.connect() as connection:
            owners = connection.execute("SELECT display_name FROM owner").fetchall()
            vaults = connection.execute("SELECT name FROM vaults").fetchall()
        self.assertEqual([row["display_name"] for row in owners], ["Admin"])
        self.assertEqual([row["name"] for row in vaults], ["Docs"])


class LocalRetentionRotationTests(_BackupCase):
    def test_retention_keeps_newest_artifacts_only(self) -> None:
        for index in range(4):
            path = self.backup_dir / f"2026010{index}T000000Z-postgresql-scheduled.bak.enc"
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
                "20260102T000000Z-postgresql-scheduled.bak.enc",
                "20260103T000000Z-postgresql-scheduled.bak.enc",
            ],
        )
        # Sidecars for removed artifacts are gone; kept ones remain.
        self.assertFalse(
            (self.backup_dir / "20260100T000000Z-postgresql-scheduled.bak.enc.sha256").exists()
        )
        self.assertTrue(
            (self.backup_dir / "20260103T000000Z-postgresql-scheduled.bak.enc.sha256").exists()
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

    def list_versions(self, prefix: str) -> list[tuple[str, str]]:
        return [(key, "null") for key in self.list_keys(prefix)]

    def delete_version(self, key: str, version_id: str) -> None:
        self.objects.pop(key, None)


class ObjectStoreBackupTests(_BackupCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = RecordingObjectStore()

    def test_store_backup_uses_system_backups_prefix_only(self) -> None:
        artifact = metadata_backups.create_metadata_backup(
            reason="scheduled",
            backup_dir=self.backup_dir,
            master_key=self.master_key,
            database_dsn=self.database.dsn,
            config_snapshot={"scan_interval": 3600},
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


class VersionedObjectStore(RecordingObjectStore):
    """A versioned bucket: every put adds a version, deletes go by VersionId."""

    def __init__(self) -> None:
        super().__init__()
        self.versions: dict[str, list[str]] = {}
        self.list_error: Exception | None = None

    def put_bytes(self, key: str, body: bytes) -> None:
        super().put_bytes(key, body)
        self.versions.setdefault(key, []).append(f"v{len(self.versions.get(key, [])) + 1}")

    def list_versions(self, prefix: str) -> list[tuple[str, str]]:
        if self.list_error is not None:
            raise self.list_error
        return [(key, version) for key, versions in sorted(self.versions.items())
                if key.startswith(prefix) for version in versions]

    def delete_version(self, key: str, version_id: str) -> None:
        self.versions[key].remove(version_id)
        if not self.versions[key]:
            del self.versions[key]


class RemoteRetentionTests(unittest.TestCase):
    """#742: the System Bucket keeps METADATA_BACKUP_RETENTION backups."""

    def setUp(self) -> None:
        self.store = VersionedObjectStore()
        for day in range(1, 6):
            key = f"system/backups/2026010{day}T000000Z-postgresql-scheduled.bak.enc"
            self.store.put_bytes(key, b"old")
            self.store.put_bytes(key, b"rewritten")  # a second version
            self.store.put_bytes(key + ".sha256", b"digest")
        for key in ("billing/cur/report.csv.gz", "system/other.bak.enc",
                    "system/backups/nested/20260101T000000Z-postgresql-x.bak.enc",
                    "system/backups/notes.txt"):
            self.store.put_bytes(key, b"not a backup")

    def _backups(self) -> list[str]:
        return sorted(key for key in self.store.versions
                      if key.startswith("system/backups/2026"))

    def test_keeps_the_newest_and_deletes_every_version_of_older_ones(self) -> None:
        deleted = metadata_backups.rotate_remote_retention(self.store, 2)
        self.assertEqual(deleted, [
            f"system/backups/2026010{day}T000000Z-postgresql-scheduled.bak.enc"
            for day in (3, 2, 1)])
        kept = [f"system/backups/2026010{day}T000000Z-postgresql-scheduled.bak.enc"
                for day in (4, 5)]
        self.assertEqual(self._backups(), [name for key in kept
                                           for name in (key, key + ".sha256")])
        self.assertEqual(self.store.versions[kept[0]], ["v1", "v2"])

    def test_never_touches_other_prefixes_or_names(self) -> None:
        metadata_backups.rotate_remote_retention(self.store, 0)
        for key in ("billing/cur/report.csv.gz", "system/other.bak.enc",
                    "system/backups/nested/20260101T000000Z-postgresql-x.bak.enc",
                    "system/backups/notes.txt"):
            self.assertIn(key, self.store.versions)

    def test_protected_backups_are_never_deleted(self) -> None:
        oldest = "system/backups/20260101T000000Z-postgresql-scheduled.bak.enc"
        metadata_backups.rotate_remote_retention(self.store, 1, protected_keys={oldest})
        self.assertIn(oldest, self.store.versions)
        self.assertIn(oldest + ".sha256", self.store.versions)
        self.assertNotIn(oldest.replace("0101", "0102"), self.store.versions)

    def test_a_retention_failure_does_not_fail_the_backup(self) -> None:
        from botocore.exceptions import ClientError

        self.store.list_error = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "secret-ish detail"}},
            "ListObjectVersions")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "20260106T000000Z-postgresql-manual.bak.enc"
            path.write_bytes(b"ciphertext")
            artifact = {
                "path": path, "filename": path.name, "backend": "postgresql",
                "digest_sha256": hashlib.sha256(b"ciphertext").hexdigest(),
                "database_sha256": "d", "reason": "manual",
                "created_at": "2026-01-06T00:00:00+00:00", "size_bytes": 10,
            }
            with patch.object(metadata_backups, "create_metadata_backup",
                              return_value=artifact), \
                 patch.object(metadata_backups, "_newest_succeeded_s3_key",
                              return_value=None), \
                 patch.object(metadata_backups, "record_backup_run") as record:
                result = metadata_backups.run_metadata_backup(
                    object(), reason="manual", backup_dir=tmp,
                    config_snapshot={}, object_store=self.store, retention=2)
        self.assertTrue(result["ok"])
        kwargs = record.call_args.kwargs
        self.assertEqual(kwargs["status"], "succeeded")
        self.assertIn("(AccessDenied)", kwargs["error_message"])
        self.assertNotIn("secret-ish", kwargs["error_message"])
        self.assertIn(f"system/backups/{path.name}", self.store.versions)


class IsolatedRestoreVerificationTests(_BackupCase):
    owner = {"display_name": "Admin"}

    def test_verify_restore_isolated_does_not_touch_the_live_db(self) -> None:
        artifact = metadata_backups.create_metadata_backup(
            reason="manual",
            backup_dir=self.backup_dir,
            master_key=self.master_key,
            database_dsn=self.database.dsn,
            config_snapshot={"scan_interval": 3600},
        )
        live_before = self.database.rows()

        result = metadata_backups.verify_restore_isolated(
            artifact["path"],
            master_key=self.master_key,
            work_dir=self.root / "verify",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["owner_count"], 1)
        self.assertEqual(self.database.rows(), live_before)


class MetadataVerifyRunBindingTests(_BackupCase):
    """BUG-016: scheduled verify must stamp the restore-tested run (REQ-028)."""

    owner = {"display_name": "Admin"}

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(override_settings(
            archive_master_key=self.master_key,
            metadata_backup_dir=str(self.backup_dir),
            metadata_backup_verify_interval_seconds=60,
        ))

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
        artifact = metadata_backups.create_metadata_backup(
            reason="manual",
            backup_dir=self.backup_dir,
            master_key=self.master_key,
            database_dsn=self.database.dsn,
            config_snapshot={"scan_interval": 3600},
        )

        orphan = self.backup_dir / "zzzz-unbound-orphan.bak.enc"
        orphan.write_bytes(b"not-a-valid-encrypted-backup")
        listed = metadata_backups.list_local_backup_files(self.backup_dir)
        self.assertEqual(listed[0], orphan)
        self.assertIn(artifact["path"], listed)

        with self.database.connect() as connection:
            run = metadata_backups.record_backup_run(
                connection,
                reason="manual",
                backend="postgresql",
                status="succeeded",
                digest_sha256=artifact["digest_sha256"],
                local_path=str(artifact["path"]),
                size_bytes=artifact["path"].stat().st_size,
            )
            run_id = run["id"]

        storage._verify_latest_metadata_backup_once()

        with self.database.connect() as connection:
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


class NextVerificationTests(unittest.TestCase):
    """#648: when the maintenance loop next restore-verifies a backup."""

    NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)

    def next_at(self, **kwargs):
        values = {"interval_seconds": 7 * 86400, "last_verified_at": None,
                  "last_attempt_at": None, "now": self.NOW, **kwargs}
        return metadata_backups.next_verification_at(**values)

    def test_disabled_interval_has_no_next_verification(self) -> None:
        self.assertIsNone(self.next_at(interval_seconds=0,
                                       last_verified_at=self.NOW.isoformat()))

    def test_counts_from_the_last_verification(self) -> None:
        verified = self.NOW - timedelta(days=2)
        self.assertEqual(self.next_at(last_verified_at=verified.isoformat()),
                         verified + timedelta(days=7))

    def test_a_later_attempt_without_success_moves_the_schedule(self) -> None:
        attempt = self.NOW - timedelta(hours=1)
        self.assertEqual(
            self.next_at(last_verified_at=self.NOW - timedelta(days=20),
                         last_attempt_at=attempt),
            attempt + timedelta(days=7),
        )

    def test_never_run_or_overdue_is_due_on_the_next_cycle(self) -> None:
        self.assertEqual(self.next_at(), self.NOW)
        self.assertEqual(self.next_at(last_verified_at=self.NOW - timedelta(days=9)),
                         self.NOW)


class DefaultObjectStoreFailClosedTests(unittest.TestCase):
    """BUG-010: configured S3 backup failures must not become None success (REQ-022)."""

    def test_bug_010_configured_store_failure_raises(self) -> None:
        """[BUG-010][Req: REQ-022] configured bucket client errors fail closed.

        Seam: ``default_object_store()`` — public factory used by admin/manual
        backup and the scheduled worker.
        With ``metadata_backup_bucket`` set and ``s3_client`` failing at the AWS
        boundary, must raise ``BackupError`` rather than return ``None`` (which
        aliases failure as “S3 unset” and yields silent local-only success).
        """
        with (
            override_settings(metadata_backup_bucket="frostvault-backups"),
            patch(
                "app.services.s3.s3_client",
                side_effect=RuntimeError("AWS credentials are not configured"),
            ),
        ):
            with self.assertRaises(
                metadata_backups.ObjectStoreUnavailableError
            ) as ctx:
                metadata_backups.default_object_store()
        self.assertIn("unavailable", str(ctx.exception).lower())
        self.assertNotIn("AWS credentials", str(ctx.exception))

    def test_bug_010_unset_or_placeholder_bucket_without_credentials_returns_none(self) -> None:
        """No METADATA_BACKUP_BUCKET and no AWS credentials deliberately mean local-only."""
        for bucket in ("", "example-bucket", "REPLACE-WITH-A-BUCKET"):
            with self.subTest(bucket=bucket):
                with (
                    override_settings(metadata_backup_bucket=bucket),
                    patch.object(metadata_backups.system_bucket,
                                 "aws_credentials_configured", return_value=False),
                    patch.object(metadata_backups.system_bucket, "vaults_exist", return_value=False),
                ):
                    self.assertIsNone(metadata_backups.default_object_store())

    def test_backups_go_to_the_system_bucket(self) -> None:
        """#734: backups go to the System Bucket, never to a Vault's bucket."""
        system = metadata_backups.system_bucket.SystemBucket(
            "frostvault-system-abcd1234", "eu-south-1", "created")
        client = object()
        with (
            override_settings(metadata_backup_bucket="") as configured,
            patch.object(metadata_backups.system_bucket,
                         "aws_credentials_configured", return_value=True),
            patch.object(metadata_backups.system_bucket, "ensure", return_value=system) as ensure,
            patch("app.services.s3.s3_client", return_value=client) as s3_client,
        ):
            store = metadata_backups.default_object_store()
        ensure.assert_called_once_with(configured)
        s3_client.assert_called_once_with(region="eu-south-1")
        self.assertEqual(store.bucket, "frostvault-system-abcd1234")

    def test_any_vault_requires_an_off_host_copy(self) -> None:
        """S1, #735: Vaults but no credentials/IAM fail loudly, never local-only."""
        refusal = metadata_backups.system_bucket.SystemBucketUnavailable(
            "Creating System Bucket frostvault-system-abcd1234 did not finish: "
            "S3 refused s3:CreateBucket (AccessDenied). FrostVault retries with the same name")
        with (
            override_settings(metadata_backup_bucket="") as configured,
            patch.object(metadata_backups.system_bucket,
                         "aws_credentials_configured", return_value=False),
            patch.object(metadata_backups.system_bucket, "vaults_exist", return_value=True),
            patch.object(metadata_backups.system_bucket, "ensure", side_effect=refusal) as ensure,
        ):
            self.assertTrue(metadata_backups.off_host_backup_configured())
            resolved = metadata_backups.resolve_object_store()
        ensure.assert_called_once_with(configured)
        self.assertIsNone(resolved.store)
        self.assertIsInstance(resolved.error, metadata_backups.ObjectStoreUnavailableError)
        self.assertEqual(str(resolved.error), str(refusal))

    def test_resolving_never_leaks_an_unexpected_error(self) -> None:
        def broken():
            raise RuntimeError("provider detail with an account id")

        resolved = metadata_backups.resolve_object_store(broken)
        self.assertEqual(str(resolved.error),
                         "Configured metadata backup object store is unavailable")

    def test_a_refused_system_bucket_keeps_its_clear_message(self) -> None:
        refusal = metadata_backups.system_bucket.SystemBucketUnavailable(
            "METADATA_BACKUP_BUCKET refused: Versioning is disabled, not enabled")
        with (
            override_settings(metadata_backup_bucket="my-backups"),
            patch.object(metadata_backups.system_bucket, "ensure", side_effect=refusal),
        ):
            with self.assertRaises(metadata_backups.ObjectStoreUnavailableError) as ctx:
                metadata_backups.default_object_store()
        self.assertEqual(str(ctx.exception), str(refusal))


class BackupOrchestrationNotificationTests(_BackupCase):
    owner = {"display_name": "Admin"}

    def setUp(self) -> None:
        super().setUp()
        self.store = RecordingObjectStore()

    def test_off_host_copy_records_the_system_bucket(self) -> None:
        """#734: the run names the bucket of its off-host copy."""
        self.store.bucket = "frostvault-system-abcd1234"
        with self.database.connect() as connection:
            result = metadata_backups.run_metadata_backup(
                connection,
                reason="manual",
                backup_dir=self.backup_dir,
                master_key=self.master_key,
                database_dsn=self.database.dsn,
                config_snapshot={"scan_interval": 3600},
                object_store=metadata_backups.resolve_object_store(lambda: self.store),
                retention=5,
            )
            run = metadata_backups.list_backup_artifacts(connection)[0]
        self.assertEqual(result["s3_bucket"], "frostvault-system-abcd1234")
        self.assertEqual((run["s3_bucket"], run["s3_key"]),
                         ("frostvault-system-abcd1234", result["s3_key"]))
        self.assertTrue(run["s3_key"].startswith("system/backups/"))
        self.assertIsNone(run["error_message"])  # remote retention applied (#742)

    def test_a_refused_system_bucket_is_a_recorded_notified_failure(self) -> None:
        refusal = metadata_backups.ObjectStoreUnavailableError(
            "METADATA_BACKUP_BUCKET refused: Versioning is disabled, not enabled")

        with self.database.connect() as connection:
            with self.assertRaises(metadata_backups.ObjectStoreUnavailableError):
                metadata_backups.run_metadata_backup(
                    connection,
                    reason="scheduled",
                    backup_dir=self.backup_dir,
                    master_key=self.master_key,
                    database_dsn=self.database.dsn,
                    config_snapshot={"scan_interval": 3600},
                    object_store=metadata_backups.ResolvedObjectStore(error=refusal),
                    retention=5,
                )
            run = metadata_backups.list_backup_artifacts(connection)[0]
            notes = connection.execute("SELECT body FROM notifications").fetchall()
        self.assertEqual((run["status"], run["error_message"]), ("failed", str(refusal)))
        self.assertTrue(any(str(refusal) in row["body"] for row in notes))
        # The local artifact is kept, as after a failed upload; nothing was rotated.
        self.assertEqual(len(list(self.backup_dir.glob("*.bak.enc"))), 1)

    def test_failed_backup_records_status_and_notifies_admins(self) -> None:
        with self.database.connect() as connection:
            with self.assertRaises(metadata_backups.BackupError):
                metadata_backups.run_metadata_backup(
                    connection,
                    reason="scheduled",
                    backup_dir=self.backup_dir,
                    master_key="",
                    database_dsn=self.database.dsn,
                    config_snapshot={"scan_interval": 3600},
                    object_store=self.store,
                    retention=5,
                )
            status = metadata_backups.backup_status(connection)
            notes = connection.execute(
                "SELECT event, title FROM notifications"
            ).fetchall()

        self.assertEqual(status["last_status"], "failed")
        self.assertTrue(any(row["event"] == "metadata_backup_failed" for row in notes))


class TempDatabaseVerifyProvenCountsTests(unittest.TestCase):
    """BUG-019: temp_database verify must not ok when tables unproven (REQ-031)."""

    def test_bug_019_temp_database_requires_proven_counts(self) -> None:
        """[BUG-019][Req: REQ-031] temp_database mode must fail closed without counts.

        Seam: ``_verify_postgres_dump_isolated`` — producer of
        ``verification_mode=temp_database`` results used by
        ``verify_restore_isolated`` / scheduled verify. After ``createdb``
        succeeds, if ``psql`` COUNT cannot prove owner/vaults/alembic_version,
        raise ``BackupError`` — do not return ``ok: True`` with null counts.
        Distinct from intentional ``pg_restore_list`` fallback (former BUG-003).
        """
        list_ok = MagicMock(returncode=0, stderr=b"", stdout=b"")
        revision_ok = MagicMock(
            returncode=0, stderr="",
            stdout="COPY public.alembic_version (version_num) FROM stdin;\n0001_baseline\n\\.\n",
        )
        created_ok = MagicMock(returncode=0, stderr=b"", stdout=b"")
        restore_soft = MagicMock(returncode=1, stderr=b"WARNING: role missing", stdout=b"")
        psql_fail = MagicMock(
            returncode=1,
            stderr="ERROR: relation \"owner\" does not exist\n",
            stdout="",
        )
        drop_ok = MagicMock(returncode=0, stderr=b"", stdout=b"")

        with patch.object(
            metadata_backups.subprocess,
            "run",
            side_effect=[list_ok, revision_ok, created_ok, restore_soft, psql_fail, drop_ok],
        ):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(metadata_backups.BackupError):
                    metadata_backups._verify_postgres_dump_isolated(
                        b"not-a-real-dump", Path(tmp)
                    )

    def test_bug_019_temp_database_source_rejects_null_count_success(self) -> None:
        """[BUG-019][Req: REQ-031] source must reject null counts in temp_database path.

        Intentional list-only fallback must remain (former BUG-003 rejected).
        Before returning temp_database ok, counts must be explicitly validated.
        """
        body = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "metadata_backups.py"
        ).read_text(encoding="utf-8")
        start = body.index("def _verify_postgres_dump_isolated")
        end = body.index("\ndef _row_run", start)
        fn_body = body[start:end]
        self.assertIn("pg_restore_list", fn_body)
        temp_idx = fn_body.find('"temp_database"')
        self.assertGreaterEqual(temp_idx, 0)
        prelude = fn_body[:temp_idx]
        self.assertIn(
            "owner_count is None",
            prelude,
            "temp_database success path must explicitly guard null owner_count",
        )


if __name__ == "__main__":
    unittest.main()
