"""Different content at the path of a cloud-only file must not replace it (#411).

Seam: the catalog after a Local Copy was freed (or deleted via SMB) while its
verified Archive Version is the only copy. New content at the same path is
hashed by the scan; it must not be uploaded, because a verified Current
Snapshot upload deletes the previous (only) cloud copy. Identical content just
becomes a Local Copy again. Only an explicit confirmation replaces the copy.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

from tests.sdk_transfer_stub import mock_sdk_upload, mock_sdk_read

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from fastapi import HTTPException

from app.catalog import ArchiveCatalog
from app.config import settings

from app.main import confirm_cloud_only_replacement, queue_jobs
from app.services import notifications as notification_service
from app.services.email_templates import render_email
from app.services.operation_policies import (
    OperationPolicy,
    queue_auto_uploads,
    set_policy,
)
from app.storage import apply_auto_renames, process_jobs_once
from tests.test_current_snapshot import _FakeS3
from tests.test_database import run_alembic

PATH = "IMG_0001.jpg"
OLD = b"original photo kept only in the cloud"
NEW = b"a different photo after the camera counter reset"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class CloudOnlyPathConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source = root / "source"
        self.source.mkdir()
        self.local = self.source / PATH
        self.database_path = root / "catalog.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash)
                VALUES (1, 'owner', 'Owner', 'hash')
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'photos', 'Photos', %s, 'bucket', 'photos')
                """,
                (str(self.source),)
            )
            set_policy(
                connection,
                2,
                OperationPolicy(auto_upload=True, stability_seconds=0),
            )
        database_settings = SimpleNamespace(
            database_dsn=database_dsn(str(self.database_path))
        )
        patcher = patch("app.database.settings", database_settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.s3 = _FakeS3()

        # The original photo is protected, then freed: S3 holds the only copy.
        self._write(OLD)
        self._scan()
        self._upload(OLD, "s3-old")
        self.local.unlink()
        with PostgresConnection(str(self.database_path)) as connection:
            file_id = connection.execute(
                "SELECT vault_file_id FROM jobs ORDER BY id LIMIT 1"
            ).fetchone()["vault_file_id"]
            ArchiveCatalog(connection).mark_local_copy_missing(
                file_id, observed_at="2026-11-02T10:00:00+00:00"
            )
        self.s3.deleted.clear()

    def _write(self, payload: bytes) -> None:
        self.local.write_bytes(payload)
        with PostgresConnection(str(self.database_path)) as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2,
                path=PATH,
                file_type="regular",
                size=len(payload),
                mtime_ns=self.local.stat().st_mtime_ns,
                observed_at="2026-11-02T11:00:00+00:00",
            )

    def _scan(self) -> int:
        """Hash Local Copies, settle reappearances, queue automatic uploads."""
        apply_auto_renames({"id": 2, "source_root": str(self.source)})
        with PostgresConnection(str(self.database_path)) as connection:
            return queue_auto_uploads(
                connection,
                vault_id=2,
                source_root=str(self.source),
                requested_by=1,
            )

    def _upload(self, payload: bytes, version_id: str) -> None:
        def fake_stream(*args, **kwargs) -> int:
            kwargs["on_chunk"](payload)
            return len(payload)

        self.s3.head_version_id = version_id
        self.s3.head_size = len(payload)
        self.s3.payload = payload
        worker_settings = SimpleNamespace(
            operation_concurrency=1, restore_poll_interval=900
        )
        with (
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            mock_sdk_upload( return_value=None),
            mock_sdk_read( side_effect=fake_stream),
            patch("app.storage.s3_client", return_value=self.s3),
        ):
            process_jobs_once()

    def _upload_jobs(self) -> list[dict]:
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT id, status FROM jobs WHERE action='upload' ORDER BY id"
            ).fetchall()

    def _incidents(self) -> list[dict]:
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT kind, status, path FROM catalog_incidents ORDER BY id"
            ).fetchall()

    def _versions(self) -> dict[str, str]:
        with PostgresConnection(str(self.database_path)) as connection:
            rows = ArchiveCatalog(connection).list_versions(2, PATH)
        return {row["provider_version_id"]: row["availability"] for row in rows}

    def test_different_content_is_not_uploaded_and_opens_a_conflict(self) -> None:
        self._write(NEW)

        self.assertEqual(self._scan(), 0)
        with self.assertRaises(HTTPException) as manual:
            queue_jobs(PATH, "upload", 2, 1)
        self.assertEqual(manual.exception.status_code, 409)
        self._upload(NEW, "s3-new")

        self.assertEqual(len(self._upload_jobs()), 1, "only the original upload")
        self.assertEqual(self.s3.deleted, [], "the only cloud copy must survive")
        self.assertEqual(self._versions(), {"s3-old": "available"})
        self.assertEqual(
            self._incidents(),
            [{"kind": "cloud_only_conflict", "status": "open", "path": PATH}],
        )
        with PostgresConnection(str(self.database_path)) as connection:
            notifications = connection.execute(
                "SELECT event, title_key, in_app_enabled FROM notifications "
                "WHERE event<>'job_completed'"
            ).fetchall()
        self.assertEqual(
            [(row["event"], row["title_key"]) for row in notifications],
            [("cloud_only_conflict", "notification.cloud_only_conflict.title")],
        )
        self.assertTrue(notifications[0]["in_app_enabled"])

        # A second scan keeps one conflict and one notification.
        self.assertEqual(self._scan(), 0)
        self.assertEqual(len(self._incidents()), 1)
        with PostgresConnection(str(self.database_path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM notifications "
                "WHERE event='cloud_only_conflict'"
            ).fetchone()
        self.assertEqual(count["n"], 1)

    def test_conflict_is_emailed_immediately_when_smtp_is_configured(
        self,
    ) -> None:
        email_settings = replace(
            settings,
            smtp_host="smtp.example.com",
            smtp_from_address="frostvault@example.com",
            smtp_to_address="owner@example.com",
        )
        with patch.object(notification_service, "settings", email_settings):
            self._write(NEW)
            self._scan()

        with PostgresConnection(str(self.database_path)) as connection:
            channels = connection.execute(
                """
                SELECT d.channel
                FROM notification_deliveries d
                JOIN notifications n ON n.id=d.notification_id
                WHERE n.event='cloud_only_conflict'
                ORDER BY d.channel
                """
            ).fetchall()
        self.assertIn("email", [row["channel"] for row in channels])
        rendered = render_email("cloud_only_conflict", vault_id=2)
        self.assertIn("only cloud copy", rendered["body"])

    def test_upload_queued_before_the_hold_does_not_replace_the_copy(
        self,
    ) -> None:
        self._write(NEW)
        self._scan()
        with PostgresConnection(str(self.database_path)) as connection:
            held = connection.execute(
                "SELECT cloud_only_version_id FROM local_copies"
            ).fetchone()["cloud_only_version_id"]
            connection.execute("UPDATE local_copies SET cloud_only_version_id=NULL")
        queue_jobs(PATH, "upload", 2, 1)
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE local_copies SET cloud_only_version_id=%s", (held,)
            )

        self._upload(NEW, "s3-new")

        self.assertEqual(
            [row["status"] for row in self._upload_jobs()],
            ["completed", "failed"],
        )
        self.assertEqual(self.s3.deleted, [])
        self.assertEqual(self._versions(), {"s3-old": "available"})

    def test_identical_content_becomes_a_local_copy_without_upload(self) -> None:
        self._write(OLD)

        self.assertEqual(self._scan(), 0)
        self._upload(OLD, "s3-again")

        self.assertEqual(len(self._upload_jobs()), 1, "no new upload")
        self.assertEqual(self._incidents(), [])
        self.assertEqual(self.s3.deleted, [])
        with PostgresConnection(str(self.database_path)) as connection:
            local = connection.execute(
                """
                SELECT lc.presence, lc.plaintext_sha256,
                       av.provider_version_id
                FROM local_copies lc
                JOIN archive_versions av ON av.id=lc.matched_archive_version_id
                """
            ).fetchone()
        self.assertEqual(local["presence"], "present")
        self.assertEqual(local["plaintext_sha256"], _sha(OLD))
        self.assertEqual(local["provider_version_id"], "s3-old")

    def test_confirmed_replacement_uploads_verifies_then_purges_previous(
        self,
    ) -> None:
        self._write(NEW)
        self._scan()

        queued = confirm_cloud_only_replacement(PATH, 2, 1)
        self.assertEqual(queued["item_count"], 1)
        self.assertEqual(
            [row["status"] for row in self._incidents()], ["resolved"]
        )
        self._upload(NEW, "s3-new")

        self.assertEqual(
            [row["status"] for row in self._upload_jobs()],
            ["completed", "completed"],
        )
        self.assertEqual(self.s3.deleted, [("photos/IMG_0001.jpg", "s3-old")])
        self.assertEqual(
            self._versions(), {"s3-old": "purged", "s3-new": "available"}
        )
        # A later scan does not reopen the confirmed conflict.
        self._scan()
        self.assertEqual(
            [row["status"] for row in self._incidents()], ["resolved"]
        )

    def test_confirmation_requires_an_open_conflict(self) -> None:
        self._write(OLD)
        self._scan()
        with self.assertRaises(HTTPException) as raised:
            confirm_cloud_only_replacement(PATH, 2, 1)
        self.assertEqual(raised.exception.status_code, 409)

    def test_renaming_the_new_file_protects_it_and_keeps_the_cloud_copy(
        self,
    ) -> None:
        self._write(NEW)
        self._scan()

        renamed = self.source / "IMG_0001-new.jpg"
        self.local.rename(renamed)
        with PostgresConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.mark_local_path_missing(
                vault_id=2, path=PATH, observed_at="2026-11-02T12:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=2,
                path="IMG_0001-new.jpg",
                file_type="regular",
                size=len(NEW),
                mtime_ns=renamed.stat().st_mtime_ns,
                observed_at="2026-11-02T12:00:00+00:00",
            )
        self.assertEqual(self._scan(), 1)
        self._upload(NEW, "s3-renamed")

        self.assertEqual(self.s3.deleted, [], "the cloud-only copy is kept")
        self.assertEqual(self._versions(), {"s3-old": "available"})
        self.assertEqual(
            [row["status"] for row in self._incidents()], ["resolved"]
        )
        with PostgresConnection(str(self.database_path)) as connection:
            paths = connection.execute(
                """
                SELECT fp.path, av.provider_version_id, av.integrity
                FROM archive_versions av
                JOIN file_paths fp
                  ON fp.vault_file_id=av.vault_file_id AND fp.valid_to IS NULL
                ORDER BY fp.path
                """
            ).fetchall()
        self.assertEqual(
            [(row["path"], row["provider_version_id"]) for row in paths],
            [("IMG_0001-new.jpg", "s3-renamed"), (PATH, "s3-old")],
        )


if __name__ == "__main__":
    unittest.main()
