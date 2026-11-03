"""Adopt an existing Source Volume directory when creating a Vault (issue #150).

Seams under test (confirmed):
1. Empty creation remains available and uses /sources/managed/<uuid>
2. Authorized adoption stores the selected absolute root; UUID/S3 prefix server-minted
3. Volume subdirectories and empty existing directories are valid candidates
4. Invalid filesystem candidates rejected with no partial Vault
5. Exact / inside / contains Vault overlap rejected (incl. concurrent + disabled)
6. Successful adoption triggers async scan; scan failure leaves retryable Vault
7. Descendant unsupported/permission findings follow existing health behavior
8. Plaintext namespace, ownership and authentication remain constrained
9. No create request can choose S3 identity, crypt secret, or arbitrary path
10. en/it API/UI errors and both create modes covered at 375px
"""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings

from app.services import source_layout
from app.services import vaults as vaults_service
from tests.test_database import run_alembic

class _AdoptionFixture(unittest.TestCase):
    """Shared Source Volume layout for adoption seams."""

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

        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash) "
                "VALUES (1, 'alice', 'Alice', 'hash')"
            )

        self.settings = replace(
            Settings(),
            database_dsn=database_dsn(str(self.database_path)),
            vault_s3_bucket="test-bucket",

        )
        for target in (
            "app.database.settings",
            "app.services.vaults.settings",
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

    def _vault_count(self) -> int:
        with PostgresConnection(str(self.database_path)) as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM vaults").fetchone()
        return int(row["total"])

    def _adopt(
        self,
        *,
        name: str = "Albums Archive",
        relative_path: str = "albums",
        volume_alias: str = "photos",
        **kwargs,
    ) -> dict:
        return vaults_service.create_vault(
            name,
            creation_mode="adopt",
            volume_alias=volume_alias,
            relative_path=relative_path,
            **kwargs,
        )


class VaultAdoptionHappyPathTests(_AdoptionFixture):
    """Seams 1–3: empty regression, authorized adoption, valid candidates."""

    def test_empty_creation_still_uses_managed_uuid_directory(self) -> None:
        vault = vaults_service.create_vault("Empty Archive")
        managed = source_layout.get_managed_root() / vault["uuid"]
        self.assertEqual(Path(vault["source_root"]), managed)
        self.assertTrue(managed.is_dir())
        self.assertEqual(vault["s3_prefix"], f"vaults/{vault['uuid']}/")

    def test_owner_adopts_existing_directory_in_place(self) -> None:
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

    def test_volume_subdirectory_and_empty_directory_are_valid_candidates(self) -> None:
        empty = self.albums / "empty-bin"
        empty.mkdir()

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
        area_root = self._adopt(name="Raw Root", relative_path="raw", slug="raw-root")
        self.assertEqual(Path(area_root["source_root"]), raw.resolve())


class VaultAdoptionRejectionTests(_AdoptionFixture):
    """Seam 4: invalid candidates leave no partial Vault."""

    def test_owner_may_adopt_any_free_volume_directory(self) -> None:
        free = self.photos / "inbox"
        free.mkdir()
        (free / "note.txt").write_text("hello", encoding="utf-8")

        vault = self._adopt(name="Inbox", relative_path="inbox")
        self.assertEqual(Path(vault["source_root"]), free.resolve())
        self.assertEqual((free / "note.txt").read_text(encoding="utf-8"), "hello")

    def test_managed_missing_symlink_and_nested_mount_are_rejected(self) -> None:

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
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, uuid, slug, name, source_root, s3_bucket, s3_prefix, enabled
                )
                VALUES (
                    1, '11111111-1111-1111-1111-111111111111', 'existing',
                    'Existing', %s, 'bucket', 'vaults/existing/', %s
                )
                """,
                (str(source_root), enabled),
            )

    def test_exact_inside_and_contains_overlap_are_rejected(self) -> None:
        child = self.albums / "2020"
        child.mkdir()
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

        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE vaults SET enabled=FALSE WHERE id=1")
        with self.assertRaises(vaults_service.VaultAdoptionError) as disabled:
            self._adopt(name="Disabled", relative_path="albums/2020")
        self.assertEqual(disabled.exception.reason, "overlap")

        self.assertEqual(self._vault_count(), 1)

    def test_concurrent_overlapping_adoptions_fail_closed(self) -> None:

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

    def _adopt_for_verified_scan(self):
        created = self._adopt()
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute("SELECT * FROM vaults WHERE id=%s", (created["id"],)).fetchone()

    def test_successful_adoption_scan_catalogs_existing_regular_file(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.storage import scan_vault

        vault = self._adopt()

        with patch("app.storage.scan_cloud", return_value=0), patch(
            "app.storage.validate_cloud_vault", return_value=None
        ), patch(
            "app.storage.sync_lifecycle_rules_for_bucket", return_value=0
        ), patch(
            "app.storage.queue_auto_uploads", return_value=0
        ), patch(
            "app.storage.queue_auto_local_cleanups", return_value=0
        ):
            result = scan_vault(dict(vault))

        self.assertGreaterEqual(result.get("local", 0), 1)
        with PostgresConnection(str(self.database_path)) as connection:
            rows = ArchiveCatalog(connection).list_file_rows(int(vault["id"]))
        by_path = {row["path"]: row for row in rows}
        self.assertIn("photo.jpg", by_path)
        self.assertEqual(by_path["photo.jpg"]["local_file_type"], "regular")
        self.assertTrue(by_path["photo.jpg"].get("upload_eligible"))

    def test_scan_failure_leaves_retryable_vault(self) -> None:
        from app.storage import runtime_status, scan_vault

        vault = self._adopt()
        runtime_status.pop(int(vault["id"]), None)

        with patch(
            "app.storage.scan_tree",
            side_effect=RuntimeError("disk vanished"),
        ), patch("app.storage.scan_cloud", return_value=0), patch(
            "app.storage.validate_cloud_vault", return_value=None
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

    def test_unreadable_file_keeps_healthy_observations_without_missing_or_rename(self) -> None:
        import os
        from app.catalog import ArchiveCatalog
        from app.storage import scan_tree, scan_vault

        vault = self._adopt_for_verified_scan()
        (self.albums / "old.jpg").write_bytes(b"old")
        scan_tree(dict(vault), "before")
        (self.albums / "old.jpg").unlink()
        (self.albums / "healthy.jpg").write_bytes(b"healthy new content")
        with PostgresConnection(str(self.database_path)) as connection:
            version_id = ArchiveCatalog(connection).record_archive_version(
                vault_id=vault["id"], path="photo.jpg", object_key="only/photo.jpg",
                provider_version_id="only-copy", size=10, storage_class="STANDARD",
                etag="etag", uploaded_at="2026-11-03", observed_at="2026-11-03",
                scan_id="before",
            )
        real_open = os.open
        def denied(path, *args, **kwargs):
            if Path(path).name == "photo.jpg":
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)
        with (
            patch("app.services.fs_preflight.os.open", side_effect=denied),
            patch("app.storage.scan_cloud", return_value=0),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client"),
            patch("app.storage.queue_auto_uploads", return_value=0),
            patch("app.storage.queue_auto_local_cleanups", return_value=0),
            patch.object(ArchiveCatalog, "list_rename_candidates") as renames,
        ):
            result = scan_vault(dict(vault))
        renames.assert_not_called()
        self.assertEqual(result["local"], 1)
        self.assertEqual(result["local_incomplete"], 1)
        with PostgresConnection(str(self.database_path)) as connection:
            problems = connection.execute("SELECT event, message_params FROM notifications").fetchall()
            self.assertTrue(any(p["event"] == "file_unreadable" and "photo.jpg" in p["message_params"] for p in problems))
            self.assertFalse(any(p["event"] == "protection_blocked" for p in problems))
            rows = {r["path"]: r for r in ArchiveCatalog(connection).list_file_rows(vault["id"])}
            self.assertEqual(set(rows), {"photo.jpg", "old.jpg", "healthy.jpg"})
            self.assertTrue(all(r["local_exists"] for r in rows.values()))
            self.assertTrue(rows["healthy.jpg"]["upload_eligible"])
            self.assertEqual(connection.execute("SELECT availability FROM archive_versions WHERE id=%s", (version_id,)).fetchone()["availability"], "available")

    def test_incomplete_scan_cannot_release_relocation_recovery(self) -> None:
        import os
        from app.storage import _verified_local_scan

        vault = self._adopt_for_verified_scan()
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE vaults SET relocation_state='scan_required' WHERE id=%s", (vault["id"],))
        vault["relocation_state"] = "scan_required"
        real_open = os.open
        def denied(path, *args, **kwargs):
            if Path(path).name == "photo.jpg":
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)
        with patch("app.services.fs_preflight.os.open", side_effect=denied):
            *_, complete = _verified_local_scan(vault, "incomplete")
        self.assertFalse(complete)
        # A later full-content read can fail even after the initial byte check.
        with patch("app.storage.hash_stable_regular_file", side_effect=PermissionError("denied")):
            *_, complete = _verified_local_scan(vault, "hash-incomplete")
        self.assertFalse(complete)
        with PostgresConnection(str(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT relocation_state FROM vaults WHERE id=%s", (vault["id"],)).fetchone()["relocation_state"], "scan_required")
        *_, complete = _verified_local_scan(vault, "complete")
        self.assertTrue(complete)
        with PostgresConnection(str(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT relocation_state FROM vaults WHERE id=%s", (vault["id"],)).fetchone()["relocation_state"], "ready")

    def test_incomplete_scan_still_holds_and_notifies_cloud_only_conflicts(self) -> None:
        import hashlib
        import os
        from app.catalog import ArchiveCatalog
        from app.services.operation_policies import OperationPolicy, set_policy
        from app.storage import scan_tree, scan_vault

        vault = self._adopt_for_verified_scan()
        scan_tree(dict(vault), "before")
        with PostgresConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            version_id = catalog.record_archive_version(
                vault_id=vault["id"], path="photo.jpg", object_key="only/photo.jpg",
                provider_version_id="only-copy", size=10, storage_class="STANDARD",
                etag="etag", uploaded_at="2026-11-03", observed_at="2026-11-03", scan_id="before",
            )
            digest = hashlib.sha256(b"jpeg-bytes").hexdigest()
            catalog.mark_version_verified(version_id, plaintext_sha256=digest, verified_at="2026-11-03")
            catalog.set_local_fingerprint(vault_id=vault["id"], path="photo.jpg", plaintext_sha256=digest, matched_archive_version_id=version_id)
            file_id = connection.execute("SELECT vault_file_id FROM archive_versions WHERE id=%s", (version_id,)).fetchone()["vault_file_id"]
            catalog.mark_local_copy_missing(file_id, observed_at="2026-11-03")
            set_policy(connection, vault["id"], OperationPolicy(auto_upload=True, stability_seconds=0))
        (self.albums / "photo.jpg").write_bytes(b"different content")
        (self.albums / "denied.jpg").write_bytes(b"unreadable")
        real_open = os.open
        def denied(path, *args, **kwargs):
            if Path(path).name == "denied.jpg":
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)
        with (
            patch("app.services.fs_preflight.os.open", side_effect=denied),
            patch("app.storage.scan_cloud", return_value=0),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client") as cloud,
        ):
            result = scan_vault(dict(vault))
        self.assertEqual(result["local_incomplete"], 1)
        self.assertEqual(result["auto_uploads"], 0)
        cloud.return_value.delete_object.assert_not_called()
        with PostgresConnection(str(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT availability FROM archive_versions WHERE id=%s", (version_id,)).fetchone()["availability"], "available")
            events = {r["event"] for r in connection.execute("SELECT event FROM notifications").fetchall()}
            self.assertLessEqual({"file_unreadable", "cloud_only_conflict"}, events)
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 0)

    def test_descendant_symlink_findings_do_not_roll_back_adoption(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.storage import scan_tree

        link = self.albums / "alias.jpg"
        link.symlink_to(self.albums / "photo.jpg")
        vault = self._adopt()
        count = scan_tree(dict(vault), "scan-adopt-1")
        self.assertGreaterEqual(count, 2)

        with PostgresConnection(str(self.database_path)) as connection:
            rows = ArchiveCatalog(connection).list_file_rows(int(vault["id"]))
        by_path = {row["path"]: row for row in rows}
        self.assertEqual(by_path["photo.jpg"]["local_file_type"], "regular")
        self.assertEqual(by_path["alias.jpg"]["local_file_type"], "symlink")
        self.assertEqual(self._vault_count(), 1)


if __name__ == "__main__":
    unittest.main()
