"""Commit-check gate for image publish (issue #306)."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import require_commit_checks as gate  # noqa: E402


class EvaluateRequiredChecksTests(unittest.TestCase):
    def test_success_requires_every_named_check(self) -> None:
        state, details = gate.evaluate_required_checks(
            ["Unit and migration tests", "S3-compatible integrity (MinIO)"],
            [
                {
                    "name": "Unit and migration tests",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-06T10:00:00Z",
                },
                {
                    "name": "S3-compatible integrity (MinIO)",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-06T10:01:00Z",
                },
            ],
        )
        self.assertEqual(state, "success")
        self.assertTrue(all(line.startswith("ok:") for line in details))

    def test_missing_check_is_pending_not_success(self) -> None:
        state, details = gate.evaluate_required_checks(
            ["Unit and migration tests", "Frontend production build"],
            [
                {
                    "name": "Unit and migration tests",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        )
        self.assertEqual(state, "pending")
        self.assertIn("missing: Frontend production build", details)

    def test_in_progress_check_is_pending(self) -> None:
        state, _details = gate.evaluate_required_checks(
            ["Unit and migration tests"],
            [
                {
                    "name": "Unit and migration tests",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ],
        )
        self.assertEqual(state, "pending")

    def test_cancelled_or_failed_checks_are_failed(self) -> None:
        for conclusion in ("failure", "cancelled", "skipped", "timed_out"):
            with self.subTest(conclusion=conclusion):
                state, details = gate.evaluate_required_checks(
                    ["Unit and migration tests"],
                    [
                        {
                            "name": "Unit and migration tests",
                            "status": "completed",
                            "conclusion": conclusion,
                        }
                    ],
                )
                self.assertEqual(state, "failed")
                self.assertTrue(details[0].startswith("failed:"))

    def test_newer_rerun_supersedes_older_failure(self) -> None:
        state, _details = gate.evaluate_required_checks(
            ["Unit and migration tests"],
            [
                {
                    "name": "Unit and migration tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "completed_at": "2026-09-06T09:00:00Z",
                },
                {
                    "name": "Unit and migration tests",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-06T10:00:00Z",
                },
            ],
        )
        self.assertEqual(state, "success")

    def test_wait_returns_nonzero_when_timeout_still_pending(self) -> None:
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return []

        times = iter([0.0, 0.0, 100.0])
        code = gate.wait_for_required_checks(
            repository="owner/repo",
            sha="a" * 40,
            token="token",
            required=["Unit and migration tests"],
            timeout_seconds=1,
            interval_seconds=1,
            fetch=fetch,
            sleeper=lambda _seconds: None,
            now=lambda: next(times, 100.0),
        )
        self.assertEqual(code, 1)
        self.assertGreaterEqual(calls["n"], 1)

    def test_wait_returns_zero_on_success(self) -> None:
        def fetch():
            return [
                {
                    "name": "Unit and migration tests",
                    "head_sha": "a" * 40,
                    "status": "completed",
                    "conclusion": "success",
                }
            ]

        code = gate.wait_for_required_checks(
            repository="owner/repo",
            sha="a" * 40,
            token="token",
            required=["Unit and migration tests"],
            timeout_seconds=1,
            interval_seconds=1,
            fetch=fetch,
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(code, 0)

    def test_publish_requires_full_stress_not_a_successful_bounded_pr(self) -> None:
        full = "Full PostgreSQL concurrency stress"
        self.assertIn(full, gate.DEFAULT_REQUIRED_CHECKS)
        sha = "a" * 40
        runs = [{"name": name, "head_sha": sha, "status": "completed", "conclusion": "success"}
                for name in gate.DEFAULT_REQUIRED_CHECKS if name != full]
        code = gate.wait_for_required_checks(
            repository="owner/repo", sha=sha, token="token",
            required=list(gate.DEFAULT_REQUIRED_CHECKS), timeout_seconds=0,
            interval_seconds=1, fetch=lambda: runs,
        )
        self.assertEqual(code, 1)
        for conclusion in ("skipped", "cancelled", "failure", "neutral"):
            with self.subTest(conclusion=conclusion):
                state, _ = gate.evaluate_required_checks([full], [
                    {"name": full, "head_sha": sha, "status": "completed", "conclusion": conclusion}
                ])
                self.assertEqual(state, "failed")

    def test_full_stress_success_for_a_different_sha_cannot_publish(self) -> None:
        code = gate.wait_for_required_checks(
            repository="owner/repo", sha="a" * 40, token="token",
            required=["Full PostgreSQL concurrency stress"], timeout_seconds=0,
            interval_seconds=1, fetch=lambda: [{
                "name": "Full PostgreSQL concurrency stress", "head_sha": "b" * 40,
                "status": "completed", "conclusion": "success",
            }],
        )
        self.assertEqual(code, 1)

    def test_custom_check_cannot_replace_mandatory_publication_checks(self) -> None:
        from unittest.mock import patch
        with patch.object(gate, "wait_for_required_checks", return_value=0) as wait:
            self.assertEqual(gate.main([
                "--sha", "a" * 40, "--repository", "owner/repo", "--token", "test",
                "--check", "Additional release proof",
            ]), 0)
        required = wait.call_args.kwargs["required"]
        self.assertTrue(set(gate.DEFAULT_REQUIRED_CHECKS).issubset(required))
        self.assertIn("Additional release proof", required)

    def test_missing_sha_evidence_cannot_publish(self) -> None:
        code = gate.wait_for_required_checks(
            repository="owner/repo", sha="a" * 40, token="token",
            required=["Full PostgreSQL concurrency stress"], timeout_seconds=0,
            interval_seconds=1, fetch=lambda: [{
                "name": "Full PostgreSQL concurrency stress",
                "status": "completed", "conclusion": "success",
            }],
        )
        self.assertEqual(code, 1)

    def test_cli_rejects_short_sha(self) -> None:
        self.assertEqual(gate.main(["--sha", "abc", "--repository", "o/r", "--token", "t"]), 64)


if __name__ == "__main__":
    unittest.main()
