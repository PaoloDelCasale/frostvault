"""#494-#496: unsupported names, and problems that close once their cause is gone.

Real PostgreSQL (TEST_POSTGRES_URL, never skipped), real files in a temp dir.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.config import settings
from app.services import notifications, source_layout
from tests.postgres import PostgresConnection, database_dsn, run_alembic


class RecoveryProblemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "sources" / "managed" / "vault"
        self.root.mkdir(parents=True)
        source_layout.override_sources_root(base / "sources")
        self.addCleanup(source_layout.reset_sources_root_override)
        self.path = base / "catalog"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.path)) as connection:
            connection.execute("INSERT INTO users(id, username, display_name, password_hash) VALUES (1, 'owner', 'Owner', 'hash')")
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (9, 'files', 'Files', %s, 'bucket', 'files')",
                (str(self.root),),
            )
        self.vault = {"id": 9, "source_root": str(self.root)}
        patcher = patch("app.database.settings", replace(settings, database_dsn=database_dsn(str(self.path))))
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.runtime_status.pop(9, None)
        storage._workers_known_ok.clear()
        self.addCleanup(storage._workers_known_ok.clear)

    def problems(self, event=None):
        with PostgresConnection(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT event, problem_status, message_params FROM notifications ORDER BY id"
            ).fetchall()
        return [row for row in rows if event is None or row["event"] == event]

    def open_problem_count(self):
        with PostgresConnection(str(self.path)) as connection:
            return notifications.count_open_problems(connection, user_id=1)

    def test_non_utf8_name_is_reported_safely_and_everything_else_is_catalogued(self):
        (self.root / "ok.txt").write_text("fine")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "nested.txt").write_text("nested")
        raw = os.fsencode(self.root)
        with open(raw + b"/non-utf8-\xe9.txt", "wb") as handle:
            handle.write(b"bad")
        os.mkdir(raw + b"/dir-\xff")
        with open(raw + b"/dir-\xff/inside.txt", "wb") as handle:
            handle.write(b"inside")

        self.assertEqual(storage.scan_tree(self.vault, "first"), 2)

        with PostgresConnection(str(self.path)) as connection:
            paths = {row["path"] for row in ArchiveCatalog(connection).list_file_rows(9)}
        self.assertEqual(paths, {"ok.txt", "sub/nested.txt"})
        problems = self.problems("file_name_unsupported")
        self.assertEqual(
            sorted(json.loads(p["message_params"])["path"] for p in problems),
            ["dir-\\udcff", "non-utf8-\\udce9.txt"],
        )
        self.assertEqual({p["problem_status"] for p in problems}, {"open"})
        # Not an unreadable path: no permission advice, and no incomplete scan.
        self.assertEqual(self.problems("file_unreadable"), [])

        # Rescanning does not duplicate the problems.
        storage.scan_tree(self.vault, "second")
        self.assertEqual(len(self.problems("file_name_unsupported")), 2)

    def test_the_problem_closes_when_a_complete_scan_no_longer_sees_the_name(self):
        raw = os.fsencode(self.root)
        (self.root / "ok.txt").write_text("fine")
        with open(raw + b"/non-utf8-\xe9.txt", "wb") as handle:
            handle.write(b"bad")
        with open(raw + b"/also-\xe9.txt", "wb") as handle:
            handle.write(b"bad")
        storage.scan_tree(self.vault, "first")
        self.assertEqual(self.open_problem_count(), 2)

        os.unlink(raw + b"/non-utf8-\xe9.txt")
        storage.scan_tree(self.vault, "second")
        statuses = {json.loads(p["message_params"])["path"]: p["problem_status"]
                    for p in self.problems("file_name_unsupported")}
        self.assertEqual(statuses, {"non-utf8-\\udce9.txt": "resolved", "also-\\udce9.txt": "open"})

        os.unlink(raw + b"/also-\xe9.txt")
        storage.scan_tree(self.vault, "third")
        self.assertEqual(self.open_problem_count(), 0)

    def test_an_incomplete_scan_does_not_close_the_problem(self):
        raw = os.fsencode(self.root)
        with open(raw + b"/non-utf8-\xe9.txt", "wb") as handle:
            handle.write(b"bad")
        storage.scan_tree(self.vault, "first")
        os.unlink(raw + b"/non-utf8-\xe9.txt")
        private = self.root / "private"
        private.mkdir()
        real_scandir = os.scandir

        def inaccessible(fd):
            if Path(os.readlink(f"/proc/self/fd/{fd}")) == private:
                raise PermissionError(13, "denied", str(private))
            return real_scandir(fd)

        with patch("app.services.fs_preflight.os.scandir", side_effect=inaccessible):
            storage.scan_tree(self.vault, "incomplete")
        # The walk could not see inside "private": that proves nothing about the name.
        self.assertEqual(
            [p["problem_status"] for p in self.problems("file_name_unsupported")], ["open"],
        )
        self.assertIn("private", str(self.problems("file_unreadable")))

    def test_a_recovered_worker_closes_its_internal_error_once(self):
        storage._report_worker_error("auto_upload_queue", RuntimeError("root gone"), 9)
        self.assertEqual(self.open_problem_count(), 1)

        storage._note_worker_ok("auto_upload_queue", 9)
        self.assertEqual(self.open_problem_count(), 0)

        # A healthy worker does not write to the inbox on every pass...
        with patch("app.storage._resolve_worker_error") as resolve:
            storage._note_worker_ok("auto_upload_queue", 9)
            resolve.assert_not_called()
        # ...but a new failure re-arms the next resolution.
        storage._report_worker_error("auto_upload_queue", RuntimeError("again"), 9)
        self.assertEqual(self.open_problem_count(), 1)
        storage._note_worker_ok("auto_upload_queue", 9)
        self.assertEqual(self.open_problem_count(), 0)

    def test_a_worker_ok_before_any_failure_closes_a_problem_left_by_a_previous_run(self):
        from app.services import worker_errors
        with PostgresConnection(str(self.path)) as connection:
            worker_errors.record_worker_error(
                connection, component="auto_upload_queue", exc=RuntimeError("x"), vault_id=9,
            )
        self.assertEqual(self.open_problem_count(), 1)
        storage._note_worker_ok("auto_upload_queue", 9)
        self.assertEqual(self.open_problem_count(), 0)


if __name__ == "__main__":
    unittest.main()
