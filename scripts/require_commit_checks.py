#!/usr/bin/env python3
"""Fail closed unless required GitHub check runs succeeded for one SHA.

Used by the image publish workflow so a tag, main push, or manual dispatch
cannot promote a commit whose SQLite, PostgreSQL, MinIO, or frontend jobs
are missing, pending, cancelled, or failed. Counters and scrapers stay
process-local; this gate is about the commit under promotion.

Does not use workflow_run or pull_request_target.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DEFAULT_REQUIRED_CHECKS = (
    "Unit and migration tests",
    "PostgreSQL migration and concurrency tests",
    "S3-compatible integrity (MinIO)",
    "Frontend generate and typecheck",
    "Frontend lint and unit tests",
    "Frontend unit tests (Node 24)",
    "Frontend production build",
    "Playwright e2e (desktop-1280)",
    "Playwright e2e (mobile-375)",
    "Production image PostgreSQL backup",
)

_FAIL_CONCLUSIONS = frozenset(
    {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
        "skipped",
        "neutral",
    }
)


def latest_check_by_name(
    check_runs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep the newest run for each check name."""
    latest: dict[str, dict[str, Any]] = {}
    for run in check_runs:
        name = str(run.get("name") or "")
        if not name:
            continue
        current = latest.get(name)
        if current is None or _run_sort_key(run) >= _run_sort_key(current):
            latest[name] = run
    return latest


def _run_sort_key(run: dict[str, Any]) -> str:
    return str(
        run.get("completed_at")
        or run.get("started_at")
        or run.get("id")
        or ""
    )


def evaluate_required_checks(
    required: list[str],
    check_runs: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Return ``success``, ``pending``, or ``failed`` plus human-readable lines."""
    latest = latest_check_by_name(check_runs)
    details: list[str] = []
    pending = False
    failed = False
    for name in required:
        run = latest.get(name)
        if run is None:
            pending = True
            details.append(f"missing: {name}")
            continue
        status = str(run.get("status") or "")
        conclusion = str(run.get("conclusion") or "")
        if status != "completed":
            pending = True
            details.append(f"pending: {name} ({status})")
            continue
        if conclusion != "success":
            failed = True
            details.append(f"failed: {name} ({conclusion or 'unknown'})")
            continue
        details.append(f"ok: {name}")
    if failed:
        return "failed", details
    if pending:
        return "pending", details
    return "success", details


def fetch_check_runs(
    *,
    repository: str,
    sha: str,
    token: str,
    opener: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Page GitHub check-runs for ``sha``. Empty pages stop pagination."""
    request = opener or urllib.request.urlopen
    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        url = (
            f"https://api.github.com/repos/{repository}/commits/"
            f"{urllib.parse.quote(sha)}/check-runs?{query}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "frostvault-require-commit-checks",
            },
        )
        try:
            with request(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise SystemExit(
                f"GitHub check-runs request failed ({exc.code}): {body}"
            ) from exc
        batch = payload.get("check_runs") if isinstance(payload, dict) else None
        if not isinstance(batch, list) or not batch:
            break
        runs.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
        page += 1
        if page > 20:
            break
    return runs


def wait_for_required_checks(
    *,
    repository: str,
    sha: str,
    token: str,
    required: list[str],
    timeout_seconds: int,
    interval_seconds: int,
    fetch: Callable[..., list[dict[str, Any]]] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> int:
    deadline = now() + max(0, timeout_seconds)
    fetch_runs = fetch or (
        lambda: fetch_check_runs(repository=repository, sha=sha, token=token)
    )
    last_details: list[str] = []
    while True:
        runs = fetch_runs()
        state, details = evaluate_required_checks(required, runs)
        last_details = details
        for line in details:
            print(line, flush=True)
        if state == "success":
            print(f"All required checks succeeded for {sha}", flush=True)
            return 0
        if state == "failed":
            print(f"Required checks failed for {sha}", file=sys.stderr)
            return 1
        if now() >= deadline:
            print(
                f"Required checks still pending or missing for {sha}",
                file=sys.stderr,
            )
            for line in last_details:
                print(line, file=sys.stderr)
            return 1
        sleeper(max(1, interval_seconds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="Exact commit SHA to gate")
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name (default: GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub token with checks:read (default: GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--check",
        action="append",
        dest="checks",
        default=[],
        help="Required check-run name (repeatable)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1500)
    parser.add_argument("--interval-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    sha = args.sha.strip().lower()
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        print("sha must be a 40-character lowercase hex commit", file=sys.stderr)
        return 64
    if not args.repository or "/" not in args.repository:
        print("repository owner/name is required", file=sys.stderr)
        return 64
    if not args.token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 64
    required = args.checks or list(DEFAULT_REQUIRED_CHECKS)
    return wait_for_required_checks(
        repository=args.repository,
        sha=sha,
        token=args.token,
        required=required,
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
