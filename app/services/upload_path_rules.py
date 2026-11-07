"""SQL projection of the automatic-upload path rules (fixed noise + Vault globs).

Single place where Python glob semantics (``operation_policies._path_matches``)
are translated to PostgreSQL regular expressions. It is informational and
read-only: it classifies observations (upload activity, protection states) and
never authorizes an upload, release or deletion. Manual operations remain
explicit and ignore these rules.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any

from .operation_policies import AUTO_UPLOAD_EXCLUSIONS


def glob_body(pattern: str) -> str:
    # Python's atomic groups are an optimization, not part of the glob language.
    # PostgreSQL ARE supports noncapturing groups and a leading (?s) flag.
    return fnmatch.translate(pattern)[4:-3].replace("(?>", "(?:")


def path_pattern(pattern: str) -> str:
    body = glob_body(pattern)
    alternatives = [f"^(?:{body})$"]
    if "**" in pattern and pattern.endswith("/**"):
        # _path_matches treats this prefix literally, even if it contains '*'.
        alternatives.append(f"^{re.escape(pattern[:-3])}(?:/|$)")
    if pattern.startswith("**/"):
        alternatives.append(f"(?:^|/)(?:{glob_body(pattern[3:])})$")
    return "(?s)(?:" + "|".join(alternatives) + ")"


def eligibility_sql(policy: Any) -> tuple[str, list[str]]:
    """Informational projection of the admission path rules, parametrized AREs.

    The SQL is true for paths (``fp.path``) the automatic upload may admit.
    ``policy`` needs only ``include_globs`` and ``exclude_globs``; None means
    just the fixed noise exclusions. The result is never empty.
    """
    clauses: list[str] = []
    params: list[str] = []

    def match(pattern: str) -> str:
        regex = path_pattern(pattern)
        params.append(regex)
        if "**" in pattern:
            return "fp.path ~ %s"
        params.append(regex)
        return ("(fp.path ~ %s OR EXISTS (SELECT 1 FROM unnest(string_to_array(fp.path,'/')) "
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
