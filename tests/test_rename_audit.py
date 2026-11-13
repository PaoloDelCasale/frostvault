"""Routine automatic renames are not logged (#416), on the rebuilt model (#595).

The #188 confirmation races moved to ``tests/test_catalog_rename`` (#587);
rename Jobs live in ``tests/test_rename_sync``.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.config import settings
from app.storage import apply_auto_renames
from tests.catalog_support import seed_vault
from tests.owner_fixtures import insert_owner
from tests.postgres import PostgresConnection, database_dsn
from tests.test_database import run_alembic

PAYLOAD = b"content-a"
DIGEST_A = hashlib.sha256(PAYLOAD).hexdigest()


class RenameAuditPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        self.source_root = Path(self._tmp.name) / "source"
        self.source_root.mkdir()
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with PostgresConnection(str(self.database_path)) as connection:
            insert_owner(connection)
            self.vault_id = seed_vault(connection, vault_id=2, source_root=str(self.source_root))
        configured = replace(settings, database_dsn=database_dsn(str(self.database_path)),
                             cookie_secure=False)
        for target in ("app.database.settings", "app.storage.settings"):
            self.enterContext(patch(target, configured))

    def test_routine_auto_rename_does_not_create_action_log_entries(self) -> None:
        old_path, new_path = "auto/old.txt", "auto/new.txt"
        new_file = self.source_root / new_path
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_bytes(PAYLOAD)
        with PostgresConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            old_id = catalog.observe_local_copy(
                vault_id=self.vault_id, path=old_path, file_type="regular", size=len(PAYLOAD),
                mtime_ns=100, observed_at="2026-07-21T10:00:00+00:00")
            generation = connection.execute(
                "SELECT generation FROM files WHERE id=%s", (old_id,)).fetchone()["generation"]
            self.assertTrue(catalog.publish_local_fingerprint(
                old_id, generation=generation, plaintext_sha256=DIGEST_A,
                size=len(PAYLOAD), mtime_ns=100))
            catalog.mark_local_copy_missing(old_id, observed_at="2026-07-21T11:00:00+00:00")
            catalog.observe_local_copy(
                vault_id=self.vault_id, path=new_path, file_type="regular", size=len(PAYLOAD),
                mtime_ns=new_file.stat().st_mtime_ns, observed_at="2026-07-21T11:00:00+00:00")
            # The new path's fingerprint stays unset so apply_auto_renames hashes it.
        vault = {"id": self.vault_id, "source_root": str(self.source_root)}
        # The scan timestamp stays correlated with the disappearance fixture;
        # automatic rename matching rejects stale digest matches.
        with patch("app.storage.now_iso", return_value="2026-07-21T11:00:00+00:00"):
            summary = apply_auto_renames(vault)
        self.assertGreaterEqual(summary["confirmed"], 1)
        with PostgresConnection(str(self.database_path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT path FROM files WHERE id=%s", (old_id,)).fetchone()["path"], new_path)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS n FROM audit_events WHERE event='vault_file_renamed'"
            ).fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
