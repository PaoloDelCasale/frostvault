"""POSIX vault filesystem preflight (issue #9).

Seams:
- app.services.fs_preflight.check_vault_filesystem — public diagnostics
  for vault root access, effective identity, unreadable files, unwritable
  directories, and symbolic links. Never mutates ownership or modes.
- app.services.fs_preflight.resolve_configured_vault_root — only returns
  roots that stay under operator-configured allowed bases.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.fs_preflight import (
    check_vault_filesystem,
    resolve_configured_vault_root,
)


class VaultFilesystemPreflightTests(unittest.TestCase):
    def test_healthy_root_passes_and_reports_effective_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("ok", encoding="utf-8")
            result = check_vault_filesystem(root)
            self.assertTrue(result.ok)
            self.assertEqual(result.uid, os.geteuid())
            self.assertEqual(result.gid, os.getegid())
            access = next(c for c in result.checks if c.code == "fs.root_access")
            self.assertEqual(access.status, "pass")
            identity = next(c for c in result.checks if c.code == "fs.identity")
            self.assertEqual(identity.status, "pass")
            self.assertIn(str(os.geteuid()), identity.message)
            self.assertEqual(result.findings, ())

    def test_missing_root_fails_with_remediation(self) -> None:
        missing = Path("/tmp/does-not-exist-vault-preflight-issue-9")
        result = check_vault_filesystem(missing)
        self.assertFalse(result.ok)
        access = next(c for c in result.checks if c.code == "fs.root_access")
        self.assertEqual(access.status, "fail")
        self.assertIn("not available", access.message.lower())
        self.assertTrue(access.remediation)

    def test_unreadable_file_is_reported_precisely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "secret.bin"
            secret.write_bytes(b"secret")
            secret.chmod(0o000)
            try:
                result = check_vault_filesystem(root)
            finally:
                secret.chmod(0o600)
            self.assertFalse(result.ok)
            finding = next(f for f in result.findings if f.path == "secret.bin")
            self.assertEqual(finding.code, "fs.unreadable_file")
            self.assertIn("unreadable", finding.message.lower())

    def test_unwritable_directory_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "locked"
            nested.mkdir()
            nested.chmod(0o555)
            try:
                result = check_vault_filesystem(root)
            finally:
                nested.chmod(0o755)
            self.assertFalse(result.ok)
            finding = next(f for f in result.findings if f.path == "locked")
            self.assertEqual(finding.code, "fs.unwritable_directory")

    def test_symlink_is_reported_and_not_treated_as_regular_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.txt"
            target.write_text("data", encoding="utf-8")
            link = root / "alias.txt"
            link.symlink_to(target)
            result = check_vault_filesystem(root)
            finding = next(f for f in result.findings if f.path == "alias.txt")
            self.assertEqual(finding.code, "fs.symlink")
            self.assertIn("symbolic link", finding.message.lower())
            # Symlinks make the vault unhealthy for archive operations.
            self.assertFalse(result.ok)

    def test_preflight_never_changes_ownership_or_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            restricted = root / "readonly.txt"
            restricted.write_text("x", encoding="utf-8")
            restricted.chmod(0o400)
            before_mode = restricted.stat().st_mode
            before_uid = restricted.stat().st_uid
            check_vault_filesystem(root)
            after = restricted.stat()
            self.assertEqual(after.st_mode, before_mode)
            self.assertEqual(after.st_uid, before_uid)

    def test_root_without_write_access_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Drop write on the root itself.
            root.chmod(0o555)
            try:
                if os.access(root, os.W_OK):
                    self.skipTest("running as root; cannot simulate unwritable root")
                result = check_vault_filesystem(root)
            finally:
                root.chmod(0o755)
            self.assertFalse(result.ok)
            access = next(c for c in result.checks if c.code == "fs.root_access")
            self.assertEqual(access.status, "fail")
            self.assertIn("write", access.message.lower())


class ConfiguredVaultRootResolutionTests(unittest.TestCase):
    def test_accepts_root_nested_under_allowed_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            vault = base / "vault-a"
            vault.mkdir()
            resolved = resolve_configured_vault_root(vault, allowed_bases=[base])
            self.assertEqual(resolved, vault.resolve())

    def test_accepts_exact_allowed_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            resolved = resolve_configured_vault_root(base, allowed_bases=[base])
            self.assertEqual(resolved, base.resolve())

    def test_rejects_root_outside_allowed_bases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "allowed"
            base.mkdir()
            outside = Path(directory) / "elsewhere"
            outside.mkdir()
            self.assertIsNone(
                resolve_configured_vault_root(outside, allowed_bases=[base])
            )

    def test_rejects_prefix_spoof_without_separator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "sources"
            base.mkdir()
            spoof = Path(directory) / "sources_evil"
            spoof.mkdir()
            self.assertIsNone(
                resolve_configured_vault_root(spoof, allowed_bases=[base])
            )


if __name__ == "__main__":
    unittest.main()
