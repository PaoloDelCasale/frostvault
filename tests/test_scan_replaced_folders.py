"""#686: a folder replaced during a scan invalidates only its own subtree.

Real local files, no database: the catalog and Problems are mocks.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import storage
from app.services.fs_preflight import PinnedTree, TreeIdentityChanged


def _tree(directory: str) -> Path:
    root = Path(directory) / "vault"
    for folder in ("library/2026", "upload/aa/07", "upload/bb"):
        (root / folder).mkdir(parents=True)
    (root / "library/2026/a.jpg").write_bytes(b"a")
    (root / "library/2026/b.jpg").write_bytes(b"b")
    (root / "upload/aa/07/new.jpg").write_bytes(b"n")
    (root / "upload/bb/other.jpg").write_bytes(b"o")
    (root / "top.txt").write_bytes(b"t")
    return root


def _replace(folder: Path) -> None:
    """What Immich does: an emptied folder goes away and is created again.

    The old folder is moved out of the Vault rather than removed, so its inode
    stays in use and the new folder cannot reuse it: a filesystem that reuses
    the inode makes the replacement undetectable, and so untestable.
    """
    for child in folder.iterdir():
        child.unlink()
    graveyard = next(parent for parent in folder.parents if parent.name == "vault").parent
    holding = Path(tempfile.mkdtemp(dir=graveyard, prefix="replaced-"))
    folder.rename(holding / folder.name)
    folder.mkdir()


def _relative(root: Path, paths) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in paths)


class PinnedTreeScopeTests(unittest.TestCase):
    def test_a_folder_replaced_while_it_is_walked_is_left_and_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            folder = root / "upload/aa/07"
            (folder / "second.jpg").write_bytes(b"s")
            seen = []
            with PinnedTree(root) as tree:
                for entry, _ in tree.entries(on_error=lambda *_: None):
                    seen.append(entry)
                    if entry.parent == folder and not tree.replaced:
                        _replace(folder)
                tree.validate()
                self.assertEqual(tree.replaced_paths(), ["upload/aa/07"])
            names = _relative(root, seen)
            # Everything outside the replaced folder was walked.
            for path in ("library/2026/a.jpg", "library/2026/b.jpg", "upload/bb/other.jpg", "top.txt"):
                self.assertIn(path, names)

    def test_a_visited_folder_recreated_after_its_walk_is_a_replaced_subtree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            with PinnedTree(root) as tree:
                list(tree.entries(on_error=lambda *_: None))
                _replace(root / "upload/aa/07")
                tree.validate()  # no TreeIdentityChanged
                self.assertEqual(tree.replaced_paths(), ["upload/aa/07"])

    def test_only_the_topmost_replaced_folder_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            with PinnedTree(root) as tree:
                list(tree.entries(on_error=lambda *_: None))
                (root / "upload/aa").rename(root / "moved")
                (root / "upload/aa/07").mkdir(parents=True)
                tree.validate()
                self.assertEqual(tree.replaced_paths(), ["upload/aa"])

    def test_a_vanished_folder_is_a_replaced_subtree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            with PinnedTree(root) as tree:
                list(tree.entries(on_error=lambda *_: None))
                (root / "upload/bb/other.jpg").unlink()
                (root / "upload/bb").rename(Path(directory) / "gone-bb")
                tree.validate()
                self.assertEqual(tree.replaced_paths(), ["upload/bb"])

    def test_a_replaced_root_still_invalidates_the_whole_walk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            with PinnedTree(root) as tree:
                list(tree.entries(on_error=lambda *_: None))
                root.rename(Path(directory) / "old")
                root.mkdir()
                with self.assertRaises(TreeIdentityChanged):
                    tree.validate()

    def test_a_replaced_start_folder_still_invalidates_a_subtree_walk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            with PinnedTree(root, subtree=root / "upload/aa") as tree:
                list(tree.entries(on_error=lambda *_: None))
                (root / "upload/aa").rename(root / "moved")
                (root / "upload/aa").mkdir()
                with self.assertRaises(TreeIdentityChanged) as raised:
                    tree.validate()
            self.assertIn("upload/aa", str(raised.exception))

    def test_a_missing_start_folder_is_empty_when_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _tree(directory)
            with PinnedTree(root, subtree=root / "upload/gone", missing_ok=True) as tree:
                self.assertEqual(list(tree.entries(on_error=lambda *_: None)), [])
                tree.validate()
                self.assertEqual(tree.replaced, [])


class ScanTreeReplacedFolderTests(unittest.TestCase):
    def scan(self, stack: ExitStack, root: Path, *, replace_times: int):
        connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None
        connection.execute.return_value.rowcount = 1
        local_copies = stack.enter_context(patch("app.storage.LocalCopies")).return_value
        observed: list[list[str]] = []
        local_copies.observe_local_copies_batch.side_effect = lambda **kw: observed.append([e[0] for e in kw["entries"]])
        problems: list[dict] = []
        stack.enter_context(patch("app.storage.report_problem", side_effect=lambda *a, **kw: problems.append(kw)))
        resolved: list[dict] = []
        stack.enter_context(patch("app.storage.resolve_stale_path_problems", side_effect=lambda *a, **kw: resolved.append(kw)))
        stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
            local_operations_allowed=True, volume_alias=None, volume_health="healthy",
        )))
        stack.enter_context(patch("app.storage.mark_path_dirty"))
        restored: list[str] = []
        real_restore = storage._restore_journal_entries

        def restore(connection, vault_id, scan_id, entries):
            restored.extend(entries)
            real_restore(connection, vault_id, scan_id, entries)
        stack.enter_context(patch("app.storage._restore_journal_entries", side_effect=restore))

        folder = root / "upload/aa/07"
        real_inspect = storage.inspect_entry
        remaining = [replace_times]

        def inspect(pinned, **kwargs):
            result = real_inspect(pinned, **kwargs)
            if pinned.name == "new.jpg" and remaining[0] > 0:
                remaining[0] -= 1
                _replace(folder)
                (folder / "new.jpg").write_bytes(b"again")
            return result
        stack.enter_context(patch("app.storage.inspect_entry", side_effect=inspect))
        journal: dict = {}
        with self.assertLogs("app.storage", level="WARNING") as logs:
            storage._scan_tree(
                connection, {"id": 7, "source_root": str(root)}, "2026-11-15T13:00:00+00:00",
                root=root, allow_chunk_commits=False, scan_journal=journal,
            )
        return SimpleNamespace(
            observed=observed, problems=problems, resolved=resolved, restored=restored,
            journal=journal, logs=logs.output, local_copies=local_copies,
        )

    def test_the_replaced_folder_is_compensated_and_walked_again_and_the_rest_stands(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = _tree(directory)
            result = self.scan(stack, root, replace_times=1)
        first, *again = result.observed
        self.assertIn("library/2026/a.jpg", first)
        self.assertIn("upload/aa/07/new.jpg", first)
        # Only the replaced folder's observations are undone.
        self.assertEqual(result.restored, ["upload/aa/07/new.jpg"])
        self.assertEqual(again, [["upload/aa/07/new.jpg"]])
        self.assertFalse(result.journal.get("incomplete", False))
        self.assertEqual(result.problems, [])
        # The full scan can still finalize missing copies.
        result.local_copies.mark_unseen_local_copies_missing.assert_called_once()
        self.assertEqual(len(result.logs), 1)
        self.assertIn("upload/aa/07", result.logs[0])

    def test_a_folder_that_keeps_changing_is_named_by_its_own_problem(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = _tree(directory)
            result = self.scan(stack, root, replace_times=storage.SCAN_REPLACED_FOLDER_REWALKS + 1)
        self.assertEqual(
            [(p["event"], p["path"]) for p in result.problems],
            [("folder_changed_during_scan", "upload/aa/07")],
        )
        # Its last walk was undone too: it keeps its last known state.
        self.assertTrue(all(path.startswith("upload/aa/07") for path in result.restored))
        self.assertTrue(result.journal["incomplete"])
        result.local_copies.mark_unseen_local_copies_missing.assert_not_called()
        folder_resolved = [r for r in result.resolved if r["event"] == "folder_changed_during_scan"]
        self.assertEqual(folder_resolved[0]["keep_paths"], {"upload/aa/07"})
        self.assertTrue(any("kept changing" in line and "upload/aa/07" in line for line in result.logs))

    def test_a_replaced_folder_without_a_journal_still_refuses_the_walk(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = _tree(directory)
            connection = stack.enter_context(patch("app.storage.db")).return_value.__enter__.return_value
            connection.execute.return_value.fetchone.return_value = None
            stack.enter_context(patch("app.storage.LocalCopies"))
            stack.enter_context(patch("app.storage.source_layout.vault_local_access", return_value=SimpleNamespace(
                local_operations_allowed=True, volume_alias=None, volume_health="healthy",
            )))
            real_inspect = storage.inspect_entry

            def inspect(pinned, **kwargs):
                result = real_inspect(pinned, **kwargs)
                if pinned.name == "new.jpg":
                    _replace(root / "upload/aa/07")
                return result
            stack.enter_context(patch("app.storage.inspect_entry", side_effect=inspect))
            with self.assertRaises(TreeIdentityChanged) as raised:
                storage._scan_tree(
                    connection, {"id": 7, "source_root": str(root)}, "scan",
                    root=root, allow_chunk_commits=False, scan_journal=None,
                )
        self.assertIn("upload/aa/07", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
