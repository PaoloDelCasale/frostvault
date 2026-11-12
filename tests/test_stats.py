"""Stats are catalog aggregates plus Source Volume safety, with no filesystem walk."""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.config import settings

from app.main import stats
from app.services.runtime_status import runtime_status, snapshot_runtime_status_for_stats
from app.services.source_layout import VaultLocalAccess
from tests.test_database import run_alembic


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.path)) as connection:
            connection.execute("INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (9, 'files', 'Files', '/sources/photos', 'bucket', 'files')")
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(vault_id=9, path="photo.jpg", file_type="regular", size=12, mtime_ns=1, observed_at="2026-07-21")
            catalog.record_archive_version(vault_id=9, path="photo.jpg", object_key="files/photo.jpg", provider_version_id="only-copy", size=44, storage_class="STANDARD", etag="etag", uploaded_at="2026-07-21", observed_at="2026-07-21", scan_id="scan", origin="upload")
        self.cfg = replace(settings, database_dsn=database_dsn(str(self.path)))
        runtime_status.pop(9, None)
        self.addCleanup(runtime_status.pop, 9, None)

    def test_stats_do_not_walk_or_resolve_roots_even_when_volume_is_replaced(self):
        for health in ("ok", "absent", "replaced", "identity_ambiguous", "identity_unsupported"):
            with (
                self.subTest(health=health),
                patch("app.database.settings", self.cfg),
                patch("app.main.settings", self.cfg),
                patch("app.main.vault_local_access", return_value=VaultLocalAccess(health == "ok", True, "photos", health)),
                patch("os.walk", side_effect=AssertionError("no diagnostic walker")),
                patch("os.path.realpath", side_effect=AssertionError("no root resolution")),
                patch.object(ArchiveCatalog, "list_file_rows", side_effect=AssertionError("SQL aggregates only")),
            ):
                payload = stats({"id": 9, "source_root": "/sources/photos"})
            self.assertEqual(payload["states"], {"pending" if health == "ok" else "unconfirmed": 1})
            self.assertEqual(payload["storage"], {"local_bytes": 12, "cloud_bytes": 44})
            self.assertEqual(payload["source_volume"]["health"], health)
            self.assertEqual(payload["source_volume"]["local_operations_allowed"], health == "ok")
            self.assertNotIn("filesystem", payload)
            self.assertNotIn("filesystem", payload["runtime"])

    def test_summary_is_scoped_and_matches_listing(self):
        with PostgresConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            rows = catalog.list_file_rows(9)
            with patch.object(ArchiveCatalog, "list_file_rows", side_effect=AssertionError("not an aggregate")):
                summary = catalog.summary(9)
                empty = catalog.summary(999)
        self.assertEqual(summary["storage"]["local_bytes"], sum(row["local_size"] for row in rows))
        self.assertEqual(empty["storage"], {"local_bytes": 0, "cloud_bytes": 0})

    def test_runtime_snapshot_is_bounded_detached_and_has_no_diagnostic_graph(self):
        report = {"healthy": False, "missing_in_cloud": 2, "unbounded": list(range(10000))}
        recursive = {}; recursive["self"] = recursive
        runtime_status[9] = {"scanning": True, "last_error": "x" * 10000, "last_audit_report": report, "filesystem": recursive, "unknown": recursive}
        snapshot = snapshot_runtime_status_for_stats(9)
        self.assertEqual(len(snapshot["last_error"]), 512)
        self.assertEqual(snapshot["last_audit_report"], {"healthy": False, "missing_in_cloud": 2})
        report["missing_in_cloud"] = 3
        self.assertEqual(snapshot["last_audit_report"]["missing_in_cloud"], 2)
        self.assertNotIn("filesystem", snapshot)
        self.assertLess(len(json.dumps(snapshot)), 1024)
