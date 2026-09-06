"""Bounded cloud scan transactions and S3 I/O outside DB locks (issue #299).

Seams under test:
- ``scan_cloud`` — listing/tag reads stay outside write transactions; catalog
  merges commit in ``CLOUD_SCAN_WRITE_BATCH_SIZE`` chunks.
- Unseen Archive Versions are marked missing only after a complete listing
  with no decoder failures while the scan generation is still current.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
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
from app.storage import CLOUD_SCAN_WRITE_BATCH_SIZE, scan_cloud
from tests.test_database import run_alembic


def _seed_vault(connection: SQLiteConnection, *, source_root: str = "/source") -> None:
    connection.execute(
        """
        INSERT INTO users(id, username, display_name, password_hash, is_admin)
        VALUES (1, 'owner', 'Owner', 'hash', TRUE)
        """
    )
    connection.execute(
        """
        INSERT INTO vaults(
            id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
        ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
        """,
        (source_root,),
    )


def _version_item(
    key: str,
    version_id: str,
    *,
    size: int = 4,
    when: datetime | None = None,
) -> dict:
    return {
        "Key": key,
        "VersionId": version_id,
        "Size": size,
        "StorageClass": "STANDARD",
        "ETag": f'"{version_id}"',
        "LastModified": when or datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
    }


def _run_scan(
    database_path: Path,
    *,
    pages: list[dict],
    get_object_tagging=None,
    paginate=None,
) -> int:
    def default_paginate(**_kwargs):
        yield from pages

    client = SimpleNamespace(
        get_paginator=lambda _name: SimpleNamespace(
            paginate=paginate or default_paginate
        ),
        get_object_tagging=get_object_tagging
        or (lambda **_kwargs: {"TagSet": []}),
    )
    test_settings = SimpleNamespace(
        db_backend="sqlite",
        sqlite_path=str(database_path),
    )
    with (
        patch("app.database.settings", test_settings),
        patch("app.storage.validate_cloud_vault"),
        patch("app.storage.rclone_remote_is_crypt", return_value=False),
        patch("app.storage.s3_client", return_value=client),
    ):
        return scan_cloud(
            {
                "id": 2,
                "s3_bucket": "bucket",
                "s3_prefix": "docs",
                "rclone_remote": "remote",
            },
            "2026-07-21T10:00:00+00:00",
        )


class CloudScanLockAndBatchTests(unittest.TestCase):
    def test_cloud_tag_io_does_not_hold_sqlite_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)

            second_started = threading.Event()
            release_second_tag = threading.Event()
            concurrent_error: list[BaseException] = []
            tag_calls: list[str] = []

            def get_object_tagging(**kwargs):
                version_id = str(kwargs.get("VersionId") or "")
                tag_calls.append(version_id)
                if version_id == "v2":
                    second_started.set()
                    release_second_tag.wait(timeout=5)
                return {"TagSet": []}

            def concurrent_writer() -> None:
                second_started.wait(timeout=5)
                try:
                    conn = sqlite3.connect(str(database_path), timeout=0)
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            "UPDATE users SET display_name='concurrent' WHERE id=1"
                        )
                        conn.commit()
                    finally:
                        conn.close()
                except BaseException as exc:
                    concurrent_error.append(exc)
                finally:
                    release_second_tag.set()

            worker = threading.Thread(target=concurrent_writer)
            worker.start()
            pages = [
                {
                    "Versions": [
                        _version_item("docs/a.txt", "v1"),
                        _version_item("docs/b.txt", "v2"),
                    ],
                    "DeleteMarkers": [],
                }
            ]
            count = _run_scan(
                database_path,
                pages=pages,
                get_object_tagging=get_object_tagging,
            )
            worker.join(timeout=5)
            self.assertEqual(count, 2)
            self.assertEqual(tag_calls, ["v1", "v2"])
            self.assertEqual(concurrent_error, [])
            with SQLiteConnection(str(database_path)) as connection:
                name = connection.execute(
                    "SELECT display_name FROM users WHERE id=1"
                ).fetchone()["display_name"]
            self.assertEqual(name, "concurrent")

    def test_incomplete_listing_does_not_mark_unseen_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=2,
                    path="kept.txt",
                    file_type="regular",
                    size=1,
                    mtime_ns=1,
                    observed_at="2026-07-20T10:00:00+00:00",
                )
                version_id = catalog.record_archive_version(
                    vault_id=2,
                    path="kept.txt",
                    object_key="docs/kept.txt",
                    provider_version_id="kept-v1",
                    size=1,
                    storage_class="STANDARD",
                    etag="kept",
                    uploaded_at="2026-07-20T10:00:00+00:00",
                    observed_at="2026-07-20T10:00:00+00:00",
                    scan_id="2026-07-20T10:00:00+00:00",
                )
                catalog.mark_version_verified(
                    version_id,
                    plaintext_sha256="a" * 64,
                    verified_at="2026-07-20T10:01:00+00:00",
                )

            def paginate(**_kwargs):
                yield {
                    "Versions": [_version_item("docs/new.txt", "new-v1")],
                    "DeleteMarkers": [],
                }
                raise RuntimeError("listing truncated")

            with self.assertRaisesRegex(RuntimeError, "listing truncated"):
                _run_scan(database_path, pages=[], paginate=paginate)

            with SQLiteConnection(str(database_path)) as connection:
                kept = ArchiveCatalog(connection).get_file_by_path(2, "kept.txt")
            self.assertEqual(kept["latest_version"]["availability"], "available")
            self.assertEqual(
                kept["latest_version"]["provider_version_id"], "kept-v1"
            )

    def test_complete_listing_marks_unseen_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=2,
                    path="gone.txt",
                    file_type="regular",
                    size=1,
                    mtime_ns=1,
                    observed_at="2026-07-20T10:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=2,
                    path="gone.txt",
                    object_key="docs/gone.txt",
                    provider_version_id="gone-v1",
                    size=1,
                    storage_class="STANDARD",
                    etag="gone",
                    uploaded_at="2026-07-20T10:00:00+00:00",
                    observed_at="2026-07-20T10:00:00+00:00",
                    scan_id="2026-07-20T10:00:00+00:00",
                )

            _run_scan(
                database_path,
                pages=[
                    {
                        "Versions": [_version_item("docs/still.txt", "still-v1")],
                        "DeleteMarkers": [],
                    }
                ],
            )
            with SQLiteConnection(str(database_path)) as connection:
                gone = ArchiveCatalog(connection).get_file_by_path(2, "gone.txt")
                still = ArchiveCatalog(connection).get_file_by_path(2, "still.txt")
            self.assertEqual(gone["latest_version"]["availability"], "missing")
            self.assertEqual(still["latest_version"]["availability"], "available")

    def test_superseded_scan_generation_does_not_mark_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=2,
                    path="kept.txt",
                    file_type="regular",
                    size=1,
                    mtime_ns=1,
                    observed_at="2026-07-20T10:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=2,
                    path="kept.txt",
                    object_key="docs/kept.txt",
                    provider_version_id="kept-v1",
                    size=1,
                    storage_class="STANDARD",
                    etag="kept",
                    uploaded_at="2026-07-20T10:00:00+00:00",
                    observed_at="2026-07-20T10:00:00+00:00",
                    scan_id="2026-07-20T10:00:00+00:00",
                )

            from app import storage as storage_module

            with storage_module.status_lock:
                storage_module.runtime_status[2] = {
                    "scanning": True,
                    "scan_id": "2026-07-21T11:00:00+00:00",
                    "last_error": None,
                }
            self.addCleanup(
                lambda: storage_module.runtime_status.pop(2, None)
            )
            _run_scan(
                database_path,
                pages=[{"Versions": [], "DeleteMarkers": []}],
            )
            with SQLiteConnection(str(database_path)) as connection:
                kept = ArchiveCatalog(connection).get_file_by_path(2, "kept.txt")
            self.assertEqual(kept["latest_version"]["availability"], "available")

    def test_version_batches_commit_without_holding_tag_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)

            db_contexts: list[str] = []
            tag_during_db = {"value": False}
            original_db = None

            from app import storage as storage_module

            original_db = storage_module.db

            class _TrackingDb:
                def __init__(self, inner):
                    self.inner = inner

                def __enter__(self):
                    db_contexts.append("enter")
                    return self.inner.__enter__()

                def __exit__(self, *args):
                    db_contexts.append("exit")
                    return self.inner.__exit__(*args)

            def tracking_db():
                return _TrackingDb(original_db())

            tag_calls = {"n": 0}

            def get_object_tagging(**_kwargs):
                tag_calls["n"] += 1
                if db_contexts and db_contexts[-1] == "enter":
                    tag_during_db["value"] = True
                return {"TagSet": []}

            pages = [
                {
                    "Versions": [
                        _version_item(
                            f"docs/f{index}.txt",
                            f"v{index}",
                            when=datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
                        )
                        for index in range(CLOUD_SCAN_WRITE_BATCH_SIZE + 1)
                    ],
                    "DeleteMarkers": [],
                }
            ]
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with (
                patch("app.database.settings", test_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.rclone_remote_is_crypt", return_value=False),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(
                        get_paginator=lambda _name: SimpleNamespace(
                            paginate=lambda **_k: pages
                        ),
                        get_object_tagging=get_object_tagging,
                    ),
                ),
                patch("app.storage.db", side_effect=tracking_db),
            ):
                count = scan_cloud(
                    {
                        "id": 2,
                        "s3_bucket": "bucket",
                        "s3_prefix": "docs",
                        "rclone_remote": "remote",
                    },
                    "2026-07-21T10:00:00+00:00",
                )
            self.assertEqual(count, CLOUD_SCAN_WRITE_BATCH_SIZE + 1)
            self.assertEqual(tag_calls["n"], CLOUD_SCAN_WRITE_BATCH_SIZE + 1)
            self.assertFalse(tag_during_db["value"])
            # assignments read + two version batches + unseen-missing write
            self.assertEqual(db_contexts.count("enter"), 4)

    def test_lease_renewal_succeeds_during_tag_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                _seed_vault(connection)
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=2,
                    path="held.txt",
                    file_type="regular",
                    size=1,
                    mtime_ns=1,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                file_id = catalog.get_file_by_path(2, "held.txt")["id"]
                connection.execute(
                    """
                    INSERT INTO jobs(
                        vault_id, vault_file_id, path, action, status,
                        requested_by, requested_at, updated_at,
                        claim_token, claimed_at, claim_expires_at
                    ) VALUES (
                        2, %s, 'held.txt', 'upload', 'uploading', 1,
                        '2026-07-21T10:00:00+00:00', '2026-07-21T10:00:00+00:00',
                        'live-worker', '2026-07-21T10:00:00+00:00',
                        '2026-07-21T10:05:00+00:00'
                    )
                    """,
                    (file_id,),
                )

            started = threading.Event()
            release_tag = threading.Event()
            renewed: list[str] = []

            def get_object_tagging(**_kwargs):
                started.set()
                release_tag.wait(timeout=5)
                return {"TagSet": []}

            def renew_lease() -> None:
                started.wait(timeout=5)
                conn = sqlite3.connect(str(database_path), timeout=0)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        UPDATE jobs
                        SET claim_expires_at='2026-07-21T10:10:00+00:00'
                        WHERE claim_token='live-worker'
                        """
                    )
                    conn.commit()
                    renewed.append("ok")
                finally:
                    conn.close()
                    release_tag.set()

            worker = threading.Thread(target=renew_lease)
            worker.start()
            _run_scan(
                database_path,
                pages=[
                    {
                        "Versions": [_version_item("docs/a.txt", "v1")],
                        "DeleteMarkers": [],
                    }
                ],
                get_object_tagging=get_object_tagging,
            )
            worker.join(timeout=5)
            self.assertEqual(renewed, ["ok"])
            with SQLiteConnection(str(database_path)) as connection:
                expiry = connection.execute(
                    "SELECT claim_expires_at FROM jobs WHERE claim_token='live-worker'"
                ).fetchone()["claim_expires_at"]
            self.assertEqual(expiry, "2026-07-21T10:10:00+00:00")


if __name__ == "__main__":
    unittest.main()
