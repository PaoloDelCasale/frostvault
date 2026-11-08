"""DB-free checks of the glob -> PostgreSQL regex projection (#498); parity with
real PostgreSQL runs in test_upload_activity_postgres."""
from __future__ import annotations

import logging
import re
import unittest

from app.services.operation_policies import (
    OperationPolicy, _path_matches, validate_policy_globs,
)
from app.services.upload_path_rules import (
    MAX_GLOB_LENGTH, MAX_GLOBS_PER_POLICY, PATH_SQL, UnsafeGlob, _stored_regex, eligibility_sql,
    inline_eligibility_sql, path_pattern, validate_glob,
)

# Globs that Python accepts but PostgreSQL rejected or misread before the
# translation emitted only POSIX ARE both engines read the same way.
NASTY = ['[a-z[:digit:]]*', '[[:alpha:]]*', '[[.a.]]*', '[[=a=]]*', 'a[[]1].jpg', '[]abc]*',
         '[!]]*', '[^x]*', '[&|~x]*', 'a[b-d-f]', '[a\\-z]', '[!x]*', '*a*b*c', 'x&y~z', '[ab',
         'we{ird}.*', '100%*.raw', "it's/**", '**/*.jpg', 'photos/**',
         '[(?>]x', '[(?!)]x', '[(?:]x', '(?>x)*']
PATHS = ['a.jpg', 'photos/a.jpg', 'x/y/z', ':', 'a:b', '.', '=', '[', ']', '1', 'a1', 'we{ird}.x',
         '100%.raw', "it's/x", 'a[b', '&', '~', '|', '-', 'a-b', 'é', '>x', ':x', '(x', '?x', '!x', ')x', '(?>x)a', '(?!)x']


class GlobTranslationTests(unittest.TestCase):
    def test_translation_never_leaves_a_posix_class_opener_in_a_set(self):
        for glob in NASTY:
            with self.subTest(glob=glob):
                regex = path_pattern(glob)
                self.assertIsNone(re.search(r'(?<!\\)\[[:.=]', regex), regex)
                re.compile(regex)

    def test_translation_keeps_python_glob_semantics(self):
        for glob in NASTY:
            regex = re.compile(path_pattern(glob))
            for path in PATHS:
                observed = bool(regex.search(path))
                if '**' not in glob:
                    observed |= any(bool(regex.search(part)) for part in path.split('/'))
                self.assertEqual(observed, _path_matches(path, glob), (glob, path))

    def test_unsupported_globs_are_refused(self):
        for glob in ('[z-a]*', 'a\x00b', '\ud800'):
            with self.subTest(glob=repr(glob)):
                with self.assertRaises(ValueError):
                    validate_glob(glob)
        for glob in NASTY:
            validate_glob(glob)

    def test_group_rewrite_and_empty_range_check_ignore_set_contents(self):
        # "(?>" and "(?!)" are only syntax outside a set; inside one they are characters.
        self.assertIn('[(?>]', path_pattern('[(?>]x'))
        self.assertIn('[(?!)]', path_pattern('[(?!)]x'))
        self.assertNotIn('(?>', path_pattern('*a*'))
        self.assertIn('(?:', path_pattern('*a*'))
        validate_glob('[(?!)]x')
        with self.assertRaises(UnsafeGlob):
            validate_glob('[z-a]x')

    def test_long_globs_are_refused_when_new_and_ignored_when_stored(self):
        long_glob = '*a' * (MAX_GLOB_LENGTH // 2) + '*'
        validate_glob('*a' * (MAX_GLOB_LENGTH // 2))
        validate_glob('**/' + '*a' * ((MAX_GLOB_LENGTH - 6) // 2) + '/**')
        with self.assertRaises(UnsafeGlob):
            validate_glob(long_glob)
        with self.assertRaises(UnsafeGlob):
            validate_glob('a' * (MAX_GLOB_LENGTH + 1))
        with self.assertRaises(ValueError):
            validate_policy_globs(OperationPolicy(exclude_globs=(long_glob,)))
        # Too many globs: refused only when the save adds one.
        many = tuple(f'g{index}*' for index in range(MAX_GLOBS_PER_POLICY + 1))
        with self.assertRaises(ValueError):
            validate_policy_globs(OperationPolicy(exclude_globs=many))
        stored = OperationPolicy(exclude_globs=many)
        validate_policy_globs(OperationPolicy(exclude_globs=many, stability_seconds=9), stored=stored)
        # A glob already stored beyond the limit classifies nothing and does not break SQL.
        sql, params = eligibility_sql(OperationPolicy(exclude_globs=(long_glob,)))
        self.assertIn('FALSE', sql)
        self.assertTrue(all(len(value) < 4 * MAX_GLOB_LENGTH for value in params))

    def test_an_unsupported_stored_glob_warns_once_not_on_every_query(self):
        _stored_regex.cache_clear()
        policy = OperationPolicy(exclude_globs=('[z-a]*',))
        with self.assertLogs('app.services.upload_path_rules', level=logging.WARNING) as logs:
            for _ in range(5):
                eligibility_sql(policy)
        self.assertEqual(len(logs.records), 1)

    def test_saving_refuses_new_unsupported_globs_but_keeps_stored_ones_editable(self):
        stored = OperationPolicy(exclude_globs=('[z-a]*',))
        with self.assertRaises(ValueError):
            validate_policy_globs(OperationPolicy(exclude_globs=('[z-a]*',)))
        with self.assertRaises(ValueError):
            validate_policy_globs(OperationPolicy(include_globs=('a\x00',)), stored=stored)
        # An unrelated edit that still carries the old glob is not blocked...
        validate_policy_globs(OperationPolicy(exclude_globs=('[z-a]*',), stability_seconds=9), stored=stored)
        # ...and dropping it is always allowed.
        validate_policy_globs(OperationPolicy(), stored=stored)

    def test_a_stored_unsupported_glob_matches_nothing_instead_of_breaking_sql(self):
        policy = OperationPolicy(include_globs=('[z-a]*', 'ok*'), exclude_globs=('\x00', 'tmp*'))
        sql, params = eligibility_sql(policy)
        self.assertIn('FALSE', sql)
        # Only the two usable globs and the fixed exclusions contribute patterns.
        self.assertTrue(all('\x00' not in value for value in params))
        self.assertNotIn('%s', inline_eligibility_sql(policy))
        self.assertNotIn('\x00', inline_eligibility_sql(policy))

    def test_sql_reads_the_same_normalized_path_as_admission(self):
        sql, _ = eligibility_sql(None)
        self.assertIn(PATH_SQL, sql)
        self.assertNotIn('(fp.path ~', sql)


if __name__ == '__main__':
    unittest.main()
