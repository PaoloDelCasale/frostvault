"""Current Snapshot cloud scan must not trust a catalog that is behind S3 (#408).

Seam: ``scan_cloud`` on a Current Snapshot Vault. A restored metadata backup
can name an Archive Version (A) whose S3 VersionId was already purged after a
newer verified upload (B). The scan must keep B, never delete it.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

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

from app.storage import scan_cloud
from tests.test_database import run_alembic

VAULT = {
    "id": 2,
    "s3_bucket": "bucket",
    "s3_prefix": "docs",

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
        # Direct scan_cloud calls have no registered scan generation. Other
        # modules can leave Vault 2 in the process-wide watcher runtime; that
        # belongs to another disposable DB and must not suppress this scan.
        runtime = patch("app.storage.runtime_status", {})
        runtime.start()
        self.addCleanup(runtime.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "catalog.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs')
                """
            )

    def _scan(self, s3: _ListingS3, scan_id: str) -> None:
        settings = SimpleNamespace(database_dsn=database_dsn(str(self.database_path)))
        with (
            patch("app.database.settings", settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client", return_value=s3),
        ):
            scan_cloud(dict(VAULT), scan_id)

    def _versions(self) -> dict[str, str]:
        with PostgresConnection(str(self.database_path)) as connection:
            rows = ArchiveCatalog(connection).list_versions(2, "report.txt")
        return {row["provider_version_id"]: row["availability"] for row in rows}

    def test_missing_keeper_is_retained_without_electing_newer_discovery(self) -> None:
        # The (restored) catalog knows only A as the recoverable version.
        self._scan(_ListingS3([_item("s3-a", 10)]), _scan_id())
        self._verify("s3-a")
        self.assertEqual(self._versions(), {"s3-a": "available"})

        # After the backup, B was uploaded and verified, and A was purged.
        s3 = _ListingS3([_item("s3-b", 12)])
        self._scan(s3, _scan_id())

        self.assertEqual(s3.deleted, [], "the only cloud copy must never be deleted")
        versions = self._versions()
        self.assertEqual(versions.get("s3-b"), "available")
        # Listing cannot declare A superseded or grant B trust. Reconnect must
        # independently read B before changing the explicit recovery reference.
        self.assertEqual(versions.get("s3-a"), "missing")
        with PostgresConnection(str(self.database_path)) as connection:
            rows = ArchiveCatalog(connection).list_versions(2, "report.txt")
            self.assertEqual([row["provider_version_id"] for row in rows if row["is_current"]], ["s3-a"])
            self.assertFalse(any(row["recoverable"] for row in rows))
            self.assertEqual(next(row for row in rows if row["provider_version_id"] == "s3-b")["integrity"], "unverified")
            incidents = connection.execute("SELECT COUNT(*) AS n FROM catalog_incidents WHERE status='open'").fetchone()
        self.assertGreater(incidents["n"], 0)

    def test_unverified_or_mismatched_legacy_keeper_never_authorizes_purge(self) -> None:
        for integrity in ("unverified", "mismatch"):
            with self.subTest(integrity=integrity):
                self._scan(_ListingS3([_item("s3-a", 12)]), _scan_id())
                with PostgresConnection(str(self.database_path)) as connection:
                    connection.execute("UPDATE archive_versions SET integrity=%s", (integrity,))
                s3 = _ListingS3([_item("s3-a", 12), _item("s3-b", 10)])
                self._scan(s3, _scan_id())
                self.assertEqual(s3.deleted, [])
                self.assertEqual(set(self._versions().values()), {"available"})
                with PostgresConnection(str(self.database_path)) as connection:
                    rows = connection.execute("SELECT integrity FROM archive_versions").fetchall()
                self.assertFalse(any(row["integrity"] == "verified" for row in rows))

    def test_verified_but_older_keeper_cannot_delete_newer_uncatalogued_content(self) -> None:
        self._scan(_ListingS3([_item("s3-a", 10)]), _scan_id())
        self._verify("s3-a")
        s3 = _ListingS3([_item("s3-a", 10), _item("s3-b", 12)])
        self._scan(s3, _scan_id())
        self.assertEqual(s3.deleted, [])

    def _verify(self, provider: str) -> None:
        with PostgresConnection(str(self.database_path)) as connection:
            row = connection.execute("SELECT id FROM archive_versions WHERE provider_version_id=%s", (provider,)).fetchone()
            ArchiveCatalog(connection).mark_version_verified(row["id"], plaintext_sha256="a" * 64, verified_at=_scan_id())

    def test_scan_extra_is_retained_without_claimed_exact_duplicate_proof(self) -> None:
        self._scan(_ListingS3([_item("s3-a", 12)]), _scan_id())
        self._verify("s3-a")
        s3 = _ListingS3([_item("s3-a", 12), _item("s3-b", 10)])
        self._scan(s3, _scan_id())
        self.assertEqual(s3.deleted, [])
        self.assertEqual(self._versions().get("s3-a"), "available")
        self.assertEqual(self._versions().get("s3-b"), "available")
        with PostgresConnection(str(self.database_path)) as connection:
            current = next(row for row in ArchiveCatalog(connection).list_versions(2, "report.txt") if row["is_current"])
            self.assertEqual(current["provider_version_id"], "s3-a", "Discovery cannot change the explicit Current Snapshot")
            self.assertIsNotNone(connection.execute("SELECT * FROM snapshot_reconciliation_blocks").fetchone())
            self.assertIsNone(connection.execute("SELECT * FROM snapshot_cleanup_proofs").fetchone())

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
        with PostgresConnection(str(self.database_path)) as connection:
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
