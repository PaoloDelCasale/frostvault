"""SQL projection of the automatic-upload path rules (fixed noise + Vault globs).

Single place where Python glob semantics (``operation_policies._path_matches``)
are translated to PostgreSQL regular expressions. It is informational and
read-only: it classifies observations (upload activity, protection states) and
never authorizes an upload, release or deletion. Manual operations remain
explicit and ignore these rules.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from typing import Any

from .operation_policies import AUTO_UPLOAD_EXCLUSIONS

_logger = logging.getLogger(__name__)


class UnsafeGlob(ValueError):
    """A glob that cannot be expressed as a safe PostgreSQL regular expression."""


# Admission matches a path with its backslashes read as separators; the SQL
# projection reads the same normalized text (a Linux name may contain "\\").
PATH_SQL = "replace(fp.path, '\\', '/')"


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


def glob_body(pattern: str) -> str:
    if "\x00" in pattern:
        raise UnsafeGlob("NUL is not valid in a path pattern")
    try:
        pattern.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UnsafeGlob("pattern is not valid text") from exc
    body = fnmatch.translate(pattern)[4:-3]
    if "(?!)" in body:
        # Python's translation of an empty range such as ``[z-a]``: it can never
        # match and PostgreSQL's reading of the empty lookahead is not guaranteed.
        raise UnsafeGlob("empty character range")
    # Python's atomic groups are an optimization, not part of the glob language.
    # PostgreSQL ARE supports noncapturing groups and a leading (?s) flag.
    return _posix_are(body).replace("(?>", "(?:")


def path_pattern(pattern: str) -> str:
    body = glob_body(pattern)
    alternatives = [f"^(?:{body})$"]
    if "**" in pattern and pattern.endswith("/**"):
        # _path_matches treats this prefix literally, even if it contains '*'.
        alternatives.append(f"^{re.escape(pattern[:-3])}(?:/|$)")
    if pattern.startswith("**/"):
        alternatives.append(f"(?:^|/)(?:{glob_body(pattern[3:])})$")
    return "(?s)(?:" + "|".join(alternatives) + ")"


def validate_glob(pattern: str) -> None:
    """Raise ``ValueError`` when the glob cannot be classified safely in SQL."""
    try:
        re.compile(path_pattern(pattern))
    except re.error as exc:
        raise UnsafeGlob(f"unsupported glob: {exc}") from exc


def eligibility_sql(policy: Any) -> tuple[str, list[str]]:
    """Informational projection of the admission path rules, parametrized AREs.

    The SQL is true for paths (``fp.path``) the automatic upload may admit.
    ``policy`` needs only ``include_globs`` and ``exclude_globs``; None means
    just the fixed noise exclusions. The result is never empty. A stored glob
    that cannot be translated safely (rejected today, but possibly saved
    earlier) matches nothing rather than breaking every query that classifies
    files.
    """
    clauses: list[str] = []
    params: list[str] = []

    def match(pattern: str) -> str:
        try:
            regex = path_pattern(pattern)
        except UnsafeGlob:
            _logger.warning("Ignoring an unsupported upload glob in the SQL projection")
            return "FALSE"
        params.append(regex)
        if "**" in pattern:
            return f"{PATH_SQL} ~ %s"
        params.append(regex)
        return (f"({PATH_SQL} ~ %s OR EXISTS (SELECT 1 FROM unnest(string_to_array({PATH_SQL},'/')) "
                "AS parts(part) WHERE part ~ %s))")

    include = tuple(getattr(policy, "include_globs", ()) or ())
    exclude = tuple(getattr(policy, "exclude_globs", ()) or ())
    if include:
        clauses.append("(" + " OR ".join(match(pattern) for pattern in include) + ")")
    for pattern in (*AUTO_UPLOAD_EXCLUSIONS, *exclude):
        clauses.append("NOT (" + match(pattern) + ")")
    return " AND ".join(clauses), params


def inline_eligibility_sql(policy: Any) -> str:
    """``eligibility_sql`` with its patterns embedded as SQL string literals.

    Needed where the expression is a fragment of a larger statement whose own
    positional parameters must stay in order. Every pattern is a quoted
    literal (``'`` doubled); ``%`` is doubled because the statement is still
    executed with driver parameters.
    """
    sql, params = eligibility_sql(policy)
    literals = iter("'" + value.replace("'", "''").replace("%", "%%") + "'" for value in params)
    return re.sub(r"%s", lambda _: next(literals), sql)
