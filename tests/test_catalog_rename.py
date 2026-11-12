"""Atomic rename on the rebuilt catalog (#587, ADR-0001).

Moved here from ``test_rename_sync`` (automatic rename matching) and
``test_rename_audit`` (#188 confirmation races) and ported to one ``files`` row
per Vault File with closed-only Path History; the folder rename cases come
from ``test_rename_sync`` too. The cloud rename Job (copy,
verify, cleanup of the old key) stays in ``test_rename_sync`` for #595.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import psycopg

from app.catalog import ArchiveCatalog, VaultFileNotFound
from tests.catalog_support import fingerprint, protect, seed_vault, sha256_hex
from tests.postgres import PostgresConnection, run_alembic

DIGEST_A = sha256_hex(b"content-a")
DIGEST_B = sha256_hex(b"content-b")


class _CatalogFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "rename.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with PostgresConnection(str(self.database_path)) as connection:
            seed_vault(connection)

    def connect(self) -> psycopg.Connection:
        return PostgresConnection(str(self.database_path))

    def seed_rename(
        self,
        catalog: ArchiveCatalog,
        *,
        old_path: str,
        new_path: str,
        old_digest: str = DIGEST_A,
        new_digest: str = DIGEST_A,
        old_size: int = 9,
        new_size: int = 9,
        old_seen: str = "2026-07-21T10:00:00+00:00",
        gone_at: str = "2026-07-21T11:00:00+00:00",
        new_seen: str = "2026-07-21T11:00:00+00:00",
        protected: bool = False,
    ) -> tuple[str, str]:
        old_id = catalog.observe_local_copy(
            vault_id=2, path=old_path, file_type="regular", size=old_size,
            mtime_ns=100, observed_at=old_seen,
        )
        self.assertTrue(fingerprint(catalog, 2, old_path, old_digest))
        if protected:
            protect(catalog, 2, old_path, old_digest,
                    provider_version_id=f"rename-{uuid.uuid4()}")
        catalog.mark_local_copy_missing(old_id, observed_at=gone_at)
        new_id = catalog.observe_local_copy(
            vault_id=2, path=new_path, file_type="regular", size=new_size,
            mtime_ns=100, observed_at=new_seen,
        )
        self.assertTrue(fingerprint(catalog, 2, new_path, new_digest))
        return old_id, new_id


class RenameMatchingTests(_CatalogFixture):
    def test_unique_digest_match_is_auto_confirmed_without_duplicate_file(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id, new_id = self.seed_rename(
                catalog, old_path="reports/old-name.txt", new_path="reports/new-name.txt",
            )
            candidates = catalog.list_rename_candidates(vault_id=2)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["missing_vault_file_id"], old_id)
            self.assertEqual(candidates[0]["new_vault_file_id"], new_id)
            self.assertEqual(candidates[0]["digest"], DIGEST_A)

            confirmed_id = catalog.confirm_file_rename(
                vault_file_id=old_id,
                new_path="reports/new-name.txt",
                changed_at="2026-07-21T11:05:00+00:00",
                vault_id=2,
            )
            renamed = catalog.get_file_by_path(2, "reports/new-name.txt")
            old_path = catalog.get_file_by_path(2, "reports/old-name.txt")
            history = catalog.list_path_history(confirmed_id, vault_id=2)
            files = connection.execute("SELECT count(*) AS n FROM files").fetchone()["n"]
            closed = connection.execute(
                "SELECT file_id::text AS file_id, path FROM file_path_history"
            ).fetchall()

        self.assertEqual(confirmed_id, old_id)
        self.assertEqual(renamed["id"], old_id)
        self.assertEqual(renamed["local_copy"]["presence"], "present")
        self.assertEqual(renamed["local_copy"]["plaintext_sha256"], DIGEST_A)
        self.assertIsNone(old_path)
        self.assertEqual(
            [entry["path"] for entry in history],
            ["reports/old-name.txt", "reports/new-name.txt"],
        )
        # One row per Vault File: the provisional identity is consumed, and
        # Path History holds only the closed mapping.
        self.assertEqual(files, 1)
        self.assertEqual(closed, [{"file_id": old_id, "path": "reports/old-name.txt"}])

    def test_unique_but_stale_digest_match_remains_a_new_file(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id, new_id = self.seed_rename(
                catalog, old_path="reports/old-name.txt", new_path="archive/old-name.txt",
                old_seen="2026-01-01T10:00:00+00:00", gone_at="2026-01-01T11:00:00+00:00",
            )
            candidates = catalog.list_rename_candidates(vault_id=2)
        self.assertEqual(candidates, [])
        self.assertNotEqual(old_id, new_id)

    def test_same_name_changed_content_remains_a_new_file(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id, new_id = self.seed_rename(
                catalog, old_path="reports/notes.txt", new_path="archive/notes.txt",
                new_digest=DIGEST_B, new_size=12,
            )
            candidates = catalog.list_rename_candidates(vault_id=2)
            with self.assertRaises(VaultFileNotFound):
                catalog.confirm_file_rename(
                    vault_file_id=old_id,
                    new_path="archive/notes.txt",
                    changed_at="2026-07-21T11:05:00+00:00",
                    vault_id=2,
                )
            self.assertEqual(catalog.get_file_by_path(2, "archive/notes.txt")["id"], new_id)
        self.assertEqual(candidates, [])
        self.assertNotEqual(old_id, new_id)

    def test_ambiguous_equal_digest_candidates_are_never_auto_merged(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            missing_ids = []
            for path in ("a/one.txt", "a/two.txt"):
                file_id = catalog.observe_local_copy(
                    vault_id=2, path=path, file_type="regular", size=9, mtime_ns=100,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                fingerprint(catalog, 2, path, DIGEST_A)
                catalog.mark_local_copy_missing(file_id, observed_at="2026-07-21T11:00:00+00:00")
                missing_ids.append(file_id)
            for path in ("b/one.txt", "b/two.txt"):
                catalog.observe_local_copy(
                    vault_id=2, path=path, file_type="regular", size=9, mtime_ns=100,
                    observed_at="2026-07-21T11:00:00+00:00",
                )
                fingerprint(catalog, 2, path, DIGEST_A)
            candidates = catalog.list_rename_candidates(vault_id=2)
            with self.assertRaises(VaultFileNotFound):
                catalog.confirm_file_rename(
                    vault_file_id=missing_ids[0], new_path="b/one.txt",
                    changed_at="2026-07-21T11:05:00+00:00", vault_id=2,
                )
            before_a = catalog.get_file_by_path(2, "a/one.txt")
            before_b = catalog.get_file_by_path(2, "b/one.txt")
        self.assertEqual(candidates, [])
        self.assertNotEqual(before_a["id"], before_b["id"])
        self.assertEqual(before_a["id"], missing_ids[0])

    def test_size_and_mtime_alone_never_produce_automatic_match(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id = catalog.observe_local_copy(
                vault_id=2, path="reports/old-name.txt", file_type="regular", size=42,
                mtime_ns=1_750_000_000_000_000_000, observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.mark_local_copy_missing(old_id, observed_at="2026-07-21T11:00:00+00:00")
            new_id = catalog.observe_local_copy(
                vault_id=2, path="reports/new-name.txt", file_type="regular", size=42,
                mtime_ns=1_750_000_000_000_000_000, observed_at="2026-07-21T11:00:00+00:00",
            )
            candidates = catalog.list_rename_candidates(vault_id=2)
            old_file = catalog.get_file_by_path(2, "reports/old-name.txt")
            new_file = catalog.get_file_by_path(2, "reports/new-name.txt")
        self.assertEqual(candidates, [])
        self.assertEqual(old_file["id"], old_id)
        self.assertEqual(new_file["id"], new_id)
        self.assertNotEqual(old_id, new_id)


class FolderRenameTests(_CatalogFixture):
    def test_folder_rename_updates_all_descendants_atomically(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            ids = {}
            for relative in ("docs/a.txt", "docs/nested/b.txt", "docs/nested/c.txt"):
                old_id, _ = self.seed_rename(
                    catalog, old_path=relative,
                    new_path=relative.replace("docs/", "archive/", 1),
                    old_digest=sha256_hex(relative.encode()),
                    new_digest=sha256_hex(relative.encode()),
                )
                ids[relative] = old_id

            # A folder move is just a batch of independently safe matches.
            renamed_ids = [
                catalog.confirm_file_rename(
                    vault_file_id=match["missing_vault_file_id"],
                    new_path=match["new_path"],
                    changed_at="2026-07-21T11:05:00+00:00",
                    vault_id=2,
                )
                for match in catalog.list_rename_candidates(2)
            ]

            for old_relative, file_id in ids.items():
                new_relative = old_relative.replace("docs/", "archive/", 1)
                observed = catalog.get_file_by_path(2, new_relative)
                missing = catalog.get_file_by_path(2, old_relative)
                history = catalog.list_path_history(file_id, vault_id=2)
                self.assertEqual(observed["id"], file_id)
                self.assertIsNone(missing)
                self.assertEqual(
                    [entry["path"] for entry in history],
                    [old_relative, new_relative],
                )
            self.assertEqual(set(renamed_ids), set(ids.values()))

    def test_case_only_and_unicode_renames_preserve_identity(self) -> None:
        cases = (
            ("Reports/Photo.JPG", "reports/photo.jpg"),
            ("album/unicodé café.txt", "album/πλάνο.txt"),
        )
        for old_path, new_path in cases:
            with self.subTest(old_path=old_path, new_path=new_path):
                digest = sha256_hex(f"{old_path}->{new_path}".encode())
                with self.connect() as connection:
                    catalog = ArchiveCatalog(connection)
                    file_id, _ = self.seed_rename(
                        catalog, old_path=old_path, new_path=new_path,
                        old_digest=digest, new_digest=digest,
                    )
                    confirmed = catalog.confirm_file_rename(
                        vault_file_id=file_id,
                        new_path=new_path,
                        changed_at="2026-07-21T11:05:00+00:00",
                        vault_id=2,
                    )
                    history = [
                        entry["path"]
                        for entry in catalog.list_path_history(confirmed, vault_id=2)
                    ]
                    observed = catalog.get_file_by_path(2, new_path)
                self.assertEqual(confirmed, file_id)
                self.assertEqual(observed["id"], file_id)
                self.assertEqual(history, [old_path, new_path])


class RenameSafetyTests(_CatalogFixture):
    def test_rename_keeps_identity_and_keeper(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id, _ = self.seed_rename(
                catalog, old_path="photos/a.jpg", new_path="photos/b.jpg", protected=True,
            )
            keeper = catalog.get_file(old_id, vault_id=2)["current_version"]
            self.assertEqual(keeper["integrity"], "verified")
            catalog.confirm_file_rename(
                vault_file_id=old_id, new_path="photos/b.jpg",
                changed_at="2026-07-21T11:05:00+00:00", vault_id=2,
            )
            renamed = catalog.get_file_by_path(2, "photos/b.jpg")
            versions = connection.execute(
                "SELECT id::text AS id, object_key FROM archive_versions"
            ).fetchall()
        self.assertEqual(renamed["id"], old_id)
        self.assertEqual(renamed["current_version"], keeper)
        # The keeper keeps its exact old key until the cloud rename (#595)
        # verifies the copy; the new path is not protected before that.
        self.assertEqual(versions, [{"id": keeper["id"], "object_key": "vaults/x/photos/a.jpg"}])
        self.assertIsNone(renamed["matched_version_id"])
        self.assertIsNotNone(renamed["cloud_since"])

    def test_identity_that_has_been_in_the_cloud_is_never_consumed(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id, new_id = self.seed_rename(
                catalog, old_path="docs/a.txt", new_path="docs/b.txt",
            )
            # The destination had an upload attempt: its replacement was
            # abandoned, but the identity has been in the cloud.
            version = catalog.begin_replacement(
                new_id, object_key="vaults/x/docs/b.txt", provider_version_id="b1", size=9,
            )
            catalog.abandon_replacement(new_id, archive_version_id=version)
            self.assertEqual(catalog.list_rename_candidates(vault_id=2), [])
            with self.assertRaises(VaultFileNotFound):
                catalog.confirm_file_rename(
                    vault_file_id=old_id, new_path="docs/b.txt",
                    changed_at="2026-07-21T11:05:00+00:00", vault_id=2,
                )
            self.assertEqual(catalog.get_file_by_path(2, "docs/b.txt")["id"], new_id)

    def test_identity_with_any_job_is_never_consumed(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            _, new_id = self.seed_rename(catalog, old_path="a.txt", new_path="b.txt")
            connection.execute(
                "INSERT INTO jobs(vault_id, file_id, action, status, origin) "
                "VALUES (2, %s, 'upload', 'failed', 'automatic')",
                (new_id,),
            )
            self.assertEqual(catalog.list_rename_candidates(vault_id=2), [])

    def test_open_work_on_the_missing_file_blocks_the_rename(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id, _ = self.seed_rename(catalog, old_path="a.txt", new_path="b.txt")
            connection.execute(
                "INSERT INTO jobs(vault_id, file_id, action, status, origin) "
                "VALUES (2, %s, 'recover', 'running', 'manual')",
                (old_id,),
            )
            self.assertEqual(catalog.list_rename_candidates(vault_id=2), [])

    def test_rename_revokes_deletion_consent_and_invalidates_aggregates_atomically(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id, _ = self.seed_rename(
                catalog, old_path="trip/day1/a.jpg", new_path="album/b.jpg", protected=True,
            )
            sibling = catalog.observe_local_copy(
                vault_id=2, path="trip/day1/c.jpg", file_type="regular", size=1, mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            group = uuid.uuid4()
            for file_id in (old_id, sibling):
                connection.execute(
                    "INSERT INTO jobs(vault_id, file_id, action, status, origin, group_id) "
                    "VALUES (2, %s, 'cloud-purge', 'pending_delay', 'manual', %s)",
                    (file_id, group),
                )
            connection.commit()
            connection.execute("DELETE FROM directory_aggregate_dirty")
            self.assertEqual(len(catalog.list_rename_candidates(vault_id=2)), 1)
            catalog.confirm_file_rename(
                vault_file_id=old_id, new_path="album/b.jpg",
                changed_at="2026-07-21T11:05:00+00:00", vault_id=2,
            )
            statuses = connection.execute(
                "SELECT status, finished_at IS NOT NULL AS finished FROM jobs ORDER BY id"
            ).fetchall()
            dirty = {
                row["path"] for row in connection.execute(
                    "SELECT path FROM directory_aggregate_dirty WHERE vault_id=2"
                ).fetchall()
            }
            connection.rollback()
            # Nothing of the rename survives a rollback of its transaction.
            after_rollback = (
                connection.execute(
                    "SELECT status FROM jobs ORDER BY id"
                ).fetchall(),
                catalog.get_file_by_path(2, "trip/day1/a.jpg")["id"],
                connection.execute("SELECT count(*) AS n FROM file_path_history").fetchone()["n"],
            )
        # The whole selection is revoked, not only the renamed file.
        self.assertEqual(statuses, [{"status": "cancelled", "finished": True}] * 2)
        self.assertEqual(dirty, {"trip", "trip/day1", "album"})
        self.assertEqual(
            after_rollback,
            ([{"status": "pending_delay"}] * 2, old_id, 0),
        )

    def test_rename_invalidates_the_previous_observation(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(
                vault_id=2, path="a.txt", file_type="regular", size=3, mtime_ns=7,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            token = catalog.local_copies_to_fingerprint(2)[0]
            catalog.rename_file(
                file_id, new_path="b.txt", changed_at="2026-07-21T10:30:00+00:00", vault_id=2,
            )
            published = catalog.publish_local_fingerprint(
                file_id, generation=token["generation"], plaintext_sha256=DIGEST_A,
                size=3, mtime_ns=7,
            )
            renamed = catalog.get_file_by_path(2, "b.txt")
        self.assertFalse(published)
        self.assertIsNone(renamed["local_copy"]["plaintext_sha256"])

    def test_rename_onto_an_occupied_path_is_refused(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            first = catalog.observe_local_copy(
                vault_id=2, path="a.txt", file_type="regular", size=1, mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.observe_local_copy(
                vault_id=2, path="b.txt", file_type="regular", size=1, mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            with self.assertRaises(psycopg.errors.UniqueViolation):
                catalog.rename_file(
                    first, new_path="b.txt", changed_at="2026-07-21T11:00:00+00:00", vault_id=2,
                )
            connection.rollback()
            with self.assertRaises(VaultFileNotFound):
                catalog.rename_file(
                    str(uuid.uuid4()), new_path="c.txt",
                    changed_at="2026-07-21T11:00:00+00:00", vault_id=2,
                )

    def test_path_history_rows_are_immutable(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(
                vault_id=2, path="a.txt", file_type="regular", size=1, mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.rename_file(
                file_id, new_path="b.txt", changed_at="2026-07-21T11:00:00+00:00", vault_id=2,
            )
            catalog.rename_file(
                file_id, new_path="c.txt", changed_at="2026-07-21T12:00:00+00:00", vault_id=2,
            )
            history = catalog.list_path_history(file_id, vault_id=2)
            with self.assertRaises(psycopg.errors.RestrictViolation):
                connection.execute("UPDATE file_path_history SET path='z.txt'")
        self.assertEqual([entry["path"] for entry in history], ["a.txt", "b.txt", "c.txt"])
        self.assertEqual(
            [entry["valid_to"] is None for entry in history], [False, False, True]
        )
        self.assertEqual(history[0]["valid_to"], history[1]["valid_from"])

    def test_a_vault_that_is_not_active_confirms_nothing(self) -> None:
        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            old_id, _ = self.seed_rename(catalog, old_path="a.txt", new_path="b.txt")
            connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=2")
            with self.assertRaises(VaultFileNotFound):
                catalog.confirm_file_rename(
                    vault_file_id=old_id, new_path="b.txt",
                    changed_at="2026-07-21T11:05:00+00:00", vault_id=2,
                )


class RenameConfirmationRaceTests(_CatalogFixture):
    """#188: competing confirmations and scanner writes, ported from test_rename_audit."""

    def test_issue_188_two_concurrent_confirms_consume_one_candidate_once(self) -> None:
        with self.connect() as connection:
            old_id, _ = self.seed_rename(
                ArchiveCatalog(connection), old_path="concurrent/old.txt",
                new_path="concurrent/new.txt", protected=True,
            )
        start = threading.Barrier(2)

        def confirm_once(_: int) -> str:
            start.wait(timeout=10)
            try:
                with self.connect() as connection:
                    ArchiveCatalog(connection).confirm_file_rename(
                        vault_file_id=old_id,
                        new_path="concurrent/new.txt",
                        changed_at="2026-07-21T11:05:00+00:00",
                        vault_id=2,
                    )
                return "confirmed"
            except VaultFileNotFound:
                return "not_found"

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = sorted(workers.map(confirm_once, range(2)))
        self.assertEqual(outcomes, ["confirmed", "not_found"])

        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            current = catalog.get_file_by_path(2, "concurrent/new.txt")
            history = catalog.list_path_history(old_id, vault_id=2)
            files = connection.execute("SELECT count(*) AS n FROM files").fetchone()["n"]
        self.assertEqual(current["id"], old_id)
        self.assertEqual(
            [entry["path"] for entry in history],
            ["concurrent/old.txt", "concurrent/new.txt"],
        )
        self.assertEqual(files, 1)

    def test_issue_188_scan_state_change_wins_before_confirmation_claim(self) -> None:
        with self.connect() as connection:
            old_id, new_id = self.seed_rename(
                ArchiveCatalog(connection), old_path="scan-race/old.txt",
                new_path="scan-race/new.txt",
            )
        scanner_reserved = threading.Event()
        release_scanner = threading.Event()
        scanner_done = threading.Event()
        scanner_errors: list[BaseException] = []

        def scanner_state_change() -> None:
            try:
                with self.connect() as connection:
                    connection.execute("SELECT id FROM vaults WHERE id=2 FOR UPDATE")
                    connection.execute(
                        """
                        UPDATE files
                        SET local_presence='present', local_sha256=%s,
                            local_observed_at=%s, generation=generation + 1
                        WHERE id=%s
                        """,
                        (bytes.fromhex("b" * 64), "2026-07-21T12:00:00+00:00", old_id),
                    )
                    scanner_reserved.set()
                    if not release_scanner.wait(timeout=10):
                        raise TimeoutError("test did not release scanner transaction")
            except BaseException as exc:  # Preserve failures from the worker thread.
                scanner_errors.append(exc)
            finally:
                scanner_done.set()

        scanner = threading.Thread(target=scanner_state_change)
        scanner.start()
        self.addCleanup(release_scanner.set)
        self.addCleanup(scanner.join)
        self.assertTrue(scanner_reserved.wait(timeout=10))

        reservation_attempted = threading.Event()
        original_reservation = ArchiveCatalog._reserve_rename_confirmation

        def note_reservation(catalog: ArchiveCatalog, vault_id: int) -> None:
            reservation_attempted.set()
            original_reservation(catalog, vault_id)

        confirmation_outcome: list[str] = []

        def confirm_while_scanner_holds_write_lock() -> None:
            try:
                with self.connect() as connection:
                    ArchiveCatalog(connection).confirm_file_rename(
                        vault_file_id=old_id,
                        new_path="scan-race/new.txt",
                        changed_at="2026-07-21T12:01:00+00:00",
                        vault_id=2,
                    )
            except VaultFileNotFound:
                confirmation_outcome.append("not_found")
            else:
                confirmation_outcome.append("confirmed")

        confirmer = threading.Thread(target=confirm_while_scanner_holds_write_lock)
        try:
            with patch.object(
                ArchiveCatalog,
                "_reserve_rename_confirmation",
                autospec=True,
                side_effect=note_reservation,
            ):
                confirmer.start()
                self.assertTrue(reservation_attempted.wait(timeout=10))
                release_scanner.set()
                self.assertTrue(scanner_done.wait(timeout=10))
                confirmer.join(timeout=10)
        finally:
            release_scanner.set()
            scanner.join(timeout=10)
            confirmer.join(timeout=10)

        self.assertFalse(scanner.is_alive())
        self.assertFalse(confirmer.is_alive())
        self.assertEqual(scanner_errors, [])
        self.assertEqual(confirmation_outcome, ["not_found"])

        with self.connect() as connection:
            catalog = ArchiveCatalog(connection)
            source = catalog.get_file(old_id, vault_id=2)
            provisional = catalog.get_file(new_id, vault_id=2)
            history = connection.execute(
                "SELECT count(*) AS n FROM file_path_history"
            ).fetchone()["n"]
        self.assertEqual(source["path"], "scan-race/old.txt")
        self.assertEqual(source["local_copy"]["presence"], "present")
        self.assertEqual(source["local_copy"]["plaintext_sha256"], "b" * 64)
        self.assertEqual(provisional["path"], "scan-race/new.txt")
        self.assertEqual(history, 0)


if __name__ == "__main__":
    unittest.main()
