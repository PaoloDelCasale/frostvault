from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.catalog import ArchiveCatalog

from app.services.catalog_audit import audit_vault_catalog
from tests.catalog_support import fingerprint, protect, seed_vault, sha256_hex
from tests.test_database import run_alembic
import tempfile
from pathlib import Path


class CatalogAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "audit.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.path)) as connection:
            seed_vault(connection, vault_id=1)
            self.vault = dict(connection.execute(
                "SELECT id, uuid FROM vaults WHERE id=1"
            ).fetchone(), s3_bucket="bucket")
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=1, path="docs/report.pdf", file_type="regular",
                size=10, mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00",
            )
            digest = sha256_hex(b"report")
            fingerprint(catalog, 1, "docs/report.pdf", digest)
            protect(catalog, 1, "docs/report.pdf", digest)
            self.key = "vaults/x/docs/report.pdf"

    def _paginator(self, versions: list[dict], markers: list[dict] | None = None):
        pages = [{"Versions": versions, "DeleteMarkers": markers or []}]

        class Paginator:
            def paginate(self, **_kwargs):
                return pages

        return Paginator()

    def test_reports_missing_cloud_version_without_restoring(self) -> None:
        client = Mock()
        client.get_paginator.return_value = self._paginator([])
        vault = self.vault
        with PostgresConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, vault, client)
            row = connection.execute(
                "SELECT availability FROM archive_versions"
            ).fetchone()
        self.assertEqual(report["missing_in_cloud"], 1)
        self.assertEqual(row["availability"], "missing")
        client.get_object.assert_not_called()
        client.restore_object.assert_not_called()

    def test_reports_storage_class_drift_without_tag_reads(self) -> None:
        client = Mock()
        client.get_paginator.return_value = self._paginator(
            [
                {
                    "Key": self.key,
                    "VersionId": "v1",
                    "Size": 10,
                    "StorageClass": "GLACIER",
                    "ETag": '"etag"',
                    "IsLatest": True,
                }
            ]
        )
        vault = self.vault
        with PostgresConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, vault, client)
            row = connection.execute(
                "SELECT storage_class, availability FROM archive_versions"
            ).fetchone()
        self.assertEqual(report["storage_class_drift"], 1)
        self.assertEqual(row["storage_class"], "GLACIER")
        self.assertEqual(row["availability"], "available")


class CatalogAuditUnknownObjectTests(CatalogAuditTests):
    def test_unknown_objects_and_markers_are_reported_never_recorded(self) -> None:
        client = Mock()
        client.get_paginator.return_value = self._paginator(
            [
                {"Key": self.key, "VersionId": "v1", "StorageClass": "STANDARD"},
                # Same key, another VersionId: not attributed to the file.
                {"Key": self.key, "VersionId": "v0", "StorageClass": "STANDARD"},
                {"Key": "vaults/x/other.txt", "VersionId": "o1", "StorageClass": "STANDARD"},
            ],
            [{"Key": self.key, "VersionId": "m1"}],
        )
        with PostgresConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, self.vault, client)
            versions = connection.execute(
                "SELECT provider_version_id FROM archive_versions"
            ).fetchall()
            files = connection.execute("SELECT path FROM files").fetchall()
        self.assertEqual(report["missing_in_catalog"], 2)
        self.assertEqual(report["missing_delete_markers"], 1)
        self.assertEqual([row["provider_version_id"] for row in versions], ["v1"])
        self.assertEqual([row["path"] for row in files], ["docs/report.pdf"])
        client.delete_object.assert_not_called()
