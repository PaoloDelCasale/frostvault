"""Single-path catalog lookups must use the current-path index (B1).

Seam: ``ArchiveCatalog`` queries that resolve one logical path.  Without a
``file_paths.vault_id`` predicate the planner starts from ``vault_files`` and
scans every file of the Vault for each lookup, which made watcher events,
upload completion, and automatic upload queueing scale with Vault size.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from pathlib import Path

from app.catalog import ArchiveCatalog

from tests.test_database import run_alembic


class SinglePathLookupPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "catalog.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs')"
            )
            catalog = ArchiveCatalog(connection)
            for index in range(50):
                catalog.observe_local_copy(
                    vault_id=1,
                    path=f"folder/file-{index:03d}.txt",
                    file_type="regular",
                    size=index,
                    mtime_ns=index,
                    observed_at="2026-09-30T00:00:00+00:00",
                )

    def _plan(self, connection: PostgresConnection, sql: str, params: tuple) -> str:
        # Small fixtures may legitimately prefer a sequential scan. Force index
        # planning to prove this query can bound lookup by both keys at scale.
        connection.execute("SET LOCAL enable_seqscan=off")
        rows = connection.execute("EXPLAIN " + sql, params).fetchall()
        return "\n".join(row["QUERY PLAN"] for row in rows)

    def test_path_lookups_search_file_paths_by_vault_and_path(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            captured: list[tuple[str, tuple]] = []
            original = connection.execute

            def recording(sql: str, params=()):
                captured.append((sql, tuple(params or ())))
                return original(sql, params)

            connection.execute = recording  # type: ignore[method-assign]
            catalog.get_file_by_path(1, "folder/file-010.txt")
            catalog.list_versions(1, "folder/file-010.txt")
            catalog.set_local_fingerprint(
                vault_id=1,
                path="folder/file-010.txt",
                plaintext_sha256="0" * 64,
                matched_archive_version_id=None,
            )
            catalog.observe_local_copy(
                vault_id=1,
                path="folder/file-010.txt",
                file_type="regular",
                size=10,
                mtime_ns=10,
                observed_at="2026-09-30T00:00:01+00:00",
            )
            connection.execute = original  # type: ignore[method-assign]

            path_lookups = [
                (sql, params)
                for sql, params in captured
                if "fp.path=%s" in sql and "%s" in sql
            ]
            self.assertGreaterEqual(len(path_lookups), 4)
            for sql, params in path_lookups:
                plan = self._plan(connection, sql, params)
                self.assertIn(
                    "file_paths_one_current_path_uq",
                    plan,
                    f"plan scans the Vault instead of the path index:\n{plan}\n{sql}",
                )
                self.assertRegex(plan, r"Index Cond: .*vault_id = 1.*path =")
                self.assertNotIn("Seq Scan on vault_files", plan)


if __name__ == "__main__":
    unittest.main()
