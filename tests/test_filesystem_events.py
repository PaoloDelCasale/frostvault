"""Filesystem regressions with real local files and no database/network."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchfiles import Change, awatch

from app import storage
from tests import test_local_release


class ReleaseWatcherRaceTests(test_local_release.LocalReleaseWorkerTests):
    # Deliberately drive the production observer inside the hash, rather than
    # relying on file size, sleeps or the kernel's scheduling of a large hash.
    def test_watcher_during_hash_does_not_invalidate_local_release(self):
        real_hash = storage.hash_stable_regular_file
        self.catalog.mark_local_path_missing.side_effect = lambda **_: self.target.update(local_presence="missing")
        access = SimpleNamespace(local_operations_allowed=True, volume_alias=None)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None

        def observe_while_hashing(path):
            storage.apply_filesystem_changes(self.job, {(Change.deleted, str(self.path))})
            return real_hash(path)

        with (patch("app.storage.hash_stable_regular_file", side_effect=observe_while_hashing),
              patch("app.storage.source_layout.vault_local_access", return_value=access),
              patch("app.storage._validate_enrolled_scan_root"),
              patch("app.storage.record_catalog_revision")):
            storage.process_free_space(self.job)
        self.assertFalse(self.path.exists())


class LiveWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_files_copied_after_new_directory_are_observed(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            ready = asyncio.Event()
            directory_seen = asyncio.Event()
            file_seen = asyncio.Event()
            events = []
            real_awatch = awatch

            async def ticking_watch(*args, **kwargs):
                # A timeout tick proves the native observer is installed. It
                # also removes timing guesses from the newly-created-dir test.
                async for changes in real_awatch(*args, **kwargs, yield_on_timeout=True, rust_timeout=50):
                    ready.set()
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
            stack.enter_context(patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=False)))
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
            stack.enter_context(patch("app.storage._validate_enrolled_scan_root"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True, volume_alias=None)))
            stack.enter_context(patch("app.storage.record_catalog_revision"))
            storage.apply_filesystem_changes({"id": 2, "source_root": str(root)}, {(Change.added, str(root / "incoming"))})
            observed = [call.kwargs["path"] for call in catalog.observe_local_copy.call_args_list]
            # Bulk writes are allowed; assert on their logical-path payload too.
            for call in catalog.observe_local_copies_batch.call_args_list:
                observed.extend(entry[0] for entry in call.kwargs["entries"])
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
