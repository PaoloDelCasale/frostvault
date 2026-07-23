"""Quota soft/hard admission auditing (issue #12).

Seams under test:
- Soft warnings and hard blocks from ``admit_quota`` / ``queue_jobs`` are
  recorded through ``audit_log`` on the caller's connection.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.vault_quotas import QuotaBlocked, QuotaLimits, set_limits
from tests.test_database import run_alembic


class QuotaAdmissionAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quota-audit.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'owner', 'Owner', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')"
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (2, 1, 'owner')"
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="big.txt",
                file_type="regular",
                size=100,
                mtime_ns=1,
                observed_at="2026-07-01T00:00:00+00:00",
            )

    def test_soft_warning_is_audited_on_admit(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(connection, 2, QuotaLimits(storage_soft_limit_bytes=10))
            ArchiveCatalog(connection).queue_jobs(
                vault_id=2,
                path="big.txt",
                action="upload",
                requested_by=1,
                requested_at="2026-07-01T00:00:00+00:00",
                group_id="g1",
                is_directory=False,
            )
            events = connection.execute(
                "SELECT event, outcome, detail_json FROM audit_events ORDER BY id"
            ).fetchall()
        soft = [row for row in events if row["event"] == "quota_soft_warning"]
        self.assertEqual(len(soft), 1)
        self.assertEqual(soft[0]["outcome"], "warned")
        detail = json.loads(soft[0]["detail_json"])
        self.assertEqual(detail["decisions"][0]["code"], "quota.storage.soft_exceeded")

    def test_hard_block_is_audited_and_raises(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(connection, 2, QuotaLimits(storage_hard_limit_bytes=10))
            with self.assertRaises(QuotaBlocked):
                ArchiveCatalog(connection).queue_jobs(
                    vault_id=2,
                    path="big.txt",
                    action="upload",
                    requested_by=1,
                    requested_at="2026-07-01T00:00:00+00:00",
                    group_id="g1",
                    is_directory=False,
                )
            events = connection.execute(
                "SELECT event, outcome, detail_json FROM audit_events ORDER BY id"
            ).fetchall()
        blocked = [row for row in events if row["event"] == "quota_hard_block"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["outcome"], "blocked")
        detail = json.loads(blocked[0]["detail_json"])
        self.assertEqual(detail["decisions"][0]["code"], "quota.storage.hard_exceeded")


if __name__ == "__main__":
    unittest.main()
