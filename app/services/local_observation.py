"""One-shot echoes of application-owned atomic filesystem mutations.

The watcher still receives every ordinary user path. Only an exact post-mutation
stat AND independently re-read digest can consume an echo; external replacements
or writes invalidate it. Coarse filesystem clocks make ctime alone insufficient.
This cache never grants cloud verification or cleanup authority.
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
    with _lock:
        key = (int(vault_id), relative)
        if key not in _echoes:
            return False
        expected = _echoes.pop(key)
    try:
        if _signature(path.lstat()) != expected[0]:
            return False
        digest, info = hash_stable_regular_file(path)
        return digest == expected[1] and _signature(info) == expected[0]
    except (OSError, RuntimeError):
        return False
