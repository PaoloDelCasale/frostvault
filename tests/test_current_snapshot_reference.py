"""Current Snapshot authority is an exact reference, never a version election.

KeeperReferenceTests need no database. SnapshotReferencePostgresTests exercise
real FK/trigger/transaction safety on the disposable PG16 fixture in CI.
"""
from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg

from app.catalog import ArchiveCatalog
from app.services.snapshot_cleanup import _keeper
from app.services.upload_provenance import UploadBlocked
from tests.postgres import PostgresConnection
from tests.test_database import run_alembic

DIGEST = hashlib.sha256(b"content").hexdigest()
STAMP = "2026-11-12T10:00:00+00:00"


class KeeperReferenceTests(unittest.TestCase):
    def fixture(self, *, current="keeper", replacement=None):
        row = dict(id="keeper", vault_file_id="file", vault_id=2,
                   integrity="verified", availability="available", size=7,
                   provider_version_id="exact-provider-id", object_key="docs/a.txt",
                   plaintext_sha256=DIGEST, uploaded_at=STAMP)
        state = dict(current_version_id=current, replacing_version_id=replacement)
        connection = Mock()
        connection.execute.side_effect = [Mock(fetchone=Mock(return_value=row)),
                                          Mock(fetchall=Mock(return_value=[])),
                                          Mock(fetchall=Mock(return_value=[])),
                                          Mock(fetchone=Mock(return_value=state))]
        job = dict(vault_file_id="file", vault_id=2, path="a.txt", s3_prefix="docs", s3_bucket="bucket")
        return connection, job, row

    def test_exact_current_reference_is_accepted_without_any_ordinal(self):
        connection, job, row = self.fixture()
        self.assertEqual(_keeper(connection, job, "keeper"), row)
        sql = " ".join(call.args[0] for call in connection.execute.call_args_list)
        self.assertIn("current_version_id", sql)
        self.assertNotIn("version_number", sql)
        self.assertNotIn("ORDER BY", sql)

    def test_verified_history_is_not_a_keeper_even_when_available(self):
        for current in (None, "another-keeper"):
            with self.subTest(current=current):
                connection, job, _ = self.fixture(current=current)
                with self.assertRaises(UploadBlocked) as raised:
                    _keeper(connection, job, "keeper")
                self.assertEqual(raised.exception.reason, "target_superseded")

    def test_outstanding_replacement_blocks_cleanup_of_the_old_keeper(self):
        connection, job, _ = self.fixture(replacement="independent-new-attempt")
        with self.assertRaises(UploadBlocked) as raised:
            _keeper(connection, job, "keeper")
        self.assertEqual(raised.exception.reason, "target_superseded")

    def test_pointer_does_not_replace_exact_identity_or_integrity_checks(self):
        for changes in (dict(integrity="unverified"), dict(availability="missing"),
                        dict(provider_version_id="null"), dict(plaintext_sha256=None),
                        dict(size=None), dict(object_key="docs/other.txt")):
            with self.subTest(changes=changes):
                connection, job, row = self.fixture()
                row.update(changes)
                with self.assertRaises(UploadBlocked):
                    _keeper(connection, job, "keeper")

    def test_expected_job_digest_still_fences_the_explicit_keeper(self):
        connection, job, row = self.fixture()
        connection.execute.side_effect = [Mock(fetchone=Mock(return_value=row)),
                                          Mock(fetchall=Mock(return_value=[dict(upload_plaintext_sha256="0" * 64)]))]
        with self.assertRaises(UploadBlocked) as raised:
            _keeper(connection, job, "keeper")
        self.assertEqual(raised.exception.reason, "proof_mismatch")

    def test_local_observation_binds_cloud_only_reference_once(self):
        # Exercise the actual dynamically assembled call, not a copied query.
        for recovered in (None, STAMP):
            for hold in (False, True):
                for stability in (False, True):
                    with self.subTest(recovered=recovered, hold=hold, stability=stability):
                        connection = Mock()
                        catalog = ArchiveCatalog(connection)
                        with patch.object(catalog, "_get_or_create_file", return_value="file"), \
                             patch.object(catalog, "_supports_cloud_only_hold", return_value=hold), \
                             patch.object(catalog, "_supports_content_observation", return_value=stability), \
                             patch.object(catalog, "_cancel_reappeared_deletions"), \
                             patch.object(catalog, "_mark_path_aggregates_dirty"):
                            catalog.observe_local_copy(vault_id=2, path="a.txt", file_type="regular",
                                size=7, mtime_ns=123, observed_at=STAMP, recovered_at=recovered)
                        call = next(call for call in connection.execute.call_args_list
                                    if "INSERT INTO local_copies" in call.args[0])
                        sql, parameters = call.args
                        self.assertEqual(sql.count("%s"), len(parameters))
                        if hold and recovered is None:
                            self.assertIn("current_version_id", sql)
                            self.assertEqual(parameters[-1], "file")

    def test_temporary_migration_refuses_old_versions_before_any_ddl(self):
        path = Path(__file__).resolve().parents[1] / "migrations/versions/0075_file_state.py"
        spec = importlib.util.spec_from_file_location("file_state_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        operations = Mock()
        operations.get_bind.return_value.execute.return_value.first.return_value = (1,)
        with patch.object(migration, "op", operations), self.assertRaisesRegex(RuntimeError, "empty catalog"):
            migration.upgrade()
        self.assertEqual([call[0] for call in operations.method_calls], ["get_bind"])


class SnapshotReferencePostgresTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.identity = Path(self.directory.name) / "snapshot-reference"
        result = run_alembic(self.identity)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.connection() as connection:
            connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix)
                VALUES (2,'reference','Reference','/unused-isolated-reference','bucket','docs')""")

    def connection(self):
        return PostgresConnection(self.identity)

    def version(self, catalog, provider, *, path="a.txt", origin="upload", stamp=STAMP):
        return catalog.record_archive_version(vault_id=2, path=path, object_key="docs/" + path,
            provider_version_id=provider, size=7, storage_class="STANDARD", etag=provider,
            uploaded_at=stamp, observed_at=STAMP, scan_id=STAMP, origin=origin)

    def state(self, connection, version_id):
        return connection.execute("""SELECT vf.* FROM vault_files vf JOIN archive_versions av
            ON av.vault_file_id=vf.id WHERE av.id=%s""", (version_id,)).fetchone()

    def verify(self, catalog, version_id):
        catalog.mark_version_verified(version_id, plaintext_sha256=DIGEST, verified_at=STAMP)

    def test_verified_publication_is_atomic_and_ignores_chronology(self):
        with self.connection() as connection:
            catalog = ArchiveCatalog(connection)
            first = self.version(catalog, "first")
            self.assertIsNone(self.state(connection, first)["current_version_id"])
            self.assertEqual(self.state(connection, first)["replacing_version_id"], first)
            self.verify(catalog, first)
            second = self.version(catalog, "second", stamp="2000-01-01T00:00:00+00:00")
            state = self.state(connection, first)
            self.assertEqual((state["current_version_id"], state["replacing_version_id"]), (first, second))
            catalog.mark_version_mismatch(second, plaintext_sha256=DIGEST, checked_at=STAMP)
            self.assertEqual(self.state(connection, first)["current_version_id"], first)
            self.verify(catalog, second)
            state = self.state(connection, first)
            self.assertEqual((state["current_version_id"], state["replacing_version_id"]), (second, None))
            versions = catalog.list_versions(2, "a.txt")
            self.assertEqual([row["id"] for row in versions if row["recoverable"]], [second])
            columns = connection.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='archive_versions'""").fetchall()
            self.assertNotIn("version_number", [row["column_name"] for row in columns])

    def test_verification_rollback_preserves_the_prior_keeper_and_pending_obligation(self):
        with self.connection() as connection:
            catalog = ArchiveCatalog(connection)
            first = self.version(catalog, "first")
            self.verify(catalog, first)
            pending = self.version(catalog, "pending")
            with self.assertRaisesRegex(RuntimeError, "after proof"), connection.transaction():
                self.verify(catalog, pending)
                raise RuntimeError("after proof")
            state = self.state(connection, first)
            self.assertEqual((state["current_version_id"], state["replacing_version_id"]), (first, pending))
            self.assertEqual(connection.execute("SELECT integrity FROM archive_versions WHERE id=%s", (pending,)).fetchone()["integrity"], "unverified")

    def test_unselected_cloud_observation_remains_visible_without_recovery_or_cleanup_authority(self):
        with self.connection() as connection:
            catalog = ArchiveCatalog(connection)
            observed = self.version(catalog, "observation", origin="discovered")
            state = self.state(connection, observed)
            self.assertIsNone(state["current_version_id"])
            self.assertIsNone(state["replacing_version_id"])
            rows = catalog.list_file_rows(2)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "unconfirmed")
            self.assertFalse(rows[0]["recover_eligible"])
            self.assertFalse(rows[0]["cleanup_eligible"])
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM archive_versions").fetchone()["n"], 1)

    def test_discovery_and_historical_verification_never_displace_the_keeper(self):
        with self.connection() as connection:
            catalog = ArchiveCatalog(connection)
            first = self.version(catalog, "first")
            self.verify(catalog, first)
            other = self.version(catalog, "outside", origin="discovered", stamp="2099-01-01T00:00:00+00:00")
            self.verify(catalog, other)
            state = self.state(connection, first)
            self.assertEqual((state["current_version_id"], state["replacing_version_id"]), (first, None))
            self.assertEqual([row["id"] for row in catalog.list_versions(2, "a.txt") if row["recoverable"]], [first])
            with self.assertRaises(UploadBlocked):
                catalog.stage_snapshot_replacement(other)

    def test_one_outstanding_replacement_cannot_be_forgotten(self):
        with self.connection() as connection:
            catalog = ArchiveCatalog(connection)
            first = self.version(catalog, "first")
            self.verify(catalog, first)
            pending = self.version(catalog, "pending")
            with self.assertRaises(UploadBlocked), connection.transaction():
                self.version(catalog, "another")
            state = self.state(connection, first)
            self.assertEqual((state["current_version_id"], state["replacing_version_id"]), (first, pending))

    def test_scope_and_verified_content_are_database_constraints(self):
        with self.connection() as connection:
            catalog = ArchiveCatalog(connection)
            first = self.version(catalog, "first")
            other = self.version(catalog, "other", path="b.txt")
            file_id = self.state(connection, first)["id"]
            for column, target in (("current_version_id", first), ("current_version_id", other),
                                   ("replacing_version_id", other)):
                with self.subTest(column=column, target=target):
                    with self.assertRaises(psycopg.Error), connection.transaction():
                        connection.execute(f"UPDATE vault_files SET {column}=%s WHERE id=%s", (target, file_id))
                        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
            self.verify(catalog, first)
            with self.assertRaises(psycopg.Error), connection.transaction():
                connection.execute("UPDATE vault_files SET replacing_version_id=%s WHERE id=%s", (first, file_id))
            with self.assertRaises(psycopg.Error), connection.transaction():
                connection.execute("UPDATE archive_versions SET availability='purged' WHERE id=%s", (first,))
                connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_lost_observability_retains_identity_without_electing_verified_history(self):
        with self.connection() as connection:
            catalog = ArchiveCatalog(connection)
            first = self.version(catalog, "first")
            self.verify(catalog, first)
            second = self.version(catalog, "second")
            self.verify(catalog, second)
            for availability in ("unknown", "missing"):
                connection.execute("UPDATE archive_versions SET availability=%s WHERE id=%s", (availability, second))
                self.assertEqual(self.state(connection, second)["current_version_id"], second)
                self.assertFalse(any(row["recoverable"] for row in catalog.list_versions(2, "a.txt")))

    def test_rename_reuse_cannot_publish_against_a_different_predecessor(self):
        with self.connection() as connection:
            catalog = ArchiveCatalog(connection)
            first = self.version(catalog, "first")
            self.verify(catalog, first)
            second = self.version(catalog, "second")
            self.verify(catalog, second)
            historical = self.version(catalog, "historical", origin="discovered")
            self.verify(catalog, historical)
            with self.assertRaises(UploadBlocked):
                catalog.stage_snapshot_replacement(historical, expected_current_id=first)
            self.assertEqual(self.state(connection, second)["current_version_id"], second)
