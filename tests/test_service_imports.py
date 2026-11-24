"""#858: the shape of the imports between ``app`` modules and ``app/services``.

No module of ``app`` reaches for a private name of a service, neither imported
(``from .x import _name``) nor through the module (``x._name``). And the import
cycles broken by #858 stay broken, counting imports inside functions too.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _module(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


MODULES = {_module(path): path for path in APP.rglob("*.py")}


def _base(module: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = module.split(".")
    if MODULES[module].name != "__init__.py":
        package = package[:-1]
    package = package[: len(package) - (node.level - 1)]
    return ".".join(package + ([node.module] if node.module else []))


def _imports(module: str):
    """``(imported module, name or None, node)`` for every import, also inside functions."""
    for node in ast.walk(ast.parse(MODULES[module].read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, None, alias.asname or alias.name, node
        elif isinstance(node, ast.ImportFrom):
            base = _base(module, node)
            for alias in node.names:
                if f"{base}.{alias.name}" in MODULES:
                    yield f"{base}.{alias.name}", None, alias.asname or alias.name, node
                else:
                    yield base, alias.name, alias.asname or alias.name, node


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


class ServicePrivateNameTests(unittest.TestCase):
    def test_no_app_module_uses_a_private_name_of_another_service(self):
        found = []
        for module, path in MODULES.items():
            bound = {}
            for target, name, local, node in _imports(module):
                if not target.startswith("app.services.") or target == module:
                    continue
                if name is None:
                    bound[local] = target
                elif _private(name):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} {target}.{name}")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id in bound and _private(node.attr)):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} "
                                 f"{bound[node.value.id]}.{node.attr}")
        self.assertEqual(found, [])


class BrokenImportCycleTests(unittest.TestCase):
    """Each pair: the first module never imports the second, even inside a function."""

    BROKEN = (
        # The Vault lock of the deletion decisions lives apart (vault_row_lock).
        ("app.services.vault_moves", "app.services.cloud_deletion"),
        # The minimum storage durations belong to minimum_storage.
        ("app.services.minimum_storage", "app.services.vault_moves"),
        # The restore estimate with list prices lives with the prices.
        ("app.services.restore_estimates", "app.services.cost_estimates"),
        # The read scheduling status with device rates lives with the meter.
        ("app.services.source_devices", "app.services.throughput"),
        # The retention read probe of a bucket lives with bucket provisioning.
        ("app.services.bucket_provisioning", "app.services.retention_lock"),
    )

    def test_broken_cycles_stay_broken(self):
        for importer, imported in self.BROKEN:
            with self.subTest(importer=importer):
                targets = {target for target, _, _, _ in _imports(importer)}
                self.assertNotIn(imported, targets)

    def test_the_vault_row_lock_imports_no_deletion_service(self):
        targets = {target for target, _, _, _ in _imports("app.services.vault_row_lock")}
        self.assertLessEqual(
            {t for t in targets if t.startswith("app.")},
            {"app.errors", "app.services.vault_locks", "app.services.vaults"},
        )


if __name__ == "__main__":
    unittest.main()
