"""Exclusive Source Area grants and directory browsers (issue #149).

Seams under test (confirmed):
1. One User can hold multiple disjoint grants
2. Exact/ancestor/descendant overlap rejected atomically
3. Admin mutations require reauth + reason and produce audit + notification
4. User deactivation preserves grants; reactivation restores use
5. Missing/renamed paths become unavailable without retargeting
6. Admin browser shows occupied Vault metadata; User browser is generic
7. Occupied descendants: ancestors navigable but non-selectable; roots not traversable
8. Path traversal, symlinks, nested mounts, managed paths fail closed
9. Source Area changes never modify vaults or vault_members
10. en/it strings complete; browser usable at 375px
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.database import SQLiteConnection
from app.services import source_areas, source_layout
from tests.test_database import run_alembic

_ASSIGN = dict(actor_user_id=99, reason="delegate photos subtree for archive creation")


class SourceAreaGrantTests(unittest.TestCase):
    """Seam 1: persist and retrieve exclusive Source Area grants."""

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
        (self.photos / "albums").mkdir()

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (99, 'admin', 'Admin', 'hash', 1)"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        patcher = patch("app.database.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_user_can_hold_a_source_area_on_a_healthy_volume(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            grant = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="albums",
                **_ASSIGN,
            )
            connection.commit()

        self.assertEqual(grant["user_id"], 1)
        self.assertEqual(grant["volume_alias"], "photos")
        self.assertEqual(grant["relative_path"], "albums")
        self.assertEqual(grant["availability"], "available")

        with SQLiteConnection(str(self.database_path)) as connection:
            listed = source_areas.list_source_areas_for_user(connection, user_id=1)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], grant["id"])
        self.assertEqual(listed[0]["volume_alias"], "photos")
        self.assertEqual(listed[0]["relative_path"], "albums")
        self.assertEqual(listed[0]["availability"], "available")

    def test_user_can_hold_multiple_disjoint_source_areas(self) -> None:
        (self.photos / "raw").mkdir()
        documents = self.sources_root / "documents"
        documents.mkdir()
        (documents / "contracts").mkdir()

        with SQLiteConnection(str(self.database_path)) as connection:
            first = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="albums",
                **_ASSIGN,
            )
            second = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="raw",
                **_ASSIGN,
            )
            third = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="documents",
                relative_path="contracts",
                **_ASSIGN,
            )
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            listed = source_areas.list_source_areas_for_user(connection, user_id=1)

        self.assertEqual(
            {(item["volume_alias"], item["relative_path"]) for item in listed},
            {
                ("photos", "albums"),
                ("photos", "raw"),
                ("documents", "contracts"),
            },
        )
        self.assertEqual({item["id"] for item in listed}, {first["id"], second["id"], third["id"]})
        self.assertTrue(all(item["availability"] == "available" for item in listed))


class SourceAreaOverlapTests(unittest.TestCase):
    """Seam 2: reject exact/ancestor/descendant overlap atomically."""

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
        (self.photos / "albums").mkdir()
        (self.photos / "albums" / "2024").mkdir()
        (self.photos / "raw").mkdir()

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (99, 'admin', 'Admin', 'hash', 1)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (2, 'bob', 'Bob', 'hash', 0)"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        patcher = patch("app.database.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_exact_overlap_is_rejected_for_same_and_other_user(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="albums",
                **_ASSIGN,
            )
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(source_areas.SourceAreaError) as same_user:
                source_areas.assign_source_area(
                    connection,
                    user_id=1,
                    volume_alias="photos",
                    relative_path="albums",
                    **_ASSIGN,
                )
            self.assertEqual(same_user.exception.reason, "overlap")

            with self.assertRaises(source_areas.SourceAreaError) as other_user:
                source_areas.assign_source_area(
                    connection,
                    user_id=2,
                    volume_alias="photos",
                    relative_path="albums",
                    **_ASSIGN,
                )
            self.assertEqual(other_user.exception.reason, "overlap")

    def test_ancestor_and_descendant_overlap_are_rejected(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="albums",
                **_ASSIGN,
            )
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(source_areas.SourceAreaError) as descendant:
                source_areas.assign_source_area(
                    connection,
                    user_id=2,
                    volume_alias="photos",
                    relative_path="albums/2024",
                    **_ASSIGN,
                )
            self.assertEqual(descendant.exception.reason, "overlap")

            with self.assertRaises(source_areas.SourceAreaError) as ancestor:
                source_areas.assign_source_area(
                    connection,
                    user_id=2,
                    volume_alias="photos",
                    relative_path="",
                    **_ASSIGN,
                )
            self.assertEqual(ancestor.exception.reason, "overlap")

        with SQLiteConnection(str(self.database_path)) as connection:
            listed = source_areas.list_source_areas_for_user(connection, user_id=2)
        self.assertEqual(listed, [])


class SourceAreaAdminMutationTests(unittest.TestCase):
    """Seam 3: assign/revoke require reason and produce audit + notification."""

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
        (self.photos / "albums").mkdir()

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (99, 'admin', 'Admin', 'hash', 1)"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        patcher = patch("app.database.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_assign_requires_reason_and_notifies_grantee(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(source_areas.SourceAreaError) as missing:
                source_areas.assign_source_area(
                    connection,
                    user_id=1,
                    volume_alias="photos",
                    relative_path="albums",
                    actor_user_id=99,
                    reason="ab",
                )
            self.assertEqual(missing.exception.reason, "reason_required")

            grant = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="albums",
                actor_user_id=99,
                reason="delegate albums for family archives",
            )
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            audit = connection.execute(
                "SELECT event, outcome, actor_user_id, detail_json FROM audit_events "
                "WHERE event=%s ORDER BY id DESC LIMIT 1",
                ("source_area_assigned",),
            ).fetchone()
            notification = connection.execute(
                "SELECT user_id, event, body FROM notifications "
                "WHERE event=%s ORDER BY id DESC LIMIT 1",
                ("source_area_assigned",),
            ).fetchone()

        self.assertIsNotNone(audit)
        self.assertEqual(audit["outcome"], "success")
        self.assertEqual(audit["actor_user_id"], 99)
        self.assertIn("delegate albums for family archives", audit["detail_json"])
        self.assertIn(str(grant["id"]), audit["detail_json"])

        self.assertIsNotNone(notification)
        self.assertEqual(notification["user_id"], 1)
        self.assertIn("delegate albums for family archives", notification["body"])

    def test_revoke_removes_grant_with_audit_and_notification(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            grant = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="albums",
                **_ASSIGN,
            )
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            source_areas.revoke_source_area(
                connection,
                source_area_id=grant["id"],
                actor_user_id=99,
                reason="reclaim albums after project ended",
            )
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            listed = source_areas.list_source_areas_for_user(connection, user_id=1)
            audit = connection.execute(
                "SELECT event, actor_user_id, detail_json FROM audit_events "
                "WHERE event=%s ORDER BY id DESC LIMIT 1",
                ("source_area_revoked",),
            ).fetchone()
            notification = connection.execute(
                "SELECT user_id, event, body FROM notifications "
                "WHERE event=%s ORDER BY id DESC LIMIT 1",
                ("source_area_revoked",),
            ).fetchone()

        self.assertEqual(listed, [])
        self.assertEqual(audit["actor_user_id"], 99)
        self.assertIn("reclaim albums after project ended", audit["detail_json"])
        self.assertEqual(notification["user_id"], 1)
        self.assertIn("reclaim albums after project ended", notification["body"])


class SourceAreaAdminHttpTests(unittest.TestCase):
    """Seam 3 HTTP: admin assign requires recent Reauthentication."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.security import hash_password
        from app.sessions import create_session

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(source_layout.reset_sources_root_override)

        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()
        (self.sources_root / "managed").mkdir()
        self.photos = self.sources_root / "photos"
        self.photos.mkdir()
        (self.photos / "albums").mkdir()
        source_layout.override_sources_root(self.sources_root)

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
            self.alice_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('alice', 'Alice', %s, FALSE) RETURNING id
                """,
                (hash_password("alice-password-1"),),
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
            "app.services.source_areas.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        mount_patcher = patch.object(
            source_layout,
            "path_is_mount",
            side_effect=lambda path: Path(path) in {self.sources_root, self.photos},
        )
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

    def _csrf(self) -> str:
        return self.client.get("/api/me").json()["csrf_token"]

    def test_assign_requires_recent_reauthentication(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                ("2000-01-01T00:00:00+00:00", self.admin_id),
            )

        denied = self.client.post(
            "/api/admin/source-areas",
            json={
                "user_id": self.alice_id,
                "volume_alias": "photos",
                "relative_path": "albums",
                "reason": "delegate albums for family archives",
            },
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"], "reauth_required")

        with SQLiteConnection(str(self.database_path)) as connection:
            from datetime import datetime, timezone

            connection.execute(
                "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                (datetime.now(timezone.utc).isoformat(), self.admin_id),
            )

        allowed = self.client.post(
            "/api/admin/source-areas",
            json={
                "user_id": self.alice_id,
                "volume_alias": "photos",
                "relative_path": "albums",
                "reason": "delegate albums for family archives",
            },
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(allowed.status_code, 201, allowed.text)
        body = allowed.json()
        self.assertEqual(body["user_id"], self.alice_id)
        self.assertEqual(body["volume_alias"], "photos")
        self.assertEqual(body["relative_path"], "albums")
        self.assertEqual(body["availability"], "available")


class SourceAreaInactiveUserTests(unittest.TestCase):
    """Seam 4: inactive Users keep reserved grants; reactivation restores use."""

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
        (self.photos / "albums").mkdir()

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (99, 'admin', 'Admin', 'hash', 1)"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        patcher = patch("app.database.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_deactivation_preserves_grants_as_reserved_until_reactivation(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            grant = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="albums",
                **_ASSIGN,
            )
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            active = source_areas.list_source_areas_for_user(connection, user_id=1)
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["usable"])

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE users SET active=FALSE WHERE id=1")
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            reserved = source_areas.list_source_areas_for_user(connection, user_id=1)
            count = connection.execute(
                "SELECT COUNT(*) AS total FROM source_areas WHERE user_id=1"
            ).fetchone()["total"]

        self.assertEqual(count, 1)
        self.assertEqual(len(reserved), 1)
        self.assertEqual(reserved[0]["id"], grant["id"])
        self.assertFalse(reserved[0]["usable"])
        self.assertEqual(reserved[0]["availability"], "available")

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE users SET active=TRUE WHERE id=1")
            connection.commit()

        with SQLiteConnection(str(self.database_path)) as connection:
            restored = source_areas.list_source_areas_for_user(connection, user_id=1)
        self.assertEqual(len(restored), 1)
        self.assertTrue(restored[0]["usable"])


class SourceAreaAvailabilityTests(unittest.TestCase):
    """Seam 5: missing/renamed paths become unavailable without retargeting."""

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

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (99, 'admin', 'Admin', 'hash', 1)"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        patcher = patch("app.database.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch.object(source_layout, "path_is_mount", return_value=True)
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_missing_or_renamed_path_marks_grant_unavailable_without_retarget(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            grant = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="albums",
                **_ASSIGN,
            )
            connection.commit()

        renamed = self.photos / "albums-renamed"
        self.albums.rename(renamed)

        with SQLiteConnection(str(self.database_path)) as connection:
            listed = source_areas.list_source_areas_for_user(connection, user_id=1)
            row = connection.execute(
                "SELECT relative_path FROM source_areas WHERE id=%s",
                (grant["id"],),
            ).fetchone()

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], grant["id"])
        self.assertEqual(listed[0]["relative_path"], "albums")
        self.assertEqual(listed[0]["availability"], "unavailable")
        # Stored path is never rewritten to follow the rename.
        self.assertEqual(row["relative_path"], "albums")
        self.assertTrue(renamed.is_dir())


class SourceAreaBrowserTests(unittest.TestCase):
    """Seams 6–7: lazy directory browser with occupied Vault rules."""

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
        self.family = self.photos / "family"
        self.family.mkdir()
        self.vacation = self.family / "vacation"
        self.vacation.mkdir()
        (self.family / "notes.txt").write_text("ignore files", encoding="utf-8")
        self.free = self.photos / "free"
        self.free.mkdir()

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch.object(
            source_layout,
            "path_is_mount",
            side_effect=lambda path: Path(path) in {self.sources_root, self.photos},
        )
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

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
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (
                    1, 'vacation', 'Vacation Archive', %s, 'b', 'p/', 'r'
                )
                """,
                (str(self.vacation),),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (1, 2, 'owner')"
            )
            source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="family",
                **_ASSIGN,
            )
            connection.commit()

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        patcher = patch("app.database.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_admin_browser_shows_occupied_vault_metadata(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            listing = source_areas.browse_source_directories(
                connection,
                volume_alias="photos",
                relative_path="family",
                viewer_user_id=99,
                viewer_is_admin=True,
            )

        names = {item["name"]: item for item in listing["items"]}
        self.assertNotIn("notes.txt", names)
        self.assertIn("vacation", names)
        occupied = names["vacation"]
        self.assertFalse(occupied["navigable"])
        self.assertFalse(occupied["selectable"])
        self.assertEqual(occupied["occupation"]["kind"], "vault")
        self.assertEqual(occupied["occupation"]["vault_name"], "Vacation Archive")
        self.assertEqual(occupied["occupation"]["owner_display_name"], "Bob")

    def test_user_browser_hides_vault_details_and_scopes_to_source_areas(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            at_volume_root = source_areas.browse_source_directories(
                connection,
                volume_alias="photos",
                relative_path="",
                viewer_user_id=1,
                viewer_is_admin=False,
            )
            inside_area = source_areas.browse_source_directories(
                connection,
                volume_alias="photos",
                relative_path="family",
                viewer_user_id=1,
                viewer_is_admin=False,
            )

        # User only sees their Source Area roots at the volume level, not free/.
        root_names = {item["name"] for item in at_volume_root["items"]}
        self.assertEqual(root_names, {"family"})

        names = {item["name"]: item for item in inside_area["items"]}
        occupied = names["vacation"]
        self.assertFalse(occupied["navigable"])
        self.assertFalse(occupied["selectable"])
        self.assertEqual(occupied["occupation"]["kind"], "vault")
        self.assertEqual(occupied["occupation"]["label"], "Occupied by a Vault")
        self.assertNotIn("vault_name", occupied["occupation"])
        self.assertNotIn("owner_display_name", occupied["occupation"])

    def test_ancestor_of_occupied_is_navigable_but_not_selectable(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            listing = source_areas.browse_source_directories(
                connection,
                volume_alias="photos",
                relative_path="",
                viewer_user_id=99,
                viewer_is_admin=True,
            )

        names = {item["name"]: item for item in listing["items"]}
        family = names["family"]
        free = names["free"]
        self.assertTrue(family["navigable"])
        self.assertFalse(family["selectable"])
        self.assertTrue(free["navigable"])
        self.assertTrue(free["selectable"])


class SourceAreaSecurityTests(unittest.TestCase):
    """Seams 8–9: fail-closed path security; mutations never touch Vault rows."""

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
        (self.photos / "albums").mkdir()

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (99, 'admin', 'Admin', 'hash', 1)"
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (1, 'docs', 'Docs', %s, 'b', 'p/', 'r')
                """,
                (str(self.photos / "albums"),),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (1, 1, 'owner')"
            )

        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        patcher = patch("app.database.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        mount_patcher = patch.object(
            source_layout,
            "path_is_mount",
            side_effect=lambda path: Path(path) in {self.sources_root, self.photos},
        )
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)

    def test_path_traversal_managed_and_symlinks_fail_closed(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(source_areas.SourceAreaError) as traversal:
                source_areas.assign_source_area(
                    connection,
                    user_id=1,
                    volume_alias="photos",
                    relative_path="../managed",
                    **_ASSIGN,
                )
            self.assertEqual(traversal.exception.reason, "invalid_path")

            with self.assertRaises(source_areas.SourceAreaError) as managed:
                source_areas.assign_source_area(
                    connection,
                    user_id=1,
                    volume_alias="managed",
                    relative_path="",
                    **_ASSIGN,
                )
            self.assertEqual(managed.exception.reason, "invalid_volume")

            with patch.object(
                source_layout,
                "path_is_symlink",
                side_effect=lambda path: Path(path) == self.photos / "albums",
            ):
                with self.assertRaises(source_areas.SourceAreaError) as symlink:
                    source_areas.assign_source_area(
                        connection,
                        user_id=1,
                        volume_alias="photos",
                        relative_path="albums",
                        **_ASSIGN,
                    )
                self.assertEqual(symlink.exception.reason, "invalid_path")

    def test_unauthorized_source_area_id_fails_closed(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(source_areas.SourceAreaError) as missing:
                source_areas.revoke_source_area(
                    connection,
                    source_area_id=99999,
                    actor_user_id=99,
                    reason="cleanup unknown grant",
                )
            self.assertEqual(missing.exception.reason, "not_found")

    def test_source_area_mutations_never_modify_vault_rows(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            before_vault = connection.execute(
                "SELECT id, slug, name, source_root, enabled FROM vaults WHERE id=1"
            ).fetchone()
            before_members = connection.execute(
                "SELECT vault_id, user_id, role FROM vault_members ORDER BY vault_id, user_id"
            ).fetchall()
            grant = source_areas.assign_source_area(
                connection,
                user_id=1,
                volume_alias="photos",
                relative_path="",
                **_ASSIGN,
            )
            source_areas.revoke_source_area(
                connection,
                source_area_id=grant["id"],
                actor_user_id=99,
                reason="temporary grant for operator check",
            )
            connection.commit()
            after_vault = connection.execute(
                "SELECT id, slug, name, source_root, enabled FROM vaults WHERE id=1"
            ).fetchone()
            after_members = connection.execute(
                "SELECT vault_id, user_id, role FROM vault_members ORDER BY vault_id, user_id"
            ).fetchall()

        self.assertEqual(dict(before_vault), dict(after_vault))
        self.assertEqual(
            [dict(row) for row in before_members],
            [dict(row) for row in after_members],
        )


if __name__ == "__main__":
    unittest.main()
