"""Cloud scan must not trust or repair a catalog that is behind S3 (#408, #593).

Seam: ``scan_cloud``. A restored metadata backup can name a Current (A) whose
S3 VersionId was already purged after a newer upload (B) the catalog never
recorded. The listing is observation only (ADR-0035): B is never recorded,
attributed, elected Current or deleted, and nothing is purged by a scan; the
known version the complete listing did not show is marked missing.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.storage import scan_cloud
from tests.catalog_support import fingerprint, sha256_hex
from tests.owner_fixtures import insert_owner, insert_vault
from tests.postgres import PostgresConnection, database_dsn
from tests.test_database import run_alembic

VAULT_UUID = "00000000-0000-4000-8000-000000000002"
PREFIX = f"vaults/{VAULT_UUID}/"
VAULT = {"id": 2, "uuid": VAULT_UUID, "s3_bucket": "bucket"}


def _item(version_id: str, hour: int) -> dict:
    return {
        "Key": PREFIX + "report.txt",
        "VersionId": version_id,
        "Size": 4,
        "StorageClass": "STANDARD",
        "ETag": f'"{version_id}"',
        "LastModified": datetime(2026, 7, 21, hour, 0, tzinfo=timezone.utc),
    }


class _ListingS3:
    def __init__(self, versions: list[dict]) -> None:
        self.versions = versions
        self.deleted: list[tuple[str, str]] = []

    def get_paginator(self, _name: str):
        return SimpleNamespace(
            paginate=lambda **_kwargs: iter(
                [{"Versions": list(self.versions), "DeleteMarkers": []}]
            )
        )

    def delete_object(self, **kwargs):
        self.deleted.append((str(kwargs["Key"]), str(kwargs["VersionId"])))
        return {"VersionId": kwargs["VersionId"]}


class SnapshotScanStaleCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        # Direct scan_cloud calls have no registered scan generation.
        runtime = patch("app.storage.runtime_status", {})
        runtime.start()
        self.addCleanup(runtime.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "catalog.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        digest = sha256_hex(b"report")
        with PostgresConnection(str(self.database_path)) as connection:
            insert_owner(connection)
            insert_vault(connection, id=2, uuid=VAULT_UUID, source_root="/source")
            catalog = ArchiveCatalog(connection)
            self.file_id = catalog.observe_local_copy(
                vault_id=2, path="report.txt", file_type="regular", size=4,
                mtime_ns=1, observed_at="2026-07-21T09:00:00+00:00",
            )
            fingerprint(catalog, 2, "report.txt", digest)
            row = catalog.get_file_by_path(2, "report.txt")
            self.current_id = catalog.begin_replacement(
                row["id"], object_key=PREFIX + "report.txt", provider_version_id="s3-a",
                size=4, storage_class="STANDARD", created_at="2026-07-21T10:00:00+00:00",
            )
            catalog.promote_replacement(
                row["id"], archive_version_id=self.current_id, plaintext_sha256=digest,
                verified_at="2026-07-21T10:01:00+00:00", local_generation=row["generation"],
            )

    def _scan(self, s3: _ListingS3, scan_id: str = "2026-07-21T13:00:00+00:00") -> dict:
        report: dict = {}
        settings = SimpleNamespace(database_dsn=database_dsn(str(self.database_path)))
        with (
            patch("app.database.settings", settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client", return_value=s3),
        ):
            scan_cloud(dict(VAULT), scan_id, report=report)
        return report

    def _versions(self) -> dict[str, str]:
        with PostgresConnection(str(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT provider_version_id, availability FROM archive_versions"
            ).fetchall()
        return {row["provider_version_id"]: row["availability"] for row in rows}

    def _file(self) -> dict:
        with PostgresConnection(str(self.database_path)) as connection:
            return ArchiveCatalog(connection).get_file_by_path(2, "report.txt")

    def test_uncatalogued_newer_version_is_never_adopted_when_current_is_gone(self) -> None:
        s3 = _ListingS3([_item("s3-b", 12)])
        report = self._scan(s3)

        self.assertEqual(s3.deleted, [], "the only cloud copy must never be deleted")
        self.assertEqual(report["unknown_versions"], 1)
        # B is not recorded; A really is no longer listed.
        self.assertEqual(self._versions(), {"s3-a": "missing"})
        current = self._file()
        self.assertEqual(current["current_version"]["id"], self.current_id)
        self.assertIsNone(current["replacing_version"])

    def test_uncatalogued_extras_are_never_elected_or_deleted(self) -> None:
        for hour in (10, 12, 14):
            with self.subTest(extra_hour=hour):
                s3 = _ListingS3([_item("s3-a", 12), _item(f"s3-x{hour}", hour)])
                self._scan(s3)
                self.assertEqual(s3.deleted, [])
                self.assertEqual(self._versions(), {"s3-a": "available"})
                current = self._file()
                self.assertEqual(current["current_version"]["id"], self.current_id)
                self.assertIsNone(current["replacing_version"])

    def test_listed_pending_replacement_is_not_promoted(self) -> None:
        with PostgresConnection(str(self.database_path)) as connection:
            pending_id = ArchiveCatalog(connection).begin_replacement(
                self.file_id, object_key=PREFIX + "report.txt", provider_version_id="s3-b",
                size=4, storage_class="STANDARD", created_at="2026-07-21T12:00:00+00:00",
            )
        s3 = _ListingS3([_item("s3-a", 10), _item("s3-b", 12)])
        self._scan(s3)

        self.assertEqual(s3.deleted, [])
        self.assertEqual(self._versions(), {"s3-a": "available", "s3-b": "available"})
        current = self._file()
        self.assertEqual(current["current_version"]["id"], self.current_id)
        self.assertEqual(current["replacing_version"]["id"], pending_id)
        with PostgresConnection(str(self.database_path)) as connection:
            integrity = connection.execute(
                "SELECT integrity FROM archive_versions WHERE id=%s", (pending_id,)
            ).fetchone()["integrity"]
        self.assertNotEqual(integrity, "verified", "a listing is not verification")


if __name__ == "__main__":
    unittest.main()
