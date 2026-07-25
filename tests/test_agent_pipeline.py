"""Gates of the automated agent pipeline (issue #84).

Seams under test: the decision functions in ``.github/scripts/agent_pipeline.py``
and the two workflows that call them — not GitHub's runtime. Every gate matters
on its own, because auto-merge means nothing else stands between an agent's pull
request and ``main``.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "agent_pipeline.py"
WORKFLOWS = ROOT / ".github" / "workflows"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("agent_pipeline", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pipeline = _load_module()


def _issue(number: int, state: str = "open", labels: tuple[str, ...] = ()) -> dict:
    return {
        "number": number,
        "state": state,
        "labels": [{"name": name} for name in labels],
    }


def _check(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


def _pull_request(**overrides: Any) -> dict:
    pull_request = {
        "number": 500,
        "isDraft": False,
        "isCrossRepository": False,
        "body": "Closes #57",
        "headRefName": "cursor/frontend-toolchain-1",
        "headRefOid": "abc123",
        "mergeable": "MERGEABLE",
    }
    pull_request.update(overrides)
    return pull_request


class FakeGitHub:
    """Records mutations so tests can assert what the pipeline would do."""

    def __init__(
        self,
        blocking: dict[int, list[dict]] | None = None,
        blocked_by: dict[int, list[dict]] | None = None,
        labels: dict[int, set[str]] | None = None,
        pull_requests: list[dict] | None = None,
        checks: dict[str, list[dict]] | None = None,
    ) -> None:
        self._blocking = blocking or {}
        self._blocked_by = blocked_by or {}
        self._labels = labels or {}
        self._pull_requests = pull_requests or []
        self._checks = checks or {}
        self.added_labels: list[tuple[int, str]] = []
        self.merged: list[int] = []

    def blocking(self, issue: int) -> list[dict]:
        return self._blocking.get(issue, [])

    def blocked_by(self, issue: int) -> list[dict]:
        return self._blocked_by.get(issue, [])

    def labels(self, issue: int) -> set[str]:
        return self._labels.get(issue, set())

    def add_label(self, issue: int, label: str) -> None:
        self.added_labels.append((issue, label))

    def open_pull_requests(self) -> list[dict]:
        return self._pull_requests

    def check_runs(self, sha: str) -> list[dict]:
        return self._checks.get(sha, [])

    def merge(self, number: int) -> None:
        self.merged.append(number)


class ClosingIssueTests(unittest.TestCase):
    def test_recognises_the_closing_keywords(self) -> None:
        for body in ("Closes #57", "closes #57", "Fixes #57", "resolved #57"):
            self.assertEqual(pipeline.closing_issue_number(body), 57, body)

    def test_ignores_a_bare_reference(self) -> None:
        self.assertIsNone(pipeline.closing_issue_number("Related to #57"))

    def test_handles_a_missing_body(self) -> None:
        self.assertIsNone(pipeline.closing_issue_number(None))

    def test_takes_the_first_closed_issue(self) -> None:
        self.assertEqual(
            pipeline.closing_issue_number("Closes #57\nAlso closes #58"), 57
        )


class ChecksVerdictTests(unittest.TestCase):
    def test_no_checks_is_not_green(self) -> None:
        # A commit nobody checked must never merge: that is how a misconfigured
        # pipeline would merge everything instantly.
        self.assertEqual(pipeline.checks_verdict([]), "none")

    def test_unfinished_checks_are_pending(self) -> None:
        runs = [_check("CI"), _check("Security", status="in_progress", conclusion=None)]
        self.assertEqual(pipeline.checks_verdict(runs), "pending")

    def test_a_failure_is_failed(self) -> None:
        runs = [_check("CI"), _check("Security", conclusion="failure")]
        self.assertEqual(pipeline.checks_verdict(runs), "failed")

    def test_cancelled_and_timed_out_are_failed(self) -> None:
        for conclusion in ("cancelled", "timed_out", "action_required", "stale"):
            self.assertEqual(
                pipeline.checks_verdict([_check("CI", conclusion=conclusion)]),
                "failed",
                conclusion,
            )

    def test_neutral_and_skipped_pass(self) -> None:
        # Bugbot reports findings as neutral, so neutral cannot mean "blocked".
        runs = [_check("CI"), _check("Cursor Bugbot", conclusion="neutral"), _check("x", conclusion="skipped")]
        self.assertEqual(pipeline.checks_verdict(runs), "green")

    def test_failed_names_are_reported(self) -> None:
        runs = [_check("CI"), _check("Security", conclusion="failure")]
        self.assertEqual(pipeline.failed_check_names(runs), ["Security (failure)"])


class UnblockTests(unittest.TestCase):
    def test_labels_a_dependent_whose_blockers_are_all_closed(self) -> None:
        client = FakeGitHub(
            blocking={84: [_issue(57, labels=("agent-pipeline",))]},
            blocked_by={57: [_issue(84, state="closed")]},
        )
        self.assertEqual(pipeline.unblock(client, 84), [57])
        self.assertEqual(client.added_labels, [(57, "ready-for-agent")])

    def test_leaves_a_dependent_with_another_open_blocker(self) -> None:
        client = FakeGitHub(
            blocking={57: [_issue(60, labels=("agent-pipeline",))]},
            blocked_by={60: [_issue(57, state="closed"), _issue(59)]},
        )
        self.assertEqual(pipeline.unblock(client, 57), [])
        self.assertEqual(client.added_labels, [])

    def test_ignores_an_issue_outside_the_pipeline(self) -> None:
        # Out-of-epic issues depend on the epic on purpose; they must not start.
        client = FakeGitHub(
            blocking={71: [_issue(81, labels=("enhancement",))]},
            blocked_by={81: [_issue(71, state="closed")]},
        )
        self.assertEqual(pipeline.unblock(client, 71), [])
        self.assertEqual(client.added_labels, [])

    def test_does_not_relabel_an_already_ready_issue(self) -> None:
        client = FakeGitHub(
            blocking={84: [_issue(57, labels=("agent-pipeline", "ready-for-agent"))]},
            blocked_by={57: []},
        )
        self.assertEqual(pipeline.unblock(client, 84), [])
        self.assertEqual(client.added_labels, [])

    def test_skips_a_dependent_that_is_already_closed(self) -> None:
        client = FakeGitHub(
            blocking={84: [_issue(57, state="closed", labels=("agent-pipeline",))]}
        )
        self.assertEqual(pipeline.unblock(client, 84), [])

    def test_an_issue_blocking_nothing_is_harmless(self) -> None:
        client = FakeGitHub()
        self.assertEqual(pipeline.unblock(client, 84), [])
        self.assertEqual(client.added_labels, [])

    def test_labels_several_dependents_at_once(self) -> None:
        client = FakeGitHub(
            blocking={
                62: [
                    _issue(68, labels=("agent-pipeline",)),
                    _issue(69, labels=("agent-pipeline",)),
                ]
            },
            blocked_by={68: [], 69: []},
        )
        self.assertEqual(pipeline.unblock(client, 62), [68, 69])


class MergeGateTests(unittest.TestCase):
    def _skip_reason(self, **overrides: Any) -> str | None:
        return pipeline.merge_reason_to_skip(
            _pull_request(**overrides), {"agent-pipeline"}, "green"
        )

    def test_a_fully_green_agent_pull_request_may_merge(self) -> None:
        self.assertIsNone(self._skip_reason())

    def test_a_draft_is_left_alone(self) -> None:
        self.assertEqual(self._skip_reason(isDraft=True), "draft")

    def test_a_fork_pull_request_is_never_merged(self) -> None:
        # The privileged half of the pipeline must not act on untrusted branches,
        # even when they imitate the naming convention.
        self.assertEqual(self._skip_reason(isCrossRepository=True), "comes from a fork")

    def test_a_human_branch_is_left_alone(self) -> None:
        reason = self._skip_reason(headRefName="fix/typo")
        self.assertIsNotNone(reason)
        self.assertIn("not an agent branch", reason or "")

    def test_a_pull_request_closing_nothing_is_left_alone(self) -> None:
        self.assertEqual(self._skip_reason(body="Refactor only"), "closes no issue")

    def test_a_conflicting_pull_request_is_left_alone(self) -> None:
        reason = self._skip_reason(mergeable="CONFLICTING")
        self.assertIn("not mergeable", reason or "")

    def test_an_issue_outside_the_pipeline_is_left_alone(self) -> None:
        reason = pipeline.merge_reason_to_skip(_pull_request(), {"enhancement"}, "green")
        self.assertEqual(reason, "the issue it closes is not in the pipeline")

    def test_pending_and_absent_checks_block_the_merge(self) -> None:
        for verdict, expected in (
            ("pending", "checks are still running"),
            ("none", "no checks have reported"),
            ("failed", "checks are not green"),
        ):
            reason = pipeline.merge_reason_to_skip(
                _pull_request(), {"agent-pipeline"}, verdict
            )
            self.assertEqual(reason, expected, verdict)


class MergeSweepTests(unittest.TestCase):
    def test_merges_the_pull_request_that_is_ready(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request()],
            labels={57: {"agent-pipeline", "ready-for-agent"}},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        self.assertEqual(pipeline.merge_ready(client), [500])
        self.assertEqual(client.merged, [500])

    def test_does_not_merge_while_a_check_is_running(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request()],
            labels={57: {"agent-pipeline"}},
            checks={
                "abc123": [
                    _check("Unit and migration tests"),
                    _check("CodeQL", status="in_progress", conclusion=None),
                ]
            },
        )
        self.assertEqual(pipeline.merge_ready(client), [])
        self.assertEqual(client.merged, [])

    def test_does_not_merge_a_commit_without_checks(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request()],
            labels={57: {"agent-pipeline"}},
            checks={},
        )
        self.assertEqual(pipeline.merge_ready(client), [])

    def test_does_not_merge_a_human_pull_request(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request(headRefName="docs/readme", number=501)],
            labels={57: {"agent-pipeline"}},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        self.assertEqual(pipeline.merge_ready(client), [])


class PipelineWorkflowContractTests(unittest.TestCase):
    def _workflow(self, name: str) -> dict:
        return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))

    def _triggers(self, workflow: dict) -> Any:
        # PyYAML 1.1 may parse the key ``on`` as boolean True.
        return workflow["on"] if "on" in workflow else workflow[True]

    def test_unblock_reacts_to_closed_issues_only(self) -> None:
        workflow = self._workflow("agent-unblock.yml")
        triggers = self._triggers(workflow)
        self.assertEqual(list(triggers), ["issues"])
        self.assertEqual(triggers["issues"]["types"], ["closed"])
        self.assertEqual(workflow["permissions"]["issues"], "write")
        self.assertEqual(workflow["permissions"]["contents"], "read")

    def test_automerge_is_scheduled_and_never_pull_request_driven(self) -> None:
        workflow = self._workflow("agent-automerge.yml")
        triggers = self._triggers(workflow)
        self.assertIn("schedule", triggers)
        self.assertNotIn("pull_request", triggers)
        self.assertNotIn("pull_request_target", triggers)
        self.assertNotIn("workflow_run", triggers)

    def test_both_workflows_call_the_tested_script(self) -> None:
        for name in ("agent-unblock.yml", "agent-automerge.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            self.assertIn(".github/scripts/agent_pipeline.py", text, name)

    def test_pipeline_is_documented_for_the_next_agent(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Automated agent pipeline", agents)
        self.assertIn("agent-pipeline", agents)
        self.assertIn("ready-for-agent", agents)


if __name__ == "__main__":
    unittest.main()
