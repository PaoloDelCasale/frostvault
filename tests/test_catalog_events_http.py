"""HTTP/SSE coverage for event-driven catalog invalidation (#227)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from httpx import ASGITransport
from watchfiles import Change

from app import main, storage
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.services.catalog_event_hub import (
    catalog_event_hub,
    coalesce_signals,
)
from app.services.catalog_event_stream import (
    MAX_CATCHUP_EVENTS,
    coalesced_catchup_from_connection,
    iter_catalog_event_sse,
    session_stream_state,
    stream_tick_snapshot,
)
from app.services.catalog_events import CatalogEventStore, record_catalog_revision
from app.sessions import create_session, csrf_token_for, revoke_session, set_session_vault
from app.storage import apply_filesystem_changes
from tests.test_database import run_alembic


_ORIGINAL_STORAGE_PATCH_TARGETS = {
    "vault_local_access": storage.source_layout.vault_local_access,
    "should_emit_local_copy_removals": (
        storage.source_layout.should_emit_local_copy_removals
    ),
    "relocation_suspended": storage.vault_relocation.local_work_suspended,
    "decommission_suspended": (
        storage.vault_decommission_service.local_work_suspended
    ),
}


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
        for target, return_value in (
            ("app.storage.source_layout.vault_local_access", access),
            ("app.storage.source_layout.should_emit_local_copy_removals", True),
            ("app.storage.vault_relocation.local_work_suspended", False),
            ("app.storage.vault_decommission_service.local_work_suspended", False),
        ):
            patcher = patch(target, return_value=return_value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(
            app=main.app, client=("127.0.0.1", 50000), follow_redirects=False
        )
        self.addCleanup(self.client.close)

        self.owner_token = self._authenticate(1, vault_id=1)
        self.viewer_token = self._authenticate(2, vault_id=1)
        self.outsider_token = self._authenticate(3, vault_id=None)
        self.owner_session_id = self._session_id_for_user(1)

    def _authenticate(self, user_id: int, *, vault_id: int | None) -> str:
        with SQLiteConnection(str(self.db_path)) as connection:
            raw_token = create_session(
                connection, user_id=user_id, auth_method="local"
            )
            csrf_token_for(connection, raw_token)
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
        return raw_token

    def _session_id_for_user(self, user_id: int) -> str:
        with SQLiteConnection(str(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT id FROM sessions
                WHERE user_id=%s AND revoked_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return str(row["id"])

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

    def test_live_stream_observes_durable_publish_without_local_hub(self) -> None:
        """Multi-process seam: durable journal advances without hub publish."""

        async def exercise() -> list[dict[str, str]]:
            frames: list[str] = []
            stop = {"value": False}

            async def is_disconnected() -> bool:
                return stop["value"]

            async def consume() -> None:
                async for chunk in iter_catalog_event_sse(
                    vault_id=1,
                    user_id=1,
                    session_id=self.owner_session_id,
                    resume_after=0,
                    subscribe=True,
                    is_disconnected=is_disconnected,
                    durable_poll_seconds=0.05,
                    use_hub=False,
                ):
                    frames.append(chunk)
                    body = "".join(frames)
                    events = self._parse_sse(body)
                    if any(
                        event.get("event") == "catalog"
                        and '"revision":1' in event.get("data", "").replace(" ", "")
                        for event in events
                    ):
                        stop["value"] = True
                        break
                    if len(body) > 8000:
                        stop["value"] = True
                        break

            async def publish_durable() -> None:
                await asyncio.sleep(0.08)
                # Intentionally skip catalog_event_hub.publish — other process.
                with SQLiteConnection(str(self.db_path)) as connection:
                    record_catalog_revision(
                        connection,
                        vault_id=1,
                        reason="other_process",
                    )

            await asyncio.gather(consume(), publish_durable())
            return self._parse_sse("".join(frames))

        events = asyncio.run(exercise())
        catalog = [event for event in events if event.get("event") == "catalog"]
        self.assertTrue(catalog, msg=events)
        payload = json.loads(catalog[-1]["data"])
        self.assertEqual(payload["vault_id"], 1)
        self.assertEqual(payload["revision"], 1)
        self.assertIn("files", payload["domains"])
        self.assertEqual(catalog_event_hub.subscriber_count(1), 0)

    def test_session_revocation_closes_live_stream(self) -> None:
        async def exercise() -> list[dict[str, str]]:
            frames: list[str] = []
            stop = {"value": False}

            async def is_disconnected() -> bool:
                return stop["value"]

            async def consume() -> None:
                async for chunk in iter_catalog_event_sse(
                    vault_id=1,
                    user_id=1,
                    session_id=self.owner_session_id,
                    resume_after=0,
                    subscribe=True,
                    is_disconnected=is_disconnected,
                    durable_poll_seconds=0.05,
                    use_hub=False,
                ):
                    frames.append(chunk)
                    body = "".join(frames)
                    if any(
                        event.get("event") == "error"
                        for event in self._parse_sse(body)
                    ):
                        stop["value"] = True
                        break
                    if len(body) > 8000:
                        stop["value"] = True
                        break

            async def revoke() -> None:
                await asyncio.sleep(0.08)
                with SQLiteConnection(str(self.db_path)) as connection:
                    self.assertTrue(revoke_session(connection, self.owner_session_id))
                self.assertIsNone(session_stream_state(self.owner_session_id))

            await asyncio.gather(consume(), revoke())
            return self._parse_sse("".join(frames))

        events = asyncio.run(exercise())
        errors = [event for event in events if event.get("event") == "error"]
        self.assertTrue(errors, msg=events)
        self.assertEqual(json.loads(errors[-1]["data"])["error"], "session_revoked")
        self.assertEqual(catalog_event_hub.subscriber_count(1), 0)

    def test_full_app_finite_streaming_response_headers_and_body(self) -> None:
        """Full FrostVault app StreamingResponse for finite catch-up frames."""

        async def exercise() -> str:
            transport = ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=5.0
            ) as client:
                response = await client.get(
                    "/api/catalog/events?subscribe=false",
                    cookies={
                        self.test_settings.session_cookie_name: self.owner_token
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            return response.text

        body = asyncio.run(exercise())
        events = self._parse_sse(body)
        self.assertTrue(any(event.get("event") == "hello" for event in events))

    def test_streaming_response_live_event_disconnect_and_cleanup(self) -> None:
        """StreamingResponse body iterator: live event, abort, hub cleanup.

        httpx/Starlette TestClient buffer infinite SSE bodies through the full
        ASGI stack in this environment, so the production generator is driven
        through ``StreamingResponse.body_iterator`` directly. Finite full-app
        StreamingResponse coverage lives in
        ``test_full_app_finite_streaming_response_headers_and_body``.
        """
        from starlette.responses import StreamingResponse

        from app.services.catalog_event_hub import publish_committed_event

        self.client.close()
        session_id = self.owner_session_id
        stop = {"value": False}

        async def disconnected() -> bool:
            return stop["value"]

        async def exercise() -> tuple[list[dict[str, str]], int, int]:
            response = StreamingResponse(
                iter_catalog_event_sse(
                    vault_id=1,
                    user_id=1,
                    session_id=session_id,
                    resume_after=0,
                    subscribe=True,
                    is_disconnected=disconnected,
                    durable_poll_seconds=0.05,
                    use_hub=True,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "Connection": "keep-alive",
                },
            )
            self.assertEqual(
                response.media_type.split(";")[0], "text/event-stream"
            )
            frames: list[str] = []
            published = False
            seen_subs = 0
            async for chunk in response.body_iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
                frames.append(text)
                seen_subs = max(seen_subs, catalog_event_hub.subscriber_count(1))
                body = "".join(frames)
                events = self._parse_sse(body)
                if not published and any(
                    event.get("event") == "hello" for event in events
                ):
                    with SQLiteConnection(str(self.db_path)) as connection:
                        event = record_catalog_revision(
                            connection,
                            vault_id=1,
                            reason="stream_live",
                        )
                    publish_committed_event(event)
                    published = True
                if any(event.get("event") == "catalog" for event in events):
                    # Client abort / disconnect.
                    stop["value"] = True
                    break
            # Generator finally should run after the iterator is closed.
            await response.body_iterator.aclose()
            deadline = asyncio.get_running_loop().time() + 2.0
            while (
                catalog_event_hub.subscriber_count(1) > 0
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.05)
            return (
                self._parse_sse("".join(frames)),
                catalog_event_hub.subscriber_count(1),
                seen_subs,
            )

        events, subscribers, seen = asyncio.run(
            asyncio.wait_for(exercise(), timeout=5.0)
        )
        self.assertTrue(any(event.get("event") == "hello" for event in events))
        catalog = [event for event in events if event.get("event") == "catalog"]
        self.assertTrue(catalog, msg=events)
        self.assertEqual(json.loads(catalog[-1]["data"])["revision"], 1)
        self.assertGreaterEqual(seen, 1)
        self.assertEqual(subscribers, 0)

    def test_tick_snapshot_uses_one_connection_and_bounded_statements(self) -> None:
        with SQLiteConnection(str(self.db_path)) as connection:
            record_catalog_revision(
                connection, vault_id=1, reason="a", invalidate=["files"]
            )
            record_catalog_revision(
                connection, vault_id=1, reason="b", invalidate=["stats"]
            )

        tick = stream_tick_snapshot(
            session_id=self.owner_session_id,
            user_id=1,
            vault_id=1,
            after_revision=0,
        )
        self.assertTrue(tick.ok)
        self.assertIsNotNone(tick.signal)
        self.assertLessEqual(tick.statement_count, 3)
        self.assertGreaterEqual(tick.statement_count, 2)
        self.assertEqual(tick.signal["revision"], 2)
        self.assertEqual(sorted(tick.signal["domains"]), ["files", "stats"])
        self.assertFalse(tick.signal["has_gap"])

    def test_bounded_backlog_thousands_of_events_emits_has_gap(self) -> None:
        total = 2500
        with SQLiteConnection(str(self.db_path)) as connection:
            for index in range(total):
                CatalogEventStore(connection).append_event(
                    vault_id=1,
                    domain="catalog",
                    payload={
                        "invalidate": ["files" if index % 2 == 0 else "stats"],
                        "reason": f"burst-{index}",
                    },
                )

        statements = [0]
        with SQLiteConnection(str(self.db_path)) as connection:
            original_execute = connection.execute

            def counting_execute(sql, params=()):
                statements[0] += 1
                return original_execute(sql, params)

            connection.execute = counting_execute  # type: ignore[method-assign]
            signal, events_read, truncated = coalesced_catchup_from_connection(
                connection,
                vault_id=1,
                after_revision=0,
                max_events=MAX_CATCHUP_EVENTS,
            )

        self.assertTrue(truncated)
        self.assertTrue(signal["has_gap"])
        self.assertEqual(signal["revision"], total)
        self.assertEqual(
            sorted(signal["domains"]),
            ["files", "rename_candidates", "stats"],
        )
        self.assertLessEqual(events_read, MAX_CATCHUP_EVENTS)
        # High-water + one bounded page only — never O(n) queries.
        self.assertEqual(statements[0], 2)

        tick = stream_tick_snapshot(
            session_id=self.owner_session_id,
            user_id=1,
            vault_id=1,
            after_revision=0,
            max_events=MAX_CATCHUP_EVENTS,
        )
        self.assertTrue(tick.ok)
        self.assertTrue(tick.backlog_truncated)
        self.assertLessEqual(tick.statement_count, 3)
        self.assertTrue(tick.signal["has_gap"])
        self.assertEqual(tick.signal["revision"], total)

    def test_hub_is_wakeup_only_journal_supplies_all_domains(self) -> None:
        """Hub may deliver only/out-of-order rev2; journal still coalesces rev1+rev2."""

        async def exercise() -> list[dict[str, str]]:
            frames: list[str] = []
            stop = {"value": False}

            async def is_disconnected() -> bool:
                return stop["value"]

            async def consume() -> None:
                async for chunk in iter_catalog_event_sse(
                    vault_id=1,
                    user_id=1,
                    session_id=self.owner_session_id,
                    resume_after=0,
                    subscribe=True,
                    is_disconnected=is_disconnected,
                    durable_poll_seconds=0.5,
                    use_hub=True,
                ):
                    frames.append(chunk)
                    body = "".join(frames)
                    catalog = [
                        event
                        for event in self._parse_sse(body)
                        if event.get("event") == "catalog"
                    ]
                    if catalog:
                        stop["value"] = True
                        break

            async def publish_and_spoof_hub() -> None:
                await asyncio.sleep(0.05)
                with SQLiteConnection(str(self.db_path)) as connection:
                    CatalogEventStore(connection).append_event(
                        vault_id=1,
                        domain="catalog",
                        payload={"invalidate": ["stats"], "reason": "rev1"},
                    )
                    CatalogEventStore(connection).append_event(
                        vault_id=1,
                        domain="catalog",
                        payload={"invalidate": ["files"], "reason": "rev2"},
                    )
                # Spoofed hub payload: only rev2/files, missing stats and rev1.
                catalog_event_hub.publish(
                    {
                        "vault_id": 1,
                        "revision": 2,
                        "payload": {"invalidate": ["files"]},
                        "has_gap": False,
                    }
                )

            await asyncio.gather(consume(), publish_and_spoof_hub())
            return self._parse_sse("".join(frames))

        events = asyncio.run(exercise())
        catalog = [event for event in events if event.get("event") == "catalog"]
        self.assertTrue(catalog, msg=events)
        payload = json.loads(catalog[-1]["data"])
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(sorted(payload["domains"]), ["files", "stats"])
        self.assertFalse(payload["has_gap"])
        # Dedupe: a single catalog frame for the catch-up, not one per hub wake.
        self.assertEqual(len(catalog), 1)

    async def _consume_until_error(self, mutate_fn) -> list[dict[str, str]]:
        frames: list[str] = []
        stop = {"value": False}

        async def is_disconnected() -> bool:
            return stop["value"]

        async def consume() -> None:
            async for chunk in iter_catalog_event_sse(
                vault_id=1,
                user_id=1,
                session_id=self.owner_session_id,
                resume_after=0,
                subscribe=True,
                is_disconnected=is_disconnected,
                durable_poll_seconds=0.05,
                use_hub=False,
            ):
                frames.append(chunk)
                if any(
                    event.get("event") == "error"
                    for event in self._parse_sse("".join(frames))
                ):
                    stop["value"] = True
                    break

        async def run_mutate() -> None:
            await asyncio.sleep(0.08)
            mutate_fn()

        await asyncio.gather(consume(), run_mutate())
        return self._parse_sse("".join(frames))

    def test_idle_expiry_closes_live_stream(self) -> None:
        def expire() -> None:
            with SQLiteConnection(str(self.db_path)) as connection:
                connection.execute(
                    "UPDATE sessions SET idle_expires_at=%s WHERE id=%s",
                    ("2000-01-01T00:00:00+00:00", self.owner_session_id),
                )

        events = asyncio.run(self._consume_until_error(expire))
        errors = [event for event in events if event.get("event") == "error"]
        self.assertTrue(errors, msg=events)
        self.assertEqual(json.loads(errors[-1]["data"])["error"], "session_expired")

    def test_absolute_expiry_closes_live_stream(self) -> None:
        def expire() -> None:
            with SQLiteConnection(str(self.db_path)) as connection:
                connection.execute(
                    "UPDATE sessions SET absolute_expires_at=%s WHERE id=%s",
                    ("2000-01-01T00:00:00+00:00", self.owner_session_id),
                )

        events = asyncio.run(self._consume_until_error(expire))
        errors = [event for event in events if event.get("event") == "error"]
        self.assertTrue(errors, msg=events)
        self.assertEqual(json.loads(errors[-1]["data"])["error"], "session_expired")

    def test_session_version_mismatch_closes_live_stream(self) -> None:
        def bump_user_version() -> None:
            with SQLiteConnection(str(self.db_path)) as connection:
                connection.execute(
                    "UPDATE users SET session_version=session_version+1 WHERE id=1"
                )

        events = asyncio.run(self._consume_until_error(bump_user_version))
        errors = [event for event in events if event.get("event") == "error"]
        self.assertTrue(errors, msg=events)
        self.assertEqual(
            json.loads(errors[-1]["data"])["error"], "session_version_mismatch"
        )

    def test_user_disabled_closes_live_stream(self) -> None:
        def disable() -> None:
            with SQLiteConnection(str(self.db_path)) as connection:
                connection.execute("UPDATE users SET active=0 WHERE id=1")

        events = asyncio.run(self._consume_until_error(disable))
        errors = [event for event in events if event.get("event") == "error"]
        self.assertTrue(errors, msg=events)
        self.assertEqual(json.loads(errors[-1]["data"])["error"], "user_disabled")

    def test_asgi_http_disconnect_cleans_hub_subscriber(self) -> None:
        """Drive real ASGI receive ``http.disconnect`` through Request.is_disconnected.

        Full-stack httpx/TestClient buffering prevents infinite SSE end-to-end
        here; this exercises the production disconnect probe path the endpoint
        wires via ``request.is_disconnected``.
        """
        from starlette.requests import Request
        from starlette.responses import StreamingResponse

        self.client.close()
        receive_queue: asyncio.Queue = asyncio.Queue()
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return await receive_queue.get()

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/stream",
            "raw_path": b"/stream",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }

        async def exercise() -> int:
            request = Request(scope, receive)
            response = StreamingResponse(
                iter_catalog_event_sse(
                    vault_id=1,
                    user_id=1,
                    session_id=self.owner_session_id,
                    resume_after=0,
                    subscribe=True,
                    is_disconnected=request.is_disconnected,
                    durable_poll_seconds=0.05,
                    use_hub=True,
                ),
                media_type="text/event-stream",
            )
            task = asyncio.create_task(response(scope, receive, send))
            # Wait until the app has started streaming body chunks.
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if any(
                    msg.get("type") == "http.response.body" for msg in sent
                ) and catalog_event_hub.subscriber_count(1) > 0:
                    break
                await asyncio.sleep(0.02)
            self.assertGreater(catalog_event_hub.subscriber_count(1), 0)
            # Queue disconnect so the next is_disconnected probe observes it
            # (Starlette uses a cancelled wait — message must already be ready).
            await receive_queue.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, timeout=3.0)
            deadline = asyncio.get_running_loop().time() + 2.0
            while (
                catalog_event_hub.subscriber_count(1) > 0
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.05)
            return catalog_event_hub.subscriber_count(1)

        remaining = asyncio.run(exercise())
        self.assertEqual(remaining, 0)
        self.assertTrue(
            any(msg.get("type") == "http.response.start" for msg in sent)
        )


class CatalogEventsPatchCleanupTests(unittest.TestCase):
    def test_http_fixture_restores_storage_patches(self) -> None:
        self.assertIs(
            storage.source_layout.vault_local_access,
            _ORIGINAL_STORAGE_PATCH_TARGETS["vault_local_access"],
        )
        self.assertIs(
            storage.source_layout.should_emit_local_copy_removals,
            _ORIGINAL_STORAGE_PATCH_TARGETS["should_emit_local_copy_removals"],
        )
        self.assertIs(
            storage.vault_relocation.local_work_suspended,
            _ORIGINAL_STORAGE_PATCH_TARGETS["relocation_suspended"],
        )
        self.assertIs(
            storage.vault_decommission_service.local_work_suspended,
            _ORIGINAL_STORAGE_PATCH_TARGETS["decommission_suspended"],
        )


if __name__ == "__main__":
    unittest.main()
