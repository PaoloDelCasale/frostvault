from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import INTEGRITY_ERRORS, SQLiteConnection
from tests.test_database import run_alembic


class VaultOwnershipMigrationTests(unittest.TestCase):
    def test_vault_gains_an_immutable_generated_namespace_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "namespace.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                first = connection.execute(
                    "SELECT uuid FROM vaults WHERE id=1"
                ).fetchone()["uuid"]

            self.assertIsNotNone(first)
            self.assertEqual(len(first), 36)

            # The name/slug remain labels; the generated UUID namespace does
            # not change when the label does.
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "UPDATE vaults SET slug='documents', name='Documents' "
                    "WHERE id=1"
                )
                after_rename = connection.execute(
                    "SELECT uuid FROM vaults WHERE id=1"
                ).fetchone()["uuid"]

            self.assertEqual(after_rename, first)

    def test_two_vaults_never_collide_on_the_generated_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collision.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'a', 'A', '/source-a', 'bucket', 'a', 'remote')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'b', 'B', '/source-b', 'bucket', 'b', 'remote')
                    """
                )
                rows = connection.execute(
                    "SELECT id, uuid FROM vaults ORDER BY id"
                ).fetchall()

            self.assertNotEqual(rows[0]["uuid"], rows[1]["uuid"])

            # The database itself refuses a duplicate namespace, not just the
            # application, so concurrent creation cannot collide silently.
            with SQLiteConnection(str(path)) as connection:
                with self.assertRaises(INTEGRITY_ERRORS):
                    connection.execute(
                        "UPDATE vaults SET uuid=%s WHERE id=2", (rows[0]["uuid"],)
                    )

    def test_a_vault_can_have_only_one_primary_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one-owner.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'alice', 'Alice', 'hash', 0)"
                )
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (2, 'bob', 'Bob', 'hash', 0)"
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'owner')"
                )

                with self.assertRaises(INTEGRITY_ERRORS):
                    connection.execute(
                        "INSERT INTO vault_members(vault_id, user_id, role) "
                        "VALUES (1, 2, 'owner')"
                    )

    def test_ownership_transfer_only_updates_the_membership_row(self) -> None:
        """Transferring the primary owner never touches Files or Archive
        Versions; it is a role swap on vault_members alone."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transfer.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'alice', 'Alice', 'hash', 0)"
                )
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (2, 'bob', 'Bob', 'hash', 0)"
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'owner')"
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 2, 'operator')"
                )
                connection.execute(
                    """
                    INSERT INTO vault_files(id, vault_id, status, created_at)
                    VALUES ('11111111-1111-1111-1111-111111111111', 1, 'active',
                    '2026-07-20T10:00:00+00:00')
                    """
                )
                namespace_before = connection.execute(
                    "SELECT uuid FROM vaults WHERE id=1"
                ).fetchone()["uuid"]
                file_id_before = connection.execute(
                    "SELECT id FROM vault_files WHERE vault_id=1"
                ).fetchone()["id"]

                # Transfer: demote the current owner, promote the operator.
                connection.execute(
                    "UPDATE vault_members SET role='operator' "
                    "WHERE vault_id=1 AND user_id=1"
                )
                connection.execute(
                    "UPDATE vault_members SET role='owner' "
                    "WHERE vault_id=1 AND user_id=2"
                )

                roles = {
                    row["user_id"]: row["role"]
                    for row in connection.execute(
                        "SELECT user_id, role FROM vault_members WHERE vault_id=1"
                    ).fetchall()
                }
                namespace_after = connection.execute(
                    "SELECT uuid FROM vaults WHERE id=1"
                ).fetchone()["uuid"]
                file_id_after = connection.execute(
                    "SELECT id FROM vault_files WHERE vault_id=1"
                ).fetchone()["id"]

            self.assertEqual(roles, {1: "operator", 2: "owner"})
            self.assertEqual(namespace_before, namespace_after)
            self.assertEqual(file_id_before, file_id_after)

    def test_operator_role_is_accepted_and_bogus_roles_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'alice', 'Alice', 'hash', 0)"
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'operator')"
                )
                role = connection.execute(
                    "SELECT role FROM vault_members WHERE vault_id=1 AND user_id=1"
                ).fetchone()["role"]
                self.assertEqual(role, "operator")

                with self.assertRaises(INTEGRITY_ERRORS):
                    connection.execute(
                        "UPDATE vault_members SET role='superuser' "
                        "WHERE vault_id=1 AND user_id=1"
                    )

    def test_migrating_a_normal_vault_preserves_all_memberships(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "normal-owner.db"
            baseline = run_alembic(path, "0006_auth_backoff")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)

            with SQLiteConnection(str(path)) as connection:
                for user_id, username in ((1, "alice"), (2, "bob")):
                    connection.execute(
                        "INSERT INTO users(id, username, display_name, password_hash, "
                        "is_admin) VALUES (%s, %s, %s, 'hash', 0)",
                        (user_id, username, username.title()),
                    )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'owner')"
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 2, 'viewer')"
                )

            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(path)) as connection:
                roles = {
                    row["user_id"]: row["role"]
                    for row in connection.execute(
                        "SELECT user_id, role FROM vault_members WHERE vault_id=1"
                    ).fetchall()
                }
            self.assertEqual(roles, {1: "owner", 2: "viewer"})

    def test_migrating_a_zero_owner_vault_fails_without_widening_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zero-owner.db"
            baseline = run_alembic(path, "0006_auth_backoff")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)

            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'alice', 'Alice', 'hash', 0)"
                )
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (2, 'bob', 'Bob', 'hash', 0)"
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 2, 'viewer')"
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'viewer')"
                )

            migrated = run_alembic(path)
            self.assertNotEqual(migrated.returncode, 0)
            self.assertIn("vault 1 has no primary owner", migrated.stderr)
            self.assertIn("assign exactly one authorized existing member", migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                roles = {
                    row["user_id"]: row["role"]
                    for row in connection.execute(
                        "SELECT user_id, role FROM vault_members WHERE vault_id=1"
                    ).fetchall()
                }
                version = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()["version_num"]
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(vaults)").fetchall()
                }
            self.assertEqual(roles, {1: "viewer", 2: "viewer"})
            self.assertEqual(version, "0006_auth_backoff")
            self.assertNotIn("uuid", columns)

    def test_migrating_a_vault_without_members_fails_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ownerless.db"
            baseline = run_alembic(path, "0006_auth_backoff")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )

            migrated = run_alembic(path)
            self.assertNotEqual(migrated.returncode, 0)
            self.assertIn("vault 1 has no primary owner", migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone()["version_num"],
                    "0006_auth_backoff",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT role FROM vault_members WHERE vault_id=1"
                    ).fetchall(),
                    [],
                )
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(vaults)").fetchall()
                }
                members_schema = connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='vault_members'"
                ).fetchone()["sql"]
            self.assertNotIn("uuid", columns)
            self.assertNotIn("'operator'", members_schema)

    def test_migrating_a_multi_owner_vault_narrows_instead_of_widening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "narrow.db"
            baseline = run_alembic(path, "0006_auth_backoff")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)

            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'alice', 'Alice', 'hash', 0)"
                )
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (2, 'bob', 'Bob', 'hash', 0)"
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                # Pre-migration data: two equivalent owners on one vault, the
                # exact widened-access shape #7 must resolve.
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 2, 'owner')"
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'owner')"
                )

            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)

            with SQLiteConnection(str(path)) as connection:
                roles = {
                    row["user_id"]: row["role"]
                    for row in connection.execute(
                        "SELECT user_id, role FROM vault_members WHERE vault_id=1"
                    ).fetchall()
                }
                owners = connection.execute(
                    "SELECT COUNT(*) AS total FROM vault_members "
                    "WHERE vault_id=1 AND role='owner'"
                ).fetchone()["total"]

            # The deterministically-first owner keeps ownership; the other
            # membership is narrowed to operator, never left as (or promoted
            # to) a role it did not already hold.
            self.assertEqual(owners, 1)
            self.assertEqual(roles[1], "owner")
            self.assertEqual(roles[2], "operator")

    def test_downgrade_restores_the_pre_operator_schema_losslessly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roundtrip.db"
            upgraded = run_alembic(path)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (1, 'alice', 'Alice', 'hash', 0)"
                )
                connection.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, "
                    "is_admin) VALUES (2, 'bob', 'Bob', 'hash', 0)"
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 1, 'owner')"
                )
                connection.execute(
                    "INSERT INTO vault_members(vault_id, user_id, role) "
                    "VALUES (1, 2, 'operator')"
                )

            downgraded = run_alembic(path, "0006_auth_backoff", command="downgrade")
            self.assertEqual(downgraded.returncode, 0, downgraded.stderr)

            with SQLiteConnection(str(path)) as connection:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(vaults)"
                    ).fetchall()
                }
                role = connection.execute(
                    "SELECT role FROM vault_members WHERE vault_id=1 AND user_id=2"
                ).fetchone()["role"]

            self.assertNotIn("uuid", columns)
            # Operator has no place in the old constraint; it narrows to
            # viewer rather than blocking the downgrade.
            self.assertEqual(role, "viewer")

            upgraded_again = run_alembic(path)
            self.assertEqual(upgraded_again.returncode, 0, upgraded_again.stderr)
