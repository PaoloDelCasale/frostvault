"""Excluded Folders: folders one of the Vault's own exclusion rules covers entirely.

ADR-0044 (#688). Only a Vault ``exclude_globs`` entry of the form ``dir/**`` or
``**/dir/**`` (the pattern ends in ``/**``) is a folder rule: every path below
such a folder is excluded by the admission rules, whatever its name, so the scan
and the watcher need not look inside. Every other pattern (``*.tmp``,
``**/x/*.jpg``) and the fixed noise exclusions keep cataloguing as before
(ADR-0028).

The rule must be certain, never approximate. A segment may use ``*`` and ``?``
only (no ``[...]`` sets and no ``**`` inside the folder part), and a folder name
containing a backslash is never pruned: admission reads ``\\`` as a separator.
With those limits, a folder whose path segments match the rule's segments one by
one has every descendant path matched by ``path_rules.path_matches`` and by its
SQL translation alike (``path_rules.FolderRule``), so the rows already known
there are shown Excluded. ``include_globs`` never re-open such a folder:
admission applies the exclusions after the inclusions, so an excluded path stays
excluded whatever the include rules say.

What it is, is derived at read time from the current policy; no catalog evidence
records it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .operation_policies import OperationPolicy, get_policy
from .path_rules import FolderRule, folder_rule, normalize_globs


def _parts(relative: str | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(relative, tuple):
        return relative
    # The text split on "/", as the SQL of the same rule reads it; the Vault
    # root ("") has no parts.
    return tuple(relative.split("/")) if relative else ()


@dataclass(frozen=True)
class ExcludedFolders:
    """The folder rules of one Vault policy."""

    rules: tuple[FolderRule, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.rules)

    @property
    def patterns(self) -> frozenset[str]:
        return frozenset(rule.pattern for rule in self.rules)

    def covers_directory(self, relative: str | tuple[str, ...]) -> bool:
        """Whether the folder at ``relative`` is, or is inside, an Excluded Folder."""
        parts = _parts(relative)
        return bool(parts) and any(rule.covers(parts) for rule in self.rules)

    def contains_path(self, relative: str) -> bool:
        """Whether a path lies strictly inside an Excluded Folder."""
        parent = _parts(relative)[:-1]
        return self.covers_directory(parent)

    def not_inside_sql(self, column: str = "path") -> tuple[str, list[Any]]:
        """SQL true for paths NOT strictly inside an Excluded Folder."""
        if not self.rules:
            return "TRUE", []
        return (
            f"NOT ({column} ~ ANY(%s::text[]))",
            [[rule.inside_regex() for rule in self.rules]],
        )

    def narrowed_by(self, new: "ExcludedFolders") -> bool:
        """Whether ``new`` may look inside a folder these rules pruned."""
        return bool(self.patterns - new.patterns)


def excluded_folders(policy: OperationPolicy | None) -> ExcludedFolders:
    if policy is None:
        return ExcludedFolders()
    rules = []
    for pattern in normalize_globs(policy.exclude_globs):
        rule = folder_rule(pattern)
        if rule is not None and rule not in rules:
            rules.append(rule)
    return ExcludedFolders(tuple(rules))


def load(connection: Any, vault_id: int) -> ExcludedFolders:
    return excluded_folders(get_policy(connection, int(vault_id)))
