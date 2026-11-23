"""#802: the path-rule semantics every caller relied on before the module moved.

``tests/fixtures/path_rules_golden.json`` was recorded from the code at commit
4a778eee (four separate implementations) and must not change with a refactor:
the same file passed before and after ``app/services/path_rules.py`` took
ownership of the glob semantics. It pins, for one corpus of patterns and paths
(segments, ``**``, regex/LIKE special characters, case, trailing slashes, empty
and root paths, unicode):

- the Python admission matcher (single glob and the whole admission rule);
- its SQL projection, evaluated by PostgreSQL;
- the Excluded Folder rules, in Python and in SQL (PostgreSQL);
- glob normalization, validation, and the SQL text the upload rules emit.

Where the implementations disagree with each other the disagreement is listed
in ``KNOWN_*_DIVERGENCES`` below: it is today's behavior, documented, not
endorsed. A change there is a behavior change and belongs to its own issue.

Record again (only for a deliberate, reviewed behavior change):
``TEST_POSTGRES_URL=... python -m tests.test_path_rules_characterization --record``
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Callable

from app.services.excluded_folders import excluded_folders
from app.services.operation_policies import (
    AutoUploadPathMatcher, OperationPolicy, auto_upload_path_is_included, path_is_included,
)
from app.services.path_rules import (
    GlobSet, ParsedPath, UnsafeGlob, folder_rule, normalize_globs, normalized_path_sql,
    path_matches, sql_glob_match, sql_path_regex, validate_glob,
)
from app.services.upload_path_rules import eligibility_sql, inline_eligibility_sql
from tests.fixtures import empty_database

GOLDEN = Path(__file__).parent / "fixtures" / "path_rules_golden.json"

PATTERNS = [
    # Segments.
    "*.jpg", "foo", "photos/*.jpg", "photos/*", "a/b", "?", "??", "*", "a*b*c", "foo*bar",
    "._*", ".DS_Store", "*.lock",
    # "**" across segments.
    "**", "**/*", "photos/**", "**/*.jpg", "**/a/b/**", "**/node_modules/**", "a/**/b",
    "a/**/b/**", "**/x/*.jpg", "**/**", "ph*/**", "**/.cache*/**", "tmp?/**", "a/*/c/**",
    "**foo", "foo**", "a/**", "**/a/**", "**/b",
    # Characters special to regular expressions and to LIKE.
    "100%*.raw", "a_b", "a.b", "a+b", "(x)", "we{ird}.*", "x&y~z", "$x", "^x", "a|b",
    "it's/**", "[ab]*", "[!x]*", "[^x]*", "[ab", "a[[]1].jpg", "[]abc]*", "[a-z[:digit:]]*",
    "[[:alpha:]]*", "[a\\-z]", "[z-a]*", "[ab]/**", "a\\b/**", "back\\ups/**",
    # Case.
    "*.JPG", "Photos/**", "[A-Z]*", "FOO",
    # Trailing slashes.
    "photos/", "photos/**/", "**/", "dir/*/",
    # Empty, root and dot paths.
    "", "/", ".", "./x", "../x/**", "a//b/**", "/abs/**", "./**",
    # Unicode.
    "é*", "**/日本語/**", "*.ÉXT", "caf?", "😀/**", "nä*", "**/é.txt",
    # Whitespace.
    "a b/**", "a\nb*", " lead/**",
]

PATHS = [
    "a.jpg", "photos/a.jpg", "photos/sub/a.jpg", "Photos/a.JPG", "a/b", "a/b/c", "x/a/b/c/y",
    "a/x/b", "a/x/y/b/z", "foo", "x/foo/y", "foobar", "foo/a/bar/x", ".DS_Store", "x/._foo",
    "db.lock/a.jpg", "node_modules/x", "src/node_modules/p/i.js", "home/.cache-v2/f",
    "tmp1/x", "tmp12/x", "a/b/c/d.txt", "x/y.jpg", "p/x/y.jpg",
    "100%.raw", "100x.raw", "a_b", "axb", "a.b", "a+b", "(x)", "we{ird}.x", "x&y~z", "$x",
    "^x", "a|b", "it's/x", "a[1].jpg", "[", "]", "a[b", "-", "a-b", "1", "A1", "b1", "z",
    "FOO", "a.JPG",
    "photos/", "photos", "photos//a.jpg", "dir/x/", "a/b/",
    "", "/", ".", "./x", "/abs/x", "..", "../x/y", "a/./b", "a//b/c", "//",
    "é", "école/é.txt", "café", "café", "日本語/x", "x/日本語/y/z", "a.ÉXT", "a.éxt",
    "😀/x", "nä", "näx",
    "a b/c", " lead/x", "a\nb.jpg", "a\\b.jpg", "a\\b/c", "foo\\._bar", "back\\ups/x", "a\tb",
]

# Rule sets whose SQL text is pinned: an unchanged statement keeps its query plan.
SQL_POLICIES: dict[str, OperationPolicy | None] = {
    "none": None,
    "default": OperationPolicy(),
    "include_plain": OperationPolicy(include_globs=("*.jpg", "*.raw")),
    "include_starred": OperationPolicy(include_globs=("photos/**", "**/*.jpg")),
    "exclude_folders": OperationPolicy(exclude_globs=("backups/**", "**/node_modules/**", "*.tmp")),
    "mixed": OperationPolicy(
        include_globs=("**/*.jpg", "100%*.raw", "it's/**"),
        exclude_globs=("**/.cache*/**", "[!x]*", "a[[]1].jpg", "é*"),
    ),
    "unsafe_stored": OperationPolicy(include_globs=("[z-a]*", "ok*"), exclude_globs=("tmp*",)),
}

NORMALIZE_INPUTS: list[list[Any]] = [
    [], ["*.jpg"], ["  photos/**  ", "", "   "], ["a\\b\\**", "x\\*.jpg"], ["./x", "a//b", "photos/"],
    ["/abs/**"], [" /abs"], ["\\abs"], ["é*", "É*", "é*"], [1, None], ["a", "a"],
]

# --- Divergences that exist today (4a778eee); see the module docstring. -------------
# The Python matcher and its SQL translation (one glob, and the admission rule built
# on it), as (pattern, path) the two read differently.
# Python parses the path with PurePosixPath (collapsing "//", "./" and a trailing "/",
# keeping "/" as the root part of an absolute path); SQL reads the stored text and
# splits it on "/". Catalog paths are relative and normalized, so none of these is
# reachable from a scan.
KNOWN_ADMISSION_DIVERGENCES: set[tuple[str, str]] = {
    # A trailing "/" or a doubled "//" disappears in Python, not in SQL.
    ("**/b", "a/b/"), ("**/a/b/**", "a//b/c"),
    # "." is a part for SQL only: PurePosixPath drops it.
    (".", "./x"), (".", "a/./b"),
    # "/" is the root part of an absolute path for Python only.
    ("/", "/abs/x"),
    # The empty glob (never stored: normalization drops it) matches the empty part SQL
    # gets from splitting "a//b", "a/" or "/a".
    ("", "photos/"), ("", "dir/x/"), ("", "a/b/"), ("", "/abs/x"), ("", "/"), ("", "//"),
    ("", "a//b/c"), ("", "photos//a.jpg"),
}
# Excluded Folder rules, Python (``contains_path``) against SQL (``not_inside_sql``).
KNOWN_FOLDER_DIVERGENCES: set[tuple[str, str]] = {
    # Python drops empty segments ("a//b", trailing "/"); the regex reads the text.
    ("photos/**", "photos/"), ("ph*/**", "photos/"), ("**/a/b/**", "a/b/"),
    ("**/a/b/**", "a//b/c"), ("a\\b/**", "a/b/"), ("a\\b/**", "a//b/c"),
}
# Paths an Excluded Folder rule reports inside the folder while admission, under the
# same single exclude glob, still includes them.
KNOWN_FOLDER_NOT_EXCLUDED_BY_ADMISSION: set[tuple[str, str]] = {
    # Only with a glob that skipped normalization: Excluded Folders normalize the
    # policy's globs ("a\\b/**" becomes "a/b/**"), path_is_included reads them as stored.
    # set_policy stores normalized globs, so a saved policy never holds this one.
    ("a\\b/**", "a/b/c"), ("a\\b/**", "a//b/c"), ("a\\b/**", "a/b/c/d.txt"),
}


def _bits(pattern: str, check: Callable[[str], bool]) -> str:
    try:
        return "".join("1" if check(path) else "0" for path in PATHS)
    except Exception as exc:  # the failure itself is behavior a caller sees
        return f"error:{type(exc).__name__}"


def _outcome(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:
        return f"error:{type(exc).__name__}"


def _folder_rule(pattern: str) -> Any:
    rule = folder_rule(pattern)
    return None if rule is None else {"segments": list(rule.segments), "floating": rule.floating}


def python_results() -> dict[str, Any]:
    def folders(pattern: str) -> Any:
        return excluded_folders(OperationPolicy(exclude_globs=(pattern,)))

    return {
        "path_matches": [_bits(p, lambda path: path_matches(path, p)) for p in PATTERNS],
        "admission_include_python": [
            _bits(p, lambda path: auto_upload_path_is_included(path, OperationPolicy(include_globs=(p,))))
            for p in PATTERNS],
        "admission_exclude_python": [
            _bits(p, lambda path: auto_upload_path_is_included(path, OperationPolicy(exclude_globs=(p,))))
            for p in PATTERNS],
        "folder_covers_python": [_bits(p, lambda path: folders(p).covers_directory(path)) for p in PATTERNS],
        "folder_contains_python": [_bits(p, lambda path: folders(p).contains_path(path)) for p in PATTERNS],
        "folder_rule": [_outcome(lambda: _folder_rule(p)) for p in PATTERNS],
        "sql_regex": [_outcome(lambda: sql_path_regex(p)) for p in PATTERNS],
        "validate_glob": [_outcome(lambda: validate_glob(p) or "ok") for p in PATTERNS],
        "normalize_globs": [_outcome(lambda: list(normalize_globs(values))) for values in NORMALIZE_INPUTS],
        "eligibility_sql": {
            name: {"sql": eligibility_sql(policy)[0], "params": eligibility_sql(policy)[1],
                   "inline": inline_eligibility_sql(policy)}
            for name, policy in SQL_POLICIES.items()},
        "not_inside_sql": {
            name: list(excluded_folders(policy).not_inside_sql("f.path"))
            for name, policy in SQL_POLICIES.items()},
    }


def _sql_bits(connection: Any, sql: str, params: list[Any]) -> str:
    rows = connection.execute(
        f"SELECT ({sql}) AS hit FROM unnest(%s::text[]) WITH ORDINALITY AS f(path, ord) ORDER BY ord",
        [*params, PATHS],
    ).fetchall()
    return "".join("1" if row["hit"] else "0" for row in rows)


def sql_results(connection: Any) -> dict[str, Any]:
    def folder_inside(pattern: str) -> str:
        try:
            sql, params = excluded_folders(OperationPolicy(exclude_globs=(pattern,))).not_inside_sql("f.path")
        except Exception as exc:
            return f"error:{type(exc).__name__}"
        return _sql_bits(connection, f"NOT ({sql})", params)

    return {
        "admission_include_sql": [
            _sql_bits(connection, *eligibility_sql(OperationPolicy(include_globs=(p,)))) for p in PATTERNS],
        "admission_exclude_sql": [
            _sql_bits(connection, *eligibility_sql(OperationPolicy(exclude_globs=(p,)))) for p in PATTERNS],
        "folder_inside_sql": [folder_inside(p) for p in PATTERNS],
    }


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _pairs(left: list[str], right: list[str]) -> set[tuple[str, str]]:
    """The (pattern, path) pairs two result tables answer differently."""
    found: set[tuple[str, str]] = set()
    for pattern, a, b in zip(PATTERNS, left, right):
        if a.startswith("error:") or b.startswith("error:"):
            if a != b:
                found.add((pattern, a + "|" + b))
            continue
        found.update((pattern, path) for path, x, y in zip(PATHS, a, b) if x != y)
    return found


class _Characterization(unittest.TestCase):
    maxDiff = None

    def assert_table(self, name: str, observed: list[Any]) -> None:
        expected = _golden()[name]
        self.assertEqual(len(observed), len(expected), name)
        for pattern, got, want in zip(PATTERNS, observed, expected):
            if got == want:
                continue
            if isinstance(got, str) and isinstance(want, str) and len(got) == len(want) == len(PATHS):
                changed = [path for path, x, y in zip(PATHS, got, want) if x != y]
                self.fail(f"{name}: pattern {pattern!r} changed for paths {changed!r}")
            self.assertEqual(got, want, f"{name}: pattern {pattern!r}")


class PythonCharacterizationTests(_Characterization):
    def test_the_golden_file_was_recorded_for_this_corpus(self):
        golden = _golden()
        self.assertEqual(golden["patterns"], PATTERNS)
        self.assertEqual(golden["paths"], PATHS)

    def test_python_behavior_is_unchanged(self):
        observed = python_results()
        for name in ("path_matches", "admission_include_python", "admission_exclude_python",
                     "folder_covers_python", "folder_contains_python", "folder_rule",
                     "sql_regex", "validate_glob"):
            with self.subTest(table=name):
                self.assert_table(name, observed[name])
        self.assertEqual(observed["normalize_globs"], _golden()["normalize_globs"])

    def test_emitted_sql_is_textually_unchanged(self):
        observed = python_results()
        golden = _golden()
        for name in SQL_POLICIES:
            with self.subTest(policy=name):
                self.assertEqual(observed["eligibility_sql"][name], golden["eligibility_sql"][name])
                self.assertEqual(observed["not_inside_sql"][name], golden["not_inside_sql"][name])

    def test_the_compiled_matcher_is_the_single_glob_matcher(self):
        """One Python semantics: compiled per glob set, same answers glob by glob."""
        for pattern in PATTERNS:
            globs = GlobSet((pattern,))
            for path in PATHS:
                normalized = path.replace("\\", "/")
                self.assertEqual(
                    globs.matches(normalized, ParsedPath(normalized)), path_matches(path, pattern),
                    (pattern, path))
        everything = GlobSet(PATTERNS)
        for path in PATHS:
            normalized = path.replace("\\", "/")
            self.assertEqual(
                everything.matches(normalized, ParsedPath(normalized)),
                any(path_matches(path, pattern) for pattern in PATTERNS), path)

    def test_the_per_cycle_matcher_is_the_admission_rule(self):
        for pattern in PATTERNS:
            for policy in (OperationPolicy(include_globs=(pattern,)), OperationPolicy(exclude_globs=(pattern,)),
                           OperationPolicy(include_globs=("**/*", pattern), exclude_globs=(pattern, "*.tmp"))):
                matcher = AutoUploadPathMatcher(policy)
                for path in PATHS:
                    self.assertEqual(matcher(path), auto_upload_path_is_included(path, policy), (pattern, path))

    def test_an_excluded_folder_is_excluded_by_admission(self):
        """ADR-0044: a pruned folder must hold only paths admission excludes."""
        golden = _golden()
        found = set()
        for pattern, inside in zip(PATTERNS, golden["folder_contains_python"]):
            if inside.startswith("error:"):
                continue
            policy = OperationPolicy(exclude_globs=(pattern,))
            found.update((pattern, path) for path, bit in zip(PATHS, inside)
                         if bit == "1" and path_is_included(path, policy))
        self.assertEqual(found, KNOWN_FOLDER_NOT_EXCLUDED_BY_ADMISSION)


class PostgresCharacterizationTests(_Characterization):
    """The SQL matchers on PostgreSQL, over the same corpus as the Python ones."""

    @classmethod
    def setUpClass(cls):
        database = cls.enterClassContext(empty_database())
        cls.connection = database.connect()
        cls.addClassCleanup(cls.connection.close)
        cls.observed = sql_results(cls.connection)

    def test_sql_behavior_is_unchanged(self):
        for name in ("admission_include_sql", "admission_exclude_sql", "folder_inside_sql"):
            with self.subTest(table=name):
                self.assert_table(name, self.observed[name])

    def test_one_glob_in_python_and_in_sql_agrees_except_where_pinned(self):
        """The equivalence of the two matchers of ``path_rules``, glob by glob."""
        found = set()
        for pattern in PATTERNS:
            try:
                sql, params = sql_glob_match(normalized_path_sql("f.path"), pattern)
            except UnsafeGlob:  # no SQL reading: the projection matches nothing
                continue
            hits = _sql_bits(self.connection, sql, list(params))
            found.update((pattern, path) for path, hit in zip(PATHS, hits)
                         if (hit == "1") != path_matches(path, pattern))
        self.assertEqual(found, KNOWN_ADMISSION_DIVERGENCES)

    def test_python_and_sql_admission_agree_except_where_pinned(self):
        python = python_results()
        found = _pairs(python["admission_include_python"], self.observed["admission_include_sql"])
        found |= _pairs(python["admission_exclude_python"], self.observed["admission_exclude_sql"])
        self.assertEqual(found, KNOWN_ADMISSION_DIVERGENCES)

    def test_python_and_sql_folder_rules_agree_except_where_pinned(self):
        python = python_results()
        found = _pairs(python["folder_contains_python"], self.observed["folder_inside_sql"])
        self.assertEqual(found, KNOWN_FOLDER_DIVERGENCES)


def _record() -> None:  # pragma: no cover - maintenance entry point
    with empty_database() as database:
        connection = database.connect()
        try:
            data = {"patterns": PATTERNS, "paths": PATHS, **python_results(), **sql_results(connection)}
        finally:
            connection.close()
    GOLDEN.write_text(json.dumps(data, ensure_ascii=True, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if "--record" in sys.argv:  # pragma: no cover
        _record()
    else:
        unittest.main()
