"""SQL projection of the automatic-upload path rules (fixed noise + Vault globs).

The glob semantics and their PostgreSQL translation live in ``path_rules``;
this module applies them to a policy. It is informational and read-only: it
classifies observations (upload activity, protection states) and never
authorizes an upload, release or deletion. Manual operations remain explicit
and ignore these rules.
"""
from __future__ import annotations

import functools
import logging
import re
from typing import Any

from .operation_policies import AUTO_UPLOAD_EXCLUSIONS
from .path_rules import UnsafeGlob, normalized_path_sql, sql_glob_match

_logger = logging.getLogger(__name__)

# Admission matches a path with its backslashes read as separators; the SQL
# projection reads the same normalized text (a Linux name may contain "\\").
PATH_SQL = normalized_path_sql("f.path")


@functools.lru_cache(maxsize=512)
def stored_glob_match(pattern: str) -> tuple[str, tuple[str, ...]] | None:
    """The SQL match of a glob already in use, or None when it is unsupported.

    Cached so the warning for an unsupported stored glob is logged once, not on
    every query that classifies files.
    """
    try:
        return sql_glob_match(PATH_SQL, pattern)
    except UnsafeGlob as exc:
        _logger.warning("Ignoring an unsupported upload glob in the SQL projection (%s)", exc)
        return None


def eligibility_sql(policy: Any) -> tuple[str, list[str]]:
    """Informational projection of the admission path rules, parametrized AREs.

    The SQL is true for paths (``f.path``) the automatic upload may admit.
    ``policy`` needs only ``include_globs`` and ``exclude_globs``; None means
    just the fixed noise exclusions. The result is never empty. A stored glob
    that cannot be translated safely (rejected today, but possibly saved
    earlier) matches nothing rather than breaking every query that classifies
    files.
    """
    clauses: list[str] = []
    params: list[str] = []

    def match(pattern: str) -> str:
        stored = stored_glob_match(pattern)
        if stored is None:
            return "FALSE"
        sql, regexes = stored
        params.extend(regexes)
        return sql

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
