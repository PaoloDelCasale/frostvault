"""S3 destination construction (issue #9).

Seams under test:
- ``expected_cloud_key`` — builds exact namespaced S3 keys.
- Plain upload Job worker — SDK destinations and HeadObject share one key.
"""

from __future__ import annotations

import hashlib
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
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.storage import expected_cloud_key, process_jobs_once
from tests.sdk_transfer_stub import mock_sdk_upload, mock_sdk_read
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

            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash
                    ) VALUES (1, 'owner', 'Owner', 'hash')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (
                        2, 'docs', 'Docs', %s, 'example-bucket', %s)
                    """,
                    (str(source), prefix),
                )
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
                db_backend="sqlite",
                sqlite_path=str(database_path),
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
            self.assertEqual(head_keys, [expected_key, expected_key, expected_key])


class S3RenamePrefixTests(unittest.TestCase):
    def test_rename_copy_destination_includes_vault_prefix(self) -> None:
        prefix = "vaults/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        old_path = "reports/old-name.txt"
        new_path = "archive/new-name.txt"
        old_key = f"{prefix}/{old_path}"
        new_key = f"{prefix}/{new_path}"
        expected_remote = f"{new_key}"
        payload = b"rename-prefixed"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = source / new_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            database_path = root / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            digest = hashlib.sha256(payload).hexdigest()

            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash
                    ) VALUES (1, 'owner', 'Owner', 'hash')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (
                        2, 'docs', 'Docs', %s, 'example-bucket', %s)
                    """,
                    (str(source), prefix),
                )
                catalog = ArchiveCatalog(connection)
                file_id = catalog.observe_local_copy(
                    vault_id=2,
                    path=old_path,
                    file_type="regular",
                    size=len(payload),
                    mtime_ns=target.stat().st_mtime_ns,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                version_id = catalog.record_archive_version(
                    vault_id=2,
                    path=old_path,
                    object_key=f"{prefix}/{old_path}",
                    provider_version_id="old-s3-version",
                    size=len(payload),
                    storage_class="STANDARD",
                    etag="old-etag",
                    uploaded_at="2026-07-21T10:01:00+00:00",
                    observed_at="2026-07-21T10:01:00+00:00",
                    scan_id="2026-07-21T10:01:00+00:00",
                    origin="upload",
                )
                catalog.mark_version_verified(
                    version_id,
                    plaintext_sha256=digest,
                    verified_at="2026-07-21T10:02:00+00:00",
                )
                catalog.set_local_fingerprint(
                    vault_id=2,
                    path=old_path,
                    plaintext_sha256=digest,
                    matched_archive_version_id=version_id,
                )
                catalog.mark_local_copy_missing(
                    file_id, observed_at="2026-07-21T11:00:00+00:00"
                )
                catalog.observe_local_copy(
                    vault_id=2,
                    path=new_path,
                    file_type="regular",
                    size=len(payload),
                    mtime_ns=target.stat().st_mtime_ns,
                    observed_at="2026-07-21T11:00:00+00:00",
                )
                catalog.set_local_fingerprint(
                    vault_id=2,
                    path=new_path,
                    plaintext_sha256=digest,
                    matched_archive_version_id=None,
                )
                catalog.confirm_file_rename(
                    vault_file_id=file_id,
                    new_path=new_path,
                    changed_at="2026-07-21T11:05:00+00:00",
                    vault_id=2,
                )

            transfer_calls: list[tuple[str, ...]] = []
            head_keys: list[str] = []
            download_calls: list[dict[str, str]] = []

            def fake_upload(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                transfer_calls.append(command)
                if command[:1] != ("upload",) or len(command) < 3:
                    return
                origin = command[1]
                if ":" in origin and not Path(origin).exists():
                    raise RuntimeError(
                        "plain rename verification must pin S3 VersionId"
                    )

            def fake_download_file(
                Bucket: str,
                Key: str,
                Filename: str,
                ExtraArgs: dict | None = None,
                Callback=None,
                Config=None,
            ) -> None:
                version_id = str((ExtraArgs or {}).get("VersionId") or "")
                download_calls.append(
                    {"Bucket": Bucket, "Key": Key, "VersionId": version_id}
                )
                out = Path(Filename)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(payload)
                if Callback is not None:
                    Callback(len(payload))

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings):
                queued = queue_jobs(new_path, "rename", 2, 1)
                self.assertEqual(queued["item_count"], 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    mock_sdk_upload( side_effect=fake_upload),
                    patch(
                        "app.storage.s3_client",
                        return_value=VersionedS3Stub(
                            initial_versions={old_key: {"old-s3-version"}},
                            head_object=lambda **kwargs: (
                                head_keys.append(kwargs["Key"])
                                or {
                                    "VersionId": (
                                        "old-s3-version"
                                        if kwargs["Key"] == old_key
                                        else "new-s3-version"
                                    ),
                                    "ContentLength": len(payload),
                                    "StorageClass": "STANDARD",
                                    "ETag": '"new-etag"',
                                }
                            ),
                            delete_object=lambda **kwargs: {
                                "VersionId": "delete-marker-1",
                                "DeleteMarker": True,
                            },
                            download_file=fake_download_file,
                        ),
                    ),
                ):
                    process_jobs_once()

            self.assertTrue(transfer_calls)
            self.assertEqual(transfer_calls[0][2], expected_remote)
            self.assertEqual(head_keys, [new_key, new_key, new_key])
            self.assertEqual(
                download_calls,
                [
                    {
                        "Bucket": "example-bucket",
                        "Key": new_key,
                        "VersionId": "new-s3-version",
                    }
                ],
            )
