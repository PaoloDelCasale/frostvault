"""Dependency-driven agent pipeline: start the next issue, merge finished work.

GitHub records issue dependencies but nothing acts on them, and Cursor has no
notion of "blocked". This module is the missing link, in three commands:

``unblock <closed-issue>``
    Label ``ready-for-agent`` on every issue the closed one was blocking, but
    only when that issue's remaining blockers are all closed — then start a cloud
    agent on each of them.

``dispatch <issue>``
    Start a cloud agent on one issue. Used to kick off the first issue of a
    chain, and by ``unblock`` for each issue it just released.

``merge``
    Sweep the open pull requests: squash-merge the ones that are finished, and
    re-dispatch a repair agent for agent PRs whose checks have failed (Cursor
    marking a run IDLE is not the same as CI green). After each merge, run
    ``unblock`` (and dispatch) in-process: a merge done with ``GITHUB_TOKEN``
    closes the linked issue, but GitHub does not re-fire ``issues: closed``
    workflows for that token, so relying on ``agent-unblock.yml`` alone would
    stall the chain.

The target issue must both carry ``agent-pipeline`` and be one of the explicitly
enumerated sub-issues of epic #56. The allowlist is deliberately not reusable:
the final sub-issue removes this module, its workflows, tests, documentation and
label, then closes the epic.

Two design notes worth keeping:

Agents are started through one private Cursor Automation webhook. This gives the
repository only the narrow credential for this one temporary automation rather
than a personal Cursor API key capable of launching arbitrary agents.

The merge half runs on a schedule instead of reacting to a workflow completion,
because this repository forbids ``workflow_run`` and ``pull_request_target``
triggers (see ``tests/test_ci_contracts.py``): both hand a privileged token to a
workflow selected by pull-request activity.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Sequence

PIPELINE_LABEL = "agent-pipeline"
READY_LABEL = "ready-for-agent"
DISPATCHED_LABEL = "agent-dispatched"
REPAIR_LABEL_PREFIX = "agent-repair-"
AGENT_BRANCH_PREFIX = "cursor/"
EPIC_ISSUE = 56
BOOTSTRAP_ISSUE = 84
CLEANUP_ISSUE = 86
PIPELINE_ISSUES = frozenset({*range(57, 73), CLEANUP_ISSUE})
UNBLOCK_SOURCES = PIPELINE_ISSUES | {BOOTSTRAP_ISSUE}

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

    def title(self, issue: int) -> str:
        return str(self._api(f"issues/{issue}").get("title", ""))

    def add_label(self, issue: int, label: str) -> None:
        self._gh(["issue", "edit", str(issue), "--repo", self.repo, "--add-label", label])

    def remove_label(self, issue: int, label: str) -> None:
        self._gh(
            ["issue", "edit", str(issue), "--repo", self.repo, "--remove-label", label]
        )

    def open_pull_requests(self) -> list[dict[str, Any]]:
        fields = (
            "number,isDraft,isCrossRepository,body,headRefName,headRefOid,"
            "mergeable,mergeStateStatus"
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

    def close_issue(self, number: int) -> None:
        self._gh(["issue", "close", str(number), "--repo", self.repo])

    def delete_label(self, label: str) -> None:
        self._gh(["label", "delete", label, "--repo", self.repo, "--yes"])


class CursorAutomation:
    """Starts the one temporary, repo-backed Cursor Automation."""

    def __init__(
        self,
        webhook_url: str,
        webhook_key: str,
        sender: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.webhook_key = webhook_key
        self._sender = sender or self._post

    def payload(
        self,
        repo: str,
        issue: int,
        title: str,
        instructions: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return {
            "event": "frostvault_epic_56_issue_ready",
            "idempotency_key": idempotency_key or f"{repo}#{issue}",
            "repository": repo,
            "issue": {
                "number": issue,
                "title": title,
                "url": f"https://github.com/{repo}/issues/{issue}",
            },
            "instructions": instructions,
        }

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.webhook_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {"accepted": True}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise RuntimeError(
                f"Cursor Automation webhook returned {error.code}: {detail}"
            ) from error

    def start(
        self,
        repo: str,
        issue: int,
        title: str,
        instructions: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._sender(
            self.payload(
                repo, issue, title, instructions, idempotency_key=idempotency_key
            )
        )


def repair_marker(sha: str) -> str:
    """Label that records a repair dispatch already launched for this head SHA."""
    return f"{REPAIR_LABEL_PREFIX}{sha[:7]}"


def agent_prompt(repo: str, issue: int, title: str) -> str:
    """The instructions handed to a cloud agent for one pipeline issue."""
    return f"""Work GitHub issue #{issue} in {repo}: {title}

Read the issue body first, then its parent epic and every issue it depends on, as
AGENTS.md requires. The epic marks some decisions as settled: apply them, do not
reopen them. If the issue seems to contradict the epic, say so in a comment on the
issue instead of diverging.

Implement it with the /tdd skill in vertical slices: one seam, one failing test,
the minimum code that makes it pass, then the next. The seams are listed in the
issue body and are already agreed, so do not stop to ask for confirmation, and do
not write all the tests up front.

Run the whole suite before opening a pull request, not only your own tests:

  .venv/bin/python -m unittest discover -s tests
  node --test tests/*.mjs
  cd frontend && npm ci && npm run lint && npm run test   # only if frontend/ exists

Open a **ready-for-review** (not draft) pull request whose body contains
"Closes #{issue}", states what you tested and how, and attaches a 375px
screenshot for any UI change. Do not leave the pull request as a draft —
drafts are never auto-merged.

Opening the PR is not the end of the job. Cursor may show the run as idle; that
does **not** mean the work is done. After the PR exists, poll GitHub Checks until
every check on the head commit has completed:

  gh pr checks <pr-number> --watch

If any check fails, read the logs (`gh run view <run-id> --log-failed`), fix the
cause, push to the same branch, and wait again. Repeat until every check is green
(or `mergeStateStatus` is CLEAN/HAS_HOOKS). Auto-merge then squash-merges; you
are finished only when the PR can merge, not when the first PR URL appears."""


def repair_prompt(
    repo: str,
    issue: int,
    title: str,
    pull_request: int,
    branch: str,
    failed_summary: str,
) -> str:
    """Instructions for a follow-up agent when CI failed and the prior run went idle."""
    return f"""Repair open pull request #{pull_request} for GitHub issue #{issue} in {repo}: {title}

The previous agent stopped while GitHub Checks were still failing ({failed_summary}).
An idle Cursor run is not a finished pipeline issue.

Do **not** open a new pull request. Check out branch `{branch}` (PR #{pull_request}),
fix the failures, push to that same branch, and keep going until every check on
the head commit is green.

Read failures with:

  gh pr checks {pull_request}
  gh run view <run-id> --log-failed

You are finished only when `gh pr checks {pull_request}` shows every check
passing (or mergeStateStatus is CLEAN/HAS_HOOKS). Auto-merge will then squash-
merge the PR."""


def dispatch(
    client: GitHubCli,
    automation: CursorAutomation | None,
    issue: int,
    repo: str,
    *,
    force: bool = False,
    instructions: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Start the automation on ``issue``. Returns what happened, for logging."""
    if issue not in PIPELINE_ISSUES:
        print(
            f"#{issue}: not a sub-issue of the temporary epic #{EPIC_ISSUE}; "
            "refusing to dispatch."
        )
        return "outside-epic"
    if DISPATCHED_LABEL in client.labels(issue):
        if not force:
            print(f"#{issue}: already dispatched.")
            return "already-dispatched"
        client.remove_label(issue, DISPATCHED_LABEL)
    if automation is None:
        print(
            f"#{issue}: Cursor Automation webhook secrets are missing; leaving "
            "it labelled for manual recovery."
        )
        return "skipped"
    title = client.title(issue)
    automation.start(
        repo,
        issue,
        title,
        instructions or agent_prompt(repo, issue, title),
        idempotency_key=idempotency_key,
    )
    client.add_label(issue, DISPATCHED_LABEL)
    print(f"#{issue}: dispatched through the temporary Cursor Automation.")
    return "dispatched"


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
    if closed_issue not in UNBLOCK_SOURCES:
        print(
            f"#{closed_issue}: outside the temporary epic #{EPIC_ISSUE}, "
            "ignoring it."
        )
        return []
    labelled: list[int] = []
    for dependent in client.blocking(closed_issue):
        if dependent.get("state") != "open":
            continue
        number = int(dependent["number"])
        if number not in PIPELINE_ISSUES:
            print(
                f"#{number}: not a sub-issue of the temporary epic "
                f"#{EPIC_ISSUE}, skipping."
            )
            continue
        labels = {label["name"] for label in dependent.get("labels", [])}
        if PIPELINE_LABEL not in labels:
            print(f"#{number}: not in the pipeline, skipping.")
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
        live_labels = client.labels(number) | labels
        if DISPATCHED_LABEL in live_labels:
            print(f"#{number}: already dispatched, skipping.")
            continue
        if READY_LABEL not in live_labels:
            client.add_label(number, READY_LABEL)
            print(f"#{number}: unblocked, labelled {READY_LABEL}.")
        else:
            print(f"#{number}: already ready, queueing dispatch.")
        labelled.append(number)
    if not labelled:
        print(f"#{closed_issue}: nothing to unblock.")
    return labelled


def merge_reason_to_skip(
    pull_request: dict[str, Any],
    issue_labels: set[str] | None,
    verdict: str,
    open_blockers: Sequence[int] | None = None,
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
    issue = closing_issue_number(pull_request.get("body"))
    if issue is None:
        return "closes no issue"
    if issue not in PIPELINE_ISSUES:
        return f"issue #{issue} is not part of temporary epic #{EPIC_ISSUE}"
    if issue_labels is None or PIPELINE_LABEL not in issue_labels:
        return "the issue it closes is not in the pipeline"
    if open_blockers:
        listed = ", ".join(f"#{blocker}" for blocker in open_blockers)
        return f"issue still blocked by {listed}"
    if pull_request.get("mergeable") != "MERGEABLE":
        return f"not mergeable ({pull_request.get('mergeable')})"
    # GitHub reports MERGEABLE while the branch is still behind required checks'
    # base; squash then fails with "head branch is not up to date". Only CLEAN
    # (and HAS_HOOKS) are safe to attempt. UNSTABLE means a non-required check
    # failed; leave those alone so humans can decide.
    merge_state = pull_request.get("mergeStateStatus")
    if merge_state and merge_state not in {"CLEAN", "HAS_HOOKS"}:
        return f"merge state is {merge_state}"
    if verdict == "none":
        return "no checks have reported"
    if verdict == "pending":
        return "checks are still running"
    if verdict == "failed":
        return "checks are not green"
    return None


def merge_ready(
    client: GitHubCli,
    automation: CursorAutomation | None = None,
) -> list[int]:
    """Squash-merge every open pull request that has finished cleanly.

    After each successful merge, unblock dependents and dispatch agents in the
    same process. ``GITHUB_TOKEN`` merges close linked issues without firing
    ``agent-unblock.yml``, so the chain would stop without this step.
    """
    merged: list[int] = []
    for pull_request in client.open_pull_requests():
        number = int(pull_request["number"])
        issue = closing_issue_number(pull_request.get("body"))
        labels = (
            client.labels(issue)
            if issue is not None and issue in PIPELINE_ISSUES
            else None
        )
        open_blockers: list[int] | None = None
        if issue is not None and labels and PIPELINE_LABEL in labels:
            open_blockers = [
                int(blocker["number"])
                for blocker in client.blocked_by(issue)
                if blocker.get("state") == "open"
            ]
        verdict = (
            checks_verdict(client.check_runs(pull_request["headRefOid"]))
            if labels and PIPELINE_LABEL in labels
            else "none"
        )
        reason = merge_reason_to_skip(
            pull_request, labels, verdict, open_blockers=open_blockers
        )
        if reason:
            print(f"#{number}: {reason}, leaving it alone.")
            continue
        try:
            client.merge(number)
        except Exception as error:  # noqa: BLE001 - keep sweeping other PRs
            print(f"#{number}: merge failed ({error}), leaving it alone.")
            continue
        merged.append(number)
        print(f"#{number}: merged, closing #{issue}.")
        if issue is not None:
            for next_issue in unblock(client, issue):
                dispatch(client, automation, next_issue, client.repo)
        if issue == CLEANUP_ISSUE:
            # This process is running the pre-merge copy of the script, so it can
            # finish after the cleanup PR has removed the checked-in automation.
            client.close_issue(EPIC_ISSUE)
            client.delete_label(PIPELINE_LABEL)
            client.delete_label(DISPATCHED_LABEL)
            print(
                f"Temporary epic #{EPIC_ISSUE} closed and dispatch labels deleted."
            )
    if not merged:
        print("Nothing to merge.")
    return merged


def repair_failed(
    client: GitHubCli,
    automation: CursorAutomation | None = None,
) -> list[int]:
    """Re-dispatch when an agent PR's checks failed and the prior run went idle.

    Cursor marks automations IDLE when the conversation ends; that is not the
    same as CI green. One repair attempt is allowed per failing head SHA
    (``agent-repair-<sha7>``). The repair agent is told to fix the existing PR
    branch, not open a new one.
    """
    repaired: list[int] = []
    for pull_request in client.open_pull_requests():
        if pull_request.get("isDraft") or pull_request.get("isCrossRepository"):
            continue
        branch = pull_request.get("headRefName", "")
        if not branch.startswith(AGENT_BRANCH_PREFIX):
            continue
        issue = closing_issue_number(pull_request.get("body"))
        if issue is None or issue not in PIPELINE_ISSUES:
            continue
        labels = client.labels(issue)
        if PIPELINE_LABEL not in labels:
            continue
        sha = str(pull_request.get("headRefOid") or "")
        if not sha:
            continue
        marker = repair_marker(sha)
        if marker in labels:
            print(
                f"#{pull_request['number']}: already repaired for {sha[:7]}, "
                "leaving it alone."
            )
            continue
        check_runs = client.check_runs(sha)
        if checks_verdict(check_runs) != "failed":
            continue
        failed = ", ".join(failed_check_names(check_runs)) or "failed checks"
        number = int(pull_request["number"])
        if automation is None:
            print(
                f"#{number}: checks failed ({failed}); webhook secrets missing, "
                "cannot dispatch a repair."
            )
            continue
        title = client.title(issue)
        result = dispatch(
            client,
            automation,
            issue,
            client.repo,
            force=True,
            instructions=repair_prompt(
                client.repo, issue, title, number, branch, failed
            ),
            idempotency_key=f"{client.repo}#{issue}:repair:{sha[:12]}",
        )
        if result != "dispatched":
            print(f"#{number}: repair dispatch returned {result}.")
            continue
        client.add_label(issue, marker)
        repaired.append(number)
        print(f"#{number}: dispatched repair agent for failed checks ({failed}).")
    if not repaired:
        print("Nothing to repair.")
    return repaired


def automation_from_environment() -> CursorAutomation | None:
    """Build the webhook client from workflow secrets, or ``None`` if unset."""
    import os

    webhook_url = os.environ.get("CURSOR_EPIC_56_WEBHOOK_URL", "").strip()
    webhook_key = os.environ.get("CURSOR_EPIC_56_WEBHOOK_KEY", "").strip()
    if not webhook_url or not webhook_key:
        return None
    return CursorAutomation(
        webhook_url=webhook_url,
        webhook_key=webhook_key,
    )


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: agent_pipeline.py <repo> unblock <closed-issue>\n"
            "       agent_pipeline.py <repo> dispatch <issue>\n"
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
        automation = automation_from_environment()
        for issue in unblock(client, int(argv[3])):
            dispatch(client, automation, issue, repo)
        return 0
    if command == "dispatch":
        if len(argv) < 4:
            print("dispatch needs the issue number", file=sys.stderr)
            return 2
        issue = int(argv[3])
        if issue not in PIPELINE_ISSUES:
            print(
                f"#{issue}: not a sub-issue of temporary epic #{EPIC_ISSUE}; "
                "refusing to dispatch.",
                file=sys.stderr,
            )
            return 1
        labels = client.labels(issue)
        if PIPELINE_LABEL not in labels:
            print(f"#{issue}: not in the pipeline, refusing to dispatch.", file=sys.stderr)
            return 1
        if READY_LABEL not in labels:
            client.add_label(issue, READY_LABEL)
        dispatch(client, automation_from_environment(), issue, repo)
        return 0
    if command == "merge":
        automation = automation_from_environment()
        merge_ready(client, automation)
        repair_failed(client, automation)
        return 0
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main(sys.argv))
