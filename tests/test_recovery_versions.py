"""Version selection for recovery (issue #4).

Seams under test:
- ArchiveCatalog.list_versions / queue_jobs: recoverable flag, explicit
  not-selectable reasons, and recover targeting a chosen Archive Version.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from tests.test_database import run_alembic


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class VersionSelectionTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        database_path = root / "catalog.db"
        self.assertEqual(run_alembic(database_path).returncode, 0)
        with SQLiteConnection(str(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash, is_admin)
                VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
                """,
                (str(source),),
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="notes.txt",
                file_type="regular",
                size=4,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            file_row = catalog.get_file_by_path(2, "notes.txt")
            catalog.mark_local_copy_missing(
                file_row["id"], observed_at="2026-07-21T11:00:00+00:00"
            )
            older = catalog.record_archive_version(
                vault_id=2,
                path="notes.txt",
                object_key="docs/notes.txt",
                provider_version_id="v-old",
                size=4,
                storage_class="STANDARD",
                etag="e1",
                uploaded_at="2026-07-20T10:00:00+00:00",
                observed_at="2026-07-20T10:00:00+00:00",
                scan_id="s1",
            )
            catalog.mark_version_verified(
                older, plaintext_sha256=_sha(b"old!"), verified_at="2026-07-20T10:01:00+00:00"
            )
            newer = catalog.record_archive_version(
                vault_id=2,
                path="notes.txt",
                object_key="docs/notes.txt",
                provider_version_id="v-new",
                size=4,
                storage_class="STANDARD",
                etag="e2",
                uploaded_at="2026-07-21T10:00:00+00:00",
                observed_at="2026-07-21T10:00:00+00:00",
                scan_id="s2",
            )
            catalog.mark_version_verified(
                newer, plaintext_sha256=_sha(b"new!"), verified_at="2026-07-21T10:01:00+00:00"
            )
            mismatched = catalog.record_archive_version(
                vault_id=2,
                path="notes.txt",
                object_key="docs/notes.txt",
                provider_version_id="v-bad",
                size=4,
                storage_class="STANDARD",
                etag="e3",
                uploaded_at="2026-07-22T10:00:00+00:00",
                observed_at="2026-07-22T10:00:00+00:00",
                scan_id="s3",
            )
            catalog.mark_version_mismatch(
                mismatched,
                plaintext_sha256=_sha(b"bad!"),
                checked_at="2026-07-22T10:01:00+00:00",
            )
            connection.execute(
                """
                UPDATE archive_versions
                SET availability='missing'
                WHERE provider_version_id='v-old'
                """
            )
        return database_path

    def test_list_versions_marks_non_selectable_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = self._fixture(Path(directory))
            with SQLiteConnection(str(database_path)) as connection:
                versions = ArchiveCatalog(connection).list_versions(2, "notes.txt")

            by_provider = {row["provider_version_id"]: row for row in versions}
            self.assertFalse(by_provider["v-bad"]["recoverable"])
            self.assertEqual(by_provider["v-bad"]["not_selectable_reason"], "mismatch")
            self.assertFalse(by_provider["v-old"]["recoverable"])
            self.assertEqual(by_provider["v-old"]["not_selectable_reason"], "missing")
            self.assertTrue(by_provider["v-new"]["recoverable"])
            self.assertIsNone(by_provider["v-new"]["not_selectable_reason"])

    def test_queue_recover_can_target_explicit_archive_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = self._fixture(Path(directory))
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                versions = catalog.list_versions(2, "notes.txt")
                # Make older available again and target it explicitly
                older = next(v for v in versions if v["provider_version_id"] == "v-old")
                connection.execute(
                    """
                    UPDATE archive_versions
                    SET availability='available' WHERE id=%s
                    """,
                    (older["id"],),
                )
                job_ids, _total, eligible = catalog.queue_jobs(
                    vault_id=2,
                    path="notes.txt",
                    action="recover",
                    requested_by=1,
                    requested_at="2026-07-22T12:00:00+00:00",
                    group_id="g1",
                    is_directory=False,
                    archive_version_id=older["id"],
                )
                job = connection.execute(
                    "SELECT archive_version_id FROM jobs WHERE id=%s",
                    (job_ids[0],),
                ).fetchone()

            self.assertEqual(eligible, 1)
            self.assertEqual(job["archive_version_id"], older["id"])

    def test_default_recover_targets_highest_recoverable_version_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = self._fixture(Path(directory))
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                versions = catalog.list_versions(2, "notes.txt")
                expected = next(
                    v for v in versions if v["provider_version_id"] == "v-new"
                )
                job_ids, _, _ = catalog.queue_jobs(
                    vault_id=2,
                    path="notes.txt",
                    action="recover",
                    requested_by=1,
                    requested_at="2026-07-22T12:00:00+00:00",
                    group_id="g1",
                    is_directory=False,
                )
                job = connection.execute(
                    "SELECT archive_version_id FROM jobs WHERE id=%s",
                    (job_ids[0],),
                ).fetchone()
            self.assertEqual(job["archive_version_id"], expected["id"])
