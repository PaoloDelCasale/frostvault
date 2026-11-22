"""Every write path of the Class Entry (``storage_class_since``, #731).

Real PostgreSQL 16 (disposable fixture databases, ``tests.postgres``); S3 is a
listing stub, never AWS. The Class Entry is set when a version is written
straight into a class (direct PUT here; the copy path is covered in
``tests.test_rename_sync``) and changes only when an observation (cloud scan,
catalog audit, restore check) sees another class:

- an unchanged class keeps the value across repeated observations;
- an unknown recorded class (NULL) keeps a NULL entry;
- an observed change takes the observation's instant, except for a version
  with no recorded entry (written before #731) seen in the class its Storage
  Plan predicts by now: it keeps none, so the plan estimate applies.

The Vault summary groups rows by Class Entry hour and projects only the
current or replacing version of a file; a new Vault never waits for the
minimum in its duplicate cleanup.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app.catalog import ArchiveCatalog
from app.config import Settings
from app.services import duplicate_cleanup, minimum_storage, source_layout
from app.services import vaults as vaults_service
from app.services.catalog_audit import audit_vault_catalog
from tests.fake_bucket_s3 import fake_vault_buckets
from tests.owner_fixtures import insert_owner
from tests.postgres import PostgresConnection, database_dsn
from tests.test_cloud_scan_batching import PREFIX, VAULT, VAULT_UUID, _run_scan, _seed_vault
from tests.test_database import run_alembic
from tests import catalog_probe

MIB = 1024 ** 2
DEEP_NOW = {"transitions": [{"days": 0, "storage_class": "DEEP_ARCHIVE"}]}
NO_PLAN = {"transitions": []}
LONG_AGO = datetime(2025, 1, 10, 9, 30, tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClassEntryFixture(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "class-entry.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with PostgresConnection(str(self.path)) as connection:
            _seed_vault(connection)
        self.plan(DEEP_NOW)

    def plan(self, plan: dict) -> None:
        with PostgresConnection(str(self.path)) as connection:
            connection.execute("UPDATE vaults SET storage_plan=%s::jsonb WHERE id=2", (json.dumps(plan),))

    def put(self, path: str, *, storage_class: str | None = "STANDARD", size: int = MIB,
            created_at: datetime = LONG_AGO) -> str:
        """A version uploaded by a direct PUT (``begin_replacement``)."""
        with PostgresConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2, path=path, file_type="regular", size=size, mtime_ns=1,
                observed_at=created_at.isoformat())
            row = catalog_probe.get_file_by_path(catalog, 2, path)
            return catalog.begin_replacement(
                row["id"], object_key=PREFIX + path, provider_version_id=f"{path}-v1",
                size=size, storage_class=storage_class, created_at=created_at.isoformat())

    def legacy(self, version_id: str, *, storage_class: str | None = "STANDARD") -> None:
        """A version written before #731: no recorded Class Entry."""
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE archive_versions SET storage_class=%s, storage_class_since=NULL WHERE id=%s",
                (storage_class, version_id))

    def version(self, version_id: str) -> dict:
        with PostgresConnection(str(self.path)) as connection:
            return dict(connection.execute(
                "SELECT storage_class, storage_class_since, created_at FROM archive_versions WHERE id=%s",
                (version_id,)).fetchone())

    def assert_now(self, value: datetime | None) -> None:
        self.assertIsNotNone(value)
        self.assertLess(abs((value - utcnow()).total_seconds()), 600, value)


class DirectPutTests(ClassEntryFixture):

    def test_a_direct_put_enters_its_class_when_created(self) -> None:
        version = self.version(self.put("a.bin"))
        self.assertEqual(version["storage_class"], "STANDARD")
        self.assertEqual(version["storage_class_since"], LONG_AGO)
        self.assertEqual(version["storage_class_since"], version["created_at"])
        deep = self.version(self.put("deep.bin", storage_class="DEEP_ARCHIVE"))
        self.assertEqual(deep["storage_class_since"], deep["created_at"])

    def test_a_put_with_an_unknown_class_records_no_entry(self) -> None:
        version = self.version(self.put("a.bin", storage_class=None))
        self.assertEqual((version["storage_class"], version["storage_class_since"]), (None, None))


class CloudScanTests(ClassEntryFixture):

    def scan(self, *items: tuple[str, str]) -> None:
        versions = [{"Key": PREFIX + path, "VersionId": f"{path}-v1", "Size": MIB,
                     "StorageClass": storage_class, "ETag": '"e"',
                     "LastModified": LONG_AGO} for path, storage_class in items]
        _run_scan(self.path, pages=[{"Versions": versions, "DeleteMarkers": []}])

    def test_an_unchanged_class_keeps_its_entry_across_repeated_scans(self) -> None:
        version_id = self.put("a.bin")
        for _ in range(3):
            self.scan(("a.bin", "STANDARD"))
            self.assertEqual(self.version(version_id)["storage_class_since"], LONG_AGO)

    def test_the_class_compares_case_insensitively(self) -> None:
        version_id = self.put("a.bin", storage_class="standard")
        self.scan(("a.bin", "STANDARD"))
        self.assertEqual(self.version(version_id)["storage_class_since"], LONG_AGO)

    def test_an_unknown_class_keeps_a_null_entry(self) -> None:
        version_id = self.put("a.bin", storage_class=None)
        self.scan(("a.bin", "DEEP_ARCHIVE"))
        self.assertEqual(self.version(version_id),
                         {"storage_class": "DEEP_ARCHIVE", "storage_class_since": None, "created_at": LONG_AGO})
        self.scan(("a.bin", "DEEP_ARCHIVE"))
        self.assertIsNone(self.version(version_id)["storage_class_since"])

    def test_an_observed_change_takes_the_observation_instant_once(self) -> None:
        version_id = self.put("a.bin")
        self.scan(("a.bin", "DEEP_ARCHIVE"))
        observed = self.version(version_id)
        self.assertEqual(observed["storage_class"], "DEEP_ARCHIVE")
        self.assert_now(observed["storage_class_since"])
        self.scan(("a.bin", "DEEP_ARCHIVE"))
        self.assertEqual(self.version(version_id)["storage_class_since"], observed["storage_class_since"])

    def test_a_legacy_version_seen_late_in_the_planned_class_keeps_the_plan_estimate(self) -> None:
        version_id = self.put("a.bin")
        self.legacy(version_id)
        self.scan(("a.bin", "DEEP_ARCHIVE"))
        self.assertEqual(self.version(version_id),
                         {"storage_class": "DEEP_ARCHIVE", "storage_class_since": None, "created_at": LONG_AGO})

    def test_a_legacy_version_in_a_class_the_plan_does_not_predict_takes_the_observation(self) -> None:
        unplanned = self.put("unplanned.bin")
        small = self.put("small.bin", size=1024)
        fresh = self.put("fresh.bin", created_at=utcnow() - timedelta(hours=2))
        for version_id in (unplanned, small, fresh):
            self.legacy(version_id)
        self.scan(("unplanned.bin", "GLACIER"), ("small.bin", "DEEP_ARCHIVE"),
                  ("fresh.bin", "DEEP_ARCHIVE"))
        # Another class than the plan's; an object under 128 KiB, which no
        # lifecycle rule moves; a transition not due yet (days + 1).
        for version_id in (unplanned, small, fresh):
            with self.subTest(version_id=version_id):
                self.assert_now(self.version(version_id)["storage_class_since"])

    def test_without_a_plan_a_legacy_version_takes_the_observation(self) -> None:
        self.plan(NO_PLAN)
        version_id = self.put("a.bin")
        self.legacy(version_id)
        self.scan(("a.bin", "DEEP_ARCHIVE"))
        self.assert_now(self.version(version_id)["storage_class_since"])


class RestoreCheckTests(ClassEntryFixture):

    def check(self, version_id: str, storage_class: str | None, checked_at: datetime) -> None:
        with PostgresConnection(str(self.path)) as connection:
            ArchiveCatalog(connection).update_restore_state(
                version_id, state=None, expiry=None, checked_at=checked_at.isoformat(),
                storage_class=storage_class)

    def test_the_restore_check_moves_the_entry_only_on_an_observed_change(self) -> None:
        version_id = self.put("a.bin")
        first = utcnow().replace(microsecond=0) - timedelta(hours=1)
        self.check(version_id, "STANDARD", first)
        self.check(version_id, None, first)
        self.assertEqual(self.version(version_id)["storage_class_since"], LONG_AGO)
        self.check(version_id, "GLACIER", first)
        self.assertEqual(self.version(version_id)["storage_class_since"], first)
        self.check(version_id, "GLACIER", first + timedelta(minutes=30))
        self.assertEqual(self.version(version_id),
                         {"storage_class": "GLACIER", "storage_class_since": first, "created_at": LONG_AGO})

    def test_an_unknown_class_and_a_planned_legacy_class_keep_a_null_entry(self) -> None:
        unknown = self.put("unknown.bin", storage_class=None)
        legacy = self.put("legacy.bin")
        self.legacy(legacy)
        checked = utcnow().replace(microsecond=0)
        self.check(unknown, "DEEP_ARCHIVE", checked)
        self.check(legacy, "DEEP_ARCHIVE", checked)
        self.assertIsNone(self.version(unknown)["storage_class_since"])
        self.assertEqual(self.version(legacy)["storage_class"], "DEEP_ARCHIVE")
        self.assertIsNone(self.version(legacy)["storage_class_since"])


class CatalogAuditTests(ClassEntryFixture):

    def audit(self, *items: tuple[str, str]) -> dict:
        versions = [{"Key": PREFIX + path, "VersionId": f"{path}-v1", "Size": MIB,
                     "StorageClass": storage_class} for path, storage_class in items]

        class Paginator:
            def paginate(self, **_kwargs):
                return [{"Versions": versions, "DeleteMarkers": []}]

        client = Mock()
        client.get_paginator.return_value = Paginator()
        with PostgresConnection(str(self.path)) as connection:
            return audit_vault_catalog(connection, dict(VAULT), client)

    def test_the_audit_keeps_an_unchanged_class_and_stamps_a_change(self) -> None:
        same = self.put("same.bin")
        lower = self.put("lower.bin", storage_class="standard")
        changed = self.put("changed.bin")
        unknown = self.put("unknown.bin", storage_class=None)
        with PostgresConnection(str(self.path)) as connection:
            # Not available: the audit writes the row although the class is the same.
            connection.execute("UPDATE archive_versions SET availability='unknown' WHERE id=%s", (same,))
        for _ in range(2):
            report = self.audit(("same.bin", "STANDARD"), ("lower.bin", "STANDARD"),
                                ("changed.bin", "GLACIER"), ("unknown.bin", "DEEP_ARCHIVE"))
            self.assertEqual(self.version(same)["storage_class_since"], LONG_AGO)
            self.assertEqual(self.version(lower)["storage_class_since"], LONG_AGO)
            self.assertIsNone(self.version(unknown)["storage_class_since"])
        stamped = self.version(changed)["storage_class_since"]
        self.assert_now(stamped)
        # The change was counted once (first audit); the case never is drift.
        self.assertEqual(report["storage_class_drift"], 0)

    def test_a_legacy_version_seen_in_the_planned_class_keeps_the_plan_estimate(self) -> None:
        legacy = self.put("legacy.bin")
        recorded = self.put("recorded.bin")
        self.legacy(legacy)
        self.audit(("legacy.bin", "DEEP_ARCHIVE"), ("recorded.bin", "DEEP_ARCHIVE"))
        self.assertEqual(self.version(legacy)["storage_class"], "DEEP_ARCHIVE")
        self.assertIsNone(self.version(legacy)["storage_class_since"])
        # A recorded entry follows the normal rule: the observation instant.
        self.assert_now(self.version(recorded)["storage_class_since"])


class VaultSummaryQueryTests(ClassEntryFixture):

    def test_versions_with_the_same_class_entry_hour_aggregate_into_one_row(self) -> None:
        ids = [self.put(f"{n}.bin", storage_class="DEEP_ARCHIVE") for n in range(3)]
        with PostgresConnection(str(self.path)) as connection:
            for minute, version_id in enumerate(ids):
                connection.execute(
                    "UPDATE archive_versions SET storage_class_since=%s WHERE id=%s",
                    (LONG_AGO.replace(minute=5 + minute * 20), version_id))
            rows = connection.execute(
                minimum_storage._VAULT_VERSIONS_SQL,
                (minimum_storage.SMALL_OBJECT_BYTES, minimum_storage.SMALL_OBJECT_BYTES, 2)).fetchall()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]["count"], rows[0]["size"], rows[0]["is_current"], rows[0]["copied"]),
                         (3, 3 * MIB, True, False))

    def test_only_the_current_or_replacing_version_is_projected(self) -> None:
        recent = utcnow() - timedelta(days=10)
        self.put("replacing.bin", created_at=recent)
        superseded = self.put("superseded.bin", created_at=recent)
        with PostgresConnection(str(self.path)) as connection:
            # Neither current nor replacing: a superseded version awaiting cleanup.
            connection.execute(
                "UPDATE files SET replacing_version_id=NULL WHERE replacing_version_id=%s", (superseded,))
            summary = minimum_storage.load_vault_summary(connection, 2)
        classes = {c["storage_class"]: c for c in summary["by_storage_class"]}
        self.assertEqual(classes["DEEP_ARCHIVE"]["within"], {"count": 1, "bytes": MIB})
        self.assertEqual(classes["STANDARD"]["beyond"], {"count": 1, "bytes": MIB})


class NewVaultTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "new-vault.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        sources = Path(self._tmp.name) / "sources"
        sources.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(sources)
        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection, display_name="Alice")
        configured = replace(Settings(), database_dsn=database_dsn(str(self.path)))
        fake_vault_buckets(self)
        for target in ("app.database.settings", "app.services.vaults.settings"):
            patcher = patch(target, configured)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_new_vault_does_not_wait_for_the_minimum(self) -> None:
        vault = vaults_service.create_vault("Fresh Archive")
        with PostgresConnection(str(self.path)) as connection:
            row = connection.execute("SELECT * FROM vaults WHERE id=%s", (vault["id"],)).fetchone()
        self.assertIs(row["duplicate_cleanup_beyond_minimum"], False)
        self.assertIs(duplicate_cleanup._settings(row)["beyond_minimum"], False)
        self.assertEqual(str(row["uuid"]), vault["uuid"])
        self.assertNotEqual(vault["uuid"], VAULT_UUID)


if __name__ == "__main__":
    unittest.main()
