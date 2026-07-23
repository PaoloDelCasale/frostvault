from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.catalog_audit import audit_vault_catalog
from app.services.lifecycle_policies import (
    create_policy,
    set_vault_default_policy,
)
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
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'vaults/uuid', 'remote')"
            )
            policy = create_policy(connection, vault_id=1, name="default")
            set_vault_default_policy(connection, 1, policy)
            self.policy_id = policy
            catalog = ArchiveCatalog(connection)
            catalog.record_archive_version(
                vault_id=1,
                path="docs/report.pdf",
                object_key="vaults/uuid/docs/report.pdf",
                provider_version_id="v1",
                size=10,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-01-01T00:00:00+00:00",
                observed_at="2026-01-01T00:00:00+00:00",
                scan_id="2026-01-01T00:00:00+00:00",
                desired_policy_id=policy,
                applied_policy_id=policy,
            )

    def _paginator(self, versions: list[dict], markers: list[dict] | None = None):
        pages = [{"Versions": versions, "DeleteMarkers": markers or []}]

        class Paginator:
            def paginate(self, **_kwargs):
                return pages

        return Paginator()

    def test_reports_missing_cloud_version_without_restoring(self) -> None:
        client = Mock()
        client.get_paginator.return_value = self._paginator([])
        vault = {"id": 1, "s3_bucket": "bucket", "s3_prefix": "vaults/uuid"}
        with SQLiteConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, vault, client)
            row = connection.execute(
                "SELECT availability FROM archive_versions"
            ).fetchone()
        self.assertEqual(report["missing_in_cloud"], 1)
        self.assertEqual(row["availability"], "missing")
        client.get_object.assert_not_called()
        client.restore_object.assert_not_called()

    def test_reports_storage_class_and_policy_tag_drift(self) -> None:
        client = Mock()
        client.get_paginator.return_value = self._paginator(
            [
                {
                    "Key": "vaults/uuid/docs/report.pdf",
                    "VersionId": "v1",
                    "Size": 10,
                    "StorageClass": "GLACIER",
                    "ETag": '"etag"',
                    "IsLatest": True,
                }
            ]
        )
        client.get_object_tagging.return_value = {
            "TagSet": [{"Key": "psa:policy-id", "Value": "other-policy"}]
        }
        vault = {"id": 1, "s3_bucket": "bucket", "s3_prefix": "vaults/uuid"}
        with SQLiteConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, vault, client)
            row = connection.execute(
                "SELECT storage_class, applied_policy_id, availability FROM archive_versions"
            ).fetchone()
        self.assertEqual(report["storage_class_drift"], 1)
        self.assertEqual(report["policy_tag_drift"], 1)
        self.assertEqual(row["storage_class"], "GLACIER")
        self.assertEqual(row["applied_policy_id"], "other-policy")
        self.assertEqual(row["availability"], "available")
