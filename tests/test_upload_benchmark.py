"""Targeted non-DB contracts for #562's external comparison harness."""
from __future__ import annotations

import base64
import copy
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import upload_benchmark as benchmark
from scripts.upload_benchmark_support import MemoryS3, Probe, sql_shape


def sample(label, traced, repetition):
    return {"status": "ok", "label": label, "instrumented": traced,
            "repetition": repetition, "sha": benchmark.BASELINE_SHA if label == "baseline" else "b" * 40,
            "files": 4, "concurrency": 2, "warmup_files": 2, "mode": "s3_checksum",
            "durability": {"fsync": "on", "synchronous_commit": "on", "full_page_writes": "on"},
            "postgres_version_num": 160012,
            "checks": {"completed_after_cleanup": 4, "extra_puts": 0},
            "timing": {"files_per_second": 8, "elapsed_seconds": 0.5,
                       "claim_to_completed_seconds": {"median": 0.1, "p95": 0.2}},
            "probe": {"counts": {"app.sql.calls": 40} if traced else {}},
            "s3_operations": {"PutObject": 4, "ListObjectVersions": 28}}


class ComparisonTests(unittest.TestCase):
    def test_frozen_baselines_keep_their_own_owner_schema_and_requester_cost(self):
        from scripts.upload_benchmark_worker import uses_legacy_owner_fixture
        for revision in ("0071_bulk_scheduler_merge", "0072_upload_lineage_indexes"):
            with self.subTest(revision=revision):
                self.assertTrue(uses_legacy_owner_fixture(revision))
        for revision in ("0074_owner_singleton", "0076_operational_retention", "0001_baseline"):
            with self.subTest(revision=revision):
                self.assertFalse(uses_legacy_owner_fixture(revision))

    def samples(self):
        return [sample(label, traced, repetition) for repetition, traced, label in benchmark.run_order(3)]

    def test_order_alternates_and_has_three_repetitions_for_every_sha_and_probe(self):
        order = list(benchmark.run_order(3))
        self.assertEqual(order[:4], [(1, True, "baseline"), (1, True, "candidate"),
                                    (1, False, "baseline"), (1, False, "candidate")])
        self.assertEqual(order[4:8], [(2, False, "candidate"), (2, False, "baseline"),
                                     (2, True, "candidate"), (2, True, "baseline")])
        result = benchmark.aggregate(self.samples())
        self.assertEqual(len(result["profiles"]), 4)
        self.assertEqual(result["candidate_over_baseline_median_throughput"], {"traced": 1, "plain": 1})
        self.assertEqual(result["profiles"]["candidate.traced"]["mean_work_per_completed_file"]["app.sql.calls"], 10)
        self.assertIn("not a timing gate", benchmark.markdown(result))
        json.dumps(result, allow_nan=False)

    def test_variance_and_probe_overhead_are_reported_not_used_as_gate(self):
        samples = self.samples()
        for row in samples:
            if row["label"] == "candidate":
                row["timing"]["files_per_second"] = float(row["repetition"])
                if row["instrumented"]:
                    row["timing"]["elapsed_seconds"] = 1
        result = benchmark.aggregate(samples)
        self.assertLess(result["candidate_over_baseline_median_throughput"]["traced"], 1)
        self.assertEqual(result["driver_probe_elapsed_overhead_ratio"]["candidate"], 1)
        self.assertGreater(result["profiles"]["candidate.traced"]["files_per_second"]["coefficient_of_variation"], 0)

    def test_invalid_or_incomparable_samples_fail_closed(self):
        for key, value in (("status", "error"), ("mode", "read_back"), ("concurrency", 16),
                           ("sha", "c" * 40), ("repetition", 2)):
            rows = self.samples()
            rows[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                benchmark.aggregate(rows)
        for field in ("completed_after_cleanup", "extra_puts"):
            rows = self.samples()
            rows[0]["checks"][field] = -1
            with self.subTest(field=field), self.assertRaises(ValueError):
                benchmark.aggregate(rows)
        rows = self.samples()
        rows[0]["s3_operations"]["PutObject"] = 5
        with self.assertRaises(ValueError):
            benchmark.aggregate(rows)
        with self.assertRaises(ValueError):
            benchmark.aggregate(self.samples()[1:])

    def test_cli_bounds_and_disposable_ack_are_required(self):
        with tempfile.TemporaryDirectory() as root:
            args = benchmark.parser().parse_args(["--baseline", root, "--candidate", root,
                "--output", str(Path(root) / "results"), "--disposable-postgres"])
            benchmark.validate_args(args)
            for field, value in (("repetitions", 2), ("files", 1024), ("concurrency", 32), ("warmup", 0)):
                invalid = copy.copy(args)
                setattr(invalid, field, value)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    benchmark.validate_args(invalid)

    def test_cli_writes_manifest_raw_samples_and_summary_from_one_worker_path(self):
        root = Path(__file__).resolve().parents[1]
        commands = []

        def fake_process(command, **kwargs):
            commands.append(command)
            config = json.loads(kwargs["input"])
            row = sample(config["label"], config["instrumented"], config["repetition"])
            row["sha"] = config["sha"]
            Path(config["output"]).write_text(json.dumps(row))
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(benchmark, "worker_env", return_value={}), \
                patch.object(benchmark.platform, "platform", return_value="test-only-runner"), \
                patch.object(benchmark, "git", side_effect=lambda root, *args: benchmark.BASELINE_SHA if args == ("rev-parse", "HEAD") else ""), \
                patch.object(benchmark.subprocess, "run", side_effect=fake_process), \
                contextlib.redirect_stdout(io.StringIO()):
            output = Path(temporary) / "results"
            self.assertEqual(benchmark.main(["--baseline", str(root), "--candidate", str(root),
                "--output", str(output), "--disposable-postgres", "--files", "4", "--concurrency", "2", "--warmup", "2"]), 0)
            self.assertEqual(len(commands), 12)
            self.assertEqual(len({tuple(command) for command in commands}), 1)
            self.assertEqual(len(list(output.glob("??-*.json"))), 12)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(len(manifest["order"]), 12)
            self.assertEqual(manifest["baseline_sha"], benchmark.BASELINE_SHA)
            self.assertEqual(len(manifest["harness_sha256"]), 64)
            self.assertIn("psycopg", manifest["dependencies"])
            self.assertTrue((output / "summary.md").is_file())
            self.assertEqual(json.loads((output / "summary.json").read_text())["candidate_over_baseline_median_throughput"]["traced"], 1)

    def test_environment_never_inherits_cloud_credentials_or_remote_pg(self):
        with patch.dict(os.environ, {"TEST_POSTGRES_URL": "postgresql://test@localhost/disposable",
                                    "AWS_SECRET_ACCESS_KEY": "do-not-export", "PGPASSWORD": "do-not-export",
                                    "DATABASE_URL": "do-not-export", "HTTPS_PROXY": "do-not-export"}, clear=True):
            env = benchmark.worker_env()
            self.assertNotIn("do-not-export", json.dumps(env))
            self.assertNotIn("PGPASSWORD", env)
            self.assertEqual(env["AWS_ENDPOINT_URL"], "http://127.0.0.1:9")
        for dsn in ("", "postgresql://test@example.com/db", "postgresql://test@localhost/db?hostaddr=192.0.2.1"):
            with patch.dict(os.environ, {"TEST_POSTGRES_URL": dsn}, clear=True), self.assertRaises(ValueError):
                benchmark.worker_env()


class ProviderTests(unittest.TestCase):
    def put(self, provider, key="docs/new", payload=b"content"):
        return provider.call("PutObject", {"Bucket": "bucket", "Key": key, "Body": io.BytesIO(payload),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(payload).digest()).decode(), "Metadata": {"opaque": "token"}})

    def test_exact_identity_checksum_and_pagination_counts_have_no_phantom_head(self):
        provider = MemoryS3(page_size=1)
        first = self.put(provider)
        self.put(provider, key="docs/second")
        page = provider.call("ListObjectVersions", {"Bucket": "bucket", "Prefix": "docs/"})
        self.assertTrue(page["IsTruncated"])
        second = provider.call("ListObjectVersions", {"Bucket": "bucket", "Prefix": "docs/",
            "KeyMarker": page["NextKeyMarker"], "VersionIdMarker": page["NextVersionIdMarker"]})
        self.assertFalse(second["IsTruncated"])
        self.assertEqual(len(page["Versions"]) + len(second["Versions"]), 2)
        result = provider.call("GetObject", {"Bucket": "bucket", "Key": "docs/new", "VersionId": first["VersionId"]})
        self.assertEqual(result["Body"].read(), b"content")
        self.assertEqual(provider.calls, {"PutObject": 2, "ListObjectVersions": 2, "GetObject": 1})
        self.assertEqual(result["Metadata"], {"opaque": "token"})

    def test_missing_exact_identity_bad_checksum_and_unexpected_writes_fail(self):
        provider = MemoryS3()
        with self.assertRaises(AssertionError):
            provider.call("HeadObject", {"Bucket": "bucket", "Key": "key"})
        with self.assertRaises(AssertionError):
            provider.call("PutObject", {"Bucket": "bucket", "Key": "key", "Body": b"bad", "ChecksumSHA256": "wrong"})
        self.assertFalse(provider.objects)
        with self.assertRaises(AssertionError):
            provider.call("DeleteObject", {"Bucket": "bucket", "Key": "key", "VersionId": "keeper"})
        provider.hook = lambda *a: (_ for _ in ()).throw(TimeoutError("fault injected at provider seam"))
        with self.assertRaises(TimeoutError):
            self.put(provider)
        self.assertFalse(provider.objects)

    def test_real_sdk_upload_facade_reads_body_and_retains_ack_progress(self):
        import boto3
        from botocore.config import Config
        from app.services.s3_upload import upload_file
        from app.services.upload_snapshot import prepare_upload_snapshot
        provider = MemoryS3()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "new.bin"
            path.write_bytes(b"x" * 10240)
            snapshot, _ = prepare_upload_snapshot(path, lambda: None)
            client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test-only",
                aws_secret_access_key="test-only", endpoint_url="http://127.0.0.1:9",
                config=Config(retries={"total_max_attempts": 1}))
            self.addCleanup(client.close)
            acknowledgments, progress = [], []
            with provider.installed():
                response = upload_file(client, source=path, bucket="bucket", key="new.bin", snapshot=snapshot,
                    check_active=lambda: None, on_progress=progress.append, on_acknowledged=acknowledgments.append)
            self.assertEqual(provider.calls, {"PutObject": 1})
            self.assertEqual(acknowledgments, [response])
            self.assertEqual(sum(progress), 10240)
            self.assertEqual(provider.objects["bucket", "new.bin", response["VersionId"]][0], path.read_bytes())


class WorkflowTests(unittest.TestCase):
    def test_separate_pg16_job_uses_fixed_baseline_same_harness_and_always_artifacts(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/migrations.yml").read_text())
        job = workflow["jobs"]["upload-execution-benchmark"]
        self.assertEqual(job["services"]["postgres"]["image"], "postgres:16")
        self.assertFalse(job.get("continue-on-error", False))
        checkouts = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 2)
        self.assertEqual(checkouts[1]["with"]["ref"], benchmark.BASELINE_SHA)
        run = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertEqual(run.count("python scripts/upload_benchmark.py"), 1)
        self.assertIn("tests.upload_benchmark_postgres_contract", run)
        self.assertEqual(job["strategy"]["matrix"]["concurrency"], [1, 4, 16])
        self.assertFalse(job["strategy"]["fail-fast"])
        self.assertIn("--mode s3_checksum --files 128 --concurrency ${{ matrix.concurrency }} --warmup 32 --repetitions 5", run)
        artifact = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(artifact["if"], "${{ always() }}")
        self.assertEqual(artifact["with"]["retention-days"], 7)
        self.assertEqual(artifact["with"]["name"], "upload-execution-comparison-${{ matrix.concurrency }}")
        self.assertNotIn("upload-execution-benchmark", workflow["jobs"]["unit-and-migration-tests"]["needs"])
        self.assertNotIn("upload-execution-benchmark", (root / "scripts/require_commit_checks.py").read_text())


class ProbeTests(unittest.TestCase):
    def test_receipt_renewal_progress_counts_do_not_require_baseline_statement_layout(self):
        probe = Probe()
        probe.record("WITH fence AS (SELECT id FROM vaults) UPDATE jobs SET transferred_bytes=%s, claim_expires_at=%s WHERE id=%s", 1, False)
        probe.record("WITH fact AS (INSERT INTO upload_receipts VALUES (%s) RETURNING intent_id) SELECT * FROM fact", 1, False)
        counts = probe.export()["counts"]
        self.assertEqual(counts["app.receipt.insert_attempts"], 1)
        self.assertEqual(counts["app.renewal.update_attempts"], 1)
        self.assertEqual(counts["app.progress.update_attempts"], 1)

    def test_redacted_bounded_shapes_separate_claim_observer_and_actual_work(self):
        probe = Probe()
        probe.record("UPDATE jobs SET claim_expires_at=%s WHERE id=%s", 1000, False)
        probe.record("UPDATE jobs SET transferred_bytes=%s WHERE id=%s", 2000, False)
        probe.record("INSERT INTO upload_receipts VALUES (%s)", 3000, False)
        probe.record(b"COMMIT", 4000, False, internal=True)
        with probe.scope("claim"):
            probe.record("BEGIN", 1, False, internal=True)
        with probe.scope("observer"):
            probe.record("SELECT confidential FROM production", 1, False)
        exported = probe.export()
        counts = exported["counts"]
        self.assertEqual(counts["app.receipt.insert_attempts"], 1)
        self.assertEqual(counts["app.renewal.update_attempts"], 1)
        self.assertEqual(counts["app.progress.update_attempts"], 1)
        self.assertEqual(counts["app.transaction.commit"], 1)
        self.assertEqual(counts["claim.transaction.begin"], 1)
        self.assertEqual(counts["app.sql.calls"], 3)
        self.assertNotIn("confidential", json.dumps(exported))
        shape = sql_shape("SELECT 'do-not-export' FROM jobs WHERE id=123 -- do-not-export")
        self.assertNotIn("do-not-export", shape)
        self.assertNotIn("123", shape)
