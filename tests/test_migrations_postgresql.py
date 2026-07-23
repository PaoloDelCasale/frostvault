from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import sqlalchemy as sa
import psycopg
from psycopg.rows import dict_row

from app.catalog import ArchiveCatalog


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL", "")
REPOSITORY_ROOT = Path(__file__).parents[1]


def run_alembic(
    revision: str = "head",
    command: str = "upgrade",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            f"database_url={POSTGRES_URL}",
            command,
            revision,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@unittest.skipUnless(POSTGRES_URL, "TEST_POSTGRES_URL is not configured")
class PostgreSQLMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sa.create_engine(POSTGRES_URL)
        with self.engine.begin() as connection:
            connection.execute(sa.text("DROP SCHEMA public CASCADE"))
            connection.execute(sa.text("CREATE SCHEMA public"))

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_fresh_database_upgrades_and_rolls_back_losslessly(self) -> None:
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        quota_columns = {
            column["name"]
            for column in sa.inspect(self.engine).get_columns("vault_quotas")
        }
        self.assertEqual(
            quota_columns,
            {
                "vault_id",
                "storage_soft_limit_bytes",
                "storage_hard_limit_bytes",
                "concurrency_soft_limit",
                "concurrency_hard_limit",
                "restore_30d_soft_limit_bytes",
                "restore_30d_hard_limit_bytes",
            },
        )

        downgraded = run_alembic("0001_current_schema", command="downgrade")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)

        upgraded_again = run_alembic()
        self.assertEqual(upgraded_again.returncode, 0, upgraded_again.stderr)

    def test_current_release_data_migrates_without_loss(self) -> None:
        baseline = run_alembic("0001_current_schema")
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

        migrated = run_alembic()
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
        baseline = run_alembic("0006_auth_backoff")
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

        migrated = run_alembic("0007_vault_ownership")
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
        baseline = run_alembic("0006_auth_backoff")
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

        migrated = run_alembic("0007_vault_ownership")
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
        baseline = run_alembic("0006_auth_backoff")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, "
                    "s3_prefix, rclone_remote) VALUES (1, 'orphan', 'Orphan', "
                    "'/source', 'bucket', 'orphan', 'remote')"
                )
            )

        migrated = run_alembic("0007_vault_ownership")
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
        upgraded = run_alembic()
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
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

        connect_url = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")
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
