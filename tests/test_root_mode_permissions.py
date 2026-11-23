"""#681: root mode on shares of other owners — writability, messages, privileges.

Unit tests only: no database, no network.
"""
from __future__ import annotations

import errno
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import storage
from app.i18n import translate
from app.runtime_privileges import (
    DAC_WARNING,
    NO_GROUPS_WARNING,
    ROOT_WARNING,
    dac_override_allowed,
    holds_dac_override,
    root_runtime_problems,
    startup_warnings,
)
from app.services import write_access
from app.services.notifications import _localized_params
from app.services.problems import report_problem
from tests.fixtures import override_settings
from app.services import job_queue

STATUS = "Name:\tpython\nCapEff:\t{eff}\nCapBnd:\t{eff}\nNoNewPrivs:\t1\n"
CHOWN_KILL = (1 << 0) | (1 << 5)
DAC = (1 << 1) | (1 << 2)


def status(bits: int) -> str:
    return STATUS.format(eff=f"{bits:016x}")


class WriteAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_unwritable_folder_names_owner_mode_and_identity(self) -> None:
        folder = self.root / "cold"
        folder.mkdir(mode=0o755)
        os.chmod(folder, 0o755)
        info = folder.stat()
        with patch.object(write_access, "_writable", return_value=False):
            with self.assertRaises(write_access.DestinationNotWritable) as raised:
                write_access.require_writable_parent(self.root, folder / "c2.bin")
        params = raised.exception.message_params
        self.assertEqual(params["path"], str(folder))
        self.assertEqual(params["owner"], f"{info.st_uid}:{info.st_gid}")
        self.assertEqual(params["mode"], "0755")
        self.assertEqual(params["identity"], f"{os.geteuid()}:{os.getegid()}")
        self.assertEqual(raised.exception.message_key, "job.destination_not_writable")
        for locale in ("en", "it"):
            text = translate("job.destination_not_writable", locale=locale, **params)
            self.assertIn(str(folder), text)
            self.assertIn("0755", text)
            self.assertNotIn("{", text)

    def test_writable_folder_passes(self) -> None:
        write_access.require_writable_parent(self.root, self.root / "file.bin")

    def test_missing_folders_are_checked_at_the_closest_existing_ancestor(self) -> None:
        (self.root / "a").mkdir()
        entry = self.root / "a" / "b" / "c" / "file.bin"
        self.assertEqual(write_access.nearest_existing_directory(entry, self.root), self.root / "a")
        self.assertEqual(write_access.nearest_existing_directory(self.root / "x" / "f", self.root), self.root)

    def test_effective_ids_are_used_when_they_differ_from_the_real_ones(self) -> None:
        with (patch.object(write_access.os, "getuid", return_value=1), patch.object(write_access.os, "geteuid", return_value=0),
              patch.object(write_access.os, "access", return_value=True) as access):
            self.assertTrue(write_access._writable(self.root))
        self.assertTrue(access.call_args.kwargs.get("effective_ids"))

    @unittest.skipIf(os.geteuid() == 0, "root of this host bypasses the mode")
    def test_real_read_only_folder_is_refused(self) -> None:
        folder = self.root / "ro"
        folder.mkdir()
        os.chmod(folder, 0o555)
        self.addCleanup(os.chmod, folder, 0o755)
        with self.assertRaises(write_access.DestinationNotWritable):
            write_access.require_writable_directory(folder)

    def test_eacces_maps_to_the_folder_of_the_destination(self) -> None:
        error = PermissionError(errno.EACCES, "Permission denied", "/v/c080b/multipart.bin",
                                None, "/v/c080b/.multipart.bin.cleanup-x.tmp")
        self.assertIsNone(write_access.from_permission_error(RuntimeError("unrelated")))
        try:
            try:
                raise error
            except PermissionError as inner:
                raise RuntimeError("outer") from inner
        except RuntimeError as outer:
            mapped = write_access.from_permission_error(outer)
        self.assertEqual(mapped.message_params["path"], "/v/c080b")
        self.assertIsNone(write_access.from_permission_error(PermissionError(errno.EPERM, "no", "/v/x")))


class LocalJobFailureMessageTests(unittest.TestCase):
    """The worker's failure branch names the folder instead of a raw Errno 13."""

    def run_failing(self, action: str, failure: BaseException, restore_kept: str | None = None):
        job = {"id": 9, "vault_id": 1, "action": action, "path": "cold/c2.bin", "status": "queued"}

        def fail(running_job):
            if restore_kept:
                running_job["_restore_kept_until"] = restore_kept
            raise failure

        with ExitStack() as stack:
            stack.enter_context(patch.object(storage.shutdown_state, "checkpoint"))
            stack.enter_context(override_settings(restore_poll_interval=0))
            stack.enter_context(patch.object(storage, "_load_claimed_job_status", return_value={"status": "queued"}))
            stack.enter_context(patch.object(job_queue, "claim_is_lost", return_value=False))
            stack.enter_context(patch.object(storage, "record_automatic_cleanup_outcome"))
            stack.enter_context(patch.object(storage, "process_recover", side_effect=fail))
            stack.enter_context(patch.object(storage, "process_free_space", side_effect=fail))
            set_job = stack.enter_context(patch.object(storage, "set_job", return_value=True))
            storage._process_claimed_job(job)
        return set_job.call_args

    def denied(self) -> PermissionError:
        return PermissionError(errno.EACCES, "Permission denied", "/v/cold/c2.bin", None,
                               "/v/cold/.c2.bin.restore-x.tmp")

    def test_free_space_eacces_gets_the_dedicated_message(self) -> None:
        call = self.run_failing("free-space", self.denied())
        self.assertEqual(call.args[1], "failed")
        self.assertEqual(call.kwargs["message_key"], "job.destination_not_writable")
        self.assertEqual(call.kwargs["message_params"]["path"], "/v/cold")

    def test_recovery_after_a_completed_restore_says_until_when_it_is_kept(self) -> None:
        call = self.run_failing("recover", self.denied(), restore_kept="2026-11-18")
        self.assertEqual(call.kwargs["message_key"], "job.destination_not_writable_restore_kept")
        self.assertEqual(call.kwargs["message_params"]["until"], "2026-11-18")
        text = translate(call.kwargs["message_key"], locale="it", **call.kwargs["message_params"])
        self.assertIn("2026-11-18", text)
        self.assertIn("Recupera", text)

    def test_any_late_recovery_failure_keeps_the_restore_hint(self) -> None:
        call = self.run_failing("recover", RuntimeError("Recovered plaintext changed before publication"),
                                restore_kept="2026-11-18")
        self.assertEqual(call.kwargs["message_key"], "job.recovery_failed_restore_kept")
        self.assertIn("changed before publication", call.kwargs["message_params"]["error"])

    def test_restore_expiry_header_becomes_a_date(self) -> None:
        self.assertEqual(storage._restore_expiry_date("Fri, 20 Nov 2026 00:00:00 GMT"), "2026-11-20")
        self.assertIsNone(storage._restore_expiry_date(None))


class RecoveryPreflightTests(unittest.TestCase):
    """A cold Recovery never asks S3 for a restore its final step cannot publish."""

    def test_no_restore_request_into_an_unwritable_folder(self) -> None:
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            catalog = MagicMock()
            catalog.get_job_target.return_value = {
                "integrity": "verified", "availability": "available", "provider_version_id": "exact",
                "object_key": "vault/c2.bin", "version_sha256": "0" * 64, "archive_version_id": "av",
                "current_version_id": "av", "file_id": "f", "local_generation": 1,
                "local_presence": "missing", "cloud_size": 5, "storage_class": "DEEP_ARCHIVE",
                "restore_tier": "Bulk", "restore_days": 3,
            }
            client = MagicMock()
            client.head_object.return_value = {"VersionId": "exact", "StorageClass": "DEEP_ARCHIVE"}
            stack.enter_context(patch.object(storage, "ArchiveCatalog", return_value=catalog))
            for name in ("db", "ensure_job_active", "validate_cloud_vault", "set_job"):
                stack.enter_context(patch.object(storage, name))
            stack.enter_context(patch.object(storage, "s3_client", return_value=client))
            stack.enter_context(patch.object(write_access, "_writable", return_value=False))
            (Path(root) / "cold").mkdir()
            with self.assertRaises(write_access.DestinationNotWritable):
                storage.process_recover({"id": 1, "vault_id": 2, "source_root": root, "path": "cold/c2.bin",
                                         "s3_bucket": "bucket", "total_bytes": 5, "status": "queued"})
            client.restore_object.assert_not_called()


class FolderOwnerFallbackTests(unittest.TestCase):
    """A version archived before v0.8 gets the owner of its destination folder."""

    def test_folder_owner_is_applied_when_it_differs(self) -> None:
        folder = SimpleNamespace(st_uid=99, st_gid=100)
        with (patch.object(storage.os, "stat", return_value=folder),
              patch.object(storage.os, "fstat", return_value=SimpleNamespace(st_uid=0, st_gid=0)),
              patch.object(storage.os, "fchown") as fchown):
            storage._inherit_folder_owner(7, Path("/v/cold"))
        fchown.assert_called_once_with(7, 99, 100)

    def test_same_owner_or_refused_chown_changes_nothing_and_never_fails(self) -> None:
        same = SimpleNamespace(st_uid=0, st_gid=0)
        with (patch.object(storage.os, "stat", return_value=same), patch.object(storage.os, "fstat", return_value=same),
              patch.object(storage.os, "fchown") as fchown):
            storage._inherit_folder_owner(7, Path("/v"))
        fchown.assert_not_called()
        with (patch.object(storage.os, "stat", return_value=SimpleNamespace(st_uid=99, st_gid=100)),
              patch.object(storage.os, "fstat", return_value=same),
              patch.object(storage.os, "fchown", side_effect=PermissionError(errno.EPERM, "no"))):
            storage._inherit_folder_owner(7, Path("/v"))


class DacOverrideOptInTests(unittest.TestCase):
    def test_dac_capabilities_need_the_explicit_opt_in(self) -> None:
        with_dac = status(CHOWN_KILL | DAC)
        self.assertIn("CAP_DAC_OVERRIDE", " ".join(root_runtime_problems(with_dac)))
        self.assertEqual(root_runtime_problems(with_dac, allow_dac_override=True), [])

    def test_opt_in_never_allows_fowner_or_sys_admin(self) -> None:
        for bit, name in ((3, "CAP_FOWNER"), (21, "CAP_SYS_ADMIN")):
            with self.subTest(name=name):
                problems = root_runtime_problems(status(CHOWN_KILL | DAC | 1 << bit), allow_dac_override=True)
                self.assertIn(name, " ".join(problems))
                self.assertNotIn("CAP_DAC_OVERRIDE", " ".join(problems))

    def test_only_the_value_1_enables_it(self) -> None:
        self.assertTrue(dac_override_allowed({"FROSTVAULT_ALLOW_DAC_OVERRIDE": "1"}))
        for value in ("", "0", "true", "yes"):
            self.assertFalse(dac_override_allowed({"FROSTVAULT_ALLOW_DAC_OVERRIDE": value}))
        self.assertFalse(dac_override_allowed({}))

    def test_startup_warnings(self) -> None:
        self.assertEqual(startup_warnings(status(CHOWN_KILL), euid=99, gid=100, groups=[100]), [])
        self.assertEqual(startup_warnings(status(CHOWN_KILL), euid=0, gid=0, groups=[0, 100]), [ROOT_WARNING])
        self.assertEqual(startup_warnings(status(CHOWN_KILL), euid=0, gid=0, groups=[0]),
                         [ROOT_WARNING, NO_GROUPS_WARNING])
        self.assertEqual(startup_warnings(status(CHOWN_KILL | DAC), euid=0, gid=0, groups=[]), [DAC_WARNING])
        self.assertTrue(holds_dac_override(status(1 << 2)))
        self.assertFalse(holds_dac_override(status(CHOWN_KILL)))
        self.assertFalse(holds_dac_override(""))


class UnreadableProblemDetailTests(unittest.TestCase):
    def test_problem_shows_owner_mode_and_frostvault_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            private = Path(root) / "perf2"
            private.mkdir()
            os.chmod(private, 0o700)
            connection = MagicMock()
            connection.execute.return_value.fetchone.return_value = {"source_root": root}
            with patch("app.services.problems.set_problem") as set_problem:
                report_problem(connection, event="file_unreadable", vault_id=1, path="perf2")
            params = set_problem.call_args.kwargs["params"]
            owner_uid = private.stat().st_uid
        self.assertEqual(params["path"], "perf2")
        self.assertEqual(params["owner_uid"], owner_uid)
        self.assertEqual(params["mode"], "0700")
        self.assertEqual(params["identity"], f"{os.geteuid()}:{os.getegid()}")
        for locale, words in (("en", "owned by uid"), ("it", "di proprietà di uid")):
            body = translate("notification.file_unreadable.body", locale=locale, **_localized_params(params, locale))
            self.assertIn(words, body)
            self.assertIn("0700", body)
            self.assertNotIn("{", body)

    def test_without_stat_the_body_is_unchanged(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"source_root": "/nonexistent-681"}
        with patch("app.services.problems.set_problem") as set_problem:
            report_problem(connection, event="file_unreadable", vault_id=1, path="hidden/inner")
        params = set_problem.call_args.kwargs["params"]
        self.assertNotIn("owner_uid", params)
        body = translate("notification.file_unreadable.body", locale="en", **_localized_params(params, "en"))
        self.assertTrue(body.startswith("Cannot read hidden/inner. "))
        # A row stored before #681 renders without a placeholder either.
        legacy = translate("notification.file_unreadable.body", locale="it",
                           **_localized_params({"path": "x", "component": ""}, "it"))
        self.assertNotIn("{access}", legacy)


if __name__ == "__main__":
    unittest.main()
