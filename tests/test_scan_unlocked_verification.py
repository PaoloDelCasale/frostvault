"""A full scan verifies fingerprints without the Vault scan lock (#689).

Seams under test:
1. While the scan hashes, watcher batches and automatic admission of the
   same Vault run; a second full scan is still refused.
2. During the scan only Local Copies whose fingerprint was committed are
   admitted, and each Job records the generation it was admitted on.
3. A scan that loses its proof after admission compensates its observations:
   the generation changes and the Job is cancelled at its claim, unread.
4. A replacement by the watcher during the hashing wins the compare-and-swap.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from watchfiles import Change

from app.config import Settings
from app.services.vaults import VAULT_ROW_SQL
from tests.postgres import PostgresConnection
from tests.test_vault_adoption import _AdoptionFixture


class UnlockedVerificationTests(_AdoptionFixture):
    def setUp(self) -> None:
        super().setUp()
        for index in range(12):
            (self.albums / f"img{index:02d}.heic").write_bytes(b"x" * (100 + index))
        import app.storage as storage
        self.storage = storage
        created = self._adopt()
        self.addCleanup(storage.runtime_status.pop, int(created["id"]), None)
        with PostgresConnection(str(self.database_path)) as connection:
            self.vault = dict(connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s", (created["id"],),
            ).fetchone())
        self.vault_id = int(self.vault["id"])
        self.later = datetime.now(timezone.utc) + timedelta(days=1)

    def _rows(self) -> dict[str, dict]:
        with PostgresConnection(str(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT id, path, generation, local_sha256 FROM files WHERE vault_id=%s",
                (self.vault_id,),
            ).fetchall()
        return {row["path"]: dict(row) for row in rows}

    def _jobs(self) -> list[dict]:
        with PostgresConnection(str(self.database_path)) as connection:
            return [dict(row) for row in connection.execute(
                """
                SELECT j.*, f.path, f.generation, v.source_root
                FROM jobs j JOIN files f ON f.id=j.file_id JOIN vaults v ON v.id=j.vault_id
                WHERE j.vault_id=%s ORDER BY f.path
                """,
                (self.vault_id,),
            ).fetchall()]

    def _in_thread(self, function, *args, **kwargs):
        """Run beside the scan; it must finish while the scan still hashes."""
        result: dict = {}

        def target():
            try:
                result["value"] = function(*args, **kwargs)
            except BaseException as exc:  # pragma: no cover - reported below
                result["error"] = exc

        thread = threading.Thread(target=target)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "blocked behind the scan lock")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _scan(self, during_hash, *extra):
        real_hash = self.storage.hash_stable_regular_file
        fired = []

        def hook(path, **kwargs):
            if path.name == "img08.heic" and not fired:
                fired.append(True)
                during_hash()
            return real_hash(path, **kwargs)

        with ExitStack() as stack:
            for item in (
                patch("app.storage._runtime_settings",
                      return_value=replace(Settings(), scan_hash_concurrency=1)),
                patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 4),
                patch("app.storage.hash_stable_regular_file", side_effect=hook),
                patch("app.storage.scan_cloud", return_value=0),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.sync_lifecycle_rules_for_bucket", return_value=0),
                patch("app.storage.queue_auto_local_cleanups", return_value=0),
                *extra,
            ):
                stack.enter_context(item)
            result = self.storage.scan_vault(dict(self.vault))
        self.assertTrue(fired)
        return result

    def test_admission_and_watcher_run_while_the_scan_verifies(self) -> None:
        storage = self.storage
        seen: dict = {}
        replaced = self.albums / "img10.heic"

        def during_hash():
            self.assertFalse(storage.scan_lock_for_vault(self.vault_id).locked())
            seen["second_scan"] = self._in_thread(storage.scan_vault, dict(self.vault))
            with patch("app.storage._identify_new_local_copies") as identify:
                seen["queued"] = self._in_thread(
                    storage.queue_vault_auto_uploads, dict(self.vault), now=self.later,
                )
            seen["identify_calls"] = identify.call_count
            seen["jobs"] = self._jobs()
            replaced.write_bytes(b"replaced while the scan verified")
            self._in_thread(
                storage.apply_filesystem_changes, dict(self.vault),
                {(Change.modified, str(replaced))},
            )

        result = self._scan(during_hash)
        self.assertEqual(result["local"], 13)
        self.assertEqual(seen["second_scan"], {"already_running": 1})
        self.assertEqual(seen["identify_calls"], 0)
        # Two committed batches of four: only those are eligible yet.
        self.assertEqual(seen["queued"], 8)
        self.assertEqual(
            [job["path"] for job in seen["jobs"]], [f"img{i:02d}.heic" for i in range(8)],
        )
        for job in seen["jobs"]:
            self.assertEqual(job["origin"], "automatic")
            self.assertEqual(job["admitted_generation"], job["generation"])
        rows = self._rows()
        old = hashlib.sha256(b"x" * 110).digest()
        self.assertNotEqual(bytes(rows["img10.heic"]["local_sha256"] or b""), old)
        self.assertTrue(rows["img11.heic"]["local_sha256"])
        # Jobs admitted on unchanged observations stay runnable.
        state = storage._automatic_upload_state(seen["jobs"][0])
        self.assertNotEqual(state, storage.UPLOAD_OBSERVATION_WITHDRAWN)

    def test_compensated_observation_cancels_the_job_admitted_on_it(self) -> None:
        storage = self.storage
        admitted: list[dict] = []

        def during_hash():
            self._in_thread(storage.queue_vault_auto_uploads, dict(self.vault), now=self.later)
            admitted.extend(self._jobs())

        # The scan loses its proof at finalization (e.g. the root was
        # replaced while it hashed): its journal is rolled back.
        result = self._scan(
            during_hash,
            patch("app.storage._apply_auto_renames",
                  side_effect=storage._ScanRootMismatch("root replaced")),
        )
        self.assertEqual(result.get("root_identity_mismatch"), 1)
        self.assertEqual(len(admitted), 8)
        jobs = {job["id"]: job for job in self._jobs()}
        for job in admitted:
            current = jobs[job["id"]]
            self.assertNotEqual(current["generation"], current["admitted_generation"])
            self.assertEqual(
                storage._automatic_upload_state(current), storage.UPLOAD_OBSERVATION_WITHDRAWN,
            )
        first = jobs[admitted[0]["id"]]
        self.assertTrue(storage._close_withdrawn_upload(first))
        with PostgresConnection(str(self.database_path)) as connection:
            closed = connection.execute(
                "SELECT status, message_key FROM jobs WHERE id=%s", (first["id"],),
            ).fetchone()
        self.assertEqual(
            (closed["status"], closed["message_key"]),
            ("cancelled", "job.upload_observation_withdrawn"),
        )

    def test_upload_gate_closes_a_withdrawn_job_before_reading(self) -> None:
        from app.services.upload_execution import UploadExecution
        storage = self.storage
        self._scan(lambda: None)
        storage.queue_vault_auto_uploads(dict(self.vault), now=self.later)
        job = self._jobs()[0]
        self.assertIsNone(job["admitted_generation"])  # admitted outside a scan
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE jobs SET admitted_generation=%s WHERE id=%s",
                (job["generation"], job["id"]),
            )
            connection.execute(
                "UPDATE files SET generation=generation + 1 WHERE id=%s", (job["file_id"],),
            )
        job = self._jobs()[0]
        with patch("app.storage.hash_stable_regular_file") as hashing:
            outcome = UploadExecution(storage)._automatic_gate(job)
        hashing.assert_not_called()
        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(self._jobs()[0]["status"], "cancelled")

    def test_restoring_a_created_row_with_a_job_withdraws_its_observation(self) -> None:
        storage = self.storage
        self._scan(lambda: None)
        scan_id = "2026-11-15T10:00:00+00:00"
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE files SET local_seen_at=%s WHERE vault_id=%s AND path='photo.jpg'",
                (storage._scan_timestamp(scan_id), self.vault_id),
            )
        storage.queue_vault_auto_uploads(dict(self.vault), now=self.later)
        before = self._rows()["photo.jpg"]["generation"]
        with PostgresConnection(str(self.database_path)) as connection:
            storage._restore_journal_entries(
                connection, self.vault_id, scan_id, {"photo.jpg": None},
            )
        row = self._rows()["photo.jpg"]
        self.assertEqual(row["generation"], before + 1)
