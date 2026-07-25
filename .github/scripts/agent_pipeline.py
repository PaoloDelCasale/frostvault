"""Dependency-driven agent pipeline: unblock the next issue, merge finished work.

GitHub records issue dependencies but nothing acts on them, and Cursor has no
notion of "blocked". This module is the missing link, in two commands:

``unblock <closed-issue>``
    Add ``ready-for-agent`` to every issue the closed one was blocking, but only
    when that issue's remaining blockers are all closed. The label is the event a
    Cursor Automation listens for, so labelling is what starts the next agent.

``merge``
    Sweep the open pull requests and squash-merge the ones that are finished.
    Merging closes the issue, which triggers ``unblock`` in turn.

Only issues carrying ``agent-pipeline`` are ever touched, so making an ordinary
issue depend on a pipeline issue never drags it into the pipeline.

Why a scheduled sweep instead of reacting to a workflow completion: this
repository forbids ``workflow_run`` and ``pull_request_target`` triggers (see
``tests/test_ci_contracts.py``), because both hand a privileged token to a
workflow selected by pull-request activity. A schedule keeps the privileged half
of the pipeline running from the default branch only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any, Iterable, Sequence

PIPELINE_LABEL = "agent-pipeline"
READY_LABEL = "ready-for-agent"
AGENT_BRANCH_PREFIX = "cursor/"

# A check run that ended in any other conclusion blocks the merge.
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

CLOSING_KEYWORD = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE
)


class GitHubCli:
    """Thin wrapper over the ``gh`` CLI, so the logic below can be tested."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _gh(self, args: Sequence[str]) -> str:
        result = subprocess.run(
            ["gh", *args], check=True, capture_output=True, text=True
        )
        return result.stdout

    def _api(self, path: str) -> Any:
        return json.loads(self._gh(["api", "--paginate", f"repos/{self.repo}/{path}"]))

    def blocking(self, issue: int) -> list[dict[str, Any]]:
        return self._api(f"issues/{issue}/dependencies/blocking")

    def blocked_by(self, issue: int) -> list[dict[str, Any]]:
        return self._api(f"issues/{issue}/dependencies/blocked_by")

    def labels(self, issue: int) -> set[str]:
        payload = self._api(f"issues/{issue}")
        return {label["name"] for label in payload.get("labels", [])}

    def add_label(self, issue: int, label: str) -> None:
        self._gh(["issue", "edit", str(issue), "--repo", self.repo, "--add-label", label])

    def open_pull_requests(self) -> list[dict[str, Any]]:
        fields = (
            "number,isDraft,isCrossRepository,body,headRefName,headRefOid,mergeable"
        )
        return json.loads(
            self._gh(
                [
                    "pr",
                    "list",
                    "--repo",
                    self.repo,
                    "--state",
                    "open",
                    "--limit",
                    "100",
                    "--json",
                    fields,
                ]
            )
        )

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        payload = self._api(f"commits/{sha}/check-runs")
        return payload.get("check_runs", [])

    def merge(self, number: int) -> None:
        self._gh(
            [
                "pr",
                "merge",
                str(number),
                "--repo",
                self.repo,
                "--squash",
                "--delete-branch",
            ]
        )


def closing_issue_number(body: str | None) -> int | None:
    """The issue a pull request body closes, or ``None``.

    A pull request that closes nothing is not part of the pipeline: merging it
    would never unblock anything.
    """
    match = CLOSING_KEYWORD.search(body or "")
    return int(match.group(1)) if match else None


def checks_verdict(check_runs: Iterable[dict[str, Any]]) -> str:
    """``"none"``, ``"pending"``, ``"failed"`` or ``"green"``.

    ``"none"`` matters: a commit with no checks at all must never be merged, or
    a misconfigured pipeline would merge everything instantly.
    """
    runs = list(check_runs)
    if not runs:
        return "none"
    if any(run.get("status") != "completed" for run in runs):
        return "pending"
    if any(run.get("conclusion") not in PASSING_CONCLUSIONS for run in runs):
        return "failed"
    return "green"


def failed_check_names(check_runs: Iterable[dict[str, Any]]) -> list[str]:
    return [
        f"{run.get('name')} ({run.get('conclusion')})"
        for run in check_runs
        if run.get("status") == "completed"
        and run.get("conclusion") not in PASSING_CONCLUSIONS
    ]


def unblock(client: GitHubCli, closed_issue: int) -> list[int]:
    """Label the dependents of ``closed_issue`` that nothing blocks any more."""
    labelled: list[int] = []
    for dependent in client.blocking(closed_issue):
        if dependent.get("state") != "open":
            continue
        number = int(dependent["number"])
        labels = {label["name"] for label in dependent.get("labels", [])}
        if PIPELINE_LABEL not in labels:
            print(f"#{number}: not in the pipeline, skipping.")
            continue
        if READY_LABEL in labels:
            print(f"#{number}: already ready, skipping.")
            continue
        open_blockers = [
            blocker["number"]
            for blocker in client.blocked_by(number)
            if blocker.get("state") == "open"
        ]
        if open_blockers:
            listed = ", ".join(f"#{blocker}" for blocker in open_blockers)
            print(f"#{number}: still blocked by {listed}.")
            continue
        client.add_label(number, READY_LABEL)
        labelled.append(number)
        print(f"#{number}: unblocked, labelled {READY_LABEL}.")
    if not labelled:
        print(f"#{closed_issue}: nothing to unblock.")
    return labelled


def merge_reason_to_skip(
    pull_request: dict[str, Any], issue_labels: set[str] | None, verdict: str
) -> str | None:
    """Why this pull request must not be merged, or ``None`` if it may be.

    Kept separate from the I/O so every gate is directly testable.
    """
    if pull_request.get("isDraft"):
        return "draft"
    if pull_request.get("isCrossRepository"):
        return "comes from a fork"
    branch = pull_request.get("headRefName", "")
    if not branch.startswith(AGENT_BRANCH_PREFIX):
        return f"branch {branch!r} is not an agent branch"
    if closing_issue_number(pull_request.get("body")) is None:
        return "closes no issue"
    if issue_labels is None or PIPELINE_LABEL not in issue_labels:
        return "the issue it closes is not in the pipeline"
    if pull_request.get("mergeable") != "MERGEABLE":
        return f"not mergeable ({pull_request.get('mergeable')})"
    if verdict == "none":
        return "no checks have reported"
    if verdict == "pending":
        return "checks are still running"
    if verdict == "failed":
        return "checks are not green"
    return None


def merge_ready(client: GitHubCli) -> list[int]:
    """Squash-merge every open pull request that has finished cleanly."""
    merged: list[int] = []
    for pull_request in client.open_pull_requests():
        number = int(pull_request["number"])
        issue = closing_issue_number(pull_request.get("body"))
        labels = client.labels(issue) if issue is not None else None
        verdict = (
            checks_verdict(client.check_runs(pull_request["headRefOid"]))
            if labels and PIPELINE_LABEL in labels
            else "none"
        )
        reason = merge_reason_to_skip(pull_request, labels, verdict)
        if reason:
            print(f"#{number}: {reason}, leaving it alone.")
            continue
        client.merge(number)
        merged.append(number)
        print(f"#{number}: merged, closing #{issue}.")
    if not merged:
        print("Nothing to merge.")
    return merged


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: agent_pipeline.py <repo> unblock <closed-issue>\n"
            "       agent_pipeline.py <repo> merge",
            file=sys.stderr,
        )
        return 2
    repo, command = argv[1], argv[2]
    client = GitHubCli(repo)
    if command == "unblock":
        if len(argv) < 4:
            print("unblock needs the closed issue number", file=sys.stderr)
            return 2
        unblock(client, int(argv[3]))
        return 0
    if command == "merge":
        merge_ready(client)
        return 0
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main(sys.argv))
