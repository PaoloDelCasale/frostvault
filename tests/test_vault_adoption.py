"""Adopt an existing Source Area directory when creating a Vault (issue #150).

Seams under test (confirmed):
1. Empty creation remains available and uses /sources/managed/<uuid>
2. Authorized adoption stores the selected absolute root; UUID/S3 prefix server-minted
3. Source Area root and empty existing directory are valid candidates
4. Unauthorized / invalid filesystem candidates rejected with no partial Vault
5. Exact / inside / contains Vault overlap rejected (incl. concurrent + disabled)
6. Successful adoption triggers async scan; scan failure leaves retryable Vault
7. Descendant unsupported/permission findings follow existing health behavior
8. Plain/crypt, ownership, recovery custody, and auth remain unchanged
9. No create request can choose S3 identity, crypt secret, or arbitrary path
10. en/it API/UI errors and both create modes covered at 375px
"""
from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.database import SQLiteConnection
from app.services import source_areas, source_layout
from app.services import vaults as vaults_service
from tests.test_database import run_alembic

_ASSIGN = dict(actor_user_id=99, reason="delegate photos subtree for archive creation")


class _AdoptionFixture(unittest.TestCase):
    """Shared Source Volume + Source Area layout for adoption seams."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()
        (self.sources_root / "managed").mkdir()
        self.photos = self.sources_root / "photos"
        self.photos.mkdir()
        self.albums = self.photos / "albums"
        self.albums.mkdir()
        (self.albums / "photo.jpg").write_bytes(b"jpeg-bytes")

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (2, 'bob', 'Bob', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (99, 'admin', 'Admin', 'hash', 1)"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            vault_s3_bucket="test-bucket",
            vault_rclone_remote="test-remote",
        )
        for target in (
            "app.database.settings",
            "app.services.vaults.settings",
            "app.services.source_areas.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch.object(
            source_layout,
            "path_is_mount",
            side_effect=lambda path: Path(path).resolve()
            in {self.sources_root.resolve(), self.photos.resolve()},
        )
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def _grant(self, *, user_id: int = 1, relative_path: str = "albums") -> dict:
        with SQLiteConnection(str(self.database_path)) as connection:
            grant = source_areas.assign_source_area(
                connection,
                user_id=user_id,
                volume_alias="photos",
                relative_path=relative_path,
                **_ASSIGN,
            )
            connection.commit()
        return grant

    def _vault_count(self) -> int:
        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM vaults").fetchone()
        return int(row["total"])

    def _adopt(
        self,
        user_id: int = 1,
        *,
        name: str = "Albums Archive",
        relative_path: str = "albums",
        volume_alias: str = "photos",
        actor_is_admin: bool = False,
        **kwargs,
    ) -> dict:
        return vaults_service.create_vault_for_user(
            user_id,
            name,
            creation_mode="adopt",
            volume_alias=volume_alias,
            relative_path=relative_path,
            actor_is_admin=actor_is_admin,
            **kwargs,
        )


class VaultAdoptionHappyPathTests(_AdoptionFixture):
    """Seams 1–3: empty regression, authorized adoption, valid candidates."""

    def test_empty_creation_still_uses_managed_uuid_directory(self) -> None:
        vault = vaults_service.create_vault_for_user(1, "Empty Archive")
        managed = source_layout.get_managed_root() / vault["uuid"]
        self.assertEqual(Path(vault["source_root"]), managed)
        self.assertTrue(managed.is_dir())
        self.assertEqual(vault["s3_prefix"], f"vaults/{vault['uuid']}/")

    def test_authorized_user_adopts_existing_directory_in_place(self) -> None:
        self._grant()
        vault = self._adopt()

        self.assertEqual(vault["name"], "Albums Archive")
        self.assertTrue(vault["uuid"])
        self.assertEqual(len(vault["uuid"]), 36)
        self.assertEqual(vault["s3_prefix"], f"vaults/{vault['uuid']}/")
        self.assertEqual(vault["s3_bucket"], "test-bucket")
        self.assertEqual(Path(vault["source_root"]), self.albums.resolve())
        self.assertTrue(Path(vault["source_root"]).is_dir())
        # Adoption never moves, copies, or rewrites existing content.
        self.assertEqual((self.albums / "photo.jpg").read_bytes(), b"jpeg-bytes")
        managed = source_layout.get_managed_root() / vault["uuid"]
        self.assertFalse(managed.exists())

        with SQLiteConnection(str(self.database_path)) as connection:
            members = connection.execute(
                "SELECT user_id, role FROM vault_members WHERE vault_id=%s",
                (vault["id"],),
            ).fetchall()
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["user_id"], 1)
        self.assertEqual(members[0]["role"], "owner")

    def test_source_area_root_and_empty_directory_are_valid_candidates(self) -> None:
        empty = self.albums / "empty-bin"
        empty.mkdir()
        self._grant(relative_path="albums")

        empty_vault = self._adopt(
            name="Empty Bin",
            relative_path="albums/empty-bin",
            slug="empty-bin",
        )
        self.assertEqual(Path(empty_vault["source_root"]), empty.resolve())
        self.assertTrue(empty.is_dir())
        self.assertEqual(list(empty.iterdir()), [])

        raw = self.photos / "raw"
        raw.mkdir()
        self._grant(relative_path="raw")
        area_root = self._adopt(name="Raw Root", relative_path="raw", slug="raw-root")
        self.assertEqual(Path(area_root["source_root"]), raw.resolve())


class VaultAdoptionRejectionTests(_AdoptionFixture):
    """Seam 4: unauthorized and invalid candidates leave no partial Vault."""

    def test_user_outside_grant_is_rejected_with_no_partial_vault(self) -> None:
        self._grant(relative_path="albums")
        outside = self.photos / "raw"
        outside.mkdir()

        with self.assertRaises(vaults_service.VaultAdoptionError) as raised:
            self._adopt(relative_path="raw")
        self.assertEqual(raised.exception.reason, "forbidden")
        self.assertEqual(self._vault_count(), 0)

    def test_admin_cannot_adopt_another_users_grant(self) -> None:
        self._grant(user_id=1, relative_path="albums")

        with self.assertRaises(vaults_service.VaultAdoptionError) as raised:
            self._adopt(
                2,
                name="Bob Archive",
                relative_path="albums",
                actor_is_admin=True,
            )
        self.assertEqual(raised.exception.reason, "forbidden")
        self.assertEqual(self._vault_count(), 0)

    def test_admin_may_adopt_unassigned_path_for_owner(self) -> None:
        free = self.photos / "inbox"
        free.mkdir()
        (free / "note.txt").write_text("hello", encoding="utf-8")

        vault = self._adopt(
            1,
            name="Inbox",
            relative_path="inbox",
            actor_is_admin=True,
        )
        self.assertEqual(Path(vault["source_root"]), free.resolve())
        self.assertEqual((free / "note.txt").read_text(encoding="utf-8"), "hello")

    def test_managed_missing_symlink_and_nested_mount_are_rejected(self) -> None:
        self._grant(relative_path="")

        with self.assertRaises(vaults_service.VaultAdoptionError) as managed:
            self._adopt(volume_alias="managed", relative_path="")
        self.assertEqual(managed.exception.reason, "invalid_volume")

        with self.assertRaises(vaults_service.VaultAdoptionError) as missing:
            self._adopt(relative_path="does-not-exist")
        self.assertEqual(missing.exception.reason, "path_missing")

        with patch.object(
            source_layout,
            "path_is_symlink",
            side_effect=lambda path: Path(path).resolve() == self.albums.resolve(),
        ):
            with self.assertRaises(vaults_service.VaultAdoptionError) as symlink:
                self._adopt(relative_path="albums")
            self.assertEqual(symlink.exception.reason, "invalid_path")

        nested = self.photos / "nested-mount"
        nested.mkdir()
        with patch.object(
            source_layout,
            "path_is_mount",
            side_effect=lambda path: Path(path).resolve()
            in {
                self.sources_root.resolve(),
                self.photos.resolve(),
                nested.resolve(),
            },
        ):
            with self.assertRaises(vaults_service.VaultAdoptionError) as mount:
                self._adopt(relative_path="nested-mount")
            self.assertEqual(mount.exception.reason, "nested_mount")

        self.assertEqual(self._vault_count(), 0)

    def test_unavailable_volume_and_unwritable_root_are_rejected(self) -> None:
        self._grant(relative_path="albums")

        with patch.object(
            source_layout,
            "discover_source_volumes",
            return_value=[
                source_layout.SourceVolume(
                    alias="photos",
                    path=str(self.photos),
                    access="rw",
                    health="unavailable",
                    diagnostic="mount lost",
                )
            ],
        ):
            with self.assertRaises(vaults_service.VaultAdoptionError) as unavailable:
                self._adopt()
            self.assertEqual(unavailable.exception.reason, "volume_unavailable")

        with patch.object(
            source_layout,
            "path_is_writable",
            side_effect=lambda path: Path(path).resolve() != self.albums.resolve(),
        ):
            with self.assertRaises(vaults_service.VaultAdoptionError) as unwritable:
                self._adopt()
            self.assertEqual(unwritable.exception.reason, "unwritable")

        self.assertEqual(self._vault_count(), 0)


class VaultAdoptionOverlapTests(_AdoptionFixture):
    """Seam 5: Vault root overlap and concurrent adoption fail closed."""

    def _insert_vault(self, source_root: Path, *, enabled: bool = True) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, uuid, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote, enabled
                )
                VALUES (
                    1, '11111111-1111-1111-1111-111111111111', 'existing',
                    'Existing', %s, 'bucket', 'vaults/existing/', 'remote', %s
                )
                """,
                (str(source_root), 1 if enabled else 0),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (1, 1, 'owner')"
            )

    def test_exact_inside_and_contains_overlap_are_rejected(self) -> None:
        child = self.albums / "2020"
        child.mkdir()
        self._grant(relative_path="")
        self._insert_vault(child)

        with self.assertRaises(vaults_service.VaultAdoptionError) as exact:
            self._adopt(name="Exact", relative_path="albums/2020")
        self.assertEqual(exact.exception.reason, "overlap")

        trip = child / "trip"
        trip.mkdir()
        with self.assertRaises(vaults_service.VaultAdoptionError) as inside:
            self._adopt(name="Inside", relative_path="albums/2020/trip")
        self.assertEqual(inside.exception.reason, "overlap")

        with self.assertRaises(vaults_service.VaultAdoptionError) as contains:
            self._adopt(name="Contains", relative_path="albums")
        self.assertEqual(contains.exception.reason, "overlap")

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE vaults SET enabled=0 WHERE id=1")
        with self.assertRaises(vaults_service.VaultAdoptionError) as disabled:
            self._adopt(name="Disabled", relative_path="albums/2020")
        self.assertEqual(disabled.exception.reason, "overlap")

        self.assertEqual(self._vault_count(), 1)

    def test_concurrent_overlapping_adoptions_fail_closed(self) -> None:
        self._grant(relative_path="")

        def _create(label: str) -> str:
            try:
                vault = self._adopt(
                    name=f"Vault {label}",
                    relative_path="",
                    slug=f"vault-{label}",
                )
                return f"ok:{vault['id']}"
            except vaults_service.VaultAdoptionError as exc:
                return f"err:{exc.reason}"
            except vaults_service.VaultSlugTaken:
                return "err:slug"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_create, ("one", "two")))

        oks = [item for item in results if item.startswith("ok:")]
        errs = [item for item in results if item.startswith("err:")]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertTrue(
            errs[0].endswith("overlap") or errs[0].endswith("slug"),
            results,
        )
        self.assertEqual(self._vault_count(), 1)


class VaultAdoptionScanTests(_AdoptionFixture):
    """Seams 6–7: post-adoption scan catalogs content; failures stay retryable."""

    def test_successful_adoption_scan_catalogs_existing_regular_file(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.storage import scan_vault

        self._grant()
        vault = self._adopt()

        with patch("app.storage.scan_cloud", return_value=0), patch(
            "app.storage.validate_cloud_vault", return_value=None
        ), patch(
            "app.storage.reconcile_pending_policy_tags", return_value=0
        ), patch(
            "app.storage.sync_lifecycle_rules_for_bucket", return_value=0
        ), patch(
            "app.storage.queue_auto_uploads", return_value=0
        ), patch(
            "app.storage.queue_auto_local_cleanups", return_value=0
        ):
            result = scan_vault(dict(vault))

        self.assertGreaterEqual(result.get("local", 0), 1)
        with SQLiteConnection(str(self.database_path)) as connection:
            rows = ArchiveCatalog(connection).list_file_rows(int(vault["id"]))
        by_path = {row["path"]: row for row in rows}
        self.assertIn("photo.jpg", by_path)
        self.assertEqual(by_path["photo.jpg"]["local_file_type"], "regular")
        self.assertTrue(by_path["photo.jpg"].get("upload_eligible"))

    def test_scan_failure_leaves_retryable_vault(self) -> None:
        from app.storage import runtime_status, scan_vault

        self._grant()
        vault = self._adopt()
        runtime_status.pop(int(vault["id"]), None)

        with patch(
            "app.storage.scan_tree",
            side_effect=RuntimeError("disk vanished"),
        ), patch("app.storage.scan_cloud", return_value=0), patch(
            "app.storage.validate_cloud_vault", return_value=None
        ), patch(
            "app.storage.reconcile_pending_policy_tags", return_value=0
        ), patch(
            "app.storage.sync_lifecycle_rules_for_bucket", return_value=0
        ), patch(
            "app.storage.queue_auto_uploads", return_value=0
        ), patch(
            "app.storage.queue_auto_local_cleanups", return_value=0
        ):
            result = scan_vault(dict(vault))

        self.assertEqual(result.get("local"), -1)
        self.assertEqual(self._vault_count(), 1)
        status = runtime_status.get(int(vault["id"]), {})
        self.assertIn("disk vanished", status.get("last_error") or "")
        self.assertFalse(status.get("scanning"))

    def test_descendant_symlink_findings_do_not_roll_back_adoption(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.storage import scan_tree

        # Simulate a descendant symlink without requiring OS symlink privileges.
        link = self.albums / "alias.jpg"
        link.write_bytes(b"not-followed")
        real_is_symlink = Path.is_symlink
        real_is_file = Path.is_file

        def fake_is_symlink(self: Path) -> bool:
            if self.resolve() == link.resolve():
                return True
            return real_is_symlink(self)

        def fake_is_file(self: Path) -> bool:
            if self.resolve() == link.resolve():
                return False
            return real_is_file(self)

        self._grant()
        vault = self._adopt()
        with patch.object(Path, "is_symlink", fake_is_symlink), patch.object(
            Path, "is_file", fake_is_file
        ):
            count = scan_tree(dict(vault), "scan-adopt-1")
        self.assertGreaterEqual(count, 2)

        with SQLiteConnection(str(self.database_path)) as connection:
            rows = ArchiveCatalog(connection).list_file_rows(int(vault["id"]))
        by_path = {row["path"]: row for row in rows}
        self.assertEqual(by_path["photo.jpg"]["local_file_type"], "regular")
        self.assertEqual(by_path["alias.jpg"]["local_file_type"], "symlink")
        self.assertEqual(self._vault_count(), 1)


class VaultAdoptionCryptTests(_AdoptionFixture):
    """Seam 8: crypt adoption preserves sealed secrets and owner membership."""

    def test_crypt_adoption_seals_secrets_without_moving_content(self) -> None:
        import base64

        master = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")
        crypt_settings = replace(
            self.settings,
            archive_master_key=master,
            vault_rclone_base_remote="crypt-base",
        )
        for target in (
            "app.services.vaults.settings",
            "app.services.vault_crypto.settings",
        ):
            patcher = patch(target, crypt_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self._grant()
        vault = self._adopt(encryption_mode="crypt", slug="albums-crypt")

        self.assertEqual(vault["encryption_mode"], "crypt")
        self.assertTrue(vault["crypt_password_ciphertext"])
        self.assertTrue(vault["crypt_password2_ciphertext"])
        self.assertEqual(Path(vault["source_root"]), self.albums.resolve())
        self.assertEqual((self.albums / "photo.jpg").read_bytes(), b"jpeg-bytes")
        self.assertEqual(vault["s3_prefix"], f"vaults/{vault['uuid']}/")


if __name__ == "__main__":
    unittest.main()
