"""Single-path catalog lookups must use the current-path index (B1).

Seam: the catalog queries that resolve one logical path.  Without both
``vault_id`` and ``path`` as index conditions the planner scans every file of
the Vault for each lookup, which made watcher events, upload completion, and
automatic upload queueing scale with Vault size. On the rebuilt schema (#587)
the current path lives in ``files`` behind ``files_vault_path_uq``.
"""

from __future__ import annotations

import unittest

import psycopg

from app.services.local_copies import LocalCopies

from tests import catalog_probe
from tests.fixtures import FrostVaultTestCase, seed_vault


class SinglePathLookupPlanTests(FrostVaultTestCase):
    owner = None

    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            seed_vault(connection, id=1, source_root="/source")
            local_copies = LocalCopies(connection)
            for index in range(50):
                local_copies.observe_local_copy(
                    vault_id=1,
                    path=f"folder/file-{index:03d}.txt",
                    file_type="regular",
                    size=index,
                    mtime_ns=index,
                    observed_at="2026-09-30T00:00:00+00:00",
                )

    def _plan(self, connection: psycopg.Connection, sql: str, params: tuple) -> str:
        # Small fixtures may legitimately prefer a sequential scan. Force index
        # planning to prove this query can bound lookup by both keys at scale.
        connection.execute("SET LOCAL enable_seqscan=off")
        rows = connection.execute("EXPLAIN " + sql, params).fetchall()
        return "\n".join(row["QUERY PLAN"] for row in rows)

    def test_path_lookups_search_files_by_vault_and_path(self) -> None:
        with self.database.connect() as connection:
            local_copies = LocalCopies(connection)
            captured: list[tuple[str, tuple]] = []
            original = connection.execute

            def recording(sql: str, params=()):
                captured.append((sql, params if isinstance(params, dict) else tuple(params or ())))
                return original(sql, params)

            connection.execute = recording  # type: ignore[method-assign]
            catalog_probe.get_file_by_path(connection, 1, "folder/file-010.txt")
            local_copies.mark_local_path_missing(
                vault_id=1, path="folder/file-011.txt",
                observed_at="2026-09-30T00:00:01+00:00",
            )
            local_copies.observe_local_copy(
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
                if "path=%s" in sql.replace(" ", "") and ("FROM files" in sql or "UPDATE files" in sql)
            ]
            self.assertGreaterEqual(len(path_lookups), 2)
            for sql, params in path_lookups:
                plan = self._plan(connection, sql, params)
                self.assertRegex(
                    plan,
                    r"(?:Index Scan|Index Only Scan|Bitmap Index Scan) using files_vault_path_uq",
                    f"plan scans the Vault instead of the path index:\n{plan}\n{sql}",
                )
                self.assertRegex(plan, r"Index Cond: .*vault_id = .*1.*path =")
                self.assertNotIn("Seq Scan on files", plan)
            # The bulk observation upserts through the same unique index.
            self.assertTrue(any("ON CONFLICT (vault_id, path)" in sql for sql, _ in captured))

if __name__ == "__main__":
    unittest.main()
