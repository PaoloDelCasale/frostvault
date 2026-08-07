"""Durable catalog revision/event persistence seams."""
from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import SQLiteConnection
from app.services.catalog_events import CatalogEventStore
from tests.test_database import run_alembic


class CatalogStatePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "catalog-state.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                """
            )

    def test_catalog_mutation_and_publication_commit_as_one_transaction(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            published = CatalogEventStore(connection).mutate_and_publish(
                2,
                lambda conn: conn.execute(
                    "UPDATE vaults SET name='Changed' WHERE id=2"
                ),
                domain="files",
                scope="root",
                payload={"operation": "observe"},
                created_at="2026-08-01T10:00:00+00:00",
            )

        with SQLiteConnection(str(self.path)) as connection:
            vault = connection.execute(
                "SELECT name FROM vaults WHERE id=2"
            ).fetchone()
            state = connection.execute(
                "SELECT revision, retained_from_revision "
                "FROM vault_catalog_revisions WHERE vault_id=2"
            ).fetchone()
            event = connection.execute(
                "SELECT vault_id, revision, domain, scope, payload_json "
                "FROM catalog_events WHERE vault_id=2"
            ).fetchone()

        self.assertEqual(vault["name"], "Changed")
        self.assertEqual(published.event["revision"], 1)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["retained_from_revision"], 1)
        self.assertEqual(event["revision"], 1)
        self.assertEqual(event["domain"], "files")
        self.assertEqual(event["scope"], "root")
        self.assertIn('"operation":"observe"', event["payload_json"])

    def test_outer_transaction_can_roll_back_a_successful_publication(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "outer failure"):
            with SQLiteConnection(str(self.path)) as connection:
                CatalogEventStore(connection).mutate_and_publish(
                    2,
                    lambda conn: conn.execute(
                        "UPDATE vaults SET name='Outer failure' WHERE id=2"
                    ),
                    domain="files",
                    scope="root",
                    payload={},
                )
                raise RuntimeError("outer failure")

        with SQLiteConnection(str(self.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT name FROM vaults WHERE id=2").fetchone()["name"],
                "Docs",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM catalog_events"
                ).fetchone()["total"],
                0,
            )

    def test_failed_canonical_mutation_rolls_back_revision_and_event(self) -> None:
        def fail(connection: SQLiteConnection) -> None:
            connection.execute("UPDATE vaults SET name='Not durable' WHERE id=2")
            raise RuntimeError("simulated catalog failure")

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            with SQLiteConnection(str(self.path)) as connection:
                CatalogEventStore(connection).mutate_and_publish(
                    2,
                    fail,
                    domain="files",
                    scope="root",
                    payload={"operation": "failed"},
                )

        with SQLiteConnection(str(self.path)) as connection:
            vault = connection.execute(
                "SELECT name FROM vaults WHERE id=2"
            ).fetchone()
            state_count = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_catalog_revisions"
            ).fetchone()["total"]
            event_count = connection.execute(
                "SELECT COUNT(*) AS total FROM catalog_events"
            ).fetchone()["total"]

        self.assertEqual(vault["name"], "Docs")
        self.assertEqual(state_count, 0)
        self.assertEqual(event_count, 0)

    def test_concurrent_mutations_serialize_per_vault_revisions(self) -> None:
        def publish(index: int) -> int:
            with SQLiteConnection(str(self.path)) as connection:
                publication = CatalogEventStore(connection).mutate_and_publish(
                    2,
                    lambda _connection: index,
                    domain="files",
                    scope="root",
                    payload={"index": index},
                )
                return publication.event["revision"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            revisions = list(executor.map(publish, range(8)))

        self.assertEqual(sorted(revisions), list(range(1, 9)))
        with SQLiteConnection(str(self.path)) as connection:
            self.assertEqual(CatalogEventStore(connection).current_revision(2), 8)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM catalog_events WHERE vault_id=2"
                ).fetchone()["total"],
                8,
            )

    def test_event_retention_reports_a_gap_without_reusing_revisions(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            store = CatalogEventStore(connection)
            for revision in range(3):
                store.append_event(
                    vault_id=2,
                    domain="files",
                    scope=f"batch-{revision}",
                    payload={},
                )
            pruned = store.prune_events(vault_id=2, retain_from_revision=3)
            page = store.read_events(vault_id=2, after_revision=0)
            contiguous = store.read_events(vault_id=2, after_revision=2)

        self.assertEqual(pruned["deleted"], 2)
        self.assertEqual(pruned["retained_from_revision"], 3)
        self.assertTrue(page["has_gap"])
        self.assertEqual([event["revision"] for event in page["events"]], [3])
        self.assertFalse(contiguous["has_gap"])
        self.assertEqual(contiguous["current_revision"], 3)

    def test_invalid_publication_is_validated_before_canonical_mutation(self) -> None:
        called = False

        def mutation(_connection: SQLiteConnection) -> None:
            nonlocal called
            called = True

        with SQLiteConnection(str(self.path)) as connection:
            with self.assertRaises(ValueError):
                CatalogEventStore(connection).mutate_and_publish(
                    2,
                    mutation,
                    domain="invalid domain",
                )

        self.assertFalse(called)

    def test_event_domain_and_payload_are_bounded_before_any_write(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            store = CatalogEventStore(connection)
            with self.assertRaises(ValueError):
                store.append_event(vault_id=2, domain="not a domain")
            with self.assertRaises(ValueError):
                store.append_event(
                    vault_id=2,
                    domain="files",
                    payload={"large": "x" * 5000},
                )
            self.assertEqual(store.current_revision(2), 0)



    def test_downgrade_refuses_to_drop_durable_catalog_history(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            CatalogEventStore(connection).append_event(
                vault_id=2,
                domain="files",
                scope="root",
                payload={},
            )

        downgraded = run_alembic(
            self.path,
            revision="0035_upload_verification_digest",
            command="downgrade",
        )
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("catalog event", downgraded.stderr)

        with SQLiteConnection(str(self.path)) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()["version_num"]
        self.assertEqual(revision, "0036_catalog_events")


if __name__ == "__main__":
    unittest.main()
