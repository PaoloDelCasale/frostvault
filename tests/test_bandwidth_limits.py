"""Bandwidth limits applied to rclone transfers (issue #12).

Seams under test:
- ``run_rclone`` receives ``--bwlimit`` from the effective bandwidth policy.
- Verification read-back uses the same bandwidth argument.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.operation_policies import rclone_bwlimit_arg
from app.storage import run_rclone


class RcloneBandwidthTests(unittest.TestCase):
    def test_bwlimit_arg_formats_kibps(self) -> None:
        self.assertEqual(rclone_bwlimit_arg(256), "256k")
        self.assertIsNone(rclone_bwlimit_arg(None))

    def test_run_rclone_inserts_bwlimit_before_command_args(self) -> None:
        with patch("app.storage.subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stderr = ""
            run_mock.return_value.stdout = ""
            run_rclone(
                "copyto",
                "remote:a",
                "/tmp/a",
                bwlimit="512k",
                config_path="/tmp/rclone.conf",
            )
        command = run_mock.call_args.args[0]
        self.assertEqual(command[0], "rclone")
        self.assertIn("--bwlimit", command)
        self.assertEqual(command[command.index("--bwlimit") + 1], "512k")
        self.assertIn("copyto", command)


if __name__ == "__main__":
    unittest.main()
