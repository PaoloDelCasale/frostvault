"""A full scan stops cooperatively at process shutdown (#696, ADR-0045).

Seams under test:
1. Shutdown during the traversal commits the batch in progress and stops: the
   rows already observed stay, with no journal compensation, no missing
   transition and no source error.
2. Shutdown during the verification keeps the digests already published and
   skips the finalization; the next scan hashes only the rows without one.
3. The worker drain awaits a stopping maintenance scan and a watcher's paired
   scan and succeeds within its budget.
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import ExitStack
from unittest.mock import patch

from app.services.shutdown import shutdown_state
from app.services.vaults import VAULT_ROW_SQL
from tests.fixtures import override_settings
from tests.test_scan_fingerprints import _quiet_scan_patches
from tests.test_vault_adoption import _AdoptionFixture


class ScanShutdownTests(_AdoptionFixture):
    def setUp(self) -> None:
        super().setUp()
        for index in range(12):
            (self.albums / f"img{index:02d}.heic").write_bytes(b"x" * (100 + index))
        shutdown_state.reset()
        self.addCleanup(shutdown_state.reset)

    def _vault(self) -> dict:
        from app.storage import runtime_status
        created = self._adopt()
        self.addCleanup(runtime_status.pop, int(created["id"]), None)
        with self.database.connect() as connection:
            return dict(connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s", (created["id"],),
            ).fetchone())

    def _concurrency(self, value: int):
        return override_settings(scan_hash_concurrency=value)

    def _digests(self, vault_id: int) -> dict[str, str | None]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT path, local_sha256 FROM files WHERE vault_id=%s", (vault_id,),
            ).fetchall()
        return {row["path"]: row["local_sha256"] for row in rows}

    def _source_errors(self, vault_id: int) -> list:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT component, last_error FROM vault_component_health"
                " WHERE vault_id=%s AND last_error IS NOT NULL",
                (vault_id,),
            ).fetchall()

    def _scan(self, vault: dict, *extra):
        from app.storage import scan_vault
        with ExitStack() as stack:
            for item in (*_quiet_scan_patches(), *extra):
                stack.enter_context(item)
            return scan_vault(dict(vault))

    def test_shutdown_during_traversal_commits_the_batch_and_compensates_nothing(self) -> None:
        import app.storage as storage
        vault = self._vault()
        vault_id = int(vault["id"])
        real_inspect = storage.inspect_entry
        seen = []

        def inspect(entry, **kwargs):
            seen.append(entry)
            if len(seen) == 7:
                shutdown_state.request()
            return real_inspect(entry, **kwargs)

        with (
            patch("app.storage._rollback_local_scan_journal") as rollback,
            patch("app.storage.ArchiveCatalog.mark_unseen_local_copies_missing") as missing,
        ):
            result = self._scan(
                vault,
                patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 5),
                patch("app.storage.inspect_entry", side_effect=inspect),
            )
        self.assertEqual(result, {"stopped": 1})
        self.assertEqual(len(seen), 7)
        # The full batch of 5 and the batch in progress (2) are committed.
        self.assertEqual(len(self._digests(vault_id)), 7)
        rollback.assert_not_called()
        missing.assert_not_called()
        self.assertEqual(self._source_errors(vault_id), [])
        self.assertNotIn((vault_id, storage.runtime_status[vault_id].get("scan_id")),
                         storage._active_scan_journals)
        self.assertFalse(storage.runtime_status[vault_id]["scanning"])
        self.assertIsNone(storage.runtime_status[vault_id]["last_error"])

        # The next start rescans as today.
        shutdown_state.reset()
        result = self._scan(vault)
        self.assertEqual(result["local"], 13)
        self.assertTrue(all(self._digests(vault_id).values()))

    def test_shutdown_during_verification_keeps_published_digests(self) -> None:
        import app.storage as storage
        vault = self._vault()
        vault_id = int(vault["id"])
        real_hash = storage.hash_stable_regular_file
        calls = []

        def hashing(path, **kwargs):
            calls.append(path)
            if len(calls) == 5:
                # The read already running aborts at its next chunk.
                shutdown_state.request()
            return real_hash(path, **kwargs)

        with (
            patch("app.storage._rollback_local_scan_journal") as rollback,
            patch("app.storage.ArchiveCatalog.mark_unseen_local_copies_missing") as missing,
        ):
            result = self._scan(
                vault, self._concurrency(1),
                patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 3),
                patch("app.storage.hash_stable_regular_file", side_effect=hashing),
            )
        self.assertEqual(result, {"stopped": 1})
        self.assertEqual(len(calls), 5)
        digests = self._digests(vault_id)
        self.assertEqual(len(digests), 13)
        published = {path for path, digest in digests.items() if digest}
        # Both batches are published: the full one (3) and the one in progress (1).
        self.assertEqual(len(published), 4)
        rollback.assert_not_called()
        missing.assert_not_called()
        self.assertEqual(self._source_errors(vault_id), [])
        self.assertNotEqual(storage.runtime_status[vault_id].get("scan_phase"), "finalization")

        # A restart hashes only what has no digest yet.
        shutdown_state.reset()
        rehashed = []

        def counting(path, **kwargs):
            rehashed.append(path.relative_to(self.albums).as_posix())
            return real_hash(path, **kwargs)

        result = self._scan(
            vault, self._concurrency(1),
            patch("app.storage.hash_stable_regular_file", side_effect=counting),
        )
        self.assertEqual(result["local"], 13)
        self.assertEqual(len(rehashed), 9)
        self.assertFalse(published & set(rehashed))
        self.assertTrue(all(self._digests(vault_id).values()))

    def test_shutdown_with_parallel_hashing_stops_without_new_reads(self) -> None:
        import app.storage as storage
        vault = self._vault()
        vault_id = int(vault["id"])
        real_hash = storage.hash_stable_regular_file
        lock = threading.Lock()
        calls = []

        def hashing(path, **kwargs):
            with lock:
                calls.append(path)
                if len(calls) == 3:
                    shutdown_state.request()
            return real_hash(path, **kwargs)

        result = self._scan(
            vault, self._concurrency(2),
            patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 2),
            patch("app.storage.hash_stable_regular_file", side_effect=hashing),
        )
        self.assertEqual(result, {"stopped": 1})
        # At most the window refilled before the stop was seen (2 × concurrency
        # after each of the reads already done); the rest never start.
        self.assertLessEqual(len(calls), 3 + 4)
        self.assertLess(len(calls), 13)
        self.assertLess(sum(1 for digest in self._digests(vault_id).values() if digest), 13)
        self.assertEqual(self._source_errors(vault_id), [])

    def _hold_hashing_until_shutdown(self, started: threading.Event):
        import app.storage as storage
        real_hash = storage.hash_stable_regular_file

        def hashing(path, **kwargs):
            started.set()
            deadline = time.monotonic() + 10
            while not shutdown_state.requested and time.monotonic() < deadline:
                time.sleep(0.01)
            # A slow disk: hashing every remaining file would exceed the budget.
            time.sleep(0.4)
            return real_hash(path, **kwargs)

        return patch("app.storage.hash_stable_regular_file", side_effect=hashing)

    def test_drain_awaits_a_stopping_maintenance_scan_and_succeeds(self) -> None:
        import app.storage as storage
        vault = self._vault()
        vault_id = int(vault["id"])
        started = threading.Event()
        with ExitStack() as stack:
            for item in (*_quiet_scan_patches(), self._concurrency(2),
                         self._hold_hashing_until_shutdown(started)):
                stack.enter_context(item)
            storage._submit_maintenance("scan", storage.scan_all_vaults)
            self.assertTrue(started.wait(10))
            began = time.monotonic()
            drained = asyncio.run(storage.drain_background_executors(timeout=1.5))
        self.assertTrue(drained)
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertFalse(storage.runtime_status[vault_id]["scanning"])
        self.assertEqual(self._source_errors(vault_id), [])

    def test_drain_awaits_a_watchers_paired_scan_and_succeeds(self) -> None:
        import app.storage as storage
        vault = self._vault()
        vault_id = int(vault["id"])
        started = threading.Event()
        with ExitStack() as stack:
            for item in (*_quiet_scan_patches(), self._concurrency(2),
                         self._hold_hashing_until_shutdown(started)):
                stack.enter_context(item)
            future = storage._submit_watcher_scan(vault)
            self.assertTrue(started.wait(10))
            drained = asyncio.run(storage.drain_background_executors(timeout=1.5))
        self.assertTrue(drained)
        self.assertEqual(future.result(), {"stopped": 1})
        self.assertEqual(self._source_errors(vault_id), [])
        self.assertFalse(storage._watcher_scan_futures)
