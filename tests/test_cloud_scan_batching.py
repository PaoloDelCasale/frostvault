"""Bounded cloud scan transactions and S3 I/O outside DB locks (issue #299).

Seams under test:
- ``scan_cloud`` — listing reads stay outside write transactions; catalog
  merges commit in ``CLOUD_SCAN_WRITE_BATCH_SIZE`` chunks.
- Unseen Archive Versions are marked missing only after a complete listing
  with no decoder failures while the scan generation is still current.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import os
import psycopg
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

from app.storage import CLOUD_SCAN_WRITE_BATCH_SIZE, scan_cloud
from tests.catalog_support import fingerprint, sha256_hex
from tests.owner_fixtures import insert_owner, insert_vault
from tests.test_database import run_alembic


VAULT_UUID = "00000000-0000-4000-8000-000000000002"
PREFIX = f"vaults/{VAULT_UUID}/"
LEASE_TOKEN = "00000000-0000-4000-8000-0000000000aa"
VAULT = {"id": 2, "uuid": VAULT_UUID, "s3_bucket": "bucket"}


def _seed_vault(connection: PostgresConnection, *, source_root: str = "/source") -> None:
    insert_owner(connection)
    insert_vault(connection, id=2, uuid=VAULT_UUID, source_root=source_root)


def _known_version(
    connection: PostgresConnection, path: str, provider_version_id: str, *, verify: bool = False
) -> str:
    """A version the catalog already knows by its exact key and VersionId."""
    catalog = ArchiveCatalog(connection)
    catalog.observe_local_copy(
        vault_id=2, path=path, file_type="regular", size=1, mtime_ns=1,
        observed_at="2026-07-20T10:00:00+00:00",
    )
    row = catalog.get_file_by_path(2, path)
    version_id = catalog.begin_replacement(
        row["id"], object_key=PREFIX + path, provider_version_id=provider_version_id,
        size=1, storage_class="STANDARD", created_at="2026-07-20T10:00:00+00:00",
    )
    if verify:
        digest = sha256_hex(path.encode())
        fingerprint(catalog, 2, path, digest)
        row = catalog.get_file_by_path(2, path)
        catalog.promote_replacement(
            row["id"], archive_version_id=version_id, plaintext_sha256=digest,
            verified_at="2026-07-20T10:01:00+00:00", local_generation=row["generation"],
        )
    return version_id


def _availability(connection: PostgresConnection, version_id: str) -> str:
    return connection.execute(
        "SELECT availability FROM archive_versions WHERE id=%s", (version_id,)
    ).fetchone()["availability"]


def _version_item(
    key: str,
    version_id: str,
    *,
    size: int = 4,
    when: datetime | None = None,
) -> dict:
    return {
        "Key": PREFIX + key,
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
    paginate=None,
    report: dict | None = None,
) -> int:
    def default_paginate(**_kwargs):
        yield from pages

    client = SimpleNamespace(
        get_paginator=lambda _name: SimpleNamespace(
            paginate=paginate or default_paginate
        ),
    )
    test_settings = SimpleNamespace(
        database_dsn=database_dsn(str(database_path)),
    )
    with (
        patch("app.database.settings", test_settings),
        patch("app.storage.validate_cloud_vault"),
        patch("app.storage.s3_client", return_value=client),
    ):
        return scan_cloud(dict(VAULT), "2026-07-21T10:00:00+00:00", report=report)


class CloudScanLockAndBatchTests(unittest.TestCase):
    def test_cloud_listing_io_does_not_hold_postgresql_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(database_path)) as connection:
                _seed_vault(connection)
                _known_version(connection, "a.txt", "v1")
                _known_version(connection, "b.txt", "v2")

            second_started = threading.Event()
            release_listing = threading.Event()
            concurrent_error: list[BaseException] = []

            def paginate(**_kwargs):
                second_started.set()
                release_listing.wait(timeout=5)
                yield from pages

            def concurrent_writer() -> None:
                second_started.wait(timeout=5)
                try:
                    conn = psycopg.connect(database_dsn(database_path), options="-c lock_timeout=1000")
                    try:
                        conn.execute(
                            "UPDATE owner SET display_name='concurrent' WHERE id=1"
                        )
                        conn.commit()
                    finally:
                        conn.close()
                except BaseException as exc:
                    concurrent_error.append(exc)
                finally:
                    release_listing.set()

            worker = threading.Thread(target=concurrent_writer)
            worker.start()
            pages = [
                {
                    "Versions": [
                        _version_item("a.txt", "v1"),
                        _version_item("b.txt", "v2"),
                    ],
                    "DeleteMarkers": [],
                }
            ]
            count = _run_scan(
                database_path,
                pages=pages,
                paginate=paginate,
            )
            worker.join(timeout=5)
            self.assertEqual(count, 2)
            self.assertEqual(concurrent_error, [])
            with PostgresConnection(str(database_path)) as connection:
                name = connection.execute(
                    "SELECT display_name FROM owner WHERE id=1"
                ).fetchone()["display_name"]
            self.assertEqual(name, "concurrent")

    def test_incomplete_listing_does_not_mark_unseen_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(database_path)) as connection:
                _seed_vault(connection)
                kept = _known_version(connection, "kept.txt", "kept-v1", verify=True)

            def paginate(**_kwargs):
                yield {
                    "Versions": [_version_item("new.txt", "new-v1")],
                    "DeleteMarkers": [],
                }
                raise RuntimeError("listing truncated")

            with self.assertRaisesRegex(RuntimeError, "listing truncated"):
                _run_scan(database_path, pages=[], paginate=paginate)

            with PostgresConnection(str(database_path)) as connection:
                self.assertEqual(_availability(connection, kept), "available")
                current = ArchiveCatalog(connection).get_file_by_path(2, "kept.txt")
            self.assertEqual(current["current_version"]["id"], kept)

    def test_complete_listing_marks_unseen_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(database_path)) as connection:
                _seed_vault(connection)
                gone = _known_version(connection, "gone.txt", "gone-v1")
                still = _known_version(connection, "still.txt", "still-v1")

            report: dict = {}
            count = _run_scan(
                database_path,
                pages=[
                    {
                        "Versions": [_version_item("still.txt", "still-v1")],
                        "DeleteMarkers": [],
                    }
                ],
                report=report,
            )
            self.assertEqual(count, 1)
            with PostgresConnection(str(database_path)) as connection:
                self.assertEqual(_availability(connection, gone), "missing")
                self.assertEqual(_availability(connection, still), "available")

    def test_unknown_versions_are_counted_never_recorded_or_attributed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(database_path)) as connection:
                _seed_vault(connection)
                known = _known_version(connection, "a.txt", "a-v1", verify=True)
                before = connection.execute(
                    "SELECT count(*) AS n FROM files"
                ).fetchone()["n"]

            report: dict = {}
            count = _run_scan(
                database_path,
                pages=[
                    {
                        "Versions": [
                            _version_item("a.txt", "a-v1"),
                            # A newer VersionId on a known path is not attributed.
                            _version_item("a.txt", "a-v2"),
                            # An object no catalog file knows is not discovered.
                            _version_item("orphan.txt", "o-v1"),
                        ],
                        "DeleteMarkers": [{"Key": PREFIX + "a.txt", "VersionId": "dm"}],
                    }
                ],
                report=report,
            )
            self.assertEqual(count, 1)
            self.assertEqual(
                report,
                {
                    "listed_versions": 3, "known_versions": 1, "unknown_versions": 2,
                    "delete_markers": 1, "unrecognized_keys": 0,
                },
            )
            with PostgresConnection(str(database_path)) as connection:
                versions = connection.execute(
                    "SELECT provider_version_id FROM archive_versions ORDER BY 1"
                ).fetchall()
                files = connection.execute(
                    "SELECT count(*) AS n FROM files"
                ).fetchone()["n"]
                current = ArchiveCatalog(connection).get_file_by_path(2, "a.txt")
            self.assertEqual([row["provider_version_id"] for row in versions], ["a-v1"])
            self.assertEqual(files, before)
            self.assertEqual(current["current_version"]["id"], known)
            self.assertIsNone(current["replacing_version"])

    def test_unrecognized_key_does_not_mark_unseen_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(database_path)) as connection:
                _seed_vault(connection)
                kept = _known_version(connection, "kept.txt", "kept-v1")

            item = _version_item("x", "x-v1")
            item["Key"] = "elsewhere/x.txt"
            report: dict = {}
            _run_scan(
                database_path,
                pages=[{"Versions": [item], "DeleteMarkers": []}],
                report=report,
            )
            self.assertEqual(report["unrecognized_keys"], 1)
            with PostgresConnection(str(database_path)) as connection:
                self.assertEqual(_availability(connection, kept), "available")

    def test_superseded_scan_generation_does_not_mark_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(database_path)) as connection:
                _seed_vault(connection)
                kept = _known_version(connection, "kept.txt", "kept-v1")

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
            with PostgresConnection(str(database_path)) as connection:
                self.assertEqual(_availability(connection, kept), "available")

    def test_version_batches_commit_after_listing_without_tag_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(database_path)) as connection:
                _seed_vault(connection)
                for index in range(CLOUD_SCAN_WRITE_BATCH_SIZE + 1):
                    _known_version(connection, f"f{index}.txt", f"v{index}")

            db_contexts: list[str] = []
            listing_during_db = {"value": False}
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

            def paginate(**_kwargs):
                if db_contexts and db_contexts[-1] == "enter":
                    listing_during_db["value"] = True
                yield from pages

            pages = [
                {
                    "Versions": [
                        _version_item(
                            f"f{index}.txt",
                            f"v{index}",
                            when=datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
                        )
                        for index in range(CLOUD_SCAN_WRITE_BATCH_SIZE + 1)
                    ],
                    "DeleteMarkers": [],
                }
            ]
            test_settings = SimpleNamespace(
                database_dsn=database_dsn(str(database_path)),
            )
            with (
                patch("app.database.settings", test_settings),
                patch("app.storage.validate_cloud_vault"),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(
                        get_paginator=lambda _name: SimpleNamespace(
                            paginate=paginate
                        ),
                    ),
                ),
                patch("app.storage.db", side_effect=tracking_db),
            ):
                count = scan_cloud(dict(VAULT), "2026-07-21T10:00:00+00:00")
            self.assertEqual(count, CLOUD_SCAN_WRITE_BATCH_SIZE + 1)
            self.assertFalse(listing_during_db["value"])
            # two version batches + unseen-missing write
            self.assertEqual(db_contexts.count("enter"), 3)

    def test_lease_renewal_succeeds_during_listing_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(database_path)) as connection:
                _seed_vault(connection)
                _known_version(connection, "held.txt", "held-v1")
                file_id = ArchiveCatalog(connection).get_file_by_path(2, "held.txt")["id"]
                job_id = connection.execute(
                    """
                    INSERT INTO jobs(vault_id, file_id, action, status)
                    VALUES (2, %s, 'upload', 'uploading') RETURNING id
                    """,
                    (file_id,),
                ).fetchone()["id"]
                connection.execute(
                    """
                    INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
                    VALUES (%s, %s, '2026-07-21T10:00:00+00:00', '2026-07-21T10:05:00+00:00')
                    """,
                    (job_id, LEASE_TOKEN),
                )

            started = threading.Event()
            release_listing = threading.Event()
            renewed: list[str] = []

            def paginate(**_kwargs):
                started.set()
                release_listing.wait(timeout=5)
                yield {"Versions": [_version_item("a.txt", "v1")], "DeleteMarkers": []}

            def renew_lease() -> None:
                started.wait(timeout=5)
                conn = psycopg.connect(database_dsn(database_path), options="-c lock_timeout=1000")
                try:
                    conn.execute(
                        """
                        UPDATE job_leases
                        SET expires_at='2026-07-21T10:10:00+00:00'
                        WHERE token=%s
                        """,
                        (LEASE_TOKEN,),
                    )
                    conn.commit()
                    renewed.append("ok")
                finally:
                    conn.close()
                    release_listing.set()

            worker = threading.Thread(target=renew_lease)
            worker.start()
            _run_scan(
                database_path,
                pages=[
                    {
                        "Versions": [_version_item("a.txt", "v1")],
                        "DeleteMarkers": [],
                    }
                ],
                paginate=paginate,
            )
            worker.join(timeout=5)
            self.assertEqual(renewed, ["ok"])
            with PostgresConnection(str(database_path)) as connection:
                expiry = connection.execute(
                    "SELECT expires_at FROM job_leases WHERE token=%s", (LEASE_TOKEN,)
                ).fetchone()["expires_at"]
            self.assertEqual(expiry, datetime(2026, 7, 21, 10, 10, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
