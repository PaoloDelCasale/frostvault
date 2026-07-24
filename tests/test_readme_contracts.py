"""Documentation contracts for public README role wording."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


class ReadmeUsersRoleDocsTests(unittest.TestCase):
    """BUG-005: README Users must not advertise operator free-space (REQ-003)."""

    def test_bug_005_readme_users_owner_only_free_space(self) -> None:
        """[BUG-005][Req: REQ-003] Users section must match owner-only free-space.

        Desired: operator capabilities exclude free local space.
        Previously: README Users claimed operator can free local space.
        Authoritative runtime: vault_roles.is_owner; /api/free-space owner gate.
        """
        users_start = README.index("## Users and vaults")
        users_end = README.index("## Local cleanup safety", users_start)
        users = README[users_start:users_end]
        self.assertNotRegex(
            users,
            r"operator`[^.\n]*free",
            "README Users must not claim operators can free local space",
        )
        # Local cleanup safety remains the authoritative owner-only wording.
        self.assertIn("for owners, and when", README)


if __name__ == "__main__":
    unittest.main()
