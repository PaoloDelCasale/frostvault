#!/usr/bin/env python3
"""Summarize saved `gh run view --json jobs,...` and `--log` evidence.

Usage: .venv/bin/python sessioni/2026-11-11-ci-latency/measure.py
All measurements are retrospective; no production/DB calls.
"""
from datetime import datetime
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
NAME = "PostgreSQL migration and concurrency tests"


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def measure(stem):
    data = json.loads((ROOT / f"{stem}-jobs.json").read_text())
    jobs = data["jobs"]
    pg = next(job for job in jobs if job["name"] == NAME)
    lines = (ROOT / f"{stem}.log").read_text().splitlines()
    pg_lines = [line.split("\t", 2)[-1] for line in lines if line.startswith(NAME + "\t")]
    result = {
        "sha": data["headSha"],
        "conclusion": data["conclusion"],
        "workflow_seconds": (instant(data["updatedAt"]) - instant(data["createdAt"])).total_seconds(),
        "postgres_job_seconds": (instant(pg["completedAt"]) - instant(pg["startedAt"])).total_seconds(),
        "postgres_started": pg["startedAt"],
        "postgres_completed": pg["completedAt"],
    }
    previous_test = None
    for line in pg_lines:
        match = re.search(r"Ran (\d+) tests in ([\d.]+)s", line)
        if match:
            result["postgres_tests"] = int(match[1])
            result["postgres_unittest_seconds"] = float(match[2])
        if re.search(r"test_\w+ .* \.\.\. (ok|FAIL|ERROR)$", line):
            stamp = instant(line.split(" ", 1)[0])
            if "test_16_workers_1024_nested_uploads" in line and previous_test:
                result["nested_stress_interval_seconds"] = (stamp - previous_test).total_seconds()
            previous_test = stamp
        if "#535 throughput:" in line or "#458 acceptance:" in line:
            result.setdefault("stress_output", []).append(line)
    unit_times = []
    for line in lines:
        if line.startswith("Python unit tests (PostgreSQL shard"):
            match = re.search(r"Ran \d+ tests in ([\d.]+)s", line)
            if match:
                unit_times.append(float(match[1]))
    if unit_times:
        result["unit_shard_unittest_seconds"] = sorted(unit_times)
    return result


results = {}
for path in sorted(ROOT.glob("*-jobs.json")):
    stem = path.name.removesuffix("-jobs.json")
    if (ROOT / f"{stem}.log").exists():
        results[stem] = measure(stem)
print(json.dumps(results, indent=2))
