"""Contract for scripts/pip_audit_gate.py: strict vs pull-request baseline mode."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pip_audit_gate as gate  # noqa: E402


def report(*vulns: tuple[str, str, str, str]) -> dict:
    """Build a pip-audit style report from (package, version, id, fix) tuples."""
    by_package: dict[tuple[str, str], list[dict]] = {}
    for package, version, ident, fix in vulns:
        by_package.setdefault((package, version), []).append(
            {"id": ident, "fix_versions": [fix] if fix else [], "aliases": []}
        )
    dependencies = [
        {"name": name, "version": version, "vulns": items}
        for (name, version), items in by_package.items()
    ]
    dependencies.append({"name": "clean", "version": "1.0.0", "vulns": []})
    return {"dependencies": dependencies, "fixes": []}


SETUPTOOLS = ("setuptools", "84.0.0", "GHSA-xmj3-9gf7-h52w", "84.1.0")
OTHER = ("other", "2.0.0", "PYSEC-2026-1", "2.0.1")


class StrictModeTests(unittest.TestCase):
    def test_clean_report_passes(self) -> None:
        ok, lines = gate.evaluate(report())
        self.assertTrue(ok)
        self.assertIn("  status: pass", lines)

    def test_any_vulnerability_fails(self) -> None:
        ok, lines = gate.evaluate(report(SETUPTOOLS))
        self.assertFalse(ok)
        self.assertTrue(any("GHSA-xmj3-9gf7-h52w setuptools==84.0.0" in line for line in lines))


class BaselineModeTests(unittest.TestCase):
    def test_vulnerability_already_on_base_does_not_block(self) -> None:
        ok, lines = gate.evaluate(report(SETUPTOOLS), report(SETUPTOOLS))
        self.assertTrue(ok)
        self.assertIn("  pre-existing on base (not blocking): 1", lines)

    def test_new_vulnerability_still_blocks(self) -> None:
        ok, lines = gate.evaluate(report(SETUPTOOLS, OTHER), report(SETUPTOOLS))
        self.assertFalse(ok)
        blocking = lines[lines.index("pip audit gate failed:") + 1 :]
        self.assertTrue(any("PYSEC-2026-1 other==2.0.0" in line for line in blocking))
        self.assertFalse(any("GHSA-xmj3" in line for line in blocking))

    def test_same_advisory_on_a_bumped_version_is_not_new(self) -> None:
        bumped = ("setuptools", "84.0.1", "GHSA-xmj3-9gf7-h52w", "84.1.0")
        ok, _ = gate.evaluate(report(bumped), report(SETUPTOOLS))
        self.assertTrue(ok)


class MainTests(unittest.TestCase):
    def _write(self, directory: str, name: str, payload: object) -> Path:
        path = Path(directory) / name
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )
        return path

    def test_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            head = self._write(tmp, "head.json", report(SETUPTOOLS))
            base = self._write(tmp, "base.json", report(SETUPTOOLS))
            clean = self._write(tmp, "clean.json", report())
            self.assertEqual(gate.main(["--head", str(head)]), 1)
            self.assertEqual(gate.main(["--head", str(head), "--baseline", str(base)]), 0)
            self.assertEqual(gate.main(["--head", str(clean)]), 0)

    def test_unusable_reports_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = self._write(tmp, "good.json", report())
            broken = self._write(tmp, "broken.json", "not json")
            empty = self._write(tmp, "empty.json", {"unexpected": True})
            missing = Path(tmp) / "missing.json"
            for bad in (broken, empty, missing):
                with self.subTest(report=bad.name):
                    self.assertEqual(gate.main(["--head", str(bad)]), 2)
                    self.assertEqual(
                        gate.main(["--head", str(good), "--baseline", str(bad)]), 2
                    )


if __name__ == "__main__":
    unittest.main()
