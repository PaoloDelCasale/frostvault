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

try:  # Linux deployments use flock; retaining the fallback keeps the module portable.
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported deployment target.
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
RUNTIME_CONFIG_PREFIX = "vault-"
RUNTIME_CONFIG_SUFFIX = ".rclone.conf"


class RuntimeConfigStorageError(RuntimeError):
    """Protected storage for a generated Rclone configuration is unavailable."""


@dataclass(frozen=True)
class RuntimeRcloneConfig:
    path: Path
    remote_name: str
    config_text: str
    secrets: CryptSecrets | None


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


def _runtime_directory(*, create: bool) -> Path | None:
    """Return the protected fallback directory without following a symlink."""
    directory = Path(tempfile.gettempdir()) / RUNTIME_DIRECTORY_NAME
    try:
        if create:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        else:
            try:
                directory.lstat()
            except FileNotFoundError:
                return None

        directory_stat = directory.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, flags)
    except RuntimeConfigStorageError:
        raise
    except OSError as exc:
        raise RuntimeConfigStorageError(
            "Protected rclone runtime storage is unavailable"
        ) from exc

    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode) or opened_stat.st_uid != os.geteuid():
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            )
        os.fchmod(descriptor, 0o700)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
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
        os.close(descriptor)
    return directory


def runtime_config_directory() -> Path:
    """Create and return the mode-0700 fallback directory for named configs."""
    directory = _runtime_directory(create=True)
    assert directory is not None
    return directory


def _is_runtime_config_name(name: str) -> bool:
    return name.startswith(RUNTIME_CONFIG_PREFIX) and name.endswith(
        RUNTIME_CONFIG_SUFFIX
    )


def _lock_runtime_config(descriptor: int, *, nonblocking: bool = False) -> bool:
    """Lock a named fallback config; False means another process still owns it."""
    if fcntl is None:  # pragma: no cover - Linux deployments always have fcntl.
        return True
    flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
    try:
        fcntl.flock(descriptor, flags)
    except BlockingIOError:
        return False
    return True


def _remove_stale_runtime_config(path: Path) -> bool:
    """Remove one inactive generated file without following a symlink."""
    try:
        entry_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeConfigStorageError(
            "Protected rclone runtime storage is unavailable"
        ) from exc

    # A generated file is regular. Removing a same-named symlink itself is safe,
    # while never traversing it prevents cleanup from touching an outside target.
    if stat.S_ISLNK(entry_stat.st_mode):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RuntimeConfigStorageError(
                "Protected rclone runtime storage is unavailable"
            ) from exc
    if not stat.S_ISREG(entry_stat.st_mode):
        return False

    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        if not _lock_runtime_config(descriptor, nonblocking=True):
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeConfigStorageError(
            "Protected rclone runtime storage is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def cleanup_runtime_configs() -> int:
    """Remove inactive fallback configs left by an interrupted process.

    Normal crypt operations use an anonymous descriptor on Linux, so the
    directory is ordinarily absent. It remains as a narrowly-scoped fallback
    and permits a later startup to erase any named residue safely.
    """
    directory = _runtime_directory(create=False)
    if directory is None:
        return 0
    try:
        candidates = tuple(directory.iterdir())
    except OSError as exc:
        raise RuntimeConfigStorageError(
            "Protected rclone runtime storage is unavailable"
        ) from exc
    return sum(
        _remove_stale_runtime_config(path)
        for path in candidates
        if _is_runtime_config_name(path.name)
    )


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


def _write_runtime_config(descriptor: int, config_text: str) -> None:
    payload = config_text.encode("utf-8")
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("Unable to write protected rclone runtime storage")
        offset += written


def _cleanup_protected_runtime_config(path: Path | None, descriptor: int) -> None:
    """Unlink a named fallback config before releasing its active-process lock."""
    cleanup_error: OSError | None = None
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_error = exc
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError as exc:
            cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise RuntimeConfigStorageError(
            "Protected rclone runtime storage cleanup failed"
        ) from cleanup_error


@contextmanager
def _protected_runtime_config(config_text: str) -> Iterator[Path]:
    """Yield a protected named fallback config and erase it on every exit path."""
    descriptor = -1
    path: Path | None = None
    try:
        directory = runtime_config_directory()
        descriptor, filename = tempfile.mkstemp(
            prefix=RUNTIME_CONFIG_PREFIX,
            suffix=RUNTIME_CONFIG_SUFFIX,
            dir=directory,
        )
        path = Path(filename)
        os.fchmod(descriptor, 0o600)
        _lock_runtime_config(descriptor)
        _write_runtime_config(descriptor, config_text)
    except RuntimeConfigStorageError:
        _cleanup_protected_runtime_config(path, descriptor)
        raise
    except OSError as exc:
        _cleanup_protected_runtime_config(path, descriptor)
        raise RuntimeConfigStorageError(
            "Protected rclone runtime storage is unavailable"
        ) from exc

    try:
        assert path is not None
        yield path
    finally:
        _cleanup_protected_runtime_config(path, descriptor)


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
