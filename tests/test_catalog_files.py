"""One row per Vault File: Local Copy, observation token and explicit states (#587).

Seams: ``ArchiveCatalog`` observation, fingerprint publication and replacement
methods, and the ``files`` / ``archive_versions`` constraints of the
``0001_baseline`` schema (ADR-0035) on a disposable PostgreSQL database.
"""
from __future__ import annotations

import inspect
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg

from app.catalog import ArchiveCatalog, ReplacementInProgress, StaleReplacement
from tests.catalog_support import fingerprint, protect, seed_vault, sha256_hex
from tests.postgres import PostgresConnection, run_alembic

DIGEST_A = sha256_hex(b"content-a")
DIGEST_B = sha256_hex(b"content-b")
T0 = "2026-07-21T10:00:00+00:00"
T1 = "2026-07-21T11:00:00+00:00"
T2 = "2026-07-21T12:00:00+00:00"


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "files.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.connection = PostgresConnection(str(self.database_path))
        self.addCleanup(self.connection.close)
        seed_vault(self.connection)
        self.catalog = ArchiveCatalog(self.connection)

    def observe(self, path: str = "a.txt", *, size: int = 9, mtime_ns: int = 100,
                at: str = T0, **extra) -> str:
        return self.catalog.observe_local_copy(
            vault_id=2, path=path, file_type=extra.pop("file_type", "regular"),
            size=size, mtime_ns=mtime_ns, observed_at=at, **extra,
        )

    def file(self, path: str = "a.txt") -> dict:
        return self.catalog.get_file_by_path(2, path)

    def rejects(self, statement: str, params=(), error=psycopg.errors.IntegrityError) -> None:
        self.connection.execute("SAVEPOINT rejected")
        with self.assertRaises(error):
            self.connection.execute(statement, params)
        self.connection.execute("ROLLBACK TO SAVEPOINT rejected")


class LocalCopyTests(_Fixture):
    def test_native_types_keep_nanosecond_mtimes_exact(self) -> None:
        types = {
            row["column_name"]: row["data_type"]
            for row in self.connection.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name IN ('files', 'file_path_history') "
                "AND NOT (table_name='file_path_history' AND column_name='id')"
            ).fetchall()
        }
        self.assertEqual(types["id"], "uuid")
        self.assertEqual(types["local_mtime_ns"], "bigint")
        self.assertEqual(types["local_sha256"], "bytea")
        for column in ("path_since", "local_observed_at", "local_seen_at",
                       "pending_since", "cloud_since", "valid_from", "valid_to"):
            self.assertEqual(types[column], "timestamp with time zone", column)
        for column in ("matched_version_id", "held_version_id", "current_version_id",
                       "file_id"):
            self.assertEqual(types[column], "uuid", column)
        for mtime in (1_750_000_000_123_456_789, -86_400_000_000_001, 0):
            path = f"m{mtime}.bin"
            self.observe(path, mtime_ns=mtime)
            self.assertEqual(self.file(path)["local_copy"]["mtime_ns"], mtime)

    def test_absence_is_distinct_from_missing(self) -> None:
        # A File can exist without any Local Copy ever observed.
        self.connection.execute(
            "INSERT INTO files(vault_id, path, path_since) VALUES (2, 'cloud.txt', now())"
        )
        self.assertIsNone(self.file("cloud.txt")["local_copy"])
        self.rejects(
            "INSERT INTO files(vault_id, path, path_since, local_size) VALUES (2, 'x', now(), 1)"
        )
        self.rejects(
            "INSERT INTO files(vault_id, path, path_since, local_presence, local_file_type, "
            "local_observed_at) VALUES (2, 'y', now(), 'present', 'symlink', now())"
        )
        file_id = self.observe()
        self.assertTrue(fingerprint(self.catalog, 2, "a.txt", DIGEST_A))
        self.catalog.mark_local_copy_missing(file_id, observed_at=T1)
        missing = self.file()["local_copy"]
        self.assertEqual(missing["presence"], "missing")
        # Missing keeps the last known content fingerprint.
        self.assertEqual(missing["plaintext_sha256"], DIGEST_A)
        # Absence has nothing to mark missing.
        self.catalog.mark_local_copy_missing(self.file("cloud.txt")["id"], observed_at=T1)
        self.assertIsNone(self.file("cloud.txt")["local_copy"])

    def test_unsupported_entries_are_observed_without_fingerprint(self) -> None:
        self.observe("link", file_type="symlink", size=0)
        self.assertEqual(self.file("link")["local_copy"]["presence"], "unsupported")
        self.assertEqual(self.catalog.local_copies_to_fingerprint(2), [])

    def test_every_new_observation_invalidates_the_previous_one(self) -> None:
        file_id = self.observe()
        before_hash = self.catalog.local_copies_to_fingerprint(2)[0]
        # A scan re-observes the same stat while the hash runs.
        self.observe(at=T1)
        after = self.file()
        self.assertGreater(after["generation"], before_hash["generation"])
        self.assertFalse(self.catalog.publish_local_fingerprint(
            file_id, generation=before_hash["generation"], plaintext_sha256=DIGEST_A,
            size=9, mtime_ns=100,
        ))
        self.assertIsNone(self.file()["local_copy"]["plaintext_sha256"])
        # A hash taken on the current observation publishes.
        self.assertTrue(self.catalog.publish_local_fingerprint(
            file_id, generation=after["generation"], plaintext_sha256=DIGEST_A,
            size=9, mtime_ns=100,
        ))

    def test_publication_requires_the_stat_the_hash_saw(self) -> None:
        file_id = self.observe()
        token = self.catalog.local_copies_to_fingerprint(2)[0]
        # The file changed under the hash: its stable stat differs from the
        # observation, so the digest belongs to no recorded observation.
        self.assertFalse(self.catalog.publish_local_fingerprint(
            file_id, generation=token["generation"], plaintext_sha256=DIGEST_A,
            size=10, mtime_ns=101,
        ))
        self.assertTrue(self.catalog.publish_local_fingerprint(
            file_id, generation=token["generation"], plaintext_sha256=DIGEST_A,
            size=9, mtime_ns=100,
        ))
        with self.assertRaises(ValueError):
            self.catalog.publish_local_fingerprint(
                file_id, generation=token["generation"], plaintext_sha256="abc",
                size=9, mtime_ns=100,
            )

    def test_unchanged_stat_keeps_fingerprint_and_match_changed_content_does_not(self) -> None:
        self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        protect(self.catalog, 2, "a.txt", DIGEST_A)
        self.assertIsNotNone(self.file()["matched_version_id"])
        self.observe(at=T1)
        unchanged = self.file()
        self.assertEqual(unchanged["local_copy"]["plaintext_sha256"], DIGEST_A)
        self.assertEqual(unchanged["matched_version_id"], unchanged["current_version"]["id"])
        # A watcher content event forgets the fingerprint even with the same stat.
        self.observe(at=T2, invalidate_content=True)
        invalidated = self.file()
        self.assertIsNone(invalidated["local_copy"]["plaintext_sha256"])
        self.assertIsNone(invalidated["matched_version_id"])
        self.assertEqual(invalidated["local_copy"]["content_observed_at"],
                         invalidated["local_copy"]["observed_at"])
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        self.observe(size=10, at="2026-07-21T13:00:00+00:00")
        changed = self.file()
        self.assertIsNone(changed["local_copy"]["plaintext_sha256"])
        self.assertIsNone(changed["matched_version_id"])
        self.assertIsNotNone(changed["current_version"])

    def test_unseen_local_copies_become_missing_only_for_this_scan(self) -> None:
        scan = "2026-07-21T10:00:00+00:00"
        self.catalog.observe_local_copies_batch(
            vault_id=2, entries=[("seen.txt", "regular", 1, 1, scan)], seen_at=scan,
        )
        self.observe("unseen.txt", at="2026-07-21T09:00:00+00:00")
        # A watcher observed this one after the scan started.
        self.observe("newer.txt", at="2026-07-21T10:30:00+00:00")
        generation = self.file("unseen.txt")["generation"]
        self.catalog.mark_unseen_local_copies_missing(
            vault_id=2, seen_at=scan, observed_at=T1, scan_started_at=scan,
        )
        self.assertEqual(self.file("seen.txt")["local_copy"]["presence"], "present")
        self.assertEqual(self.file("newer.txt")["local_copy"]["presence"], "present")
        self.assertEqual(self.file("unseen.txt")["local_copy"]["presence"], "missing")
        self.assertGreater(self.file("unseen.txt")["generation"], generation)
        status = self.connection.execute(
            "SELECT status FROM directory_aggregate_status WHERE vault_id=2"
        ).fetchone()
        self.assertEqual(status["status"], "rebuild_required")

    def test_a_directory_removal_marks_every_descendant_missing(self) -> None:
        for path in ("dir/a.txt", "dir/sub/b.txt", "dir_x/c.txt", "dir"):
            self.observe(path)
        self.catalog.mark_local_path_missing(vault_id=2, path="dir", observed_at=T1)
        presences = {
            path: self.file(path)["local_copy"]["presence"]
            for path in ("dir/a.txt", "dir/sub/b.txt", "dir_x/c.txt", "dir")
        }
        self.assertEqual(presences, {
            "dir/a.txt": "missing", "dir/sub/b.txt": "missing",
            "dir_x/c.txt": "present", "dir": "missing",
        })

    def test_reappearance_revokes_the_whole_pending_deletion(self) -> None:
        first = self.observe("a.txt")
        second = self.observe("b.txt")
        group = uuid.uuid4()
        self.connection.execute("INSERT INTO job_groups(id, vault_id) VALUES (%s, 2)", (group,))
        for file_id in (first, second):
            self.connection.execute(
                "INSERT INTO jobs(vault_id, file_id, action, status, origin, group_id, run_after) "
                "VALUES (2, %s, 'cloud-purge', 'pending_delay', 'manual', %s, now() + interval '7 days')",
                (file_id, group),
            )
        # One active operation per File: other work runs on another File.
        third = self.observe("c.txt")
        self.connection.execute(
            "INSERT INTO jobs(vault_id, file_id, action, status, origin) "
            "VALUES (2, %s, 'upload', 'queued', 'automatic')", (third,),
        )
        self.catalog.mark_local_copy_missing(first, observed_at=T1)
        self.observe("a.txt", at=T2)
        statuses = [row["status"] for row in self.connection.execute(
            "SELECT status FROM jobs ORDER BY id"
        ).fetchall()]
        self.assertEqual(statuses, ["cancelled", "cancelled", "queued"])


class ExplicitStateTests(_Fixture):
    def test_current_changes_only_by_promoting_the_verified_replacement(self) -> None:
        file_id = self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        first = self.catalog.begin_replacement(
            file_id, object_key="vaults/x/a.txt", provider_version_id="v1", size=9,
        )
        with self.assertRaises(ReplacementInProgress):
            self.catalog.begin_replacement(
                file_id, object_key="vaults/x/a.txt", provider_version_id="v2", size=9,
            )
        # Only one replacement row was written.
        self.assertEqual(self.connection.execute(
            "SELECT count(*) AS n FROM archive_versions"
        ).fetchone()["n"], 1)
        # Current cannot be pointed at an unverified version, nor set directly.
        self.rejects(
            "UPDATE files SET current_version_id=replacing_version_id, "
            "replacing_version_id=NULL WHERE id=%s", (file_id,),
            error=psycopg.errors.RestrictViolation,
        )
        self.rejects(
            "UPDATE files SET current_version_id=%s WHERE id=%s", (first, file_id),
            error=psycopg.errors.RestrictViolation,
        )
        with self.assertRaises(StaleReplacement):
            self.catalog.promote_replacement(
                file_id, archive_version_id=str(uuid.uuid4()),
                plaintext_sha256=DIGEST_A, verified_at=T1,
            )
        matched = self.catalog.promote_replacement(
            file_id, archive_version_id=first, plaintext_sha256=DIGEST_A, verified_at=T1,
            local_generation=self.file()["generation"],
        )
        self.assertTrue(matched)
        state = self.file()
        self.assertEqual(state["current_version"]["id"], first)
        self.assertEqual(state["current_version"]["integrity"], "verified")
        self.assertIsNone(state["replacing_version"])
        self.assertEqual(state["matched_version_id"], first)

        # A second content: the replacement waits; Current stays the keeper.
        self.observe(size=10, at=T2)
        self.assertIsNone(self.file()["matched_version_id"])
        fingerprint(self.catalog, 2, "a.txt", DIGEST_B)
        second = self.catalog.begin_replacement(
            file_id, object_key="vaults/x/a.txt", provider_version_id="v2", size=10,
        )
        state = self.file()
        self.assertEqual((state["current_version"]["id"], state["replacing_version"]["id"]),
                         (first, second))
        self.catalog.promote_replacement(
            file_id, archive_version_id=second, plaintext_sha256=DIGEST_B,
            verified_at="2026-07-21T13:00:00+00:00", local_generation=state["generation"],
        )
        state = self.file()
        self.assertEqual(state["current_version"]["id"], second)
        # The superseded version is unreferenced, awaiting its exact cleanup.
        superseded = self.connection.execute(
            """
            SELECT id::text AS id FROM archive_versions av
            WHERE NOT EXISTS (
                SELECT 1 FROM files f
                WHERE av.id IN (f.current_version_id, f.replacing_version_id)
            )
            """
        ).fetchall()
        self.assertEqual(superseded, [{"id": first}])

    def test_a_stale_observation_is_never_matched_at_promotion(self) -> None:
        file_id = self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        generation = self.file()["generation"]
        version = self.catalog.begin_replacement(
            file_id, object_key="vaults/x/a.txt", provider_version_id="v1", size=9,
        )
        self.observe(at=T1)  # same stat, newer observation
        matched = self.catalog.promote_replacement(
            file_id, archive_version_id=version, plaintext_sha256=DIGEST_A,
            verified_at=T1, local_generation=generation,
        )
        self.assertFalse(matched)
        state = self.file()
        self.assertEqual(state["current_version"]["id"], version)
        self.assertIsNone(state["matched_version_id"])

    def test_a_failed_replacement_preserves_current(self) -> None:
        file_id = self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        keeper = protect(self.catalog, 2, "a.txt", DIGEST_A)
        replacement = self.catalog.begin_replacement(
            file_id, object_key="vaults/x/a.txt", provider_version_id="v2", size=9,
        )
        self.catalog.abandon_replacement(
            file_id, archive_version_id=replacement, mismatch_sha256=DIGEST_B, checked_at=T1,
        )
        state = self.file()
        self.assertEqual(state["current_version"]["id"], keeper)
        self.assertIsNone(state["replacing_version"])
        integrity = self.connection.execute(
            "SELECT integrity FROM archive_versions WHERE id=%s", (replacement,)
        ).fetchone()["integrity"]
        self.assertEqual(integrity, "mismatch")
        with self.assertRaises(StaleReplacement):
            self.catalog.abandon_replacement(file_id, archive_version_id=replacement)

    def test_only_current_can_be_matched_or_held(self) -> None:
        file_id = self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        keeper = protect(self.catalog, 2, "a.txt", DIGEST_A)
        replacement = self.catalog.begin_replacement(
            file_id, object_key="vaults/x/a.txt", provider_version_id="v2", size=9,
        )
        self.rejects("UPDATE files SET matched_version_id=%s WHERE id=%s",
                     (replacement, file_id))
        self.rejects("UPDATE files SET held_version_id=%s WHERE id=%s",
                     (replacement, file_id))
        # Held content is never matched at the same time.
        self.rejects("UPDATE files SET held_version_id=%s WHERE id=%s", (keeper, file_id))
        self.rejects("UPDATE files SET local_sha256=NULL WHERE id=%s", (file_id,))

    def test_content_reappearing_at_a_cloud_only_path_is_held_until_identical(self) -> None:
        file_id = self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        keeper = protect(self.catalog, 2, "a.txt", DIGEST_A)
        self.catalog.mark_local_copy_missing(file_id, observed_at=T1)
        # Same stat, but reappeared bytes do not inherit the old fingerprint.
        self.observe(at=T2)
        held = self.file()
        self.assertEqual(held["held_version_id"], keeper)
        self.assertIsNone(held["matched_version_id"])
        self.assertIsNone(held["local_copy"]["plaintext_sha256"])
        # Different content stays held.
        self.assertTrue(fingerprint(self.catalog, 2, "a.txt", DIGEST_B))
        self.assertEqual(self.file()["held_version_id"], keeper)
        # Identical content (after a new observation) becomes the Local Copy.
        self.observe(at="2026-07-21T13:00:00+00:00", invalidate_content=True)
        self.assertTrue(fingerprint(self.catalog, 2, "a.txt", DIGEST_A))
        settled = self.file()
        self.assertIsNone(settled["held_version_id"])
        self.assertEqual(settled["matched_version_id"], keeper)

    def test_a_completed_recovery_holds_nothing_back(self) -> None:
        file_id = self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        protect(self.catalog, 2, "a.txt", DIGEST_A)
        self.catalog.mark_local_copy_missing(file_id, observed_at=T1)
        self.observe(at=T2, recovered_at=T2)
        state = self.file()
        self.assertIsNone(state["held_version_id"])
        self.assertEqual(state["local_copy"]["recovered_at"].isoformat(), T2)
        # Nor does a later ordinary observation forget the retention instant.
        self.observe(at="2026-07-21T13:00:00+00:00")
        self.assertEqual(self.file()["local_copy"]["recovered_at"].isoformat(), T2)

    def test_replaced_local_copy_is_held_against_current(self) -> None:
        file_id = self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        keeper = protect(self.catalog, 2, "a.txt", DIGEST_A)
        generation = self.file()["generation"]
        self.catalog.observe_replaced_local_copy(
            file_id, file_type="regular", size=12, mtime_ns=5, observed_at=T1,
        )
        state = self.file()
        self.assertEqual(state["held_version_id"], keeper)
        self.assertIsNone(state["local_copy"]["plaintext_sha256"])
        self.assertGreater(state["generation"], generation)

    def test_pending_timers_are_continuous_and_explicit(self) -> None:
        first = self.observe("a.txt")
        second = self.observe("b.txt")
        durable = self.catalog.record_pending_files(2, {first: T0, second: T0})
        self.assertEqual({key: value.isoformat() for key, value in durable.items()},
                         {first: T0, second: T0})
        # A later observation of the same pending file never resets its timer.
        durable = self.catalog.record_pending_files(2, {first: T2})
        self.assertEqual(durable[first].isoformat(), T0)
        self.assertIsNone(self.file("b.txt")["pending_since"])
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        protect(self.catalog, 2, "a.txt", DIGEST_A)
        # Becoming protected ends the pending state in the same write.
        self.assertIsNone(self.file("a.txt")["pending_since"])

    def test_unchanged_pending_timers_are_never_rewritten(self) -> None:
        """A long queue costs no write per call (#622): only starts and ends."""
        first = self.observe("a.txt")
        second = self.observe("b.txt")
        self.catalog.record_pending_files(2, {first: T0, second: T0})
        self.connection.commit()
        before = self.connection.execute(
            "SELECT id, xmin::text AS xmin FROM files WHERE vault_id=2 ORDER BY id").fetchall()
        statements = []
        execute = psycopg.Connection.execute

        def counting(connection, sql, *args, **kwargs):
            statements.append(" ".join(str(sql).split()))
            return execute(connection, sql, *args, **kwargs)
        with patch.object(psycopg.Connection, "execute", counting):
            durable = self.catalog.record_pending_files(2, {first: T2, second: T2})
        self.connection.commit()
        self.assertEqual({key: value.isoformat() for key, value in durable.items()},
                         {first: T0, second: T0})
        self.assertEqual([sql.split()[0] for sql in statements], ["SELECT"])
        after = self.connection.execute(
            "SELECT id, xmin::text AS xmin FROM files WHERE vault_id=2 ORDER BY id").fetchall()
        self.assertEqual(before, after, "no row version was written")

    def test_reconciliation_block_is_explicit(self) -> None:
        file_id = self.observe()
        self.catalog.block_reconciliation(file_id, reason="ambiguous_send", at=T1)
        self.assertEqual(self.file()["reconciliation_block"]["reason"], "ambiguous_send")
        self.rejects("UPDATE files SET reconciliation_blocked_at=NULL WHERE id=%s", (file_id,))
        self.catalog.clear_reconciliation_block(file_id)
        self.assertIsNone(self.file()["reconciliation_block"])

    def test_identity_and_cloud_history_are_permanent(self) -> None:
        file_id = self.observe()
        fingerprint(self.catalog, 2, "a.txt", DIGEST_A)
        version = protect(self.catalog, 2, "a.txt", DIGEST_A)
        self.assertIsNotNone(self.file()["cloud_since"])
        self.rejects("UPDATE files SET cloud_since=NULL WHERE id=%s", (file_id,),
                     error=psycopg.errors.RestrictViolation)
        seed_vault(self.connection, vault_id=3, source_root="/other")
        self.rejects("UPDATE files SET vault_id=3 WHERE id=%s", (file_id,))
        for column, value in (("object_key", "vaults/x/b.txt"),
                              ("provider_version_id", "other")):
            self.rejects(f"UPDATE archive_versions SET {column}=%s WHERE id=%s",
                         (value, version), error=psycopg.errors.RestrictViolation)
        self.rejects(
            "UPDATE archive_versions SET plaintext_sha256=NULL WHERE id=%s", (version,)
        )

    def test_no_current_version_is_derived_by_ordering(self) -> None:
        methods = (
            "_observe", "observe_replaced_local_copy", "publish_local_fingerprint",
            "begin_replacement", "promote_replacement", "abandon_replacement",
            "mark_unseen_archive_versions_missing", "get_file", "get_file_by_path",
            "list_rename_candidates", "confirm_file_rename", "rename_file",
        )
        import app.catalog as catalog_module

        sources = [inspect.getsource(getattr(ArchiveCatalog, name)) for name in methods]
        sources.append(inspect.getsource(catalog_module._only_cloud_copy_sql))
        sources.append(catalog_module._FILE_JOINS_SQL)
        for source in sources:
            self.assertNotIn("version_number", source)
            self.assertNotIn("DESC LIMIT 1", source.replace("\n", " "))
            for table in ("vault_files", "local_copies", "file_paths", "protection_pending",
                          "snapshot_reconciliation_blocks"):
                self.assertNotIn(table, source)


class AggregateInvalidationTests(_Fixture):
    """Dirty marks ported from ``test_aggregate_deadlocks`` (the flush belongs to #593)."""

    def dirty(self) -> list[str]:
        return [row["path"] for row in self.connection.execute(
            "SELECT DISTINCT path FROM directory_aggregate_dirty ORDER BY path"
        ).fetchall()]

    def test_catalog_writes_mark_ancestors_in_their_own_transaction(self) -> None:
        self.connection.commit()
        self.observe("0/a/b.bin")
        self.assertEqual(self.dirty(), ["0", "0/a"])
        self.connection.rollback()
        self.assertEqual(self.dirty(), [])
        self.assertIsNone(self.file("0/a/b.bin"))

    def test_same_connection_rollback_does_not_suppress_durable_mark(self) -> None:
        from app.services import directory_aggregates as aggregates

        self.connection.commit()
        aggregates.mark_path_dirty(self.connection, 2, "0/a.bin")
        self.connection.rollback()
        aggregates.mark_path_dirty(self.connection, 2, "0/b.bin")
        self.connection.commit()
        self.assertEqual(self.dirty(), ["0"])

    def test_writer_does_not_wait_for_another_transaction_marking_the_same_path(self) -> None:
        from app.services import directory_aggregates as aggregates

        self.connection.commit()
        aggregates.mark_path_dirty(self.connection, 2, "0/a.bin")
        # The first transaction is still open and owns its mark row.
        with PostgresConnection(str(self.database_path)) as writer:
            writer.execute("SET LOCAL lock_timeout='250ms'")
            aggregates.mark_path_dirty(writer, 2, "0/b.bin")
        self.connection.commit()
        marks = self.connection.execute(
            "SELECT count(*) AS n FROM directory_aggregate_dirty WHERE path='0'"
        ).fetchone()["n"]
        self.assertEqual(marks, 2)


if __name__ == "__main__":
    unittest.main()
