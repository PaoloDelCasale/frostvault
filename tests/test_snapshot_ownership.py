"""#679: Recovery gives the file back its original owner and mode."""
import json
import os
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import storage
from app.runtime_privileges import root_runtime_problems
from app.services.upload_snapshot import (
    FileOwnership,
    UploadSnapshot,
    prepare_upload_snapshot,
    recovery_ownership,
)

UNCHANGED_UID = os.getuid()
UNCHANGED_GID = os.getgid()


def _snapshot(root: str, mode: int = 0o640) -> UploadSnapshot:
    source = Path(root) / "archived"
    source.write_bytes(b"photo")
    os.chmod(source, mode)
    snapshot, _ = prepare_upload_snapshot(source, lambda: None)
    return snapshot


class SnapshotOwnershipTests(unittest.TestCase):
    def test_hashing_records_owner_and_permission_bits(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = _snapshot(root, 0o640)
            self.assertEqual(snapshot.ownership, FileOwnership(UNCHANGED_UID, UNCHANGED_GID, 0o640))
            data = json.loads(snapshot.as_json())
            self.assertEqual((data["uid"], data["gid"], data["mode"]), (UNCHANGED_UID, UNCHANGED_GID, 0o640))
            self.assertEqual(UploadSnapshot.from_json(snapshot.as_json()), snapshot)
            self.assertEqual(snapshot.file_metadata()["frostvault-mode"], "0640")
            self.assertEqual(snapshot.file_metadata()["frostvault-uid"], str(UNCHANGED_UID))
            record = snapshot.record()
            self.assertEqual((record["owner_uid"], record["owner_gid"], record["file_mode"]),
                             (UNCHANGED_UID, UNCHANGED_GID, 0o640))
            rebuilt = UploadSnapshot.from_record(
                size=record["size"], plaintext_sha256=record["plaintext_sha256"],
                part_size=record["part_size"], part_sha256=record["part_sha256"],
                mtime_ns=record["mtime_ns"], owner_uid=record["owner_uid"],
                owner_gid=record["owner_gid"], file_mode=record["file_mode"])
            self.assertEqual(rebuilt, snapshot)

    def test_snapshot_before_679_has_no_invented_ownership(self):
        with tempfile.TemporaryDirectory() as root:
            data = json.loads(_snapshot(root).as_json())
            for key in ("uid", "gid", "mode"):
                data.pop(key)
            legacy = json.dumps(data, separators=(",", ":"))
            restored = UploadSnapshot.from_json(legacy)
            self.assertIsNone(restored.ownership)
            self.assertEqual(restored.as_json(), legacy)
            self.assertNotIn("frostvault-uid", restored.file_metadata())
            self.assertIsNone(restored.record()["owner_uid"])
            self.assertIsNone(recovery_ownership(legacy, {}))
            self.assertIsNone(recovery_ownership(None, {}))

    def test_partial_or_invalid_ownership_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            data = json.loads(_snapshot(root).as_json())
            for invalid in ({"uid": None}, {"gid": -1}, {"mode": 0o10000}, {"uid": True},
                            {"uid": 2**32 - 1}, {"mode": "644"}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    UploadSnapshot.from_json(json.dumps({**data, **invalid}))
            partial = dict(data)
            partial.pop("gid")
            with self.assertRaises(ValueError):
                UploadSnapshot.from_json(json.dumps(partial))
        valid = {"frostvault-uid": "1002", "frostvault-gid": "100", "frostvault-mode": "0766"}
        self.assertEqual(FileOwnership.from_metadata(valid), FileOwnership(1002, 100, 0o766))
        for key, value in (("frostvault-uid", "+1"), ("frostvault-uid", "01"), ("frostvault-mode", "644"),
                           ("frostvault-mode", "0o644"), ("frostvault-mode", "10000"),
                           ("frostvault-gid", " 100"), ("frostvault-uid", "4294967295"),
                           ("frostvault-gid", None)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                FileOwnership.from_metadata({**valid, key: value})

    def test_catalog_evidence_wins_over_provider_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = _snapshot(root, 0o600)
            other = {"frostvault-uid": "7", "frostvault-gid": "8", "frostvault-mode": "0777"}
            self.assertEqual(recovery_ownership(snapshot.as_json(), other), snapshot.ownership)
            self.assertEqual(recovery_ownership(None, other), FileOwnership(7, 8, 0o777))


class RecoveryOwnershipTests(unittest.TestCase):
    def recover(self, *, snapshot_json=None, metadata=None, fchown=None):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            evidence = _snapshot(root)
            catalog = MagicMock()
            catalog.get_job_target.return_value = {
                "integrity": "verified", "availability": "available", "provider_version_id": "exact",
                "object_key": "vault/photo", "version_sha256": evidence.plaintext_sha256,
                "archive_version_id": "av", "current_version_id": "av", "file_id": "f",
                "local_generation": 4, "local_presence": "missing", "cloud_size": 5,
                "upload_snapshot_json": snapshot_json,
            }
            client = MagicMock()
            client.head_object.return_value = {"VersionId": "exact", "Metadata": metadata or {}}

            def download(**kwargs):
                Path(kwargs["Filename"]).write_bytes(b"photo")
                kwargs["Callback"](5)
            client.download_file.side_effect = download
            stack.enter_context(patch.object(storage, "ArchiveCatalog", return_value=catalog))
            for name in ("db", "ensure_job_active", "validate_cloud_vault",
                         "ensure_job_claim_owned_in_transaction", "set_job_progress",
                         "_ThrottledByteProgress"):
                stack.enter_context(patch.object(storage, name))
            set_job = stack.enter_context(patch.object(storage, "set_job"))
            stack.enter_context(patch.object(storage, "s3_client", return_value=client))
            remember = stack.enter_context(patch.object(storage.local_observation, "remember"))
            if fchown is not None:
                stack.enter_context(patch.object(storage.os, "fchown", side_effect=fchown))
            storage.process_recover({"id": 1, "vault_id": 2, "source_root": root,
                "path": "recovered", "s3_bucket": "bucket", "total_bytes": 5, "status": "queued"})
            destination = Path(root) / "recovered"
            info = destination.stat()
            published_stat = remember.call_args.args[2]
            self.assertEqual((published_stat.st_ino, published_stat.st_mode, published_stat.st_ctime_ns),
                             (info.st_ino, info.st_mode, info.st_ctime_ns))
            return info, set_job.call_args_list[-1]

    def test_original_mode_and_owner_are_applied_to_the_published_file(self):
        calls = []
        ownership = {"frostvault-uid": "1002", "frostvault-gid": "100", "frostvault-mode": "0766"}
        info, completed = self.recover(metadata=ownership, fchown=lambda fd, uid, gid: calls.append((uid, gid)))
        self.assertEqual(calls, [(1002, 100)])
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o766)
        self.assertEqual(completed.kwargs["message_key"], "job.recovered_to")

    def test_refused_chown_keeps_todays_behavior_and_says_so(self):
        def refuse(fd, uid, gid):
            raise PermissionError(1, "Operation not permitted")

        ownership = {"frostvault-uid": "0", "frostvault-gid": "0", "frostvault-mode": "0644"}
        before_mode = 0o666 & ~self._umask()
        info, completed = self.recover(metadata=ownership, fchown=refuse)
        self.assertEqual(stat.S_IMODE(info.st_mode), before_mode)
        self.assertEqual((info.st_uid, info.st_gid), (UNCHANGED_UID, UNCHANGED_GID))
        self.assertEqual(completed.kwargs["message_key"], "job.recovered_to_without_ownership")
        self.assertEqual(completed.kwargs["message_params"]["mode"], "0644")
        self.assertEqual(completed.kwargs["message_params"]["uid"], 0)

    def test_own_identity_needs_no_capability(self):
        with tempfile.TemporaryDirectory() as root:
            evidence = _snapshot(root, 0o600).as_json()
        info, completed = self.recover(snapshot_json=evidence)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual((info.st_uid, info.st_gid), (UNCHANGED_UID, UNCHANGED_GID))
        self.assertEqual(completed.kwargs["message_key"], "job.recovered_to")

    def test_version_without_ownership_is_recovered_as_before(self):
        def unexpected(fd, uid, gid):
            raise AssertionError("no ownership evidence, no chown")

        info, completed = self.recover(fchown=unexpected)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o666 & ~self._umask())
        self.assertEqual(completed.kwargs["message_key"], "job.recovered_to")

    @unittest.skipUnless(os.geteuid() == 0, "a real chown to another owner needs CAP_CHOWN")
    def test_real_chown_restores_another_owner(self):
        ownership = {"frostvault-uid": "1002", "frostvault-gid": "100", "frostvault-mode": "0766"}
        info, completed = self.recover(metadata=ownership)
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (1002, 100, 0o766))
        self.assertEqual(completed.kwargs["message_key"], "job.recovered_to")

    @staticmethod
    def _umask() -> int:
        current = os.umask(0)
        os.umask(current)
        return current


class RootRuntimeTests(unittest.TestCase):
    STATUS = "Name:\tpython\nCapEff:\t{eff}\nCapBnd:\t{bnd}\nNoNewPrivs:\t{nnp}\n"
    CHOWN_KILL = f"{(1 << 0) | (1 << 5):016x}"

    def problems(self, eff=None, bnd=None, nnp="1"):
        eff = self.CHOWN_KILL if eff is None else eff
        bnd = eff if bnd is None else bnd
        return root_runtime_problems(self.STATUS.format(eff=eff, bnd=bnd, nnp=nnp))

    def test_root_with_chown_and_kill_only_may_start(self):
        self.assertEqual(self.problems(), [])

    def test_each_permission_bypass_is_refused_in_effective_or_bounding_set(self):
        for bit, name in ((1, "CAP_DAC_OVERRIDE"), (2, "CAP_DAC_READ_SEARCH"),
                          (3, "CAP_FOWNER"), (21, "CAP_SYS_ADMIN")):
            value = f"{int(self.CHOWN_KILL, 16) | 1 << bit:016x}"
            with self.subTest(name=name):
                self.assertIn(name, " ".join(self.problems(eff=value)))
                self.assertIn(name, " ".join(self.problems(bnd=value)))

    def test_default_docker_capabilities_are_refused(self):
        # Docker's default set includes CAP_DAC_OVERRIDE and CAP_FOWNER.
        problems = " ".join(self.problems(eff="00000000a80425fb"))
        self.assertIn("CAP_DAC_OVERRIDE", problems)
        self.assertIn("CAP_FOWNER", problems)

    def test_missing_no_new_privileges_or_status_is_refused(self):
        self.assertIn("no-new-privileges", " ".join(self.problems(nnp="0")))
        self.assertTrue(root_runtime_problems(""))


if __name__ == "__main__":
    unittest.main()
