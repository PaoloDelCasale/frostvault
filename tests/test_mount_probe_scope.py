"""#753: path-walking mount checks decode mountinfo once per operation.

Pure filesystem seams; no database, S3 or credentials. ``read_mountinfo_text``
is replaced by a fixed mountinfo listing the temporary tree, so a nested
mount is simulated without privileges.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import source_directories, source_identity, source_layout, vault_relocation


class MountProbeScopeTests(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.volume = Path(tmp.name).resolve() / "photos"
        self.deep = self.volume / "a/b/c/d/e/f/g/h"
        self.deep.mkdir(parents=True)
        self.source = SimpleNamespace(path=str(self.volume))
        self.writable = patch.object(source_layout, "path_is_writable", return_value=True)
        self.writable.start()
        self.addCleanup(self.writable.stop)

    def mountinfo(self, *nested: Path) -> str:
        lines = ["21 1 0:20 / / rw - overlay overlay rw",
                 f"30 21 8:3 /photos {self.volume} rw - xfs /dev/sda3 rw"]
        lines += [f"{31 + i} 30 8:3 /photos/x{i} {path} rw - xfs /dev/sda3 rw"
                  for i, path in enumerate(nested)]
        return "\n".join(lines) + "\n"

    def reads(self, text: str):
        return patch.object(source_identity, "read_mountinfo_text", return_value=text)

    def test_the_scope_reads_once_and_a_new_scope_reads_again(self) -> None:
        with self.reads(self.mountinfo(self.volume / "a/b")) as spy:
            with source_layout.mount_points_once():
                self.assertTrue(source_layout.path_is_mount(self.volume / "a/b"))
                self.assertFalse(source_layout.path_is_mount(self.deep))
                self.assertTrue(source_layout.path_is_mount(self.volume))
            self.assertEqual(spy.call_count, 1)
            with source_layout.mount_points_once():
                source_layout.path_is_mount(self.volume)
            self.assertEqual(spy.call_count, 2)
            source_layout.path_is_mount(self.volume)
            source_layout.path_is_mount(self.volume)
            self.assertEqual(spy.call_count, 4, "outside a scope every probe reads")

    def test_unreadable_mountinfo_in_a_scope_fails_closed_and_is_read_once(self) -> None:
        with patch.object(source_identity, "read_mountinfo_text",
                          side_effect=PermissionError("denied")) as spy:
            with source_layout.mount_points_once():
                self.assertFalse(source_layout.path_is_mount(self.volume))
                self.assertFalse(source_layout.path_is_mount(self.deep))
        self.assertEqual(spy.call_count, 1)

    def test_adoption_reads_mountinfo_once_whatever_the_depth(self) -> None:
        with self.reads(self.mountinfo()) as spy:
            source_directories._assert_no_nested_mount(self.source, self.deep)
        self.assertEqual(spy.call_count, 1)
        with self.reads(self.mountinfo(self.volume / "a/b/c")) as spy:
            with self.assertRaises(source_directories.SourceDirectoryError) as caught:
                source_directories._assert_no_nested_mount(self.source, self.deep)
        self.assertEqual(caught.exception.reason, "nested_mount")
        self.assertEqual(spy.call_count, 1)

    def test_relocation_reads_mountinfo_once_whatever_the_depth(self) -> None:
        relative = str(self.deep.relative_to(self.volume))
        with self.reads(self.mountinfo()) as spy:
            self.assertEqual(vault_relocation._candidate(self.source, relative),
                             (relative, self.deep))
        self.assertEqual(spy.call_count, 1)
        with self.reads(self.mountinfo(self.volume / "a/b/c")) as spy:
            with self.assertRaises(vault_relocation.VaultRelocationError) as caught:
                vault_relocation._candidate(self.source, relative)
        self.assertEqual(caught.exception.reason, "different_volume")
        self.assertEqual(spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
