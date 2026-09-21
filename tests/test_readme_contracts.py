"""Documentation contracts for public README role wording."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


class ReadmeUsersRoleDocsTests(unittest.TestCase):
    """BUG-005: README must not advertise operator free-space (REQ-003)."""

    def test_bug_005_readme_owner_only_free_space(self) -> None:
        """[BUG-005][Req: REQ-003] README must match owner-only free-space.

        Desired: operator capabilities exclude free local space.
        Previously: README Users claimed operator can free local space.
        Authoritative runtime: vault_roles.is_owner; /api/free-space owner gate.
        """
        self.assertNotRegex(
            README,
            r"operator`[^.\n]*free",
            "README must not claim operators can free local space",
        )
        self.assertRegex(
            README,
            re.compile(r"free local space[^\n]*owner", re.IGNORECASE),
            "README must say free local space is owner-only",
        )


if __name__ == "__main__":
    unittest.main()
