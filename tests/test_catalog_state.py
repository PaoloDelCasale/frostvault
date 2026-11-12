"""Durable catalog revision/event persistence seams."""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


from app.database import HEAD_SCHEMA_REVISION
from app.services.catalog_events import CatalogEventStore
from tests.test_database import run_alembic


class CatalogStatePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "catalog-state.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs')
                """
            )

    def test_catalog_mutation_and_publication_commit_as_one_transaction(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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

        with PostgresConnection(str(self.path)) as connection:
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
            with PostgresConnection(str(self.path)) as connection:
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

        with PostgresConnection(str(self.path)) as connection:
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

    def test_failed_helper_preserves_outer_work_and_connection_usability(self) -> None:
        def fail(connection):
            connection.execute("UPDATE vaults SET name='Helper mutation' WHERE id=2")
            raise RuntimeError("helper failure")

        with PostgresConnection(self.path) as connection:
            connection.execute("UPDATE vaults SET name='Outer mutation' WHERE id=2")
            with self.assertRaisesRegex(RuntimeError, "helper failure"):
                CatalogEventStore(connection).mutate_and_publish(
                    2, fail, domain="files", scope="root", payload={},
                )
            self.assertEqual(connection.execute(
                "SELECT name FROM vaults WHERE id=2"
            ).fetchone()["name"], "Outer mutation")
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT name FROM vaults WHERE id=2"
            ).fetchone()["name"], "Outer mutation")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS n FROM catalog_events"
            ).fetchone()["n"], 0)

    def test_autocommit_caller_gets_one_atomic_publication(self) -> None:
        with PostgresConnection(self.path) as connection:
            connection.autocommit = True
            CatalogEventStore(connection).mutate_and_publish(
                2, lambda conn: conn.execute("UPDATE vaults SET name='Autocommit' WHERE id=2"),
                domain="files", scope="root", payload={},
            )
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT name FROM vaults WHERE id=2"
            ).fetchone()["name"], "Autocommit")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS n FROM catalog_events"
            ).fetchone()["n"], 1)

    def test_failed_canonical_mutation_rolls_back_revision_and_event(self) -> None:
        def fail(connection: PostgresConnection) -> None:
            connection.execute("UPDATE vaults SET name='Not durable' WHERE id=2")
            raise RuntimeError("simulated catalog failure")

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            with PostgresConnection(str(self.path)) as connection:
                CatalogEventStore(connection).mutate_and_publish(
                    2,
                    fail,
                    domain="files",
                    scope="root",
                    payload={"operation": "failed"},
                )

        with PostgresConnection(str(self.path)) as connection:
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
            with PostgresConnection(str(self.path)) as connection:
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
        with PostgresConnection(str(self.path)) as connection:
            self.assertEqual(CatalogEventStore(connection).current_revision(2), 8)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM catalog_events WHERE vault_id=2"
                ).fetchone()["total"],
                8,
            )

    def test_event_retention_reports_a_gap_without_reusing_revisions(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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

        def mutation(_connection: PostgresConnection) -> None:
            nonlocal called
            called = True

        with PostgresConnection(str(self.path)) as connection:
            with self.assertRaises(ValueError):
                CatalogEventStore(connection).mutate_and_publish(
                    2,
                    mutation,
                    domain="invalid domain",
                )

        self.assertFalse(called)

    def test_event_domain_and_payload_are_bounded_before_any_write(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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



    def test_baseline_downgrade_refusal_preserves_durable_history_and_schema(self) -> None:
        tables = ('vaults', 'vault_catalog_revisions', 'catalog_events')

        def snapshot(connection):
            state = {table: connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                     for table in tables}
            state['columns'] = connection.execute("""
                SELECT table_name,column_name,data_type,column_default,is_nullable
                FROM information_schema.columns WHERE table_schema=current_schema()
                ORDER BY table_name,ordinal_position
            """).fetchall()
            state['indexes'] = connection.execute("""
                SELECT indexname,indexdef FROM pg_indexes
                WHERE schemaname=current_schema() ORDER BY indexname
            """).fetchall()
            state['revision'] = connection.execute('SELECT version_num FROM alembic_version').fetchone()
            return state

        with PostgresConnection(self.path) as connection:
            CatalogEventStore(connection).append_event(
                vault_id=2, domain='files', scope='root', payload={'operation': 'keep-history'},
            )
            before = snapshot(connection)
        downgraded = run_alembic(self.path, '0035_upload_verification_digest', command='downgrade')
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn('incompatible Current Snapshot baseline cannot be downgraded', downgraded.stderr)
        # Check before reentry too: an upgrade must not mask failed-DDL damage.
        for stage in ('after_refusal', 'after_head_reentry'):
            with self.subTest(stage=stage):
                if stage == 'after_head_reentry':
                    result = run_alembic(self.path)
                    self.assertEqual(result.returncode, 0, result.stderr)
                with PostgresConnection(self.path) as connection:
                    self.assertEqual(snapshot(connection), before)
                    self.assertEqual(before['revision']['version_num'], HEAD_SCHEMA_REVISION)

    def test_historical_event_guard_refuses_to_drop_history_before_baseline(self) -> None:
        path = self.path.with_name('historical-catalog-state.db')
        result = run_alembic(path, '0036_catalog_events')
        self.assertEqual(result.returncode, 0, result.stderr)
        tables = ('vault_catalog_revisions', 'catalog_events')
        with PostgresConnection(path) as connection:
            connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix,rclone_remote)
                VALUES (2,'docs','Docs','/source','fixture-bucket','docs','fixture-remote')""")
            connection.execute("""INSERT INTO vault_catalog_revisions(vault_id,revision,updated_at)
                VALUES (2,1,'2026-11-06')""")
            connection.execute("""INSERT INTO catalog_events(vault_id,revision,domain,scope,payload_json,created_at)
                VALUES (2,1,'files','root','{}','2026-11-06')""")
            before = {table: connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                      for table in tables}
        result = run_alembic(path, '0035_upload_verification_digest', command='downgrade')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('catalog event data has been persisted', result.stderr)
        with PostgresConnection(path) as connection:
            self.assertEqual({table: connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                              for table in tables}, before)
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone()['version_num'],
                             '0036_catalog_events')


if __name__ == "__main__":
    unittest.main()
