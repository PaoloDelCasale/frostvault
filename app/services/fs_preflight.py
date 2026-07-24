"""POSIX vault filesystem readiness diagnostics.

Framework-agnostic: callers pass a vault root path. The checker never changes
ownership or modes; it only reports access, identity, and per-entry problems.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .s3_preflight import PreflightCheck

CheckStatus = str  # reused vocabulary: pass / fail / warn


@dataclass(frozen=True)
class FilesystemFinding:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class FilesystemPreflightResult:
    root: str
    ok: bool
    uid: int
    gid: int
    checks: tuple[PreflightCheck, ...]
    findings: tuple[FilesystemFinding, ...]


def resolve_configured_vault_root(
    raw: str | Path,
    *,
    allowed_bases: Sequence[str | Path],
) -> Path | None:
    """Return a canonical vault root only when it stays under an allowed base.

    Uses realpath + prefix checks so callers never walk operator-unexpected
    paths derived from stored vault configuration.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    resolved = os.path.realpath(text)
    for base in allowed_bases:
        base_text = str(base or "").strip()
        if not base_text:
            continue
        base_real = os.path.realpath(base_text)
        if resolved == base_real or resolved.startswith(base_real + os.sep):
            return Path(resolved)
    return None


def check_vault_filesystem(
    root: str | Path,
    *,
    allowed_bases: Sequence[str | Path],
) -> FilesystemPreflightResult:
    """Inspect ``root`` for archive-safe local filesystem access.

    Reports vault-root read/write/execute access, the effective UID/GID,
    unreadable files, unwritable directories, and symbolic links. Does not
    follow directory symlinks when walking, and never mutates permissions.
    """
    root_path = resolve_configured_vault_root(root, allowed_bases=allowed_bases)
    uid = os.geteuid()
    gid = os.getegid()
    checks: list[PreflightCheck] = [
        PreflightCheck(
            code="fs.identity",
            status="pass",
            message=f"Effective identity is uid={uid} gid={gid}",
        )
    ]
    findings: list[FilesystemFinding] = []

    if root_path is None:
        checks.append(
            PreflightCheck(
                code="fs.root_access",
                status="fail",
                message="Vault root is outside the configured source roots",
                remediation=(
                    "Choose a vault directory that stays under a configured "
                    "source root"
                ),
            )
        )
        return FilesystemPreflightResult(
            root=str(root or ""),
            ok=False,
            uid=uid,
            gid=gid,
            checks=tuple(checks),
            findings=(),
        )

    if not root_path.exists() or not root_path.is_dir():
        checks.append(
            PreflightCheck(
                code="fs.root_access",
                status="fail",
                message=f"Vault root is not available: {root_path}",
                remediation=(
                    "Create the vault directory on the host and mount it at the "
                    "configured source root"
                ),
            )
        )
        return FilesystemPreflightResult(
            root=str(root_path),
            ok=False,
            uid=uid,
            gid=gid,
            checks=tuple(checks),
            findings=(),
        )

    missing: list[str] = []
    if not os.access(root_path, os.R_OK):
        missing.append("read")
    if not os.access(root_path, os.W_OK):
        missing.append("write")
    if not os.access(root_path, os.X_OK):
        missing.append("execute")

    if missing:
        needed = "/".join(missing)
        checks.append(
            PreflightCheck(
                code="fs.root_access",
                status="fail",
                message=f"Vault root lacks {needed} access for uid={uid} gid={gid}",
                remediation=(
                    f"Grant the archive user (PUID={uid}/PGID={gid}) {needed} "
                    "permission on the host vault directory without running the "
                    "container as root"
                ),
            )
        )
    else:
        checks.append(
            PreflightCheck(
                code="fs.root_access",
                status="pass",
                message="Vault root is readable, writable, and executable",
            )
        )

    for dirpath, dirnames, filenames in os.walk(
        root_path, topdown=True, followlinks=False, onerror=_walk_error_noop
    ):
        current = Path(dirpath)
        keep_dirs: list[str] = []
        for name in list(dirnames):
            entry = current / name
            relative = entry.relative_to(root_path).as_posix()
            if entry.is_symlink():
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.symlink",
                        message=f"Symbolic link rejected: {relative}",
                    )
                )
                continue
            if not os.access(entry, os.X_OK) or not os.access(entry, os.R_OK):
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.unreadable_file",
                        message=f"Directory is unreadable: {relative}",
                    )
                )
                continue
            if not os.access(entry, os.W_OK):
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.unwritable_directory",
                        message=f"Directory is not writable: {relative}",
                    )
                )
            # Descend only into real directories (never through symlinks).
            keep_dirs.append(name)
        dirnames[:] = keep_dirs

        for name in filenames:
            entry = current / name
            relative = entry.relative_to(root_path).as_posix()
            if entry.is_symlink():
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.symlink",
                        message=f"Symbolic link rejected: {relative}",
                    )
                )
                continue
            if not os.access(entry, os.R_OK):
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.unreadable_file",
                        message=f"File is unreadable: {relative}",
                    )
                )

    ok = all(check.status != "fail" for check in checks) and not findings
    if findings:
        checks.append(
            PreflightCheck(
                code="fs.entries",
                status="fail",
                message=f"{len(findings)} filesystem problem(s) under the vault root",
                remediation=(
                    "Fix host permissions for the reported paths or remove "
                    "symbolic links; the archive never changes ownership or modes"
                ),
            )
        )
    else:
        checks.append(
            PreflightCheck(
                code="fs.entries",
                status="pass",
                message="No unreadable files, unwritable directories, or symbolic links",
            )
        )

    return FilesystemPreflightResult(
        root=str(root_path),
        ok=ok,
        uid=uid,
        gid=gid,
        checks=tuple(checks),
        findings=tuple(findings),
    )


def _walk_error_noop(_error: OSError) -> None:
    """Continue walking when a directory cannot be listed."""
    return None
