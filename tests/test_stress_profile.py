"""Upload stress profile contract, without requiring PostgreSQL or importing app."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.stress_profile import stress_file_count


class StressProfileTests(unittest.TestCase):
    def test_explicit_local_run_defaults_to_full_acceptance(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(stress_file_count(1024), 1024)
            self.assertEqual(stress_file_count(2048), 2048)

    def test_pr_profile_exercises_four_jobs_on_each_of_sixteen_workers(self):
        with patch.dict(os.environ, {"FROSTVAULT_STRESS_PROFILE": "pr"}, clear=True):
            for full_count in (1024, 2048):
                count = stress_file_count(full_count)
                self.assertEqual(count, 64)
                batches = [list(range(count))[i::16] for i in range(16)]
                self.assertEqual([len(batch) for batch in batches], [4] * 16)

    def test_full_profile_keeps_both_acceptance_workloads(self):
        with patch.dict(os.environ, {"FROSTVAULT_STRESS_PROFILE": "full"}, clear=True):
            self.assertEqual(stress_file_count(1024), 1024)
            self.assertEqual(stress_file_count(2048), 2048)

    def test_unknown_or_empty_profile_fails_closed(self):
        for profile in ("", "PR", "bounded", "none", "full ", "0"):
            with self.subTest(profile=profile), patch.dict(
                os.environ, {"FROSTVAULT_STRESS_PROFILE": profile}, clear=True
            ), self.assertRaisesRegex(ValueError, "FROSTVAULT_STRESS_PROFILE"):
                stress_file_count(1024)


if __name__ == "__main__":
    unittest.main()
