"""What a Vault watcher cannot observe, and how to watch everything else.

A directory the process may not enter, or whose name is not valid UTF-8, cannot
be registered with the native observer. Registering a whole tree recursively
stops at the first such directory (which one comes first depends on the order
the filesystem lists names), so its readable siblings would silently go
unwatched. The scan therefore records which directories are unobservable, and
the watcher registers the readable part of the tree around them explicitly:

* every ancestor of an unobservable directory is watched without recursion
  (its own entries and the appearance of new subfolders);
* every other folder next to those ancestors is watched recursively.

When the set changes (a permission was fixed, a name was repaired, a new folder
turned out unreadable) the watcher registers the tree again.
"""
from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .fs_preflight import inspect_entry, is_storable_name


@dataclass(frozen=True)
class UnobservedPaths:
    """Relative POSIX paths the last scan could not observe."""

    unreadable: frozenset[str] = frozenset()
    unsupported: frozenset[str] = frozenset()
    # Subset of the two sets above that are directories.
    directories: frozenset[str] = frozenset()

    def under(self, prefix: str) -> "UnobservedPaths":
        def inside(path: str) -> bool:
            return path == prefix or path.startswith(prefix + "/")
        return UnobservedPaths(
            frozenset(p for p in self.unreadable if inside(p)),
            frozenset(p for p in self.unsupported if inside(p)),
            frozenset(p for p in self.directories if inside(p)),
        )

    def without(self, prefix: str) -> "UnobservedPaths":
        def outside(path: str) -> bool:
            return not (path == prefix or path.startswith(prefix + "/"))
        return UnobservedPaths(
            frozenset(p for p in self.unreadable if outside(p)),
            frozenset(p for p in self.unsupported if outside(p)),
            frozenset(p for p in self.directories if outside(p)),
        )

    def merged(self, other: "UnobservedPaths") -> "UnobservedPaths":
        return UnobservedPaths(
            self.unreadable | other.unreadable,
            self.unsupported | other.unsupported,
            self.directories | other.directories,
        )

    def __bool__(self) -> bool:
        return bool(self.unreadable or self.unsupported)


_state: dict[int, UnobservedPaths] = {}
_lock = threading.Lock()


def unobserved(vault_id: int) -> UnobservedPaths:
    with _lock:
        return _state.get(int(vault_id), UnobservedPaths())


def record_full_scan(vault_id: int, found: UnobservedPaths) -> None:
    """A walk of the whole Vault is the complete picture: it replaces the old one."""
    with _lock:
        _state[int(vault_id)] = found


def record_subtree_scan(vault_id: int, subtree: str, found: UnobservedPaths) -> None:
    """A partial walk only speaks for its own subtree."""
    with _lock:
        old = _state.get(int(vault_id), UnobservedPaths())
        _state[int(vault_id)] = old.without(subtree).merged(found)


def note_unreadable_directories(vault_id: int, directories: Iterable[str]) -> None:
    """A failed observer named folders it could not enter: avoid them now.

    The next scan still decides; this only spares the restart from failing on
    the same folder before that scan has run.
    """
    added = frozenset(directories)
    if not added:
        return
    with _lock:
        old = _state.get(int(vault_id), UnobservedPaths())
        _state[int(vault_id)] = old.merged(UnobservedPaths(added, frozenset(), added))


def forget(vault_id: int) -> None:
    with _lock:
        _state.pop(int(vault_id), None)


def watch_directories(root: Path, found: UnobservedPaths) -> tuple[list[Path], list[Path]]:
    """Return (non-recursive directories, recursive subtree roots) to register."""
    if not found.directories:
        return [], [root]
    ancestors: set[tuple[str, ...]] = {()}
    skipped: set[tuple[str, ...]] = set()
    for relative in found.directories:
        parts = PurePosixPath(relative).parts
        skipped.add(parts)
        for end in range(1, len(parts)):
            ancestors.add(parts[:end])
    flat: list[Path] = []
    recursive: list[Path] = []
    for ancestor in sorted(ancestors):
        directory = root.joinpath(*ancestor)
        try:
            with os.scandir(directory) as entries:
                children = [
                    entry.name for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                ]
        except OSError:
            # Gone or no longer readable: the next scan reports it.
            continue
        flat.append(directory)
        for name in sorted(children):
            child = ancestor + (name,)
            if child in ancestors or child in skipped or not is_storable_name(name):
                continue
            recursive.append(directory / name)
    return flat, recursive


def still_unobserved(root: Path, found: UnobservedPaths) -> bool:
    """Whether every recorded path is still a problem.

    This is the cheap re-check that decides if a full scan is worth running: it
    looks only at the reported paths. False when any of them was repaired,
    renamed away or removed.
    """
    for relative in found.unsupported:
        if not os.path.lexists(root / relative):
            return False
    for relative in found.unreadable:
        target = root / relative
        try:
            if relative in found.directories:
                with os.scandir(target):
                    pass
            else:
                inspect_entry(target)
        except PermissionError:
            continue
        except OSError:
            # Missing, replaced or otherwise changed: let a scan decide.
            return False
        else:
            return False
    return True


class MergedWatch:
    """One async iterator over several observers, with the same barrier.

    The first item is produced once every observer has yielded its first tick
    (the registration barrier of ``awatch(..., yield_on_timeout=True)``); after
    that, items come from whichever observer reports next.
    """

    def __init__(self, observers: Iterable[AsyncIterator[Any]]):
        self._observers = list(observers)
        self._queue: asyncio.Queue[tuple[int, Any, BaseException | None]] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []

    async def _pump(self, index: int, observer: AsyncIterator[Any]) -> None:
        try:
            async for changes in observer:
                await self._queue.put((index, changes, None))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # forwarded to the consumer
            await self._queue.put((index, None, exc))

    def __aiter__(self) -> "MergedWatch":
        return self

    async def _take(self) -> tuple[int, Any]:
        index, changes, error = await self._queue.get()
        if error is not None:
            raise error
        return index, changes

    async def __anext__(self) -> set[Any]:
        if not self._tasks:
            self._tasks = [
                asyncio.ensure_future(self._pump(index, observer))
                for index, observer in enumerate(self._observers)
            ]
            ready: set[int] = set()
            merged: set[Any] = set()
            while len(ready) < len(self._observers):
                index, changes = await self._take()
                ready.add(index)
                merged |= set(changes)
            return merged
        _, changes = await self._take()
        return set(changes)

    async def aclose(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for observer in self._observers:
            close = getattr(observer, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
