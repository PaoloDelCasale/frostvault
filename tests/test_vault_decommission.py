"""Vault decommission lifecycle, dispositions, and root occupancy (issue #153)."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.config import Settings
from app.database import SQLiteConnection
from app.services import source_areas, source_layout, vault_decommission
from app.services.vault_relocation import enroll_vault_root_identity
from app import storage
from tests.test_database import run_alembic


class VaultDecommissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"
        with storage.operation_process_lock:
            storage.cancelled_jobs.clear()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.root = Path(self.tmp.name) / "vault-root"
        self.root.mkdir()
        self.addCleanup(vault_decommission.release_runtime_gate, 7)
        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            vault_s3_bucket="bucket",
            vault_rclone_remote="remote",
        )
        for target in (
            "app.database.settings",
            "app.services.source_areas.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash, is_admin)
                VALUES (1, 'owner', 'Owner', 'hash', FALSE),
                       (2, 'admin', 'Admin', 'hash', TRUE),
                       (3, 'member', 'Member', 'hash', FALSE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote
                ) VALUES (7, 'archive', 'Archive', %s, 'bucket',
                          'vaults/archive/', 'remote')
                """,
                (str(self.root),),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (7, 1, 'owner'), (7, 3, 'viewer')"
            )
            enroll_vault_root_identity(connection, 7, str(self.root))

    def preview(self, local: str = "retain", cloud: str = "retain") -> dict:
        with SQLiteConnection(str(self.db_path)) as connection:
            return vault_decommission.build_preview(
                connection,
                vault_id=7,
                local_disposition=local,
                cloud_disposition=cloud,
                local_delete_enabled=True,
            )

    def start(self, preview: dict, *, actor: int = 1) -> dict:
        with SQLiteConnection(str(self.db_path)) as connection:
            vault_decommission.start_decommission(
                connection,
                vault_id=7,
                actor_user_id=actor,
                actor_is_admin=actor == 2,
                local_disposition=preview["local_disposition"],
                cloud_disposition=preview["cloud_disposition"],
                confirmation="Archive",
                reason="retire completed archive",
                preview_fingerprint=preview["fingerprint"],
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            return vault_decommission.reconcile_one(
                connection,
                vault_id=7,
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )

    def add_verified_local_copy(self, path: str = "report.txt") -> str:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"verified content")
        info = target.stat()
        digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
        with SQLiteConnection(str(self.db_path)) as connection:
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(
                vault_id=7,
                path=path,
                file_type="regular",
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                observed_at="2026-08-01T10:00:00+00:00",
            )
            version_id = catalog.record_archive_version(
                vault_id=7,
                path=path,
                object_key=f"vaults/archive/{path}",
                provider_version_id="provider-version-1",
                size=info.st_size,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-08-01T10:00:00+00:00",
                observed_at="2026-08-01T10:00:00+00:00",
                scan_id="scan-1",
                origin="upload",
            )
            catalog.mark_version_verified(
                version_id,
                plaintext_sha256=digest,
                verified_at="2026-08-01T10:01:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=7,
                path=path,
                plaintext_sha256=digest,
                matched_archive_version_id=version_id,
            )
        return file_id

    def test_preview_is_authoritative_and_reports_counts_blockers_and_fingerprint(self) -> None:
        self.add_verified_local_copy()
        with SQLiteConnection(str(self.db_path)) as connection:
            file_id = connection.execute(
                "SELECT id FROM vault_files WHERE vault_id=7"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status,
                    requested_by, requested_at, updated_at
                ) VALUES (7, %s, 'report.txt', 'free-space', 'queued', 1,
                          '2026-08-01', '2026-08-01')
                """,
                (file_id,),
            )
        preview = self.preview(local="remove")
        self.assertEqual(preview["counts"]["local_files"], 1)
        self.assertEqual(preview["counts"]["archive_versions"], 1)
        self.assertGreater(preview["counts"]["local_bytes"], 0)
        self.assertEqual(len(preview["fingerprint"]), 64)
        self.assertFalse(preview["can_start"])
        self.assertIn("active_jobs", {item["code"] for item in preview["blockers"]})
        self.assertIn(
            "pending_destructive_actions",
            {item["code"] for item in preview["blockers"]},
        )

    def test_stale_fingerprint_and_exact_name_are_rejected_without_quiescing(self) -> None:
        preview = self.preview()
        with SQLiteConnection(str(self.db_path)) as connection:
            with self.assertRaises(vault_decommission.VaultDecommissionError) as stale:
                vault_decommission.start_decommission(
                    connection,
                    vault_id=7,
                    actor_user_id=1,
                    actor_is_admin=False,
                    local_disposition="retain",
                    cloud_disposition="retain",
                    confirmation="Archive",
                    reason="retire completed archive",
                    preview_fingerprint="0" * 64,
                    local_delete_enabled=True,
                    purge_delay_seconds=3600,
                )
            self.assertEqual(stale.exception.reason, "stale_preview")
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                "SELECT decommission_state, root_released_at FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(row["decommission_state"], "active")
        self.assertIsNone(row["root_released_at"])

        with SQLiteConnection(str(self.db_path)) as connection:
            with self.assertRaises(vault_decommission.VaultDecommissionError) as confirm:
                vault_decommission.start_decommission(
                    connection,
                    vault_id=7,
                    actor_user_id=1,
                    actor_is_admin=False,
                    local_disposition="retain",
                    cloud_disposition="retain",
                    confirmation="archive",
                    reason="retire completed archive",
                    preview_fingerprint=preview["fingerprint"],
                    local_delete_enabled=True,
                    purge_delay_seconds=3600,
                )
        self.assertEqual(confirm.exception.reason, "confirmation_required")

    def test_retain_retain_preserves_data_and_history_and_releases_atomically(self) -> None:
        self.add_verified_local_copy()
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET enabled=FALSE WHERE id=7")
        preview = self.preview()
        status = self.start(preview, actor=2)
        self.assertEqual(status["state"], "completed")
        self.assertTrue(status["root_released"])
        self.assertTrue((self.root / "report.txt").is_file())

        with SQLiteConnection(str(self.db_path)) as connection:
            vault = connection.execute("SELECT * FROM vaults WHERE id=7").fetchone()
            self.assertFalse(vault["enabled"])
            self.assertEqual(vault["decommission_state"], "decommissioned")
            self.assertIsNotNone(vault["root_released_at"])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM vault_members WHERE vault_id=7"
                ).fetchone()["total"],
                2,
            )
            events = {
                row["event"]
                for row in connection.execute(
                    "SELECT event FROM audit_events WHERE vault_id=7"
                ).fetchall()
            }
            notifications = connection.execute(
                "SELECT user_id, event FROM notifications WHERE vault_id=7"
            ).fetchall()
        self.assertIn("vault_decommission.requested", events)
        self.assertIn("vault_decommission.completed", events)
        self.assertEqual(
            {row["user_id"] for row in notifications if row["event"] == "vault_decommission.completed"},
            {1, 3},
        )

    def test_terminal_root_release_rolls_back_atomically_if_audit_notification_fails(self) -> None:
        preview = self.preview()
        with SQLiteConnection(str(self.db_path)) as connection:
            vault_decommission.start_decommission(
                connection,
                vault_id=7,
                actor_user_id=1,
                actor_is_admin=False,
                local_disposition="retain",
                cloud_disposition="retain",
                confirmation="Archive",
                reason="retire completed archive",
                preview_fingerprint=preview["fingerprint"],
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
        with patch(
            "app.services.vault_decommission._notify_members",
            side_effect=RuntimeError("notification store unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                with SQLiteConnection(str(self.db_path)) as connection:
                    vault_decommission.reconcile_one(
                        connection,
                        vault_id=7,
                        local_delete_enabled=True,
                        purge_delay_seconds=3600,
                    )
        with SQLiteConnection(str(self.db_path)) as connection:
            vault = connection.execute(
                "SELECT decommission_state, root_released_at FROM vaults WHERE id=7"
            ).fetchone()
            operation = connection.execute(
                "SELECT state, completed_at FROM vault_decommissions WHERE vault_id=7"
            ).fetchone()
        self.assertEqual(vault["decommission_state"], "decommissioning")
        self.assertIsNone(vault["root_released_at"])
        self.assertEqual(operation["state"], "quiescing")
        self.assertIsNone(operation["completed_at"])

    def test_in_progress_request_replay_does_not_duplicate_destructive_jobs(self) -> None:
        self.add_verified_local_copy()
        preview = self.preview(local="remove")
        request = {
            "vault_id": 7,
            "actor_user_id": 1,
            "actor_is_admin": False,
            "local_disposition": "remove",
            "cloud_disposition": "retain",
            "confirmation": "Archive",
            "reason": "remove local archive copy",
            "preview_fingerprint": preview["fingerprint"],
            "local_delete_enabled": True,
            "purge_delay_seconds": 3600,
        }
        with SQLiteConnection(str(self.db_path)) as connection:
            first = vault_decommission.start_decommission(connection, **request)
            replay = vault_decommission.start_decommission(connection, **request)
            operation_count = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_decommissions WHERE vault_id=7"
            ).fetchone()["total"]
            job_count = connection.execute(
                "SELECT COUNT(*) AS total FROM jobs WHERE vault_id=7"
            ).fetchone()["total"]
        self.assertEqual(first["state"], "local_cleanup")
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(operation_count, 1)
        self.assertEqual(job_count, 1)

    def test_completed_request_replay_is_idempotent_but_conflicts_fail(self) -> None:
        preview = self.preview()
        completed = self.start(preview)
        self.assertEqual(completed["state"], "completed")
        with SQLiteConnection(str(self.db_path)) as connection:
            replay = vault_decommission.start_decommission(
                connection,
                vault_id=7,
                actor_user_id=1,
                actor_is_admin=False,
                local_disposition="retain",
                cloud_disposition="retain",
                confirmation="Archive",
                reason="retire completed archive",
                preview_fingerprint=preview["fingerprint"],
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            self.assertEqual(replay["state"], "completed")
            with self.assertRaises(vault_decommission.VaultDecommissionError) as conflict:
                vault_decommission.start_decommission(
                    connection,
                    vault_id=7,
                    actor_user_id=1,
                    actor_is_admin=False,
                    local_disposition="retain",
                    cloud_disposition="retain",
                    confirmation="Archive",
                    reason="a conflicting repeated reason",
                    preview_fingerprint=preview["fingerprint"],
                    local_delete_enabled=True,
                    purge_delay_seconds=3600,
                )
        self.assertEqual(conflict.exception.reason, "already_decommissioned")

    def test_remove_queues_only_decommission_free_space_and_holds_root(self) -> None:
        self.add_verified_local_copy()
        preview = self.preview(local="remove")
        with SQLiteConnection(str(self.db_path)) as connection:
            started = vault_decommission.start_decommission(
                connection,
                vault_id=7,
                actor_user_id=1,
                actor_is_admin=False,
                local_disposition="remove",
                cloud_disposition="retain",
                confirmation="Archive",
                reason="remove local archive copy",
                preview_fingerprint=preview["fingerprint"],
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            job = connection.execute(
                "SELECT action, origin, status, group_id FROM jobs WHERE vault_id=7"
            ).fetchone()
        self.assertEqual(job["action"], "free-space")
        self.assertEqual(job["origin"], "decommission")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(started["local_status"], "removing")
        self.assertFalse(started["root_released"])

    def test_scheduler_runs_only_tagged_decommission_job_for_disabled_quiesced_vault(self) -> None:
        from types import SimpleNamespace

        from app.storage import process_jobs_once

        self.add_verified_local_copy()
        preview = self.preview(local="remove")
        with SQLiteConnection(str(self.db_path)) as connection:
            vault_decommission.start_decommission(
                connection,
                vault_id=7,
                actor_user_id=1,
                actor_is_admin=False,
                local_disposition="remove",
                cloud_disposition="retain",
                confirmation="Archive",
                reason="remove local archive copy",
                preview_fingerprint=preview["fingerprint"],
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            connection.execute("UPDATE vaults SET enabled=FALSE WHERE id=7")
        processed: list[dict] = []
        with (
            patch(
                "app.storage._runtime_settings",
                return_value=SimpleNamespace(
                    operation_concurrency=1,
                    bandwidth_limit_kibps=None,
                ),
            ),
            patch("app.storage.db", side_effect=lambda: SQLiteConnection(str(self.db_path))),
            patch("app.storage.process_job", side_effect=lambda job: processed.append(job)),
        ):
            self.assertEqual(process_jobs_once(), 1)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["origin"], "decommission")
        self.assertEqual(processed[0]["action"], "free-space")

    def test_verified_free_space_worker_completes_local_remove_before_release(self) -> None:
        from types import SimpleNamespace

        from app.storage import process_free_space

        self.add_verified_local_copy()
        preview = self.preview(local="remove")
        with SQLiteConnection(str(self.db_path)) as connection:
            vault_decommission.start_decommission(
                connection,
                vault_id=7,
                actor_user_id=1,
                actor_is_admin=False,
                local_disposition="remove",
                cloud_disposition="retain",
                confirmation="Archive",
                reason="remove local archive copy",
                preview_fingerprint=preview["fingerprint"],
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            job = connection.execute(
                """
                SELECT j.*, v.source_root, v.s3_bucket, v.s3_prefix,
                       v.rclone_remote, v.encryption_mode,
                       v.crypt_password_ciphertext, v.crypt_password2_ciphertext,
                       v.uuid AS vault_uuid, v.name AS vault_name,
                       v.cloud_deletion_enabled, v.decommission_state
                FROM jobs j JOIN vaults v ON v.id=j.vault_id
                WHERE j.vault_id=7
                """
            ).fetchone()
        with (
            patch(
                "app.storage._runtime_settings",
                return_value=SimpleNamespace(allow_local_delete=True),
            ),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client") as client,
        ):
            client.return_value.head_object.return_value = {"ContentLength": 16}
            process_free_space(dict(job))
        self.assertFalse((self.root / "report.txt").exists())
        with SQLiteConnection(str(self.db_path)) as connection:
            completed = vault_decommission.reconcile_one(
                connection,
                vault_id=7,
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
        self.assertEqual(completed["state"], "completed")
        self.assertTrue(completed["root_released"])

    def test_cloud_purge_reuses_delay_and_releases_only_after_verified_terminal_items(self) -> None:
        self.add_verified_local_copy()
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET cloud_deletion_enabled=TRUE WHERE id=7")
        preview = self.preview(cloud="purge")
        with SQLiteConnection(str(self.db_path)) as connection:
            vault_decommission.start_decommission(
                connection,
                vault_id=7,
                actor_user_id=1,
                actor_is_admin=False,
                local_disposition="retain",
                cloud_disposition="purge",
                confirmation="Archive",
                reason="purge expired cloud archive",
                preview_fingerprint=preview["fingerprint"],
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            status = vault_decommission.reconcile_one(
                connection,
                vault_id=7,
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            job = connection.execute(
                "SELECT id, status, origin, pending_until FROM jobs WHERE vault_id=7"
            ).fetchone()
            self.assertEqual(status["state"], "cloud_purge")
            self.assertFalse(status["root_released"])
            self.assertEqual(job["status"], "pending_delay")
            self.assertEqual(job["origin"], "decommission")
            self.assertIsNotNone(job["pending_until"])
            connection.execute(
                "UPDATE cloud_deletion_items SET status='deleted' WHERE job_id=%s",
                (job["id"],),
            )
            connection.execute(
                "UPDATE archive_versions SET availability='purged' WHERE vault_id=7"
            )
            connection.execute(
                "UPDATE jobs SET status='completed' WHERE id=%s", (job["id"],)
            )
            completed = vault_decommission.reconcile_one(
                connection,
                vault_id=7,
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
        self.assertEqual(completed["state"], "completed")
        self.assertTrue(completed["root_released"])
        self.assertTrue((self.root / "report.txt").is_file())

    def test_cloud_purge_delay_remains_cancellable_and_keeps_root_occupied(self) -> None:
        self.add_verified_local_copy()
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET cloud_deletion_enabled=TRUE WHERE id=7")
        preview = self.preview(cloud="purge")
        with SQLiteConnection(str(self.db_path)) as connection:
            vault_decommission.start_decommission(
                connection,
                vault_id=7,
                actor_user_id=1,
                actor_is_admin=False,
                local_disposition="retain",
                cloud_disposition="purge",
                confirmation="Archive",
                reason="purge expired cloud archive",
                preview_fingerprint=preview["fingerprint"],
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            waiting = vault_decommission.reconcile_one(
                connection,
                vault_id=7,
                local_delete_enabled=True,
                purge_delay_seconds=3600,
            )
            self.assertTrue(waiting["cloud_cancellable"])
            cancelled = vault_decommission.cancel_pending_cloud_purge(
                connection, vault_id=7, actor_user_id=1
            )
            job = connection.execute(
                "SELECT status FROM jobs WHERE vault_id=7"
            ).fetchone()
            vault = connection.execute(
                "SELECT root_released_at FROM vaults WHERE id=7"
            ).fetchone()
        self.assertEqual(cancelled["state"], "blocked")
        self.assertEqual(cancelled["error_code"], "cloud_purge_cancelled")
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(vault["root_released_at"])

    def test_crypt_retain_requires_custody_but_purge_does_not(self) -> None:
        with SQLiteConnection(str(self.db_path)) as connection:
            # Existing check requires sealed values for crypt mode.
            connection.execute(
                """
                UPDATE vaults SET encryption_mode='crypt',
                    crypt_password_ciphertext='sealed-1',
                    crypt_password2_ciphertext='sealed-2',
                    cloud_deletion_enabled=TRUE
                WHERE id=7
                """
            )
        retained = self.preview(cloud="retain")
        purged = self.preview(cloud="purge")
        self.assertIn(
            "recovery_custody_unconfirmed",
            {item["code"] for item in retained["blockers"]},
        )
        self.assertNotIn(
            "recovery_custody_unconfirmed",
            {item["code"] for item in purged["blockers"]},
        )

    def test_root_occupation_depends_only_on_root_released_at(self) -> None:
        sources = Path(self.tmp.name) / "sources"
        managed = sources / "managed"
        photos = sources / "photos"
        managed.mkdir(parents=True)
        photos.mkdir()
        occupied = photos / "archive"
        self.root.rename(occupied)
        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                "UPDATE vaults SET source_root=%s, enabled=FALSE WHERE id=7",
                (str(occupied),),
            )
        volume = source_layout.SourceVolume(
            alias="photos", path=str(photos), access="rw", health="ok"
        )
        with SQLiteConnection(str(self.db_path)) as connection:
            roots = source_areas._occupied_vault_roots(connection, volume)
            self.assertEqual([item["relative_path"] for item in roots], ["archive"])
            stamp = "2026-08-01T12:00:00+00:00"
            connection.execute(
                """
                UPDATE vaults SET decommission_state='decommissioned',
                    decommissioned_at=%s, root_released_at=%s WHERE id=7
                """,
                (stamp, stamp),
            )
        with SQLiteConnection(str(self.db_path)) as connection:
            self.assertEqual(source_areas._occupied_vault_roots(connection, volume), [])


class VaultDecommissionMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_disabled_vault_as_occupied_and_adds_lifecycle_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upgrade.db"
            baseline = run_alembic(path, "0030_vault_root_relocation")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                    "VALUES (1, 'owner', 'Owner', 'hash', FALSE)"
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote, enabled
                    ) VALUES (7, 'archive', 'Archive', '/source', 'bucket',
                              'vaults/archive/', 'remote', FALSE)
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (7, 1, 'owner')"
                )
            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            with SQLiteConnection(str(path)) as connection:
                vault = connection.execute(
                    """
                    SELECT enabled, decommission_state, decommissioned_at,
                           root_released_at FROM vaults WHERE id=7
                    """
                ).fetchone()
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertFalse(vault["enabled"])
            self.assertEqual(vault["decommission_state"], "active")
            self.assertIsNone(vault["decommissioned_at"])
            self.assertIsNone(vault["root_released_at"])
            self.assertIn("vault_decommissions", tables)

    def test_downgrade_restores_jobs_origin_constraint_and_removes_lifecycle_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "downgrade.db"
            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            downgraded = run_alembic(
                path,
                "0030_vault_root_relocation",
                command="downgrade",
            )
            self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
            with SQLiteConnection(str(path)) as connection:
                vault_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(vaults)").fetchall()
                }
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                jobs_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
                ).fetchone()["sql"]
            self.assertNotIn("root_released_at", vault_columns)
            self.assertNotIn("vault_decommissions", tables)
            self.assertNotIn("decommission", jobs_sql)


if __name__ == "__main__":
    unittest.main()
