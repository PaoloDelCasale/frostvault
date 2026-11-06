"""Filesystem regressions with real local files and no database/network."""
from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchfiles import Change, awatch

from app import storage
from tests import test_local_release


class ReleaseWatcherRaceTests(unittest.TestCase):
    setUp = test_local_release.LocalReleaseWorkerTests.setUp

    # Deliberately drive the production observer inside the hash, rather than
    # relying on file size, sleeps or the kernel's scheduling of a large hash.
    def test_watcher_during_hash_does_not_invalidate_local_release(self):
        real_hash = storage.hash_stable_regular_file
        self.catalog.mark_local_path_missing.side_effect = lambda **_: self.target.update(local_presence="missing")
        access = SimpleNamespace(local_operations_allowed=True, volume_alias=None)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None

        threads = []
        def observe_while_hashing(path):
            started = threading.Event()
            def watch():
                started.set()
                storage.apply_filesystem_changes(self.job, {(Change.deleted, str(self.path))})
            thread = threading.Thread(target=watch)
            threads.append(thread)
            thread.start()
            self.assertTrue(started.wait(2))
            # On the broken path, deterministically finish the watcher before
            # the hash. With serialization it must wait for catalog publication.
            if not storage.scan_lock_for_vault(self.job["vault_id"]).locked():
                thread.join(2)
                self.assertFalse(thread.is_alive())
            return real_hash(path)

        with (patch("app.storage.hash_stable_regular_file", side_effect=observe_while_hashing),
              patch("app.storage.source_layout.vault_local_access", return_value=access),
              patch("app.storage._validate_enrolled_scan_root"),
              patch("app.storage.record_catalog_revision")):
            try:
                storage.process_free_space(self.job)
            finally:
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
        self.assertFalse(self.path.exists())


    def test_one_gib_file_with_active_watcher_is_released(self):
        size = 1 << 30
        with self.path.open("wb") as stream:
            stream.truncate(size)  # Sparse, disposable file; no media or real data.
        expected = hashlib.sha256()
        zeroes = b"\x00" * (1 << 20)
        for _ in range(1024):
            expected.update(zeroes)
        self.target.update(local_size=size, cloud_size=size, local_sha256=expected.hexdigest(), version_sha256=expected.hexdigest())
        self.target["local_mtime_ns"] = self.path.stat().st_mtime_ns
        self.head.return_value = {"ContentLength": size, "VersionId": "exact"}
        self.test_watcher_during_hash_does_not_invalidate_local_release()


class LocalObservationEchoTests(unittest.TestCase):
    def test_external_same_size_and_mtime_edit_is_not_suppressed(self):
        from app.services import local_observation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"first")
            observed = path.stat()
            local_observation.remember(123, path.name, observed, hashlib.sha256(b"first").hexdigest())
            path.write_bytes(b"other")
            os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns))
            self.assertFalse(local_observation.consume(123, path.name, path))

    def test_echo_is_exact_one_shot_and_does_not_restat_later_content(self):
        from app.services import local_observation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"photo")
            local_observation.remember(124, path.name, path.stat(), hashlib.sha256(b"photo").hexdigest())
            self.assertTrue(local_observation.consume(124, path.name, path))
            self.assertFalse(local_observation.consume(124, path.name, path))


class LiveWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_files_copied_after_new_directory_are_observed(self):
        await self.check_new_directory(False)

    async def test_polling_files_copied_after_new_directory_are_observed(self):
        await self.check_new_directory(True)

    async def check_new_directory(self, force_polling):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            ready = asyncio.Event()
            rearmed = asyncio.Event()
            watch_calls = 0
            directory_seen = asyncio.Event()
            file_seen = asyncio.Event()
            events = []
            real_awatch = awatch

            async def ticking_watch(*args, **kwargs):
                # A timeout tick proves the native observer is installed. It
                # also removes timing guesses from the newly-created-dir test.
                nonlocal watch_calls
                watch_calls += 1
                kwargs.update(yield_on_timeout=True, rust_timeout=50)
                async for changes in real_awatch(*args, **kwargs):
                    ready.set()
                    if watch_calls > 1:
                        rearmed.set()
                    yield changes

            def apply(vault, changes):
                events.extend(changes)
                for _, value in changes:
                    if value == str(root / "new"):
                        directory_seen.set()
                    if value == str(root / "new" / "nested" / "later.bin"):
                        file_seen.set()

            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True)))
            stack.enter_context(patch("app.storage._runtime_settings", return_value=SimpleNamespace(filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=30)))
            stack.enter_context(patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=force_polling)))
            stack.enter_context(patch("app.storage.awatch", side_effect=ticking_watch))
            # Keep callbacks on the event-loop thread for asyncio.Event safety.
            async def direct(function, *args, **kwargs):
                return function(*args, **kwargs)
            stack.enter_context(patch("app.storage.asyncio.to_thread", side_effect=direct))
            stack.enter_context(patch("app.storage.apply_filesystem_changes", side_effect=apply))
            task = asyncio.create_task(storage._watch_vault_filesystem({"id": 2, "source_root": directory}))
            try:
                await asyncio.wait_for(ready.wait(), 3)
                (root / "new" / "nested").mkdir(parents=True)
                await asyncio.wait_for(directory_seen.wait(), 3)
                await asyncio.wait_for(rearmed.wait(), 3)
                (root / "new" / "nested" / "later.bin").write_bytes(b"later")
                await asyncio.wait_for(file_seen.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(events)


class WatcherSubtreeTests(unittest.TestCase):
    def test_move_in_emits_only_directory_event_but_catalogues_nested_files(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            base = Path(directory)
            root = base / "vault"
            root.mkdir()
            incoming = base / "incoming"
            (incoming / "nested").mkdir(parents=True)
            (incoming / "nested" / "file.bin").write_bytes(b"data")
            incoming.rename(root / "incoming")
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            catalog = stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
            observed = []
            catalog.observe_local_copy.side_effect = lambda **kwargs: observed.append(kwargs["path"])
            catalog.observe_local_copies_batch.side_effect = lambda **kwargs: observed.extend(entry[0] for entry in kwargs["entries"])
            stack.enter_context(patch("app.storage._validate_enrolled_scan_root"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True, volume_alias=None)))
            stack.enter_context(patch("app.storage.record_catalog_revision"))
            storage.apply_filesystem_changes({"id": 2, "source_root": str(root)}, {(Change.added, str(root / "incoming"))})
            self.assertIn("incoming/nested/file.bin", observed)


class SDKTemporaryNameTests(unittest.TestCase):
    def test_managed_download_suffix_is_reserved_only_on_our_generated_names(self):
        for operation in ("restore", "verify"):
            name = f".photo.jpg.{operation}-{'a' * 32}.tmp.f8Fc29bA"
            self.assertTrue(storage.is_restore_temporary_name(name), name)
            self.assertFalse(storage._filesystem_watch_filter(Change.added, name))
        for name in ("photo.jpg.tmp.f8Fc29bA", "notes.tmp.12345678", ".user.restore-not-a-uuid.tmp.f8Fc29bA"):
            self.assertFalse(storage.is_restore_temporary_name(name), name)
            self.assertTrue(storage._filesystem_watch_filter(Change.added, name))
