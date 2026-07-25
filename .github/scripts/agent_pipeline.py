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
    Sweep the open pull requests and squash-merge the ones that are finished.
    Merging closes the issue, which triggers ``unblock`` in turn.

Only issues carrying ``agent-pipeline`` are ever touched, so making an ordinary
issue depend on a pipeline issue never drags it into the pipeline.

Two design notes worth keeping:

Agents are started through ``POST /v1/agents`` rather than by a Cursor
Automation reacting to the label. Automations can trigger on a label added to a
*pull request*, but not on a label added to an *issue*, which is the event this
pipeline produces. Calling the API keeps the trigger in our hands. When
``CURSOR_API_KEY`` is absent the pipeline still labels issues, so an Automation
watching the label remains a valid alternative.

The merge half runs on a schedule instead of reacting to a workflow completion,
because this repository forbids ``workflow_run`` and ``pull_request_target``
triggers (see ``tests/test_ci_contracts.py``): both hand a privileged token to a
workflow selected by pull-request activity.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterable, Sequence

PIPELINE_LABEL = "agent-pipeline"
READY_LABEL = "ready-for-agent"
AGENT_BRANCH_PREFIX = "cursor/"

CURSOR_AGENTS_URL = "https://api.cursor.com/v1/agents"
CURSOR_MODELS_URL = "https://api.cursor.com/v1/models"

# Namespace for deterministic agent ids: re-dispatching an issue must not create a
# second agent working the same branch.
AGENT_ID_NAMESPACE = uuid.UUID("6f2d0a1e-2f2a-4f3d-9a4b-1c5d6e7f8a90")

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


class CursorAgents:
    """Launches cloud agents through the Cursor Cloud Agents API."""

    def __init__(
        self,
        api_key: str,
        repo_url: str,
        starting_ref: str = "main",
        model_id: str | None = None,
        model_params: list[dict[str, str]] | None = None,
        environment: str | None = None,
        sender: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.repo_url = repo_url
        self.starting_ref = starting_ref
        self.model_id = model_id
        self.model_params = model_params
        self.environment = environment
        self._sender = sender or self._post

    def payload(self, issue: int, title: str, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "prompt": {"text": prompt},
            "autoCreatePR": True,
            "name": f"Issue #{issue}: {title}"[:100],
            "agentId": agent_id_for(self.repo_url, issue),
        }
        if self.environment:
            # A named environment already carries its repository, and the two are
            # mutually exclusive in the API.
            body["env"] = {"type": "cloud", "name": self.environment}
        else:
            body["repos"] = [
                {"url": self.repo_url, "startingRef": self.starting_ref}
            ]
        if self.model_id:
            model: dict[str, Any] = {"id": self.model_id}
            if self.model_params:
                model["params"] = self.model_params
            body["model"] = model
        return body

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            CURSOR_AGENTS_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            if error.code == 409:
                return {"conflict": True, "detail": detail}
            raise RuntimeError(
                f"Cursor API returned {error.code}: {detail}"
            ) from error

    def create(self, issue: int, title: str, prompt: str) -> dict[str, Any]:
        return self._sender(self.payload(issue, title, prompt))

    def models(self) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            CURSOR_MODELS_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return list(payload.get("items", []))

    def model_error(self) -> str | None:
        """Why the configured model cannot be used, or ``None`` if it can.

        Worth the extra call: a wrong model id is otherwise an opaque 400 at
        dispatch time, and a wrong parameter silently gets you a different variant
        from the one you asked for.
        """
        if not self.model_id:
            return None
        return model_selection_error(self.models(), self.model_id, self.model_params)


def model_selection_error(
    items: Iterable[dict[str, Any]],
    model_id: str,
    params: list[dict[str, str]] | None,
) -> str | None:
    """Validate a model id and its parameters against ``GET /v1/models``."""
    catalogue = list(items)
    match = next(
        (
            item
            for item in catalogue
            if item.get("id") == model_id or model_id in (item.get("aliases") or [])
        ),
        None,
    )
    if match is None:
        available = ", ".join(sorted(str(item.get("id")) for item in catalogue))
        return f"unknown model {model_id!r}. Available: {available}"

    if not params:
        return None

    accepted = {
        str(parameter.get("id")): {
            str(value.get("value")) for value in parameter.get("values") or []
        }
        for parameter in match.get("parameters") or []
    }
    for parameter in params:
        name, value = parameter["id"], parameter["value"]
        if name not in accepted:
            known = ", ".join(sorted(accepted)) or "none"
            return (
                f"model {model_id!r} does not accept the parameter {name!r}. "
                f"Accepted parameters: {known}"
            )
        if accepted[name] and value not in accepted[name]:
            allowed = ", ".join(sorted(accepted[name]))
            return (
                f"model {model_id!r} does not accept {name}={value!r}. "
                f"Allowed values: {allowed}"
            )
    return None


def agent_id_for(repo_url: str, issue: int) -> str:
    """A stable agent id per issue, so a repeated dispatch cannot fork the work."""
    seed = f"{repo_url}#{issue}"
    return f"bc-{uuid.uuid5(AGENT_ID_NAMESPACE, seed)}"


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

Open a pull request whose body contains "Closes #{issue}", states what you tested
and how, and attaches a 375px screenshot for any UI change. Auto-merge is enabled:
the pull request merges as soon as every check is green, so nothing else reviews
it. Do not open it with tests you have not run."""


def dispatch(
    client: GitHubCli, cursor: CursorAgents | None, issue: int, repo: str
) -> str:
    """Start a cloud agent on ``issue``. Returns what happened, for logging."""
    if cursor is None:
        print(
            f"#{issue}: no CURSOR_API_KEY, leaving it labelled for an Automation "
            "to pick up."
        )
        return "skipped"
    problem = cursor.model_error()
    if problem:
        raise RuntimeError(
            f"Refusing to dispatch #{issue}: {problem}. Fix the "
            "CURSOR_AGENT_MODEL / CURSOR_AGENT_MODEL_PARAMS repository variables."
        )
    title = client.title(issue)
    response = cursor.create(issue, title, agent_prompt(repo, issue, title))
    if response.get("conflict"):
        print(f"#{issue}: an agent was already dispatched for this issue.")
        return "already-dispatched"
    print(f"#{issue}: dispatched agent {response.get('id', '?')}.")
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


def cursor_from_environment(repo: str) -> CursorAgents | None:
    """Build the API client from the workflow environment, or ``None`` if unset.

    Without a key the pipeline degrades to labelling only, which keeps a Cursor
    Automation watching ``ready-for-agent`` as a working alternative.
    """
    api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not api_key:
        return None
    model_id = os.environ.get("CURSOR_AGENT_MODEL", "").strip() or None
    raw_params = os.environ.get("CURSOR_AGENT_MODEL_PARAMS", "").strip()
    model_params: list[dict[str, str]] | None = None
    if raw_params:
        # "fast=true,thinking=high" — kept as a flat string so it can live in a
        # repository variable rather than in code.
        model_params = [
            {"id": key.strip(), "value": value.strip()}
            for key, _, value in (item.partition("=") for item in raw_params.split(","))
            if key.strip() and value.strip()
        ]
    return CursorAgents(
        api_key=api_key,
        repo_url=f"https://github.com/{repo}",
        starting_ref=os.environ.get("CURSOR_AGENT_BASE_REF", "main").strip() or "main",
        model_id=model_id,
        model_params=model_params,
        environment=os.environ.get("CURSOR_AGENT_ENV", "").strip() or None,
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
        cursor = cursor_from_environment(repo)
        for issue in unblock(client, int(argv[3])):
            dispatch(client, cursor, issue, repo)
        return 0
    if command == "dispatch":
        if len(argv) < 4:
            print("dispatch needs the issue number", file=sys.stderr)
            return 2
        issue = int(argv[3])
        labels = client.labels(issue)
        if PIPELINE_LABEL not in labels:
            print(f"#{issue}: not in the pipeline, refusing to dispatch.", file=sys.stderr)
            return 1
        if READY_LABEL not in labels:
            client.add_label(issue, READY_LABEL)
        dispatch(client, cursor_from_environment(repo), issue, repo)
        return 0
    if command == "merge":
        merge_ready(client)
        return 0
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main(sys.argv))
