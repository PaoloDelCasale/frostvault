"""HTTP/SSE coverage for event-driven catalog invalidation (#227)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from watchfiles import Change

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.services.catalog_event_hub import catalog_event_hub, coalesce_signals
from app.services.catalog_events import CatalogEventStore
from app.sessions import create_session, csrf_token_for, set_session_vault
from app.storage import apply_filesystem_changes
from tests.test_database import run_alembic


class CatalogEventHubTests(unittest.TestCase):
    def test_coalesce_keeps_newest_revision_and_union_of_domains(self) -> None:
        merged = coalesce_signals(
            {
                "vault_id": 1,
                "revision": 3,
                "domains": ["files"],
                "has_gap": False,
            },
            {
                "vault_id": 1,
                "revision": 7,
                "domains": ["stats", "files"],
                "has_gap": True,
            },
        )
        self.assertEqual(
            merged,
            {
                "vault_id": 1,
                "revision": 7,
                "domains": ["files", "stats"],
                "has_gap": True,
            },
        )

    def test_out_of_order_and_duplicate_revisions_coalesce(self) -> None:
        first = {
            "vault_id": 1,
            "revision": 4,
            "domains": ["files"],
            "has_gap": False,
        }
        duplicate = dict(first)
        later = {
            "vault_id": 1,
            "revision": 2,
            "domains": ["stats"],
            "has_gap": False,
        }
        merged = coalesce_signals(first, duplicate, later)
        self.assertEqual(merged["revision"], 4)
        self.assertEqual(merged["domains"], ["files", "stats"])


class CatalogEventsHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "catalog-events.db"
        self.source = self.root / "source"
        self.source.mkdir()
        migrated = run_alembic(self.db_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'owner', 'Owner', %s, 0)",
                (hash_password("correct-horse-battery-staple"),),
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (2, 'viewer', 'Viewer', %s, 0)",
                (hash_password("correct-horse-battery-staple"),),
            )
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (3, 'outsider', 'Outsider', %s, 0)",
                (hash_password("correct-horse-battery-staple"),),
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES
                (1, 'alpha', 'Alpha', %s, 'bucket', 'alpha', 'remote'),
                (2, 'beta', 'Beta', %s, 'bucket', 'beta', 'remote')
                """,
                (str(self.source), str(self.source)),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES "
                "(1, 1, 'owner'), (1, 2, 'viewer'), (2, 1, 'owner')"
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.db_path),
            cookie_secure=False,
            filesystem_watch_enabled=False,
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

        access = Mock(
            local_operations_allowed=True,
            volume_health="ok",
            volume_alias=None,
        )
        self.addCleanup(
            patch(
                "app.storage.source_layout.vault_local_access",
                return_value=access,
            ).start()
        )
        self.addCleanup(
            patch(
                "app.storage.source_layout.should_emit_local_copy_removals",
                return_value=True,
            ).start()
        )
        self.addCleanup(
            patch(
                "app.storage.vault_relocation.local_work_suspended",
                return_value=False,
            ).start()
        )
        self.addCleanup(
            patch(
                "app.storage.vault_decommission_service.local_work_suspended",
                return_value=False,
            ).start()
        )

        self.client = TestClient(
            app=main.app, client=("127.0.0.1", 50000), follow_redirects=False
        )
        self.addCleanup(self.client.close)

        self.owner_token = self._authenticate(1, vault_id=1)
        self.viewer_token = self._authenticate(2, vault_id=1)
        self.outsider_token = self._authenticate(3, vault_id=None)

    def _authenticate(self, user_id: int, *, vault_id: int | None) -> str:
        with SQLiteConnection(str(self.db_path)) as connection:
            raw_token = create_session(
                connection, user_id=user_id, auth_method="local"
            )
            csrf_token = csrf_token_for(connection, raw_token)
            session = connection.execute(
                "SELECT id, offline_cache_generation, offline_cache_nonce "
                "FROM sessions WHERE token_hash IS NOT NULL ORDER BY created_at DESC"
            ).fetchone()
            # Prefer the newest session for this user.
            session = connection.execute(
                """
                SELECT id, offline_cache_generation, offline_cache_nonce
                FROM sessions
                WHERE user_id=%s AND revoked_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if vault_id is not None:
                set_session_vault(
                    connection,
                    session["id"],
                    vault_id,
                    expected_generation=session["offline_cache_generation"],
                    expected_nonce=session["offline_cache_nonce"],
                )
        # Tokens are returned for stream helpers that build isolated clients.
        return raw_token

    def _client_for(self, token: str) -> TestClient:
        client = TestClient(
            app=main.app, client=("127.0.0.1", 50000), follow_redirects=False
        )
        client.cookies.set(self.test_settings.session_cookie_name, token)
        return client

    def _parse_sse(self, body: str) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for raw_line in body.splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                if current:
                    events.append(current)
                    current = {}
                continue
            if line.startswith(":"):
                continue
            if ":" not in line:
                continue
            field, value = line.split(":", 1)
            current[field] = value.lstrip()
        if current:
            events.append(current)
        return events

    def test_filesystem_batch_publishes_one_revision_not_per_path(self) -> None:
        vault = {
            "id": 1,
            "source_root": str(self.source),
            "relocation_state": "ready",
            "decommission_state": "active",
        }
        paths = []
        for index in range(5):
            path = self.source / f"cam-{index}.jpg"
            path.write_bytes(b"frame")
            paths.append(path)

        changed = apply_filesystem_changes(
            vault,
            {(Change.added, str(path)) for path in paths},
        )
        self.assertEqual(changed, 5)

        with SQLiteConnection(str(self.db_path)) as connection:
            store = CatalogEventStore(connection)
            self.assertEqual(store.current_revision(1), 1)
            page = store.read_events(vault_id=1, after_revision=0)
            self.assertEqual(len(page["events"]), 1)
            event = page["events"][0]
            self.assertEqual(event["domain"], "catalog")
            self.assertEqual(
                event["payload"]["invalidate"],
                ["files", "stats", "rename_candidates"],
            )
            self.assertEqual(event["payload"]["reason"], "filesystem_watch")
            self.assertEqual(event["payload"]["changed_paths"], 5)

    def test_mount_loss_does_not_publish_mass_local_copy_removals(self) -> None:
        vault = {
            "id": 1,
            "source_root": str(self.source),
            "relocation_state": "ready",
            "decommission_state": "active",
        }
        existing = self.source / "kept.txt"
        existing.write_text("present", encoding="utf-8")
        apply_filesystem_changes(vault, {(Change.added, str(existing))})

        blocked = Mock(
            local_operations_allowed=False,
            volume_health="unmounted",
            volume_alias="managed",
        )
        with patch("app.storage.source_layout.vault_local_access", return_value=blocked):
            with self.assertRaisesRegex(RuntimeError, "Source Volume health"):
                apply_filesystem_changes(
                    vault,
                    {(Change.deleted, str(self.source / "kept.txt"))},
                )

        with SQLiteConnection(str(self.db_path)) as connection:
            # Only the successful add published a revision.
            self.assertEqual(CatalogEventStore(connection).current_revision(1), 1)
            presence = connection.execute(
                """
                SELECT lc.presence
                FROM local_copies lc
                JOIN vault_files vf ON vf.id=lc.vault_file_id
                JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                WHERE vf.vault_id=1 AND fp.path='kept.txt'
                """
            ).fetchone()
        self.assertEqual(presence["presence"], "present")

    def test_sse_requires_authentication(self) -> None:
        response = self.client.get("/api/catalog/events")
        self.assertIn(response.status_code, {401, 403})

    def test_sse_rejects_user_without_vault_membership(self) -> None:
        client = self._client_for(self.outsider_token)
        response = client.get("/api/catalog/events")
        self.assertEqual(response.status_code, 403)
        client.close()

    def test_sse_hello_and_catchup_are_vault_scoped(self) -> None:
        with SQLiteConnection(str(self.db_path)) as connection:
            CatalogEventStore(connection).append_event(
                vault_id=1,
                domain="catalog",
                payload={"invalidate": ["files"], "reason": "seed"},
            )
            CatalogEventStore(connection).append_event(
                vault_id=2,
                domain="catalog",
                payload={"invalidate": ["files"], "reason": "other-vault"},
            )

        client = self._client_for(self.owner_token)
        try:
            response = client.get(
                "/api/catalog/events?after_revision=0&subscribe=false"
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            body = response.text
            snapshot = client.get("/api/catalog/revision?after_revision=0")
        finally:
            client.close()

        events = self._parse_sse(body)
        hello = next(event for event in events if event.get("event") == "hello")
        catalog = next(event for event in events if event.get("event") == "catalog")
        hello_data = json.loads(hello["data"])
        catalog_data = json.loads(catalog["data"])
        self.assertEqual(hello_data["vault_id"], 1)
        self.assertEqual(catalog_data["vault_id"], 1)
        self.assertEqual(catalog_data["revision"], 1)
        self.assertNotIn("path", catalog_data)
        self.assertEqual(catalog_data["domains"], ["files"])
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["vault_id"], 1)
        self.assertEqual(snapshot.json()["revision"], 1)
        self.assertTrue(snapshot.json()["changed"])

    def test_sse_viewer_can_subscribe_without_operate_role(self) -> None:
        client = self._client_for(self.viewer_token)
        try:
            response = client.get("/api/catalog/events?subscribe=false")
            self.assertEqual(response.status_code, 200)
            body = response.text
        finally:
            client.close()
        events = self._parse_sse(body)
        self.assertTrue(any(event.get("event") == "hello" for event in events))

    def test_live_publish_reaches_subscriber_and_cleans_up(self) -> None:
        async def exercise() -> list[dict[str, object]]:
            loop = asyncio.get_running_loop()
            subscriber = catalog_event_hub.subscribe(1)
            self.assertEqual(catalog_event_hub.subscriber_count(1), 1)

            def mutate() -> None:
                vault = {
                    "id": 1,
                    "source_root": str(self.source),
                    "relocation_state": "ready",
                    "decommission_state": "active",
                }
                path = self.source / "live.txt"
                path.write_text("hello", encoding="utf-8")
                apply_filesystem_changes(vault, {(Change.added, str(path))})

            await asyncio.to_thread(mutate)
            signal = await asyncio.wait_for(subscriber.queue.get(), timeout=2.0)
            catalog_event_hub.unsubscribe(1, subscriber)
            self.assertEqual(catalog_event_hub.subscriber_count(1), 0)
            return [signal]

        signals = asyncio.run(exercise())
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["vault_id"], 1)
        self.assertEqual(signals[0]["revision"], 1)
        self.assertIn("files", signals[0]["domains"])

        client = self._client_for(self.owner_token)
        try:
            snapshot = client.get("/api/catalog/revision?after_revision=0")
        finally:
            client.close()
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["revision"], 1)

    def test_reconnect_after_revision_does_not_replay_older_events(self) -> None:
        with SQLiteConnection(str(self.db_path)) as connection:
            store = CatalogEventStore(connection)
            store.append_event(
                vault_id=1,
                domain="catalog",
                payload={"invalidate": ["files"], "reason": "one"},
            )
            store.append_event(
                vault_id=1,
                domain="catalog",
                payload={"invalidate": ["stats"], "reason": "two"},
            )

        client = self._client_for(self.owner_token)
        try:
            response = client.get(
                "/api/catalog/events?after_revision=2&subscribe=false"
            )
            body = response.text
            snapshot = client.get("/api/catalog/revision?after_revision=2")
        finally:
            client.close()
        events = self._parse_sse(body)
        catalog_events = [event for event in events if event.get("event") == "catalog"]
        self.assertEqual(catalog_events, [])
        hello = next(event for event in events if event.get("event") == "hello")
        self.assertEqual(json.loads(hello["data"])["revision"], 2)
        self.assertFalse(snapshot.json()["changed"])


if __name__ == "__main__":
    unittest.main()
