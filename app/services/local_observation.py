"""Verified echoes of application-owned atomic filesystem mutations.

One mutation can emit several delayed watcher batches. Each echo must match the
exact post-mutation stat AND an independently re-read digest; the evidence is
retained only while both match. External replacements or writes invalidate it.
Coarse filesystem clocks make ctime alone insufficient. This bounded cache never
grants cloud verification or cleanup authority.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import os
import threading

from .local_files import hash_stable_regular_file

_lock = threading.Lock()
_echoes: OrderedDict[tuple[int, str], tuple[tuple[int, ...], str]] = OrderedDict()
_MAX_ECHOES = 4096


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def remember(vault_id: int, relative: str, info: os.stat_result, plaintext_sha256: str) -> None:
    """Remember the exact verified observation, never a later re-stat of it."""
    signature = _signature(info)
    with _lock:
        key = (int(vault_id), relative)
        _echoes[key] = (signature, plaintext_sha256)
        _echoes.move_to_end(key)
        while len(_echoes) > _MAX_ECHOES:
            _echoes.popitem(last=False)


def consume(vault_id: int, relative: str, path: Path) -> bool:
    """Consume a matching event, not the proof needed for later echo batches."""
    key = (int(vault_id), relative)
    with _lock:
        expected = _echoes.get(key)
    if expected is None:
        return False
    matches = False
    try:
        if _signature(path.lstat()) == expected[0]:
            digest, info = hash_stable_regular_file(path)
            matches = digest == expected[1] and _signature(info) == expected[0]
    except (OSError, RuntimeError):
        pass
    with _lock:
        # Never erase/approve evidence replaced while the independent read ran.
        if _echoes.get(key) != expected:
            return False
        if matches:
            _echoes.move_to_end(key)
        else:
            _echoes.pop(key)
    return matches
