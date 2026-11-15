"""Readability checks used by the real scan/watcher, never a diagnostic walk."""
from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path


def is_storable_name(name: str) -> bool:
    """Whether a directory entry name can be catalogued as UTF-8 text.

    Linux names are bytes. Python decodes the undecodable ones with
    surrogateescape, and such a name can neither be stored in the catalog nor
    become a cloud key. It is reported and skipped, never allowed to abort a walk.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def removal_is_unproven(root: Path, path: Path) -> bool:
    """Whether a path that no longer exists may only have been renamed away.

    A rename to a name that is not valid UTF-8 (another client, a remount with
    another charset) looks like a removal: the new name is never catalogued.
    The nearest existing folder above the path is listed; if it holds such a
    name, or cannot be listed at all, the removal is not proven and the path
    must not become missing. The scan that follows decides, and it treats the
    same name as an incomplete observation (#494).
    """
    directory = path.parent
    while directory == root or root in directory.parents:
        try:
            names = os.listdir(directory)
        except (FileNotFoundError, NotADirectoryError):
            if directory == root:
                return True
            directory = directory.parent
            continue
        except OSError:
            return True
        return any(not is_storable_name(name) for name in names)
    return True


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
    """The scope that proves the whole walk changed: the root or a start folder.

    A folder below them that is replaced or removed during the walk does not
    raise: it is a :attr:`PinnedTree.replaced` subtree (#686).
    """


class _SubtreeReplaced(Exception):
    """Unwinds the walk out of one replaced folder, to its parent's listing."""

    def __init__(self, parts: tuple[str, ...]):
        super().__init__("/".join(parts))
        self.parts = parts


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


def _within(parts: tuple[str, ...], ancestor: tuple[str, ...]) -> bool:
    return parts[: len(ancestor)] == ancestor


class PinnedTree:
    """Descriptor-relative traversal with bounded descriptors (tree depth).

    Logical paths are for catalog keys only. All inspection uses the pinned
    parent descriptor, including the final O_NOFOLLOW readability open. Keep
    directory identities, not open descriptors, for the completion fence.

    Two scopes (#686). The root and the folders leading to a starting subtree
    prove the walk as a whole: their replacement raises TreeIdentityChanged.
    Any folder below them that is replaced, removed or made inaccessible during
    the walk only invalidates its own subtree: the walk leaves it, goes on with
    its siblings and lists it in :attr:`replaced` (topmost folders only). What
    was yielded below a replaced folder may describe the folder it replaced;
    the caller compensates those observations and walks the folder again. The
    caller owns catalog compensation.
    """

    def __init__(self, root: Path, *, subtree: Path | None = None, missing_ok: bool = False):
        self.root = root
        self.start = (subtree or root).relative_to(root).parts
        if ".." in self.start:
            raise TreeIdentityChanged("Subtree escapes the pinned root")
        # A re-walk of a replaced folder: a start folder that no longer exists
        # is an empty subtree, not a changed root.
        self.missing_ok = missing_ok
        self.identities: dict[tuple[str, ...], tuple[int, int]] = {}
        self.active: list[tuple[int, str, tuple[int, int], tuple[str, ...]]] = []
        self.replaced: list[tuple[str, ...]] = []
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

    def replaced_paths(self) -> list[str]:
        """The replaced subtrees, as root-relative POSIX paths."""
        return ["/".join(parts) for parts in self.replaced]

    def _mark_replaced(self, parts: tuple[str, ...]) -> None:
        if any(_within(parts, known) for known in self.replaced):
            return
        self.replaced = [known for known in self.replaced if not _within(known, parts)]
        self.replaced.append(parts)

    def _changed_active(self) -> int | None:
        """The index of the outermost folder on the current path that changed."""
        for index, (parent, name, identity, _parts) in enumerate(self.active):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                return index
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                return index
        return None

    def _start_changed(self, parts: tuple[str, ...]) -> TreeIdentityChanged:
        where = "/".join(parts) or "."
        return TreeIdentityChanged(f"Start folder changed during traversal: {where}")

    def validate_active(self) -> None:
        """Fence the current path; only a changed start folder raises.

        Used before a batch of observations is staged. A replaced folder below
        the start is left to the walk, which notices it at its next entry.
        """
        index = self._changed_active()
        if index is not None and index < len(self.start):
            raise self._start_changed(self.active[index][3])

    def _validate_active_in_walk(self) -> None:
        index = self._changed_active()
        if index is None:
            return
        parts = self.active[index][3]
        if index < len(self.start):
            raise self._start_changed(parts)
        raise _SubtreeReplaced(parts)

    def validate(self) -> None:
        """Fence all visited paths, including ancestors of the starting subtree.

        The root and the start folders raise; any other visited folder that
        changed becomes a replaced subtree.
        """
        try:
            with _open_directory_from_path(self.root) as current:
                if _directory_identity(current) != self.identities[()]:
                    raise TreeIdentityChanged("Scan root changed during traversal")
        except OSError as exc:
            raise TreeIdentityChanged("Scan root is no longer accessible") from exc
        for parts, identity in sorted(self.identities.items(), key=lambda item: len(item[0])):
            if not parts or any(_within(parts, known) for known in self.replaced):
                continue
            try:
                with _open_directory(self.fd, parts) as current:
                    changed = _directory_identity(current) != identity
            except OSError:
                changed = True
            if not changed:
                continue
            if len(parts) <= len(self.start):
                raise self._start_changed(parts)
            self._mark_replaced(parts)

    def entries(
        self, *, on_error: Callable[[Path, OSError], None],
        on_unsupported_name: Callable[[Path, bool], None] | None = None,
    ) -> Iterator[tuple[Path, Path]]:
        """Yield (logical path, descriptor path) for every storable entry.

        A name that is not valid UTF-8 is handed to ``on_unsupported_name``
        (with whether it is a directory) and skipped together with its subtree:
        no catalog row can exist beneath a path that cannot be stored, and the
        rest of the walk goes on. An entry that disappears between the listing
        and its stat is skipped: it no longer exists to be observed.
        """
        def descend(fd: int, parts: tuple[str, ...]):
            logical = self.root.joinpath(*parts)
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        self._validate_active_in_walk()
                        if not is_storable_name(entry.name):
                            if on_unsupported_name is not None:
                                on_unsupported_name(
                                    logical / entry.name,
                                    entry.is_dir(follow_symlinks=False),
                                )
                            continue
                        path = logical / entry.name
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except (FileNotFoundError, NotADirectoryError):
                            continue
                        # Only this descriptor path is ever passed to inspection.
                        yield path, Path(f"/proc/self/fd/{fd}") / entry.name
                        if not stat.S_ISDIR(info.st_mode):
                            continue
                        expected = info.st_dev, info.st_ino
                        child_parts = parts + (entry.name,)
                        try:
                            child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=fd)
                        except PermissionError as exc:
                            on_error(path, exc)
                            continue
                        except OSError:
                            # Removed, or replaced by a file or a symlink.
                            self._mark_replaced(child_parts)
                            continue
                        try:
                            if _directory_identity(child) != expected:
                                self._mark_replaced(child_parts)
                                continue
                            self.identities[child_parts] = expected
                            self.active.append((fd, entry.name, expected, child_parts))
                            try:
                                yield from descend(child, child_parts)
                            except _SubtreeReplaced as replaced:
                                if replaced.parts != child_parts:
                                    raise
                                self._mark_replaced(child_parts)
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
            with ExitStack() as stack:
                try:
                    child = stack.enter_context(_open_directory(fd, (name,)))
                except PermissionError as exc:
                    # A folder that appeared unreadable (created by another
                    # user) is a named, unreadable path, not a replaced tree.
                    on_error(self.root.joinpath(*parts, name), exc)
                    return
                except (FileNotFoundError, NotADirectoryError) as exc:
                    if self.missing_ok:
                        return
                    raise TreeIdentityChanged("Subtree component could not be pinned") from exc
                except OSError as exc:
                    raise TreeIdentityChanged("Subtree component could not be pinned") from exc
                expected = _directory_identity(child)
                self.identities[parts + (name,)] = expected
                self.active.append((fd, name, expected, parts + (name,)))
                try:
                    yield from start(child, parts + (name,))
                finally:
                    self.active.pop()

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
