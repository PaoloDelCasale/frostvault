"""Current Snapshot cloud scan must not trust a catalog that is behind S3 (#408).

Seam: ``scan_cloud`` on a Current Snapshot Vault. A restored metadata backup
can name an Archive Version (A) whose S3 VersionId was already purged after a
newer verified upload (B). The scan must keep B, never delete it.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.storage import scan_cloud
from tests.test_database import run_alembic

VAULT = {
    "id": 2,
    "s3_bucket": "bucket",
    "s3_prefix": "docs",
    "rclone_remote": "remote",
}


def _item(version_id: str, hour: int) -> dict:
    return {
        "Key": "docs/report.txt",
        "VersionId": version_id,
        "Size": 4,
        "StorageClass": "STANDARD",
        "ETag": f'"{version_id}"',
        "LastModified": datetime(2026, 7, 21, hour, 0, tzinfo=timezone.utc),
    }


class _ListingS3:
    def __init__(self, versions: list[dict]) -> None:
        self.versions = versions
        self.deleted: list[tuple[str, str]] = []

    def get_paginator(self, _name: str):
        return SimpleNamespace(
            paginate=lambda **_kwargs: iter(
                [{"Versions": list(self.versions), "DeleteMarkers": []}]
            )
        )

    def get_object_tagging(self, **_kwargs):
        return {"TagSet": []}

    def list_object_versions(self, **kwargs):
        prefix = str(kwargs.get("Prefix") or "")
        return {
            "Versions": [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for item in self.versions
                if item["Key"].startswith(prefix)
            ],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }

    def delete_object(self, **kwargs):
        self.deleted.append((str(kwargs["Key"]), str(kwargs["VersionId"])))
        self.versions = [
            item
            for item in self.versions
            if item["VersionId"] != str(kwargs["VersionId"])
        ]
        return {"VersionId": kwargs["VersionId"]}


def _scan_id() -> str:
    """Scan ids are scan start times; rows discovered later are left alone."""
    return (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()


class SnapshotScanStaleCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "catalog.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash)
                VALUES (1, 'owner', 'Owner', 'hash')
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote
                ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                """
            )

    def _scan(self, s3: _ListingS3, scan_id: str) -> None:
        settings = SimpleNamespace(db_backend="sqlite", sqlite_path=str(self.database_path))
        with (
            patch("app.database.settings", settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.rclone_remote_is_crypt", return_value=False),
            patch("app.storage.s3_client", return_value=s3),
        ):
            scan_cloud(dict(VAULT), scan_id)

    def _versions(self) -> dict[str, str]:
        with SQLiteConnection(str(self.database_path)) as connection:
            rows = ArchiveCatalog(connection).list_versions(2, "report.txt")
        return {row["provider_version_id"]: row["availability"] for row in rows}

    def test_newer_version_survives_when_catalog_keeper_is_gone(self) -> None:
        # The (restored) catalog knows only A as the recoverable version.
        self._scan(_ListingS3([_item("s3-a", 10)]), _scan_id())
        self.assertEqual(self._versions(), {"s3-a": "available"})

        # After the backup, B was uploaded and verified, and A was purged.
        s3 = _ListingS3([_item("s3-b", 12)])
        self._scan(s3, _scan_id())

        self.assertEqual(s3.deleted, [], "the only cloud copy must never be deleted")
        versions = self._versions()
        self.assertEqual(versions.get("s3-b"), "available")
        # A is superseded, not a loss: no false missing-copy incident.
        self.assertEqual(versions.get("s3-a"), "purged")
        with SQLiteConnection(str(self.database_path)) as connection:
            incidents = connection.execute(
                "SELECT COUNT(*) AS n FROM catalog_incidents"
            ).fetchone()
        self.assertEqual(incidents["n"], 0)

    def test_unverified_or_mismatched_legacy_keeper_never_authorizes_purge(self) -> None:
        for integrity in ("unverified", "mismatch"):
            with self.subTest(integrity=integrity):
                self._scan(_ListingS3([_item("s3-a", 12)]), _scan_id())
                with SQLiteConnection(str(self.database_path)) as connection:
                    connection.execute("UPDATE archive_versions SET integrity=%s", (integrity,))
                s3 = _ListingS3([_item("s3-a", 12), _item("s3-b", 10)])
                self._scan(s3, _scan_id())
                self.assertEqual(s3.deleted, [])
                self.assertEqual(set(self._versions().values()), {"available"})
                with SQLiteConnection(str(self.database_path)) as connection:
                    rows = connection.execute("SELECT integrity FROM archive_versions").fetchall()
                self.assertFalse(any(row["integrity"] == "verified" for row in rows))

    def test_verified_but_older_keeper_cannot_delete_newer_uncatalogued_content(self) -> None:
        self._scan(_ListingS3([_item("s3-a", 10)]), _scan_id())
        self._verify("s3-a")
        s3 = _ListingS3([_item("s3-a", 10), _item("s3-b", 12)])
        self._scan(s3, _scan_id())
        self.assertEqual(s3.deleted, [])

    def _verify(self, provider: str) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute("UPDATE archive_versions SET integrity='verified', plaintext_sha256=%s WHERE provider_version_id=%s", ("a" * 64, provider))

    def test_extra_is_purged_only_with_verified_newest_keeper_present(self) -> None:
        self._scan(_ListingS3([_item("s3-a", 12)]), _scan_id())
        self._verify("s3-a")
        s3 = _ListingS3([_item("s3-a", 12), _item("s3-b", 10)])
        self._scan(s3, _scan_id())
        self.assertEqual(s3.deleted, [("docs/report.txt", "s3-b")])
        self.assertEqual(self._versions().get("s3-a"), "available")

    def test_equal_timestamp_ambiguity_preserves_both_copies(self) -> None:
        self._scan(_ListingS3([_item("s3-a", 12)]), _scan_id())
        self._verify("s3-a")
        s3 = _ListingS3([_item("s3-a", 12), _item("s3-b", 12)])
        self._scan(s3, _scan_id())
        self.assertEqual(s3.deleted, [])
        self.assertEqual(self._versions(), {"s3-a": "available", "s3-b": "available"})

    def test_active_job_prevents_scan_cleanup(self) -> None:
        self._scan(_ListingS3([_item("s3-a", 12)]), _scan_id())
        self._verify("s3-a")
        with SQLiteConnection(str(self.database_path)) as connection:
            file_id = connection.execute("SELECT vault_file_id FROM archive_versions").fetchone()["vault_file_id"]
            connection.execute("INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_at, updated_at) VALUES (2, %s, 'report.txt', 'upload', 'queued', '2026-11-03', '2026-11-03')", (file_id,))
        s3 = _ListingS3([_item("s3-a", 12), _item("s3-b", 10)])
        self._scan(s3, _scan_id())
        self.assertEqual(s3.deleted, [])
        self.assertEqual(set(self._versions().values()), {"available"})

    def test_failed_or_changed_keeper_listing_never_deletes_extras(self) -> None:
        self._scan(_ListingS3([_item("s3-a", 12)]), _scan_id())
        self._verify("s3-a")
        for response in ({"Versions": [], "IsTruncated": False}, RuntimeError("listing failed")):
            s3 = _ListingS3([_item("s3-a", 12), _item("s3-b", 10)])
            with patch.object(s3, "list_object_versions", **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response})):
                self._scan(s3, _scan_id())
            self.assertEqual(s3.deleted, [])
            self.assertEqual(self._versions().get("s3-b"), "available", "A failed cleanup is not evidence that a discovered extra is missing")


if __name__ == "__main__":
    unittest.main()
