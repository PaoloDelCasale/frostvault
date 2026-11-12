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
from app.services import notifications, source_layout, vault_relocation, watch_plan
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
            connection.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')")
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (9, 'files', 'Files', %s, 'bucket', 'files')",
                (str(self.root),),
            )
        self.vault = {"id": 9, "source_root": str(self.root)}
        patcher = patch("app.database.settings", replace(settings, database_dsn=database_dsn(str(self.path))))
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.runtime_status.pop(9, None)
        watch_plan.forget(9)
        self.addCleanup(watch_plan.forget, 9)
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
        # Not an unreadable path: no permission advice.
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

    # --- review of PR #504 ------------------------------------------------

    def copies(self):
        with PostgresConnection(str(self.path)) as connection:
            return {
                row["path"]: row["presence"]
                for row in connection.execute(
                    """SELECT fp.path, lc.presence FROM local_copies lc
                       JOIN file_paths fp ON fp.vault_file_id=lc.vault_file_id AND fp.valid_to IS NULL"""
                ).fetchall()
            }

    def test_a_folder_renamed_to_an_unsupported_name_never_makes_its_files_missing(self):
        from app.services.component_health import load_vault_health
        from app.services.protection_states import observation_is_unconfirmed

        raw = os.fsencode(self.root)
        (self.root / "Foto").mkdir()
        (self.root / "Foto" / "a.jpg").write_text("photo")
        (self.root / "other.txt").write_text("other")
        storage.scan_tree(self.vault, "first")
        self.assertEqual(self.copies(), {"Foto/a.jpg": "present", "other.txt": "present"})

        # Another client (or a remount with another charset) renames the folder.
        os.rename(raw + b"/Foto", raw + b"/Fot\xf2")
        journal = {}
        storage.scan_tree(self.vault, "second", _scan_journal=journal)

        # The old rows are not proven gone: they would otherwise look
        # cloud-only and become deletable from the cloud.
        self.assertEqual(self.copies(), {"Foto/a.jpg": "present", "other.txt": "present"})
        self.assertTrue(journal["incomplete"])

        # The Vault is not confirmed while that holds, so cloud deletion and
        # local release stay closed (CloudDeletion refuses unconfirmed Vaults).
        # scan_vault reloads the row: enrol the root as a real Vault has it.
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE vaults SET root_identity=%s, root_identity_version=%s WHERE id=9",
                (vault_relocation.root_identity(str(self.root)), vault_relocation.ROOT_IDENTITY_VERSION),
            )
        with (
            patch("app.storage._validate_enrolled_scan_root"),
            patch("app.storage.scan_cloud", return_value=0),
        ):
            result = storage.scan_vault(self.vault)
        self.assertEqual(result.get("local_incomplete"), 1)
        with PostgresConnection(str(self.path)) as connection:
            health = load_vault_health(connection, 9)
        self.assertTrue(health["source"]["last_error"])
        self.assertTrue(observation_is_unconfirmed(health))

        # Once the name is repaired the next complete scan decides again.
        os.rename(raw + b"/Fot\xf2", raw + b"/Foto")
        storage.scan_tree(self.vault, "third")
        self.assertEqual(self.copies()["Foto/a.jpg"], "present")
        os.unlink(self.root / "Foto" / "a.jpg")
        storage.scan_tree(self.vault, "fourth")
        self.assertEqual(self.copies()["Foto/a.jpg"], "missing")

    def test_complete_scan_waits_for_repaired_subtree_registration_before_confirming_source(self):
        """A manual/periodic scan must obey the same gate as a paired scan."""
        from app.services.component_health import load_vault_health, record_component_health

        repaired = self.root / "repaired"
        repaired.mkdir()
        (repaired / "already-here.txt").write_text("catch-up")
        gaps = watch_plan.UnobservedPaths(
            unreadable=frozenset({"repaired"}), directories=frozenset({"repaired"}),
        )
        watch_plan.record_full_scan(9, gaps)
        watch_plan.record_registration(9, gaps)
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE vaults SET root_identity=%s, root_identity_version=%s WHERE id=9",
                (vault_relocation.root_identity(str(self.root)), vault_relocation.ROOT_IDENTITY_VERSION),
            )
            record_component_health(connection, vault_id=9, component="source", ok=False,
                                    error="Source scan incomplete")

        with (
            patch("app.storage._validate_enrolled_scan_root"),
            patch("app.storage.scan_cloud", return_value=0),
        ):
            result = storage.scan_vault(self.vault)
            self.assertNotIn("local_incomplete", result, "the walk itself is complete")
            self.assertEqual(self.copies(), {"repaired/already-here.txt": "present"})
            with PostgresConnection(str(self.path)) as connection:
                health = load_vault_health(connection, 9)
            self.assertEqual(health["source"]["last_error"], "Source watcher registration pending")

            # The successor's actual barrier, followed by its paired scan.
            (repaired / "before-barrier.txt").write_text("also catch up")
            watch_plan.record_registration(9, watch_plan.UnobservedPaths())
            storage.scan_vault(self.vault)
        with PostgresConnection(str(self.path)) as connection:
            health = load_vault_health(connection, 9)
        self.assertIsNone(health["source"]["last_error"])
        self.assertEqual(self.copies(), {
            "repaired/already-here.txt": "present", "repaired/before-barrier.txt": "present",
        })

    def test_each_unreadable_path_closes_by_itself(self):
        from app.services.problems import report_problem, resolve_stale_path_problems

        with PostgresConnection(str(self.path)) as connection:
            for path in ("kept/x", "gone/y", "below/hidden/z", "below"):
                report_problem(connection, event="file_unreadable", vault_id=9, path=path)
        with PostgresConnection(str(self.path)) as connection:
            # "kept/x" is still unreadable and "below" could not be entered:
            # what lies under it is unknown, not repaired.
            resolve_stale_path_problems(
                connection, event="file_unreadable", vault_id=9,
                keep_paths={"kept/x", "below"}, keep_prefixes={"below"},
            )
        statuses = {json.loads(p["message_params"])["path"]: p["problem_status"]
                    for p in self.problems("file_unreadable")}
        self.assertEqual(statuses, {
            "kept/x": "open", "gone/y": "resolved", "below/hidden/z": "open", "below": "open",
        })

    def test_a_corrected_s3_setup_scans_the_vaults_whose_cloud_check_failed(self):
        from app.services.component_health import record_component_health

        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (10, 'other', 'Other', %s, 'bucket', 'other')",
                (str(self.root),),
            )
            record_component_health(connection, vault_id=9, component="cloud", ok=False, error="Cloud scan: denied")
            record_component_health(connection, vault_id=10, component="cloud", ok=True)
            connection.execute(
                "INSERT INTO cloud_preflight_state(id, status, remediation, failure_count, next_probe_at) "
                "VALUES (1, 'blocked', 'S3 access was refused', 3, '2000-01-01T00:00:00+00:00')"
            )
        with (
            patch("app.storage.validate_cloud_vault") as validate,
            patch("app.storage._submit_maintenance", side_effect=lambda name, func, *args: func(*args)),
            patch("app.storage.scan_vault", return_value={}) as scan,
        ):
            storage.reprobe_cloud_readiness_once()
        validate.assert_called_once()
        self.assertEqual([call.args[0]["id"] for call in scan.call_args_list], [9])

    def test_a_gate_that_was_never_blocked_scans_nothing(self):
        from app.services.component_health import record_component_health

        with PostgresConnection(str(self.path)) as connection:
            record_component_health(connection, vault_id=9, component="cloud", ok=False, error="Cloud scan: denied")
        with (
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage._submit_maintenance", side_effect=lambda name, func, *args: func(*args)),
            patch("app.storage.scan_vault", return_value={}) as scan,
        ):
            storage.reprobe_cloud_readiness_once()
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
