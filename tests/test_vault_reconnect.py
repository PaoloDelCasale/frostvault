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
        versions = [dict(Key="vaults/immutable/" + path, VersionId="new-" + path,
                         Size=len(data), StorageClass="STANDARD", ETag="etag",
                         LastModified=datetime(2026, 11, 3, 10, tzinfo=timezone.utc))
                    for path, data in self.content.items()]
        return SimpleNamespace(paginate=lambda **kwargs: iter([{"Versions": versions}]))

    def get_object(self, **kwargs):
        self.reads.append(kwargs)
        path = kwargs["Key"].removeprefix("vaults/immutable/")
        return {"VersionId": "new-" + path, "Body": io.BytesIO(self.content[path])}

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)
        raise AssertionError("Reconnect must never delete S3 content")


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
            connection.execute("""INSERT INTO vaults(id,uuid,slug,name,source_root,s3_bucket,s3_prefix)
                VALUES (7,'11111111-1111-1111-1111-111111111111','photos','Photos',%s,'bucket','vaults/immutable/')""", (str(self.old),))
            vault_relocation.enroll_vault_root_identity(connection, 7, str(self.old))
        source_layout.reconcile_source_volume_identities()

    def row(self):
        with PostgresConnection(str(self.db_path)) as connection:
            return connection.execute("SELECT * FROM vaults WHERE id=7").fetchone()

    def reconnect(self, **overrides):
        args = dict(vault_id=7, volume_alias="photos", relative_path="new", actor_user_id=1, reason="restored server")
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
        self.assertEqual(result["relocation_state"], "scan_required")
        self.assertEqual(result["reconnect_volume_fingerprint"], "mount-a")
        for field in ("id", "uuid", "s3_bucket", "s3_prefix"):
            self.assertEqual(result[field], before[field])
        with PostgresConnection(str(self.db_path)) as connection:
            event = connection.execute("SELECT * FROM audit_events WHERE event='vault_folder_reconnected'").fetchone()
            self.assertEqual(json.loads(event["detail_json"])["reason"], "restored server")
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM notifications").fetchone()["n"], 0)

    def test_new_source_volume_and_missing_old_identity_are_accepted_explicitly(self):
        other = self.sources / "newdisk"
        other.mkdir()
        (other / "restored").mkdir()
        with patch.object(source_layout, "path_is_mount", side_effect=lambda p: Path(p) in {self.sources, self.volume, other}):
            with PostgresConnection(str(self.db_path)) as connection:
                connection.execute("UPDATE vaults SET root_identity=NULL WHERE id=7")
            result = self.reconnect(volume_alias="newdisk", relative_path="restored")
        self.assertEqual(result["source_root"], str(other / "restored"))

    def test_replacement_acceptance_does_not_unblock_sibling_vault(self):
        sibling = self.volume / "sibling"
        sibling.mkdir()
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (8,'other','Other',%s,'bucket','other/')", (str(sibling),))
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
            ({"volume_alias": "managed"}, "volume_unavailable"),
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
            connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (8,'other','Other',%s,'b','other/')", (str(nested),))
        with self.assertRaises(vault_relocation.VaultRelocationError) as error: self.reconnect()
        self.assertEqual(error.exception.reason, "overlap")

    def test_active_job_and_stopped_protection_block_reconnect(self):
        with PostgresConnection(str(self.db_path)) as connection:
            file_id = ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="a", file_type="regular", size=1, mtime_ns=1, observed_at="2026-11-03T00:00:00Z")
            connection.execute("INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at) VALUES (7,%s,'a','upload','queued','now','now')", (file_id,))
        with self.assertRaises(vault_relocation.VaultRelocationError) as error: self.reconnect()
        self.assertEqual(error.exception.reason, "active_jobs")
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE jobs SET status='completed'")
            connection.execute("UPDATE vaults SET decommission_state='decommissioning' WHERE id=7")
        with self.assertRaises(vault_relocation.VaultRelocationError) as error: self.reconnect()
        self.assertEqual(error.exception.reason, "protection_stopped")

    def test_handoff_failure_rolls_back_root_and_acceptance(self):
        def fail(): raise RuntimeError("handoff failed")
        with self.assertRaises(RuntimeError): self.reconnect(after_update=fail)
        row = self.row()
        self.assertEqual(row["source_root"], str(self.old))
        self.assertEqual(row["relocation_state"], "ready")
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
                outcome.append(reconnect_vault_root(7, VaultReconnect(volume_alias="photos", relative_path="new", reason="restore"), BackgroundTasks(), {"id": 1}))
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
        self.assertEqual(self.row()["relocation_state"], "scan_required")

    def test_replaced_or_symlink_root_is_rejected_before_cloud_or_local_scan(self):
        from app.storage import scan_vault
        result = self.reconnect()
        self.new.rename(self.volume / "original")
        self.new.mkdir()
        with patch("app.storage.scan_tree") as tree, patch("app.storage.scan_cloud") as cloud:
            scanned = scan_vault(result)
        tree.assert_not_called(); cloud.assert_not_called()
        self.assertEqual(scanned["root_identity_mismatch"], 1)
        self.assertEqual(self.row()["relocation_state"], "scan_required")

    def test_root_swap_during_walk_rolls_back_and_never_resumes(self):
        self.reconnect()
        def swap(*args, **kwargs):
            self.new.rename(self.volume / "original"); self.new.mkdir()
            return 0, None
        with patch("app.storage._scan_tree", side_effect=swap): result = self.scan()
        self.assertEqual(result["root_identity_mismatch"], 1)
        self.assertEqual(self.row()["relocation_state"], "scan_required")

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
        self.assertEqual(self.row()["relocation_state"], "scan_required")

    def test_scan_mutations_roll_back_after_root_changes_during_walk(self):
        self.reconnect()
        def mutate_then_swap(connection, vault, scan_id, **kwargs):
            ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="unrelated.txt", file_type="regular", size=9, mtime_ns=1, seen_at=scan_id, observed_at=scan_id)
            self.new.rename(self.volume / "original"); self.new.mkdir()
            return 1, None
        with patch("app.storage._scan_tree", side_effect=mutate_then_swap): result = self.scan()
        self.assertEqual(result["root_identity_mismatch"], 1)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM vault_files WHERE vault_id=7").fetchone()["n"], 0)
        self.assertEqual(self.row()["relocation_state"], "scan_required")

    def test_finalization_mutations_roll_back_after_late_root_change(self):
        self.reconnect()
        def mutate_then_swap(connection, vault, **kwargs):
            file_id = ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="unrelated.txt", file_type="regular", size=9, mtime_ns=1, observed_at="scan-auto")
            connection.execute("INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at) VALUES (7,%s,'unrelated.txt','upload','queued','scan-auto','scan-auto')", (file_id,))
            self.new.rename(self.volume / "original"); self.new.mkdir()
            return {"hashed": 1, "confirmed": 0, "queued": 1}
        with patch("app.storage._apply_auto_renames", side_effect=mutate_then_swap): result = self.scan()
        self.assertEqual(result["root_identity_mismatch"], 1)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM vault_files WHERE vault_id=7").fetchone()["n"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM jobs WHERE vault_id=7").fetchone()["n"], 0)
        self.assertEqual(self.row()["relocation_state"], "scan_required")

    def test_manual_restore_invalidates_local_authority_and_stale_jobs(self):
        from app.storage import scan_vault
        with PostgresConnection(str(self.db_path)) as connection:
            file_id = ArchiveCatalog(connection).observe_local_copy(vault_id=7, path="a", file_type="regular", size=1, mtime_ns=1, observed_at="2026-11-03T00:00:00Z")
            connection.execute("INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at) VALUES (7,%s,'a','recover','queued','now','now')", (file_id,))
            record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")
            self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"], "cancelled")
        self.assertEqual(vault_relocation.reconcile_vault_root_identities(), 0)
        with patch("app.storage.scan_cloud") as cloud, patch("app.storage.scan_tree") as tree:
            result = scan_vault(self.row())
        self.assertEqual(result["root_identity_mismatch"], 1)
        tree.assert_not_called(); cloud.assert_not_called()
        self.assertIsNone(self.row()["root_identity"])
        self.reconnect()  # No stale Job can strand reconnect indefinitely.

    def test_stale_catalog_reconnect_protected_cloud_only_conflict_no_cloud_deletes(self):
        # A is the restored catalog; only B remains remotely after a verified
        # replacement. Even identical local size/mtime cannot retain A's digest.
        content = {"same.jpg": b"new!", "missing.jpg": b"only", "conflict.jpg": b"safe"}
        (self.new / "same.jpg").write_bytes(content["same.jpg"])
        (self.new / "conflict.jpg").write_bytes(b"oops")
        with PostgresConnection(str(self.db_path)) as connection:
            record_metadata_restore(connection, "2026-11-02T10:00:00+00:00")
            catalog = ArchiveCatalog(connection)
            for path in content:
                version = catalog.record_archive_version(vault_id=7, path=path, object_key="vaults/immutable/" + path,
                    provider_version_id="old-" + path, size=4, storage_class="STANDARD", etag="old", uploaded_at="2026-11-01T10:00:00+00:00",
                    observed_at="2026-11-01T10:00:00+00:00", scan_id="old")
                catalog.mark_version_verified(version, plaintext_sha256=hashlib.sha256(b"old!").hexdigest(), verified_at="2026-11-01T10:00:00+00:00")
                local = self.new / path
                stat = local.stat() if local.exists() else SimpleNamespace(st_size=4, st_mtime_ns=1)
                catalog.observe_local_copy(vault_id=7, path=path, file_type="regular", size=stat.st_size,
                                           mtime_ns=stat.st_mtime_ns, observed_at="2026-11-01T10:00:00+00:00")
                catalog.set_local_fingerprint(vault_id=7, path=path, plaintext_sha256=hashlib.sha256(b"old!").hexdigest(), matched_archive_version_id=version)
        s3 = ListingS3(content)
        reconnected = self.reconnect()
        result = self.scan(reconnected, s3)
        self.assertEqual(result["local"], 2, result)
        self.assertEqual(s3.deleted, [])
        self.assertEqual(len(s3.reads), 3)
        with PostgresConnection(str(self.db_path)) as connection:
            rows = {r["path"]: r for r in ArchiveCatalog(connection).list_file_rows(7)}
            self.assertEqual(rows["same.jpg"]["state"], "protected")
            self.assertEqual(rows["missing.jpg"]["state"], "cloud_only")
            self.assertEqual(rows["conflict.jpg"]["state"], "problem")
            self.assertFalse(rows["conflict.jpg"]["upload_eligible"])
            notice = kit_status(connection)["metadata_restore"]
            self.assertEqual(notice["backup_created_at"], "2026-11-02T10:00:00+00:00")
            self.assertEqual(notice["updated_files"], 3)
            versions = connection.execute("SELECT availability FROM archive_versions WHERE provider_version_id LIKE 'new-%'").fetchall()
            self.assertTrue(all(v["availability"] == "available" for v in versions))
        self.assertEqual(self.row()["relocation_state"], "ready")
        self.scan(s3=s3)
        with PostgresConnection(str(self.db_path)) as connection:
            self.assertEqual(kit_status(connection)["metadata_restore"]["updated_files"], 3)


if __name__ == "__main__": unittest.main()
