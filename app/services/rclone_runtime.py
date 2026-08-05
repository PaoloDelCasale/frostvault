"""Ephemeral Rclone configuration for per-vault crypt remotes (issues #6, #201)."""
from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import os
import re
import secrets as token_secrets
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, IO, Iterator

try:  # FrostVault production deployments are Linux/POSIX containers.
    import fcntl
except ImportError:  # pragma: no cover - fallback files are unavailable without flock.
    fcntl = None  # type: ignore[assignment]

from ..config import settings
from .vault_crypto import (
    CryptSecrets,
    StoredCryptSecrets,
    decrypt_vault_secrets,
    obscure_for_rclone,
)

RUNTIME_REMOTE_NAME = "vault"
RUNTIME_DIRECTORY_NAME = "frostvault-rclone-runtime"
RUNTIME_RUN_DIRECTORY_PREFIX = "run-"
RUNTIME_RUN_MANIFEST_NAME = ".frostvault-runtime.json"
RUNTIME_CONFIG_FILENAME = "config.rclone.conf"
# Retained as public naming vocabulary for callers that inspect runtime storage.
RUNTIME_CONFIG_PREFIX = RUNTIME_RUN_DIRECTORY_PREFIX
RUNTIME_CONFIG_SUFFIX = ".rclone.conf"

_RUNTIME_RUN_KIND = "frostvault-rclone-runtime-run"
_RUNTIME_PROVENANCE_DOMAIN = b"frostvault/rclone-runtime/v1\0"
_RUNTIME_CONFIG_DOMAIN = b"frostvault/rclone-config/v1\0"
_RUNTIME_ROOT_MODE = 0o700
# Run directories are sealed while Rclone reads the config. Cleanup briefly
# widens the directory only after authenticating its provenance and lock.
_RUNTIME_RUN_MODE = 0o500
_RUNTIME_FILE_MODE = 0o600
_MAX_MANIFEST_BYTES = 16 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_RUN_NAME = re.compile(r"^run-[0-9]+-[0-9a-f]{32}$")


class RuntimeConfigStorageError(RuntimeError):
    """Protected storage for a generated Rclone configuration is unavailable."""


@dataclass(frozen=True)
class RuntimeRcloneConfig:
    path: Path
    remote_name: str
    config_text: str
    secrets: CryptSecrets | None


@dataclass(frozen=True)
class RuntimeConfigCleanupResult:
    """Non-secret cleanup outcome suitable for startup diagnostics."""

    removed: int = 0
    skipped_active: int = 0
    skipped_foreign: int = 0
    skipped_unsafe: int = 0
    skipped_raced: int = 0


@dataclass(frozen=True)
class _RuntimeRoot:
    path: Path
    descriptor: int
    key: bytes


@dataclass(frozen=True)
class _FallbackRuntimeConfig:
    run_name: str
    run_path: Path
    config_path: Path
    descriptor: int


def _read_base_section(base_remote: str) -> tuple[str, configparser.SectionProxy]:
    config_path = Path(settings.rclone_config)
    if not config_path.is_file():
        raise RuntimeError(f"Rclone configuration not found: {config_path}")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error as exc:
        raise RuntimeError(f"Invalid Rclone configuration: {exc}") from exc
    section = base_remote.strip().rstrip(":")
    if not section or not parser.has_section(section):
        raise RuntimeError(f"Rclone remote is not configured: {base_remote}")
    return section, parser[section]


def _section_as_text(name: str, section: configparser.SectionProxy) -> str:
    lines = [f"[{name}]"]
    for key, value in section.items():
        lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


def _crypt_target(vault: dict[str, Any]) -> str:
    bucket = str(vault["s3_bucket"]).strip().strip("/")
    prefix = str(vault["s3_prefix"]).strip().strip("/")
    base = str(vault["rclone_remote"]).strip().rstrip(":")
    if prefix:
        return f"{base}:{bucket}/{prefix}/"
    return f"{base}:{bucket}/"


def build_crypt_config_text(
    vault: dict[str, Any], secrets: CryptSecrets, *, base_section_text: str | None = None
) -> str:
    base_name = str(vault["rclone_remote"]).strip().rstrip(":")
    if base_section_text is None:
        section_name, section = _read_base_section(base_name)
        base_section_text = _section_as_text(section_name, section)
    crypt = (
        f"[{RUNTIME_REMOTE_NAME}]\n"
        "type = crypt\n"
        f"remote = {_crypt_target(vault)}\n"
        "filename_encryption = standard\n"
        "directory_name_encryption = true\n"
        f"password = {obscure_for_rclone(secrets.password)}\n"
        f"password2 = {obscure_for_rclone(secrets.password2)}\n"
    )
    return base_section_text.rstrip() + "\n\n" + crypt


def secrets_for_vault(vault: dict[str, Any]) -> CryptSecrets:
    return decrypt_vault_secrets(
        StoredCryptSecrets(
            password_ciphertext=vault["crypt_password_ciphertext"],
            password2_ciphertext=vault["crypt_password2_ciphertext"],
        )
    )


def _runtime_root_path() -> Path:
    return Path(tempfile.gettempdir()) / RUNTIME_DIRECTORY_NAME


def _provenance_key() -> bytes | None:
    """Return a process-local signing key without serializing or exposing it."""
    master_key = getattr(settings, "archive_master_key", "")
    if not isinstance(master_key, str):
        return None
    normalized = master_key.strip()
    return normalized.encode("utf-8") if normalized else None


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _is_owned_directory(info: os.stat_result, mode: int) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == mode
    )


def _is_owned_regular(info: os.stat_result, mode: int = _RUNTIME_FILE_MODE) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == mode
        and info.st_nlink == 1
    )


def _lstat_at(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None


def _no_follow_flag() -> int | None:
    return getattr(os, "O_NOFOLLOW", None)


def _open_at(directory_fd: int, name: str, *, directory: bool) -> int | None:
    no_follow = _no_follow_flag()
    if no_follow is None:
        return None
    flags = os.O_RDONLY | no_follow
    if directory:
        flags |= os.O_DIRECTORY
    try:
        return os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        return None


def _entry_matches_descriptor(
    directory_fd: int,
    name: str,
    descriptor: int,
    *,
    directory: bool,
    mode: int,
) -> bool:
    """Require the current no-follow name and open descriptor to share dev+ino."""
    named = _lstat_at(directory_fd, name)
    if named is None:
        return False
    try:
        opened = os.fstat(descriptor)
    except OSError:
        return False
    expected = _is_owned_directory if directory else _is_owned_regular
    return (
        expected(named, mode)
        and expected(opened, mode)
        and _same_identity(named, opened)
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("Unable to write protected rclone runtime storage")
        offset += written


def _read_limited(descriptor: int, limit: int) -> bytes | None:
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
            return None
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if not _same_identity(before, after) or before.st_size != after.st_size:
            return None
        return b"".join(chunks)
    except OSError:
        return None


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _signature(payload: dict[str, Any], key: bytes) -> str:
    return hmac.new(
        key,
        _RUNTIME_PROVENANCE_DOMAIN + _canonical_payload(payload),
        hashlib.sha256,
    ).hexdigest()


def _config_hmac(contents: bytes, key: bytes) -> str:
    return hmac.new(
        key,
        _RUNTIME_CONFIG_DOMAIN + contents,
        hashlib.sha256,
    ).hexdigest()


def _write_manifest(
    directory_fd: int,
    payload: dict[str, Any],
    key: bytes,
) -> None:
    no_follow = _no_follow_flag()
    if no_follow is None:
        raise RuntimeConfigStorageError("Protected rclone runtime storage is unavailable")
    document = _canonical_payload(
        {"payload": payload, "signature": _signature(payload, key)}
    )
    descriptor = -1
    try:
        descriptor = os.open(
            RUNTIME_RUN_MANIFEST_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
            _RUNTIME_FILE_MODE,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, _RUNTIME_FILE_MODE)
        _write_all(descriptor, document)
        if not _is_owned_regular(os.fstat(descriptor)):
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )
    except RuntimeConfigStorageError:
        raise
    except OSError as exc:
        raise RuntimeConfigStorageError(
            "Protected rclone runtime storage is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_manifest(directory_fd: int, key: bytes) -> tuple[dict[str, Any], int] | None:
    named = _lstat_at(directory_fd, RUNTIME_RUN_MANIFEST_NAME)
    if named is None or not _is_owned_regular(named):
        return None
    descriptor = _open_at(directory_fd, RUNTIME_RUN_MANIFEST_NAME, directory=False)
    if descriptor is None:
        return None
    try:
        if not _entry_matches_descriptor(
            directory_fd,
            RUNTIME_RUN_MANIFEST_NAME,
            descriptor,
            directory=False,
            mode=_RUNTIME_FILE_MODE,
        ):
            os.close(descriptor)
            return None
        raw = _read_limited(descriptor, _MAX_MANIFEST_BYTES)
        if raw is None:
            os.close(descriptor)
            return None
        document = json.loads(raw.decode("ascii"))
        payload = document.get("payload") if isinstance(document, dict) else None
        signature = document.get("signature") if isinstance(document, dict) else None
        if (
            not isinstance(payload, dict)
            or not isinstance(signature, str)
            or payload.get("kind") != _RUNTIME_RUN_KIND
            or payload.get("version") != 1
            or not hmac.compare_digest(signature, _signature(payload, key))
        ):
            os.close(descriptor)
            return None
        return payload, descriptor
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        return None


def _lock(descriptor: int, *, nonblocking: bool) -> bool:
    if fcntl is None:
        return False
    flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
    try:
        fcntl.flock(descriptor, flags)
    except OSError:
        return False
    return True


def _open_runtime_root(
    *,
    create: bool,
    require_key: bool,
) -> tuple[_RuntimeRoot | None, str | None]:
    """Open a private root by fd; never repair or follow an existing one."""
    path = _runtime_root_path()
    created = False
    try:
        named = path.lstat()
    except FileNotFoundError:
        if not create:
            return None, None
        try:
            path.mkdir(mode=_RUNTIME_ROOT_MODE)
            created = True
            named = path.lstat()
        except OSError:
            return None, "unsafe"
    except OSError:
        return None, "unsafe"

    if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
        return None, "foreign"
    no_follow = _no_follow_flag()
    if no_follow is None or fcntl is None:
        return None, "unsafe"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | no_follow)
    except OSError:
        return None, "unsafe"

    keep_descriptor = False
    try:
        opened = os.fstat(descriptor)
        if not _same_identity(named, opened):
            return None, "raced"
        if created:
            if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
                return None, "unsafe"
            os.fchmod(descriptor, _RUNTIME_ROOT_MODE)
            opened = os.fstat(descriptor)
        if not _is_owned_directory(opened, _RUNTIME_ROOT_MODE):
            return None, "foreign"
        key = _provenance_key() if require_key else b""
        if require_key and key is None:
            return None, "unsafe"
        if not _lock(descriptor, nonblocking=False):
            return None, "unsafe"
        # Compare immediately after serializing with other FrostVault cleaners.
        current = path.lstat()
        if not _same_identity(current, os.fstat(descriptor)):
            return None, "raced"
        keep_descriptor = True
        return _RuntimeRoot(path=path, descriptor=descriptor, key=key or b""), None
    except OSError:
        return None, "unsafe"
    finally:
        if not keep_descriptor:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _close_root(root: _RuntimeRoot | None) -> None:
    if root is not None:
        try:
            os.close(root.descriptor)
        except OSError:
            pass


def runtime_config_directory() -> Path:
    """Create and return the protected root for named fallback configs."""
    root, outcome = _open_runtime_root(create=True, require_key=False)
    if root is None:
        raise RuntimeConfigStorageError("Protected rclone runtime storage is unavailable")
    try:
        return root.path
    finally:
        _close_root(root)


def _new_run_name() -> str:
    return f"{RUNTIME_RUN_DIRECTORY_PREFIX}{os.getpid()}-{token_secrets.token_hex(16)}"


def _run_name_is_valid(name: str) -> bool:
    return bool(_RUN_NAME.fullmatch(name))


def _create_protected_runtime_config(config_text: str) -> _FallbackRuntimeConfig:
    """Create a signed private run directory and keep its config flock active."""
    root, outcome = _open_runtime_root(create=True, require_key=True)
    if root is None:
        raise RuntimeConfigStorageError("Protected rclone runtime storage is unavailable")
    run_fd = -1
    config_fd = -1
    run_name: str | None = None
    complete = False
    try:
        for _ in range(8):
            candidate = _new_run_name()
            try:
                os.mkdir(candidate, _RUNTIME_ROOT_MODE, dir_fd=root.descriptor)
            except FileExistsError:
                continue
            run_name = candidate
            break
        if run_name is None:
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )
        run_fd = _open_at(root.descriptor, run_name, directory=True) or -1
        if run_fd < 0:
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )
        os.fchmod(run_fd, _RUNTIME_ROOT_MODE)
        run_stat = os.fstat(run_fd)
        if not _is_owned_directory(run_stat, _RUNTIME_ROOT_MODE):
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )

        no_follow = _no_follow_flag()
        if no_follow is None:
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )
        config_fd = os.open(
            RUNTIME_CONFIG_FILENAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | no_follow,
            _RUNTIME_FILE_MODE,
            dir_fd=run_fd,
        )
        os.fchmod(config_fd, _RUNTIME_FILE_MODE)
        if not _lock(config_fd, nonblocking=False):
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )
        config_bytes = config_text.encode("utf-8")
        _write_all(config_fd, config_bytes)
        config_stat = os.fstat(config_fd)
        if not _is_owned_regular(config_stat):
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )
        payload = {
            "kind": _RUNTIME_RUN_KIND,
            "version": 1,
            "run_name": run_name,
            "run_device": run_stat.st_dev,
            "run_inode": run_stat.st_ino,
            "owner_uid": run_stat.st_uid,
            "run_mode": _RUNTIME_RUN_MODE,
            "config_name": RUNTIME_CONFIG_FILENAME,
            "config_device": config_stat.st_dev,
            "config_inode": config_stat.st_ino,
            "config_mode": _RUNTIME_FILE_MODE,
            "config_hmac": _config_hmac(config_bytes, root.key),
        }
        _write_manifest(run_fd, payload, root.key)
        os.fchmod(run_fd, _RUNTIME_RUN_MODE)
        if not _is_owned_directory(os.fstat(run_fd), _RUNTIME_RUN_MODE):
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )
        complete = True
        return _FallbackRuntimeConfig(
            run_name=run_name,
            run_path=root.path / run_name,
            config_path=root.path / run_name / RUNTIME_CONFIG_FILENAME,
            descriptor=config_fd,
        )
    except RuntimeConfigStorageError:
        raise
    except OSError as exc:
        raise RuntimeConfigStorageError(
            "Protected rclone runtime storage is unavailable"
        ) from exc
    finally:
        if not complete and config_fd >= 0:
            # An incomplete run has no trustworthy cleanup provenance. Retain it
            # rather than guessing which matching entry is safe to remove.
            try:
                os.close(config_fd)
            except OSError:
                pass
        if run_fd >= 0:
            try:
                os.close(run_fd)
            except OSError:
                pass
        _close_root(root)


def _validated_run(
    root: _RuntimeRoot,
    run_name: str,
    run_fd: int,
) -> tuple[dict[str, Any], int, int] | None:
    try:
        run_stat = os.fstat(run_fd)
    except OSError:
        return None
    if not _is_owned_directory(run_stat, _RUNTIME_RUN_MODE):
        return None
    manifest = _read_manifest(run_fd, root.key)
    if manifest is None:
        return None
    payload, manifest_fd = manifest
    required = (
        payload.get("run_name") == run_name
        and payload.get("run_device") == run_stat.st_dev
        and payload.get("run_inode") == run_stat.st_ino
        and payload.get("owner_uid") == run_stat.st_uid
        and payload.get("run_mode") == _RUNTIME_RUN_MODE
        and payload.get("config_name") == RUNTIME_CONFIG_FILENAME
        and payload.get("config_mode") == _RUNTIME_FILE_MODE
        and isinstance(payload.get("config_hmac"), str)
    )
    if not required:
        os.close(manifest_fd)
        return None
    named_config = _lstat_at(run_fd, RUNTIME_CONFIG_FILENAME)
    if named_config is None or not _is_owned_regular(named_config):
        os.close(manifest_fd)
        return None
    config_fd = _open_at(run_fd, RUNTIME_CONFIG_FILENAME, directory=False)
    if config_fd is None:
        os.close(manifest_fd)
        return None
    if not _entry_matches_descriptor(
        run_fd,
        RUNTIME_CONFIG_FILENAME,
        config_fd,
        directory=False,
        mode=_RUNTIME_FILE_MODE,
    ):
        os.close(config_fd)
        os.close(manifest_fd)
        return None
    try:
        config_stat = os.fstat(config_fd)
    except OSError:
        os.close(config_fd)
        os.close(manifest_fd)
        return None
    if (
        payload.get("config_device") != config_stat.st_dev
        or payload.get("config_inode") != config_stat.st_ino
    ):
        os.close(config_fd)
        os.close(manifest_fd)
        return None
    return payload, manifest_fd, config_fd


def _restore_sealed_mode(root: _RuntimeRoot, run_name: str, run_fd: int) -> None:
    if _entry_matches_descriptor(
        root.descriptor,
        run_name,
        run_fd,
        directory=True,
        mode=_RUNTIME_ROOT_MODE,
    ):
        try:
            os.fchmod(run_fd, _RUNTIME_RUN_MODE)
        except OSError:
            pass


def _cleanup_run(root: _RuntimeRoot, run_name: str) -> str:
    """Clean one authenticated inactive run, or return a non-secret outcome."""
    named_run = _lstat_at(root.descriptor, run_name)
    if named_run is None:
        return "raced"
    if stat.S_ISLNK(named_run.st_mode) or not _is_owned_directory(
        named_run, _RUNTIME_RUN_MODE
    ):
        return "foreign"
    run_fd = _open_at(root.descriptor, run_name, directory=True)
    if run_fd is None:
        return "raced"
    manifest_fd = -1
    config_fd = -1
    widened = False
    removed = False
    try:
        if not _same_identity(named_run, os.fstat(run_fd)):
            return "raced"
        validated = _validated_run(root, run_name, run_fd)
        if validated is None:
            return "foreign"
        payload, manifest_fd, config_fd = validated
        if not _lock(config_fd, nonblocking=True):
            return "active"
        contents = _read_limited(config_fd, _MAX_CONFIG_BYTES)
        if contents is None:
            return "unsafe"
        digest = _config_hmac(contents, root.key)
        if not hmac.compare_digest(payload["config_hmac"], digest):
            return "foreign"

        # The root flock serializes FrostVault cleaners. Every destructive
        # operation below is descriptor-relative and preceded by a final
        # lstat/fstat dev+ino comparison, so a seen replacement is retained.
        if not _entry_matches_descriptor(
            root.descriptor,
            run_name,
            run_fd,
            directory=True,
            mode=_RUNTIME_RUN_MODE,
        ) or not _entry_matches_descriptor(
            run_fd,
            RUNTIME_CONFIG_FILENAME,
            config_fd,
            directory=False,
            mode=_RUNTIME_FILE_MODE,
        ) or not _entry_matches_descriptor(
            run_fd,
            RUNTIME_RUN_MANIFEST_NAME,
            manifest_fd,
            directory=False,
            mode=_RUNTIME_FILE_MODE,
        ):
            return "raced"

        os.fchmod(run_fd, _RUNTIME_ROOT_MODE)
        widened = True
        if not _entry_matches_descriptor(
            root.descriptor,
            run_name,
            run_fd,
            directory=True,
            mode=_RUNTIME_ROOT_MODE,
        ) or not _entry_matches_descriptor(
            run_fd,
            RUNTIME_CONFIG_FILENAME,
            config_fd,
            directory=False,
            mode=_RUNTIME_FILE_MODE,
        ) or not _entry_matches_descriptor(
            run_fd,
            RUNTIME_RUN_MANIFEST_NAME,
            manifest_fd,
            directory=False,
            mode=_RUNTIME_FILE_MODE,
        ):
            return "raced"

        os.unlink(RUNTIME_CONFIG_FILENAME, dir_fd=run_fd)
        os.unlink(RUNTIME_RUN_MANIFEST_NAME, dir_fd=run_fd)
        if not _entry_matches_descriptor(
            root.descriptor,
            run_name,
            run_fd,
            directory=True,
            mode=_RUNTIME_ROOT_MODE,
        ):
            return "raced"
        os.rmdir(run_name, dir_fd=root.descriptor)
        removed = True
        return "removed"
    except OSError:
        return "unsafe"
    finally:
        if not removed and widened:
            _restore_sealed_mode(root, run_name, run_fd)
        for descriptor in (config_fd, manifest_fd, run_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _with_outcome(result: RuntimeConfigCleanupResult, outcome: str) -> RuntimeConfigCleanupResult:
    field = {
        "removed": "removed",
        "active": "skipped_active",
        "foreign": "skipped_foreign",
        "unsafe": "skipped_unsafe",
        "raced": "skipped_raced",
    }.get(outcome, "skipped_unsafe")
    return replace(result, **{field: getattr(result, field) + 1})


def cleanup_runtime_configs() -> RuntimeConfigCleanupResult:
    """Remove only signed, inactive fallback residues.

    Unknown entries, symlinks, ownership/mode mismatches, active locks, and
    races are retained and returned as non-secret diagnostic counters.
    """
    root, outcome = _open_runtime_root(create=False, require_key=True)
    if root is None:
        return RuntimeConfigCleanupResult() if outcome is None else _with_outcome(
            RuntimeConfigCleanupResult(), outcome
        )
    try:
        try:
            names = tuple(os.listdir(root.descriptor))
        except OSError:
            return _with_outcome(RuntimeConfigCleanupResult(), "unsafe")
        result = RuntimeConfigCleanupResult()
        for name in names:
            if not _run_name_is_valid(name):
                result = _with_outcome(result, "foreign")
            else:
                result = _with_outcome(result, _cleanup_run(root, name))
        return result
    finally:
        _close_root(root)


def _cleanup_one_runtime_run(run_name: str) -> RuntimeConfigCleanupResult:
    root, outcome = _open_runtime_root(create=False, require_key=True)
    if root is None:
        return _with_outcome(RuntimeConfigCleanupResult(), outcome or "unsafe")
    try:
        return _with_outcome(RuntimeConfigCleanupResult(), _cleanup_run(root, run_name))
    finally:
        _close_root(root)


def _anonymous_runtime_config(config_text: str) -> tuple[IO[str], Path] | None:
    """Return an unlinked config descriptor and a child-readable procfs path.

    Rclone receives ``/proc/<parent-pid>/fd/<fd>`` and opens the parent's live
    descriptor. The descriptor is closed on context exit and therefore cannot
    survive cancellation, process termination, or restart.
    """
    handle: IO[str] | None = None
    try:
        if not Path("/proc/self/fd").is_dir():
            return None
        handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        handle.write(config_text)
        handle.flush()
        path = Path("/proc") / str(os.getpid()) / "fd" / str(handle.fileno())
        # Verify that an independently opened child process can read this path
        # before advertising it to rclone. Do not read the secret-bearing body.
        with path.open("rb"):
            pass
        return handle, path
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return None


@contextmanager
def _protected_runtime_config(config_text: str) -> Iterator[Path]:
    """Yield a signed fallback config and remove only its verified residue."""
    fallback = _create_protected_runtime_config(config_text)
    try:
        yield fallback.config_path
    finally:
        try:
            os.close(fallback.descriptor)
        except OSError:
            pass
        result = _cleanup_one_runtime_run(fallback.run_name)
        if result.removed != 1:
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage cleanup deferred"
            )


@contextmanager
def vault_rclone_config(vault: dict[str, Any]) -> Iterator[RuntimeRcloneConfig]:
    """Yield an ephemeral per-vault Rclone config without persistent residue."""
    if vault.get("encryption_mode") != "crypt":
        raise RuntimeError("Runtime crypt configuration requires encryption_mode=crypt")
    secrets = secrets_for_vault(vault)
    config_text = build_crypt_config_text(vault, secrets)

    anonymous = _anonymous_runtime_config(config_text)
    if anonymous is not None:
        handle, path = anonymous
        try:
            yield RuntimeRcloneConfig(
                path=path,
                remote_name=RUNTIME_REMOTE_NAME,
                config_text=config_text,
                secrets=secrets,
            )
        finally:
            try:
                handle.close()
            except OSError as exc:
                raise RuntimeConfigStorageError(
                    "Protected rclone runtime storage cleanup failed"
                ) from exc
        return

    with _protected_runtime_config(config_text) as path:
        yield RuntimeRcloneConfig(
            path=path,
            remote_name=RUNTIME_REMOTE_NAME,
            config_text=config_text,
            secrets=secrets,
        )


def encode_object_relative_path(
    runtime: RuntimeRcloneConfig, logical_path: str
) -> str:
    """Return the encrypted relative object path for a logical vault path."""
    completed = subprocess.run(
        [
            "rclone",
            "--config",
            str(runtime.path),
            "backend",
            "encode",
            f"{runtime.remote_name}:",
            logical_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "rclone encode failed").strip()
        raise RuntimeError(message[-1500:])
    encoded = completed.stdout.strip()
    if not encoded:
        raise RuntimeError("rclone encode returned an empty path")
    return encoded


def decode_object_relative_path(
    runtime: RuntimeRcloneConfig, encrypted_relative: str
) -> str:
    completed = subprocess.run(
        [
            "rclone",
            "--config",
            str(runtime.path),
            "backend",
            "decode",
            f"{runtime.remote_name}:",
            encrypted_relative,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "rclone decode failed").strip()
        raise RuntimeError(message[-1500:])
    decoded = completed.stdout.strip()
    if not decoded:
        raise RuntimeError("rclone decode returned an empty path")
    return decoded
