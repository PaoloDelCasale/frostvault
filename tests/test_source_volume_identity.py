"""Markerless Source Volume replacement detection (issue #151)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.database import SQLiteConnection
from app.services import source_identity, source_layout
from tests.test_database import run_alembic


def mountinfo_line(
    target: Path,
    *,
    mount_id: int = 41,
    root: str = "/host/photos",
    source: str = "/dev/sda1",
    fstype: str = "ext4",
    options: str = "rw,relatime",
) -> str:
    escaped_target = str(target.resolve()).replace("\\", r"\134").replace(" ", r"\040")
    escaped_root = root.replace("\\", r"\134").replace(" ", r"\040")
    escaped_source = source.replace("\\", r"\134").replace(" ", r"\040")
    return (
        f"{mount_id} 1 8:1 {escaped_root} {escaped_target} {options} shared:7 "
        f"- {fstype} {escaped_source} rw\n"
    )


class MountInfoParserTests(unittest.TestCase):
    def test_decodes_kernel_escapes_exactly(self) -> None:
        entry = source_identity.parse_mountinfo(
            "37 25 0:31 /host/My\\040Photos /sources/my\\040photos rw - "
            "nfs nas:/My\\040Photos rw\\134sync\n"
        )[0]
        self.assertEqual(entry.root, "/host/My Photos")
        self.assertEqual(entry.mount_point, "/sources/my photos")
        self.assertEqual(entry.mount_source, "nas:/My Photos")
        self.assertEqual(entry.super_options, ("rw\\sync",))

    def test_remount_ids_and_options_do_not_change_fingerprint(self) -> None:
        target = Path("/sources/photos")
        first = mountinfo_line(target, mount_id=41, options="rw,relatime")
        remounted = mountinfo_line(target, mount_id=909, options="rw,nosuid,nodev")
        self.assertEqual(
            source_identity.fingerprint_for_mount(target, text=first),
            source_identity.fingerprint_for_mount(target, text=remounted),
        )

    def test_duplicate_exact_targets_are_ambiguous(self) -> None:
        target = Path("/sources/photos")
        with self.assertRaisesRegex(source_identity.MountIdentityError, "ambiguous"):
            source_identity.fingerprint_for_mount(
                target,
                text=mountinfo_line(target) + mountinfo_line(target, mount_id=42),
            )

    def test_virtual_filesystem_is_explicitly_unsupported(self) -> None:
        target = Path("/sources/photos")
        with self.assertRaisesRegex(source_identity.MountIdentityError, "unsupported"):
            source_identity.fingerprint_for_mount(
                target, text=mountinfo_line(target, fstype="overlay", source="overlay")
            )

    def test_placeholder_identity_fields_fail_closed_without_resolving_target(self) -> None:
        target = Path("/sources/photos")
        text = mountinfo_line(target).replace("/host/photos", "?").replace(
            "/dev/sda1", "?"
        )
        with patch.object(
            Path,
            "resolve",
            side_effect=AssertionError("mount identity resolved the target"),
        ):
            with self.assertRaisesRegex(source_identity.MountIdentityError, "insufficient"):
                source_identity.fingerprint_for_mount(target, text=text)


class IdentityDiagnosticI18nTests(unittest.TestCase):
    def test_en_and_it_distinguish_absent_inaccessible_and_replaced(self) -> None:
        for locale in ("en", "it"):
            messages = json.loads(
                (Path("app/locales") / f"{locale}.json").read_text(encoding="utf-8")
            )
            labels = {
                messages["admin.sources_health_absent"],
                messages["admin.sources_health_inaccessible"],
                messages["admin.sources_health_replaced"],
            }
            self.assertEqual(len(labels), 3)
            for health in ("absent", "inaccessible", "replaced"):
                self.assertTrue(messages[f"admin.sources_diagnostic_{health}"].strip())


class PersistedSourceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sources = Path(self._tmp.name) / "sources"
        self.sources.mkdir()
        (self.sources / "managed").mkdir()
        self.photos = self.sources / "photos"
        self.photos.mkdir()
        self.vault_root = self.photos / "family"
        self.vault_root.mkdir()
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.settings = replace(
            Settings(), db_backend="sqlite", sqlite_path=str(self.database_path)
        )
        settings_patcher = patch("app.database.settings", self.settings)
        settings_patcher.start()
        self.addCleanup(settings_patcher.stop)
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources)
        self.mounts = {self.sources, self.photos}
        mount_patcher = patch.object(
            source_layout,
            "path_is_mount",
            side_effect=lambda path: Path(path) in self.mounts,
        )
        mount_patcher.start()
        self.addCleanup(mount_patcher.stop)
        for name in ("path_is_writable", "path_is_accessible"):
            probe = patch.object(source_layout, name, return_value=True)
            probe.start()
            self.addCleanup(probe.stop)
        self.mountinfo = mountinfo_line(self.photos)
        identity_patcher = patch.object(
            source_identity, "read_mountinfo_text", side_effect=lambda: self.mountinfo
        )
        identity_patcher.start()
        self.addCleanup(identity_patcher.stop)

    def enroll(self) -> None:
        source_layout.reconcile_source_volume_identities()

    def test_same_source_remount_stays_available_and_raw_metadata_is_not_persisted(self) -> None:
        self.enroll()
        self.mountinfo = mountinfo_line(
            self.photos, mount_id=888, options="rw,nosuid", root="/host/photos"
        )
        access = source_layout.vault_local_access(self.vault_root)
        self.assertTrue(access.local_operations_allowed)
        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute("SELECT * FROM source_volumes").fetchone()
        serialized = json.dumps(row, sort_keys=True)
        self.assertNotIn("/host/photos", serialized)
        self.assertNotIn("/dev/sda1", serialized)
        self.assertRegex(row["expected_fingerprint"], r"^[0-9a-f]{64}$")

    def test_replacement_is_blocked_before_scan_and_audited_once_without_raw_paths(self) -> None:
        from app.storage import scan_tree

        self.enroll()
        with SQLiteConnection(str(self.database_path)) as connection:
            vault_id = connection.execute(
                """
                INSERT INTO vaults(slug, name, source_root, s3_bucket, s3_prefix, rclone_remote)
                VALUES ('family', 'Family', %s, 'b', 'p/', 'r') RETURNING id
                """,
                (str(self.vault_root),),
            ).fetchone()["id"]
        self.mountinfo = mountinfo_line(
            self.photos, root="/host/other", source="/dev/sdb1"
        )
        with self.assertRaisesRegex(RuntimeError, "replaced"):
            scan_tree({"id": vault_id, "source_root": str(self.vault_root)}, "scan")
        # Retry/rescan cannot accept the replacement and does not duplicate audit.
        self.assertEqual(
            source_layout.vault_local_access(self.vault_root).volume_health,
            "replaced",
        )
        source_layout.reconcile_source_volume_identities()
        source_layout.reconcile_source_volume_identities()
        with SQLiteConnection(str(self.database_path)) as connection:
            events = connection.execute(
                "SELECT detail_json FROM audit_events "
                "WHERE event='source_volume_identity_transition'"
            ).fetchall()
            local_count = connection.execute(
                "SELECT COUNT(*) AS total FROM local_copies"
            ).fetchone()["total"]
        self.assertEqual(len(events), 1)
        self.assertEqual(local_count, 0)
        detail = events[0]["detail_json"]
        self.assertNotIn("/host/", detail)
        self.assertNotIn("/dev/", detail)

    def test_ambiguity_fails_closed(self) -> None:
        self.enroll()
        self.mountinfo += mountinfo_line(self.photos, mount_id=42)
        access = source_layout.vault_local_access(self.vault_root)
        self.assertFalse(access.local_operations_allowed)
        self.assertEqual(access.volume_health, "identity_ambiguous")

    def test_persisted_absent_alias_remains_in_inventory(self) -> None:
        self.enroll()
        self.mounts.remove(self.photos)
        inventory = source_layout.source_volume_inventory()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["alias"], "photos")
        self.assertEqual(inventory[0]["health"], "absent")
        self.assertEqual(inventory[0]["access"], "none")

    def test_present_but_unreadable_volume_is_inaccessible(self) -> None:
        self.enroll()
        with patch.object(source_layout, "path_is_accessible", return_value=False):
            inventory = source_layout.source_volume_inventory()
        self.assertEqual(inventory[0]["health"], "inaccessible")

    def test_unsafe_known_health_is_classified_lexically_without_tree_traversal(self) -> None:
        self.enroll()
        configured_root = self.vault_root / "nested"
        unsafe_states = (
            ("replaced", {"identity_health": "replaced"}),
            ("identity_ambiguous", {"identity_health": "identity_ambiguous"}),
            ("identity_unsupported", {"identity_health": "identity_unsupported"}),
            ("absent", {"identity_health": "absent", "lost": True}),
            ("mount_lost", {"lost": True, "needs_scan": True}),
        )
        for expected_health, state in unsafe_states:
            with self.subTest(health=expected_health):
                source_layout._runtime_mount_state["photos"] = state

                def reject_resolve(path: Path, *_args, **_kwargs):
                    if str(path).startswith(str(self.photos)):
                        raise AssertionError(f"resolved unsafe Source Volume path: {path}")
                    return Path(str(path))

                def reject_stat(path: Path, *_args, **_kwargs):
                    if str(path).startswith(str(self.photos)):
                        raise AssertionError(f"statted unsafe Source Volume path: {path}")
                    raise AssertionError(f"unexpected stat outside Source Volume: {path}")

                with (
                    patch.object(Path, "resolve", reject_resolve),
                    patch.object(Path, "stat", reject_stat),
                    patch("os.path.realpath", side_effect=AssertionError("realpath called")),
                ):
                    access = source_layout.vault_local_access(configured_root)
                self.assertEqual(access.volume_alias, "photos")
                self.assertEqual(access.volume_health, expected_health)
                self.assertFalse(access.local_operations_allowed)

    def test_parent_segments_fail_closed_before_filesystem_access(self) -> None:
        configured_root = f"{self.photos}/family/../other"
        with (
            patch.object(Path, "resolve", side_effect=AssertionError("resolve called")),
            patch.object(Path, "stat", side_effect=AssertionError("stat called")),
            patch("os.path.realpath", side_effect=AssertionError("realpath called")),
            patch.object(
                source_layout,
                "discover_source_volumes",
                side_effect=AssertionError("volume discovery called"),
            ),
        ):
            access = source_layout.vault_local_access(configured_root)
        self.assertFalse(access.local_operations_allowed)
        self.assertIsNone(access.volume_alias)
        self.assertEqual(access.volume_health, "unavailable")

    def test_inaccessible_volume_does_not_resolve_or_stat_configured_tree(self) -> None:
        configured_root = self.vault_root / "nested"
        original_stat = Path.stat

        def guarded_stat(path: Path, *args, **kwargs):
            if str(path).startswith(str(configured_root)):
                raise AssertionError(f"statted inaccessible Vault tree: {path}")
            return original_stat(path, *args, **kwargs)

        with (
            patch.object(source_layout, "path_is_accessible", return_value=False),
            patch.object(
                Path,
                "resolve",
                side_effect=AssertionError("resolved inaccessible Vault tree"),
            ),
            patch.object(Path, "stat", guarded_stat),
            patch("os.path.realpath", side_effect=AssertionError("realpath called")),
        ):
            access = source_layout.vault_local_access(configured_root)
        self.assertEqual(access.volume_alias, "photos")
        self.assertEqual(access.volume_health, "inaccessible")
        self.assertFalse(access.local_operations_allowed)

    def test_safe_volume_canonicalization_rejects_symlink_escape(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        link = self.photos / "escape"
        link.symlink_to(outside, target_is_directory=True)
        access = source_layout.vault_local_access(link)
        self.assertEqual(access.volume_alias, "photos")
        self.assertEqual(access.volume_health, "unavailable")
        self.assertFalse(access.local_operations_allowed)

    def test_watcher_checks_identity_gate_before_resolving_vault_root(self) -> None:
        from app import storage

        source_layout.note_mount_lost("photos")
        vault = {"id": 99, "source_root": str(self.vault_root)}
        with (
            patch.object(
                Path,
                "resolve",
                side_effect=AssertionError("watcher resolved blocked vault root"),
            ),
            patch.object(
                storage.asyncio,
                "sleep",
                side_effect=asyncio.CancelledError,
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(storage._watch_vault_filesystem(vault))


if __name__ == "__main__":
    unittest.main()
