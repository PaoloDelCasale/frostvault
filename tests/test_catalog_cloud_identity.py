"""Cloud scan never elects identity from Path History (BUG-008 / REQ-020, #593).

Seam: ``scan_cloud`` after ``confirm_file_rename``. An Archive Version belongs
to its Vault File by ``file_id``, never by the path its object key spells
(ADR-0035): rediscovering the old key of a renamed file observes the same
version on the same identity, and a VersionId the catalog does not know at a
historical path is counted, never attributed to the file that once had it.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from app.catalog import ArchiveCatalog
from app.storage import scan_cloud
from tests.catalog_support import fingerprint, sha256_hex
from tests.fixtures import FrostVaultTestCase, seed_vault
from tests import catalog_probe

VAULT_UUID = "00000000-0000-4000-8000-000000000002"
PREFIX = f"vaults/{VAULT_UUID}/"
OLD_PATH = "reports/q1.pdf"
NEW_PATH = "archive/q1.pdf"


def _item(key: str, version_id: str) -> dict:
    return {
        "Key": PREFIX + key,
        "VersionId": version_id,
        "Size": 32,
        "StorageClass": "STANDARD",
        "ETag": f'"{version_id}"',
        "LastModified": datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
    }


class CloudRediscoveryIdentityTests(FrostVaultTestCase):
    """BUG-008: cloud scan must not fork Vault File identity after rename."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch("app.storage.runtime_status", {}))
        digest = sha256_hex(b"q1")
        with self.database.connect() as connection:
            seed_vault(connection, id=2, uuid=VAULT_UUID, source_root="/source")
            catalog = ArchiveCatalog(connection)
            self.file_id = catalog.observe_local_copy(
                vault_id=2, path=OLD_PATH, file_type="regular", size=32,
                mtime_ns=1_000, observed_at="2026-07-21T10:00:00+00:00",
            )
            fingerprint(catalog, 2, OLD_PATH, digest)
            row = catalog_probe.get_file_by_path(catalog, 2, OLD_PATH)
            self.version_id = catalog.begin_replacement(
                row["id"], object_key=PREFIX + OLD_PATH, provider_version_id="s3-v-old",
                size=32, storage_class="STANDARD", created_at="2026-07-21T10:01:00+00:00",
            )
            catalog.promote_replacement(
                row["id"], archive_version_id=self.version_id, plaintext_sha256=digest,
                verified_at="2026-07-21T10:02:00+00:00", local_generation=row["generation"],
            )
            catalog.mark_local_copy_missing(
                self.file_id, observed_at="2026-07-21T11:00:00+00:00"
            )
            catalog.observe_local_copy(
                vault_id=2, path=NEW_PATH, file_type="regular", size=32,
                mtime_ns=1_000, observed_at="2026-07-21T11:00:00+00:00",
            )
            fingerprint(catalog, 2, NEW_PATH, digest)
            catalog.confirm_file_rename(
                vault_file_id=self.file_id, new_path=NEW_PATH,
                changed_at="2026-07-21T11:00:00+00:00", vault_id=2,
            )
            self.assertEqual(catalog_probe.get_file_by_path(catalog, 2, NEW_PATH)["id"], self.file_id)
            self.assertIsNone(catalog_probe.get_file_by_path(catalog, 2, OLD_PATH))

    def _scan(self, versions: list[dict]) -> tuple[int, dict]:
        report: dict = {}
        client = SimpleNamespace(
            get_paginator=lambda _name: SimpleNamespace(
                paginate=lambda **_kwargs: iter([{"Versions": versions, "DeleteMarkers": []}])
            )
        )
        with (
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client", return_value=client),
        ):
            count = scan_cloud(
                {"id": 2, "uuid": VAULT_UUID, "s3_bucket": "bucket", "s3_key_layout": "vault_prefix"},
                "2026-07-21T12:00:00+00:00",
                report=report,
            )
        return count, report

    def _assert_identity_kept(self, connection: psycopg.Connection) -> None:
        catalog = ArchiveCatalog(connection)
        self.assertIsNone(
            catalog_probe.get_file_by_path(catalog, 2, OLD_PATH),
            "cloud rediscovery must not mint an orphan Vault File at the old path",
        )
        current = catalog_probe.get_file_by_path(catalog, 2, NEW_PATH)
        self.assertEqual(current["id"], self.file_id)
        self.assertEqual(current["current_version"]["id"], self.version_id)
        self.assertIsNone(current["replacing_version"])
        files = connection.execute(
            "SELECT count(*) AS n FROM files WHERE vault_id=2"
        ).fetchone()["n"]
        self.assertEqual(files, 1)

    def test_bug_008_old_key_rediscovery_keeps_renamed_identity(self) -> None:
        """[BUG-008][Req: REQ-020] The old key is the same version, same file."""
        count, report = self._scan([_item(OLD_PATH, "s3-v-old")])

        self.assertEqual(count, 1)
        self.assertEqual(report["unknown_versions"], 0)
        with self.database.connect() as connection:
            self._assert_identity_kept(connection)
            row = connection.execute(
                "SELECT file_id::text AS file_id, availability FROM archive_versions"
            ).fetchone()
        self.assertEqual(row, {"file_id": self.file_id, "availability": "available"})

    def test_bug_008_new_version_at_historical_path_is_never_attributed(self) -> None:
        """[BUG-008][Req: REQ-020] Path History never elects an owner."""
        count, report = self._scan(
            [_item(OLD_PATH, "s3-v-old"), _item(OLD_PATH, "s3-v-new")]
        )

        self.assertEqual(count, 1)
        self.assertEqual(report["unknown_versions"], 1)
        with self.database.connect() as connection:
            self._assert_identity_kept(connection)
            versions = connection.execute(
                "SELECT provider_version_id FROM archive_versions"
            ).fetchall()
            history = connection.execute(
                "SELECT file_id::text AS file_id, path FROM file_path_history"
            ).fetchall()
        self.assertEqual([row["provider_version_id"] for row in versions], ["s3-v-old"])
        self.assertEqual(history, [{"file_id": self.file_id, "path": OLD_PATH}])


if __name__ == "__main__":
    unittest.main()
