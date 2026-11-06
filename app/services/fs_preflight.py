"""Readability checks used by the real scan/watcher, never a diagnostic walk."""
from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


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


class TreeIdentityChanged(RuntimeError):
    """A directory was replaced while its observations were being staged."""


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def _open_directory(root_fd: int, parts: tuple[str, ...]):
    """Resolve every component relative to a pinned directory, never a symlink."""
    fd = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _directory_identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


class PinnedTree:
    """Descriptor-relative traversal with bounded descriptors (tree depth).

    Logical paths are for catalog keys only. All inspection uses the pinned
    parent descriptor, including the final O_NOFOLLOW readability open. Keep
    directory identities, not open descriptors, for the completion fence;
    replacement of a previously visited directory invalidates committed batches
    as well as the current one. The caller owns catalog compensation.
    """

    def __init__(self, root: Path, *, subtree: Path | None = None):
        self.root = root
        self.start = (subtree or root).relative_to(root).parts
        if ".." in self.start:
            raise TreeIdentityChanged("Subtree escapes the pinned root")
        self.identities: dict[tuple[str, ...], tuple[int, int]] = {}
        self.active: list[tuple[int, str, tuple[int, int]]] = []
        self.fd = -1

    def __enter__(self):
        try:
            # Full scans already pass an enrolled, verified procfs handle.
            if self.root.parent == Path("/proc/self/fd") and self.root.name.isdecimal():
                self.fd = os.dup(int(self.root.name))
            else:
                with _open_directory_from_path(self.root) as fd:
                    self.fd = os.dup(fd)
            if not os.access(f"/proc/self/fd/{self.fd}", os.R_OK | os.X_OK):
                raise PermissionError("Vault root is inaccessible")
            self.identities[()] = _directory_identity(self.fd)
            return self
        except OSError as exc:
            if self.fd >= 0:
                os.close(self.fd)
            raise TreeIdentityChanged("Scan root could not be pinned") from exc

    def __exit__(self, *_):
        os.close(self.fd)

    def validate_active(self) -> None:
        try:
            for parent, name, identity in self.active:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                    raise TreeIdentityChanged("Directory changed during traversal")
        except OSError as exc:
            raise TreeIdentityChanged("Directory disappeared during traversal") from exc

    def validate(self) -> None:
        """Fence all visited paths, including ancestors of the starting subtree."""
        try:
            with _open_directory_from_path(self.root) as current:
                if _directory_identity(current) != self.identities[()]:
                    raise TreeIdentityChanged("Scan root changed during traversal")
            for parts, identity in self.identities.items():
                with _open_directory(self.fd, parts) as current:
                    if _directory_identity(current) != identity:
                        raise TreeIdentityChanged("Visited directory changed during traversal")
        except OSError as exc:
            raise TreeIdentityChanged("Visited directory is no longer accessible") from exc

    def entries(self, *, on_error: Callable[[Path, OSError], None]) -> Iterator[tuple[Path, Path]]:
        def descend(fd: int, parts: tuple[str, ...]):
            logical = self.root.joinpath(*parts)
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        self.validate_active()
                        path = logical / entry.name
                        info = entry.stat(follow_symlinks=False)
                        # Only this descriptor path is ever passed to inspection.
                        yield path, Path(f"/proc/self/fd/{fd}") / entry.name
                        if not stat.S_ISDIR(info.st_mode):
                            continue
                        expected = info.st_dev, info.st_ino
                        try:
                            child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=fd)
                        except PermissionError as exc:
                            on_error(path, exc)
                            continue
                        except OSError as exc:
                            raise TreeIdentityChanged("Directory changed before descent") from exc
                        try:
                            if _directory_identity(child) != expected:
                                raise TreeIdentityChanged("Directory changed before descent")
                            self.identities[parts + (entry.name,)] = expected
                            self.active.append((fd, entry.name, expected))
                            try:
                                yield from descend(child, parts + (entry.name,))
                            finally:
                                self.active.pop()
                        finally:
                            os.close(child)
            except OSError as exc:
                on_error(logical, exc)

        def start(fd: int, parts: tuple[str, ...]):
            if len(parts) == len(self.start):
                yield from descend(fd, parts)
                return
            name = self.start[len(parts)]
            try:
                with _open_directory(fd, (name,)) as child:
                    expected = _directory_identity(child)
                    self.identities[parts + (name,)] = expected
                    self.active.append((fd, name, expected))
                    try:
                        yield from start(child, parts + (name,))
                    finally:
                        self.active.pop()
            except OSError as exc:
                raise TreeIdentityChanged("Subtree component could not be pinned") from exc

        yield from start(self.fd, ())


@contextmanager
def _open_directory_from_path(path: Path):
    if path.parent == Path("/proc/self/fd") and path.name.isdecimal():
        fd = os.dup(int(path.name))
        try:
            yield fd
        finally:
            os.close(fd)
    else:
        absolute = Path(os.path.abspath(path))
        fd = os.open(absolute.anchor, _DIRECTORY_FLAGS)
        try:
            with _open_directory(fd, absolute.parts[1:]) as opened:
                yield opened
        finally:
            os.close(fd)
