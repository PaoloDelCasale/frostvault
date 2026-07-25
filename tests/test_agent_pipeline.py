"""Gates of the automated agent pipeline (issue #84).

Seams under test: the decision functions in ``.github/scripts/agent_pipeline.py``
and the two workflows that call them — not GitHub's runtime. Every gate matters
on its own, because auto-merge means nothing else stands between an agent's pull
request and ``main``.
"""

from __future__ import annotations

import importlib.util
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
    ) -> None:
        self._blocking = blocking or {}
        self._blocked_by = blocked_by or {}
        self._labels = labels or {}
        self._pull_requests = pull_requests or []
        self._checks = checks or {}
        self._titles = titles or {}
        self.added_labels: list[tuple[int, str]] = []
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
        return self._labels.get(issue, set())

    def add_label(self, issue: int, label: str) -> None:
        self.added_labels.append((issue, label))

    def open_pull_requests(self) -> list[dict]:
        return self._pull_requests

    def check_runs(self, sha: str) -> list[dict]:
        return self._checks.get(sha, [])

    def merge(self, number: int) -> None:
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

    def test_cleanup_merge_closes_the_epic_and_deletes_the_pipeline_label(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request(body="Closes #86", number=502)],
            labels={86: {"agent-pipeline"}},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        self.assertEqual(pipeline.merge_ready(client), [502])
        self.assertEqual(client.merged, [502])
        self.assertEqual(client.closed_issues, [56])
        self.assertEqual(client.deleted_labels, ["agent-pipeline"])

    def test_an_ordinary_merge_does_not_run_tracker_cleanup(self) -> None:
        client = FakeGitHub(
            pull_requests=[_pull_request()],
            labels={57: {"agent-pipeline"}},
            checks={"abc123": [_check("Unit and migration tests")]},
        )
        pipeline.merge_ready(client)
        self.assertEqual(client.closed_issues, [])
        self.assertEqual(client.deleted_labels, [])


class RecordingSender:
    """Stands in for the Cursor API so payloads can be asserted."""

    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {"id": "bc-123"}
        self.payloads: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self.response


def _cursor(sender: RecordingSender, **overrides) -> Any:
    options = {
        "api_key": "key",
        "repo_url": "https://github.com/PaoloDelCasale/frostvault",
        "sender": sender,
    }
    options.update(overrides)
    return pipeline.CursorAgents(**options)


class AgentIdentityTests(unittest.TestCase):
    def test_the_same_issue_always_gets_the_same_agent_id(self) -> None:
        first = pipeline.agent_id_for("https://github.com/o/r", 57)
        second = pipeline.agent_id_for("https://github.com/o/r", 57)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("bc-"))

    def test_different_issues_get_different_agent_ids(self) -> None:
        self.assertNotEqual(
            pipeline.agent_id_for("https://github.com/o/r", 57),
            pipeline.agent_id_for("https://github.com/o/r", 58),
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
        self.assertIn("node --test", prompt)


class CursorPayloadTests(unittest.TestCase):
    def test_a_repository_agent_starts_from_the_base_ref_and_opens_a_pull_request(self) -> None:
        sender = RecordingSender()
        _cursor(sender, starting_ref="main").create(57, "Toolchain", "do it")
        payload = sender.payloads[0]
        self.assertEqual(payload["prompt"]["text"], "do it")
        self.assertTrue(payload["autoCreatePR"])
        self.assertEqual(
            payload["repos"],
            [
                {
                    "url": "https://github.com/PaoloDelCasale/frostvault",
                    "startingRef": "main",
                }
            ],
        )
        self.assertNotIn("env", payload)

    def test_a_named_environment_replaces_the_repository_block(self) -> None:
        # The API treats env and repos as mutually exclusive.
        sender = RecordingSender()
        _cursor(sender, environment="PaoloDelCasale/frostvault").create(57, "t", "p")
        payload = sender.payloads[0]
        self.assertEqual(payload["env"], {"type": "cloud", "name": "PaoloDelCasale/frostvault"})
        self.assertNotIn("repos", payload)

    def test_the_model_is_omitted_unless_chosen(self) -> None:
        sender = RecordingSender()
        _cursor(sender).create(57, "t", "p")
        self.assertNotIn("model", sender.payloads[0])

    def test_a_chosen_model_and_its_parameters_are_sent(self) -> None:
        sender = RecordingSender()
        _cursor(
            sender,
            model_id="claude-4.6-sonnet-thinking",
            model_params=[{"id": "fast", "value": "true"}],
        ).create(57, "t", "p")
        self.assertEqual(
            sender.payloads[0]["model"],
            {
                "id": "claude-4.6-sonnet-thinking",
                "params": [{"id": "fast", "value": "true"}],
            },
        )

    def test_the_agent_name_stays_within_the_api_limit(self) -> None:
        sender = RecordingSender()
        _cursor(sender).create(57, "x" * 200, "p")
        self.assertLessEqual(len(sender.payloads[0]["name"]), 100)


MODEL_CATALOGUE = [
    {
        "id": "composer-2",
        "aliases": ["composer-latest", "composer"],
        "parameters": [
            {"id": "fast", "values": [{"value": "false"}, {"value": "true"}]}
        ],
    },
    {
        "id": "cursor-grok-4.5-high",
        "parameters": [],
    },
    {
        "id": "cursor-grok-4.5-high-fast",
        "parameters": [],
    },
]


class ModelSelectionTests(unittest.TestCase):
    def test_a_known_model_without_parameters_is_accepted(self) -> None:
        self.assertIsNone(
            pipeline.model_selection_error(
                MODEL_CATALOGUE, "cursor-grok-4.5-high", None
            )
        )

    def test_an_alias_is_accepted(self) -> None:
        self.assertIsNone(
            pipeline.model_selection_error(MODEL_CATALOGUE, "composer-latest", None)
        )

    def test_an_unknown_model_lists_what_is_available(self) -> None:
        error = pipeline.model_selection_error(MODEL_CATALOGUE, "grok-4.5", None)
        self.assertIn("unknown model", error or "")
        self.assertIn("cursor-grok-4.5-high", error or "")
        self.assertIn("composer-2", error or "")

    def test_valid_parameters_are_accepted(self) -> None:
        self.assertIsNone(
            pipeline.model_selection_error(
                MODEL_CATALOGUE,
                "composer-2",
                [{"id": "fast", "value": "true"}],
            )
        )

    def test_an_unsupported_parameter_name_is_reported(self) -> None:
        error = pipeline.model_selection_error(
            MODEL_CATALOGUE, "composer-2", [{"id": "effort", "value": "high"}]
        )
        self.assertIn("does not accept the parameter", error or "")
        self.assertIn("fast", error or "")

    def test_an_unsupported_parameter_value_lists_the_allowed_ones(self) -> None:
        error = pipeline.model_selection_error(
            MODEL_CATALOGUE, "composer-2", [{"id": "fast", "value": "sometimes"}]
        )
        self.assertIn("fast='sometimes'", error or "")
        self.assertIn("true", error or "")


class DispatchTests(unittest.TestCase):
    def test_dispatch_sends_the_issue_title_in_the_prompt(self) -> None:
        sender = RecordingSender()
        client = FakeGitHub(titles={57: "Frontend toolchain"})
        result = pipeline.dispatch(client, _cursor(sender), 57, "o/r")
        self.assertEqual(result, "dispatched")
        self.assertIn("Frontend toolchain", sender.payloads[0]["prompt"]["text"])

    def test_a_conflict_means_already_dispatched_not_an_error(self) -> None:
        sender = RecordingSender(response={"conflict": True, "detail": "exists"})
        result = pipeline.dispatch(FakeGitHub(), _cursor(sender), 57, "o/r")
        self.assertEqual(result, "already-dispatched")

    def test_without_an_api_key_the_issue_is_only_labelled(self) -> None:
        # The label still lets a Cursor Automation pick the issue up.
        self.assertEqual(pipeline.dispatch(FakeGitHub(), None, 57, "o/r"), "skipped")

    def test_dispatch_refuses_an_issue_outside_epic_56_even_with_a_client(self) -> None:
        sender = RecordingSender()
        result = pipeline.dispatch(FakeGitHub(), _cursor(sender), 73, "o/r")
        self.assertEqual(result, "outside-epic")
        self.assertEqual(sender.payloads, [])

    def test_a_misconfigured_model_stops_the_dispatch_with_a_useful_message(self) -> None:
        sender = RecordingSender()
        cursor = _cursor(sender, model_id="grok-4.5")
        cursor.models = lambda: MODEL_CATALOGUE  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError) as raised:
            pipeline.dispatch(FakeGitHub(), cursor, 57, "o/r")
        message = str(raised.exception)
        self.assertIn("unknown model", message)
        self.assertIn("CURSOR_AGENT_MODEL", message)
        self.assertEqual(sender.payloads, [])

    def test_a_valid_model_is_dispatched(self) -> None:
        sender = RecordingSender()
        cursor = _cursor(sender, model_id="cursor-grok-4.5-high")
        cursor.models = lambda: MODEL_CATALOGUE  # type: ignore[method-assign]
        self.assertEqual(pipeline.dispatch(FakeGitHub(), cursor, 57, "o/r"), "dispatched")
        self.assertEqual(
            sender.payloads[0]["model"],
            {"id": "cursor-grok-4.5-high"},
        )


class CursorFromEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            name: os.environ.pop(name, None)
            for name in (
                "CURSOR_API_KEY",
                "CURSOR_AGENT_MODEL",
                "CURSOR_AGENT_MODEL_PARAMS",
                "CURSOR_AGENT_ENV",
                "CURSOR_AGENT_BASE_REF",
            )
        }

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_no_key_means_no_client(self) -> None:
        self.assertIsNone(pipeline.cursor_from_environment("o/r"))

    def test_a_blank_key_means_no_client(self) -> None:
        os.environ["CURSOR_API_KEY"] = "   "
        self.assertIsNone(pipeline.cursor_from_environment("o/r"))

    def test_the_model_choice_comes_from_the_environment(self) -> None:
        os.environ["CURSOR_API_KEY"] = "key"
        os.environ["CURSOR_AGENT_MODEL"] = "composer-2"
        os.environ["CURSOR_AGENT_MODEL_PARAMS"] = "fast=true"
        client = pipeline.cursor_from_environment("PaoloDelCasale/frostvault")
        assert client is not None
        self.assertEqual(client.model_id, "composer-2")
        self.assertEqual(client.model_params, [{"id": "fast", "value": "true"}])
        self.assertEqual(
            client.repo_url, "https://github.com/PaoloDelCasale/frostvault"
        )

    def test_malformed_model_parameters_are_ignored_rather_than_crashing(self) -> None:
        os.environ["CURSOR_API_KEY"] = "key"
        os.environ["CURSOR_AGENT_MODEL_PARAMS"] = "fast,=true,thinking=high"
        client = pipeline.cursor_from_environment("o/r")
        assert client is not None
        self.assertEqual(client.model_params, [{"id": "thinking", "value": "high"}])


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
        self.assertEqual(workflow["permissions"]["issues"], "write")

    def test_all_pipeline_workflows_call_the_tested_script(self) -> None:
        for name in ("agent-unblock.yml", "agent-automerge.yml", "agent-dispatch.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            self.assertIn(".github/scripts/agent_pipeline.py", text, name)

    def test_dispatch_is_manual_only_and_takes_an_issue_number(self) -> None:
        workflow = self._workflow("agent-dispatch.yml")
        triggers = self._triggers(workflow)
        self.assertEqual(list(triggers), ["workflow_dispatch"])
        self.assertIn("issue", triggers["workflow_dispatch"]["inputs"])

    def test_dispatching_workflows_pass_the_api_key_and_the_model_choice(self) -> None:
        # The model is a repository variable so it can change without a commit.
        for name in ("agent-unblock.yml", "agent-dispatch.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            self.assertIn("secrets.CURSOR_API_KEY", text, name)
            self.assertIn("vars.CURSOR_AGENT_MODEL", text, name)

    def test_pipeline_is_documented_for_the_next_agent(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Temporary agent pipeline for epic #56", agents)
        self.assertIn("agent-pipeline", agents)
        self.assertIn("ready-for-agent", agents)
        self.assertIn("self-removal issue #86", agents)


if __name__ == "__main__":
    unittest.main()
