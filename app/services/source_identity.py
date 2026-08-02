"""Markerless Source Volume identity from Linux mount metadata (issue #151).

Only opaque SHA-256 fingerprints leave this module. Raw mount source and root
values are deliberately neither persisted nor logged.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

FINGERPRINT_VERSION = "linux-mountinfo-v1"
MOUNTINFO_PATH = Path("/proc/self/mountinfo")

# Virtual/container filesystems do not provide an operator-volume identity.
_UNSUPPORTED_FILESYSTEMS = {
    "autofs", "cgroup", "cgroup2", "configfs", "debugfs", "devpts",
    "devtmpfs", "efivarfs", "fusectl", "hugetlbfs", "mqueue", "overlay",
    "proc", "pstore", "ramfs", "securityfs", "sysfs", "tmpfs", "tracefs",
}
_PLACEHOLDER_FIELDS = {"", "-", "?", "none", "unknown"}


class MountIdentityError(ValueError):
    """Mount metadata cannot establish one supported identity."""


@dataclass(frozen=True)
class MountInfo:
    mount_id: int
    parent_id: int
    major_minor: str
    root: str
    mount_point: str
    mount_options: tuple[str, ...]
    optional_fields: tuple[str, ...]
    filesystem_type: str
    mount_source: str
    super_options: tuple[str, ...]


def _unescape_mountinfo(value: str) -> str:
    """Decode kernel mountinfo octal escapes, rejecting malformed escapes."""
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            result.append(value[index])
            index += 1
            continue
        digits = value[index + 1:index + 4]
        if len(digits) != 3 or any(char not in "01234567" for char in digits):
            raise MountIdentityError("malformed mountinfo escape")
        result.append(chr(int(digits, 8)))
        index += 4
    return "".join(result)


def parse_mountinfo(text: str) -> tuple[MountInfo, ...]:
    """Parse Linux ``/proc/*/mountinfo`` without lossy whitespace handling."""
    entries: list[MountInfo] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line:
            continue
        fields = raw_line.split(" ")
        if "" in fields:
            raise MountIdentityError(f"malformed mountinfo line {line_number}")
        separators = [index for index, value in enumerate(fields) if value == "-"]
        if len(separators) != 1:
            raise MountIdentityError(f"malformed mountinfo separator on line {line_number}")
        separator = separators[0]
        if separator < 6 or len(fields) < separator + 4:
            raise MountIdentityError(f"incomplete mountinfo line {line_number}")
        try:
            mount_id = int(fields[0])
            parent_id = int(fields[1])
        except ValueError as exc:
            raise MountIdentityError(
                f"invalid mountinfo identifier on line {line_number}"
            ) from exc
        if ":" not in fields[2]:
            raise MountIdentityError(f"invalid mountinfo device on line {line_number}")
        entries.append(
            MountInfo(
                mount_id=mount_id,
                parent_id=parent_id,
                major_minor=fields[2],
                root=_unescape_mountinfo(fields[3]),
                mount_point=_unescape_mountinfo(fields[4]),
                mount_options=tuple(
                    _unescape_mountinfo(value) for value in fields[5].split(",")
                ),
                optional_fields=tuple(
                    _unescape_mountinfo(value) for value in fields[6:separator]
                ),
                filesystem_type=_unescape_mountinfo(fields[separator + 1]),
                mount_source=_unescape_mountinfo(fields[separator + 2]),
                super_options=tuple(
                    _unescape_mountinfo(value)
                    for value in fields[separator + 3].split(",")
                ),
            )
        )
    return tuple(entries)


def read_mountinfo_text() -> str:
    return MOUNTINFO_PATH.read_text(encoding="utf-8")


def fingerprint_for_mount(target: str | Path, *, text: str | None = None) -> str:
    """Return an opaque stable fingerprint for exactly one mount at ``target``.

    Mount IDs, device numbers, optional fields and mount/super options are
    intentionally excluded because they can change during an ordinary remount.
    The target is normalized lexically: identity lookup must not resolve or
    traverse the Source Volume before the mount metadata gate has passed.
    """
    target_text = os.path.normpath(os.path.abspath(os.fspath(target)))
    try:
        entries = parse_mountinfo(read_mountinfo_text() if text is None else text)
    except (OSError, UnicodeError) as exc:
        raise MountIdentityError("Linux mount metadata is inaccessible") from exc
    matches = [entry for entry in entries if entry.mount_point == target_text]
    if not matches:
        raise MountIdentityError("mount metadata is absent")
    if len(matches) != 1:
        raise MountIdentityError("mount identity is ambiguous")
    entry = matches[0]
    filesystem_type = entry.filesystem_type.strip().lower()
    source = entry.mount_source
    root = entry.root
    if filesystem_type in _UNSUPPORTED_FILESYSTEMS:
        raise MountIdentityError("mount filesystem type is unsupported")
    if (
        filesystem_type in _PLACEHOLDER_FIELDS
        or source.strip().lower() in _PLACEHOLDER_FIELDS
        or root.strip().lower() in _PLACEHOLDER_FIELDS
    ):
        raise MountIdentityError("mount metadata is insufficient")
    payload = json.dumps(
        {
            "filesystem_type": filesystem_type,
            "mount_source": source,
            "root": root,
            "version": FINGERPRINT_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
