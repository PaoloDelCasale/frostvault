"""Durable missing-archive incidents (issue #303)."""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

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

from app.services.catalog_audit import audit_vault_catalog
from app.services.component_health import (
    load_vault_health,
    record_component_health,
    runtime_fields_from_health,
)
from app.services import notifications
from tests.catalog_support import fingerprint, protect, seed_vault, sha256_hex
from tests.owner_fixtures import insert_owner
from tests.test_database import run_alembic


class CatalogIncidentTests(unittest.TestCase):
    """Missing-version problems are notifications derived from availability (#593)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "incidents.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection)
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
            self.version_id = protect(catalog, 1, "docs/report.pdf", digest)
            self.file_id = catalog.get_file_by_path(1, "docs/report.pdf")["id"]
            self.key = "vaults/x/docs/report.pdf"

    def _problems(self, connection) -> list[dict]:
        return connection.execute(
            "SELECT event, dedupe_key, problem_status, resolved_at, message_params "
            "FROM notifications ORDER BY id"
        ).fetchall()

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
        client = self._empty_client()
        with PostgresConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, self.vault, client)
            notes = self._problems(connection)
        self.assertEqual(report["missing_in_cloud"], 1)
        self.assertEqual(report["healthy"], 0)
        self.assertEqual(report["command_ok"], 1)
        self.assertEqual(report["incidents_opened"], 1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["event"], "archive_version_missing")
        self.assertEqual(notes[0]["dedupe_key"], f"missing:{self.version_id}")
        self.assertEqual(notes[0]["problem_status"], "open")
        self.assertEqual(notes[0]["message_params"]["path"], "docs/report.pdf")

        with PostgresConnection(str(self.path)) as connection:
            report = audit_vault_catalog(connection, self.vault, client)
            self.assertEqual(notifications.count_open_problems(connection), 1)
            notes = self._problems(connection)
        self.assertEqual(report["incidents_opened"], 0)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["problem_status"], "open")

    def test_incident_survives_reopen_and_resolves_when_listed(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            audit_vault_catalog(connection, self.vault, self._empty_client())
        with PostgresConnection(str(self.path)) as connection:
            self.assertEqual(notifications.count_open_problems(connection), 1)
            client = Mock()
            client.get_paginator.return_value = self._paginator(
                [{"Key": self.key, "VersionId": "v1", "Size": 10,
                  "StorageClass": "STANDARD", "IsLatest": True}]
            )
            report = audit_vault_catalog(connection, self.vault, client)
            note = self._problems(connection)[0]
        self.assertEqual(report["healthy"], 1)
        self.assertEqual(report["incidents_resolved"], 1)
        self.assertEqual(note["problem_status"], "resolved")
        self.assertIsNotNone(note["resolved_at"])

    def test_problem_closes_from_the_version_state_alone(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            audit_vault_catalog(connection, self.vault, self._empty_client())
            # A later listing (any observation) locates the exact version again.
            connection.execute(
                "UPDATE archive_versions SET availability='available' WHERE id=%s",
                (self.version_id,),
            )
            self.assertEqual(notifications.count_open_problems(connection), 0)
            self.assertEqual(self._problems(connection)[0]["problem_status"], "resolved")

    def test_concurrent_upload_does_not_mark_missing_or_alert(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, file_id, action, status, origin,
                    requested_at, updated_at
                ) VALUES (1, %s, 'upload', 'uploading', 'manual',
                          '2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')
                """,
                (self.file_id,),
            )
            report = audit_vault_catalog(connection, self.vault, self._empty_client())
            availability = connection.execute(
                "SELECT availability FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()["availability"]
            notes = self._problems(connection)
        self.assertEqual(report["missing_in_cloud"], 0)
        self.assertEqual(availability, "available")
        self.assertEqual(notes, [])

    def test_incomplete_listing_does_not_mutate_or_alert(self) -> None:
        class BrokenPaginator:
            def paginate(self, **_kwargs):
                raise RuntimeError("listing truncated")

        client = Mock()
        client.get_paginator.return_value = BrokenPaginator()
        with PostgresConnection(str(self.path)) as connection:
            with self.assertRaisesRegex(RuntimeError, "listing truncated"):
                audit_vault_catalog(connection, self.vault, client)
            availability = connection.execute(
                "SELECT availability FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()["availability"]
            notes = self._problems(connection)
        self.assertEqual(availability, "available")
        self.assertEqual(notes, [])

    def test_component_health_survives_reopen(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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
        with PostgresConnection(str(self.path)) as connection:
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
