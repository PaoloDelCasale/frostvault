"""Ephemeral Rclone configuration for per-vault crypt remotes (issue #6)."""
from __future__ import annotations

import configparser
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..config import settings
from .vault_crypto import (
    CryptSecrets,
    StoredCryptSecrets,
    decrypt_vault_secrets,
    obscure_for_rclone,
)

RUNTIME_REMOTE_NAME = "vault"


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


@contextmanager
def vault_rclone_config(vault: dict[str, Any]) -> Iterator[RuntimeRcloneConfig]:
    """Yield a temp Rclone config for ``vault`` and delete it afterwards."""
    if vault.get("encryption_mode") != "crypt":
        raise RuntimeError("Runtime crypt configuration requires encryption_mode=crypt")
    secrets = secrets_for_vault(vault)
    config_text = build_crypt_config_text(vault, secrets)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".rclone.conf",
        delete=False,
    )
    path = Path(tmp.name)
    try:
        with tmp:
            tmp.write(config_text)
            tmp.flush()
        os_chmod = path.chmod
        os_chmod(0o600)
        yield RuntimeRcloneConfig(
            path=path,
            remote_name=RUNTIME_REMOTE_NAME,
            config_text=config_text,
            secrets=secrets,
        )
    finally:
        path.unlink(missing_ok=True)


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
