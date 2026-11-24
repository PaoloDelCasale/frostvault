"""#759: the rename analysis (#621) costs one probe per file, never a scan.

An adoption interrupted by a restart leaves many unfingerprinted Local Copies
next to many fingerprinted ones. Each of them asked whether a fingerprinted
Local Copy of the same size went missing, and PostgreSQL answered by walking
every fingerprinted file of the Vault: (files without fingerprint) x (files
with fingerprint), about 29 minutes per call at 90k files. These tests seed
50k + 50k files with bulk SQL on disposable PostgreSQL 16 and check, from the
real execution plans, that the rows the statements examine grow linearly.
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from app.services.local_copies import LocalCopies
from app.services.operation_policies import queue_auto_uploads
from tests.fixtures import FrostVaultTestCase, seed_vault


VAULT_ID = 2
PER_KIND = 50_000
OBSERVED_AT = datetime(2026, 11, 17, 17, 30, tzinfo=timezone.utc)
# No file of the bulk has this size: only the rename pair below shares it.
RENAMED_SIZE = 5_000_000


class _Buffered:
    """The rows of one recorded statement, read back like a psycopg cursor."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _Recorder:
    """Times and keeps every statement that carries the rename analysis."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self.statements: list[dict[str, Any]] = []

    def execute(self, query: str, params: Any = None):
        if "any_source" not in query:
            return self._connection.execute(query, params)
        start = time.perf_counter()
        rows = self._connection.execute(query, params).fetchall()
        self.statements.append({
            "query": query, "params": params, "rows": rows,
            "seconds": time.perf_counter() - start,
        })
        return _Buffered(rows)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _plan_nodes(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


class RenameAnalysisScaleTests(FrostVaultTestCase):
    owner = None

    def setUp(self) -> None:
        super().setUp()
        self.source_root = str(self.tmp_path / "source")
        self.connection = self.database.connect()
        self.addCleanup(self.connection.close)
        seed_vault(self.connection, id=VAULT_ID, source_root=self.source_root)
        # Half of the Vault fingerprinted, half still waiting for its digest:
        # an adoption interrupted by a restart. Nothing is missing.
        self.connection.execute(
            """
            INSERT INTO files(
                vault_id, path, path_since, local_presence, local_file_type,
                local_size, local_mtime_ns, local_sha256, local_seen_at, local_observed_at
            )
            SELECT %(vault)s,
                   CASE WHEN n %% 2 = 0 THEN 'hashed/' ELSE 'new/' END || lpad(n::text, 7, '0'),
                   %(at)s, 'present', 'regular', 1000 + n %% 997, 1,
                   CASE WHEN n %% 2 = 0 THEN sha256(int8send(n)) END,
                   %(at)s, %(at)s
            FROM generate_series(1, %(count)s) AS n
            """,
            {"vault": VAULT_ID, "at": OBSERVED_AT, "count": 2 * PER_KIND},
        )
        self.connection.execute("ANALYZE files")

    def add_rename_pair(self) -> str:
        """A fingerprinted Local Copy gone, and a new one of its size: a rename."""
        self.connection.execute(
            """
            INSERT INTO files(
                vault_id, path, path_since, local_presence, local_file_type,
                local_size, local_mtime_ns, local_sha256, local_seen_at, local_observed_at
            ) VALUES (%s, 'old/renamed.bin', %s, 'missing', 'regular', %s, 1,
                      sha256('renamed'::bytea), %s, %s)
            """,
            (VAULT_ID, OBSERVED_AT, RENAMED_SIZE, OBSERVED_AT, OBSERVED_AT),
        )
        later = OBSERVED_AT + timedelta(minutes=10)
        row = self.connection.execute(
            """
            INSERT INTO files(
                vault_id, path, path_since, local_presence, local_file_type,
                local_size, local_mtime_ns, local_seen_at, local_observed_at
            ) VALUES (%s, 'moved/renamed.bin', %s, 'present', 'regular', %s, 1, %s, %s)
            RETURNING id
            """,
            (VAULT_ID, later, RENAMED_SIZE, later, later),
        ).fetchone()
        self.connection.execute("ANALYZE files")
        return str(row["id"])

    def identify(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        recorder = _Recorder(self.connection)
        rows = LocalCopies(recorder).local_copies_to_identify(VAULT_ID)
        self.assertEqual(len(recorder.statements), 1)
        return rows, recorder.statements[0]

    def admit(self) -> dict[str, Any]:
        recorder = _Recorder(self.connection)
        # Every Local Copy is unreadable: only the admission query matters.
        with patch("app.services.vault_paths.safe_local_path", side_effect=ValueError):
            self.assertEqual(queue_auto_uploads(
                recorder, vault_id=VAULT_ID, source_root=self.source_root,
                now=OBSERVED_AT + timedelta(days=1)), 0)
        self.assertEqual(len(recorder.statements), 1)
        return recorder.statements[0]

    def explain(self, statement: dict[str, Any]) -> tuple[int, set[str], float]:
        """Rows of ``files`` the statement read over all loops, its indexes and
        its server execution time in milliseconds."""
        explained = self.connection.execute(
            "EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + statement["query"],
            statement["params"],
        ).fetchone()
        result = next(iter(explained.values()))[0]
        plan = result["Plan"]
        examined = 0
        indexes = set()
        for node in _plan_nodes(plan):
            # A Bitmap Index Scan names its index but not the table.
            if "Index Name" in node:
                indexes.add(node["Index Name"])
            if node.get("Relation Name") != "files":
                continue
            per_loop = (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
                        + node.get("Rows Removed by Index Recheck", 0))
            examined += int(node.get("Actual Loops", 0) * per_loop)
        return examined, indexes, float(result["Execution Time"])

    def assert_linear(self, statement: dict[str, Any], *,
                      max_ms: float | None = 1000) -> set[str]:
        examined, indexes, milliseconds = self.explain(statement)
        # Quadratic was PER_KIND * PER_KIND = 2.5e9 rows; linear is a few
        # passes over the 100k files.
        self.assertLess(examined, 3 * 2 * PER_KIND, (examined, indexes))
        if max_ms is not None:
            self.assertLess(milliseconds, max_ms, (milliseconds, statement["seconds"]))
        return indexes

    def test_without_missing_files_identification_skips_rename_analysis(self):
        rows, statement = self.identify()
        self.assertEqual(rows, [], "nothing missing: no file can be a rename")
        self.assertLess(statement["seconds"], 1.0)
        self.assert_linear(statement)

    def test_without_missing_files_admission_skips_rename_analysis(self):
        statement = self.admit()
        self.assertEqual(len(statement["rows"]), 2 * PER_KIND,
                         "nothing missing: every Local Copy is an admission candidate")
        # The statement returns all 100k candidates: the time to build them
        # in Python is not the rename analysis, nor is sorting and checking
        # Jobs for each of them on the server: only rows examined are bounded.
        self.assert_linear(statement, max_ms=None)

    def test_a_missing_file_still_makes_its_size_twin_a_rename_candidate(self):
        renamed = self.add_rename_pair()
        rows, statement = self.identify()
        self.assertEqual([(row["vault_file_id"], row["path"], row["held"]) for row in rows],
                         [(renamed, "moved/renamed.bin", False)])
        indexes = self.assert_linear(statement)
        self.assertIn("files_rename_missing_size_idx", indexes)
        self.assertNotIn("files_rename_digest_idx", indexes)

        admission = self.admit()
        admitted = {str(row["file_id"]) for row in admission["rows"]}
        self.assertNotIn(renamed, admitted, "a possible rename waits for its digest")
        self.assertEqual(len(admitted), 2 * PER_KIND, "every other Local Copy is a candidate")
        indexes = self.assert_linear(admission, max_ms=None)
        self.assertIn("files_rename_missing_size_idx", indexes)
        self.assertNotIn("files_rename_digest_idx", indexes)

    def test_a_missing_file_outside_the_rename_window_is_not_a_source(self):
        renamed = self.add_rename_pair()
        self.connection.execute(
            "UPDATE files SET local_observed_at=local_observed_at - interval '2 hours' "
            "WHERE path='old/renamed.bin'")
        rows, _statement = self.identify()
        self.assertEqual(rows, [])
        admitted = {str(row["file_id"]) for row in self.admit()["rows"]}
        self.assertIn(renamed, admitted)


if __name__ == "__main__":
    unittest.main()
