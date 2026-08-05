"""Ephemeral Rclone configuration for per-vault crypt remotes (issues #6, #201)."""
from __future__ import annotations

import configparser
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, IO, Iterator

try:  # FrostVault production deployments are Linux/POSIX containers.
    import fcntl
except ImportError:  # pragma: no cover - Linux provides fcntl.
    fcntl = None  # type: ignore[assignment]

from ..config import settings
from .vault_crypto import (
    CryptSecrets,
    StoredCryptSecrets,
    decrypt_vault_secrets,
    obscure_for_rclone,
)

RUNTIME_REMOTE_NAME = "vault"
# This is a legacy location from the short-lived named-file implementation.
# New runtime configurations never create it or any other secret-bearing name.
RUNTIME_DIRECTORY_NAME = "frostvault-rclone-runtime"
_LEGACY_RUNTIME_ROOT_MODE = 0o700
_RUNTIME_CONFIG_MODE = 0o600


class RuntimeConfigStorageError(RuntimeError):
    """Anonymous storage for a generated Rclone configuration is unavailable."""


@dataclass(frozen=True)
class RuntimeRcloneConfig:
    path: Path
    remote_name: str
    config_text: str
    secrets: CryptSecrets | None


@dataclass(frozen=True)
class RuntimeConfigCleanupResult:
    """Non-secret observation of retained legacy named runtime residue.

    ``removed`` remains for startup-call compatibility, but is always zero:
    named runtime configurations can no longer be deleted safely after a crash.
    """

    removed: int = 0
    skipped_active: int = 0
    skipped_foreign: int = 0
    skipped_unsafe: int = 0
    skipped_raced: int = 0


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


def _legacy_runtime_root() -> Path:
    return Path(tempfile.gettempdir()) / RUNTIME_DIRECTORY_NAME


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _is_expected_legacy_root(info: os.stat_result) -> bool:
    """Recognize only the private directory the retired fallback created."""
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == _LEGACY_RUNTIME_ROOT_MODE
    )


def _legacy_runtime_has_residue(directory_fd: int) -> bool | None:
    """Observe at most one legacy entry without opening, reading, or removing it."""
    try:
        with os.scandir(directory_fd) as entries:
            return next(entries, None) is not None
    except OSError:
        return None


def cleanup_runtime_configs() -> RuntimeConfigCleanupResult:
    """Report legacy named runtime residue without deleting any pathname.

    Old releases could leave a signed run directory behind after a crash.  A
    same-UID process can replace any of its names between validation and unlink,
    and POSIX has no portable inode-bound unlink operation.  Consequently this
    process performs a descriptor-pinned, read-only observation only.  New
    configurations use sealed anonymous memfds, so they need no startup cleanup.
    """
    root_path = _legacy_runtime_root()
    try:
        named_root = root_path.lstat()
    except FileNotFoundError:
        return RuntimeConfigCleanupResult()
    except OSError:
        return RuntimeConfigCleanupResult(skipped_unsafe=1)

    if stat.S_ISLNK(named_root.st_mode) or not _is_expected_legacy_root(named_root):
        return RuntimeConfigCleanupResult(skipped_foreign=1)

    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None:
        return RuntimeConfigCleanupResult(skipped_unsafe=1)

    try:
        directory_fd = os.open(
            root_path,
            os.O_RDONLY | no_follow | directory_flag,
        )
    except OSError:
        return RuntimeConfigCleanupResult(skipped_unsafe=1)

    try:
        opened_root = os.fstat(directory_fd)
        if not _same_identity(named_root, opened_root):
            return RuntimeConfigCleanupResult(skipped_raced=1)
        if not _is_expected_legacy_root(opened_root):
            return RuntimeConfigCleanupResult(skipped_foreign=1)

        has_residue = _legacy_runtime_has_residue(directory_fd)
        if has_residue is None:
            return RuntimeConfigCleanupResult(skipped_unsafe=1)

        # The final comparison is diagnostic only.  There is intentionally no
        # destructive operation after it, so a replacement is always retained.
        try:
            current_root = root_path.lstat()
        except FileNotFoundError:
            return RuntimeConfigCleanupResult(skipped_raced=1)
        except OSError:
            return RuntimeConfigCleanupResult(skipped_unsafe=1)
        if not _same_identity(current_root, opened_root):
            return RuntimeConfigCleanupResult(skipped_raced=1)

        # Every entry is untrusted legacy residue.  Do not inspect names or
        # content further: names may race and content may contain credentials.
        return RuntimeConfigCleanupResult(skipped_foreign=int(has_residue))
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _required_memfd_flags() -> tuple[int, int] | None:
    """Return Linux flags required for an anonymous, close-on-exec memfd."""
    create = getattr(os, "memfd_create", None)
    close_on_exec = getattr(os, "MFD_CLOEXEC", None)
    allow_sealing = getattr(os, "MFD_ALLOW_SEALING", None)
    if not callable(create) or not isinstance(close_on_exec, int) or not isinstance(
        allow_sealing, int
    ):
        return None
    return close_on_exec, allow_sealing


def _seal_runtime_config(descriptor: int) -> bool:
    """Make a complete anonymous config immutable before Rclone can open it."""
    if fcntl is None:
        return False
    add_seals = getattr(fcntl, "F_ADD_SEALS", None)
    seal_constants = (
        getattr(fcntl, "F_SEAL_SEAL", None),
        getattr(fcntl, "F_SEAL_SHRINK", None),
        getattr(fcntl, "F_SEAL_GROW", None),
        getattr(fcntl, "F_SEAL_WRITE", None),
    )
    if not isinstance(add_seals, int) or not all(
        isinstance(value, int) for value in seal_constants
    ):
        return False
    try:
        seals = 0
        for value in seal_constants:
            assert isinstance(value, int)
            seals |= value
        fcntl.fcntl(descriptor, add_seals, seals)
        return True
    except OSError:
        return False


def _anonymous_runtime_config(config_text: str) -> tuple[IO[str], Path] | None:
    """Return a sealed memfd and a child-readable procfs path to that descriptor.

    The descriptor is close-on-exec, so it is not inherited incidentally.  Rclone
    opens the parent's live descriptor through ``/proc/<pid>/fd/<fd>`` while the
    context owns it.  There is never a secret-bearing filesystem pathname to
    clean up after cancellation, SIGKILL, or a process crash.
    """
    required_flags = _required_memfd_flags()
    if required_flags is None:
        return None
    close_on_exec, allow_sealing = required_flags
    create = getattr(os, "memfd_create")
    descriptor = -1
    handle: IO[str] | None = None
    try:
        descriptor = create(
            "frostvault-rclone-runtime",
            close_on_exec | allow_sealing,
        )
        os.fchmod(descriptor, _RUNTIME_CONFIG_MODE)
        handle = os.fdopen(descriptor, "w+", encoding="utf-8")
        descriptor = -1  # The text handle owns the descriptor from here.
        handle.write(config_text)
        handle.flush()
        handle.seek(0)
        if not _seal_runtime_config(handle.fileno()):
            raise OSError("Unable to seal anonymous rclone runtime config")

        path = Path("/proc") / str(os.getpid()) / "fd" / str(handle.fileno())
        # Verify procfs exposes a separately opened descriptor without reading
        # its secret-bearing contents.  Rclone performs the same kind of open.
        reader = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        os.close(reader)
        return handle, path
    except (OSError, ValueError):
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        elif descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None


@contextmanager
def vault_rclone_config(vault: dict[str, Any]) -> Iterator[RuntimeRcloneConfig]:
    """Yield an anonymous per-vault Rclone config or fail closed.

    There is deliberately no named-file fallback.  Retrying a checked pathname
    before unlink cannot make cleanup safe against a concurrent same-UID writer.
    """
    if vault.get("encryption_mode") != "crypt":
        raise RuntimeError("Runtime crypt configuration requires encryption_mode=crypt")
    secrets = secrets_for_vault(vault)
    config_text = build_crypt_config_text(vault, secrets)

    anonymous = _anonymous_runtime_config(config_text)
    if anonymous is None:
        raise RuntimeConfigStorageError(
            "Anonymous rclone runtime storage is unavailable"
        )
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
                "Anonymous rclone runtime storage cleanup failed"
            ) from exc


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
