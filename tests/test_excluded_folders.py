"""#688: Excluded Folders are not observed (ADR-0044).

Rule parsing is pure. Pruning runs on real directory trees: the walk, the
watch plan and the real watcher with native observers; the catalog is a mock
there. The missing transitions run against PostgreSQL.
"""
from __future__ import annotations

import asyncio
import re
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import storage
from app.services import watch_plan
from app.services.excluded_folders import ExcludedFolders, excluded_folders
from app.services.fs_preflight import PinnedTree
from app.services.operation_policies import OperationPolicy, path_is_included
from app.services.path_rules import folder_rule, path_matches, sql_path_regex
from tests.test_watcher_real_filesystem import VAULT_ID, Harness


def rules(*exclude: str, include: tuple[str, ...] = ()) -> ExcludedFolders:
    return excluded_folders(OperationPolicy(include_globs=include, exclude_globs=exclude))


class FolderRuleTests(unittest.TestCase):
    def test_only_whole_folder_patterns_prune(self):
        for pattern in ("backups/**", "**/node_modules/**", "a/b/**", "**/.cache*/**", "tmp?/**"):
            self.assertIsNotNone(folder_rule(pattern), pattern)
        for pattern in (
            "*.tmp", "**/x/*.jpg", "backups", "backups/*", "**", "**/**", "a/**/b/**",
            "[ab]/**", "a\\b/**", "../x/**", "a//b/**",
        ):
            self.assertIsNone(folder_rule(pattern), pattern)

    def test_fixed_noise_exclusions_never_prune(self):
        folders = rules()
        for directory in ("._resource", ".DS_Store", "x.lock"):
            self.assertFalse(folders.covers_directory(directory))

    def test_anchored_and_floating_folders(self):
        folders = rules("backups/**", "**/node_modules/**")
        self.assertTrue(folders.covers_directory("backups"))
        self.assertTrue(folders.covers_directory("backups/deep"))
        self.assertFalse(folders.covers_directory("a/backups"))
        self.assertFalse(folders.covers_directory("backups-old"))
        self.assertTrue(folders.covers_directory("node_modules"))
        self.assertTrue(folders.covers_directory("src/app/node_modules/pkg"))
        self.assertFalse(folders.covers_directory("src/node_modules_x"))
        # A file named like the folder is not inside it.
        self.assertFalse(folders.contains_path("backups"))
        self.assertTrue(folders.contains_path("backups/a.txt"))

    def test_include_globs_never_reopen_an_excluded_path(self):
        policy = OperationPolicy(include_globs=("**/*.jpg",), exclude_globs=("backups/**",))
        self.assertFalse(path_is_included("backups/photo.jpg", policy))
        self.assertTrue(excluded_folders(policy).contains_path("backups/photo.jpg"))

    def test_everything_inside_a_pruned_folder_is_excluded_by_admission_and_sql(self):
        """The rule is certain: Python admission and the SQL projection agree."""
        patterns = ("backups/**", "**/node_modules/**", "**/.cache*/**", "a/*/c/**", "tmp?/**")
        folders = rules(*patterns)
        candidates = [
            "backups/a.txt", "backups/x/y/z.bin", "src/node_modules/p/i.js", "node_modules/x",
            "home/.cache-v2/f", "a/b/c/d.txt", "a/b/cc/d.txt", "tmp1/x", "tmp12/x",
            "backups", "a/backups/x", "tmp/x", "x/a/b/c/d",
        ]
        for path in candidates:
            if not folders.contains_path(path):
                continue
            self.assertTrue(any(path_matches(path, pattern) for pattern in patterns), path)
            self.assertTrue(any(re.search(sql_path_regex(pattern), path) for pattern in patterns), path)

    def test_inside_regex_agrees_with_contains_path(self):
        folders = rules("backups/**", "**/node_modules/**", "**/.cache*/**", "a/*/c/**", "tmp?/**")
        _sql, (regexes,) = folders.not_inside_sql()
        for path in (
            "backups/a.txt", "backups", "a/backups/x", "src/node_modules/p/i.js",
            "node_modules", "home/.cache-v2/f", "a/b/c/d.txt", "a/b/c", "a/b/x/c/d",
            "tmp1/x", "tmp12/x", "back\\ups/x", "a/b\\x/c/d",
        ):
            in_sql = any(re.search(regex, path) for regex in regexes)
            self.assertEqual(in_sql, folders.contains_path(path), path)

    def test_a_folder_name_with_a_backslash_is_never_pruned(self):
        self.assertFalse(rules("**/x*/**").covers_directory("x\\y"))

    def test_narrowing(self):
        self.assertTrue(rules("a/**", "b/**").narrowed_by(rules("a/**")))
        self.assertFalse(rules("a/**").narrowed_by(rules("a/**", "b/**")))
        self.assertFalse(rules("a/**").narrowed_by(rules("a/**", "*.tmp")))
        self.assertFalse(rules("*.tmp").narrowed_by(rules()))


def _tree(directory: str) -> Path:
    root = Path(directory) / "vault"
    for folder in ("photos/2026", "backups/old/deep", "app/node_modules/pkg"):
        (root / folder).mkdir(parents=True)
    for path in ("photos/2026/a.jpg", "backups/b.zip", "backups/old/deep/c.zip",
                 "app/main.js", "app/node_modules/pkg/i.js", "backups.txt"):
        (root / path).write_bytes(b"x")
    return root


class PinnedTreePruneTests(unittest.TestCase):
    def test_an_excluded_folder_is_neither_entered_nor_listed(self):
        folders = rules("backups/**", "**/node_modules/**")
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            pruned: list[str] = []
            with PinnedTree(root) as tree:
                seen = [
                    entry.relative_to(root).as_posix()
                    for entry, _ in tree.entries(
                        on_error=lambda *_: self.fail("no error expected"),
                        prune=folders.covers_directory,
                        on_pruned=lambda entry: pruned.append(entry.relative_to(root).as_posix()),
                    )
                ]
                tree.validate()
                # Never opened: not part of the identity fence either.
                self.assertNotIn(("backups",), tree.identities)
        self.assertEqual(sorted(pruned), ["app/node_modules", "backups"])
        self.assertIn("photos/2026/a.jpg", seen)
        self.assertIn("backups.txt", seen)
        self.assertFalse([path for path in seen if path.startswith(("backups/", "app/node_modules/"))])

    def test_a_subtree_walk_inside_an_excluded_folder_yields_nothing(self):
        folders = rules("backups/**")
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            pruned: list[Path] = []
            with PinnedTree(root, subtree=root / "backups/old") as tree:
                seen = list(tree.entries(
                    on_error=lambda *_: None, prune=folders.covers_directory, on_pruned=pruned.append,
                ))
                tree.validate()
        self.assertEqual(seen, [])
        self.assertEqual(pruned, [root / "backups"])


class ScanTreePruneTests(unittest.TestCase):
    def test_a_full_scan_skips_excluded_folders_and_keeps_them_out_of_missing(self):
        folders = rules("backups/**")
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = _tree(directory)
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            catalog = stack.enter_context(patch("app.storage.ArchiveCatalog")).return_value
            observed: list[str] = []
            catalog.observe_local_copies_batch.side_effect = lambda **kw: observed.extend(e[0] for e in kw["entries"])
            stack.enter_context(patch("app.storage.report_problem"))
            stack.enter_context(patch("app.storage.resolve_stale_path_problems"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None, volume_health="healthy",
            )))
            self.addCleanup(watch_plan.forget, 7)
            storage._scan_tree(
                connection, {"id": 7, "source_root": str(root)}, "2026-11-15T13:00:00+00:00",
                root=root, allow_chunk_commits=False, scan_journal={}, excluded_folders=folders,
            )
        self.assertIn("photos/2026/a.jpg", observed)
        self.assertIn("backups.txt", observed)
        self.assertFalse([path for path in observed if path.startswith("backups/")])
        plan = watch_plan.unobserved(7)
        self.assertEqual(plan.excluded, {"backups"})
        self.assertIn("backups", plan.directories)
        self.assertFalse(plan)  # Not a gap: no re-check, no unconfirmed Vault.
        kwargs = catalog.mark_unseen_local_copies_missing.call_args.kwargs
        self.assertIs(kwargs["excluded_folders"], folders)

    def test_a_removed_excluded_folder_stays_out_of_the_plan_while_its_rule_holds(self):
        self.addCleanup(watch_plan.forget, 7)
        watch_plan.note_excluded_directories(7, {"gone"})
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = _tree(directory)
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            stack.enter_context(patch("app.storage.ArchiveCatalog"))
            stack.enter_context(patch("app.storage.resolve_stale_path_problems"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None, volume_health="healthy",
            )))
            storage._scan_tree(
                connection, {"id": 7, "source_root": str(root)}, "2026-11-15T13:00:00+00:00",
                root=root, allow_chunk_commits=False, scan_journal={},
                excluded_folders=rules("backups/**", "gone/**"),
            )
        self.assertEqual(watch_plan.unobserved(7).excluded, {"backups", "gone"})


class WatchPlanExcludedTests(unittest.TestCase):
    vault_id = 6881

    def setUp(self):
        watch_plan.forget(self.vault_id)
        self.addCleanup(watch_plan.forget, self.vault_id)

    def test_the_watcher_registers_around_an_excluded_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            found = watch_plan.UnobservedPaths(
                directories=frozenset({"backups", "app/node_modules"}),
                excluded=frozenset({"backups", "app/node_modules"}),
            )
            flat, recursive = watch_plan.watch_directories(root, found)
        self.assertEqual(sorted(flat), [root, root / "app"])
        self.assertEqual(sorted(recursive), [root / "photos"])

    def test_releasing_a_registered_excluded_folder_expands_coverage(self):
        excluded = frozenset({"backups", "cache"})
        found = watch_plan.UnobservedPaths(directories=excluded, excluded=excluded)
        watch_plan.record_full_scan(self.vault_id, found)
        watch_plan.record_registration(self.vault_id, found)
        released = watch_plan.release_excluded(self.vault_id, lambda path: path == "cache")
        self.assertEqual(released, {"backups"})
        plan = watch_plan.unobserved(self.vault_id)
        self.assertEqual(plan.excluded, {"cache"})
        self.assertEqual(plan.directories, {"cache"})
        # The new registration covers more: the watcher pairs it with a scan.
        self.assertFalse(watch_plan.source_confirmation_ready(self.vault_id))
        watch_plan.record_registration(self.vault_id, plan)
        self.assertTrue(watch_plan.source_confirmation_ready(self.vault_id))

    def test_without_a_watcher_nothing_is_released_to_it(self):
        excluded = frozenset({"backups"})
        watch_plan.record_full_scan(
            self.vault_id, watch_plan.UnobservedPaths(directories=excluded, excluded=excluded),
        )
        self.assertEqual(watch_plan.release_excluded(self.vault_id, lambda _path: False), set())
        self.assertEqual(watch_plan.unobserved(self.vault_id).directories, set())


class WatcherExcludedFolderTests(Harness):
    """The real watcher and batch application, with a stand-in scan."""

    def excluded_scan(self, root: Path, folders: ExcludedFolders):
        def scan(vault):
            excluded = frozenset(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir() and folders.covers_directory(path.relative_to(root).as_posix())
                and not folders.covers_directory(path.parent.relative_to(root).as_posix())
            )
            watch_plan.record_full_scan(
                VAULT_ID, watch_plan.UnobservedPaths(directories=excluded, excluded=excluded),
            )
            return {"local": 1, "cloud": 1}

        return self.stack.enter_context(patch("app.storage.scan_vault", side_effect=scan))

    async def test_no_watch_inside_an_excluded_folder_and_its_events_are_ignored(self):
        root = self.make_root()
        (root / "backups" / "deep").mkdir(parents=True)
        (root / "photos").mkdir()
        folders = rules("backups/**")
        observed = self.catalog_observations()
        self.stack.enter_context(patch("app.storage.excluded_folder_rules.load", return_value=folders))
        scan = self.excluded_scan(root, folders)
        task = self.start(root)
        try:
            await self.settle(lambda: scan.call_count >= 1 and any(
                str(root / "photos") in paths for paths in self.ready_recursive_registrations))
            registered = {path for paths in self.ready_registrations[-2:] for path in paths}
            self.assertNotIn(str(root / "backups"), registered)
            self.assertNotIn(str(root), {p for ps in self.ready_recursive_registrations[-1:] for p in ps})
            (root / "backups" / "deep" / "inside.zip").write_text("x")
            (root / "backups" / "new").mkdir()
            (root / "photos" / "a.jpg").write_text("x")
            (root / "top.txt").write_text("x")
            await self.settle(lambda: {"photos/a.jpg", "top.txt"} <= observed)
            await asyncio.sleep(0.3)
            self.assertFalse([path for path in observed if path.startswith("backups/")])
        finally:
            await self.stop(task)

    async def test_a_new_excluded_folder_is_registered_around(self):
        root = self.make_root()
        (root / "photos").mkdir()
        folders = rules("**/cache/**")
        observed = self.catalog_observations()
        self.stack.enter_context(patch("app.storage.excluded_folder_rules.load", return_value=folders))
        scan = self.excluded_scan(root, folders)
        task = self.start(root)
        try:
            await self.settle(lambda: scan.call_count >= 1)
            (root / "photos" / "cache").mkdir()
            (root / "photos" / "cache" / "thumb.jpg").write_text("x")
            await self.settle(lambda: "photos/cache" in watch_plan.unobserved(VAULT_ID).excluded)
            await self.settle(lambda: any(str(root / "photos") in paths and len(paths) >= 1
                                          for paths in self.ready_registrations[-2:])
                              and watch_plan.registered_directories(VAULT_ID) >= {"photos/cache"})
            (root / "photos" / "cache" / "later.jpg").write_text("x")
            (root / "photos" / "b.jpg").write_text("x")
            await self.settle(lambda: "photos/b.jpg" in observed)
            self.assertFalse([path for path in observed if "cache/" in path])
        finally:
            await self.stop(task)

    async def test_releasing_the_rule_rewatches_the_folder_and_pairs_a_scan(self):
        root = self.make_root()
        (root / "backups").mkdir()
        (root / "photos").mkdir()
        folders = rules("backups/**")
        observed = self.catalog_observations()
        load = self.stack.enter_context(patch("app.storage.excluded_folder_rules.load", return_value=folders))
        scan = self.excluded_scan(root, folders)
        task = self.start(root)
        try:
            await self.settle(lambda: scan.call_count >= 1
                              and watch_plan.registered_directories(VAULT_ID) == {"backups"})
            scans = scan.call_count
            load.return_value = rules()
            released = watch_plan.release_excluded(VAULT_ID, rules().covers_directory)
            self.assertEqual(released, {"backups"})
            # The watcher registers the folder again and pairs that with a scan.
            await self.settle(lambda: watch_plan.registered_directories(VAULT_ID) == set())
            await self.settle(lambda: scan.call_count > scans)
            (root / "backups" / "now.zip").write_text("x")
            await self.settle(lambda: "backups/now.zip" in observed)
        finally:
            await self.stop(task)


class RequestFullScanTests(unittest.TestCase):
    def test_requests_while_the_vault_is_busy_share_one_scan_after_it(self):
        calls: list[int] = []

        def scan(vault, *, _on_start=None):
            calls.append(1)
            if _on_start:
                _on_start()
            return {"local": 1}

        self.addCleanup(storage.runtime_status.pop, 6882, None)
        lock = storage.scan_lock_for_vault(6882)
        with patch("app.storage.scan_vault", side_effect=scan), \
                patch("app.storage.QUEUED_SCAN_POLL_SECONDS", 0.01):
            with lock.holding("full_scan"):
                future = storage.queue_scan({"id": 6882})
                storage.request_full_scan({"id": 6882})  # Already requested: one scan.
                time.sleep(0.05)
                self.assertEqual(calls, [])
            self.assertEqual(future.result(timeout=5), {"local": 1})
        self.assertEqual(len(calls), 1)
        self.assertNotIn(6882, storage._queued_scans)


if __name__ == "__main__":
    unittest.main()
