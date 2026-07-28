"""Fixed Source Volume layout and fail-closed health (issue #148).

Seams under test (confirmed):
1. Fresh startup creates managed/; empty Vaults use managed/<uuid>
2. Structural /sources and managed failures fail closed
3. Direct rw mounts are discovered; ordinary children and nested mounts rejected
4. Isolated custom-volume failure degrades only affected Vaults
5. Runtime mount loss does not mass-remove Local Copies or allow local ops
6. Returning mount requires a full local scan before local ops resume
7. No production setting/API can redirect the fixed source namespace
8. en/it UI and docs explain the fixed flat layout
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.database import SQLiteConnection
from app.services import source_layout
from app.services import vaults as vaults_service
from tests.test_database import run_alembic


class ManagedEmptyVaultRootTests(unittest.TestCase):
    """Seam 1: managed directory and empty-Vault UUID roots."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            vault_s3_bucket="test-bucket",
            vault_rclone_remote="test-remote",
        )
        for target in ("app.database.settings", "app.services.vaults.settings"):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)

    def test_startup_creates_managed_and_empty_vault_uses_managed_uuid(self) -> None:
        managed = source_layout.ensure_managed_directory()
        self.assertEqual(managed, self.sources_root / "managed")
        self.assertTrue(managed.is_dir())

        vault = vaults_service.create_vault_for_user(1, "My Archive")

        expected = self.sources_root / "managed" / vault["uuid"]
        self.assertEqual(Path(vault["source_root"]), expected)
        self.assertTrue(expected.is_dir())
        self.assertFalse((self.sources_root / vault["uuid"]).exists())


class SourcesStructureFailClosedTests(unittest.TestCase):
    """Seam 2: structural /sources and managed failures fail closed.

    Symlink and mount probes are mocked so coverage runs on Windows and
    Linux CI without requiring privileged filesystem fixtures.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        # Temp directories are not real mounts; default the probe to True so
        # structural tests exercise the intended failure modes.
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_missing_sources_root_fails_closed(self) -> None:
        missing = Path(self._tmp.name) / "absent-sources"
        source_layout.override_sources_root(missing)

        with self.assertRaises(source_layout.SourcesLayoutError) as raised:
            source_layout.validate_sources_structure()

        self.assertIn("sources", str(raised.exception).lower())

    def test_symlinked_managed_fails_closed(self) -> None:
        sources = Path(self._tmp.name) / "sources"
        sources.mkdir()
        managed = sources / "managed"
        managed.mkdir()
        source_layout.override_sources_root(sources)

        with patch.object(
            source_layout,
            "path_is_symlink",
            side_effect=lambda path: Path(path) == managed,
        ):
            with self.assertRaises(source_layout.SourcesLayoutError) as raised:
                source_layout.validate_sources_structure()

        self.assertIn("managed", str(raised.exception).lower())
        self.assertRegex(str(raised.exception), r"(?i)symlink|symbolic")

    def test_foreign_entry_under_managed_fails_closed(self) -> None:
        sources = Path(self._tmp.name) / "sources"
        sources.mkdir()
        managed = sources / "managed"
        managed.mkdir()
        (managed / "not-a-vault-uuid").mkdir()
        source_layout.override_sources_root(sources)

        with self.assertRaises(source_layout.SourcesLayoutError) as raised:
            source_layout.validate_sources_structure()

        self.assertIn("managed", str(raised.exception).lower())
        self.assertRegex(str(raised.exception), r"(?i)foreign|unexpected|invalid")

    def test_non_mount_sources_root_fails_closed(self) -> None:
        sources = Path(self._tmp.name) / "sources"
        sources.mkdir()
        source_layout.override_sources_root(sources)

        with patch.object(source_layout, "path_is_mount", return_value=False):
            with self.assertRaises(source_layout.SourcesLayoutError) as raised:
                source_layout.validate_sources_structure()

        self.assertIn("sources", str(raised.exception).lower())
        self.assertRegex(str(raised.exception), r"(?i)mount")


class PrepareSourcesLayoutTests(unittest.TestCase):
    """Seam 2 continued: prepare creates managed after structure checks."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_prepare_creates_managed_when_sources_structure_is_valid(self) -> None:
        sources = Path(self._tmp.name) / "sources"
        sources.mkdir()
        source_layout.override_sources_root(sources)

        source_layout.prepare_sources_layout()

        self.assertTrue((sources / "managed").is_dir())
        self.assertTrue(source_layout.sources_layout_is_ready())


class SourceVolumeDiscoveryTests(unittest.TestCase):
    """Seam 3: discover direct rw mounts; reject ordinary children and nests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.sources = Path(self._tmp.name) / "sources"
        self.sources.mkdir()
        source_layout.override_sources_root(self.sources)
        (self.sources / "managed").mkdir()
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_discovers_direct_rw_custom_mounts(self) -> None:
        photos = self.sources / "photos"
        photos.mkdir()

        def is_mount(path: Path | str) -> bool:
            return Path(path) in {self.sources, photos}

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            with patch.object(source_layout, "path_is_writable", return_value=True):
                volumes = source_layout.discover_source_volumes()

        self.assertEqual(
            [(v.alias, v.path, v.access, v.health) for v in volumes],
            [("photos", str(photos), "rw", "ok")],
        )

    def test_ordinary_direct_child_is_rejected_with_actionable_diagnostic(self) -> None:
        ordinary = self.sources / "photos"
        ordinary.mkdir()

        def is_mount(path: Path | str) -> bool:
            return Path(path) == self.sources

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            with self.assertRaises(source_layout.SourcesLayoutError) as raised:
                source_layout.validate_sources_structure()

        message = str(raised.exception).lower()
        self.assertIn("photos", message)
        self.assertRegex(str(raised.exception), r"(?i)mount")
        self.assertRegex(str(raised.exception), r"(?i)nested|direct|sibling|/sources/")

    def test_nested_mount_inside_custom_volume_is_rejected(self) -> None:
        photos = self.sources / "photos"
        photos.mkdir()
        nested = photos / "nested-disk"
        nested.mkdir()

        def is_mount(path: Path | str) -> bool:
            candidate = Path(path)
            return candidate in {self.sources, photos, nested}

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            with patch.object(source_layout, "path_is_writable", return_value=True):
                with self.assertRaises(source_layout.SourcesLayoutError) as raised:
                    source_layout.reject_nested_mounts()

        self.assertIn("photos", str(raised.exception).lower())
        self.assertRegex(str(raised.exception), r"(?i)nested")


class LegacyFixtureSourceRootTests(unittest.TestCase):
    """Historical suites inject private source_root dirs without the layout seam."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.reset_sources_root_override()

    def test_private_fixture_source_root_remains_usable_without_layout_seam(self) -> None:
        root = Path(self._tmp.name) / "vault-root"
        root.mkdir()
        access = source_layout.vault_local_access(str(root))
        self.assertTrue(access.local_operations_allowed)
        self.assertEqual(access.volume_health, "ok")

        synthetic = source_layout.vault_local_access("/source")
        self.assertTrue(synthetic.local_operations_allowed)
        self.assertEqual(synthetic.volume_health, "ok")


class FixedProductionNamespaceTests(unittest.TestCase):
    """Seam 7: no production setting/API redirects the fixed namespace."""

    def test_settings_no_longer_expose_vault_sources_root(self) -> None:
        self.assertFalse(hasattr(Settings(), "vault_sources_root"))

    def test_production_sources_root_ignores_vault_sources_root_env(self) -> None:
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.reset_sources_root_override()
        with patch.dict("os.environ", {"VAULT_SOURCES_ROOT": "/tmp/attacker-sources"}):
            # Re-importing Settings would pick env at class definition time;
            # the layout seam must ignore that env entirely.
            self.assertEqual(
                source_layout.get_sources_root(),
                source_layout.PRODUCTION_SOURCES_ROOT,
            )


class IsolatedVolumeDegradationTests(unittest.TestCase):
    """Seam 4: missing/ro custom volume degrades only affected Vaults."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.sources = Path(self._tmp.name) / "sources"
        self.sources.mkdir()
        (self.sources / "managed").mkdir()
        self.photos = self.sources / "photos"
        self.photos.mkdir()
        source_layout.override_sources_root(self.sources)

    def test_read_only_custom_volume_is_unhealthy_but_layout_stays_ready(self) -> None:
        def is_mount(path: Path | str) -> bool:
            return Path(path) in {self.sources, self.photos}

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            with patch.object(source_layout, "path_is_writable", return_value=False):
                source_layout.prepare_sources_layout()
                volumes = source_layout.discover_source_volumes()

        self.assertTrue(source_layout.sources_layout_is_ready())
        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0].alias, "photos")
        self.assertEqual(volumes[0].access, "ro")
        self.assertEqual(volumes[0].health, "read_only")

    def test_vault_on_unhealthy_volume_blocks_local_ops_not_cloud_catalog(self) -> None:
        vault_root = self.photos / "family"
        vault_root.mkdir()

        def is_mount(path: Path | str) -> bool:
            return Path(path) in {self.sources, self.photos}

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            with patch.object(source_layout, "path_is_writable", return_value=False):
                decision = source_layout.vault_local_access(str(vault_root))

        self.assertFalse(decision.local_operations_allowed)
        self.assertTrue(decision.cloud_catalog_allowed)
        self.assertEqual(decision.volume_alias, "photos")
        self.assertEqual(decision.volume_health, "read_only")


class RuntimeMountLossTests(unittest.TestCase):
    """Seam 5-6: mount loss suspends local ops; return requires full scan."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.addCleanup(source_layout.reset_runtime_mount_state)
        self.sources = Path(self._tmp.name) / "sources"
        self.sources.mkdir()
        (self.sources / "managed").mkdir()
        self.photos = self.sources / "photos"
        self.photos.mkdir()
        self.vault_root = self.photos / "family"
        self.vault_root.mkdir()
        source_layout.override_sources_root(self.sources)

    def test_mount_loss_blocks_local_ops_and_requires_rescan_on_return(self) -> None:
        def is_mount(path: Path | str) -> bool:
            return Path(path) in {self.sources, self.photos}

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            with patch.object(source_layout, "path_is_writable", return_value=True):
                before = source_layout.vault_local_access(str(self.vault_root))
                self.assertTrue(before.local_operations_allowed)

                source_layout.note_mount_lost("photos")
                lost = source_layout.vault_local_access(str(self.vault_root))
                self.assertFalse(lost.local_operations_allowed)
                self.assertTrue(lost.cloud_catalog_allowed)
                self.assertFalse(source_layout.should_emit_local_copy_removals("photos"))

                source_layout.note_mount_returned("photos")
                returned = source_layout.vault_local_access(str(self.vault_root))
                self.assertFalse(returned.local_operations_allowed)
                self.assertTrue(source_layout.requires_full_local_scan("photos"))

                source_layout.note_full_local_scan_completed("photos")
                recovered = source_layout.vault_local_access(str(self.vault_root))
                self.assertTrue(recovered.local_operations_allowed)
                self.assertFalse(source_layout.requires_full_local_scan("photos"))


class SourceVolumeInventoryTests(unittest.TestCase):
    """Admin inventory: alias, path, access, health, counts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.sources = Path(self._tmp.name) / "sources"
        self.sources.mkdir()
        (self.sources / "managed").mkdir()
        self.photos = self.sources / "photos"
        self.photos.mkdir()
        source_layout.override_sources_root(self.sources)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        patcher = patch("app.database.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_inventory_includes_vault_counts_and_zero_source_areas(self) -> None:
        vault_root = self.photos / "family"
        vault_root.mkdir()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('family', 'Family', %s, 'b', 'p/', 'r')
                """,
                (str(vault_root),),
            )

        def is_mount(path: Path | str) -> bool:
            return Path(path) in {self.sources, self.photos}

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            with patch.object(source_layout, "path_is_writable", return_value=True):
                items = source_layout.source_volume_inventory()

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["alias"], "photos")
        self.assertEqual(item["path"], str(self.photos))
        self.assertEqual(item["access"], "rw")
        self.assertEqual(item["health"], "ok")
        self.assertEqual(item["vault_count"], 1)
        self.assertEqual(item["source_area_count"], 0)


class MountVerificationTests(unittest.TestCase):
    """Continuous mount verification updates runtime state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.sources = Path(self._tmp.name) / "sources"
        self.sources.mkdir()
        (self.sources / "managed").mkdir()
        self.photos = self.sources / "photos"
        self.photos.mkdir()
        source_layout.override_sources_root(self.sources)

    def test_verify_mounts_once_records_loss_and_return(self) -> None:
        mounts = {self.sources, self.photos}

        def is_mount(path: Path | str) -> bool:
            return Path(path) in mounts

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            with patch.object(source_layout, "path_is_writable", return_value=True):
                source_layout.verify_mounts_once()
                self.assertFalse(source_layout.requires_full_local_scan("photos"))

                mounts.remove(self.photos)
                source_layout.verify_mounts_once()
                self.assertTrue(
                    source_layout.vault_local_access(str(self.photos / "x")).volume_health
                    == "mount_lost"
                    or not source_layout.should_emit_local_copy_removals("photos")
                )
                self.assertFalse(source_layout.should_emit_local_copy_removals("photos"))

                mounts.add(self.photos)
                source_layout.verify_mounts_once()
                self.assertTrue(source_layout.requires_full_local_scan("photos"))


class LocalOpsGateTests(unittest.TestCase):
    """Local ops refuse when Source Volume is unhealthy."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.sources = Path(self._tmp.name) / "sources"
        self.sources.mkdir()
        (self.sources / "managed").mkdir()
        self.photos = self.sources / "photos"
        self.photos.mkdir()
        self.vault_root = self.photos / "family"
        self.vault_root.mkdir()
        source_layout.override_sources_root(self.sources)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('family', 'Family', %s, 'b', 'p/', 'r')
                RETURNING id
                """,
                (str(self.vault_root),),
            ).fetchone()["id"]
        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
            allow_local_delete=True,
        )
        for target in (
            "app.database.settings",
            "app.main.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_queue_jobs_rejects_upload_when_mount_lost(self) -> None:
        from fastapi import HTTPException

        from app.main import queue_jobs

        source_layout.note_mount_lost("photos")
        with self.assertRaises(HTTPException) as raised:
            queue_jobs("note.txt", "upload", self.vault_id, 1)
        self.assertEqual(raised.exception.status_code, 503)

    def test_scan_tree_skips_mass_missing_when_mount_lost(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.storage import scan_tree

        source_layout.note_mount_lost("photos")
        (self.vault_root / "a.txt").write_text("a", encoding="utf-8")
        with SQLiteConnection(str(self.database_path)) as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=self.vault_id,
                path="gone.txt",
                file_type="regular",
                size=1,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )

        def is_mount(path: Path | str) -> bool:
            return Path(path) in {self.sources, self.photos}

        with patch.object(source_layout, "path_is_mount", side_effect=is_mount):
            scan_tree(
                {"id": self.vault_id, "source_root": str(self.vault_root)},
                "scan-1",
            )

        with SQLiteConnection(str(self.database_path)) as connection:
            missing = connection.execute(
                "SELECT COUNT(*) AS total FROM local_copies WHERE presence='missing'"
            ).fetchone()["total"]
            present = connection.execute(
                "SELECT COUNT(*) AS total FROM local_copies WHERE presence='present'"
            ).fetchone()["total"]
        # Must not mass-mark missing during mount loss.
        self.assertEqual(missing, 0)
        self.assertGreaterEqual(present, 1)



class AdminSourceVolumeInventoryHttpTests(unittest.TestCase):
    """Admin inventory HTTP seam."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.security import hash_password
        from app.sessions import create_session

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.sources = Path(self._tmp.name) / "sources"
        self.sources.mkdir()
        (self.sources / "managed").mkdir()
        self.photos = self.sources / "photos"
        self.photos.mkdir()
        source_layout.override_sources_root(self.sources)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', %s, TRUE) RETURNING id
                """,
                (hash_password("admin-password-1"),),
            ).fetchone()["id"]
        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.services.health.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)
        writable_patcher = patch.object(
            source_layout, "path_is_writable", return_value=True
        )
        writable_patcher.start()
        self.addCleanup(writable_patcher.stop)
        source_layout.prepare_sources_layout()
        self.client = TestClient(app, client=("127.0.0.1", 50000))
        with SQLiteConnection(str(self.database_path)) as connection:
            token = create_session(
                connection, user_id=self.admin_id, auth_method="oidc"
            )
        self.client.cookies.set(self.settings.session_cookie_name, token)

    def test_admin_can_list_source_volumes(self) -> None:
        response = self.client.get("/api/admin/source-volumes")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["alias"], "photos")
        self.assertEqual(items[0]["access"], "rw")
        self.assertEqual(items[0]["health"], "ok")
        self.assertEqual(items[0]["vault_count"], 0)
        self.assertEqual(items[0]["source_area_count"], 0)



if __name__ == "__main__":
    unittest.main()
