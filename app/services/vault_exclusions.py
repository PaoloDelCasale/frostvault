"""Exclusions as the Owner sees them: rules, Excluded Folders on disk, one-item edits.

Issue #754. An Excluded Folder is not observed (ADR-0044), so in a new Vault it
has no catalog row and would vanish from the Vault browser. This module reads
the disk on demand, one directory level per request, to show it:

- at a catalogued level, the subfolders an Excluded Folder rule covers, with the
  rule;
- inside an Excluded Folder, its entries as an unprotected disk listing.

Nothing read here is catalogued, fingerprinted or watched: scans and the watcher
keep ignoring Excluded Folders. Every read is confined to the Vault root: the
root is opened ``O_NOFOLLOW`` and checked against its enrolled identity, each
component is opened relative to its parent descriptor without following a
symbolic link, and symbolic links are never listed as folders or entered.

The same module edits one rule at a time (add or remove a single glob), so the
browser context menu never replaces a whole list read earlier, and estimates
what including an item again would upload, walking the disk within a time
budget.
"""
from __future__ import annotations

import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Iterator, Literal

from .excluded_folders import ExcludedFolders, excluded_folders, folder_rule
from .fs_preflight import is_storable_name
from .operation_policies import (
    AUTO_UPLOAD_EXCLUSIONS,
    OperationPolicy,
    _normalize_globs,
    _path_matches,
    auto_upload_path_is_included,
)
from .source_layout import vault_local_access

# Characters with a meaning in a glob: a path holding one cannot be its own rule.
GLOB_CHARACTERS = frozenset("*?[]\\")
# One listing never returns more entries than this; the rest is counted.
MAX_LISTING_ENTRIES = 1000
# «Include again» walks the disk for at most this long, then reports a minimum.
INCLUSION_WALK_BUDGET_SECONDS = 5.0

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

GlobField = Literal["include_globs", "exclude_globs"]


class DiskReadError(Exception):
    """The disk could not be read: ``unavailable``, ``missing`` or ``unreadable``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RuleNotFound(LookupError):
    """The rule to remove is not in the Vault policy (any more)."""


# --- Rules -------------------------------------------------------------------


def has_glob_characters(path: str) -> bool:
    return any(char in GLOB_CHARACTERS for char in path)


def own_rule(path: str, *, is_directory: bool) -> str | None:
    """The rule that excludes exactly this item, or None when it cannot exist.

    A folder's rule is ``path/**`` (an Excluded Folder, ADR-0044); a file's is
    its path. A path holding a glob character would match other items too.
    """
    if not path or has_glob_characters(path):
        return None
    return f"{path}/**" if is_directory else path


def edit_glob(policy: OperationPolicy, field: GlobField, action: str, glob: str) -> OperationPolicy:
    """``policy`` with one glob added to, or removed from, one list.

    Adding a glob already present changes nothing; removing one that is not
    present raises :class:`RuleNotFound`, so a stale view never removes another
    rule by position.
    """
    normalized = _normalize_globs((glob,))
    if not normalized:
        raise ValueError("glob must not be empty")
    value = normalized[0]
    current = tuple(_normalize_globs(getattr(policy, field)))
    if action == "add":
        updated = current if value in current else (*current, value)
    elif action == "remove":
        if value not in current:
            raise RuleNotFound(value)
        updated = tuple(item for item in current if item != value)
    else:  # Defensive; the API model allows add/remove only.
        raise ValueError("action must be add or remove")
    return replace(policy, **{field: updated})


def folder_rule_covering(rules: ExcludedFolders, parts: tuple[str, ...]) -> str | None:
    """The first Excluded Folder rule that covers the folder at ``parts``."""
    if not parts:
        return None
    for rule in rules.rules:
        if rule.covers(parts):
            return rule.pattern
    return None


def _parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)


def exclusion_reasons(policy: OperationPolicy, path: str, *, is_directory: bool) -> dict[str, Any]:
    """Why an item is excluded from automatic upload under ``policy``.

    A folder is excluded when an Excluded Folder rule covers it (other patterns
    exclude some files only, which keep their own state). A file is excluded
    by the matching exclude globs, the fixed noise exclusions, or by not
    matching any include glob.
    """
    if is_directory:
        rules = [rule.pattern for rule in excluded_folders(policy).rules if rule.covers(_parts(path))]
        return {"rules": rules, "fixed": [], "not_included": False, "excluded": bool(rules)}
    normalized = path.replace("\\", "/")
    rules = [pattern for pattern in _normalize_globs(policy.exclude_globs) if _path_matches(normalized, pattern)]
    fixed = [pattern for pattern in AUTO_UPLOAD_EXCLUSIONS
             if any(fnmatchcase(part, pattern) for part in _parts(normalized))]
    include = _normalize_globs(policy.include_globs)
    not_included = bool(include) and not any(_path_matches(normalized, pattern) for pattern in include)
    return {
        "rules": rules,
        "fixed": fixed,
        "not_included": not_included,
        "excluded": not auto_upload_path_is_included(normalized, policy),
    }


# --- Disk --------------------------------------------------------------------


@contextmanager
def _open_directory(vault: dict[str, Any], parts: tuple[str, ...]) -> Iterator[int]:
    """A descriptor of the Vault folder at ``parts``, never through a symlink."""
    from .vault_relocation import ROOT_IDENTITY_VERSION, VaultRelocationError, opened_root_identity

    source_root = str(vault.get("source_root") or "")
    if not source_root or not vault_local_access(source_root).local_operations_allowed:
        raise DiskReadError("unavailable")
    if vault.get("root_identity_version") != ROOT_IDENTITY_VERSION or not vault.get("root_identity"):
        raise DiskReadError("unavailable")
    try:
        fd = os.open(source_root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise DiskReadError("unavailable") from exc
    try:
        try:
            identity = opened_root_identity(fd)
        except VaultRelocationError as exc:
            raise DiskReadError("unavailable") from exc
        if identity != vault["root_identity"]:
            raise DiskReadError("unavailable")
        for part in parts:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except PermissionError as exc:
                raise DiskReadError("unreadable") from exc
            except OSError as exc:
                # Absent, a file, or a symbolic link (ELOOP): not a folder here.
                raise DiskReadError("missing") from exc
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _scan(fd: int) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(fd) as entries:
            return list(entries)
    except PermissionError as exc:
        raise DiskReadError("unreadable") from exc
    except OSError as exc:
        raise DiskReadError("missing") from exc


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _level(fd: int, directory: str, parts: tuple[str, ...], rules: ExcludedFolders,
           excluded_by: str | None) -> list[dict[str, Any]]:
    """The Excluded Folders of one level, or (inside one) all its entries."""
    listed: list[dict[str, Any]] = []
    for entry in _scan(fd):
        if not is_storable_name(entry.name):
            continue
        if excluded_by is None:
            if not entry.is_dir(follow_symlinks=False):
                continue
            rule = folder_rule_covering(rules, (*parts, entry.name))
            if rule is not None:
                listed.append({"name": entry.name, "path": _join(directory, entry.name), "rule": rule})
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            kind, size = "directory", None
        elif stat.S_ISREG(info.st_mode):
            kind, size = "file", int(info.st_size)
        else:
            continue  # Symbolic links and special files are never followed.
        listed.append({
            "name": entry.name,
            "path": _join(directory, entry.name),
            "type": kind,
            "size": size,
            "modified_at": _iso(info.st_mtime),
        })
    if excluded_by is None:
        listed.sort(key=lambda item: item["name"].lower())
    return listed


def directory_listing(vault: dict[str, Any], policy: OperationPolicy, directory: str) -> dict[str, Any]:
    """One level of the Vault folder ``directory``, read from disk.

    Outside an Excluded Folder: the subfolders an Excluded Folder rule covers
    (``folders``), so a folder the scan never enters still appears in the
    browser. Inside one (``excluded_by`` set): every entry of the level
    (``entries``), unprotected. Without any folder rule the disk is not read.
    """
    rules = excluded_folders(policy)
    parts = _parts(directory)
    excluded_by = folder_rule_covering(rules, parts)
    result: dict[str, Any] = {
        "directory": directory,
        "excluded_by": excluded_by,
        "folders": [],
        "entries": [],
        "total_entries": 0,
        "truncated": False,
    }
    if not rules:
        return result
    with _open_directory(vault, parts) as fd:
        # Entries stat relative to ``fd``: read them while it is open.
        listed = _level(fd, directory, parts, rules, excluded_by)
    if excluded_by is None:
        result["folders"] = listed
        return result
    listed.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
    result["total_entries"] = len(listed)
    result["truncated"] = len(listed) > MAX_LISTING_ENTRIES
    result["entries"] = listed[:MAX_LISTING_ENTRIES]
    return result


@dataclass
class _Tally:
    files: int = 0
    bytes: int = 0
    complete: bool = True


def _walk(fd: int, parts: tuple[str, ...], policy: OperationPolicy, rules: ExcludedFolders,
          tally: _Tally, deadline: float, clock: Any) -> None:
    try:
        entries = _scan(fd)
    except DiskReadError:
        return  # An unreadable folder adds nothing; the estimate stays a minimum.
    for entry in entries:
        if clock() > deadline:
            tally.complete = False
            return
        if not is_storable_name(entry.name):
            continue
        child_parts = (*parts, entry.name)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            if auto_upload_path_is_included("/".join(child_parts), policy):
                tally.files += 1
                tally.bytes += int(info.st_size)
        elif stat.S_ISDIR(info.st_mode) and not rules.covers_directory(child_parts):
            try:
                child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=fd)
            except OSError:
                continue
            try:
                _walk(child, child_parts, policy, rules, tally, deadline, clock)
            finally:
                os.close(child)
            if not tally.complete:
                return


def measure_upload(vault: dict[str, Any], policy: OperationPolicy, path: str, *, is_directory: bool,
                   budget_seconds: float = INCLUSION_WALK_BUDGET_SECONDS,
                   clock: Any = time.monotonic) -> dict[str, Any]:
    """Files and bytes under ``path`` that ``policy`` would upload, from disk now.

    The walk honours ``policy`` exactly as admission does and skips the folders
    it still excludes. Past ``budget_seconds`` it stops: ``complete`` is false
    and the counts are a minimum.
    """
    parts = _parts(path)
    tally = _Tally()
    if is_directory:
        rules = excluded_folders(policy)
        if not rules.covers_directory(parts):
            with _open_directory(vault, parts) as fd:
                _walk(fd, parts, policy, rules, tally, clock() + budget_seconds, clock)
        return {"files": tally.files, "bytes": tally.bytes, "complete": tally.complete}
    with _open_directory(vault, parts[:-1]) as fd:
        try:
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except OSError as exc:
            raise DiskReadError("missing") from exc
    if stat.S_ISREG(info.st_mode) and auto_upload_path_is_included(path, policy):
        tally.files, tally.bytes = 1, int(info.st_size)
    return {"files": tally.files, "bytes": tally.bytes, "complete": True}
