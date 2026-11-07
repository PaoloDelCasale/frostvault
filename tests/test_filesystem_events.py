"""Filesystem regressions with real local files and no database/network."""
from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import ExitStack, aclosing
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


    def test_failed_release_preserves_salvage_and_observes_external_replacement(self):
        real_hash = storage.hash_stable_regular_file
        def replace_then_fail(path):
            if path != self.path:
                self.path.write_bytes(b"other")
                raise RuntimeError("Injected hash failure")
            return real_hash(path)
        with patch("app.storage.hash_stable_regular_file", side_effect=replace_then_fail):
            with self.assertRaisesRegex(RuntimeError, "original content was preserved"):
                storage.process_free_space(self.job)
        self.assertEqual(self.path.read_bytes(), b"other")
        claims = list(self.path.parent.glob(".*.cleanup-*.tmp"))
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].read_bytes(), b"photo")
        self.catalog.observe_replaced_local_copy.assert_called_once()

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

    def test_separate_delayed_echoes_each_revalidate_the_same_restored_bytes(self):
        from app.services import local_observation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"photo")
            local_observation.remember(124, path.name, path.stat(), hashlib.sha256(b"photo").hexdigest())
            # deleted and added may arrive in different watcher batches after
            # rollback. Each batch must preserve this exact restored snapshot.
            with patch.object(local_observation, "hash_stable_regular_file", wraps=local_observation.hash_stable_regular_file) as hashing:
                self.assertTrue(local_observation.consume(124, path.name, path))
                self.assertTrue(local_observation.consume(124, path.name, path))
                self.assertTrue(local_observation.consume(124, path.name, path))
                self.assertEqual(hashing.call_count, 3)

    def test_later_same_stat_external_edit_is_hashed_and_retires_echo(self):
        from app.services import local_observation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"photo")
            observed = path.stat()
            signature = local_observation._signature(observed)
            local_observation.remember(125, path.name, observed, hashlib.sha256(b"photo").hexdigest())
            self.assertTrue(local_observation.consume(125, path.name, path))
            path.write_bytes(b"other")
            os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns))
            # Pin coarse/unchanged stat evidence: a fresh independent digest,
            # not a TTL/stat-only ignore, must catch this subsequent mutation.
            with patch.object(local_observation, "_signature", return_value=signature):
                self.assertFalse(local_observation.consume(125, path.name, path))
                path.write_bytes(b"photo")
                self.assertFalse(local_observation.consume(125, path.name, path))


class LiveWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_files_copied_after_new_directory_are_observed(self):
        await self.check_new_directory(False)

    async def test_polling_files_copied_after_new_directory_are_observed(self):
        await self.check_new_directory(True)

    async def test_native_sibling_changes_during_rearm_are_not_lost(self):
        await self.check_new_directory(False, mutate_siblings=True)

    async def test_polling_sibling_changes_during_topology_refresh_are_not_lost(self):
        await self.check_new_directory(True, mutate_siblings=True)

    async def test_slow_polling_preserves_siblings_without_replacing_snapshot(self):
        await self.check_new_directory(True, mutate_siblings=True, poll_ms=300)

    async def check_new_directory(self, force_polling, *, mutate_siblings=False, poll_ms=30):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            ready = asyncio.Event()
            rearmed = asyncio.Event()
            watch_calls = 0
            active_observers = 0
            peak_observers = 0
            directory_seen = asyncio.Event()
            file_seen = asyncio.Event()
            events = []
            real_awatch = awatch
            siblings_seen = asyncio.Event()
            for name in ("existing.jpg", "deleted.jpg", "before-rename.jpg"):
                (root / name).write_bytes(b"first")
            expected = {
                (Change.modified, str(root / "existing.jpg")),
                (Change.deleted, str(root / "deleted.jpg")),
                (Change.deleted, str(root / "before-rename.jpg")),
                (Change.added, str(root / "after-rename.jpg")),
            }

            mutated = False
            def change_siblings():
                nonlocal mutated
                mutated = True
                original = (root / "existing.jpg").stat()
                (root / "existing.jpg").write_bytes(b"modified sibling bytes")
                # notify's polling backend compares whole-second mtimes.
                os.utime(root / "existing.jpg", ns=(original.st_atime_ns, original.st_mtime_ns + 2_000_000_000))
                (root / "deleted.jpg").unlink()
                (root / "before-rename.jpg").rename(root / "after-rename.jpg")

            async def ticking_watch(*args, **kwargs):
                # A timeout tick proves the native observer is installed. It
                # also removes timing guesses from the newly-created-dir test.
                nonlocal watch_calls, active_observers, peak_observers
                watch_calls += 1
                active_observers += 1
                peak_observers = max(peak_observers, active_observers)
                if watch_calls == 2 and mutate_siblings:
                    # At successor construction: old implementation has already
                    # closed the observer. An overlapping handoff must retain
                    # these unrelated writes/deletions/renames in the old one.
                    change_siblings()
                kwargs.update(yield_on_timeout=True, rust_timeout=50)
                try:
                    async with aclosing(real_awatch(*args, **kwargs)) as observer:
                        async for changes in observer:
                            ready.set()
                            if watch_calls > 1:
                                rearmed.set()
                            yield changes
                finally:
                    active_observers -= 1

            def apply(vault, changes):
                events.extend(changes)
                if expected <= set(events):
                    siblings_seen.set()
                for _, value in changes:
                    if value == str(root / "new"):
                        if force_polling and mutate_siblings and not mutated:
                            # Same topology-refresh boundary, but polling must
                            # keep the observer/snapshot instead of rearming it.
                            change_siblings()
                        directory_seen.set()
                    if value == str(root / "new" / "nested" / "later.bin"):
                        file_seen.set()

            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True)))
            stack.enter_context(patch("app.storage._runtime_settings", return_value=SimpleNamespace(filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=poll_ms)))
            stack.enter_context(patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=force_polling)))
            stack.enter_context(patch("app.storage.awatch", side_effect=ticking_watch))
            # Keep callbacks on the event-loop thread for asyncio.Event safety.
            async def direct(function, *args, **kwargs):
                return function(*args, **kwargs)
            stack.enter_context(patch("app.storage.asyncio.to_thread", side_effect=direct))
            stack.enter_context(patch("app.storage.apply_filesystem_changes", side_effect=apply))
            # The scan paired with every watcher start has its own tests.
            stack.enter_context(patch("app.storage.scan_vault", return_value={}))
            task = asyncio.create_task(storage._watch_vault_filesystem({"id": 2, "source_root": directory}))
            try:
                await asyncio.wait_for(ready.wait(), 3)
                (root / "new" / "nested").mkdir(parents=True)
                await asyncio.wait_for(directory_seen.wait(), 3)
                if not force_polling:
                    await asyncio.wait_for(rearmed.wait(), 3)
                if mutate_siblings:
                    try:
                        await asyncio.wait_for(siblings_seen.wait(), 3)
                    except TimeoutError:
                        self.fail(f"Missing sibling events: {expected - set(events)}; got {events}")
                (root / "new" / "nested" / "later.bin").write_bytes(b"later")
                await asyncio.wait_for(file_seen.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(events)
            self.assertEqual(active_observers, 0, "shutdown leaked a recursive observer")
            self.assertEqual(peak_observers, 1 if force_polling else 2)
            if force_polling:
                self.assertEqual(watch_calls, 1, "polling must keep its original snapshot")


class WatcherSubtreeTests(unittest.TestCase):
    def test_directory_echo_preserves_recovered_child_but_invalidates_external_child(self):
        from app.services import local_observation
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            recovered = root / "new" / "photo.jpg"
            recovered.parent.mkdir()
            recovered.write_bytes(b"photo")
            (recovered.parent / "external.jpg").write_bytes(b"other")
            local_observation.remember(126, "new/photo.jpg", recovered.stat(), hashlib.sha256(b"photo").hexdigest())
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            catalog = stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
            invalidated = []
            def record_sql(sql, params=()):
                if "plaintext_sha256=NULL" in sql:
                    invalidated.extend(params[2])
                return connection.execute.return_value
            connection.execute.side_effect = record_sql
            stack.enter_context(patch("app.storage._validate_enrolled_scan_root"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None,
            )))
            stack.enter_context(patch("app.storage.record_catalog_revision"))
            storage.apply_filesystem_changes({"id": 126, "source_root": directory}, {
                (Change.added, str(recovered.parent)),
                (Change.added, str(recovered)),
            })
            self.assertTrue(catalog.observe_local_copies_batch.called)
            self.assertEqual(invalidated, ["new/external.jpg"])
            # A later file echo cannot repair a digest the subtree has erased.
            self.assertTrue(local_observation.consume(126, "new/photo.jpg", recovered))

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
            catalog.observe_local_copies_batch.reset_mock()
            self.assertEqual(storage.apply_filesystem_changes(
                {"id": 2, "source_root": str(root)}, {(Change.modified, str(root / "incoming"))},
            ), 0)
            catalog.observe_local_copies_batch.assert_not_called()


class SubtreeContainmentTests(unittest.TestCase):
    def check_swap(self, *, during_walk=False, nested=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            base = Path(directory)
            root = base / "vault"
            incoming = root / "parent" / "incoming" if nested else root / "incoming"
            incoming.mkdir(parents=True)
            for name in ("a.jpg", "b.jpg", "c.jpg"):
                (incoming / name).write_bytes(b"inside")
            outside = base / "outside"
            outside.mkdir()
            (outside / "outside-only.txt").write_bytes(b"private")
            if nested:
                (outside / "incoming").mkdir()
                (outside / "incoming" / "outside-only.txt").write_bytes(b"private")
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            catalog = stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
            observed = []
            catalog.observe_local_copies_batch.side_effect = lambda **kwargs: observed.extend(entry[0] for entry in kwargs["entries"])
            stack.enter_context(patch("app.storage._validate_enrolled_scan_root"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True, volume_alias=None)))
            stack.enter_context(patch("app.storage.record_catalog_revision"))
            rollback = stack.enter_context(patch("app.storage._rollback_local_scan_journal"))
            stack.enter_context(patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 1))
            real_scan, real_inspect, real_read = storage._scan_tree, storage.inspect_entry, os.read
            read_outside = []
            inspections = 0

            def swap():
                target = incoming.parent if nested else incoming
                target.rename(base / "detached")
                target.symlink_to(outside, target_is_directory=True)

            def scan(*args, **kwargs):
                if not during_walk:
                    swap()  # After is_symlink(), immediately before real traversal.
                return real_scan(*args, **kwargs)

            def inspect(path):
                nonlocal inspections
                result = real_inspect(path)
                if result and result[0] == "regular":
                    inspections += 1
                    if during_walk and inspections == 2:
                        swap()  # First bounded batch has already committed.
                return result

            def read(fd, count):
                if Path(os.readlink(f"/proc/self/fd/{fd}")).is_relative_to(outside):
                    read_outside.append(True)
                return real_read(fd, count)

            with (patch("app.storage._scan_tree", side_effect=scan),
                  patch("app.storage.inspect_entry", side_effect=inspect),
                  patch("os.read", side_effect=read)):
                error = None
                try:
                    storage.apply_filesystem_changes({"id": 2, "source_root": str(root)}, {(Change.added, str(incoming))})
                except RuntimeError as exc:
                    error = exc
            self.assertEqual(read_outside, [], "read bytes outside the enrolled Vault")
            self.assertFalse(any("outside-only" in path for path in observed), observed)
            self.assertIsNotNone(error, "substituted subtree must fail closed")
            rollback.assert_called_once()
            if during_walk:
                self.assertGreaterEqual(connection.commit.call_count, 1)

    def test_subtree_swapped_to_symlink_before_traversal(self):
        self.check_swap()

    def test_ancestor_swapped_to_symlink_before_traversal(self):
        self.check_swap(nested=True)

    def test_subtree_swapped_during_traversal_after_committed_batch(self):
        self.check_swap(during_walk=True)

    def test_ancestor_swapped_during_traversal_after_committed_batch(self):
        self.check_swap(during_walk=True, nested=True)


class ReleaseRepairVaultIdentityTests(unittest.TestCase):
    setUp = test_local_release.LocalReleaseWorkerTests.setUp

    def test_repair_uses_vault_id_not_unrelated_same_number_job_id(self):
        self.job.update(id=100, vault_id=2)
        vault = {"id": 2, "source_root": self.job["source_root"]}
        unrelated = {"id": 100, "source_root": "/unrelated"}
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.side_effect = lambda sql, params: SimpleNamespace(fetchone=lambda: vault if params == (2,) else unrelated)
        with (patch("app.storage._validate_enrolled_scan_root") as validate,
              patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True))):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        validate.assert_called_with(vault)
        self.catalog.set_local_fingerprint.assert_called_once()

    def test_missing_actual_vault_never_falls_back_to_unenrolled_job(self):
        self.job.update(id=100, vault_id=2)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None
        with (patch("app.storage.inspect_entry") as inspect,
              self.assertRaises(RuntimeError)):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        inspect.assert_not_called()
        self.catalog.set_local_fingerprint.assert_not_called()

    def test_actual_vault_identity_change_fails_before_read(self):
        self.job.update(id=100, vault_id=2)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = {
            "id": 2, "source_root": self.job["source_root"],
            "root_identity": "replaced", "root_identity_version": storage.vault_relocation.ROOT_IDENTITY_VERSION,
        }
        with (patch("app.storage.inspect_entry") as inspect,
              self.assertRaisesRegex(RuntimeError, "identity mismatch")):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        inspect.assert_not_called()
        self.catalog.set_local_fingerprint.assert_not_called()

    def test_actual_vault_unavailable_source_fails_before_read(self):
        self.job.update(id=100, vault_id=2)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = {"id": 2, "source_root": self.job["source_root"]}
        with (patch("app.storage.inspect_entry") as inspect,
              patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=False)),
              self.assertRaisesRegex(RuntimeError, "Source Volume changed")):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        inspect.assert_not_called()
        self.catalog.set_local_fingerprint.assert_not_called()

    def test_changed_actual_vault_fails_before_read_or_publication(self):
        self.job.update(id=100, vault_id=2)
        connection = self.stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = {"id": 2, "source_root": "/changed"}
        with (patch("app.storage.inspect_entry") as inspect,
              self.assertRaisesRegex(RuntimeError, "root changed")):
            storage._repair_failed_local_release(self.job, self.target, self.path)
        inspect.assert_not_called()
        self.catalog.set_local_fingerprint.assert_not_called()


class SDKTemporaryNameTests(unittest.TestCase):
    def test_managed_download_suffix_is_reserved_only_on_our_generated_names(self):
        for operation in ("restore", "verify"):
            name = f".photo.jpg.{operation}-{'a' * 32}.tmp.f8Fc29bA"
            self.assertTrue(storage.is_restore_temporary_name(name), name)
            self.assertFalse(storage._filesystem_watch_filter(Change.added, name))
        for name in ("photo.jpg.tmp.f8Fc29bA", "notes.tmp.12345678", ".user.restore-not-a-uuid.tmp.f8Fc29bA"):
            self.assertFalse(storage.is_restore_temporary_name(name), name)
            self.assertTrue(storage._filesystem_watch_filter(Change.added, name))


class UnsupportedNameWalkTests(unittest.TestCase):
    """Issue #494: a name that is not UTF-8 never aborts a walk."""

    def make_tree(self, base: Path) -> Path:
        root = base / "vault"
        (root / "docs").mkdir(parents=True)
        (root / "ok.txt").write_bytes(b"ok")
        (root / "docs" / "fine.txt").write_bytes(b"fine")
        raw = os.fsencode(root)
        with open(raw + b"/non-utf8-\xe9.txt", "wb") as handle:
            handle.write(b"bad")
        os.mkdir(raw + b"/dir-\xff")
        with open(raw + b"/dir-\xff/inside.txt", "wb") as handle:
            handle.write(b"inside")
        return root

    def test_walk_skips_unsupported_names_with_their_subtree_and_continues(self):
        from app.services.fs_preflight import PinnedTree
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_tree(Path(directory))
            seen, unsupported, failed = [], [], []
            with PinnedTree(root) as tree:
                for entry, _ in tree.entries(
                    on_error=lambda path, exc: failed.append(path),
                    on_unsupported_name=unsupported.append,
                ):
                    seen.append(entry.relative_to(root).as_posix())
            self.assertEqual(sorted(seen), ["docs", "docs/fine.txt", "ok.txt"])
            self.assertEqual(failed, [])
            self.assertEqual(
                sorted(entry.relative_to(root).as_posix().encode("utf-8", "backslashreplace") for entry in unsupported),
                [b"dir-\\udcff", b"non-utf8-\\udce9.txt"],
            )

    def test_scan_catalogues_the_rest_and_reports_each_name_once_without_incompleteness(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = self.make_tree(Path(directory))
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            catalog = stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
            observed = []
            catalog.observe_local_copies_batch.side_effect = lambda **kw: observed.extend(e[0] for e in kw["entries"])
            problems = []
            stack.enter_context(patch("app.storage.report_problem", side_effect=lambda *a, **kw: problems.append(kw)))
            resolved = []
            stack.enter_context(patch("app.storage.resolve_stale_path_problems", side_effect=lambda *a, **kw: resolved.append(kw)))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None, volume_health="healthy",
            )))
            journal = {}
            storage._scan_tree(
                connection, {"id": 7, "source_root": str(root)}, "scan", root=root,
                allow_chunk_commits=False, scan_journal=journal,
            )
            self.assertEqual(sorted(observed), ["docs/fine.txt", "ok.txt"])
            self.assertEqual(
                sorted((p["event"], p["path"]) for p in problems),
                [("file_name_unsupported", "dir-\udcff"), ("file_name_unsupported", "non-utf8-\udce9.txt")],
            )
            # The catalog is still complete: nothing may be blocked or left unconfirmed.
            self.assertNotIn("incomplete", journal)
            # A complete walk of the whole root closes every other open problem.
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0]["event"], "file_name_unsupported")
            self.assertEqual(resolved[0]["keep_paths"], {"dir-\\udcff", "non-utf8-\\udce9.txt"})

    def test_a_subtree_walk_never_closes_other_problems(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = self.make_tree(Path(directory))
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            stack.enter_context(patch("app.storage.ArchiveCatalog"))
            stack.enter_context(patch("app.storage.report_problem"))
            resolved = stack.enter_context(patch("app.storage.resolve_stale_path_problems"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None, volume_health="healthy",
            )))
            storage._scan_tree(
                connection, {"id": 7, "source_root": str(root)}, "scan", root=root,
                subtree=root / "docs", allow_chunk_commits=False, finalize_missing=False,
            )
            resolved.assert_not_called()

    def test_report_problem_shows_undecodable_bytes_as_escapes(self):
        from app.services.problems import display_path
        self.assertEqual(display_path("a\udce9.txt"), "a\\udce9.txt")
        self.assertEqual(display_path("plain/ünï.txt"), "plain/ünï.txt")


class UndecodablePathErrorTests(unittest.IsolatedAsyncioTestCase):
    def test_exception_groups_are_searched(self):
        error = ExceptionGroup("unhandled errors in a TaskGroup", [
            RuntimeError('Unable to decode path "/v/x\\xE9" to string'),
        ])
        self.assertTrue(storage._is_undecodable_path_error(error))
        self.assertFalse(storage._is_undecodable_path_error(RuntimeError("boom")))
        self.assertFalse(storage._is_undecodable_path_error(
            ExceptionGroup("g", [PermissionError("Permission denied")])))

    async def test_the_real_observer_failure_is_recognised(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = os.fsencode(directory)
            observer = awatch(directory, yield_on_timeout=True, rust_timeout=200, debounce=30)
            failure = None
            try:
                await asyncio.wait_for(anext(observer), 5)
                with open(raw + b"/non-utf8-\xe9.txt", "wb") as handle:
                    handle.write(b"x")
                for _ in range(20):
                    await asyncio.wait_for(anext(observer), 5)
            except Exception as exc:
                failure = exc
            finally:
                await observer.aclose()
            self.assertIsNotNone(failure, "the observer should stop on an undecodable path")
            self.assertTrue(storage._is_undecodable_path_error(failure), repr(failure))


class WatcherScanPairingTests(unittest.IsolatedAsyncioTestCase):
    """Issues #494-#497: every healthy watcher start is paired with a scan."""

    def start(self, stack, root, observers, scans, *, record_health=None):
        self.awatch_kwargs = []
        calls = iter(observers)

        def make_observer(*args, **kwargs):
            self.awatch_kwargs.append(kwargs)
            return next(calls)(*args, **kwargs)

        connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None
        stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=True)))
        stack.enter_context(patch("app.storage._runtime_settings", return_value=SimpleNamespace(filesystem_watch_debounce_ms=30, filesystem_watch_poll_ms=30)))
        stack.enter_context(patch("app.storage.settings", SimpleNamespace(filesystem_watch_force_polling=False)))
        stack.enter_context(patch("app.storage.awatch", side_effect=make_observer))
        async def direct(function, *args, **kwargs):
            return function(*args, **kwargs)
        stack.enter_context(patch("app.storage.asyncio.to_thread", side_effect=direct))
        real_sleep = asyncio.sleep
        async def fast_sleep(delay, *args):
            await real_sleep(min(delay, 0.01))
        stack.enter_context(patch("app.storage.asyncio.sleep", side_effect=fast_sleep))
        stack.enter_context(patch("app.storage._resolve_worker_error"))
        self.report = stack.enter_context(patch("app.storage._report_worker_error"))
        self.health = stack.enter_context(patch("app.storage._record_scan_component_health"))
        stack.enter_context(patch("app.storage.worker_error_store.log_worker_error"))
        self.scan = stack.enter_context(patch("app.storage.scan_vault", side_effect=scans))
        return asyncio.create_task(storage._watch_vault_filesystem({"id": 2, "source_root": str(root)}))

    @staticmethod
    def quiet(*args, **kwargs):
        async def generate():
            while True:
                yield set()
                await asyncio.sleep(0.01)
        return generate()

    @staticmethod
    def failing(error):
        def make(*args, **kwargs):
            async def generate():
                yield set()
                raise error
            return generate()
        return make

    async def settle(self, condition, timeout=3):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            self.assertLess(asyncio.get_running_loop().time(), deadline, "condition not reached")
            await asyncio.sleep(0.01)

    async def stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_a_new_watcher_always_starts_with_one_scan(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            task = self.start(stack, directory, [self.quiet], [{}])
            try:
                await self.settle(lambda: self.scan.call_count >= 1)
                await asyncio.sleep(0.3)
                self.assertEqual(self.scan.call_count, 1, "a reconciled Vault is not rescanned every tick")
            finally:
                await self.stop(task)

    async def test_the_scan_waits_for_the_registration_barrier(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            release = asyncio.Event()
            def late(*args, **kwargs):
                async def generate():
                    await release.wait()
                    while True:
                        yield set()
                        await asyncio.sleep(0.01)
                return generate()
            task = self.start(stack, directory, [late], [{}])
            try:
                await asyncio.sleep(0.2)
                self.assertEqual(self.scan.call_count, 0, "scanning before registration loses events in the gap")
                release.set()
                await self.settle(lambda: self.scan.call_count == 1)
            finally:
                await self.stop(task)

    async def test_an_unreadable_subfolder_does_not_stop_the_observer(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            task = self.start(stack, directory, [self.quiet], [{}])
            try:
                await self.settle(lambda: self.awatch_kwargs)
                self.assertTrue(self.awatch_kwargs[0]["ignore_permission_denied"])
            finally:
                await self.stop(task)

    async def test_a_non_utf8_name_restarts_the_watcher_and_rescans_without_an_internal_error(self):
        error = ExceptionGroup("unhandled errors in a TaskGroup", [
            RuntimeError('Unable to decode path "/v/non-utf8-\\xE9.txt" to string'),
        ])
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            task = self.start(stack, directory, [self.failing(error), self.quiet], [{}, {}])
            try:
                await self.settle(lambda: self.scan.call_count >= 2)
                await asyncio.sleep(0.2)
                self.assertEqual(self.scan.call_count, 2)
            finally:
                await self.stop(task)
            self.report.assert_not_called()
            self.health.assert_not_called()

    async def test_other_watcher_failures_are_still_reported_and_followed_by_a_scan(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            task = self.start(stack, directory, [self.failing(RuntimeError("boom")), self.quiet], [{}, {}])
            try:
                await self.settle(lambda: self.scan.call_count >= 2)
            finally:
                await self.stop(task)
            self.report.assert_called_once()
            self.assertEqual(self.report.call_args.args[0], "source_watcher")

    async def test_an_os_error_blames_a_path_not_the_root(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            task = self.start(stack, directory, [self.failing(PermissionError(13, "denied")), self.quiet], [{}, {}])
            try:
                await self.settle(lambda: self.health.call_count >= 1)
            finally:
                await self.stop(task)
            message = self.health.call_args.kwargs["error"]
            self.assertIn("unreadable path", message)
            self.assertNotIn("root", message)

    async def test_an_incomplete_scan_is_repeated_until_the_vault_is_readable(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("app.storage.WATCHER_INCOMPLETE_RESCAN_SECONDS", 0.05))
            task = self.start(stack, directory, [self.quiet], [{"local_incomplete": 1}, {"local_incomplete": 1}, {}])
            try:
                await self.settle(lambda: self.scan.call_count >= 3)
                await asyncio.sleep(0.3)
                self.assertEqual(self.scan.call_count, 3, "a complete scan ends the repetition")
            finally:
                await self.stop(task)

    async def test_a_scan_that_found_another_scan_running_is_retried(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("app.storage.WATCHER_SCAN_BUSY_SECONDS", 0.05))
            task = self.start(stack, directory, [self.quiet], [{"already_running": 1}, {}])
            try:
                await self.settle(lambda: self.scan.call_count >= 2)
                await asyncio.sleep(0.2)
                self.assertEqual(self.scan.call_count, 2)
            finally:
                await self.stop(task)

    async def test_restart_pauses_grow_and_are_capped(self):
        delays = [storage._watcher_restart_delay(n) for n in range(1, 10)]
        self.assertEqual(delays[:4], [5.0, 10.0, 20.0, 40.0])
        self.assertEqual(max(delays), float(storage.WATCHER_RESTART_MAX_SECONDS))
