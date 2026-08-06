"""Plain Rclone destination construction (issue #9).

Seams under test:
- ``plain_rclone_destination`` — builds bucket-rooted plain Rclone object specs.
- Plain upload Job worker — Rclone destinations and HeadObject share one key.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.storage import plain_rclone_destination, process_jobs_once
from tests.test_database import run_alembic


class PlainRcloneDestinationTests(unittest.TestCase):
    def test_two_vault_prefixes_isolate_the_same_logical_path(self) -> None:
        first = plain_rclone_destination(
            "frostvault-plain",
            "vaults/11111111-1111-1111-1111-111111111111",
            "reports/q1.txt",
        )
        second = plain_rclone_destination(
            "frostvault-plain",
            "vaults/22222222-2222-2222-2222-222222222222",
            "reports/q1.txt",
        )
        self.assertEqual(
            first,
            "frostvault-plain:vaults/11111111-1111-1111-1111-111111111111/reports/q1.txt",
        )
        self.assertEqual(
            second,
            "frostvault-plain:vaults/22222222-2222-2222-2222-222222222222/reports/q1.txt",
        )
        self.assertNotEqual(first, second)

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            plain_rclone_destination(
                "frostvault-plain",
                "vaults/11111111-1111-1111-1111-111111111111",
                "../outside.txt",
            )


class PlainUploadPrefixedRemoteTests(unittest.TestCase):
    def test_upload_and_read_back_use_vault_prefix_matching_head_object(self) -> None:
        prefix = "vaults/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        relative_path = "reports/q1.txt"
        expected_key = f"{prefix}/{relative_path}"
        expected_remote = f"frostvault-plain:{expected_key}"
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
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote, encryption_mode
                    ) VALUES (
                        2, 'docs', 'Docs', %s, 'example-bucket', %s,
                        'frostvault-plain', 'plain'
                    )
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

            rclone_calls: list[tuple[str, ...]] = []
            head_keys: list[str] = []

            def fake_rclone(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                rclone_calls.append(command)

            def fake_rclone_stream(*args, **kwargs) -> int:
                command = tuple(str(arg) for arg in args if not callable(arg))
                rclone_calls.append(command)
                self.assertEqual(command[:2], ("cat", expected_remote))
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
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone", side_effect=fake_rclone),
                    patch("app.storage.run_rclone_stream", side_effect=fake_rclone_stream),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
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
                call[2] for call in rclone_calls if call[:1] == ("copyto",)
            ]
            cat_calls = [call for call in rclone_calls if call[:1] == ("cat",)]
            self.assertGreaterEqual(len(rclone_calls), 2)
            self.assertEqual(rclone_calls[0][2], expected_remote)
            self.assertEqual(cat_calls[0][1], expected_remote)
            self.assertIn(expected_remote, upload_destinations)
            self.assertEqual(head_keys, [expected_key, expected_key])


class PlainRenamePrefixedRemoteTests(unittest.TestCase):
    def test_rename_copy_destination_includes_vault_prefix(self) -> None:
        prefix = "vaults/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        old_path = "reports/old-name.txt"
        new_path = "archive/new-name.txt"
        old_key = f"{prefix}/{old_path}"
        new_key = f"{prefix}/{new_path}"
        expected_remote = f"frostvault-plain:{new_key}"
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
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote, encryption_mode
                    ) VALUES (
                        2, 'docs', 'Docs', %s, 'example-bucket', %s,
                        'frostvault-plain', 'plain'
                    )
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

            rclone_calls: list[tuple[str, ...]] = []
            head_keys: list[str] = []

            def fake_rclone(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                rclone_calls.append(command)
                if command[:1] != ("copyto",) or len(command) < 3:
                    return
                origin, destination = command[1], command[2]
                if ":" in origin and not Path(origin).exists():
                    out = Path(destination)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(payload)

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
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone", side_effect=fake_rclone),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
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
                        ),
                    ),
                ):
                    process_jobs_once()

            self.assertTrue(rclone_calls)
            self.assertEqual(rclone_calls[0][2], expected_remote)
            self.assertEqual(head_keys, [new_key, old_key])
