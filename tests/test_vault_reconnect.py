"""Reconnect safety: explicit per-Vault identity, cloud-first scan and no deletion."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.config import Settings
from app.services import source_layout, source_identity, vault_relocation
from app.services.recovery_kit import kit_status, record_metadata_restore
from tests.postgres import PostgresConnection, database_dsn
from tests.test_database import run_alembic


class ListingS3:
    def __init__(self, content=None):
        self.content = content or {}
        self.deleted = []
        self.reads = []

    def get_paginator(self, _name):
        versions = [dict(Key="vaults/11111111-1111-1111-1111-111111111111/" + path, VersionId="new-" + path,
                         Size=len(data), StorageClass="STANDARD", ETag="etag",
                         LastModified=datetime(2026, 11, 3, 10, tzinfo=timezone.utc))
                    for path, data in self.content.items()]
        return SimpleNamespace(paginate=lambda **kwargs: iter([{"Versions": versions}]))

    def get_object(self, **kwargs):
        self.reads.append(kwargs)
        path = kwargs["Key"].removeprefix("vaults/11111111-1111-1111-1111-111111111111/")
        return {"VersionId": "new-" + path, "Body": io.BytesIO(self.content[path])}

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)
        raise AssertionError("Reconnect must never delete S3 content")


OTHER_VAULT = """INSERT INTO vaults(id, target_id, name, source_root)
    VALUES (8, (SELECT id FROM vault_targets WHERE bucket='bucket'), 'Other', %s)"""


class VaultReconnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog"
        result = run_alembic(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settings = replace(Settings(), database_dsn=database_dsn(str(self.db_path)))
        patcher = patch("app.database.settings", self.settings)
        patcher.start(); self.addCleanup(patcher.stop)
        self.sources = Path(self.tmp.name) / "sources"
        self.volume = self.sources / "photos"
        self.old = self.volume / "old"
        self.old.mkdir(parents=True)
        (self.sources / "managed").mkdir()
        self.new = self.volume / "new"
        self.new.mkdir()
        source_layout.override_sources_root(self.sources)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.addCleanup(vault_relocation._runtime_suspended_vaults.discard, 7)
        mount = patch.object(source_layout, "path_is_mount", side_effect=lambda p: Path(p) in {self.sources, self.volume})
        mount.start(); self.addCleanup(mount.stop)
        identity = patch.object(source_identity, "fingerprint_for_mount", return_value="mount-a")
        self.identity = identity.start(); self.addCleanup(identity.stop)
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')")
            connection.execute("INSERT INTO vault_targets(provider, bucket, region) VALUES ('s3', 'bucket', 'eu-south-1')")
            connection.execute("""INSERT INTO vaults(id, uuid, target_id, name, source_root)
                VALUES (7, '11111111-1111-1111-1111-111111111111',
                        (SELECT id FROM vault_targets WHERE bucket='bucket'), 'Photos', %s)""", (str(self.old),))
            vault_relocation.enroll_vault_root_identity(connection, 7, str(self.old))
        source_layout.reconcile_source_volume_identities()

    def row(self):
        with PostgresConnection(str(self.db_path)) as connection:
            return connection.execute("SELECT * FROM vaults WHERE id=7").fetchone()

    def reconnect(self, **overrides):
        args = dict(vault_id=7, volume_alias="photos", relative_path="new", actor="owner", reason="restored server")
        args.update(overrides)
        with PostgresConnection(str(self.db_path)) as connection:
            return vault_relocation.reconnect_vault_root(connection, **args)

    def scan(self, vault=None, s3=None):
        from app.storage import scan_vault
        with (patch("app.storage.validate_cloud_vault"),
              patch("app.storage.s3_client", return_value=s3 or ListingS3()),
              patch("app.storage.sync_lifecycle_rules_for_bucket", return_value=0),
              patch("app.storage.queue_auto_uploads", return_value=0),
              patch("app.storage.queue_auto_local_cleanups", return_value=0)):
            return scan_vault(dict(vault or self.row()))

    def test_explicit_different_folder_even_when_old_exists_is_audited(self):
        before = self.row()
        result = self.reconnect()
        self.assertEqual(result["source_root"], str(self.new))
        self.assertEqual(result["lifecycle"], "reconnect_required")
        self.assertEqual(result["reconnect_volume_fingerprint"], "mount-a")
        for field in ("id", "uuid", "target_id"):
            self.assertEqual(result[field], before[field])
        with PostgresConnection(str(self.db_path)) as connection:
            event = connection.execute("SELECT * FROM audit_events WHERE event='vault_folder_reconnected'").fetchone()
            self.assertEqual(event["detail"]["reason"], "restored server")
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM notifications").fetchone()["n"], 0)

    def test_new_source_volume_and_missing_old_identity_are_accepted_explicitly(self):
        other = self.sources / "newdisk"
        other.mkdir()
        (other / "restored").mkdir()
        with patch.object(source_layout, "path_is_mount", side_effect=lambda p: Path(p) in {self.sources, self.volume, other}):
            with PostgresConnection(str(self.db_path)) as connection:
                connection.execute("UPDATE vaults SET root_identity=NULL, root_identity_version=NULL WHERE id=7")
            result = self.reconnect(volume_alias="newdisk", relative_path="restored")
        self.assertEqual(result["source_root"], str(other / "restored"))

    def test_replacement_acceptance_does_not_unblock_sibling_vault(self):
        sibling = self.volume / "sibling"
        sibling.mkdir()
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute(OTHER_VAULT, (str(sibling),))
        self.identity.return_value = "mount-b"
        source_layout.reconcile_source_volume_identities()
        self.assertFalse(source_layout.vault_local_access(str(self.old)).local_operations_allowed)
        self.reconnect()
        self.assertTrue(source_layout.vault_local_access(str(self.new)).local_operations_allowed)
        self.assertFalse(source_layout.vault_local_access(str(sibling)).local_operations_allowed)
        self.identity.return_value = "mount-c"
        self.assertFalse(source_layout.vault_local_access(str(self.new)).local_operations_allowed)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT expected_fingerprint FROM source_volumes WHERE alias='photos'").fetchone()["expected_fingerprint"], "mount-a")

    def test_containment_overlap_identity_and_busy_rejection_matrix(self):
        linked = self.volume / "linked"
        linked.symlink_to(self.new, target_is_directory=True)
        for overrides, reason in [
            ({"relative_path": "../new"}, "invalid_path"),
            ({"relative_path": "/new"}, "invalid_path"),
            ({"relative_path": "new\0x"}, "invalid_path"),
            ({"relative_path": "missing"}, "inaccessible"),
            ({"relative_path": "linked"}, "symlink"),
            ({"volume_alias": "../photos"}, "volume_unavailable"),
            ({"volume_alias": "managed"}, "managed_not_own"),
            ({"volume_alias": "managed", "relative_path": "11111111-1111-1111-1111-111111111111"}, "managed_not_own"),
            ({"runtime_busy": True}, "active_jobs"),
        ]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(vault_relocation.VaultRelocationError) as error:
                    self.reconnect(**overrides)
                self.assertEqual(error.exception.reason, reason)
                self.assertEqual(self.row()["source_root"], str(self.old))
        with patch.object(source_identity, "fingerprint_for_mount", side_effect=source_identity.MountIdentityError("ambiguous")):
            with self.assertRaises(vault_relocation.VaultRelocationError): self.reconnect()
        nested = self.new / "nested"
        nested.mkdir()
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute(OTHER_VAULT, (str(nested),))
        with self.assertRaises(vault_relocation.VaultRelocationError) as error: self.reconnect()
        self.assertEqual(error.exception.reason, "overlap")

    def test_active_job_and_stopped_protection_block_reconnect(self):
        with PostgresConnection(str(self.db_path)) as connection:
            file_id = ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="a", file_type="regular", size=1, mtime_ns=1, observed_at="2026-11-03T00:00:00Z")
            connection.execute("INSERT INTO jobs(vault_id,file_id,action,status) VALUES (7,%s,'upload','queued')", (file_id,))
        with self.assertRaises(vault_relocation.VaultRelocationError) as error: self.reconnect()
        self.assertEqual(error.exception.reason, "active_jobs")
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE jobs SET status='completed'")
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=7")
        with self.assertRaises(vault_relocation.VaultRelocationError) as error: self.reconnect()
        self.assertEqual(error.exception.reason, "protection_stopped")

    def test_handoff_failure_rolls_back_root_and_acceptance(self):
        def fail(): raise RuntimeError("handoff failed")
        with self.assertRaises(RuntimeError): self.reconnect(after_update=fail)
        row = self.row()
        self.assertEqual(row["source_root"], str(self.old))
        self.assertEqual(row["lifecycle"], "active")
        self.assertIsNone(row["reconnect_volume_fingerprint"])
        self.assertFalse(vault_relocation.local_work_suspended(row))

    def test_scan_lock_serializes_reconnect_handoff(self):
        from fastapi import BackgroundTasks
        from app.main import VaultReconnect, reconnect_vault_root
        from app.storage import scan_vault
        entered, proceed = threading.Event(), threading.Event()
        original = vault_relocation.reconnect_vault_root
        outcome = []
        def delayed(connection, **kwargs):
            entered.set(); self.assertTrue(proceed.wait(5))
            return original(connection, **kwargs)
        def invoke():
            try:
                request = SimpleNamespace(state=SimpleNamespace(session={"auth_method": "oidc"}))
                outcome.append(reconnect_vault_root(7, VaultReconnect(volume_alias="photos", relative_path="new", reason="restore"), BackgroundTasks(), request, {"id": 1}))
            except BaseException as exc: outcome.append(exc)
        with patch("app.main.vault_relocation_service.reconnect_vault_root", side_effect=delayed):
            thread = threading.Thread(target=invoke); thread.start()
            self.assertTrue(entered.wait(5))
            self.assertEqual(scan_vault({"id": 7}), {"already_running": 1})
            proceed.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], dict)

    def test_cloud_failure_leaves_scan_suspended_and_local_catalog_untouched(self):
        result = self.reconnect()
        with patch("app.storage.scan_cloud", side_effect=RuntimeError("failure")), patch("app.storage.scan_tree") as tree:
            scanned = self.scan(result)
        tree.assert_not_called()
        self.assertEqual(scanned["reconnect_suspended"], 1)
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")

    def test_replaced_or_symlink_root_is_rejected_before_cloud_or_local_scan(self):
        from app.storage import scan_vault
        result = self.reconnect()
        self.new.rename(self.volume / "original")
        self.new.mkdir()
        with patch("app.storage.scan_tree") as tree, patch("app.storage.scan_cloud") as cloud:
            scanned = scan_vault(result)
        tree.assert_not_called(); cloud.assert_not_called()
        self.assertEqual(scanned["root_identity_mismatch"], 1)
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")

    def test_root_swap_during_walk_rolls_back_and_never_resumes(self):
        self.reconnect()
        def swap(*args, **kwargs):
            self.new.rename(self.volume / "original"); self.new.mkdir()
            return 0, None
        with patch("app.storage._scan_tree", side_effect=swap): result = self.scan()
        self.assertEqual(result["root_identity_mismatch"], 1)
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")

    def test_root_symlink_swap_is_rejected_before_cloud_or_local_scan(self):
        from app.storage import scan_vault
        vault = self.reconnect()
        original = self.volume / "original"
        self.new.rename(original)
        self.new.symlink_to(original, target_is_directory=True)
        with patch("app.storage.scan_cloud") as cloud, patch("app.storage.scan_tree") as tree:
            result = scan_vault(vault)
        self.assertEqual(result["root_identity_mismatch"], 1)
        cloud.assert_not_called(); tree.assert_not_called()
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")

    def test_scan_mutations_roll_back_after_root_changes_during_walk(self):
        self.reconnect()
        def mutate_then_swap(connection, vault, scan_id, **kwargs):
            ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="unrelated.txt", file_type="regular", size=9, mtime_ns=1, seen_at=scan_id, observed_at=scan_id)
            self.new.rename(self.volume / "original"); self.new.mkdir()
            return 1, None
        with patch("app.storage._scan_tree", side_effect=mutate_then_swap): result = self.scan()
        self.assertEqual(result["root_identity_mismatch"], 1)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM files WHERE vault_id=7").fetchone()["n"], 0)
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")

    def test_finalization_mutations_roll_back_after_late_root_change(self):
        self.reconnect()
        def mutate_then_swap(connection, vault, **kwargs):
            file_id = ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="unrelated.txt", file_type="regular", size=9, mtime_ns=1, observed_at="2026-11-03T00:00:00Z")
            connection.execute("INSERT INTO jobs(vault_id,file_id,action,status) VALUES (7,%s,'upload','queued')", (file_id,))
            self.new.rename(self.volume / "original"); self.new.mkdir()
            return {"hashed": 1, "confirmed": 0, "queued": 1}
        with patch("app.storage._apply_auto_renames", side_effect=mutate_then_swap): result = self.scan()
        self.assertEqual(result["root_identity_mismatch"], 1)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM files WHERE vault_id=7").fetchone()["n"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM jobs WHERE vault_id=7").fetchone()["n"], 0)
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")

    def test_manual_restore_invalidates_local_authority_and_stale_jobs(self):
        from app.storage import scan_vault
        with PostgresConnection(str(self.db_path)) as connection:
            file_id = ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="a", file_type="regular", size=1, mtime_ns=1, observed_at="2026-11-03T00:00:00Z")
            connection.execute("INSERT INTO jobs(vault_id,file_id,action,status) VALUES (7,%s,'recover','queued')", (file_id,))
            record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")
            job = connection.execute("SELECT status, finished_at FROM jobs").fetchone()
            self.assertEqual(job["status"], "cancelled")
            self.assertIsNotNone(job["finished_at"])
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")
        self.assertEqual(vault_relocation.reconcile_vault_root_identities(), 0)
        with patch("app.storage.scan_cloud") as cloud, patch("app.storage.scan_tree") as tree:
            result = scan_vault(self.row())
        self.assertEqual(result["root_identity_mismatch"], 1)
        tree.assert_not_called(); cloud.assert_not_called()
        self.assertIsNone(self.row()["root_identity"])
        self.reconnect()  # No stale Job can strand reconnect indefinitely.

    def test_manual_restore_withdraws_deletion_consent_without_starving_later_uploads(self):
        with PostgresConnection(str(self.db_path)) as connection:
            file_id = ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="a", file_type="regular", size=1, mtime_ns=1, observed_at="2026-11-03T00:00:00Z")
            group = connection.execute("INSERT INTO job_groups(vault_id) VALUES (7) RETURNING id").fetchone()["id"]
            connection.execute("INSERT INTO cloud_purge_requests(group_id, reason) VALUES (%s, 'no longer needed')", (group,))
            connection.execute("""INSERT INTO jobs(vault_id,file_id,group_id,action,status,run_after)
                VALUES (7,%s,%s,'cloud-purge','pending_delay', now() + interval '7 days')""", (file_id, group))
            connection.execute("UPDATE vaults SET paused=true WHERE id=7")
            record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")
            purge = connection.execute("SELECT status, message FROM jobs WHERE action='cloud-purge'").fetchone()
            self.assertEqual(purge["status"], "cancelled")
            self.assertIn("Metadata restored", purge["message"])
            # The withdrawn consent leaves no active work holding the file.
            connection.execute("INSERT INTO jobs(vault_id,file_id,action,status) VALUES (7,%s,'upload','queued')", (file_id,))
            vault = connection.execute("SELECT lifecycle, paused FROM vaults WHERE id=7").fetchone()
        self.assertEqual((vault["lifecycle"], vault["paused"]), ("reconnect_required", True))

    def test_manual_restore_keeps_a_stopping_vault_stopped(self):
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=7")
            record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM metadata_restore_vaults").fetchone()["n"], 0)
        self.assertEqual(self.row()["lifecycle"], "stopping")

    def test_stale_catalog_reconnect_protected_cloud_only_conflict_no_cloud_deletes(self):
        # A is the restored catalog; only B remains remotely after a verified
        # replacement. Even identical local size/mtime cannot retain A's digest.
        content = {"same.jpg": b"new!", "missing.jpg": b"only", "conflict.jpg": b"safe"}
        (self.new / "same.jpg").write_bytes(content["same.jpg"])
        (self.new / "conflict.jpg").write_bytes(b"oops")
        old_digest = hashlib.sha256(b"old!").digest()
        with PostgresConnection(str(self.db_path)) as connection:
            catalog = ArchiveCatalog(connection)
            for path in content:
                local = self.new / path
                stat = local.stat() if local.exists() else SimpleNamespace(st_size=4, st_mtime_ns=1)
                file_id = catalog.observe_local_copy(vault_id=7, path=path, file_type="regular", size=stat.st_size,
                                                     mtime_ns=stat.st_mtime_ns, observed_at="2026-11-01T10:00:00+00:00")
                version = catalog.begin_replacement(file_id, object_key="vaults/11111111-1111-1111-1111-111111111111/" + path,
                    provider_version_id="old-" + path, size=4, storage_class="STANDARD", created_at="2026-11-01T10:00:00+00:00")
                catalog.promote_replacement(file_id, archive_version_id=version, plaintext_sha256=old_digest,
                                            verified_at="2026-11-01T10:00:00+00:00")
                connection.execute("UPDATE files SET local_sha256=%s, matched_version_id=current_version_id WHERE id=%s",
                                   (old_digest, file_id))
            record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")
        s3 = ListingS3(content)
        reconnected = self.reconnect()
        result = self.scan(reconnected, s3)
        self.assertEqual(result["local"], 2, result)
        self.assertEqual(s3.deleted, [])
        self.assertEqual(len(s3.reads), 3)
        self.assertTrue(all(read["VersionId"] == "new-" + read["Key"].rsplit("/", 1)[1] for read in s3.reads))
        with PostgresConnection(str(self.db_path)) as connection:
            rows = {r["path"]: r for r in ArchiveCatalog(connection).list_file_rows(7)}
            self.assertEqual(rows["same.jpg"]["state"], "protected")
            self.assertEqual(rows["missing.jpg"]["state"], "cloud_only")
            self.assertEqual(rows["conflict.jpg"]["state"], "problem")
            self.assertFalse(rows["conflict.jpg"]["upload_eligible"])
            notice = kit_status(connection)["metadata_restore"]
            self.assertEqual(notice["backup_created_at"], "2026-11-02T10:00:00+00:00")
            self.assertEqual(notice["updated_files"], 3)
            current = connection.execute(
                "SELECT av.provider_version_id, av.availability, av.integrity, av.verification_method "
                "FROM files f JOIN archive_versions av ON av.id=f.current_version_id WHERE f.vault_id=7").fetchall()
            self.assertEqual(sorted(v["provider_version_id"] for v in current), ["new-conflict.jpg", "new-missing.jpg", "new-same.jpg"])
            self.assertTrue(all((v["availability"], v["integrity"], v["verification_method"]) == ("available", "verified", "read_back") for v in current))
            # The restored versions were not deleted: they stay as superseded rows.
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM archive_versions WHERE provider_version_id LIKE 'old-%'").fetchone()["n"], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM metadata_restore_vaults").fetchone()["n"], 0)
        self.assertEqual(self.row()["lifecycle"], "active")
        self.scan(s3=s3)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(kit_status(connection)["metadata_restore"]["updated_files"], 3)

    def test_restore_reconciliation_never_imports_unknown_paths_or_cold_versions(self):
        content = {"known.jpg": b"new!", "unknown.jpg": b"else"}
        with PostgresConnection(str(self.db_path)) as connection:
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(vault_id=7, path="known.jpg", file_type="regular", size=4,
                                                 mtime_ns=1, observed_at="2026-11-01T10:00:00+00:00")
            version = catalog.begin_replacement(file_id, object_key="vaults/11111111-1111-1111-1111-111111111111/known.jpg",
                provider_version_id="old-known.jpg", size=4, storage_class="STANDARD")
            catalog.promote_replacement(file_id, archive_version_id=version,
                                        plaintext_sha256=hashlib.sha256(b"old!").digest(), verified_at="2026-11-01T10:00:00+00:00")
            record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")
        s3 = ListingS3(content)
        cold = s3.get_paginator
        def glacier(_name):
            pages = list(cold(_name).paginate())
            for item in pages[0]["Versions"]:
                item["StorageClass"] = "DEEP_ARCHIVE"
            return SimpleNamespace(paginate=lambda **kwargs: iter(pages))
        s3.get_paginator = glacier
        result = self.scan(self.reconnect(), s3)
        self.assertEqual(result["reconnect_suspended"], 1)
        self.assertEqual(s3.reads, [])
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")
        s3.get_paginator = cold
        self.scan(s3=s3)
        self.assertEqual([read["Key"].rsplit("/", 1)[1] for read in s3.reads], ["known.jpg"])
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual([r["path"] for r in connection.execute("SELECT path FROM files WHERE vault_id=7")], ["known.jpg"])
            self.assertEqual(kit_status(connection)["metadata_restore"]["updated_files"], 1)
        self.assertEqual(self.row()["lifecycle"], "active")


MANAGED_UUID = "11111111-1111-1111-1111-111111111111"
OTHER_UUID = "22222222-2222-2222-2222-222222222222"


class ManagedVaultReconnectTests(unittest.TestCase):
    """#641: a restored managed Vault reconnects to its own managed folder only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog"
        result = run_alembic(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settings = replace(Settings(), database_dsn=database_dsn(str(self.db_path)))
        patcher = patch("app.database.settings", self.settings)
        patcher.start(); self.addCleanup(patcher.stop)
        self.sources = Path(self.tmp.name) / "sources"
        self.managed = self.sources / "managed"
        self.own = self.managed / MANAGED_UUID
        self.other = self.managed / OTHER_UUID
        self.own.mkdir(parents=True)
        self.other.mkdir()
        source_layout.override_sources_root(self.sources)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.addCleanup(vault_relocation._runtime_suspended_vaults.discard, 7)
        mount = patch.object(source_layout, "path_is_mount", side_effect=lambda p: Path(p) == self.sources)
        self.mount = mount.start(); self.addCleanup(mount.stop)
        identity = patch.object(source_identity, "fingerprint_for_mount", return_value="sources-mount")
        self.identity = identity.start(); self.addCleanup(identity.stop)
        boundary = patch.object(source_identity, "validate_root_mount_boundary")
        self.boundary = boundary.start(); self.addCleanup(boundary.stop)
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')")
            connection.execute("INSERT INTO vault_targets(provider, bucket, region) VALUES ('s3', 'bucket', 'eu-south-1')")
            for vault_id, vault_uuid, name, root in ((7, MANAGED_UUID, "Photos", self.own),
                                                     (8, OTHER_UUID, "Other", self.other)):
                connection.execute("""INSERT INTO vaults(id, uuid, target_id, name, source_root)
                    VALUES (%s, %s, (SELECT id FROM vault_targets WHERE bucket='bucket'), %s, %s)""",
                    (vault_id, vault_uuid, name, str(root)))
                vault_relocation.enroll_vault_root_identity(connection, vault_id, str(root))

    def row(self, vault_id=7):
        with PostgresConnection(str(self.db_path)) as connection:
            return connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()

    def reconnect(self, **overrides):
        args = dict(vault_id=7, volume_alias="managed", relative_path=MANAGED_UUID,
                    actor="owner", reason="restored backup")
        args.update(overrides)
        with PostgresConnection(str(self.db_path)) as connection:
            return vault_relocation.reconnect_vault_root(connection, **args)

    def refused(self, reason, **overrides):
        with self.assertRaises(vault_relocation.VaultRelocationError) as error:
            self.reconnect(**overrides)
        self.assertEqual(error.exception.reason, reason)
        row = self.row()
        self.assertEqual(row["source_root"], str(self.own))
        self.assertFalse(7 in vault_relocation._runtime_suspended_vaults)

    def restore(self, content):
        digest = hashlib.sha256(b"old!").digest()
        with PostgresConnection(str(self.db_path)) as connection:
            catalog = ArchiveCatalog(connection)
            for path in content:
                file_id = catalog.observe_local_copy(vault_id=7, path=path, file_type="regular", size=4,
                                                     mtime_ns=1, observed_at="2026-11-01T10:00:00+00:00")
                version = catalog.begin_replacement(file_id, object_key=f"vaults/{MANAGED_UUID}/" + path,
                    provider_version_id="old-" + path, size=4, storage_class="STANDARD",
                    created_at="2026-11-01T10:00:00+00:00")
                catalog.promote_replacement(file_id, archive_version_id=version, plaintext_sha256=digest,
                                            verified_at="2026-11-01T10:00:00+00:00")
            record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")

    def scan(self, vault, s3):
        from app.storage import scan_vault
        with (patch("app.storage.validate_cloud_vault"),
              patch("app.storage.s3_client", return_value=s3),
              patch("app.storage.sync_lifecycle_rules_for_bucket", return_value=0),
              patch("app.storage.queue_auto_uploads", return_value=0),
              patch("app.storage.queue_auto_local_cleanups", return_value=0)):
            return scan_vault(dict(vault))

    def test_restored_managed_vault_reconnects_to_own_folder_and_becomes_protected(self):
        content = {"photo.jpg": b"new!", "gone.jpg": b"only"}
        (self.own / "photo.jpg").write_bytes(content["photo.jpg"])
        self.restore(content)
        self.assertEqual(self.row()["lifecycle"], "reconnect_required")
        reconnected = self.reconnect()
        self.assertEqual(reconnected["source_root"], str(self.own))
        self.assertEqual(reconnected["lifecycle"], "reconnect_required")
        self.assertIsNone(reconnected["reconnect_volume_fingerprint"])
        self.assertEqual(reconnected["root_identity"], vault_relocation.root_identity(self.own))
        self.boundary.assert_called_with(self.sources, self.own)
        with PostgresConnection(str(self.db_path)) as connection:
            event = connection.execute(
                "SELECT detail FROM audit_events WHERE event='vault_folder_reconnected'").fetchone()
        self.assertEqual(event["detail"]["source_root"], str(self.own))
        self.assertTrue(event["detail"]["mandatory_full_scan"])
        s3 = ListingS3(content)
        result = self.scan(reconnected, s3)
        self.assertEqual(result["local"], 1, result)
        self.assertEqual(s3.deleted, [])
        self.assertEqual(self.row()["lifecycle"], "active")
        with PostgresConnection(str(self.db_path)) as connection:
            rows = {r["path"]: r for r in ArchiveCatalog(connection).list_file_rows(7)}
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS n FROM metadata_restore_vaults WHERE vault_id=7").fetchone()["n"], 0)
        self.assertEqual(rows["photo.jpg"]["state"], "protected")
        self.assertEqual(rows["gone.jpg"]["state"], "cloud_only")
        # The sibling managed Vault stays suspended until it is reconnected too.
        self.assertEqual(self.row(8)["lifecycle"], "reconnect_required")

    def test_other_vault_managed_folder_or_arbitrary_path_is_refused(self):
        self.restore({})
        self.refused("managed_not_own", relative_path=OTHER_UUID)
        self.refused("managed_not_own", relative_path="")
        self.refused("managed_not_own", relative_path=f"{MANAGED_UUID}/sub")
        self.refused("managed_not_own", relative_path="../photos")

    def test_custom_volume_vault_cannot_move_into_managed(self):
        custom = self.sources / "photos" / "dir"
        custom.mkdir(parents=True)
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET source_root=%s WHERE id=7", (str(custom),))
        with self.assertRaises(vault_relocation.VaultRelocationError) as error:
            self.reconnect()
        self.assertEqual(error.exception.reason, "managed_not_own")
        self.assertEqual(self.row()["source_root"], str(custom))

    def test_missing_or_symlinked_own_folder_is_refused_and_never_created(self):
        self.restore({})
        moved = self.managed / "moved"
        self.own.rename(moved)
        self.refused("managed_missing")
        self.assertFalse(self.own.exists())
        self.own.symlink_to(moved, target_is_directory=True)
        self.refused("managed_missing")
        self.own.unlink()
        self.own.write_bytes(b"not a directory")
        self.refused("managed_missing")

    def test_nested_mount_or_unavailable_sources_identity_fails_closed(self):
        self.restore({})
        self.mount.side_effect = lambda p: Path(p) in {self.sources, self.own}
        self.refused("different_volume")
        self.mount.side_effect = lambda p: Path(p) == self.sources
        self.boundary.side_effect = source_identity.MountIdentityError("nested mounts are unsupported")
        self.refused("identity_ambiguous")
        self.boundary.side_effect = None
        self.identity.side_effect = source_identity.MountIdentityError("absent")
        self.refused("identity_ambiguous")
        self.identity.side_effect = ["sources-mount", "replaced"]
        self.refused("identity_mismatch")
        self.identity.side_effect = None
        self.assertEqual(self.reconnect()["source_root"], str(self.own))

    def test_active_jobs_still_block_managed_reconnect(self):
        self.refused("active_jobs", runtime_busy=True)


if __name__ == "__main__": unittest.main()
