"""Validate the remaining, preconfigured plaintext Rclone upload route.

No generated config, per-Vault secrets, name encoding, or crypt remotes.
The shared alias must point at this Vault's bucket root; otherwise Rclone and
boto3 could operate on different namespaces. Legacy/unknown routes fail closed.
"""
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any


def require_plain_vault_remote(vault: dict[str, Any], config_path: str) -> None:
    # Also refuse stale in-memory/externally supplied legacy rows. Migration
    # rejects persisted crypt rows before dropping their columns.
    if vault.get("encryption_mode") not in (None, "plain"):
        raise RuntimeError("Legacy encrypted Vaults are unsupported; no objects were changed")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with Path(config_path).open(encoding="utf-8") as handle:
            parser.read_file(handle)
        remote = str(vault["rclone_remote"]).strip().rstrip(":")
        section = parser[remote]
        if section.get("type", "").strip().lower() != "alias":
            raise ValueError()
        base, separator, bucket = section.get("remote", "").strip().partition(":")
        if (
            not separator
            or bucket.strip("/") != str(vault["s3_bucket"]).strip("/")
            or parser[base].get("type", "").strip().lower() != "s3"
        ):
            raise ValueError()
    except (OSError, UnicodeError, configparser.Error, KeyError, ValueError):
        # Never include config content, provider errors or legacy credentials.
        raise RuntimeError(
            "Vault uploads require a plaintext Rclone alias to the configured S3 bucket root"
        ) from None
