from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from app.branding import PRODUCT_NAME, PRODUCT_SLUG
from app.config import is_placeholder
from app.main import app


ROOT = Path(__file__).resolve().parents[1]


def git_available() -> bool:
    """True when ROOT is a Git checkout and the git binary can read it.

    A release tarball has no ``.git`` and a minimal image may lack ``git``:
    the hygiene checks then read the files from disk instead of failing.
    """
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        capture_output=True,
    )
    return result.returncode == 0


def git_paths(*args: str) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [Path(raw) for raw in result.stdout.decode("utf-8").split("\0") if raw]


def gitignore_rules() -> list[tuple[bool, bool, bool, str]]:
    """Parse the root .gitignore into (negated, anchored, dir_only, glob)."""
    rules: list[tuple[bool, bool, bool, str]] = []
    gitignore = ROOT / ".gitignore"
    if not gitignore.exists():
        return rules
    for line in gitignore.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        line = line.removeprefix("!")
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        rules.append((negated, anchored, dir_only, line.lstrip("/")))
    return rules


def ignored_on_disk(path: Path, is_dir: bool, rules) -> bool:
    """Minimal .gitignore evaluation for checkouts without Git."""
    ignored = False
    rel = path.as_posix()
    for negated, anchored, dir_only, glob in rules:
        if dir_only and not is_dir:
            continue
        if glob.startswith("**/"):
            # "**/name" matches at any depth.
            hit = fnmatch.fnmatchcase(path.name, glob[3:])
        elif anchored:
            hit = fnmatch.fnmatchcase(rel, glob)
        else:
            hit = fnmatch.fnmatchcase(path.name, glob)
        if hit:
            ignored = not negated
    return ignored


def disk_paths() -> list[Path]:
    rules = gitignore_rules()
    paths: list[Path] = []

    def walk(directory: Path) -> None:
        for entry in sorted(directory.iterdir()):
            rel = entry.relative_to(ROOT)
            if entry.name == ".git":
                continue
            if entry.is_dir() and not entry.is_symlink():
                if not ignored_on_disk(rel, True, rules):
                    walk(entry)
            elif entry.is_file() and not ignored_on_disk(rel, False, rules):
                paths.append(rel)

    walk(ROOT)
    return paths


def tracked_text() -> dict[Path, str]:
    if git_available():
        paths = git_paths("--cached", "--others", "--exclude-standard")
    else:
        paths = disk_paths()
    files: dict[Path, str] = {}
    for path in paths:
        try:
            content = (ROOT / path).read_bytes()
        except FileNotFoundError:
            # Deleted in the working tree but still in the index.
            continue
        if b"\0" not in content:
            files[path] = content.decode("utf-8-sig").replace("\r\n", "\n")
    return files


class PublicReleaseHygieneTests(unittest.TestCase):
    def test_examples_contain_no_real_email_aws_account_or_home_path(self) -> None:
        email = re.compile(
            r"[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})",
            re.IGNORECASE,
        )
        aws_account = re.compile(r"arn:aws:(?:iam|sts)::\d{12}:")
        home_paths = (
            re.compile(r"[A-Z]:\\" + "Users" + r"\\", re.IGNORECASE),
            re.compile("/" + r"(?:Users|home)" + "/"),
        )
        # Absolute host paths leak the layout of a development server. The
        # documented example host roots under /srv are placeholders.
        host_path = re.compile("/" + r"(?:mnt|srv)" + r"/[^\s`'\")]*")
        allowed_host_prefixes = (
            "/" + "srv/frostvault/",
            "/" + "srv/archive/",
        )
        findings: list[str] = []
        for path, text in tracked_text().items():
            # Lockfiles embed upstream npm "deprecated" notices (maintainer
            # contacts), which are not FrostVault configuration secrets.
            skip_email = path.name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
            for match in email.finditer(text):
                if skip_email:
                    continue
                if match.group(1).lower() != "example.com":
                    findings.append(f"{path}:{match.group(0)}")
            for pattern in (aws_account, *home_paths):
                findings.extend(
                    f"{path}:{match.group(0)}" for match in pattern.finditer(text)
                )
            for match in host_path.finditer(text):
                if not match.group(0).startswith(allowed_host_prefixes):
                    findings.append(f"{path}:{match.group(0)}")
        self.assertEqual(findings, [])

    def test_no_tracked_file_is_ignored_by_gitignore(self) -> None:
        # Files added before an ignore rule (or with `git add -f`) keep being
        # published in every release tarball although .gitignore excludes them.
        if not git_available():
            self.skipTest("no Git checkout: the index cannot be inspected")
        tracked_ignored = git_paths("--cached", "--ignored", "--exclude-standard")
        self.assertEqual([path.as_posix() for path in tracked_ignored], [])

    def test_frostvault_brand_and_apache_license_are_declared(self) -> None:
        files = tracked_text()
        self.assertEqual(PRODUCT_NAME, "FrostVault")
        self.assertEqual(PRODUCT_SLUG, "frostvault")
        self.assertEqual(app.title, PRODUCT_NAME)
        self.assertEqual(files[Path("README.md")].splitlines()[0], "# FrostVault")
        self.assertIn("  frostvault:\n", files[Path("compose.yaml")])
        self.assertIn("Apache License", files[Path("LICENSE")])
        self.assertIn("Version 2.0, January 2004", files[Path("LICENSE")])
        self.assertIn("Apache-2.0", files[Path("Dockerfile")])

    def test_example_configuration_values_fail_closed(self) -> None:
        for value in (
            "REPLACE-WITH-ACCESS-KEY",
            "CHANGE-ME-LOCAL-DEV-ONLY",
            "example-bucket",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_placeholder(value))
        self.assertFalse(is_placeholder("configured-value"))


if __name__ == "__main__":
    unittest.main()
