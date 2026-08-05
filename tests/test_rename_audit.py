"""Durable audit for Path History rename confirmations (BUG-009 / issue #19).

Selected TDD seam:
- ``POST /api/confirm-rename`` and ``POST /api/confirm-folder-rename`` —
  public Path History mutation surfaces already used by the UI, observed
  through ``GET /api/audit-events`` (operators' durable audit view).
- ``apply_auto_renames`` — scan-time auto-confirm path, observed through
  ``list_vault_audit_events`` on the same vault.

Reason: reproduces the externally observable defect (rename mutations leave
no ``audit_events`` row), uses the real ``audit_log`` persistence contract
without mocking away the missing ``connection``, and remains valid if
``audit_log`` internals are refactored so long as durable events appear.
Avoids the audit's source-regex assertions on ``main.py``.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.catalog import ArchiveCatalog
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.services import audit_events
from app.sessions import create_session, csrf_token_for
from app.storage import apply_auto_renames
from tests.test_database import run_alembic

DIGEST_A = hashlib.sha256(b"content-a").hexdigest()


class RenameAuditPersistenceTests(unittest.TestCase):
    """BUG-009: rename Path History mutations must durable-audit (REQ-021)."""

    PASSWORD = "correct-horse-battery"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        self.source_root = Path(self._tmp.name) / "source"
        self.source_root.mkdir()
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.owner_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES (%s, %s, %s, FALSE) RETURNING id
                """,
                ("owner", "Owner", hash_password(self.PASSWORD)),
            ).fetchone()["id"]
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (
                    'docs', 'Docs', %s, 'bucket', 'docs', 'remote'
                ) RETURNING id
                """,
                (str(self.source_root),),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (self.vault_id, self.owner_id),
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.storage.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(
            app=main.app, client=("127.0.0.1", 50000), follow_redirects=False
        )

    def _authenticate(self, *, user_id: int | None = None) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection,
                user_id=self.owner_id if user_id is None else user_id,
                auth_method="oidc",
            )
            csrf_token = csrf_token_for(connection, raw_token)
        self.client.cookies.set(self.test_settings.session_cookie_name, raw_token)
        self.client.cookies.set(self.test_settings.csrf_cookie_name, csrf_token)

    def _headers(self) -> dict[str, str]:
        return {
            "X-CSRF-Token": self.client.cookies.get(
                self.test_settings.csrf_cookie_name
            )
            or ""
        }

    def _select_vault(self, vault_id: int | None = None) -> None:
        response = self.client.post(
            "/api/vaults/select",
            json={"vault_id": self.vault_id if vault_id is None else vault_id},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _seed_digest_rename(
        self,
        *,
        old_path: str,
        new_path: str,
        digest: str = DIGEST_A,
        vault_id: int | None = None,
    ) -> str:
        target_vault_id = self.vault_id if vault_id is None else vault_id
        with SQLiteConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            old_id = catalog.observe_local_copy(
                vault_id=target_vault_id,
                path=old_path,
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=target_vault_id,
                path=old_path,
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
            catalog.mark_local_copy_missing(
                old_id, observed_at="2026-07-21T11:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=target_vault_id,
                path=new_path,
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T11:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=target_vault_id,
                path=new_path,
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
            return old_id

    def _create_operator_with_cross_vault_access(self) -> tuple[int, int]:
        second_root = Path(self._tmp.name) / "second-source"
        second_root.mkdir()
        with SQLiteConnection(str(self.database_path)) as connection:
            operator_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES (%s, %s, %s, FALSE) RETURNING id
                """,
                ("operator", "Operator", hash_password(self.PASSWORD)),
            ).fetchone()["id"]
            second_vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (
                    'media', 'Media', %s, 'bucket', 'media', 'remote'
                ) RETURNING id
                """,
                (str(second_root),),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'operator')",
                (self.vault_id, operator_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'viewer')",
                (second_vault_id, operator_id),
            )
        return operator_id, second_vault_id

    def _rename_state(self) -> dict[str, list[dict[str, object]]]:
        with SQLiteConnection(str(self.database_path)) as connection:
            return {
                "vault_files": connection.execute(
                    "SELECT * FROM vault_files ORDER BY id"
                ).fetchall(),
                "file_paths": connection.execute(
                    "SELECT * FROM file_paths ORDER BY id"
                ).fetchall(),
                "local_copies": connection.execute(
                    "SELECT * FROM local_copies ORDER BY vault_file_id"
                ).fetchall(),
                "jobs": connection.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
                "audit_events": connection.execute(
                    "SELECT * FROM audit_events ORDER BY id"
                ).fetchall(),
            }

    def test_bug_009_rename_audit_persists_to_audit_events(self) -> None:
        """[BUG-009][Req: REQ-021] File rename must durable-audit.

        Desired: confirm rename passes ``connection`` into ``audit_log`` so
        ``audit_events`` (and ``GET /api/audit-events``) receive the row.
        Current: connection omitted → log-only.
        """
        old_id = self._seed_digest_rename(
            old_path="reports/old-name.txt",
            new_path="reports/new-name.txt",
        )
        self._authenticate()
        self._select_vault()

        confirm = self.client.post(
            "/api/confirm-rename",
            json={"vault_file_id": old_id, "new_path": "reports/new-name.txt"},
            headers=self._headers(),
        )
        self.assertIn(confirm.status_code, {200, 202}, confirm.text)

        events = self.client.get("/api/audit-events")
        self.assertEqual(events.status_code, 200, events.text)
        names = {item["event"] for item in events.json()["events"]}
        self.assertIn(
            "vault_file_renamed",
            names,
            "confirm_rename must persist a durable vault_file_renamed audit event",
        )

    def test_issue_188_rejects_unknown_stale_and_foreign_ids_without_side_effects(
        self,
    ) -> None:
        """An operator in Vault A cannot confirm a Vault B candidate as a viewer."""
        operator_id, second_vault_id = self._create_operator_with_cross_vault_access()
        foreign_id = self._seed_digest_rename(
            vault_id=second_vault_id,
            old_path="foreign/old.txt",
            new_path="foreign/new.txt",
        )
        retired_id = self._seed_digest_rename(
            old_path="retired/old.txt",
            new_path="retired/new.txt",
        )
        stale_candidate_id = self._seed_digest_rename(
            old_path="stale/old.txt",
            new_path="stale/new.txt",
        )
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE vault_files
                SET status='retired', retired_at=%s
                WHERE id=%s
                """,
                ("2026-07-21T12:00:00+00:00", retired_id),
            )
            ArchiveCatalog(connection).confirm_file_rename(
                vault_file_id=stale_candidate_id,
                new_path="stale/new.txt",
                changed_at="2026-07-21T12:00:00+00:00",
                vault_id=self.vault_id,
            )

        self._authenticate(user_id=operator_id)
        self._select_vault(second_vault_id)
        candidates = self.client.get("/api/rename-candidates")
        self.assertEqual(candidates.status_code, 200, candidates.text)
        foreign_candidates = [
            candidate
            for candidate in candidates.json()["items"]
            if candidate["missing_vault_file_id"] == foreign_id
        ]
        self.assertEqual(len(foreign_candidates), 1)
        foreign_id_from_viewer_lookup = foreign_candidates[0]["missing_vault_file_id"]

        self._select_vault(self.vault_id)
        responses = []
        for vault_file_id, new_path in (
            (str(uuid.uuid4()), "unknown/new.txt"),
            (retired_id, "retired/new.txt"),
            (stale_candidate_id, "stale/new.txt"),
            (foreign_id_from_viewer_lookup, "foreign/new.txt"),
        ):
            with self.subTest(vault_file_id=vault_file_id):
                before = self._rename_state()
                response = self.client.post(
                    "/api/confirm-rename",
                    json={"vault_file_id": vault_file_id, "new_path": new_path},
                    headers=self._headers(),
                )
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(self._rename_state(), before)
                responses.append(response.json())

        self.assertEqual(
            responses,
            [{"detail": "Vault File not found"}] * 4,
            (
                "Unknown, retired, stale-candidate, and foreign IDs must have "
                "the same non-oracle response"
            ),
        )

    def test_issue_188_rejects_candidate_stale_after_source_state_changes(
        self,
    ) -> None:
        """A candidate must still pair a missing source with its new Local Copy."""
        old_id = self._seed_digest_rename(
            old_path="stale-state/old.txt",
            new_path="stale-state/new.txt",
        )
        self._authenticate()
        self._select_vault()

        candidates = self.client.get("/api/rename-candidates")
        self.assertEqual(candidates.status_code, 200, candidates.text)
        candidate = next(
            item
            for item in candidates.json()["items"]
            if item["missing_vault_file_id"] == old_id
        )

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE local_copies
                SET presence='present'
                WHERE vault_file_id=%s
                """,
                (old_id,),
            )

        refreshed_candidates = self.client.get("/api/rename-candidates")
        self.assertEqual(refreshed_candidates.status_code, 200, refreshed_candidates.text)
        self.assertNotIn(candidate, refreshed_candidates.json()["items"])

        before = self._rename_state()
        response = self.client.post(
            "/api/confirm-rename",
            json={
                "vault_file_id": candidate["missing_vault_file_id"],
                "new_path": candidate["new_path"],
            },
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), {"detail": "Vault File not found"})
        self.assertEqual(self._rename_state(), before)

    def test_bug_009_folder_rename_audit_persists_to_audit_events(self) -> None:
        """[BUG-009][Req: REQ-021] Folder rename must durable-audit."""
        folder_old = self._seed_digest_rename(
            old_path="docs/a.txt",
            new_path="archive/a.txt",
            digest=hashlib.sha256(b"folder-a").hexdigest(),
        )
        self._authenticate()
        self._select_vault()

        folder_confirm = self.client.post(
            "/api/confirm-folder-rename",
            json={"old_prefix": "docs", "new_prefix": "archive"},
            headers=self._headers(),
        )
        self.assertIn(folder_confirm.status_code, {200, 202}, folder_confirm.text)
        self.assertIn(folder_old, folder_confirm.json()["renamed_ids"])

        folder_events = self.client.get("/api/audit-events")
        self.assertEqual(folder_events.status_code, 200, folder_events.text)
        folder_names = {item["event"] for item in folder_events.json()["events"]}
        self.assertIn(
            "vault_folder_renamed",
            folder_names,
            "confirm_folder_rename must persist a durable vault_folder_renamed audit event",
        )

    def test_bug_009_auto_rename_audit_persists_to_audit_events(self) -> None:
        """[BUG-009] Auto-confirm renames must also durable-audit."""
        old_path = "auto/old.txt"
        new_path = "auto/new.txt"
        payload = b"content-a"
        new_file = self.source_root / new_path
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_bytes(payload)

        with SQLiteConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            old_id = catalog.observe_local_copy(
                vault_id=self.vault_id,
                path=old_path,
                file_type="regular",
                size=len(payload),
                mtime_ns=100,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=self.vault_id,
                path=old_path,
                plaintext_sha256=DIGEST_A,
                matched_archive_version_id=None,
            )
            catalog.mark_local_copy_missing(
                old_id, observed_at="2026-07-21T11:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=self.vault_id,
                path=new_path,
                file_type="regular",
                size=len(payload),
                mtime_ns=new_file.stat().st_mtime_ns,
                observed_at="2026-07-21T11:00:00+00:00",
            )
            # Leave new-path fingerprint unset so apply_auto_renames hashes it.

        vault = {
            "id": self.vault_id,
            "source_root": str(self.source_root),
        }
        summary = apply_auto_renames(vault, requested_by=self.owner_id)
        self.assertGreaterEqual(summary["confirmed"], 1)

        with SQLiteConnection(str(self.database_path)) as connection:
            events = audit_events.list_vault_audit_events(connection, self.vault_id)
        renamed = [row for row in events if row["event"] == "vault_file_renamed"]
        self.assertTrue(
            renamed,
            "apply_auto_renames must persist a durable vault_file_renamed audit event",
        )
        self.assertEqual(renamed[0]["detail"].get("decision"), "auto")


if __name__ == "__main__":
    unittest.main()
