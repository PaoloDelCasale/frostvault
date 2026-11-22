"""Root Identity v3 and the upgrade of v1/v2 identities (#784 ADR-0054, #787 ADR-0055)."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.services import (
    bulk_upload_admission, fs_preflight, root_pins, snapshot_cleanup, source_identity,
    source_layout, upload_remediation, vault_relocation,
)
from app.services.upload_provenance import UploadBlocked
from tests.postgres import PostgresConnection, database_dsn
from tests.test_database import run_alembic

ANON_47 = os.makedev(0, 47)
ANON_46 = os.makedev(0, 46)
SDA1 = os.makedev(8, 1)
SDB1 = os.makedev(8, 17)
INO = 216454260602842259


def dir_stat(dev: int, ino: int = INO) -> os.stat_result:
    return os.stat_result((stat.S_IFDIR | 0o755, ino, dev, 2, 0, 0, 4096, 0, 0, 0))


V3 = "linux-stat-dir-v3"


def mountinfo(volume: Path, device: int, *, filesystem: str = "fuse.shfs",
              source: str = "shfs", root: str = "/server/Backup") -> str:
    """A mountinfo text with ``volume`` mounted as ``filesystem`` on ``device``."""
    return ("20 1 8:1 / / rw shared:1 - ext4 /dev/sda1 rw\n"
            f"40 20 {os.major(device)}:{os.minor(device)} {root} {volume} rw shared:9 "
            f"- {filesystem} {source} rw\n")


def path_identity(volume: str, relative: str) -> str:
    return hashlib.sha256(json.dumps(
        {"path": relative, "version": V3, "volume": volume},
        separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def json_v1(dev: int, ino: int) -> str:
    payload = json.dumps({"dev": dev, "ino": ino, "version": "linux-stat-dir-v1"},
                         separators=(",", ":"), sort_keys=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def v3_inode(info: os.stat_result) -> str:
    return vault_relocation._inode_identity(info, V3)


class IdentityShapeTests(unittest.TestCase):
    def test_anonymous_device_identity_is_the_inode_alone(self):
        self.assertEqual(vault_relocation._v2_identity(dir_stat(ANON_47)),
                         vault_relocation._v2_identity(dir_stat(ANON_46)))
        expected = hashlib.sha256(
            b'{"dev":null,"ino":%d,"version":"linux-stat-dir-v2"}' % INO).hexdigest()
        self.assertEqual(vault_relocation._v2_identity(dir_stat(ANON_46)), expected)
        self.assertNotEqual(vault_relocation._v2_identity(dir_stat(ANON_46, INO + 1)), expected)
        # v3 keeps the rule off FUSE, under its own version.
        self.assertEqual(v3_inode(dir_stat(ANON_46)), hashlib.sha256(
            b'{"dev":null,"ino":%d,"version":"linux-stat-dir-v3"}' % INO).hexdigest())

    def test_real_block_device_keeps_the_device_number(self):
        # Two ext4 roots share inode 2: only the device tells them apart.
        sda = vault_relocation._v2_identity(dir_stat(SDA1, 2))
        sdb = vault_relocation._v2_identity(dir_stat(SDB1, 2))
        self.assertNotEqual(sda, sdb)
        expected = hashlib.sha256(
            b'{"dev":%d,"ino":2,"version":"linux-stat-dir-v2"}' % SDA1).hexdigest()
        self.assertEqual(sda, expected)
        self.assertNotEqual(sda, vault_relocation._v2_identity(dir_stat(ANON_47, 2)))
        self.assertNotEqual(v3_inode(dir_stat(SDA1, 2)), v3_inode(dir_stat(SDB1, 2)))

    def test_versions_never_collide(self):
        for dev in (ANON_47, SDA1):
            values = {vault_relocation._v2_identity(dir_stat(dev)), json_v1(dev, INO),
                      v3_inode(dir_stat(dev))}
            self.assertEqual(len(values), 3)

    def test_non_directory_and_missing_inode_are_refused(self):
        regular = os.stat_result((stat.S_IFREG | 0o644, INO, ANON_46, 1, 0, 0, 1, 0, 0, 0))
        with self.assertRaises(vault_relocation.VaultRelocationError):
            vault_relocation._identity_for("/x", regular)
        with self.assertRaises(vault_relocation.VaultRelocationError):
            vault_relocation._identity_for("/x", dir_stat(ANON_46, 0))

    def test_legacy_bytes_are_exactly_the_json_dumps_of_v1(self):
        for dev in (0, 47, ANON_46, os.makedev(0, 255), os.makedev(0, 256),
                    os.makedev(0, (1 << 20) - 1), SDA1, os.makedev(259, 3)):
            for ino in (1, 2, INO, (1 << 64) - 1):
                self.assertEqual(vault_relocation._legacy_identity(dev, ino), json_v1(dev, ino))

    def test_search_finds_exactly_the_old_anonymous_device(self):
        self.assertEqual(vault_relocation._anonymous_devices_reproducing(json_v1(ANON_47, INO), INO),
                         [ANON_47])
        high = os.makedev(0, 700001)
        self.assertEqual(vault_relocation._anonymous_devices_reproducing(json_v1(high, INO), INO), [high])
        self.assertEqual(vault_relocation._anonymous_devices_reproducing(json_v1(ANON_47, INO + 1), INO), [])
        self.assertEqual(vault_relocation._anonymous_devices_reproducing(json_v1(SDA1, INO), INO), [])
        self.assertEqual(vault_relocation._anonymous_devices_reproducing("not-hex", INO), [])


class FusePathIdentityTests(unittest.TestCase):
    """On FUSE the identity is the Source Volume Identity and the path (#787, ADR-0055)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.volume = Path(self.tmp.name) / "appdata-backup"
        self.root = self.volume / "Backup"
        self.root.mkdir(parents=True)
        self.device = self.root.stat().st_dev
        self.text = mountinfo(self.volume, self.device)
        read = patch.object(source_identity, "read_mountinfo_text", side_effect=lambda: self.text)
        read.start(); self.addCleanup(read.stop)

    def fingerprint(self) -> str:
        return source_identity.fingerprint_for_mount(self.volume, text=self.text)

    def test_identity_is_volume_and_relative_path(self):
        self.assertEqual(vault_relocation.root_identity(self.root),
                         path_identity(self.fingerprint(), "Backup"))
        self.assertEqual(vault_relocation.root_identity(self.volume),
                         path_identity(self.fingerprint(), "."))
        self.assertTrue(vault_relocation.root_is_on_fuse(self.root))

    def test_every_fuse_type_uses_the_path(self):
        for filesystem in ("fuse.shfs", "fuse.mergerfs", "fuse", "FUSE.sshfs"):
            with self.subTest(filesystem=filesystem):
                self.text = mountinfo(self.volume, self.device, filesystem=filesystem)
                self.assertTrue(vault_relocation.root_is_on_fuse(self.root))
        self.assertFalse(vault_relocation.filesystem_is_fuse("fuseblk"))
        self.assertFalse(vault_relocation.filesystem_is_fuse("fusectl"))

    def test_other_filesystems_keep_the_inode_rule(self):
        for filesystem in ("nfs4", "zfs", "btrfs", "xfs", "fuseblk"):
            with self.subTest(filesystem=filesystem):
                self.text = mountinfo(self.volume, self.device, filesystem=filesystem)
                self.assertFalse(vault_relocation.root_is_on_fuse(self.root))
                self.assertEqual(vault_relocation.root_identity(self.root),
                                 v3_inode(self.root.stat()))

    def test_a_changed_inode_keeps_the_identity(self):
        # shfs reports the inode of whichever branch holds the folder first:
        # the mover changes it. Here: the same path holds another directory.
        before = vault_relocation.root_identity(self.root)
        flipped = dir_stat(self.device, self.root.stat().st_ino + (1 << 56))
        self.assertEqual(vault_relocation._identity_for(self.root, flipped)[0], before)
        self.root.rmdir()
        self.root.mkdir()
        self.assertEqual(vault_relocation.root_identity(self.root), before)

    def test_another_path_or_source_volume_changes_the_identity(self):
        before = vault_relocation.root_identity(self.root)
        other = self.volume / "Other"
        other.mkdir()
        self.assertNotEqual(vault_relocation.root_identity(other), before)
        # The same alias bound to another host folder: another Source Volume.
        self.text = mountinfo(self.volume, self.device, root="/server")
        self.assertNotEqual(vault_relocation.root_identity(self.root), before)
        self.text = mountinfo(self.volume, self.device, source="mergerfs")
        self.assertNotEqual(vault_relocation.root_identity(self.root), before)

    def test_symlink_non_directory_and_foreign_device_fail_closed(self):
        link = self.volume / "link"
        link.symlink_to(self.root)
        with self.assertRaises(vault_relocation.VaultRelocationError) as raised:
            vault_relocation.root_identity(link)
        self.assertEqual(raised.exception.reason, "symlink")
        regular = self.volume / "file"
        regular.write_bytes(b"x")
        with self.assertRaises(vault_relocation.VaultRelocationError) as raised:
            vault_relocation.root_identity(regular)
        self.assertEqual(raised.exception.reason, "not_directory")
        # The folder is not on the mount the metadata names.
        self.text = mountinfo(self.volume, os.makedev(0, 999))
        with self.assertRaises(vault_relocation.VaultRelocationError) as raised:
            vault_relocation.root_identity(self.root)
        self.assertEqual(raised.exception.reason, "identity_ambiguous")
        # Stacked mounts: no single Source Volume Identity.
        self.text = mountinfo(self.volume, self.device) + mountinfo(self.volume, self.device).split("\n")[1]
        with self.assertRaises(vault_relocation.VaultRelocationError) as raised:
            vault_relocation.root_identity(self.root)
        self.assertEqual(raised.exception.reason, "identity_ambiguous")

    def test_unreadable_mount_metadata_is_inaccessible(self):
        with patch.object(source_identity, "read_mountinfo_text", side_effect=OSError("gone")), \
                self.assertRaises(vault_relocation.VaultRelocationError) as raised:
            vault_relocation.root_identity(self.root)
        self.assertEqual(raised.exception.reason, "inaccessible")

    def test_an_open_descriptor_must_still_be_the_folder_at_the_path(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        identity = vault_relocation.root_identity(self.root)
        self.assertEqual(vault_relocation.opened_root_identity(fd, self.root), identity)
        # Same path, another folder: the path identity matches, the descriptor does not.
        self.root.rename(self.volume / "moved")
        self.root.mkdir()
        with self.assertRaises(vault_relocation.VaultRelocationError) as raised:
            vault_relocation.opened_root_identity(fd, self.root)
        self.assertEqual(raised.exception.reason, "replaced")


class RootFenceTests(unittest.TestCase):
    """The in-scan root fence on FUSE follows the Root Identity (#787)."""

    def test_root_check_replaces_the_inode_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flips = iter([(1, 1), (1, 2), (1, 2)])
            with patch.object(fs_preflight, "_directory_identity", side_effect=lambda fd: next(flips)):
                with fs_preflight.PinnedTree(root) as tree:
                    with self.assertRaises(fs_preflight.TreeIdentityChanged):
                        tree.validate()
            flips = iter([(1, 1), (1, 2)])
            with patch.object(fs_preflight, "_directory_identity", side_effect=lambda fd: next(flips)):
                with fs_preflight.PinnedTree(root, root_check=lambda fd: True) as tree:
                    tree.validate()
            with fs_preflight.PinnedTree(root, root_check=lambda fd: False) as tree:
                with self.assertRaises(fs_preflight.TreeIdentityChanged):
                    tree.validate()

    def test_scan_root_check_only_on_fuse(self):
        from app import storage
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory) / "vol"
            volume.mkdir()
            device = volume.stat().st_dev
            text = mountinfo(volume, device)
            with patch.object(source_identity, "read_mountinfo_text", return_value=text):
                vault = {"source_root": str(volume), "root_identity": vault_relocation.root_identity(volume)}
                check = storage._scan_root_check(vault)
                self.assertIsNotNone(check)
                fd = os.open(volume, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    self.assertTrue(check(fd))
                    self.assertFalse(storage._scan_root_check({**vault, "root_identity": "other"})(fd))
                finally:
                    os.close(fd)
            with patch.object(source_identity, "read_mountinfo_text",
                              return_value=mountinfo(volume, device, filesystem="xfs")):
                self.assertIsNone(storage._scan_root_check(vault))


class RootPinTests(unittest.TestCase):
    vault = {"root_identity": "v2", "retired_root_identities": ["v1"]}

    def test_every_retired_identity_names_the_same_root(self):
        vault = {"root_identity": "v3", "retired_root_identities": ["v1", "v2"]}
        for pinned in ("v1", "v2", "v3"):
            for current in ("v1", "v2", "v3"):
                self.assertTrue(root_pins.same_root(pinned, current, vault))
        self.assertFalse(root_pins.same_root("v0", "v3", vault))
        self.assertFalse(root_pins.same_root("v1", "v3", {"root_identity": "v3",
                                                           "retired_root_identities": []}))
        # A retired value without a current root names nothing.
        self.assertFalse(root_pins.same_root("v1", "v2", {"root_identity": None,
                                                           "retired_root_identities": ["v1", "v2"]}))

    def test_retired_identity_names_the_same_root(self):
        self.assertTrue(root_pins.same_root("v1", "v2", self.vault))
        self.assertTrue(root_pins.same_root("v2", "v1", self.vault))
        self.assertTrue(root_pins.same_root("v2", "v2", {"root_identity": "v2"}))
        self.assertFalse(root_pins.same_root("v1", "v2", {"root_identity": "v2"}))
        self.assertFalse(root_pins.same_root("other", "v2", self.vault))
        self.assertFalse(root_pins.same_root(None, "v2", self.vault))

    def test_same_pin_compares_every_other_field_exactly(self):
        pin = {"path": "a", "root_identity": "v1", "source_root": "/sources/x"}
        self.assertTrue(root_pins.same_pin(pin, {**pin, "root_identity": "v2"}, self.vault))
        self.assertFalse(root_pins.same_pin(pin, {**pin, "root_identity": "v2", "path": "b"}, self.vault))
        self.assertFalse(root_pins.same_pin(pin, {"path": "a", "root_identity": "v2"}, self.vault))

    def test_remediation_accepts_a_proof_pinned_with_the_retired_identity(self):
        state = {"path": "a", "path_since": 1, "held_version_id": None}
        target = {"path": "a", "path_since": 1, "source_root": "/sources/x", "root_identity": "v1"}
        pinned = SimpleNamespace(target=target)
        vault = {"id": 7, "lifecycle": "active", "paused": False, "source_root": "/sources/x",
                 "root_identity": "v2", "retired_root_identities": []}
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        self.assertEqual(upload_remediation._suspension(connection, state, vault, pinned, automatic=False),
                         "identity_changed")
        vault["retired_root_identities"] = ["v1"]
        self.assertIsNone(upload_remediation._suspension(connection, state, vault, pinned, automatic=False))

    def test_upload_bound_to_a_snapshot_pinned_with_the_retired_identity_proceeds(self):
        from dataclasses import replace as dc_replace
        from app.services import upload_provenance as provenance
        from app.services.upload_snapshot import prepare_upload_snapshot
        from tests.upload_provenance_provider import provider_attempt
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file"
            source.write_bytes(b"content")
            snapshot, _ = prepare_upload_snapshot(source, lambda: None)
        attempt = provider_attempt(snapshot, token="token", provider="fake", send_started=False)
        old_pin = {**attempt.target, "root_identity": "v1"}
        admitted = dc_replace(attempt.admitted, target=old_pin)
        attempt = dc_replace(attempt, admitted=admitted)
        live_pin = {**old_pin, "root_identity": "v2"}
        job = {"id": 1, "vault_id": 2}
        for vault, allowed in (({"root_identity": "v2", "retired_root_identities": ["v1"]}, True),
                               ({"root_identity": "v2", "retired_root_identities": []}, False)):
            with self.subTest(allowed=allowed), \
                    patch.object(provenance, "_target", return_value=(live_pin, vault)), \
                    patch.object(provenance, "_bound", return_value=(admitted, False)), \
                    patch.object(provenance, "_open_attempt", return_value=attempt):
                connection = MagicMock()
                connection.execute.return_value.fetchone.return_value = vault
                calls = (lambda: provenance.prepare(
                             connection, job, pinned=live_pin, snapshot=snapshot,
                             requested="s3_checksum", local_generation=1, before={},
                             timestamp="now"),
                         lambda: provenance.require_target(connection, job, old_pin))
                for call in calls:
                    if allowed:
                        call()
                    else:
                        with self.assertRaises(UploadBlocked):
                            call()

    def test_cleanup_batch_pinned_with_the_retired_identity_resumes(self):
        batch = {"provider": "s3", "path_since": 1, "source_root": "/x", "root_identity": "v1"}
        live = {**batch, "root_identity": "v2"}
        snapshot_cleanup._require_pin(batch, live, self.vault)
        with self.assertRaises(UploadBlocked):
            snapshot_cleanup._require_pin(batch, live, {"root_identity": "v2"})
        with self.assertRaises(UploadBlocked):
            snapshot_cleanup._require_pin(batch, {**live, "source_root": "/y"}, self.vault)


class LegacyUpgradeTests(unittest.TestCase):
    """The startup upgrade against a real catalog and a real folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog"
        result = run_alembic(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = replace(Settings(), database_dsn=database_dsn(str(self.db_path)))
        patcher = patch("app.database.settings", settings)
        patcher.start(); self.addCleanup(patcher.stop)
        self.sources = Path(self.tmp.name) / "sources"
        self.volume = self.sources / "photos"
        self.root = self.volume / "root"
        self.root.mkdir(parents=True)
        (self.root / "a.txt").write_bytes(b"a")
        (self.sources / "managed").mkdir()
        source_layout.override_sources_root(self.sources)
        self.addCleanup(source_layout.reset_sources_root_override)
        self.mounted = {self.sources, self.volume}
        mount = patch.object(source_layout, "path_is_mount", side_effect=lambda p: Path(p) in self.mounted)
        mount.start(); self.addCleanup(mount.stop)
        identity = patch.object(source_identity, "fingerprint_for_mount", return_value="mount-a")
        self.fingerprint = identity.start(); self.addCleanup(identity.stop)
        # The device number the folder reports; the real one depends on the host.
        self.device = ANON_46
        real_payload = vault_relocation._identity_payload
        device = patch.object(vault_relocation, "_identity_payload",
                              side_effect=lambda info: {**real_payload(info), "dev": self.device})
        device.start(); self.addCleanup(device.stop)
        self.addCleanup(vault_relocation._legacy_recovery_attempted.clear)
        self.addCleanup(setattr, vault_relocation, "_legacy_upgrade_pending", False)
        self.ino = self.root.stat().st_ino
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')")
            connection.execute("INSERT INTO vault_targets(provider, bucket, region, key_layout) "
                               "VALUES ('s3', 'bucket', 'eu-south-1', 'vault_prefix')")
            connection.execute("""INSERT INTO vaults(id, uuid, target_id, name, source_root)
                VALUES (7, '11111111-1111-1111-1111-111111111111',
                        (SELECT id FROM vault_targets WHERE bucket='bucket'), 'Photos', %s)""",
                               (str(self.root),))
        source_layout.reconcile_source_volume_identities()

    def store_v1(self, dev: int, ino: int | None = None) -> str:
        identity = json_v1(dev, self.ino if ino is None else ino)
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET root_identity_version='linux-stat-dir-v1', "
                               "root_identity=%s WHERE id=7", (identity,))
        return identity

    def row(self):
        with PostgresConnection(str(self.db_path)) as connection:
            return connection.execute("SELECT * FROM vaults WHERE id=7").fetchone()

    def audit_events(self):
        with PostgresConnection(str(self.db_path)) as connection:
            return connection.execute(
                "SELECT * FROM audit_events WHERE event='vault_root_identity_recovered'").fetchall()

    def scan(self):
        from app.storage import scan_vault
        with (patch("app.storage.validate_cloud_vault"),
              patch("app.storage.scan_cloud", return_value=0),
              patch("app.storage.sync_lifecycle_rules_for_bucket", return_value=0),
              patch("app.storage.queue_auto_uploads", return_value=0),
              patch("app.storage.queue_auto_local_cleanups", return_value=0)):
            return scan_vault(dict(self.row()))

    def open_blocked_problems(self):
        with PostgresConnection(str(self.db_path)) as connection:
            return connection.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE event='protection_blocked' "
                "AND problem_status='open'").fetchone()["n"]

    def test_unchanged_v1_is_rewritten_as_v3_silently(self):
        self.device = SDB1
        old = self.store_v1(SDB1)
        counts = vault_relocation.upgrade_legacy_root_identities()
        self.assertEqual(counts["upgraded"], 1)
        row = self.row()
        self.assertEqual(row["root_identity_version"], V3)
        self.assertEqual(row["root_identity"], vault_relocation.root_identity(self.root))
        self.assertEqual(row["retired_root_identities"], [old])
        self.assertEqual(self.audit_events(), [])
        # Running again changes nothing: the identity is no longer v1.
        self.assertEqual(vault_relocation.upgrade_legacy_root_identities(),
                         {"upgraded": 0, "recovered": 0, "adopted": 0, "unmatched": 0, "deferred": 0})

    def test_device_only_change_is_verified_recorded_and_the_scan_proceeds(self):
        old = self.store_v1(ANON_47)
        # The reboot: the stored identity no longer matches and the scan is blocked.
        self.assertEqual(self.scan().get("root_identity_mismatch"), 1)
        self.assertEqual(self.open_blocked_problems(), 1)
        counts = vault_relocation.upgrade_legacy_root_identities()
        self.assertEqual(counts["recovered"], 1)
        row = self.row()
        self.assertEqual(row["root_identity_version"], V3)
        self.assertEqual(row["root_identity"], vault_relocation.root_identity(self.root))
        self.assertEqual(row["retired_root_identities"], [old])
        self.assertEqual(row["lifecycle"], "active")
        [event] = self.audit_events()
        self.assertEqual(event["actor"], "system")
        self.assertEqual(event["vault_id"], 7)
        self.assertEqual(event["detail"]["verified"], "device_number_only")
        self.assertEqual(event["detail"]["previous_device"], "0:47")
        self.assertEqual(event["detail"]["current_device"], "0:46")
        result = self.scan()
        self.assertNotIn("root_identity_mismatch", result)
        self.assertEqual(result.get("local"), 1)
        self.assertEqual(self.open_blocked_problems(), 0)
        with PostgresConnection(str(self.db_path)) as connection:
            files = connection.execute("SELECT path, local_presence FROM files WHERE vault_id=7").fetchall()
        self.assertEqual([(f["path"], f["local_presence"]) for f in files], [("a.txt", "present")])
        # The next reboot renumbers the device again: v3 does not notice.
        self.device = os.makedev(0, 51)
        self.assertNotIn("root_identity_mismatch", self.scan())

    def test_no_reproducing_device_leaves_the_vault_blocked(self):
        old = self.store_v1(ANON_47, ino=self.ino + 1)
        with patch.object(vault_relocation, "_anonymous_devices_reproducing",
                          wraps=vault_relocation._anonymous_devices_reproducing) as search:
            self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["unmatched"], 1)
            # At most once per Vault per process.
            self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["unmatched"], 1)
        self.assertEqual(search.call_count, 1)
        row = self.row()
        self.assertEqual((row["root_identity_version"], row["root_identity"]), ("linux-stat-dir-v1", old))
        self.assertEqual(row["retired_root_identities"], [])
        self.assertEqual(self.audit_events(), [])
        self.assertEqual(self.scan().get("root_identity_mismatch"), 1)

    def test_real_block_device_mismatch_is_not_searched(self):
        self.device = SDB1
        self.store_v1(SDA1)
        with patch.object(vault_relocation, "_anonymous_devices_reproducing") as search:
            self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["unmatched"], 1)
        search.assert_not_called()
        self.assertEqual(self.row()["root_identity_version"], "linux-stat-dir-v1")

    def test_unhealthy_source_volume_is_skipped_until_it_is_back(self):
        old = self.store_v1(ANON_47)
        self.mounted = {self.sources}  # The Source Volume is not mounted.
        source_layout.reconcile_source_volume_identities()
        self.assertFalse(source_layout.vault_local_access(str(self.root)).local_operations_allowed)
        self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["deferred"], 1)
        self.assertEqual(self.row()["root_identity"], old)
        self.assertEqual(self.audit_events(), [])
        self.assertEqual(vault_relocation.upgrade_pending_legacy_root_identities()["deferred"], 1)
        self.mounted = {self.sources, self.volume}
        source_layout.reconcile_source_volume_identities()
        self.assertEqual(vault_relocation.upgrade_pending_legacy_root_identities()["recovered"], 1)
        self.assertEqual(self.row()["root_identity_version"], V3)
        self.assertIsNone(vault_relocation.upgrade_pending_legacy_root_identities())

    def test_released_root_is_left_alone(self):
        old = self.store_v1(ANON_47)
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET lifecycle='tombstone', root_released_at=now() WHERE id=7")
        self.assertEqual(sum(vault_relocation.upgrade_legacy_root_identities().values()), 0)
        self.assertEqual(self.row()["root_identity"], old)

    def test_pending_bulk_upload_stays_valid_and_reconnect_forgets_the_retired_identity(self):
        self.store_v1(ANON_47)
        group = str(uuid.uuid4())
        with PostgresConnection(str(self.db_path)) as connection:
            before = connection.execute("SELECT * FROM vaults WHERE id=7").fetchone()
            connection.execute(
                """INSERT INTO bulk_upload_admissions(group_id, vault_id, requested_at, updated_at,
                       path, verification_mode, vault_identity_json, selected_count, status)
                   VALUES (%s, 7, now(), now(), '', 'read_back', %s, 1, 'pending')""",
                (group, bulk_upload_admission._identity(before)))
        vault_relocation.upgrade_legacy_root_identities()
        with PostgresConnection(str(self.db_path)) as connection:
            request = connection.execute("SELECT vault_identity_json FROM bulk_upload_admissions "
                                         "WHERE group_id=%s", (group,)).fetchone()
            after = connection.execute("SELECT * FROM vaults WHERE id=7").fetchone()
        self.assertEqual(request["vault_identity_json"], bulk_upload_admission._identity(after))
        other = self.volume / "other"
        other.mkdir()
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("DELETE FROM bulk_upload_admissions")
            vault_relocation.reconnect_vault_root(
                connection, vault_id=7, volume_alias="photos", relative_path="other",
                actor="owner", reason="moved")
        self.addCleanup(vault_relocation._runtime_suspended_vaults.discard, 7)
        self.assertEqual(self.row()["retired_root_identities"], [])


    # --- v2 identities and FUSE Source Volumes (#787, ADR-0055) -----------

    def store(self, version: str, identity: str, retired: list[str] | None = None) -> str:
        with PostgresConnection(str(self.db_path)) as connection:
            connection.execute("UPDATE vaults SET root_identity_version=%s, root_identity=%s, "
                               "retired_root_identities=%s WHERE id=7",
                               (version, identity, retired or []))
        return identity

    def events(self, event: str):
        with PostgresConnection(str(self.db_path)) as connection:
            return connection.execute("SELECT * FROM audit_events WHERE event=%s", (event,)).fetchall()

    def source_error(self):
        with PostgresConnection(str(self.db_path)) as connection:
            row = connection.execute("SELECT last_error FROM vault_component_health "
                                     "WHERE vault_id=7 AND component='source'").fetchone()
        return row["last_error"] if row else None

    def on_fuse(self):
        text = mountinfo(self.volume, self.root.stat().st_dev)
        read = patch.object(source_identity, "read_mountinfo_text", return_value=text)
        read.start(); self.addCleanup(read.stop)
        self.assertTrue(vault_relocation.root_is_on_fuse(self.root))

    def test_unchanged_v2_is_rewritten_as_v3_and_every_retired_value_is_kept(self):
        v2 = self.store("linux-stat-dir-v2", vault_relocation._v2_identity(self.root.stat()), ["v1-old"])
        self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["upgraded"], 1)
        row = self.row()
        self.assertEqual((row["root_identity_version"], row["root_identity"]),
                         (V3, vault_relocation.root_identity(self.root)))
        self.assertEqual(row["retired_root_identities"], ["v1-old", v2])
        for pinned in ("v1-old", v2):
            self.assertTrue(root_pins.same_root(pinned, row["root_identity"], row))
        self.assertEqual(self.events("vault_root_identity_adopted"), [])

    def test_changed_v2_off_fuse_stays_blocked_as_a_mismatch(self):
        old = self.store("linux-stat-dir-v2", vault_relocation._v2_identity(
            dir_stat(self.device, self.ino + 1)))
        self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["unmatched"], 1)
        self.assertEqual(self.row()["root_identity"], old)
        self.assertEqual(self.scan().get("root_identity_mismatch"), 1)
        self.assertIn("Vault root identity mismatch", self.source_error())
        from app.services.scan_causes import source_error_cause
        self.assertEqual(source_error_cause(self.source_error()), "root_changed")

    def test_unverifiable_v1_on_fuse_is_adopted_and_the_scan_proceeds(self):
        # Vault 4 of #787: the mover changed both the device and the inode.
        self.on_fuse()
        old = self.store_v1(ANON_47, ino=self.ino + (9 << 56))
        self.assertEqual(self.scan().get("root_identity_mismatch"), 1)
        self.assertIn("Vault root identity mismatch", self.source_error())
        self.assertEqual(self.open_blocked_problems(), 1)
        counts = vault_relocation.upgrade_legacy_root_identities()
        self.assertEqual(counts["adopted"], 1)
        row = self.row()
        self.assertEqual(row["root_identity_version"], V3)
        self.assertEqual(row["root_identity"], path_identity("mount-a", "root"))
        self.assertEqual(row["retired_root_identities"], [old])
        self.assertEqual(row["lifecycle"], "active")
        [event] = self.events("vault_root_identity_adopted")
        self.assertEqual((event["actor"], event["vault_id"]), ("system", 7))
        self.assertEqual(event["detail"]["verified"], "source_volume_and_path")
        self.assertIs(event["detail"]["previous_identity_matched"], False)
        self.assertEqual(event["detail"]["previous_identity_version"], "linux-stat-dir-v1")
        self.assertEqual(event["detail"]["identity_version"], V3)
        self.assertEqual(self.audit_events(), [])
        result = self.scan()
        self.assertNotIn("root_identity_mismatch", result)
        self.assertEqual(result.get("local"), 1)
        self.assertEqual(self.open_blocked_problems(), 0)
        # The folder leaves the cache: same path, another inode. Nothing changes.
        self.root.rename(self.volume / "root-cache")
        self.root.mkdir()
        (self.root / "a.txt").write_bytes(b"a")
        self.assertNotIn("root_identity_mismatch", self.scan())
        self.assertEqual(sum(vault_relocation.upgrade_legacy_root_identities().values()), 0)

    def test_v2_on_fuse_moves_to_the_path_identity_and_keeps_every_pin(self):
        self.on_fuse()
        v2 = self.store("linux-stat-dir-v2", vault_relocation._v2_identity(self.root.stat()), ["v1-old"])
        self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["adopted"], 1)
        row = self.row()
        self.assertEqual(row["root_identity"], path_identity("mount-a", "root"))
        self.assertEqual(row["retired_root_identities"], ["v1-old", v2])
        for pinned in ("v1-old", v2):
            self.assertTrue(root_pins.same_root(pinned, row["root_identity"], row))
        [event] = self.events("vault_root_identity_adopted")
        self.assertIs(event["detail"]["previous_identity_matched"], True)
        self.assertEqual(event["detail"]["previous_identity_version"], "linux-stat-dir-v2")

    def test_fuse_adoption_needs_a_healthy_source_volume_and_a_real_folder(self):
        self.on_fuse()
        old = self.store_v1(ANON_47, ino=self.ino + 1)
        self.mounted = {self.sources}
        source_layout.reconcile_source_volume_identities()
        self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["deferred"], 1)
        self.mounted = {self.sources, self.volume}
        source_layout.reconcile_source_volume_identities()
        self.root.rename(self.volume / "elsewhere")
        self.root.symlink_to(self.volume / "elsewhere")
        self.assertEqual(vault_relocation.upgrade_legacy_root_identities()["unmatched"], 1)
        self.assertEqual(self.row()["root_identity"], old)
        self.assertEqual(self.events("vault_root_identity_adopted"), [])


if __name__ == "__main__":
    unittest.main()
