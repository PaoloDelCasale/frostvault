"""Gates of the automated agent pipeline (issue #84).

Seams under test: the decision functions in ``.github/scripts/agent_pipeline.py``
and the three workflows that call them — not GitHub's runtime. Every gate matters
on its own, because auto-merge means nothing else stands between an agent's pull
request and ``main``.
"""

from __future__ import annotations

import importlib.util
import json
import os
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
        "mergeStateStatus": "CLEAN",
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
        titles: dict[int, str] | None = None,
        fail_merge: set[int] | None = None,
    ) -> None:
        self._blocking = blocking or {}
        self._blocked_by = blocked_by or {}
        self._labels = labels or {}
        self._pull_requests = pull_requests or []
        self._checks = checks or {}
        self._titles = titles or {}
        self._fail_merge = fail_merge or set()
        self.repo = "o/r"
        self.added_labels: list[tuple[int, str]] = []
        self.removed_labels: list[tuple[int, str]] = []
        self.merged: list[int] = []
        self.closed_issues: list[int] = []
        self.deleted_labels: list[str] = []

    def title(self, issue: int) -> str:
        return self._titles.get(issue, f"Issue {issue}")

    def blocking(self, issue: int) -> list[dict]:
        return self._blocking.get(issue, [])

    def blocked_by(self, issue: int) -> list[dict]:
        return self._blocked_by.get(issue, [])

    def labels(self, issue: int) -> set[str]:
        return set(self._labels.get(issue, set()))

    def add_label(self, issue: int, label: str) -> None:
        self.added_labels.append((issue, label))
        self._labels.setdefault(issue, set()).add(label)

    def remove_label(self, issue: int, label: str) -> None:
        self.removed_labels.append((issue, label))
        self._labels.setdefault(issue, set()).discard(label)

    def open_pull_requests(self) -> list[dict]:
        return self._pull_requests

    def check_runs(self, sha: str) -> list[dict]:
        return self._checks.get(sha, [])

    def merge(self, number: int) -> None:
        if number in self._fail_merge:
            raise RuntimeError(f"merge of #{number} refused")
        self.merged.append(number)

    def close_issue(self, number: int) -> None:
        self.closed_issues.append(number)

    def delete_label(self, label: str) -> None:
        self.deleted_labels.append(label)


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
            blocking={
                71: [_issue(81, labels=("enhancement", "agent-pipeline"))]
            },
            blocked_by={81: [_issue(71, state="closed")]},
        )
        self.assertEqual(pipeline.unblock(client, 71), [])
        self.assertEqual(client.added_labels, [])

    def test_ignores_a_closed_issue_outside_the_temporary_epic(self) -> None:
        client = FakeGitHub(
            blocking={4: [_issue(57, labels=("agent-pipeline",))]},
            blocked_by={57: []},
        )
        self.assertEqual(pipeline.unblock(client, 4), [])
        self.assertEqual(client.added_labels, [])

    def test_already_ready_issue_is_still_queued_for_dispatch(self) -> None:
        # A human may have added ready-for-agent before unblock ran; still dispatch.
        client = FakeGitHub(
            blocking={84: [_issue(57, labels=("agent-pipeline", "ready-for-agent"))]},
            blocked_by={57: []},
        )
        self.assertEqual(pipeline.unblock(client, 84), [57])
        self.assertEqual(client.added_labels, [])

    def test_already_dispatched_issue_is_not_queued_again(self) -> None:
        client = FakeGitHub(
            blocking={
                84: [
                    _issue(
                        57,
                        labels=("agent-pipeline", "ready-for-agent", "agent-dispatched"),
                    )
                ]
            },
            blocked_by={57: []},
            labels={57: {"agent-pipeline", "ready-for-agent", "agent-dispatched"}},
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

    def test_a_behind_pull_request_is_left_alone(self) -> None:
        # MERGEABLE alone is not enough when branch protection requires an
        # up-to-date head; gh pr merge then fails mid-sweep.
        reason = self._skip_reason(mergeStateStatus="BEHIND")
        self.assertEqual(reason, "merge state is BEHIND")

    def test_an_issue_outside_the_pipeline_is_left_alone(self) -> None:
        reason = pipeline.merge_reason_to_skip(_pull_request(), {"enhancement"}, "green")
        self.assertEqual(reason, "the issue it closes is not in the pipeline")

    def test_a_label_cannot_enrol_an_issue_outside_epic_56(self) -> None:
        reason = pipeline.merge_reason_to_skip(
            _pull_request(body="Closes #73"), {"agent-pipeline"}, "green"
        )
        self.assertEqual(reason, "issue #73 is not part of temporary epic #56")

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

    def test_open_issue_blockers_block_the_merge(self) -> None:
        # Parallel agents may open PRs before a sibling blocker lands; merging
        # would close the issue and falsely unblock the rest of the chain.
        reason = pipeline.merge_reason_to_skip(
            _pull_request(body="Closes #61"),
            {"agent-pipeline"},
            "green",
            open_blockers=[58],
        )
        self.assertEqual(reason, "issue still blocked by #58")


class MergeSweepTests(unittest.TestCase):
    def test_merges_the_pull_request_that_is_ready(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request()],
            labels={57: {"agent-pipeline", "ready-for-agent"}},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        self.assertEqual(pipeline.merge_ready(client), [500])
        self.assertEqual(client.merged, [500])

    def test_does_not_merge_while_issue_blockers_are_open(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request(body="Closes #61", number=591)],
            labels={61: {"agent-pipeline", "ready-for-agent"}},
            blocked_by={61: [_issue(58, state="open")]},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        self.assertEqual(pipeline.merge_ready(client), [])
        self.assertEqual(client.merged, [])

    def test_continues_sweeping_after_one_merge_fails(self) -> None:
        client = FakeGitHub(
            pull_requests=[
                _pull_request(number=500, body="Closes #57", headRefOid="aaa"),
                _pull_request(number=501, body="Closes #58", headRefOid="bbb"),
            ],
            labels={
                57: {"agent-pipeline", "ready-for-agent"},
                58: {"agent-pipeline", "ready-for-agent"},
            },
            checks={
                "aaa": [_check("Unit and migration tests")],
                "bbb": [_check("Unit and migration tests")],
            },
            fail_merge={500},
        )
        self.assertEqual(pipeline.merge_ready(client), [501])
        self.assertEqual(client.merged, [501])

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

    def test_cleanup_merge_closes_the_epic_and_deletes_the_pipeline_label(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request(body="Closes #86", number=502)],
            labels={86: {"agent-pipeline"}},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        self.assertEqual(pipeline.merge_ready(client), [502])
        self.assertEqual(client.merged, [502])
        self.assertEqual(client.closed_issues, [56])
        self.assertEqual(
            client.deleted_labels, ["agent-pipeline", "agent-dispatched"]
        )

    def test_an_ordinary_merge_does_not_run_tracker_cleanup(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request()],
            labels={57: {"agent-pipeline"}},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        pipeline.merge_ready(client)
        self.assertEqual(client.closed_issues, [])
        self.assertEqual(client.deleted_labels, [])

    def test_merge_unblocks_and_dispatches_dependents(self) -> None:
        # GITHUB_TOKEN merges do not re-fire issues:closed; merge must continue
        # the chain in-process or the pipeline stalls after the first auto-merge.
        sender = RecordingSender()
        client = FakeGitHub(
            pull_requests=[_pull_request(body="Closes #67", number=101)],
            labels={67: {"agent-pipeline", "ready-for-agent"}},
            blocking={
                67: [
                    _issue(
                        70,
                        labels=("agent-pipeline",),
                    )
                ]
            },
            blocked_by={
                70: [
                    _issue(63, state="closed"),
                    _issue(64, state="closed"),
                    _issue(67, state="closed"),
                    _issue(68, state="closed"),
                    _issue(69, state="closed"),
                ]
            },
            titles={70: "Playwright end-to-end tests"},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        self.assertEqual(
            pipeline.merge_ready(client, _automation(sender)), [101]
        )
        self.assertEqual(client.merged, [101])
        self.assertIn((70, "ready-for-agent"), client.added_labels)
        self.assertIn((70, "agent-dispatched"), client.added_labels)
        self.assertEqual(len(sender.payloads), 1)
        self.assertEqual(sender.payloads[0]["issue"]["number"], 70)


class RepairFailedTests(unittest.TestCase):
    def test_dispatches_a_repair_when_checks_failed(self) -> None:
        sender = RecordingSender()
        client = FakeGitHub(
            pull_requests=[
                _pull_request(
                    number=103,
                    body="Closes #70",
                    headRefName="cursor/playwright-1",
                    headRefOid="deadbeef01",
                    mergeStateStatus="BLOCKED",
                )
            ],
            labels={70: {"agent-pipeline", "ready-for-agent", "agent-dispatched"}},
            titles={70: "Playwright e2e"},
            checks={
                "deadbeef01": [
                    _check("Unit and migration tests"),
                    _check("Playwright e2e", conclusion="failure"),
                ]
            },
        )
        self.assertEqual(
            pipeline.repair_failed(client, _automation(sender)), [103]
        )
        self.assertIn((70, "agent-dispatched"), client.removed_labels)
        self.assertIn((70, "agent-dispatched"), client.added_labels)
        self.assertIn((70, "agent-repair-deadbee"), client.added_labels)
        self.assertEqual(
            sender.payloads[0]["idempotency_key"], "o/r#70:repair:deadbeef01"
        )
        self.assertIn(
            "Do **not** open a new pull request",
            sender.payloads[0]["instructions"],
        )

    def test_does_not_repair_twice_for_the_same_sha(self) -> None:
        sender = RecordingSender()
        client = FakeGitHub(
            pull_requests=[
                _pull_request(
                    number=103,
                    body="Closes #70",
                    headRefOid="deadbeef01",
                    mergeStateStatus="BLOCKED",
                )
            ],
            labels={
                70: {
                    "agent-pipeline",
                    "agent-dispatched",
                    "agent-repair-deadbee",
                }
            },
            checks={
                "deadbeef01": [_check("Playwright e2e", conclusion="failure")]
            },
        )
        self.assertEqual(pipeline.repair_failed(client, _automation(sender)), [])
        self.assertEqual(sender.payloads, [])

    def test_ignores_green_and_pending_pull_requests(self) -> None:
        sender = RecordingSender()
        client = FakeGitHub(
            pull_requests=[
                _pull_request(number=500, headRefOid="aaa"),
                _pull_request(number=501, body="Closes #58", headRefOid="bbb"),
            ],
            labels={
                57: {"agent-pipeline", "agent-dispatched"},
                58: {"agent-pipeline", "agent-dispatched"},
            },
            checks={
                "aaa": [_check("Unit and migration tests")],
                "bbb": [
                    _check(
                        "Unit and migration tests",
                        status="in_progress",
                        conclusion=None,
                    )
                ],
            },
        )
        self.assertEqual(pipeline.repair_failed(client, _automation(sender)), [])
        self.assertEqual(sender.payloads, [])


class RecordingSender:
    """Stands in for the Automation webhook so payloads can be asserted."""

    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {"accepted": True}
        self.payloads: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self.response


def _automation(sender: RecordingSender) -> Any:
    return pipeline.CursorAutomation(
        webhook_url="https://cursor.example/automation",
        webhook_key="secret",
        sender=sender,
    )


class AgentPromptTests(unittest.TestCase):
    def test_the_prompt_names_the_issue_and_the_method(self) -> None:
        prompt = pipeline.agent_prompt("o/r", 57, "Frontend toolchain")
        self.assertIn("#57", prompt)
        self.assertIn("Frontend toolchain", prompt)
        self.assertIn("/tdd", prompt)
        self.assertIn("Closes #57", prompt)

    def test_the_prompt_forbids_asking_to_confirm_the_seams(self) -> None:
        # The tdd skill would otherwise stall an agent that has nobody to ask.
        prompt = pipeline.agent_prompt("o/r", 57, "x")
        self.assertIn("already agreed", prompt)
        self.assertIn("do not stop to ask", prompt)

    def test_the_prompt_requires_the_whole_suite(self) -> None:
        prompt = pipeline.agent_prompt("o/r", 57, "x")
        self.assertIn("unittest discover -s tests", prompt)
        self.assertIn("npm run lint", prompt)
        self.assertIn("npm run test", prompt)
        self.assertNotIn("node --test", prompt)

    def test_the_prompt_requires_a_ready_for_review_pull_request(self) -> None:
        prompt = pipeline.agent_prompt("o/r", 57, "x")
        self.assertIn("not draft", prompt)
        self.assertIn("drafts are never auto-merged", prompt)

    def test_the_prompt_requires_waiting_for_ci_green(self) -> None:
        prompt = pipeline.agent_prompt("o/r", 57, "x")
        self.assertIn("gh pr checks", prompt)
        self.assertIn("not the end of the job", prompt)
        self.assertIn("every check is green", prompt)

    def test_repair_prompt_forbids_a_second_pull_request(self) -> None:
        prompt = pipeline.repair_prompt(
            "o/r", 70, "Playwright", 103, "cursor/x", "Playwright e2e (failure)"
        )
        self.assertIn("Do **not** open a new pull request", prompt)
        self.assertIn("#103", prompt)
        self.assertIn("cursor/x", prompt)
        self.assertIn("gh pr checks 103", prompt)


class AutomationPayloadTests(unittest.TestCase):
    def test_payload_identifies_the_event_repository_and_issue(self) -> None:
        sender = RecordingSender()
        _automation(sender).start("o/r", 57, "Toolchain", "do it")
        payload = sender.payloads[0]
        self.assertEqual(payload["event"], "frostvault_epic_56_issue_ready")
        self.assertEqual(payload["idempotency_key"], "o/r#57")
        self.assertEqual(payload["repository"], "o/r")
        self.assertEqual(
            payload["issue"],
            {
                "number": 57,
                "title": "Toolchain",
                "url": "https://github.com/o/r/issues/57",
            },
        )
        self.assertEqual(payload["instructions"], "do it")

    def test_payload_contains_no_webhook_credentials(self) -> None:
        sender = RecordingSender()
        _automation(sender).start("o/r", 57, "t", "p")
        serialized = json.dumps(sender.payloads[0])
        self.assertNotIn("secret", serialized)
        self.assertNotIn("cursor.example", serialized)


class DispatchTests(unittest.TestCase):
    def test_dispatch_sends_the_issue_title_in_the_prompt(self) -> None:
        sender = RecordingSender()
        client = FakeGitHub(titles={57: "Frontend toolchain"})
        result = pipeline.dispatch(client, _automation(sender), 57, "o/r")
        self.assertEqual(result, "dispatched")
        self.assertIn("Frontend toolchain", sender.payloads[0]["instructions"])
        self.assertEqual(client.added_labels, [(57, "agent-dispatched")])

    def test_the_dispatched_label_makes_retries_idempotent(self) -> None:
        sender = RecordingSender()
        client = FakeGitHub(labels={57: {"agent-dispatched"}})
        result = pipeline.dispatch(client, _automation(sender), 57, "o/r")
        self.assertEqual(result, "already-dispatched")
        self.assertEqual(sender.payloads, [])

    def test_without_webhook_secrets_the_issue_is_only_labelled(self) -> None:
        self.assertEqual(pipeline.dispatch(FakeGitHub(), None, 57, "o/r"), "skipped")

    def test_dispatch_refuses_an_issue_outside_epic_56_even_with_a_client(self) -> None:
        sender = RecordingSender()
        result = pipeline.dispatch(FakeGitHub(), _automation(sender), 73, "o/r")
        self.assertEqual(result, "outside-epic")
        self.assertEqual(sender.payloads, [])

class AutomationFromEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            name: os.environ.pop(name, None)
            for name in (
                "CURSOR_EPIC_56_WEBHOOK_URL",
                "CURSOR_EPIC_56_WEBHOOK_KEY",
            )
        }

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_both_secrets_are_required(self) -> None:
        self.assertIsNone(pipeline.automation_from_environment())
        os.environ["CURSOR_EPIC_56_WEBHOOK_URL"] = "https://cursor.example/hook"
        self.assertIsNone(pipeline.automation_from_environment())

    def test_webhook_configuration_comes_from_secrets(self) -> None:
        os.environ["CURSOR_EPIC_56_WEBHOOK_URL"] = "https://cursor.example/hook"
        os.environ["CURSOR_EPIC_56_WEBHOOK_KEY"] = "key"
        automation = pipeline.automation_from_environment()
        assert automation is not None
        self.assertEqual(automation.webhook_url, "https://cursor.example/hook")
        self.assertEqual(automation.webhook_key, "key")


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
        self.assertIn("check_suite", triggers)
        self.assertEqual(triggers["check_suite"]["types"], ["completed"])
        self.assertNotIn("pull_request", triggers)
        self.assertNotIn("pull_request_target", triggers)
        self.assertNotIn("workflow_run", triggers)
        self.assertEqual(workflow["permissions"]["issues"], "write")
        text = (WORKFLOWS / "agent-automerge.yml").read_text(encoding="utf-8")
        self.assertIn("cursor/", text)
        self.assertIn("check_suite", text)
    def test_all_pipeline_workflows_call_the_tested_script(self) -> None:
        for name in ("agent-unblock.yml", "agent-automerge.yml", "agent-dispatch.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            self.assertIn(".github/scripts/agent_pipeline.py", text, name)

    def test_dispatch_is_manual_or_ready_label_and_takes_an_issue_number(self) -> None:
        workflow = self._workflow("agent-dispatch.yml")
        triggers = self._triggers(workflow)
        self.assertEqual(set(triggers), {"workflow_dispatch", "issues"})
        self.assertIn("issue", triggers["workflow_dispatch"]["inputs"])
        self.assertEqual(triggers["issues"]["types"], ["labeled"])
        text = (WORKFLOWS / "agent-dispatch.yml").read_text(encoding="utf-8")
        self.assertIn("ready-for-agent", text)

    def test_dispatching_workflows_pass_only_the_narrow_webhook_secrets(self) -> None:
        for name in (
            "agent-unblock.yml",
            "agent-dispatch.yml",
            "agent-automerge.yml",
        ):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            self.assertIn("secrets.CURSOR_EPIC_56_WEBHOOK_URL", text, name)
            self.assertIn("secrets.CURSOR_EPIC_56_WEBHOOK_KEY", text, name)
            self.assertNotIn("CURSOR_API_KEY", text, name)

    def test_automerge_continues_the_chain_after_merge(self) -> None:
        text = (WORKFLOWS / "agent-automerge.yml").read_text(encoding="utf-8")
        self.assertIn("GITHUB_TOKEN", text)
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("unblocks and dispatches the next issues in the same job", agents)

    def test_pipeline_is_documented_for_the_next_agent(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Temporary agent pipeline for epic #56", agents)
        self.assertIn("agent-pipeline", agents)
        self.assertIn("ready-for-agent", agents)
        self.assertIn("self-removal issue #86", agents)


if __name__ == "__main__":
    unittest.main()
