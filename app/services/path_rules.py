"""Path rules: what a glob means for a Vault path, in Python and in SQL.

One module owns the glob dialect of the Vault's include/exclude rules (#802):

- normalization and validation of the globs an Owner writes;
- the Python matcher, compiled once per set of globs (the scan hot path);
- the translation of the same semantics to a PostgreSQL regular expression;
- folder rules, the ``dir/**`` globs that cover a whole folder (ADR-0044).

It is a leaf: it imports no other service and knows no policy. Which globs
apply, the fixed noise exclusions and what an excluded path implies stay with
the callers (``operation_policies``, ``upload_path_rules``,
``excluded_folders``, ``vault_exclusions``).

The contract. A path is a catalog path: relative to the Vault root and
normalized (no empty, ``.`` or ``..`` segment, no leading or trailing ``/``).
Its parts are its text split on ``/``, in Python as in SQL, which decides at
scale (#811): nothing here parses a path again, so a path outside the contract
is read segment by segment as written (``a//b`` has an empty part, ``./x`` a
``.`` part), the same by every matcher. The globs of a policy have one way in,
:func:`normalize_globs`: every caller matches the globs it returns.

The dialect. A path is read with its backslashes as separators. A glob without
``**`` matches the whole path or any single part of it (``fnmatch`` case
sensitive, ``*`` crossing ``/``). A glob with ``**`` matches the whole path;
``prefix/**`` also matches ``prefix`` and everything below it, the prefix read
literally; ``**/suffix`` also matches ``suffix`` against every tail of the
path. The Python and the SQL matcher are compared on PostgreSQL over one
corpus (``tests/test_path_rules_characterization.py``).
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase, translate as fnmatch_translate
from typing import Any, Sequence

# --- Normalization and validation ----------------------------------------------------


class UnsafeGlob(ValueError):
    """A glob that cannot be expressed as a safe PostgreSQL regular expression."""


# PostgreSQL refuses a regular expression it finds "too complex" (observed from
# ~12.6 KB of ``*a`` repeated), which would fail every query that classifies
# files. A prudent cap keeps a large safety margin; the count bounds the size of
# the generated statements.
MAX_GLOB_LENGTH = 1024
MAX_GLOBS_PER_POLICY = 100


def normalize_globs(values: Sequence[str]) -> tuple[str, ...]:
    """The globs as stored: trimmed, ``/`` separators, blanks dropped, relative."""
    normalized: list[str] = []
    for value in values:
        text = str(value).strip().replace("\\", "/")
        if not text:
            continue
        if text.startswith("/"):
            raise ValueError("globs must be relative paths")
        normalized.append(text)
    return tuple(normalized)


def validate_glob(pattern: str) -> None:
    """Raise ``ValueError`` when the glob cannot be classified safely in SQL."""
    try:
        re.compile(sql_path_regex(pattern))
    except re.error as exc:
        raise UnsafeGlob(f"unsupported glob: {exc}") from exc


# --- Python matcher ------------------------------------------------------------------

# One path part (or any text) against one glob, case sensitive.
name_matches = fnmatchcase


class ParsedPath:
    """A normalized path split once for every glob it is matched against (#767)."""

    __slots__ = ("parts", "_tails")

    def __init__(self, normalized: str) -> None:
        # The text split on "/", as string_to_array does in SQL.
        self.parts = tuple(normalized.split("/"))
        self._tails: list[str] | None = None

    def tails(self) -> list[str]:
        # What follows the start or any "/": the SQL "(?:^|/)suffix$".
        if self._tails is None:
            parts = self.parts
            self._tails = ["/".join(parts[index:]) for index in range(len(parts))]
        return self._tails


class _CompiledGlob:
    """One glob of the dialect, its regular expressions compiled once."""

    __slots__ = ("pattern", "_match", "_prefix", "_suffix")

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        # fnmatchcase(name, pattern) is compile(translate(pattern)).match(name).
        self._match = re.compile(fnmatch_translate(pattern)).match
        self._prefix = pattern[: -len("/**")] if "**" in pattern and pattern.endswith("/**") else None
        self._suffix = (re.compile(fnmatch_translate(pattern[3:])).match
                        if "**" in pattern and pattern.startswith("**/") else None)

    def matches(self, normalized: str, path: ParsedPath) -> bool:
        if "**" in self.pattern:
            prefix = self._prefix
            if prefix is not None and (normalized == prefix or normalized.startswith(prefix + "/")):
                return True
            suffix = self._suffix
            if suffix is not None and (suffix(normalized) or any(
                    suffix(tail) for tail in path.tails())):
                return True
            return self._match(normalized) is not None
        match = self._match
        return match(normalized) is not None or any(match(part) for part in path.parts)


@functools.lru_cache(maxsize=4096)
def _compiled_glob(pattern: str) -> _CompiledGlob:
    return _CompiledGlob(pattern)


def path_matches(path: str, pattern: str) -> bool:
    """Whether one glob matches one path.

    For a single question; a caller that classifies many paths against the
    same globs builds a :class:`GlobSet` once instead.
    """
    normalized = path.replace("\\", "/")
    return _compiled_glob(pattern).matches(normalized, ParsedPath(normalized))


def any_name_glob(patterns: Sequence[str]) -> Any:
    """One compiled ``match``: true where :data:`name_matches` is for any pattern.

    Each alternative is anchored at the start by ``match`` and at the end by
    its own ``\\Z``, so the alternation matches exactly when one pattern does.
    """
    try:
        return re.compile("|".join(f"(?:{fnmatch_translate(pattern)})" for pattern in patterns)).match
    except re.error:  # pragma: no cover - fnmatch output always combines
        matches = tuple(re.compile(fnmatch_translate(pattern)).match for pattern in patterns)
        return lambda name: any(match(name) for match in matches)


class GlobSet:
    """Whether any glob of a set matches, as ``any(path_matches(path, glob))``.

    Compiled once: the globs without ``**`` are one regular expression, and a
    path is parsed once (:class:`ParsedPath`) for the whole set.
    """

    __slots__ = ("_plain", "_starred")

    def __init__(self, patterns: Sequence[str]) -> None:
        plain = [pattern for pattern in patterns if "**" not in pattern]
        # Without "**" a glob matches the whole path or any one part.
        self._plain = any_name_glob(plain) if plain else None
        self._starred = tuple(_CompiledGlob(pattern) for pattern in patterns if "**" in pattern)

    def __bool__(self) -> bool:
        return self._plain is not None or bool(self._starred)

    def matches(self, normalized: str, path: ParsedPath) -> bool:
        match = self._plain
        if match is not None:
            if match(normalized):
                return True
            for part in path.parts:
                if match(part):
                    return True
        for glob in self._starred:
            if glob.matches(normalized, path):
                return True
        return False


# --- SQL matcher ---------------------------------------------------------------------


def normalized_path_sql(column: str) -> str:
    """The SQL text of a path column as the matcher reads it.

    The Python matcher reads backslashes as separators; SQL reads the same
    normalized text (a Linux name may contain "\\").
    """
    return f"replace({column}, '\\', '/')"


def _posix_are(body: str) -> str:
    """Rewrite Python's regex for a glob as a POSIX ARE both engines read alike.

    The glob dialect only yields literals, ``.``, ``.*``, ``.*?``, groups and
    bracket sets, so a small scanner is enough. Inside a set every ``[`` and a
    leading ``]`` are escaped: PostgreSQL would otherwise read ``[:``, ``[.``
    and ``[=`` as class/collating/equivalence openers and reject the set.
    """
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        char = body[i]
        if char == "\\":
            out.append(body[i:i + 2])
            i += 2
        elif body.startswith("(?!)", i):
            # Python's translation of an empty range such as ``[z-a]``: it can never
            # match and PostgreSQL's reading of the empty lookahead is not guaranteed.
            raise UnsafeGlob("empty character range")
        elif body.startswith("(?>", i):
            # Python's atomic groups are an optimization, not part of the glob
            # language; PostgreSQL ARE has noncapturing groups instead.
            out.append("(?:")
            i += 3
        elif char == "[":
            out.append("[")
            i += 1
            if body[i] == "^":
                out.append("^")
                i += 1
            first = True
            while True:
                char = body[i]
                if char == "\\":
                    out.append(body[i:i + 2])
                    i += 2
                elif char == "]" and not first:
                    break
                elif char in "[]":
                    out.append("\\" + char)
                    i += 1
                else:
                    out.append(char)
                    i += 1
                first = False
            out.append("]")
            i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _glob_body(pattern: str) -> str:
    if "\x00" in pattern:
        raise UnsafeGlob("NUL is not valid in a path pattern")
    try:
        pattern.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UnsafeGlob("pattern is not valid text") from exc
    return _posix_are(fnmatch_translate(pattern)[4:-3])


def sql_path_regex(pattern: str) -> str:
    """The PostgreSQL regular expression of one glob against a whole path.

    Raises :class:`UnsafeGlob` for a glob PostgreSQL cannot read safely.
    """
    if len(pattern) > MAX_GLOB_LENGTH:
        raise UnsafeGlob(f"glob longer than {MAX_GLOB_LENGTH} characters")
    body = _glob_body(pattern)
    alternatives = [f"^(?:{body})$"]
    if "**" in pattern and pattern.endswith("/**"):
        # The matcher reads this prefix literally, even if it contains '*'.
        alternatives.append(f"^{re.escape(pattern[:-3])}(?:/|$)")
    if pattern.startswith("**/"):
        alternatives.append(f"(?:^|/)(?:{_glob_body(pattern[3:])})$")
    return "(?s)(?:" + "|".join(alternatives) + ")"


def sql_glob_match(path_sql: str, pattern: str) -> tuple[str, tuple[str, ...]]:
    """SQL true where the glob matches the path, and its ``%s`` parameters.

    ``path_sql`` is the normalized path expression (:func:`normalized_path_sql`).
    Raises :class:`UnsafeGlob` like :func:`sql_path_regex`.
    """
    regex = sql_path_regex(pattern)
    if "**" in pattern:
        return f"{path_sql} ~ %s", (regex,)
    return (f"({path_sql} ~ %s OR EXISTS (SELECT 1 FROM unnest(string_to_array({path_sql},'/')) "
            "AS parts(part) WHERE part ~ %s))"), (regex, regex)


# --- Folder rules --------------------------------------------------------------------

_FLOATING = "**/"
_SUFFIX = "/**"


def _segment_regex(segment: str) -> str:
    """One path component, as a POSIX ARE that Python's ``re`` reads alike."""
    out: list[str] = []
    for char in segment:
        if char == "*":
            out.append("[^/\\\\]*")
        elif char == "?":
            out.append("[^/\\\\]")
        else:
            out.append(re.escape(char))
    return "".join(out)


@dataclass(frozen=True)
class FolderRule:
    """``segments/**`` anchored at the Vault root, or ``**/segments/**`` anywhere.

    A glob that covers a whole folder: every path below a folder whose segments
    match the rule's one by one is matched by :func:`path_matches` and by
    :func:`sql_path_regex` alike. The rule is certain, never approximate: a
    segment may use ``*`` and ``?`` only, and a folder name containing a
    backslash is never covered (the matcher reads ``\\`` as a separator).
    """

    pattern: str
    segments: tuple[str, ...]
    floating: bool

    def _matches_at(self, parts: tuple[str, ...], start: int) -> bool:
        end = start + len(self.segments)
        return end <= len(parts) and all(
            fnmatchcase(part, segment)
            for part, segment in zip(parts[start:end], self.segments)
        )

    def covers(self, parts: tuple[str, ...]) -> bool:
        """Whether the folder at ``parts`` is at or below a folder of this rule."""
        if any("\\" in part for part in parts):
            return False
        if not self.floating:
            return self._matches_at(parts, 0)
        return any(self._matches_at(parts, start) for start in range(len(parts)))

    def inside_regex(self) -> str:
        """PostgreSQL regex matching a path strictly inside a folder of this rule."""
        body = "/".join(_segment_regex(segment) for segment in self.segments)
        return ("(^|/)" if self.floating else "^") + body + "/"


def folder_rule(pattern: str) -> FolderRule | None:
    """The folder rule of one glob, or None when the glob is not one."""
    if not pattern.endswith(_SUFFIX):
        return None
    folder = pattern[: -len(_SUFFIX)]
    floating = folder.startswith(_FLOATING)
    if floating:
        folder = folder[len(_FLOATING):]
    segments = tuple(folder.split("/"))
    for segment in segments:
        if (
            not segment or segment in {".", ".."} or "**" in segment
            or any(char in segment for char in "[]\\")
        ):
            return None
    try:
        # A glob SQL cannot classify matches nothing there (ADR-0028): its rows
        # would not be shown Excluded, so it is never a folder rule.
        sql_path_regex(pattern)
    except UnsafeGlob:
        return None
    return FolderRule(pattern, segments, floating)
