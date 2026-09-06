"""Durable missing-archive incidents (issue #303)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.catalog_audit import audit_vault_catalog
from app.services.component_health import (
    load_vault_health,
    record_component_health,
    runtime_fields_from_health,
)
from app.services.lifecycle_policies import create_policy, set_vault_default_policy
from tests.test_database import run_alembic


class CatalogIncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "incidents.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash, is_admin, active)
                VALUES (1, 'owner', 'Owner', 'hash', TRUE, TRUE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'vaults/uuid', 'remote')
                """
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (1, 1, 'owner')"
            )
            policy = create_policy(connection, vault_id=1, name="default")
            set_vault_default_policy(connection, 1, policy)
            catalog = ArchiveCatalog(connection)
            self.version_id = catalog.record_archive_version(
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
            catalog.mark_version_verified(
                self.version_id,
                plaintext_sha256="a" * 64,
                verified_at="2026-01-01T00:01:00+00:00",
            )
            self.file_id = connection.execute(
                "SELECT vault_file_id FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()["vault_file_id"]

    def _paginator(self, versions: list[dict], markers: list[dict] | None = None):
        pages = [{"Versions": versions, "DeleteMarkers": markers or []}]

        class Paginator:
            def paginate(self, **_kwargs):
                return pages

        return Paginator()

    def _empty_client(self) -> Mock:
        client = Mock()
        client.get_paginator.return_value = self._paginator([])
        return client

    def test_missing_archive_audit_opens_deduped_durable_alert(self) -> None:
        vault = {"id": 1, "s3_bucket": "bucket", "s3_prefix": "vaults/uuid"}
        client = self._empty_client()
        with SQLiteConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, vault, client)
            first = connection.execute(
                "SELECT id, status, path FROM catalog_incidents"
            ).fetchall()
            notes = connection.execute(
                "SELECT event, dedupe_key FROM notifications"
            ).fetchall()
        self.assertEqual(report["missing_in_cloud"], 1)
        self.assertEqual(report["healthy"], 0)
        self.assertEqual(report["command_ok"], 1)
        self.assertEqual(report["incidents_opened"], 1)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["status"], "open")
        self.assertEqual(first[0]["path"], "docs/report.pdf")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["event"], "archive_version_missing")

        with SQLiteConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, vault, client)
            incidents = connection.execute(
                "SELECT id, status FROM catalog_incidents"
            ).fetchall()
            notes = connection.execute("SELECT id FROM notifications").fetchall()
        self.assertEqual(report["incidents_opened"], 0)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["status"], "open")
        self.assertEqual(len(notes), 1)

    def test_incident_survives_reopen_and_resolves_when_listed(self) -> None:
        vault = {"id": 1, "s3_bucket": "bucket", "s3_prefix": "vaults/uuid"}
        with SQLiteConnection(str(self.path)) as connection:
            audit_vault_catalog(connection, vault, self._empty_client())
        with SQLiteConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT status FROM catalog_incidents"
            ).fetchone()
            self.assertEqual(row["status"], "open")
            client = Mock()
            client.get_paginator.return_value = self._paginator(
                [
                    {
                        "Key": "vaults/uuid/docs/report.pdf",
                        "VersionId": "v1",
                        "Size": 10,
                        "StorageClass": "STANDARD",
                        "ETag": '"etag"',
                        "IsLatest": True,
                    }
                ]
            )
            client.get_object_tagging.return_value = {"TagSet": []}
            report = audit_vault_catalog(connection, vault, client)
            incident = connection.execute(
                "SELECT status, resolved_at FROM catalog_incidents"
            ).fetchone()
        self.assertEqual(report["healthy"], 1)
        self.assertEqual(report["incidents_resolved"], 1)
        self.assertEqual(incident["status"], "resolved")
        self.assertIsNotNone(incident["resolved_at"])

    def test_authorized_purge_does_not_open_incident(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE archive_versions SET availability='purged' WHERE id=%s",
                (self.version_id,),
            )
            report = audit_vault_catalog(
                connection,
                {"id": 1, "s3_bucket": "bucket", "s3_prefix": "vaults/uuid"},
                self._empty_client(),
            )
            incidents = connection.execute(
                "SELECT id FROM catalog_incidents"
            ).fetchall()
        self.assertEqual(report["missing_in_cloud"], 0)
        self.assertEqual(incidents, [])

    def test_concurrent_upload_does_not_mark_missing_or_alert(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status,
                    requested_by, requested_at, updated_at
                ) VALUES (1, %s, 'docs/report.pdf', 'upload', 'uploading', 1,
                          '2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')
                """,
                (self.file_id,),
            )
            report = audit_vault_catalog(
                connection,
                {"id": 1, "s3_bucket": "bucket", "s3_prefix": "vaults/uuid"},
                self._empty_client(),
            )
            availability = connection.execute(
                "SELECT availability FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()["availability"]
            incidents = connection.execute(
                "SELECT id FROM catalog_incidents"
            ).fetchall()
        self.assertEqual(report["missing_in_cloud"], 0)
        self.assertEqual(availability, "available")
        self.assertEqual(incidents, [])

    def test_incomplete_listing_does_not_mutate_or_alert(self) -> None:
        class BrokenPaginator:
            def paginate(self, **_kwargs):
                raise RuntimeError("listing truncated")

        client = Mock()
        client.get_paginator.return_value = BrokenPaginator()
        with SQLiteConnection(str(self.path)) as connection:
            with self.assertRaisesRegex(RuntimeError, "listing truncated"):
                audit_vault_catalog(
                    connection,
                    {"id": 1, "s3_bucket": "bucket", "s3_prefix": "vaults/uuid"},
                    client,
                )
            availability = connection.execute(
                "SELECT availability FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()["availability"]
            incidents = connection.execute(
                "SELECT id FROM catalog_incidents"
            ).fetchall()
        self.assertEqual(availability, "available")
        self.assertEqual(incidents, [])

    def test_component_health_survives_reopen(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            record_component_health(
                connection,
                vault_id=1,
                component="cloud",
                ok=False,
                error="Cloud scan: timeout",
            )
            record_component_health(
                connection,
                vault_id=1,
                component="audit",
                ok=True,
                verified=True,
                extra={"missing_in_cloud": 1, "healthy": 0, "command_ok": 1},
            )
        with SQLiteConnection(str(self.path)) as connection:
            health = load_vault_health(connection, 1)
            overlay = runtime_fields_from_health(health)
        self.assertEqual(health["cloud"]["last_error"], "Cloud scan: timeout")
        self.assertFalse(health["audit"]["healthy"])
        self.assertEqual(overlay["last_error_cloud"], "Cloud scan: timeout")
        self.assertEqual(overlay["last_audit_healthy"], False)
        self.assertEqual(overlay["last_audit_report"]["missing_in_cloud"], 1)
        self.assertIsNotNone(overlay["last_verified_at"])


if __name__ == "__main__":
    unittest.main()
