"""S3 destination construction (issue #9).

Seams under test:
- ``expected_cloud_key`` — builds exact namespaced S3 keys.
- Plain upload Job worker — SDK destinations and HeadObject share one key.

The rename destination under the Vault namespace (former
``S3RenamePrefixTests``) is a server-side copy since #595: covered by
``tests.test_rename_sync.test_rename_copies_the_exact_current_and_removes_the_old_key_exactly``
(exact source key + VersionId, destination ``vaults/<uuid>/<new path>``, no upload).
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import os
import tempfile
import unittest
from pathlib import Path
from tests.snapshot_s3_stub import VersionedS3Stub
from types import SimpleNamespace
from unittest.mock import patch

# Windows test hosts lack the Linux open flags imported by vault decommission.
for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog

from app.main import queue_jobs
from app.storage import expected_cloud_key, process_jobs_once
from tests.sdk_transfer_stub import mock_sdk_upload, mock_sdk_read
from tests.owner_fixtures import insert_owner, insert_vault
from tests.test_database import run_alembic


class S3DestinationTests(unittest.TestCase):
    def test_two_vault_prefixes_isolate_the_same_logical_path(self) -> None:
        first = expected_cloud_key("reports/q1.txt", "vaults/11111111-1111-1111-1111-111111111111")
        second = expected_cloud_key("reports/q1.txt", "vaults/22222222-2222-2222-2222-222222222222")
        self.assertEqual(
            first,
            "vaults/11111111-1111-1111-1111-111111111111/reports/q1.txt",
        )
        self.assertEqual(
            second,
            "vaults/22222222-2222-2222-2222-222222222222/reports/q1.txt",
        )
        self.assertNotEqual(first, second)

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            expected_cloud_key("../outside.txt", "vaults/11111111-1111-1111-1111-111111111111")


class S3UploadPrefixTests(unittest.TestCase):
    def test_upload_and_read_back_use_vault_prefix_matching_head_object(self) -> None:
        prefix = "vaults/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        relative_path = "reports/q1.txt"
        expected_key = f"{prefix}/{relative_path}"
        expected_remote = f"{expected_key}"
        payload = b"prefixed-plain-bytes"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = source / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            database_path = root / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)

            with PostgresConnection(str(database_path)) as connection:
                insert_owner(connection)
                insert_vault(connection, id=2, source_root=str(source),
                             bucket="example-bucket", uuid=prefix.removeprefix("vaults/"))
                from app.services.vault_relocation import enroll_vault_root_identity
                enroll_vault_root_identity(connection, 2, str(source))
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2,
                    path=relative_path,
                    file_type="regular",
                    size=len(payload),
                    mtime_ns=target.stat().st_mtime_ns,
                    observed_at="2026-07-21T10:00:00+00:00",
                )

            transfer_calls: list[tuple[str, ...]] = []
            head_keys: list[str] = []

            def fake_upload(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                transfer_calls.append(command)

            def fake_read(*args, **kwargs) -> int:
                command = tuple(str(arg) for arg in args if not callable(arg))
                transfer_calls.append(command)
                self.assertEqual(command[:2], ("read", expected_remote))
                kwargs["on_chunk"](payload)
                return len(payload)

            database_settings = SimpleNamespace(
                database_dsn=database_dsn(str(database_path)),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings):
                queue_jobs(relative_path, "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    mock_sdk_upload( side_effect=fake_upload),
                    mock_sdk_read( side_effect=fake_read),
                    patch(
                        "app.storage.s3_client",
                        return_value=VersionedS3Stub(
                            version_bytes=payload,
                            head_object=lambda **kwargs: (
                                head_keys.append(kwargs["Key"])
                                or {
                                    "VersionId": "s3-version-1",
                                    "ContentLength": len(payload),
                                    "StorageClass": "STANDARD",
                                    "ETag": '"etag"',
                                }
                            )
                        ),
                    ),
                ):
                    process_jobs_once()

            upload_destinations = [
                call[2] for call in transfer_calls if call[:1] == ("upload",)
            ]
            cat_calls = [call for call in transfer_calls if call[:1] == ("read",)]
            self.assertGreaterEqual(len(transfer_calls), 2)
            self.assertEqual(transfer_calls[0][2], expected_remote)
            self.assertEqual(cat_calls[0][1], expected_remote)
            self.assertIn(expected_remote, upload_destinations)
            # Includes the checksum-enabled exact HEAD before visible GET fallback.
            self.assertEqual(head_keys, [expected_key] * 4)
