from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from app.branding import PRODUCT_NAME, PRODUCT_SLUG
from app.config import is_placeholder
from app.main import app


ROOT = Path(__file__).resolve().parents[1]
CODEOWNERS = Path(".github/CODEOWNERS")
HISTORICAL_OWNER = ("Pa" + "olo") + "DelCasale"


def tracked_text() -> dict[Path, str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    files: dict[Path, str] = {}
    for raw_path in result.stdout.decode("utf-8").split("\0"):
        if not raw_path:
            continue
        path = Path(raw_path)
        content = (ROOT / path).read_bytes()
        if b"\0" not in content:
            files[path] = content.decode("utf-8-sig").replace("\r\n", "\n")
    return files


class PublicReleaseHygieneTests(unittest.TestCase):
    def test_personal_and_legacy_names_are_absent(self) -> None:
        forbidden = (
            re.compile(r"\bpa" + r"olo\b", re.IGNORECASE),
            re.compile(r"\bga" + r"ia\b", re.IGNORECASE),
            re.compile("personal " + "s3 archive", re.IGNORECASE),
            re.compile("personal" + r"[-_ ]s3[-_ ]archive", re.IGNORECASE),
            re.compile("s3" + "glacierrsync", re.IGNORECASE),
            re.compile("s3" + "gr_", re.IGNORECASE),
        )
        findings: list[str] = []
        for path, text in tracked_text().items():
            for pattern in forbidden:
                for match in pattern.finditer(text):
                    findings.append(f"{path}:{match.group(0)}")
        self.assertEqual(findings, [])

    def test_only_historical_repository_codeowners_use_owner_name(self) -> None:
        owner = re.compile(re.escape(HISTORICAL_OWNER), re.IGNORECASE)
        findings = [
            (path, match.group(0))
            for path, text in tracked_text().items()
            for match in owner.finditer(text)
        ]
        self.assertEqual(len(findings), 7)
        self.assertTrue(all(path == CODEOWNERS for path, _ in findings), findings)

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
        findings: list[str] = []
        for path, text in tracked_text().items():
            for match in email.finditer(text):
                if match.group(1).lower() != "example.com":
                    findings.append(f"{path}:{match.group(0)}")
            for pattern in (aws_account, *home_paths):
                findings.extend(
                    f"{path}:{match.group(0)}" for match in pattern.finditer(text)
                )
        self.assertEqual(findings, [])

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
