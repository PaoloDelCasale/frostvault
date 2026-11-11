"""No database/provider needed: decryption is not schema compatibility."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from app.database import HEAD_SCHEMA_REVISION
from app.services import metadata_backups as backups


class BackupCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key()
        self.database = b"synthetic custom-format dump"
        self.manifest = {
            "created_at": "2026-11-12T10:00:00+00:00", "reason": "manual",
            "metadata_format": backups.METADATA_FORMAT,
            "schema_revision": HEAD_SCHEMA_REVISION,
            "backend": "postgresql", "schema_format": "postgresql-custom",
            "database_sha256": hashlib.sha256(self.database).hexdigest(),
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def artifact(self, manifest=None):
        plaintext = backups._pack_payload(database_bytes=self.database, config={},
                                          manifest=self.manifest if manifest is None else manifest)
        path = self.root / "artifact.enc"
        path.write_bytes(Fernet(self.key).encrypt(plaintext))
        return path

    def render(self, rows):
        return ("-- PostgreSQL database dump\nSET statement_timeout = 0;\n"
                "COPY public.alembic_version (version_num) FROM stdin;\n"
                + "".join(str(row) + "\n" for row in rows) + "\\.\n\n-- end\n").encode()

    def test_revision_is_proven_from_dump_without_connecting_or_executing_sql(self):
        result = SimpleNamespace(returncode=0, stdout=self.render([HEAD_SCHEMA_REVISION]))
        with patch.object(backups.subprocess, "run", return_value=result) as process:
            self.assertEqual(backups._dump_schema_revision(self.database), HEAD_SCHEMA_REVISION)
        args = process.call_args
        self.assertEqual(args.args[0], ["pg_restore", "--data-only", "--table=alembic_version", "--file=-"])
        self.assertEqual(args.kwargs["input"], self.database)
        self.assertNotIn("env", args.kwargs)
        self.assertNotIn("--dbname", args.args[0])

    def test_actual_dump_revision_cannot_be_missing_old_or_divergent(self):
        for output in (b"", b"-- empty dump", self.render([]), self.render(["0071_bulk_scheduler_merge"]),
                       self.render([HEAD_SCHEMA_REVISION, "other"]),
                       self.render([HEAD_SCHEMA_REVISION]) * 2,
                       b"INSERT INTO alembic_version VALUES ('" + HEAD_SCHEMA_REVISION.encode() + b"');\n"):
            with self.subTest(output=output), patch.object(backups.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0, stdout=output)):
                with self.assertRaises(backups.BackupError):
                    backups._dump_schema_revision(self.database)

    def test_restore_tool_failures_are_safe_and_do_not_leak_details(self):
        secret_marker = "synthetic-private-dsn-marker"
        for result in (SimpleNamespace(returncode=1, stdout=b"", stderr=secret_marker.encode()),
                       OSError(secret_marker)):
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(result=type(result)), patch.object(backups.subprocess, "run", **kwargs):
                with self.assertRaises(backups.BackupError) as caught:
                    backups._dump_schema_revision(self.database)
                self.assertNotIn(secret_marker, str(caught.exception))

    def test_legacy_or_unknown_encrypted_artifacts_are_rejected_before_provider_or_scratch_writes(self):
        for change in ({"metadata_format": None}, {"metadata_format": "old-format"},
                       {"schema_revision": None}, {"schema_revision": "unknown"},
                       {"schema_revision": "0071_bulk_scheduler_merge"},
                       {"backend": "sqlite"}, {"schema_format": "sql"}):
            with self.subTest(change=change), patch.object(backups, "_dump_schema_revision") as inspect:
                artifact = self.artifact({**self.manifest, **change})
                provider = Mock()
                work = self.root / "scratch-not-created"
                with self.assertRaisesRegex(backups.BackupError, "incompatible"):
                    backups.verify_restore_isolated(artifact, master_key=self.key.decode(),
                                                    work_dir=work, object_store=provider)
                self.assertFalse(work.exists())
                provider.list_keys.assert_not_called()
                inspect.assert_not_called()

    def test_decrypt_refuses_digest_mismatch_and_manifest_cannot_override_actual_schema(self):
        artifact = self.artifact({**self.manifest, "database_sha256": "0" * 64})
        with patch.object(backups, "_dump_schema_revision") as inspect:
            with self.assertRaisesRegex(backups.BackupError, "digest mismatch"):
                backups.decrypt_backup_file(artifact, master_key=self.key.decode())
            inspect.assert_not_called()
        artifact = self.artifact()
        with patch.object(backups.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0, stdout=self.render(["old_revision"]))):
            with self.assertRaisesRegex(backups.BackupError, "incompatible"):
                backups.decrypt_backup_file(artifact, master_key=self.key.decode())

    def test_current_backup_roundtrip_freezes_format_and_actual_schema(self):
        destination = self.root / "new-artifacts"
        with patch.object(backups, "_dump_postgres_bytes", return_value=self.database), \
                patch.object(backups.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=self.render([HEAD_SCHEMA_REVISION]))):
            artifact = backups.create_metadata_backup(reason="manual", backup_dir=destination,
                                                       master_key=self.key.decode(), config_snapshot={})
            payload = backups.decrypt_backup_file(artifact["path"], master_key=self.key.decode())
        self.assertEqual(payload["manifest"]["metadata_format"], backups.METADATA_FORMAT)
        self.assertEqual(payload["manifest"]["schema_revision"], HEAD_SCHEMA_REVISION)
        self.assertEqual(payload["database"], self.database)

    def test_creation_rejects_an_old_captured_catalog_before_writing_artifacts(self):
        destination = self.root / "not-created"
        with patch.object(backups, "_dump_postgres_bytes", return_value=self.database), \
                patch.object(backups.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=self.render(["old_revision"]))):
            with self.assertRaises(backups.BackupError):
                backups.create_metadata_backup(reason="manual", backup_dir=destination,
                                               master_key=self.key.decode(), config_snapshot={})
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
