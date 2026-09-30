"""Logical-path, local-path and cloud-key mapping for Vault Files.

A logical path is Vault-relative and POSIX; the local path is confined to the
Vault root; the cloud key adds the S3 prefix and, for crypt Vaults, the
encrypted name or ``.bin`` suffix.  Temporary restore/cleanup/verify names
are recognised here so scans and watchers can ignore them.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from .rclone_runtime import (
    decode_object_relative_path,
    encode_object_relative_path,
)


RESTORE_TEMPORARY_RE = re.compile(
    r"\..+\.restore-[0-9a-f]{32}\.tmp(?:\..+\.partial)?"
)


CLEANUP_TEMPORARY_RE = re.compile(r"\..+\.cleanup-[0-9a-f]{32}\.tmp")


VERIFY_TEMPORARY_RE = re.compile(r"\..+\.verify-[0-9a-f]{32}\.tmp")


class InvalidLogicalPath(ValueError):
    """A caller supplied a path that cannot be a Vault-relative logical path.

    This remains a ``ValueError`` subclass for direct-call compatibility, but
    the API maps this dedicated domain exception rather than catching every
    ``ValueError`` raised by application code.
    """

    message_key = "api.invalid_path"

    def __init__(self) -> None:
        super().__init__("Invalid path")


def safe_relative_path(value: str) -> PurePosixPath:
    """Normalize one non-empty, traversal-safe Vault-relative logical path."""
    if not isinstance(value, str) or "\x00" in value:
        raise InvalidLogicalPath()
    normalized = value.replace("\\", "/")
    # PurePosixPath intentionally does not treat a Windows drive prefix as an
    # absolute path. Reject it explicitly because callers may submit paths
    # produced on another platform.
    if re.match(r"^[A-Za-z]:/", normalized):
        raise InvalidLogicalPath()
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise InvalidLogicalPath()
    return candidate


def safe_local_path(root_value: str, logical_path: str) -> Path:
    """Resolve a vault-relative path without following a final symbolic link."""
    root = Path(root_value).resolve()
    relative = safe_relative_path(logical_path)
    candidate = root.joinpath(*relative.parts)
    parent = candidate.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("Path is outside the allowed folder")
    if candidate.is_symlink():
        raise ValueError("Symbolic links are not allowed")
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Path is outside the allowed folder")
    return resolved


def safe_local_entry_path(root_value: str, logical_path: str) -> Path:
    """Return an on-disk entry without following a final symlink."""
    root = Path(root_value).resolve()
    relative = safe_relative_path(logical_path)
    candidate = root.joinpath(*relative.parts)
    parent = candidate.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("Path is outside the allowed folder")
    if candidate.is_symlink():
        raise ValueError("Symbolic links are not allowed")
    return candidate


_UNSET_DECODED_PATH = object()


def _cloud_relative_key(key: str, prefix_value: str) -> str | None:
    prefix = f"{prefix_value.strip('/')}/" if prefix_value.strip('/') else ""
    if prefix and not key.startswith(prefix):
        return None
    relative = key[len(prefix):]
    if not relative or relative.endswith('/'):
        return None
    return relative


def object_key_to_path(
    key: str,
    prefix_value: str,
    is_crypt: bool,
    *,
    encrypted_names: bool = False,
    runtime: Any | None = None,
    decoded_relative_path: str | None | object = _UNSET_DECODED_PATH,
) -> str | None:
    relative = _cloud_relative_key(key, prefix_value)
    if relative is None:
        return None
    if encrypted_names:
        if runtime is None:
            return None
        if decoded_relative_path is _UNSET_DECODED_PATH:
            try:
                relative = decode_object_relative_path(runtime, relative)
            except RuntimeError:
                return None
        elif decoded_relative_path is None:
            # The batch decoder deliberately makes one bad key unknown rather
            # than allowing a partial/ambiguous plaintext path into the catalog.
            return None
        else:
            relative = str(decoded_relative_path)
    elif is_crypt:
        if not relative.endswith('.bin'):
            return None
        relative = relative[:-4]
    if not relative or relative.endswith('/'):
        return None
    try:
        return safe_relative_path(relative).as_posix()
    except ValueError:
        return None


def expected_cloud_key(
    logical_path: str,
    prefix_value: str,
    is_crypt: bool,
    *,
    encrypted_names: bool = False,
    runtime: Any | None = None,
) -> str:
    relative = safe_relative_path(logical_path).as_posix()
    if encrypted_names:
        if runtime is None:
            raise RuntimeError("Filename-encrypted keys require a runtime Rclone config")
        relative = encode_object_relative_path(runtime, relative)
    elif is_crypt:
        relative += ".bin"
    prefix = prefix_value.strip("/")
    return f"{prefix}/{relative}" if prefix else relative


def plain_rclone_destination(
    remote_name: str,
    s3_prefix: str,
    logical_path: str,
) -> str:
    """Build a bucket-rooted plain Rclone object spec including the vault prefix."""
    remote = remote_name.strip().rstrip(":")
    if not remote:
        raise ValueError("Rclone remote name is required")
    key = expected_cloud_key(logical_path, s3_prefix, is_crypt=False)
    return f"{remote}:{key}"


def is_restore_temporary_name(name: str) -> bool:
    return (
        RESTORE_TEMPORARY_RE.fullmatch(name) is not None
        or CLEANUP_TEMPORARY_RE.fullmatch(name) is not None
        or VERIFY_TEMPORARY_RE.fullmatch(name) is not None
    )
