"""Upload stress profile contract, without requiring PostgreSQL or importing app."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.stress_profile import stress_file_count


class StressProfileTests(unittest.TestCase):
    def test_explicit_local_run_defaults_to_full_acceptance(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(stress_file_count(256), 256)
            self.assertEqual(stress_file_count(512), 512)

    def test_pr_profile_exercises_four_jobs_on_each_of_sixteen_workers(self):
        with patch.dict(os.environ, {"FROSTVAULT_STRESS_PROFILE": "pr"}, clear=True):
            for full_count in (256, 512):
                count = stress_file_count(full_count)
                self.assertEqual(count, 64)
                batches = [list(range(count))[i::16] for i in range(16)]
                self.assertEqual([len(batch) for batch in batches], [4] * 16)

    def test_full_profile_keeps_both_acceptance_workloads(self):
        with patch.dict(os.environ, {"FROSTVAULT_STRESS_PROFILE": "full"}, clear=True):
            self.assertEqual(stress_file_count(256), 256)
            self.assertEqual(stress_file_count(512), 512)

    def test_profile_is_used_only_by_quantitative_bursts_not_deterministic_races(self):
        import ast
        from pathlib import Path
        tree = ast.parse(Path(__file__).with_name("test_aggregate_deadlocks.py").read_text())
        methods = {node.name: node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")}
        expected = {
            "test_16_workers_256_nested_uploads_with_concurrent_maintenance",
            "test_535_small_file_burst_keeps_throughput_and_reuses_connections",
        }
        profiled = {name for name, method in methods.items()
                    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                           and node.func.id == "stress_file_count" for node in ast.walk(method))}
        self.assertEqual(profiled, expected)
        # Guard the full workloads and safety floors independently of the knob.
        constants = {target.id: node.value.value for node in ast.walk(tree)
                     if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                     for target in node.targets if isinstance(target, ast.Name)}
        self.assertEqual(constants["THROUGHPUT_FILES"], 512)
        self.assertEqual(constants["THROUGHPUT_MIN_FILES_PER_SECOND"], 0.5)
        self.assertEqual(constants["THROUGHPUT_MAX_NEW_CONNECTIONS"], 200)
        nested = methods["test_16_workers_256_nested_uploads_with_concurrent_maintenance"]
        full_counts = [node.args[0].value for node in ast.walk(nested)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                       and node.func.id == "stress_file_count" and isinstance(node.args[0], ast.Constant)]
        self.assertEqual(full_counts, [256])

    def test_unknown_or_empty_profile_fails_closed(self):
        for profile in ("", "PR", "bounded", "none", "full ", "0"):
            with self.subTest(profile=profile), patch.dict(
                os.environ, {"FROSTVAULT_STRESS_PROFILE": profile}, clear=True
            ), self.assertRaisesRegex(ValueError, "FROSTVAULT_STRESS_PROFILE"):
                stress_file_count(256)


if __name__ == "__main__":
    unittest.main()
