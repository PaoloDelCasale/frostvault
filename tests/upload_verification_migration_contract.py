"""Populated upgrade/downgrade/reupgrade contract, disposable PostgreSQL only."""
from app.catalog import ArchiveCatalog

NEW_FIELDS = {
    "jobs": {"upload_snapshot_json", "upload_verification_requested"},
    "archive_versions": {"upload_snapshot_json", "upload_verification_requested", "verification_method", "verification_fallback_reason", "verification_egress_bytes"},
    "vault_operation_policies": {"upload_verification"},
}


def without_upload_verification_fields(rows, table):
    """Project pre-0063 preservation tests onto the schema they own."""
    return [{k: v for k, v in row.items() if k not in NEW_FIELDS.get(table, set())} for row in rows]


def state(connection):
    return {table: [{k: v for k, v in row.items() if k not in NEW_FIELDS.get(table, set())}
                    for row in connection.execute(f"SELECT * FROM {table} ORDER BY id" if table != "vault_operation_policies" else f"SELECT * FROM {table} ORDER BY vault_id").fetchall()]
            for table in ("vaults", "jobs", "archive_versions", "vault_operation_policies")}


def assert_upload_verification_migration(test, run, connect):
    result = run("0062_cloud_preflight_gate")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
        connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'test','Test','/source','bucket','vaults/test')")
        connection.execute("INSERT INTO vault_operation_policies(vault_id,auto_upload,stability_seconds) VALUES (2,true,300)")
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(vault_id=2, path="a", file_type="regular", size=1, mtime_ns=100, observed_at="2026-11-06")
        ids = []
        for integrity in ("verified", "unverified", "mismatch"):
            version = catalog.record_archive_version(vault_id=2,path="a",object_key="vaults/test/a",provider_version_id=integrity,size=1,storage_class="STANDARD",etag="etag",uploaded_at="2026-11-06",observed_at="2026-11-06",scan_id="seed",origin="upload")
            connection.execute("UPDATE archive_versions SET integrity=%s, plaintext_sha256=%s WHERE id=%s", (integrity, "a"*64 if integrity != "unverified" else None, version))
            ids.append(version)
        file_id = connection.execute("SELECT id FROM vault_files").fetchone()["id"]
        connection.execute("INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,requested_by,requested_at,updated_at,upload_plaintext_sha256) VALUES (2,%s,%s,'a','upload','failed',1,'2026-11-06','2026-11-06',%s)", (file_id,ids[1],"b"*64))
        before = state(connection)
    for target, command in (("0063_upload_verification", "upgrade"), ("0062_cloud_preflight_gate", "downgrade"), ("0063_upload_verification", "upgrade")):
        result = run(target, command)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as connection:
            test.assertEqual(state(connection), before)
            if command == "upgrade":
                for table, fields in NEW_FIELDS.items():
                    row = connection.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
                    test.assertTrue(fields <= row.keys())
                    test.assertTrue(all(row[field] is None for field in fields))
                # Existing Vault inherits without a trust rewrite. Downgrade also
                # preserves populated *new* method evidence's core digest/state.
                connection.execute("UPDATE vault_operation_policies SET upload_verification='read_back'")
                connection.execute("UPDATE archive_versions SET verification_method='s3_checksum',verification_egress_bytes=0 WHERE integrity='verified'")
                connection.execute("UPDATE jobs SET upload_verification_requested='read_back'")
            else:
                for table, fields in NEW_FIELDS.items():
                    row = connection.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
                    test.assertTrue(fields.isdisjoint(row.keys()))
