"""Job/provider/catalog Seam contract for the benchmark's dedicated PG16 job.

Not a test_*.py shard module: run explicitly in the separate benchmark CI job.
Uses the existing real Alembic/unique-database fixture, never a mocked database.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.upload_benchmark import worker_env

ROOT = Path(__file__).resolve().parents[1]


class UploadBenchmarkPostgresTests(unittest.TestCase):
    def test_completed_cleanup_exact_integrity_and_real_driver_trace_in_both_modes(self):
        for mode in ("s3_checksum", "read_back"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "sample.json"
                config = {"repo": str(ROOT), "output": str(output), "sha": "contract-test",
                          "label": "candidate", "repetition": 1, "instrumented": True,
                          "files": 4, "warmup": 2, "concurrency": 2, "mode": mode}
                env = worker_env()  # Missing disposable configuration is an error.
                env.update(OPERATION_CONCURRENCY="2", UPLOAD_VERIFICATION=mode)
                result = subprocess.run([sys.executable, str(ROOT / "scripts/upload_benchmark_worker.py")],
                    cwd=ROOT, input=json.dumps(config), text=True, capture_output=True, env=env, timeout=180)
                sample = json.loads(output.read_text()) if output.exists() else {}
                # Sanitized worker frames are safe and useful on a CI failure.
                self.assertEqual(result.returncode, 0, sample)
                self.assertEqual(sample["checks"]["completed_after_cleanup"], 4)
                self.assertEqual(sample["checks"]["extra_puts"], 0)
                self.assertEqual(sample["s3_operations"]["PutObject"], 4)
                self.assertGreaterEqual(sample["s3_operations"]["ListObjectVersions"], 4)
                self.assertEqual(sample["s3_operations"].get("GetObject", 0), 4 if mode == "read_back" else 0)
                counts = sample["probe"]["counts"]
                for key in ("app.sql.calls", "claim.sql.calls", "app.transaction.begin",
                            "app.transaction.commit", "app.receipt.insert_attempts",
                            "app.progress.update_attempts", "app.renewal.update_attempts"):
                    self.assertGreater(counts.get(key, 0), 0, key)
                self.assertGreater(counts["app.transaction.started"], 0)
                self.assertEqual(counts["app.transaction.started"], counts["app.transaction.finished"])
                self.assertTrue(sample["probe"]["sql_trace"])
                self.assertTrue(sample["locks"]["samples"])
                self.assertEqual(sample["durability"], {"fsync": "on", "synchronous_commit": "on", "full_page_writes": "on"})

    def test_missing_pg_configuration_fails_with_sanitized_error_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "failed.json"
            config = {"repo": str(ROOT), "output": str(output), "sha": "contract-test",
                      "label": "candidate", "repetition": 1, "instrumented": True,
                      "files": 2, "warmup": 2, "concurrency": 2, "mode": "s3_checksum"}
            env = worker_env()
            env.pop("TEST_POSTGRES_URL")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/upload_benchmark_worker.py")],
                cwd=ROOT, input=json.dumps(config), text=True, capture_output=True, env=env, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            failure = json.loads(output.read_text())
            self.assertEqual(failure["status"], "error")
            self.assertTrue(failure["frames"])
            self.assertNotIn("postgresql://", output.read_text())
