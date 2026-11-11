from __future__ import annotations

import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
import psycopg
from psycopg.rows import dict_row

from app import backoff
from app.catalog import ArchiveCatalog, VaultFileNotFound
from app.config import settings
from app.services import notifications
from app.services.catalog_events import CatalogEventStore


from tests.postgres import database_dsn, run_alembic as migrate_fixture


class PostgreSQLMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.identity = Path(self._tmp.name) / "migration"
        self.postgres_url = database_dsn(self.identity)
        from psycopg.conninfo import conninfo_to_dict
        self.engine = sa.create_engine(sa.URL.create("postgresql+psycopg", query=conninfo_to_dict(self.postgres_url)))

    def tearDown(self) -> None:
        self.engine.dispose()

    def run_alembic(self, revision="head", command="upgrade"):
        return migrate_fixture(self.identity, revision, command)

    def _connection(self) -> psycopg.Connection:
        return psycopg.connect(
            self.postgres_url,
            row_factory=dict_row,
        )

    def _seed_rename_candidate(self) -> str:
        digest = "a" * 64
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs')
                """
            )
            catalog = ArchiveCatalog(connection)
            missing_id = catalog.observe_local_copy(
                vault_id=2,
                path="concurrent/old.txt",
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=2,
                path="concurrent/old.txt",
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
            catalog.mark_local_copy_missing(
                missing_id, observed_at="2026-07-21T11:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=2,
                path="concurrent/new.txt",
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T11:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=2,
                path="concurrent/new.txt",
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
        return missing_id

    def test_vault_pause_preserves_existing_rows_and_refuses_active_hold_downgrade(self):
        from tests.vault_pause_migration_contract import assert_vault_pause_migration
        assert_vault_pause_migration(self, self.run_alembic, self._connection)

    def test_upload_intent_per_attempt_allows_one_open_intent_and_refuses_reused_downgrade(self):
        from tests.upload_intent_per_attempt_migration_contract import assert_upload_intent_per_attempt_migration
        assert_upload_intent_per_attempt_migration(self, self.run_alembic, self._connection)

    def test_upload_multipart_journal_adds_no_authority_and_refuses_open_downgrade(self):
        from tests.upload_multipart_journal_migration_contract import assert_upload_multipart_journal_migration
        assert_upload_multipart_journal_migration(self, self.run_alembic, self._connection)

    def test_upload_remediation_populated_discovery_preserves_history_and_refuses_pending_downgrade(self):
        from tests.upload_remediation_migration_contract import assert_upload_remediation_migration
        assert_upload_remediation_migration(self, self.run_alembic, self._connection)

    def test_upload_provenance_populated_roundtrip_and_ambiguous_downgrade_refused(self):
        from tests.upload_provenance_migration_contract import assert_upload_provenance_migration
        assert_upload_provenance_migration(self, self.run_alembic, self._connection)

    def test_upload_verification_populated_roundtrip_never_rewrites_trust(self):
        from tests.upload_verification_migration_contract import assert_upload_verification_migration
        assert_upload_verification_migration(self, self.run_alembic, self._connection)

    def test_storage_plan_migration_preserves_populated_catalog_and_only_cloud_copy(self):
        from tests.storage_plan_migration_contract import assert_storage_plan_migration
        assert_storage_plan_migration(self, self.run_alembic, self._connection)

    def test_storage_plan_migration_refuses_ambiguous_folder_policies(self):
        from tests.storage_plan_migration_contract import assert_ambiguous_plan_refused
        assert_ambiguous_plan_refused(self, self.run_alembic, self._connection)

    def test_storage_plan_migration_maps_missing_default_and_redundant_pins(self):
        from tests.storage_plan_safety_contract import assert_standard_plan_mapping
        assert_standard_plan_mapping(self, self.run_alembic, self._connection)

    def test_storage_plan_migration_refuses_unsafe_legacy_namespaces(self):
        from tests.storage_plan_safety_contract import assert_unsafe_namespaces_refused
        assert_unsafe_namespaces_refused(self, self.run_alembic, self._connection)

    def test_storage_plan_migration_refuses_pins_before_ddl(self):
        from tests.storage_plan_migration_contract import assert_ambiguous_plan_refused
        assert_ambiguous_plan_refused(self, self.run_alembic, self._connection, pinned=True)

    def test_sdk_migration_preserves_populated_catalog_and_only_cloud_copy(self) -> None:
        from tests.s3_sdk_migration_contract import assert_sdk_migration
        assert_sdk_migration(self, self.run_alembic, self._connection)

    def test_plaintext_vaults_populated_roundtrip_and_legacy_refusal(self) -> None:
        from tests.plaintext_vaults_migration_contract import assert_plaintext_migration
        assert_plaintext_migration(self, self.run_alembic, self._connection)

    def test_current_snapshot_preserves_every_copy_and_trust_on_upgrade_downgrade_reupgrade(self) -> None:
        from tests.current_snapshot_migration_contract import assert_current_snapshot_migration
        assert_current_snapshot_migration(self, self.run_alembic, self._connection)

    def test_file_action_cut_preserves_cloud_content_and_cancels_only_legacy_hides(self) -> None:
        from tests.essential_file_actions_migration_contract import assert_file_actions_migration

        assert_file_actions_migration(self, self.run_alembic, self._connection)

    def test_notification_cut_preserves_inbox_email_and_only_cloud_copy(self) -> None:
        from tests.notification_migration_contract import (
            REMOVED_TABLES, preserved_state, seed_legacy_notifications,
        )

        baseline = self.run_alembic("0045_drop_quotas_windows")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self._connection() as connection:
            seed_legacy_notifications(connection)
            before = preserved_state(connection)
        for revision, command in (
            ("0046_simplify_notifications", "upgrade"),
            ("0045_drop_quotas_windows", "downgrade"),
            ("0046_simplify_notifications", "upgrade"),
        ):
            result = self.run_alembic(revision, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            tables = set(sa.inspect(self.engine).get_table_names())
            with self._connection() as connection:
                self.assertEqual(preserved_state(connection), before)
                if command == "upgrade":
                    self.assertFalse(tables & REMOVED_TABLES)
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) AS total FROM notification_deliveries"
                    ).fetchone()["total"], 1)
                else:
                    self.assertTrue(REMOVED_TABLES <= tables)
                    for table in REMOVED_TABLES:
                        self.assertEqual(connection.execute(
                            f"SELECT COUNT(*) AS total FROM {table}"
                        ).fetchone()["total"], 0)

    def test_offline_removal_preserves_sessions_and_the_only_cloud_copy(self) -> None:
        from tests.notification_migration_contract import seed_legacy_notifications
        from tests.offline_removal_migration_contract import normalized_state, assert_session_schema

        baseline = self.run_alembic("0045_drop_quotas_windows")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self._connection() as connection:
            seed_legacy_notifications(connection)
            connection.execute("UPDATE sessions SET offline_cache_generation=7")
        baseline = self.run_alembic("0046_simplify_notifications")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self._connection() as connection:
            before = normalized_state(connection)
        for revision, command in (
            ("0047_remove_offline_cache", "upgrade"),
            ("0046_simplify_notifications", "downgrade"),
            ("0047_remove_offline_cache", "upgrade"),
        ):
            result = self.run_alembic(revision, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            with self._connection() as connection:
                self.assertEqual(normalized_state(connection), before)
                assert_session_schema(self, connection, upgraded=command == "upgrade")

    def test_administration_cut_preserves_sessions_jobs_and_only_cloud_copy(self) -> None:
        from tests.essential_administration_migration_contract import assert_administration_migration
        assert_administration_migration(self, self.run_alembic, self._connection)
        self.assertTrue({"system_settings", "worker_errors"}.isdisjoint(sa.inspect(self.engine).get_table_names()))

    def test_fresh_database_upgrades_and_rolls_back_losslessly(self) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        policy_columns = {
            column["name"]
            for column in sa.inspect(self.engine).get_columns("vault_operation_policies")
        }
        self.assertEqual(
            policy_columns & {"bandwidth_limit_kibps", "operating_windows_json"},
            set(),
        )
        self.assertNotIn("system_settings", sa.inspect(self.engine).get_table_names())
        self.assertNotIn("worker_errors", sa.inspect(self.engine).get_table_names())
        session_columns = {
            column["name"]
            for column in sa.inspect(self.engine).get_columns("sessions")
        }
        self.assertIn("transition_version", session_columns)
        self.assertTrue({"offline_cache_generation", "offline_cache_nonce"}.isdisjoint(session_columns))
        claim_columns = {
            column["name"] for column in sa.inspect(self.engine).get_columns("jobs")
        }
        self.assertLessEqual(
            {"claim_token", "claimed_at", "claim_expires_at"}, claim_columns
        )
        removed_tables = {
            "invites",
            "oidc_configuration",
            "source_areas",
            "vault_members",
            "lookup_rate_limit_keys",
            "lookup_rate_limit_attempts",
            "vault_quotas",
            "cost_price_books",
        }
        self.assertEqual(
            removed_tables & set(sa.inspect(self.engine).get_table_names()), set()
        )
        user_columns = {
            column["name"] for column in sa.inspect(self.engine).get_columns("users")
        }
        self.assertNotIn("is_admin", user_columns)
        self.assertNotIn("active", user_columns)
        revision_columns = {
            column["name"]
            for column in sa.inspect(self.engine).get_columns(
                "vault_catalog_revisions"
            )
        }
        event_columns = {
            column["name"]
            for column in sa.inspect(self.engine).get_columns("catalog_events")
        }
        self.assertEqual(
            revision_columns,
            {"vault_id", "revision", "retained_from_revision", "updated_at"},
        )
        self.assertEqual(
            event_columns,
            {"id", "vault_id", "revision", "domain", "scope", "payload_json", "created_at"},
        )
        inspector = sa.inspect(self.engine)
        self.assertNotIn("filesystem_health_snapshots", inspector.get_table_names())
        self.assertNotIn("filesystem_health_findings", inspector.get_table_names())

        downgraded = self.run_alembic("0001_current_schema", command="downgrade")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)

        upgraded_again = self.run_alembic()
        self.assertEqual(upgraded_again.returncode, 0, upgraded_again.stderr)

    def test_catalog_state_helpers_commit_and_roll_back_on_postgresql(self) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'state', 'State', '/source', 'bucket', 'state')
                """
            )
            published = CatalogEventStore(connection).mutate_and_publish(
                2,
                lambda conn: conn.execute(
                    "UPDATE vaults SET name='Committed' WHERE id=2"
                ),
                domain="files",
                scope="root",
                payload={"changed": True},
            )
            self.assertEqual(published.event["revision"], 1)

        with self._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT revision FROM vault_catalog_revisions WHERE vault_id=%s",
                    (2,),
                ).fetchone()["revision"],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM catalog_events"
                ).fetchone()["total"],
                1,
            )

    def test_current_release_data_migrates_without_loss(self) -> None:
        baseline = self.run_alembic("0001_current_schema")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (7, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (
                        2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote'
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (2, 7, 'owner')"
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO files(
                        vault_id, path, local_exists, local_size, local_mtime,
                        cloud_exists, cloud_size, cloud_key, storage_class, etag,
                        last_local_scan, last_cloud_scan, updated_at
                    ) VALUES (
                        2, 'report.txt', 1, 12, 1750000000.25,
                        1, 44, 'docs/report.txt', 'STANDARD', 'etag',
                        '2026-07-20', '2026-07-20', '2026-07-20'
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO jobs(
                        id, vault_id, path, action, status, requested_by,
                        requested_at, updated_at, total_bytes, transferred_bytes
                    ) VALUES (
                        9, 2, 'report.txt', 'upload', 'completed', 7,
                        '2026-07-20', '2026-07-20', 12, 12
                    )
                    """
                )
            )
            connection.execute(sa.text("DROP TABLE alembic_version"))

        migrated = self.run_alembic()
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with self.engine.connect() as connection:
            preserved = connection.execute(
                sa.text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM users WHERE id=7) AS users,
                        (SELECT COUNT(*) FROM vaults WHERE id=2) AS vaults,
                        (SELECT COUNT(*) FROM vault_files WHERE vault_id=2) AS files,
                        (SELECT COUNT(*) FROM jobs WHERE id=9) AS jobs,
                        (
                            SELECT COUNT(*) FROM archive_versions
                            WHERE origin='legacy' AND integrity='unverified'
                        ) AS legacy_versions
                    """
                )
            ).mappings().one()

        self.assertEqual(dict(preserved), {
            "users": 1,
            "vaults": 1,
            "files": 1,
            "jobs": 1,
            "legacy_versions": 1,
        })

    def test_vault_ownership_migration_preserves_normal_and_narrows_multi_owner_data(self) -> None:
        baseline = self.run_alembic("0006_auth_backoff")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            for user_id in range(1, 5):
                connection.execute(
                    sa.text(
                        "INSERT INTO users(id, username, display_name, password_hash, "
                        "is_admin) VALUES (:id, :username, :display_name, 'hash', FALSE)"
                    ),
                    {
                        "id": user_id,
                        "username": f"user-{user_id}",
                        "display_name": f"User {user_id}",
                    },
                )
            for vault_id, slug in ((1, "normal"), (2, "multi")):
                connection.execute(
                    sa.text(
                        "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, "
                        "s3_prefix, rclone_remote) VALUES (:id, :slug, :name, "
                        "'/source', 'bucket', :slug, 'remote')"
                    ),
                    {"id": vault_id, "slug": slug, "name": slug.title()},
                )
            connection.execute(
                sa.text(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'owner'), (1, 2, 'viewer'), "
                    "(2, 3, 'owner'), (2, 4, 'owner')"
                )
            )

        migrated = self.run_alembic("0007_vault_ownership")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with self.engine.connect() as connection:
            roles = connection.execute(
                sa.text(
                    "SELECT vault_id, user_id, role FROM vault_members "
                    "ORDER BY vault_id, user_id"
                )
            ).mappings().all()
        self.assertEqual(
            [(row["vault_id"], row["user_id"], row["role"]) for row in roles],
            [
                (1, 1, "owner"),
                (1, 2, "viewer"),
                (2, 3, "owner"),
                (2, 4, "operator"),
            ],
        )

    def test_zero_owner_vault_fails_atomically_without_widening_access(self) -> None:
        baseline = self.run_alembic("0006_auth_backoff")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'viewer', 'Viewer', 'hash', FALSE)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, "
                    "s3_prefix, rclone_remote) VALUES (1, 'orphan', 'Orphan', "
                    "'/source', 'bucket', 'orphan', 'remote')"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'viewer')"
                )
            )

        migrated = self.run_alembic("0007_vault_ownership")
        self.assertNotEqual(migrated.returncode, 0)
        self.assertIn("vault 1 has no primary owner", migrated.stderr)
        self.assertIn("assign exactly one authorized existing member", migrated.stderr)
        with self.engine.connect() as connection:
            version = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            role = connection.execute(
                sa.text(
                    "SELECT role FROM vault_members WHERE vault_id=1 AND user_id=1"
                )
            ).scalar_one()
            columns = {
                column["name"]
                for column in sa.inspect(self.engine).get_columns("vaults")
            }
        self.assertEqual(version, "0006_auth_backoff")
        self.assertEqual(role, "viewer")
        self.assertNotIn("uuid", columns)

    def test_ownerless_vault_without_memberships_fails_atomically(self) -> None:
        baseline = self.run_alembic("0006_auth_backoff")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, "
                    "s3_prefix, rclone_remote) VALUES (1, 'orphan', 'Orphan', "
                    "'/source', 'bucket', 'orphan', 'remote')"
                )
            )

        migrated = self.run_alembic("0007_vault_ownership")
        self.assertNotEqual(migrated.returncode, 0)
        self.assertIn("vault 1 has no primary owner", migrated.stderr)
        with self.engine.connect() as connection:
            version = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            columns = {
                column["name"]
                for column in sa.inspect(self.engine).get_columns("vaults")
            }
        self.assertEqual(version, "0006_auth_backoff")
        self.assertNotIn("uuid", columns)

    def test_duplicate_job_does_not_abort_the_catalog_transaction(self) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash
                    ) VALUES (7, 'owner', 'Owner', 'hash')
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix
                    ) VALUES (
                        2, 'docs', 'Docs', '/source', 'bucket', 'docs'
                    )
                    """
                )
            )

        connect_url = self.postgres_url
        with psycopg.connect(connect_url, row_factory=dict_row) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="report.txt",
                file_type="regular",
                size=12,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            arguments = {
                "vault_id": 2,
                "path": "report.txt",
                "action": "upload",
                "requested_by": 7,
                "requested_at": "2026-07-21T10:01:00+00:00",
                "group_id": "group",
                "is_directory": False,
            }
            first, _, _ = catalog.queue_jobs(**arguments)
            duplicate, _, _ = catalog.queue_jobs(**arguments)
            catalog.observe_local_copy(
                vault_id=2,
                path="after-duplicate.txt",
                file_type="regular",
                size=1,
                mtime_ns=2,
                observed_at="2026-07-21T10:02:00+00:00",
            )

        self.assertEqual(len(first), 1)
        self.assertEqual(duplicate, [])

    def test_changed_local_copy_can_queue_second_upload_on_postgresql(self) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        digest_a = "a" * 64
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO users(
                    id, username, display_name, password_hash
                ) VALUES (7, 'owner', 'Owner', 'hash')
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (
                    2, 'docs', 'Docs', '/source', 'bucket', 'docs')
                """
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="report.txt",
                file_type="regular",
                size=12,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            version_id = catalog.record_archive_version(
                vault_id=2,
                path="report.txt",
                object_key="docs/report.txt",
                provider_version_id="s3-a",
                size=12,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-07-21T10:00:00+00:00",
                observed_at="2026-07-21T10:00:00+00:00",
                scan_id="scan-a",
            )
            catalog.mark_version_verified(
                version_id,
                plaintext_sha256=digest_a,
                verified_at="2026-07-21T10:01:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=2,
                path="report.txt",
                plaintext_sha256=digest_a,
                matched_archive_version_id=version_id,
            )
            file_id = catalog.get_file_by_path(2, "report.txt")["id"]
            protected, _, _ = catalog.queue_jobs(
                vault_id=2,
                path="report.txt",
                action="upload",
                requested_by=7,
                requested_at="2026-07-21T10:02:00+00:00",
                group_id="protected",
                is_directory=False,
            )
            catalog.observe_local_copy(
                vault_id=2,
                path="report.txt",
                file_type="regular",
                size=20,
                mtime_ns=2,
                observed_at="2026-07-21T10:03:00+00:00",
            )
            changed, _, eligible = catalog.queue_jobs(
                vault_id=2,
                path="report.txt",
                action="upload",
                requested_by=7,
                requested_at="2026-07-21T10:04:00+00:00",
                group_id="changed",
                is_directory=False,
            )
            row = catalog.list_file_rows(2)[0]
            after_id = catalog.get_file_by_path(2, "report.txt")["id"]
        self.assertEqual(protected, [])
        self.assertEqual(len(changed), 1)
        self.assertEqual(eligible, 1)
        self.assertTrue(row["upload_eligible"])
        self.assertEqual(row["state"], "pending")
        self.assertEqual(after_id, file_id)

    def test_terminal_job_commits_when_notification_savepoint_fails_on_postgresql(
        self,
    ) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

        connect_url = self.postgres_url
        with psycopg.connect(connect_url, row_factory=dict_row) as connection:
            user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash)
                VALUES ('notification-owner', 'Notification Owner', 'hash')
                RETURNING id
                """
            ).fetchone()["id"]
            vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix) VALUES ('notification-vault', 'Notification Vault', '/source',
                          'bucket', 'notifications')
                RETURNING id
                """
            ).fetchone()["id"]
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=vault_id,
                path="terminal.txt",
                file_type="regular",
                size=12,
                mtime_ns=1,
                observed_at="2026-07-22T10:00:00+00:00",
            )
            job_ids, _, _ = catalog.queue_jobs(
                vault_id=vault_id,
                path="terminal.txt",
                action="upload",
                requested_by=user_id,
                requested_at="2026-07-22T10:01:00+00:00",
                group_id="notification-regression",
                is_directory=False,
            )
            self.assertEqual(len(job_ids), 1)
            job_id = job_ids[0]
            connection.execute(
                "UPDATE jobs SET status='completed' WHERE id=%s", (job_id,)
            )

            def fail_inside_savepoint(connection, *, job_id):
                connection.execute(
                    """
                    INSERT INTO notifications(
                        user_id, vault_id, job_id, event, title, body, created_at
                    ) VALUES (%s, %s, %s, 'recovery_completed', 'Partial', '', %s)
                    """,
                    (user_id, vault_id, job_id, "2026-07-22T10:02:00+00:00"),
                )
                connection.execute(
                    "INSERT INTO missing_notification_table(id) VALUES (1)"
                )

            with patch.object(
                notifications,
                "enqueue_job_terminal_notification",
                side_effect=fail_inside_savepoint,
            ):
                enqueued = notifications.enqueue_job_terminal_notification_best_effort(
                    connection, job_id=job_id
                )
            self.assertIsNone(enqueued)
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()["status"],
                "completed",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM notifications WHERE job_id=%s",
                    (job_id,),
                ).fetchone()["total"],
                0,
            )

        with psycopg.connect(connect_url, row_factory=dict_row) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (job_id,)
                ).fetchone()["status"],
                "completed",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM notifications WHERE job_id=%s",
                    (job_id,),
                ).fetchone()["total"],
                0,
            )

    def test_single_owner_migration_keeps_only_the_administrator(self) -> None:
        baseline = self.run_alembic("0043_cloud_only_conflict")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'member', 'Member', 'hash', FALSE), "
                    "(2, 'owner', 'Owner', 'hash', TRUE)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO user_identities(user_id, issuer, subject, created_at) "
                    "VALUES (1, 'https://id.example', 'member', '2026-07-20'), "
                    "(2, 'https://id.example', 'owner', '2026-07-20')"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO system_settings(key, value, updated_by, updated_at) "
                    "VALUES ('restore_tier', '\"Standard\"', 1, '2026-07-20')"
                )
            )

        migrated = self.run_alembic("0044_single_owner")
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with self.engine.connect() as connection:
            users = connection.execute(
                sa.text("SELECT id FROM users ORDER BY id")
            ).scalars().all()
            identities = connection.execute(
                sa.text("SELECT user_id, subject FROM user_identities")
            ).all()
            updated_by = connection.execute(
                sa.text("SELECT updated_by FROM system_settings")
            ).scalar_one()
        self.assertEqual(users, [2])
        self.assertEqual([tuple(row) for row in identities], [(2, "owner")])
        self.assertEqual(updated_by, 2)

    def test_issue_188_concurrent_confirms_consume_one_candidate_once(self) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        missing_id = self._seed_rename_candidate()
        start = threading.Barrier(2)

        def confirm_once(_: int) -> str:
            connection = self._connection()
            try:
                try:
                    with connection:
                        start.wait(timeout=20)
                        ArchiveCatalog(connection).confirm_file_rename(
                            vault_file_id=missing_id,
                            new_path="concurrent/new.txt",
                            changed_at="2026-07-21T11:05:00+00:00",
                            vault_id=2,
                        )
                except VaultFileNotFound:
                    return "not_found"
                return "confirmed"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(confirm_once, range(2)))

        self.assertEqual(outcomes.count("confirmed"), 1, outcomes)
        self.assertEqual(outcomes.count("not_found"), 1, outcomes)
        with self._connection() as connection:
            catalog = ArchiveCatalog(connection)
            current = catalog.get_file_by_path(2, "concurrent/new.txt")
            history = catalog.list_path_history(missing_id, vault_id=2)
            retired = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_files WHERE status='retired'"
            ).fetchone()["total"]
            copies = connection.execute(
                "SELECT COUNT(*) AS total FROM local_copies"
            ).fetchone()["total"]

        self.assertIsNotNone(current)
        self.assertEqual(current["id"], missing_id)
        self.assertEqual(
            [entry["path"] for entry in history],
            ["concurrent/old.txt", "concurrent/new.txt"],
        )
        self.assertEqual(retired, 1)
        self.assertEqual(copies, 1)

    def test_issue_188_scan_state_change_wins_before_confirmation_claim(self) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        missing_id = self._seed_rename_candidate()
        scanner_updated = threading.Event()
        release_scanner = threading.Event()
        scanner_done = threading.Event()
        scanner_errors: list[BaseException] = []

        def scanner_state_change() -> None:
            connection = self._connection()
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE local_copies
                        SET presence='present', plaintext_sha256=%s, observed_at=%s
                        WHERE vault_file_id=%s
                        """,
                        ("b" * 64, "2026-07-21T12:00:00+00:00", missing_id),
                    )
                    scanner_updated.set()
                    if not release_scanner.wait(timeout=20):
                        raise TimeoutError("test did not release scanner transaction")
            except BaseException as exc:  # Preserve failures from the worker thread.
                scanner_errors.append(exc)
            finally:
                connection.close()
                scanner_done.set()

        scanner = threading.Thread(target=scanner_state_change)
        scanner.start()
        self.addCleanup(release_scanner.set)
        self.addCleanup(scanner.join)
        self.assertTrue(scanner_updated.wait(timeout=20))

        reservation_attempted = threading.Event()
        original_reservation = ArchiveCatalog._reserve_rename_confirmation

        def note_reservation(catalog: ArchiveCatalog, vault_id: int) -> None:
            reservation_attempted.set()
            original_reservation(catalog, vault_id)

        confirmation_outcome: list[str] = []

        def confirm_while_scanner_holds_copy_lock() -> None:
            connection = self._connection()
            try:
                try:
                    with connection:
                        ArchiveCatalog(connection).confirm_file_rename(
                            vault_file_id=missing_id,
                            new_path="concurrent/new.txt",
                            changed_at="2026-07-21T12:01:00+00:00",
                            vault_id=2,
                        )
                except VaultFileNotFound:
                    confirmation_outcome.append("not_found")
                else:
                    confirmation_outcome.append("confirmed")
            finally:
                connection.close()

        confirmer = threading.Thread(target=confirm_while_scanner_holds_copy_lock)
        try:
            with patch.object(
                ArchiveCatalog,
                "_reserve_rename_confirmation",
                autospec=True,
                side_effect=note_reservation,
            ):
                confirmer.start()
                self.assertTrue(reservation_attempted.wait(timeout=20))
                release_scanner.set()
                self.assertTrue(scanner_done.wait(timeout=20))
                confirmer.join(timeout=20)
        finally:
            release_scanner.set()
            scanner.join(timeout=20)
            confirmer.join(timeout=20)

        self.assertFalse(scanner.is_alive())
        self.assertFalse(confirmer.is_alive())
        self.assertEqual(scanner_errors, [])
        self.assertEqual(confirmation_outcome, ["not_found"])
        with self._connection() as connection:
            source_copy = connection.execute(
                """
                SELECT presence, plaintext_sha256
                FROM local_copies WHERE vault_file_id=%s
                """,
                (missing_id,),
            ).fetchone()
            active = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_files WHERE status='active'"
            ).fetchone()["total"]
            current_paths = connection.execute(
                "SELECT COUNT(*) AS total FROM file_paths WHERE valid_to IS NULL"
            ).fetchone()["total"]

        self.assertEqual(source_copy["presence"], "present")
        self.assertEqual(source_copy["plaintext_sha256"], "b" * 64)
        self.assertEqual(active, 2)
        self.assertEqual(current_paths, 2)

    def test_issue_193_postgresql_claim_is_atomic_across_two_workers(self) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (9, 'claims', 'Claims', '/source', 'bucket', 'claims')
                """
            )
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(
                vault_id=9,
                path="one.txt",
                file_type="regular",
                size=1,
                mtime_ns=1,
                observed_at="2026-08-01T10:00:00+00:00",
            )
            job_id = connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status,
                    requested_at, updated_at
                ) VALUES (
                    9, %s, 'one.txt', 'upload', 'queued',
                    '2026-08-01T10:00:00+00:00', '2026-08-01T10:00:00+00:00'
                ) RETURNING id
                """,
                (file_id,),
            ).fetchone()["id"]

        start = threading.Barrier(2)

        def claim_once(worker: int) -> str | None:
            connection = self._connection()
            try:
                with connection:
                    start.wait(timeout=20)
                    claimed = ArchiveCatalog(connection).claim_job(
                        job_id=int(job_id),
                        claim_token=f"postgres-worker-{worker}",
                        claimed_at="2026-08-01T10:01:00+00:00",
                        claim_expires_at="2026-08-01T10:06:00+00:00",
                        now="2026-08-01T10:01:00+00:00",
                        restore_due_before="2026-08-01T10:00:00+00:00",
                    )
                    return claimed["claim_token"] if claimed else None
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(claim_once, range(2)))

        self.assertEqual(sum(outcome is not None for outcome in outcomes), 1)
        with self._connection() as connection:
            job = connection.execute(
                """
                SELECT status, claim_token, claim_expires_at
                FROM jobs WHERE id=%s
                """,
                (job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "queued")
        self.assertIn(job["claim_token"], {outcome for outcome in outcomes if outcome})
        self.assertEqual(job["claim_expires_at"], "2026-08-01T10:06:00+00:00")

    def test_issue_193_postgresql_expired_takeover_cancel_and_group_contention(self) -> None:
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (10, 'claim-groups', 'Claim Groups', '/source',
                          'bucket', 'claims')
                """
            )
            catalog = ArchiveCatalog(connection)
            file_ids = [
                catalog.observe_local_copy(
                    vault_id=10,
                    path=path,
                    file_type="regular",
                    size=1,
                    mtime_ns=index,
                    observed_at="2026-08-02T10:00:00+00:00",
                )
                for index, path in enumerate(
                    ("ordinary.txt", "group-one.txt", "group-two.txt"), start=1
                )
            ]
            ordinary_job_id = connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status,
                    requested_at, updated_at, claim_token, claimed_at,
                    claim_expires_at
                ) VALUES (
                    10, %s, 'ordinary.txt', 'upload', 'queued',
                    '2026-08-02T10:00:00+00:00', '2026-08-02T10:00:00+00:00',
                    'dead-worker', '2026-08-02T10:00:00+00:00',
                    '2000-01-01T00:00:00+00:00'
                ) RETURNING id
                """,
                (file_ids[0],),
            ).fetchone()["id"]
            group_job_ids: list[int] = []
            for file_id, path in zip(file_ids[1:], ("group-one.txt", "group-two.txt")):
                group_job_ids.append(
                    int(
                        connection.execute(
                            """
                            INSERT INTO jobs(
                                vault_id, vault_file_id, path, action, status,
                                requested_at, updated_at, group_id
                            ) VALUES (
                                10, %s, %s, 'cloud-purge', 'queued',
                                '2026-08-02T10:00:00+00:00', '2026-08-02T10:00:00+00:00',
                                'postgres-group'
                            ) RETURNING id
                            """,
                            (file_id, path),
                        ).fetchone()["id"]
                    )
                )

            taken = catalog.claim_job(
                job_id=int(ordinary_job_id),
                claim_token="takeover-worker",
                claimed_at="2026-08-02T10:01:00+00:00",
                claim_expires_at="2026-08-02T10:06:00+00:00",
                now="2026-08-02T10:01:00+00:00",
                restore_due_before="2026-08-02T10:00:00+00:00",
            )
            self.assertIsNotNone(taken)
            connection.execute(
                "UPDATE jobs SET status='cancelled' WHERE id=%s",
                (ordinary_job_id,),
            )
            self.assertIsNone(
                catalog.renew_job_claim(
                    job_id=int(ordinary_job_id),
                    claim_token="takeover-worker",
                    now="2026-08-02T10:02:00+00:00",
                    claim_expires_at="2026-08-02T10:07:00+00:00",
                )
            )

        start = threading.Barrier(2)

        def claim_group(worker: int) -> list[int]:
            connection = self._connection()
            try:
                with connection:
                    start.wait(timeout=20)
                    rows = ArchiveCatalog(connection).claim_purge_group(
                        lead_job_id=group_job_ids[0],
                        claim_token=f"group-worker-{worker}",
                        claimed_at="2026-08-02T10:03:00+00:00",
                        claim_expires_at="2026-08-02T10:08:00+00:00",
                        now="2026-08-02T10:03:00+00:00",
                        message="deleting",
                        message_key="job.cloud_purge_deleting",
                    )
                    return [int(row["id"]) for row in rows]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(claim_group, range(2)))
        self.assertEqual(sorted(len(outcome) for outcome in outcomes), [0, 2])
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT status, claim_token FROM jobs
                WHERE group_id='postgres-group' ORDER BY id
                """
            ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["cleaning", "cleaning"])
        self.assertEqual(len({row["claim_token"] for row in rows}), 1)

    def test_auth_backoff_concurrent_threshold_results_keep_matching_deadlines(
        self,
    ) -> None:
        """A real PostgreSQL row lock preserves each count/deadline pair."""
        upgraded = self.run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        attempts = backoff.THRESHOLD + 2
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        start = threading.Barrier(attempts)

        def record() -> tuple[int, str | None]:
            with self._connection() as connection:
                start.wait(timeout=20)
                return backoff.record_failure(
                    connection,
                    scope="ip",
                    key="concurrent-backoff",
                )

        with patch.object(backoff, "_now", return_value=now):
            with ThreadPoolExecutor(max_workers=attempts) as pool:
                results = list(pool.map(lambda _: record(), range(attempts)))

        expected = [
            (
                count,
                (
                    now
                    + timedelta(seconds=backoff._backoff_seconds(count))
                ).isoformat()
                if backoff._backoff_seconds(count)
                else None,
            )
            for count in range(1, attempts + 1)
        ]
        self.assertEqual(sorted(results), expected)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT failure_count, next_allowed_at
                FROM auth_backoff
                WHERE scope='ip' AND key='concurrent-backoff'
                """
            ).fetchone()
        self.assertEqual(
            (row["failure_count"], row["next_allowed_at"]),
            expected[-1],
        )
