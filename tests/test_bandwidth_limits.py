"""The one optional upload bandwidth limit (#413, #366 Q3).

Seams under test:
- ``upload_bwlimit_arg`` formats the global ``BANDWIDTH_LIMIT_KIBPS``.
- ``process_jobs_once`` attaches it to upload Jobs only; other transfers are
  never throttled and no per-Vault limit exists.
- ``run_rclone`` receives ``--bwlimit`` before the command arguments.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.database import SQLiteConnection
from app.services.operation_policies import upload_bwlimit_arg
from app.storage import process_jobs_once, run_rclone
from tests.test_job_scheduler import _SchedulerDbMixin


class UploadBandwidthArgTests(unittest.TestCase):
    def test_formats_kibps_and_treats_empty_or_zero_as_unlimited(self) -> None:
        self.assertEqual(upload_bwlimit_arg(256), "256k")
        self.assertIsNone(upload_bwlimit_arg(None))
        self.assertIsNone(upload_bwlimit_arg(0))


class UploadBandwidthSchedulingTests(_SchedulerDbMixin, unittest.TestCase):
    def _scheduled_bwlimits(self, limit_kibps: int | None) -> dict[str, str | None]:
        upload_id = self._insert_upload_job(vault_id=10, path="a.txt")
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO jobs(vault_id, vault_file_id, path, action, status, "
                "requested_by, requested_at, updated_at) "
                "SELECT 20, id, 'a.txt', 'free-space', 'queued', 1, "
                "'2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00' "
                "FROM vault_files WHERE vault_id=20 ORDER BY id LIMIT 1"
            )
        seen: dict[str, str | None] = {}

        def fake_process(job: dict) -> bool:
            key = "upload" if int(job["id"]) == upload_id else str(job["action"])
            seen[key] = job.get("bwlimit")
            return True

        with (
            patch("app.storage.settings") as mock_settings,
            patch("app.storage.db") as mock_db,
            patch("app.storage.process_job", side_effect=fake_process),
        ):
            mock_settings.operation_concurrency = 4
            mock_settings.bandwidth_limit_kibps = limit_kibps
            mock_db.side_effect = lambda: SQLiteConnection(str(self.path))
            process_jobs_once()
        return seen

    def test_global_limit_throttles_uploads_only(self) -> None:
        seen = self._scheduled_bwlimits(512)
        self.assertEqual(seen, {"upload": "512k", "free-space": None})

    def test_no_limit_leaves_uploads_unthrottled(self) -> None:
        seen = self._scheduled_bwlimits(None)
        self.assertEqual(seen, {"upload": None, "free-space": None})


class RcloneBandwidthTests(unittest.TestCase):
    def test_run_rclone_inserts_bwlimit_before_command_args(self) -> None:
        with patch("app.storage.subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stderr = ""
            run_mock.return_value.stdout = ""
            run_rclone(
                "copyto",
                "remote:a",
                "/tmp/a",
                bwlimit="512k",
                config_path="/tmp/rclone.conf",
            )
        command = run_mock.call_args.args[0]
        self.assertEqual(command[0], "rclone")
        self.assertIn("--bwlimit", command)
        self.assertEqual(command[command.index("--bwlimit") + 1], "512k")
        self.assertIn("copyto", command)


if __name__ == "__main__":
    unittest.main()
