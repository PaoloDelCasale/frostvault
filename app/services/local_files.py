"""Local Copy inspection helpers with change detection around every read."""
from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def hash_stable_regular_file(path: Path) -> tuple[str, os.stat_result]:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("Local file is not a regular file")
    expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != expected:
            raise RuntimeError("Local file changed since fingerprinting")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) != expected:
        raise RuntimeError("Local file changed since fingerprinting")
    return digest.hexdigest(), after


def restore_claimed_local_copy(claimed: Path, target: Path) -> bool:
    try:
        claimed.lstat()
    except FileNotFoundError:
        return True
    try:
        os.link(claimed, target, follow_symlinks=False)
    except FileExistsError:
        return False
    except OSError:
        try:
            claimed.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Cleanup claim disappeared while restoring local content"
            ) from exc
        return False
    claimed.unlink()
    return True


def _require_linked_upload_digest(target: Mapping[str, Any]) -> str:
    """Return the durable snapshot fingerprint for a linked Archive Version."""
    integrity = str(target.get("integrity") or "unverified")
    upload_digest = str(
        target.get("upload_plaintext_sha256")
        or (target.get("version_sha256") if integrity == "verified" else "")
        or ""
    ).lower()
    if len(upload_digest) != 64:
        raise RuntimeError(
            "The linked Archive Version has no durable upload fingerprint"
        )
    try:
        int(upload_digest, 16)
    except ValueError as exc:
        raise RuntimeError(
            "The linked Archive Version has an invalid upload fingerprint"
        ) from exc
    return upload_digest


def _local_stat_unchanged(path: Path, previous: os.stat_result) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        current.st_size == previous.st_size
        and current.st_mtime_ns == previous.st_mtime_ns
        and current.st_dev == previous.st_dev
        and current.st_ino == previous.st_ino
    )
