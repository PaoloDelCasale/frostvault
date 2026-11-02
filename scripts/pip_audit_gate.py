#!/usr/bin/env python3
"""Decide whether a pip-audit JSON report should fail CI.

Two modes, mirroring ``frontend/scripts/npm-audit-gate.mjs``:

* strict (default): any known vulnerability fails. Used on ``main`` and on the
  scheduled run, which report drift through a tracking issue.
* baseline (``--baseline``): used on pull requests. Vulnerabilities that the
  base branch already has are reported as pre-existing and never fail the PR,
  so an advisory published after a PR was opened (or against a dependency the PR
  does not touch) cannot block unrelated merges such as Dependabot updates.
  Only vulnerabilities the change introduces fail.

Usage:
    pip-audit -r requirements.txt --format json --output head.json || true
    python scripts/pip_audit_gate.py --head head.json [--baseline base.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class AuditReportError(ValueError):
    """The pip-audit report is missing or not usable; the gate must fail closed."""


def load_report(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditReportError(f"{label} report not readable: {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditReportError(f"{label} report is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("dependencies"), list):
        raise AuditReportError(f"{label} report has no dependencies list: {path}")
    return payload


def collect_vulnerabilities(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map vulnerability id -> {package, version, fix} (first occurrence wins)."""
    found: dict[str, dict[str, str]] = {}
    for dependency in report["dependencies"]:
        if not isinstance(dependency, dict):
            continue
        for vuln in dependency.get("vulns") or []:
            identifier = str(vuln.get("id") or "").strip()
            if not identifier:
                continue
            fixes = vuln.get("fix_versions") or []
            found.setdefault(
                identifier,
                {
                    "package": str(dependency.get("name") or "?"),
                    "version": str(dependency.get("version") or "?"),
                    "fix": ", ".join(str(item) for item in fixes) or "none",
                },
            )
    return found


def evaluate(
    head: dict[str, Any], baseline: dict[str, Any] | None = None
) -> tuple[bool, list[str]]:
    current = collect_vulnerabilities(head)
    known = set(collect_vulnerabilities(baseline)) if baseline is not None else set()
    new = {key: value for key, value in current.items() if key not in known}
    preexisting = {key: value for key, value in current.items() if key in known}

    def describe(items: dict[str, dict[str, str]]) -> list[str]:
        return [
            f"  - {key} {value['package']}=={value['version']} (fix: {value['fix']})"
            for key, value in sorted(items.items())
        ]

    lines = ["pip audit gate"]
    if baseline is not None:
        lines.append("  mode: baseline (only vulnerabilities new to this change fail)")
        lines.append(f"  pre-existing on base (not blocking): {len(preexisting)}")
        lines.extend(f"  {line.strip()}" for line in describe(preexisting))
    lines.append(f"  blocking vulnerabilities: {len(new)}")
    ok = not new
    if ok:
        lines.append("  status: pass")
    else:
        lines.append("  status: fail")
        lines.append("pip audit gate failed:")
        lines.extend(describe(new))
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--head", required=True, type=Path, help="pip-audit JSON for this checkout")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="pip-audit JSON for the base branch; enables pull-request mode",
    )
    args = parser.parse_args(argv)
    try:
        head = load_report(args.head, "head")
        baseline = load_report(args.baseline, "baseline") if args.baseline else None
    except AuditReportError as exc:
        print(f"pip audit gate error: {exc}", file=sys.stderr)
        return 2
    ok, lines = evaluate(head, baseline)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
