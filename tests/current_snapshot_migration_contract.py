"""Current baseline equivalent of the superseded populated #418 migration test.

Old catalogs are intentionally incompatible (ADR-0035). Keep the original safety
contract -- no object operations, no discarded cloud copies, no fabricated trust
-- against current initialization/re-entry rather than an obsolete roundtrip.
"""
from app.catalog import ArchiveCatalog


def _state(connection):
    return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("vault_files", "file_paths", "local_copies", "archive_versions", "delete_markers")}


def assert_current_snapshot_migration(test, run, connect):
    result = run("head")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix)
            VALUES (2,'snapshot','Snapshot','/unused-snapshot-contract','bucket','photos')""")
        catalog = ArchiveCatalog(connection)
        stamp = "2026-11-12T10:00:00+00:00"
        keeper = catalog.record_archive_version(vault_id=2, path="photo.jpg", object_key="photos/photo.jpg",
            provider_version_id="keeper", size=10, storage_class="STANDARD", etag="keeper",
            uploaded_at=stamp, observed_at=stamp, scan_id=stamp, origin="upload")
        catalog.mark_version_verified(keeper, plaintext_sha256="a" * 64, verified_at=stamp)
        for provider, integrity in (("observed-unverified", "unverified"), ("observed-mismatch", "mismatch")):
            version = catalog.record_archive_version(vault_id=2, path="photo.jpg", object_key="photos/photo.jpg",
                provider_version_id=provider, size=10, storage_class="STANDARD", etag=provider,
                uploaded_at=stamp, observed_at=stamp, scan_id=stamp)
            if integrity == "mismatch":
                catalog.mark_version_mismatch(version, plaintext_sha256="b" * 64, checked_at=stamp)
        before = _state(connection)
        test.assertEqual(len(before["archive_versions"]), 3)
    result = run("head")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        test.assertEqual(_state(connection), before)
        versions = ArchiveCatalog(connection).list_versions(2, "photo.jpg")
        test.assertEqual([row["id"] for row in versions if row["recoverable"]], [keeper])
        test.assertEqual({row["provider_version_id"]: row["integrity"] for row in versions},
                         {"keeper": "verified", "observed-unverified": "unverified", "observed-mismatch": "mismatch"})
        test.assertTrue(all(row["availability"] == "available" for row in versions))
        file_state = connection.execute("SELECT current_version_id,replacing_version_id FROM vault_files").fetchone()
        test.assertEqual(file_state, {"current_version_id": keeper, "replacing_version_id": None})
        test.assertNotIn("version_number", before["archive_versions"][0])
        test.assertNotIn("cloud_history_policy", connection.execute("SELECT * FROM vaults").fetchone())
