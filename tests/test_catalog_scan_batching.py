"""Catalog scan batching (issue #242).

Seams under test:
- ArchiveCatalog.observe_local_copies_batch — the bulk Local Copy observation
  used by ``_scan_tree`` flush batches.  It must be semantically identical to
  the serialized per-file ``observe_local_copy`` path (identity creation,
  upsert columns, digest preservation, dirty ancestors) while issuing a
  constant number of statements (#587: one upsert on ``files``).
- ``_vault_lock_touch`` is taken once per batch, not once per file.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog

from tests.catalog_support import seed_vault
from tests.test_database import run_alembic


def _seed_vault(connection: PostgresConnection, vault_id: int = 2) -> None:
    seed_vault(connection, vault_id=vault_id)


class CatalogScanBatchIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog.db"
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connection = PostgresConnection(str(self.db_path))
        self.connection.__enter__()
        _seed_vault(self.connection, 2)
        self.catalog = ArchiveCatalog(self.connection)

    def tearDown(self) -> None:
        self.connection.__exit__(None, None, None)

    def _rows(self) -> list[dict]:
        return self.connection.execute(
            """
            SELECT path, id::text AS vault_file_id,
                   local_presence AS presence, local_file_type AS file_type,
                   local_size AS size, local_mtime_ns AS mtime_ns,
                   local_sha256 AS plaintext_sha256,
                   local_seen_at AS last_seen_at, local_observed_at AS observed_at
            FROM files
            WHERE vault_id=2
            ORDER BY path
            """
        ).fetchall()

    def test_batch_mints_new_identities_like_serialized_path(self) -> None:
        entries = [
            ("photos/a.jpg", "regular", 100, 1, "2026-07-21T10:00:00+00:00"),
            ("photos/b.jpg", "regular", 200, 2, "2026-07-21T10:00:00+00:00"),
            ("readme.txt", "regular", 12, 3, "2026-07-21T10:00:00+00:00"),
        ]
        ids = self.catalog.observe_local_copies_batch(vault_id=2, entries=entries)
        rows = self._rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["path"] for r in rows], ["photos/a.jpg", "photos/b.jpg", "readme.txt"])
        for row in rows:
            self.assertEqual(row["presence"], "present")
            self.assertEqual(row["file_type"], "regular")
        # Returned map covers every input path.
        self.assertEqual(set(ids), {"photos/a.jpg", "photos/b.jpg", "readme.txt"})
        for path, row in zip(("photos/a.jpg", "photos/b.jpg", "readme.txt"), rows):
            self.assertEqual(ids[path], row["vault_file_id"])

    def test_batch_deduplicates_repeated_paths(self) -> None:
        entries = [
            ("dup.txt", "regular", 10, 1, "2026-07-21T10:00:00+00:00"),
            ("dup.txt", "regular", 20, 2, "2026-07-21T10:00:00+00:00"),
        ]
        ids = self.catalog.observe_local_copies_batch(vault_id=2, entries=entries)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        # Last observation wins.
        self.assertEqual(rows[0]["path"], "dup.txt")
        self.assertEqual(rows[0]["size"], 20)
        self.assertEqual(rows[0]["mtime_ns"], 2)

    def test_batch_preserves_digest_on_unchanged_reobservation(self) -> None:
        entries = [
            ("stable.bin", "regular", 50, 7, "2026-07-21T10:00:00+00:00"),
        ]
        ids = self.catalog.observe_local_copies_batch(vault_id=2, entries=entries)
        self.connection.execute(
            "UPDATE files SET local_sha256=%s WHERE id=%s",
            (bytes.fromhex("d" * 64), ids["stable.bin"]),
        )
        self.catalog.observe_local_copies_batch(
            vault_id=2,
            entries=[("stable.bin", "regular", 50, 7, "2026-07-21T10:01:00+00:00")],
        )
        rows = self._rows()
        self.assertEqual(bytes(rows[0]["plaintext_sha256"]).hex(), "d" * 64)

    def test_batch_clears_digest_when_size_changes(self) -> None:
        entries = [
            ("mutating.bin", "regular", 50, 7, "2026-07-21T10:00:00+00:00"),
        ]
        ids = self.catalog.observe_local_copies_batch(vault_id=2, entries=entries)
        self.connection.execute(
            "UPDATE files SET local_sha256=%s WHERE id=%s",
            (bytes.fromhex("d" * 64), ids["mutating.bin"]),
        )
        self.catalog.observe_local_copies_batch(
            vault_id=2,
            entries=[("mutating.bin", "regular", 99, 8, "2026-07-21T10:01:00+00:00")],
        )
        rows = self._rows()
        self.assertEqual(rows[0]["size"], 99)
        self.assertIsNone(rows[0]["plaintext_sha256"])

    def test_batch_reuses_existing_identities_across_calls(self) -> None:
        first = self.catalog.observe_local_copies_batch(
            vault_id=2,
            entries=[("keep.txt", "regular", 1, 1, "2026-07-21T10:00:00+00:00")],
        )
        second = self.catalog.observe_local_copies_batch(
            vault_id=2,
            entries=[("keep.txt", "regular", 2, 2, "2026-07-21T10:00:00+00:00")],
        )
        self.assertEqual(first["keep.txt"], second["keep.txt"])
        self.assertEqual(len(self._rows()), 1)

    def test_batch_marks_ancestors_dirty_once_per_directory(self) -> None:
        self.catalog.observe_local_copies_batch(
            vault_id=2,
            entries=[
                ("recordings/cam1/a.bin", "regular", 1, 1, "2026-07-21T10:00:00+00:00"),
                ("recordings/cam1/b.bin", "regular", 1, 1, "2026-07-21T10:00:00+00:00"),
                ("recordings/cam2/c.bin", "regular", 1, 1, "2026-07-21T10:00:00+00:00"),
            ],
        )
        dirty = {
            row["path"]
            for row in self.connection.execute(
                "SELECT path FROM directory_aggregate_dirty WHERE vault_id=2"
            ).fetchall()
        }
        # One dirty row per ancestor directory, never per file.
        self.assertEqual(dirty, {"recordings", "recordings/cam1", "recordings/cam2"})

    def test_batch_and_serialized_path_are_semantically_equivalent(self) -> None:
        """The batch path must produce exactly the per-file serialized state."""
        # Serialized per-file reference on a second migrated database file so
        # the two PostgreSQL handles never contend on one WAL file.
        ref_db = Path(self.tmp.name) / "reference.db"
        migrated_ref = run_alembic(ref_db)
        self.assertEqual(migrated_ref.returncode, 0, migrated_ref.stderr)
        serialized_rows = None
        with PostgresConnection(str(ref_db)) as reference:
            seed_vault(reference)
            catalog = ArchiveCatalog(reference)
            for path, file_type, size, mtime_ns, observed_at in [
                ("photos/a.jpg", "regular", 100, 1, "2026-07-21T10:00:00+00:00"),
                ("photos/b.jpg", "regular", 200, 2, "2026-07-21T10:00:00+00:00"),
                ("link/x", "symlink", 0, 3, "2026-07-21T10:00:00+00:00"),
            ]:
                catalog.observe_local_copy(
                    vault_id=2,
                    path=path,
                    file_type=file_type,
                    size=size,
                    mtime_ns=mtime_ns,
                    observed_at=observed_at,
                )
            serialized_rows = reference.execute(
                """
                SELECT path, local_presence AS presence,
                       local_file_type AS file_type, local_size AS size,
                       local_mtime_ns AS mtime_ns, local_seen_at AS last_seen_at
                FROM files
                WHERE vault_id=2
                ORDER BY path
                """
            ).fetchall()

        # Batch path on the fixture connection (vault id 2 pre-seeded).
        self.catalog.observe_local_copies_batch(
            vault_id=2,
            entries=[
                ("photos/a.jpg", "regular", 100, 1, "2026-07-21T10:00:00+00:00"),
                ("photos/b.jpg", "regular", 200, 2, "2026-07-21T10:00:00+00:00"),
                ("link/x", "symlink", 0, 3, "2026-07-21T10:00:00+00:00"),
            ],
        )
        batch_rows = self._rows()

        def key(rows):
            return [
                (
                    r["path"],
                    r["presence"],
                    r["file_type"],
                    r["size"],
                    r["mtime_ns"],
                    r["last_seen_at"],
                )
                for r in rows
            ]

        self.assertEqual(key(serialized_rows), key(batch_rows))

    def test_vault_lock_touch_is_single_per_batch(self) -> None:
        """One vault lock touch for a whole batch, not one per entry."""
        touches: list[tuple] = []

        original = self.catalog._vault_lock_touch

        def recording(self_, *args, **kwargs):
            touches.append(args)
            return original(*args, **kwargs)

        with patch.object(ArchiveCatalog, "_vault_lock_touch", recording):
            self.catalog.observe_local_copies_batch(
                vault_id=2,
                entries=[
                    ("a/1.bin", "regular", 1, 1, "2026-07-21T10:00:00+00:00"),
                    ("a/2.bin", "regular", 1, 1, "2026-07-21T10:00:00+00:00"),
                    ("a/3.bin", "regular", 1, 1, "2026-07-21T10:00:00+00:00"),
                ],
            )
        self.assertEqual(len(touches), 1)


class CatalogScanBatchStatementCountTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog.db"
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connection = PostgresConnection(str(self.db_path))
        self.connection.__enter__()
        _seed_vault(self.connection, 2)
        self.catalog = ArchiveCatalog(self.connection)

    def tearDown(self) -> None:
        self.connection.__exit__(None, None, None)

    def test_100_file_batch_uses_constant_statement_count(self) -> None:
        """A 100-file batch must stay far below 100 per-file round-trips."""
        entries = [
            (f"dir/file-{index:03d}.bin", "regular", index, index, "2026-07-21T10:00:00+00:00")
            for index in range(100)
        ]
        statements = []

        class CountingConnection:
            def __init__(self, inner):
                self.inner = inner

            def execute(self, sql, params=()):
                statements.append(sql)
                return self.inner.execute(sql, params)

        counting = CountingConnection(self.connection)
        catalog = ArchiveCatalog(counting)
        catalog.observe_local_copies_batch(vault_id=2, entries=entries)
        # 1 vault lock touch + 1 bulk upsert + 1 consent revocation + 1
        # dirty-ancestor write: constant, far below 100 per-file calls.
        self.assertLessEqual(len(statements), 4)
        self.assertEqual(len(self._rows()), 100)

    def _rows(self) -> list[dict]:
        return self.connection.execute(
            """
            SELECT path, id AS vault_file_id
            FROM files
            WHERE vault_id=2
            ORDER BY path
            """
        ).fetchall()


if __name__ == "__main__":
    unittest.main()
