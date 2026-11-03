"""Readability checks used by the real scan/watcher, never a diagnostic walk."""
from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path


class UnreadablePathsError(PermissionError):
    """The scan is incomplete, but its unreadable paths already have problems."""


def inspect_entry(path: Path) -> tuple[str, os.stat_result] | None:
    """Classify without following symlinks; prove regular files can be opened.

    This is not a content verification (upload still hashes and verifies the
    exact cloud version). A stat alone cannot detect an unreadable file.
    """
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return "symlink", info
    if stat.S_ISDIR(info.st_mode):
        return None
    if stat.S_ISREG(info.st_mode):
        # Never follow a symlink substituted between lstat and open.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev, opened.st_ino
            ) != (info.st_dev, info.st_ino):
                raise OSError("Local file changed during readability check")
            os.read(descriptor, 1)
        finally:
            os.close(descriptor)
        return "regular", info
    return "other", info


def walk_entries(
    root: Path, *, on_error: Callable[[Path, OSError], None],
) -> Iterator[Path]:
    """Walk once for cataloguing; report unreadable subtrees instead of hiding them."""
    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        raise PermissionError("Vault root is inaccessible")

    def failed(error: OSError) -> None:
        on_error(Path(error.filename) if error.filename else root, error)

    for directory, directories, files in os.walk(root, followlinks=False, onerror=failed):
        # Symlink directories are catalogued as unsupported entries, not followed.
        for name in directories + files:
            yield Path(directory) / name
