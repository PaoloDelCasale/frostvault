"""Root Identity v2 and the upgrade of v1 identities (#784, ADR-0054)."""
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
    bulk_upload_admission, root_pins, snapshot_cleanup, source_identity, source_layout,
    upload_remediation, vault_relocation,
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


def json_v1(dev: int, ino: int) -> str:
    payload = json.dumps({"dev": dev, "ino": ino, "version": "linux-stat-dir-v1"},
                         separators=(",", ":"), sort_keys=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


class IdentityShapeTests(unittest.TestCase):
    def test_anonymous_device_identity_is_the_inode_alone(self):
        self.assertEqual(vault_relocation._identity_from_stat(dir_stat(ANON_47)),
                         vault_relocation._identity_from_stat(dir_stat(ANON_46)))
        expected = hashlib.sha256(
            b'{"dev":null,"ino":%d,"version":"linux-stat-dir-v2"}' % INO).hexdigest()
        self.assertEqual(vault_relocation._identity_from_stat(dir_stat(ANON_46)), expected)
        self.assertNotEqual(vault_relocation._identity_from_stat(dir_stat(ANON_46, INO + 1)), expected)

    def test_real_block_device_keeps_the_device_number(self):
        # Two ext4 roots share inode 2: only the device tells them apart.
        sda = vault_relocation._identity_from_stat(dir_stat(SDA1, 2))
        sdb = vault_relocation._identity_from_stat(dir_stat(SDB1, 2))
        self.assertNotEqual(sda, sdb)
        expected = hashlib.sha256(
            b'{"dev":%d,"ino":2,"version":"linux-stat-dir-v2"}' % SDA1).hexdigest()
        self.assertEqual(sda, expected)
        self.assertNotEqual(sda, vault_relocation._identity_from_stat(dir_stat(ANON_47, 2)))

    def test_v2_never_equals_v1(self):
        for dev in (ANON_47, SDA1):
            self.assertNotEqual(vault_relocation._identity_from_stat(dir_stat(dev)), json_v1(dev, INO))

    def test_non_directory_and_missing_inode_are_refused(self):
        regular = os.stat_result((stat.S_IFREG | 0o644, INO, ANON_46, 1, 0, 0, 1, 0, 0, 0))
        with self.assertRaises(vault_relocation.VaultRelocationError):
            vault_relocation._identity_from_stat(regular)
        with self.assertRaises(vault_relocation.VaultRelocationError):
            vault_relocation._identity_from_stat(dir_stat(ANON_46, 0))

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


class RootPinTests(unittest.TestCase):
    vault = {"root_identity": "v2", "legacy_root_identity": "v1"}

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
                 "root_identity": "v2", "legacy_root_identity": None}
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        self.assertEqual(upload_remediation._suspension(connection, state, vault, pinned, automatic=False),
                         "identity_changed")
        vault["legacy_root_identity"] = "v1"
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
        for vault, allowed in (({"root_identity": "v2", "legacy_root_identity": "v1"}, True),
                               ({"root_identity": "v2", "legacy_root_identity": None}, False)):
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

    def test_unchanged_v1_is_rewritten_as_v2_silently(self):
        self.device = SDB1
        old = self.store_v1(SDB1)
        counts = vault_relocation.upgrade_legacy_root_identities()
        self.assertEqual(counts["upgraded"], 1)
        row = self.row()
        self.assertEqual(row["root_identity_version"], "linux-stat-dir-v2")
        self.assertEqual(row["root_identity"], vault_relocation.root_identity(self.root))
        self.assertEqual(row["legacy_root_identity"], old)
        self.assertEqual(self.audit_events(), [])
        # Running again changes nothing: the identity is no longer v1.
        self.assertEqual(vault_relocation.upgrade_legacy_root_identities(),
                         {"upgraded": 0, "recovered": 0, "unmatched": 0, "deferred": 0})

    def test_device_only_change_is_verified_recorded_and_the_scan_proceeds(self):
        old = self.store_v1(ANON_47)
        # The reboot: the stored identity no longer matches and the scan is blocked.
        self.assertEqual(self.scan().get("root_identity_mismatch"), 1)
        self.assertEqual(self.open_blocked_problems(), 1)
        counts = vault_relocation.upgrade_legacy_root_identities()
        self.assertEqual(counts["recovered"], 1)
        row = self.row()
        self.assertEqual(row["root_identity_version"], "linux-stat-dir-v2")
        self.assertEqual(row["root_identity"], vault_relocation.root_identity(self.root))
        self.assertEqual(row["legacy_root_identity"], old)
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
        # The next reboot renumbers the device again: v2 does not notice.
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
        self.assertIsNone(row["legacy_root_identity"])
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
        self.assertEqual(self.row()["root_identity_version"], "linux-stat-dir-v2")
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
        self.assertIsNone(self.row()["legacy_root_identity"])


if __name__ == "__main__":
    unittest.main()
