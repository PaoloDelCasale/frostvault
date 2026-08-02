"""Issue #152: verified same-Source-Volume Vault Root Relocation."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.database import SQLiteConnection
from app.services import source_layout, vault_relocation
from tests.test_database import run_alembic


class VaultRelocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.sources = Path(self.tmp.name) / "sources"
        self.sources.mkdir()
        (self.sources / "managed").mkdir()
        self.volume = self.sources / "photos"
        self.volume.mkdir()
        self.old = self.volume / "old-name"
        self.old.mkdir()
        (self.old / "photo.jpg").write_bytes(b"same tree")
        self.settings = replace(
            Settings(), db_backend="sqlite", sqlite_path=str(self.db_path)
        )
        for target in (
            "app.database.settings",
            "app.services.source_areas.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.addCleanup(vault_relocation._runtime_suspended_vaults.discard, 7)
        source_layout.override_sources_root(self.sources)
        mount = patch.object(
            source_layout,
            "path_is_mount",
            side_effect=lambda path: Path(path).resolve()
            in {self.sources.resolve(), self.volume.resolve()},
        )
        mount.start()
        self.addCleanup(mount.stop)
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'owner', 'Owner', 'hash', 0), (9, 'admin', 'Admin', 'hash', 1)"
            )
            self.vault = connection.execute(
                """
                INSERT INTO vaults(
                    id, uuid, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote, encryption_mode
                ) VALUES (
                    7, '11111111-1111-1111-1111-111111111111', 'photos',
                    'Photos', %s, 'bucket', 'vaults/immutable/', 'remote', 'plain'
                ) RETURNING *
                """,
                (str(self.old),),
            ).fetchone()
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (7, 1, 'owner')"
            )
            vault_relocation.enroll_vault_root_identity(connection, 7, str(self.old))
        self.new = self.volume / "new-name"
        self.old.rename(self.new)

    def relocate(self, **overrides):
        args = dict(
            vault_id=7,
            volume_alias="photos",
            relative_path="new-name",
            actor_user_id=9,
            reason="operator renamed the directory",
        )
        args.update(overrides)
        with SQLiteConnection(str(self.db_path)) as connection:
            return vault_relocation.relocate_vault_root(connection, **args)

    def reason(self, **overrides) -> str:
        with self.assertRaises(vault_relocation.VaultRelocationError) as raised:
            self.relocate(**overrides)
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute("SELECT source_root FROM vaults WHERE id=7").fetchone()
        self.assertEqual(row["source_root"], str(self.old))
        return raised.exception.reason

    def test_verified_relocation_preserves_identity_and_notifies_owner(self) -> None:
        before = dict(self.vault)
        relocated = self.relocate()
        self.assertEqual(relocated["source_root"], str(self.new.resolve()))
        self.assertEqual(relocated["relocation_state"], "scan_required")
        for field in ("id", "uuid", "s3_bucket", "s3_prefix", "rclone_remote", "encryption_mode"):
            self.assertEqual(relocated[field], before[field])
        with SQLiteConnection(str(self.db_path)) as connection:
            member = connection.execute(
                "SELECT user_id, role FROM vault_members WHERE vault_id=7"
            ).fetchone()
            event = connection.execute(
                "SELECT event, actor_user_id, detail_json "
                "FROM audit_events WHERE vault_id=7 ORDER BY id DESC"
            ).fetchone()
            notice = connection.execute(
                "SELECT user_id, event FROM notifications WHERE vault_id=7 ORDER BY id DESC"
            ).fetchone()
        self.assertEqual(member, {"user_id": 1, "role": "owner"})
        self.assertEqual(event["event"], "vault_root_relocated")
        self.assertEqual(event["actor_user_id"], 9)
        self.assertEqual(
            json.loads(event["detail_json"])["reason"],
            "operator renamed the directory",
        )
        self.assertEqual(notice, {"user_id": 1, "event": "vault_root_relocated"})

    def test_destination_rejection_matrix_fails_without_update(self) -> None:
        self.assertEqual(self.reason(volume_alias="other"), "different_volume")
        self.assertEqual(self.reason(relative_path="missing"), "inaccessible")
        self.assertEqual(self.reason(runtime_busy=True), "active_jobs")
        with patch("app.services.vault_relocation._active_jobs", return_value=True):
            self.assertEqual(self.reason(), "active_jobs")
        unrelated = self.volume / "unrelated"
        unrelated.mkdir()
        self.assertEqual(self.reason(relative_path="unrelated"), "identity_mismatch")
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET root_identity=NULL WHERE id=7")
        self.assertEqual(self.reason(), "identity_ambiguous")

    def test_root_identity_rejects_an_ancestor_symlink(self) -> None:
        real_parent = Path(self.tmp.name) / "real-parent"
        real_parent.mkdir()
        root = real_parent / "root"
        root.mkdir()
        linked_parent = Path(self.tmp.name) / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaises(vault_relocation.VaultRelocationError) as raised:
            vault_relocation.root_identity(linked_parent / "root")
        self.assertEqual(raised.exception.reason, "symlink")

    def test_overlap_symlink_and_source_present_are_rejected(self) -> None:
        overlap = self.new / "child"
        overlap.mkdir()
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (8, 'other', 'Other', %s, 'b', 'vaults/other/', 'r')",
                (str(overlap),),
            )
        self.assertEqual(self.reason(), "overlap")
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute("DELETE FROM vaults WHERE id=8")
        with patch.object(
            source_layout,
            "path_is_symlink",
            side_effect=lambda path: Path(path) == self.new,
        ):
            self.assertEqual(self.reason(), "symlink")
        self.old.mkdir()
        self.assertEqual(self.reason(), "source_present")

    def test_exception_after_update_rolls_back_path_and_suspension(self) -> None:
        def fail_handoff() -> None:
            raise RuntimeError("watcher handoff failed")

        with self.assertRaises(RuntimeError):
            self.relocate(after_update=fail_handoff)
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT source_root, relocation_state, relocation_previous_root FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(row["source_root"], str(self.old))
        self.assertEqual(row["relocation_state"], "ready")
        self.assertIsNone(row["relocation_previous_root"])

    def test_relocation_lock_blocks_a_new_scan_during_handoff(self) -> None:
        from fastapi import BackgroundTasks

        from app.main import VaultRelocate, admin_relocate_vault_root
        from app.storage import scan_lock_for_vault, scan_vault

        entered = threading.Event()
        continue_relocation = threading.Event()
        original = vault_relocation.relocate_vault_root

        def delayed_relocation(connection, **kwargs):
            entered.set()
            self.assertTrue(continue_relocation.wait(2))
            return original(connection, **kwargs)

        lock = scan_lock_for_vault(7)
        with patch(
            "app.main.vault_relocation_service.relocate_vault_root",
            side_effect=delayed_relocation,
        ):
            outcome: list[object] = []

            def invoke() -> None:
                try:
                    outcome.append(
                        admin_relocate_vault_root(
                            7,
                            VaultRelocate(
                                volume_alias="photos",
                                relative_path="new-name",
                                reason="operator renamed the directory",
                            ),
                            BackgroundTasks(),
                            {"id": 9},
                            None,
                        )
                    )
                except BaseException as exc:  # pragma: no cover - assertion below
                    outcome.append(exc)

            thread = threading.Thread(target=invoke)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(scan_vault(dict(self.vault)), {"already_running": 1})
            continue_relocation.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict)
        self.assertFalse(lock.locked())
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT source_root, relocation_state FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(row["source_root"], str(self.new.resolve()))
        self.assertEqual(row["relocation_state"], "scan_required")

    def test_replaced_destination_is_rejected_before_scan(self) -> None:
        from app.storage import scan_vault

        relocated = self.relocate()
        self.new.rename(self.volume / "original")
        self.new.mkdir()

        with patch("app.storage.scan_tree") as scan_tree:
            result = scan_vault(dict(relocated))

        scan_tree.assert_not_called()
        self.assertEqual(result["local"], -1)
        self.assertEqual(result["root_identity_mismatch"], 1)
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT relocation_state FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(row["relocation_state"], "scan_required")
        self.assertTrue(vault_relocation.local_work_suspended({"id": 7}))

    def test_symlink_destination_is_rejected_before_scan(self) -> None:
        from app.storage import scan_vault

        relocated = self.relocate()
        target = self.volume / "target"
        target.mkdir()
        self.new.rename(self.volume / "original")
        self.new.symlink_to(target, target_is_directory=True)

        with patch("app.storage.scan_tree") as scan_tree:
            result = scan_vault(dict(relocated))

        scan_tree.assert_not_called()
        self.assertEqual(result["local"], -1)
        self.assertEqual(result["root_identity_mismatch"], 1)
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT relocation_state FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(row["relocation_state"], "scan_required")
        self.assertTrue(vault_relocation.local_work_suspended({"id": 7}))

    def test_destination_swap_during_scan_does_not_resume_suspension(self) -> None:
        from app.storage import scan_vault

        relocated = self.relocate()

        def swap_destination(*_args, **_kwargs):
            self.new.rename(self.volume / "original")
            self.new.mkdir()
            return 1, None

        with patch("app.storage._scan_tree", side_effect=swap_destination):
            result = scan_vault(dict(relocated))

        self.assertEqual(result["local"], -1)
        self.assertEqual(result["root_identity_mismatch"], 1)
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT relocation_state FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(row["relocation_state"], "scan_required")
        self.assertTrue(vault_relocation.local_work_suspended({"id": 7}))

    def test_ancestor_symlink_is_rejected_before_scan(self) -> None:
        from app.storage import scan_vault

        relocated = self.relocate()
        real_volume = self.sources / "photos-real"
        self.volume.rename(real_volume)
        self.volume.symlink_to(real_volume, target_is_directory=True)

        with patch("app.storage._scan_tree") as scan_tree:
            result = scan_vault(dict(relocated))

        scan_tree.assert_not_called()
        self.assertEqual(result["root_identity_mismatch"], 1)
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT relocation_state FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(row["relocation_state"], "scan_required")

    def test_scan_mutations_roll_back_when_root_changes_during_walk(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.storage import scan_vault

        relocated = self.relocate()

        def mutate_then_swap(connection, vault, scan_id, **_kwargs):
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=7,
                path="unrelated.txt",
                file_type="regular",
                size=9,
                mtime_ns=1,
                seen_at=scan_id,
                observed_at=scan_id,
            )
            self.new.rename(self.volume / "original")
            self.new.mkdir()
            return 1, None

        with patch("app.storage._scan_tree", side_effect=mutate_then_swap):
            result = scan_vault(dict(relocated))

        self.assertEqual(result["root_identity_mismatch"], 1)
        with SQLiteConnection(str(self.db_path)) as connection:
            files = connection.execute(
                "SELECT COUNT(*) AS count FROM vault_files WHERE vault_id=7"
            ).fetchone()
            jobs = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE vault_id=7"
            ).fetchone()
            state = connection.execute(
                "SELECT relocation_state FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(files["count"], 0)
        self.assertEqual(jobs["count"], 0)
        self.assertEqual(state["relocation_state"], "scan_required")

    def test_auto_rename_mutations_roll_back_when_root_changes(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.storage import scan_vault

        relocated = self.relocate()

        def mutate_then_swap(connection, vault, **_kwargs):
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=7,
                path="unrelated.txt",
                file_type="regular",
                size=9,
                mtime_ns=1,
                seen_at="scan-auto",
                observed_at="scan-auto",
            )
            file_row = connection.execute(
                "SELECT id FROM vault_files WHERE vault_id=7"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status,
                    requested_at, updated_at
                ) VALUES (7, %s, 'unrelated.txt', 'upload', 'queued', %s, %s)
                """,
                (file_row["id"], "scan-auto", "scan-auto"),
            )
            self.new.rename(self.volume / "original")
            self.new.mkdir()
            return {"hashed": 1, "confirmed": 0, "queued": 1, "ambiguous": 0}

        with patch("app.storage._apply_auto_renames", side_effect=mutate_then_swap):
            result = scan_vault(dict(relocated))

        self.assertEqual(result["root_identity_mismatch"], 1)
        with SQLiteConnection(str(self.db_path)) as connection:
            files = connection.execute(
                "SELECT COUNT(*) AS count FROM vault_files WHERE vault_id=7"
            ).fetchone()
            jobs = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE vault_id=7"
            ).fetchone()
        self.assertEqual(files["count"], 0)
        self.assertEqual(jobs["count"], 0)

    def test_successful_full_scan_resumes_after_restart_state(self) -> None:
        relocated = self.relocate()
        # The persisted state is the restart recovery seam; scan_vault receives
        # a freshly loaded row and clears it only after local scan success.
        from app.storage import scan_vault

        with patch("app.storage.scan_cloud", return_value=0), patch(
            "app.storage.validate_cloud_vault", return_value=None
        ), patch("app.storage.reconcile_pending_policy_tags", return_value=0), patch(
            "app.storage.sync_lifecycle_rules_for_bucket", return_value=0
        ), patch("app.storage.queue_auto_uploads", return_value=0), patch(
            "app.storage.queue_auto_local_cleanups", return_value=0
        ):
            result = scan_vault(dict(relocated))
        self.assertEqual(result["local"], 1)
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT relocation_state, relocation_previous_root FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(row["relocation_state"], "ready")
        self.assertIsNone(row["relocation_previous_root"])


if __name__ == "__main__":
    unittest.main()
