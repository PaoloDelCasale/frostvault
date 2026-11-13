#!/usr/bin/env python3
"""Compare immutable v0.5.3 with a candidate using ONE external harness.

Only disposable, loopback PostgreSQL 16 is accepted. No timing pass/fail gate.
See docs/ci.md; --help requires neither app imports nor a DB connection.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

BASELINE_SHA = "628d065fe54a5c13364d5da7c58cb563d0bd80b7"
HARNESS_FILES = ("upload_benchmark.py", "upload_benchmark_support.py", "upload_benchmark_worker.py")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--baseline", type=Path, required=True, help="Clean checkout at fixed v0.5.3 SHA")
    result.add_argument("--candidate", type=Path, required=True, help="Clean candidate checkout")
    result.add_argument("--output", type=Path, required=True, help="New artifact directory (must not exist)")
    result.add_argument("--disposable-postgres", action="store_true", required=True,
                        help="Acknowledge TEST_POSTGRES_URL names a disposable loopback PG16 service with CREATEDB")
    result.add_argument("--mode", choices=("s3_checksum", "read_back"), default="s3_checksum")
    result.add_argument("--files", type=int, default=32)
    result.add_argument("--concurrency", type=int, default=16)
    result.add_argument("--warmup", type=int, default=16)
    result.add_argument("--repetitions", type=int, default=3)
    return result


def validate_args(args):
    require(3 <= args.repetitions <= 5, "Use 3..5 repetitions on each SHA")
    require(1 <= args.concurrency <= 16, "Concurrency must be 1..16")
    require(args.concurrency <= args.files <= 128, "Files must be concurrency..128")
    require(args.concurrency <= args.warmup <= 32, "Warmup must be concurrency..32")
    require(not args.output.exists(), "Output already exists; choose a fresh artifact directory")


def run_order(repetitions):
    # Each instrumentation profile has >=3 independent repetitions per SHA.
    # AB/BA controls order; trace/plain order also alternates to limit drift.
    for repetition in range(repetitions):
        labels = ("baseline", "candidate") if repetition % 2 == 0 else ("candidate", "baseline")
        profiles = (True, False) if repetition % 2 == 0 else (False, True)
        for instrumented in profiles:
            for label in labels:
                yield repetition + 1, instrumented, label


def summary_stats(values):
    require(bool(values) and all(value > 0 for value in values), "Non-positive measurement")
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0
    return {"n": len(values), "mean": mean, "median": statistics.median(values),
            "min": min(values), "max": max(values), "stdev": sd,
            "coefficient_of_variation": sd / mean}


def aggregate(samples):
    require(bool(samples), "No samples")
    grouped = {}
    for sample in samples:
        require(sample["status"] == "ok", "Cannot aggregate an unsuccessful sample")
        require(sample["checks"]["completed_after_cleanup"] == sample["files"], "Incomplete sample")
        require(sample["checks"]["extra_puts"] == 0, "Extra PUT")
        require(sample["s3_operations"].get("PutObject") == sample["files"], "PUT count differs from completed files")
        key = sample["label"], sample["instrumented"]
        grouped.setdefault(key, []).append(sample)
    profiles = {}
    for (label, traced), group in grouped.items():
        require(len(group) >= 3, "At least three samples per SHA/profile are required")
        require(len({sample["repetition"] for sample in group}) == len(group), "Duplicate repetition")
        require(len({sample["sha"] for sample in group}) == 1, "SHA drift")
        profiles[f"{label}.{'traced' if traced else 'plain'}"] = {
            "sha": group[0]["sha"],
            "files_per_second": summary_stats([s["timing"]["files_per_second"] for s in group]),
            "elapsed_seconds": summary_stats([s["timing"]["elapsed_seconds"] for s in group]),
            "median_claim_to_completed_seconds": summary_stats([s["timing"]["claim_to_completed_seconds"]["median"] for s in group]),
            "p95_claim_to_completed_seconds": summary_stats([s["timing"]["claim_to_completed_seconds"]["p95"] for s in group]),
            "mean_work_per_completed_file": {
                metric: statistics.mean([s["probe"]["counts"].get(metric, 0) / s["files"] for s in group])
                for metric in sorted(set().union(*(s["probe"]["counts"] for s in group)))},
            "mean_s3_operations_per_completed_file": {
                op: statistics.mean([s["s3_operations"].get(op, 0) / s["files"] for s in group])
                for op in sorted(set().union(*(s["s3_operations"] for s in group)))}}
    require(set(profiles) == {f"{label}.{profile}" for label in ("baseline", "candidate") for profile in ("traced", "plain")}, "Missing SHA/profile")
    require(len({(s["mode"], s["files"], s["concurrency"], s["warmup_files"],
                  json.dumps(s["durability"], sort_keys=True), s["postgres_version_num"]) for s in samples}) == 1,
            "Workload/durability/server drift")
    return {"profiles": profiles,
            "candidate_over_baseline_median_throughput": {
                profile: profiles[f"candidate.{profile}"]["files_per_second"]["median"] /
                         profiles[f"baseline.{profile}"]["files_per_second"]["median"]
                for profile in ("traced", "plain")},
            "driver_probe_elapsed_overhead_ratio": {
                label: profiles[f"{label}.traced"]["elapsed_seconds"]["median"] /
                       profiles[f"{label}.plain"]["elapsed_seconds"]["median"] - 1
                for label in ("baseline", "candidate")}}


def markdown(summary):
    lines = ["# Upload execution comparison", "", "Descriptive evidence, **not a timing gate or AWS throughput claim**.", "",
             "Real PG16 + real SDK/client/facade orchestration; in-memory synchronous S3 API responses (no HTTP).",
             "Dataset: newly created temporary files, **10 KiB/file**; fixed mode/concurrency/durability on both SHAs.", "",
             "| SHA/profile | files/s median | stdev | CV | claim→completed median / p95 (s) |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for name, profile in sorted(summary["profiles"].items()):
        rate = profile["files_per_second"]
        lines.append(f"| {name} `{profile['sha'][:12]}` | {rate['median']:.3f} | {rate['stdev']:.3f} | {rate['coefficient_of_variation']:.3f} | "
                     f"{profile['median_claim_to_completed_seconds']['median']:.3f} / {profile['p95_claim_to_completed_seconds']['median']:.3f} |")
    lines += ["", "## Comparison and probe overhead", ""]
    for profile, ratio in summary["candidate_over_baseline_median_throughput"].items():
        lines.append(f"- Candidate / baseline median throughput ({profile}): **{ratio:.3f}×**.")
    for label, ratio in summary["driver_probe_elapsed_overhead_ratio"].items():
        lines.append(f"- {label}: traced/plain median elapsed − 1 = **{ratio:+.1%}** (noisy calibration, not subtracted).")
    lines += ["", "Driver timing is inclusive and may overlap implicit BEGIN timing; it is not CPU time or physical fsync count.",
              "Only the driver SQL probe is disabled in plain runs; API counting and the 50 ms lock observer remain identical.",
              "Raw per-sample JSON includes latency distributions, bounded SQL-shape trace, attempts/errors, commits/rollbacks,",
              "renewal/progress/receipt SQL, lock observations and S3 operation counts. See summary.json for per-file means.", "",
              "Timing: synchronized real claims/job loads through all process_job returns after completed/cleanup.",
              "Excluded: migration, seeding, warmup, initial claim connections, validation, readiness, scheduler nomination,",
              "pause/cancel and aggregate maintenance. No durability/concurrency/bandwidth changes between SHAs.", ""]
    return "\n".join(lines)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def worker_env():
    # Never inherit real cloud credentials, app .env, PG*, proxy or SDK settings.
    from psycopg.conninfo import conninfo_to_dict
    dsn = os.environ.get("TEST_POSTGRES_URL", "")
    require(bool(dsn), "TEST_POSTGRES_URL is required")
    try:
        parsed = conninfo_to_dict(dsn.replace("postgresql+psycopg://", "postgresql://", 1))
    except Exception:
        raise ValueError("Invalid TEST_POSTGRES_URL (value withheld)") from None
    require(parsed.get("host") in {"localhost", "127.0.0.1", "::1"}, "Only explicit loopback disposable PG is accepted")
    require(not parsed.get("hostaddr") or parsed["hostaddr"] in {"127.0.0.1", "::1"}, "PG hostaddr must also be loopback")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV", "LD_LIBRARY_PATH") if key in os.environ}
    env.update(TEST_POSTGRES_URL=dsn, AWS_ACCESS_KEY_ID="benchmark-only", AWS_SECRET_ACCESS_KEY="benchmark-only",
               AWS_DEFAULT_REGION="us-east-1", AWS_REGION="us-east-1", AWS_EC2_METADATA_DISABLED="true",
               AWS_ENDPOINT_URL="http://127.0.0.1:9", AWS_SHARED_CREDENTIALS_FILE=os.devnull,
               AWS_CONFIG_FILE=os.devnull, OPERATION_CONCURRENCY="16", BANDWIDTH_LIMIT_KIBPS="0",
               PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    return env


def main(argv=None):
    args = parser().parse_args(argv)
    validate_args(args)
    env = worker_env()
    env["OPERATION_CONCURRENCY"] = str(args.concurrency)
    env["UPLOAD_VERIFICATION"] = args.mode
    roots = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    shas = {label: git(root, "rev-parse", "HEAD") for label, root in roots.items()}
    require(shas["baseline"] == BASELINE_SHA, "Baseline must be the fixed v0.5.3 SHA")
    for root in roots.values():
        require(not git(root, "status", "--porcelain", "--untracked-files=no"), "Compared checkout must have no tracked modifications")
    # Each checkout migrates with its own real fixture: v0.6 rebuilt the schema
    # (ADR-0035), so the fixture API differs, not what a sample runs against
    # (a fresh clone of the head template on the same disposable PG16 server).
    fixtures = {label: hashlib.sha256((root / "tests/postgres.py").read_bytes()).hexdigest()
                for label, root in roots.items()}
    harness = Path(__file__).resolve().parent
    digest = hashlib.sha256(b"".join((harness / name).read_bytes() for name in HARNESS_FILES)).hexdigest()
    output = args.output.resolve()
    output.mkdir(parents=True)
    manifest = {"baseline_sha": shas["baseline"], "candidate_sha": shas["candidate"], "harness_sha256": digest,
                "postgres_fixture_sha256": fixtures, "python": platform.python_version(),
                "dependencies": {name: importlib.metadata.version(name)
                                 for name in ("psycopg", "psycopg-pool", "boto3", "botocore", "s3transfer")},
                "requirements_sha256": {label: hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest()
                                        for label, root in roots.items()},
                "python_executable": sys.executable, "platform": platform.platform(), "cpu_count": os.cpu_count(),
                "workload": {"files": args.files, "bytes_per_file": 10240, "concurrency": args.concurrency,
                             "warmup": args.warmup, "mode": args.mode, "repetitions": args.repetitions},
                "order": list(run_order(args.repetitions))}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    samples = []
    for repetition, instrumented, label in run_order(args.repetitions):
        profile = "traced" if instrumented else "plain"
        path = output / f"{repetition:02d}-{label}-{profile}.json"
        config = {"repo": str(roots[label]), "output": str(path), "sha": shas[label], "label": label,
                  "repetition": repetition, "instrumented": instrumented, "files": args.files,
                  "warmup": args.warmup, "concurrency": args.concurrency, "mode": args.mode}
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(harness / "upload_benchmark_worker.py")],
                                cwd=roots[label], env=env, input=json.dumps(config), text=True,
                                capture_output=True, timeout=600)
        # No child stderr/exception/provider text in artifacts or logs.
        require(result.returncode == 0, f"{label}/{profile}/{repetition} failed; inspect sanitized sample JSON")
        samples.append(json.loads(path.read_text()))
        print(f"{label} {shas[label][:12]} {profile} repeat {repetition}: valid ({time.monotonic() - started:.1f}s)", flush=True)
    summary = aggregate(samples)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (output / "summary.md").write_text(markdown(summary))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, subprocess.SubprocessError) as exc:
        # Our validation errors are fixed strings, never DSNs or provider errors.
        print(str(exc) if isinstance(exc, ValueError) else "Benchmark subprocess failed/timed out", file=sys.stderr)
        raise SystemExit(1)
