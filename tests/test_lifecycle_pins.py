"""Lifecycle pin and deepening-only seams (issue #110).

Seams under test:
9. Setting a pin prevents subsequent automatic lifecycle application to that
   file/prefix; clearing the pin allows policy again without warming.
10. After a manual force to a cold class, a shallower lifecycle target does not
    warm the object (deepening-only invariant), with or without pin.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.lifecycle_policies import (
    create_policy,
    load_policy_assignments,
    refresh_desired_policies,
    resolve_effective_policy_id,
    set_vault_default_policy,
)
from app.services.lifecycle_pins import (
    clear_lifecycle_pin,
    is_path_pinned,
    load_lifecycle_pins,
    set_lifecycle_pin,
)
from app.services.policy_reconciliation import reconcile_pending_policy_tags
from app.services.storage_classes import lifecycle_may_apply_transition
from tests.test_database import run_alembic


class DeepeningOnlyTests(unittest.TestCase):
    def test_shallower_lifecycle_target_does_not_apply(self) -> None:
        self.assertFalse(
            lifecycle_may_apply_transition(
                current_class="GLACIER",
                target_class="STANDARD_IA",
            )
        )

    def test_deeper_lifecycle_target_may_apply(self) -> None:
        self.assertTrue(
            lifecycle_may_apply_transition(
                current_class="GLACIER",
                target_class="DEEP_ARCHIVE",
            )
        )


class LifecyclePinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pins.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'owner', 'Owner', 'x', FALSE)"
            )
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'vaults/uuid', 'remote')"
            )
            self.default_policy = create_policy(connection, vault_id=1, name="default")
            set_vault_default_policy(connection, 1, self.default_policy)
            catalog = ArchiveCatalog(connection)
            self.version_id = catalog.record_archive_version(
                vault_id=1,
                path="photos/album.jpg",
                object_key="vaults/uuid/photos/album.jpg",
                provider_version_id="v1",
                size=10,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-01-01T00:00:00+00:00",
                observed_at="2026-01-01T00:00:00+00:00",
                scan_id="2026-01-01T00:00:00+00:00",
                desired_policy_id=self.default_policy,
                applied_policy_id=self.default_policy,
            )

    def test_file_pin_clears_desired_policy_and_reconciliation_removes_tag(self) -> None:
        client = Mock()
        with SQLiteConnection(str(self.path)) as connection:
            set_lifecycle_pin(
                connection,
                vault_id=1,
                path="photos/album.jpg",
                is_directory=False,
                pinned_by=1,
                pinned_at="2026-07-21T12:00:00+00:00",
            )
            refresh_desired_policies(connection, 1)
            version = connection.execute(
                "SELECT desired_policy_id, applied_policy_id FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()
            self.assertIsNone(version["desired_policy_id"])
            self.assertEqual(version["applied_policy_id"], self.default_policy)
            cleared = reconcile_pending_policy_tags(
                connection,
                {"id": 1, "s3_bucket": "bucket"},
                client,
            )
            self.assertEqual(cleared, 1)
            client.delete_object_tagging.assert_called_once_with(
                Bucket="bucket",
                Key="vaults/uuid/photos/album.jpg",
                VersionId="v1",
            )
            version = connection.execute(
                "SELECT desired_policy_id, applied_policy_id FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()
            self.assertIsNone(version["desired_policy_id"])
            self.assertIsNone(version["applied_policy_id"])

    def test_clearing_pin_restores_desired_policy_without_warming(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_lifecycle_pin(
                connection,
                vault_id=1,
                path="photos",
                is_directory=True,
                pinned_by=1,
                pinned_at="2026-07-21T12:00:00+00:00",
            )
            refresh_desired_policies(connection, 1)
            self.assertTrue(is_path_pinned(connection, 1, "photos/album.jpg"))
            clear_lifecycle_pin(connection, vault_id=1, path="photos")
            refresh_desired_policies(connection, 1)
            version = connection.execute(
                "SELECT desired_policy_id, storage_class FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()
            self.assertEqual(version["desired_policy_id"], self.default_policy)
            self.assertEqual(version["storage_class"], "STANDARD")
            pins = load_lifecycle_pins(connection, 1)
            self.assertEqual(pins, ())
            # Effective policy resolution still returns the vault default; warming
            # is independently blocked by the deepening-only helper.
            self.assertEqual(
                resolve_effective_policy_id(
                    "photos/album.jpg",
                    load_policy_assignments(connection, 1),
                ),
                self.default_policy,
            )
            self.assertFalse(
                lifecycle_may_apply_transition(
                    current_class="GLACIER",
                    target_class="STANDARD_IA",
                )
            )


if __name__ == "__main__":
    unittest.main()
