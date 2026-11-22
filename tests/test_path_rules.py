"""#802: ``path_rules`` is the leaf that owns the glob semantics.

Its behavior is pinned by ``tests.test_path_rules_characterization``; here only
the shape of the dependency: it imports no other service, and no module reaches
for its private names or imports a sibling inside a function to dodge a cycle.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"
ROOT = SERVICES.parents[1]


def _imports(path: Path) -> list[tuple[ast.ImportFrom, bool]]:
    """Every ``from ... import`` of a file, and whether it sits at module level."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = {id(node) for node in tree.body}
    return [(node, id(node) in top) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]


class PathRulesDependencyTests(unittest.TestCase):
    def test_path_rules_imports_no_application_module(self):
        for node, _ in _imports(SERVICES / "path_rules.py"):
            self.assertEqual(node.level, 0, ast.unparse(node))
            self.assertFalse((node.module or "").startswith("app"), ast.unparse(node))

    def test_nothing_imports_a_private_name_from_path_rules(self):
        for path in [*ROOT.glob("app/**/*.py"), *ROOT.glob("tests/**/*.py")]:
            for node, _ in _imports(path):
                if (node.module or "").split(".")[-1] != "path_rules":
                    continue
                private = [alias.name for alias in node.names if alias.name.startswith("_")]
                self.assertEqual(private, [], f"{path.relative_to(ROOT)}: {ast.unparse(node)}")

    def test_the_policy_and_its_sql_projection_do_not_import_each_other(self):
        policy = [node.module for node, _ in _imports(SERVICES / "operation_policies.py")]
        self.assertNotIn("upload_path_rules", policy)
        for name in ("upload_path_rules.py", "excluded_folders.py", "vault_exclusions.py"):
            for node, at_module_level in _imports(SERVICES / name):
                if node.module in {"path_rules", "operation_policies", "upload_path_rules", "excluded_folders"}:
                    self.assertTrue(at_module_level, f"{name}: {ast.unparse(node)}")


if __name__ == "__main__":
    unittest.main()
