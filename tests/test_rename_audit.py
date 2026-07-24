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

    def _authenticate(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection, user_id=self.owner_id, auth_method="oidc"
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

    def _select_vault(self) -> None:
        response = self.client.post(
            "/api/vaults/select",
            json={"vault_id": self.vault_id},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _seed_digest_rename(
        self,
        *,
        old_path: str,
        new_path: str,
        digest: str = DIGEST_A,
    ) -> str:
        with SQLiteConnection(str(self.database_path)) as connection:
            catalog = ArchiveCatalog(connection)
            old_id = catalog.observe_local_copy(
                vault_id=self.vault_id,
                path=old_path,
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=self.vault_id,
                path=old_path,
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
            catalog.mark_local_copy_missing(
                old_id, observed_at="2026-07-21T11:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=self.vault_id,
                path=new_path,
                file_type="regular",
                size=9,
                mtime_ns=100,
                observed_at="2026-07-21T11:00:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=self.vault_id,
                path=new_path,
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
            return old_id

    def test_bug_009_rename_audit_persists_to_audit_events(self) -> None:
        """[BUG-009][Req: REQ-021] Path History mutations must durable-audit.

        Desired: confirm rename / folder rename pass ``connection`` into
        ``audit_log`` so ``audit_events`` (and ``GET /api/audit-events``)
        receive the row. Current: connection omitted → log-only.
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

        # Folder rename Path History mutation (second site in the audited defect).
        folder_old = self._seed_digest_rename(
            old_path="docs/a.txt",
            new_path="archive/a.txt",
            digest=hashlib.sha256(b"folder-a").hexdigest(),
        )
        folder_confirm = self.client.post(
            "/api/confirm-folder-rename",
            json={"old_prefix": "docs", "new_prefix": "archive"},
            headers=self._headers(),
        )
        self.assertIn(folder_confirm.status_code, {200, 202}, folder_confirm.text)
        self.assertIn(folder_old, folder_confirm.json().get("renamed_ids", [folder_old]))

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
