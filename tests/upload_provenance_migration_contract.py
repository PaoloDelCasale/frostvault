"""Populated metadata-only #464 roundtrip from merged 0063, disposable PG16."""
from unittest.mock import patch
import hashlib
from app.services.upload_snapshot import sha256_b64, DEFAULT_PART_SIZE

from tests.historical_catalog_fixture import HistoricalCatalogFixture as ArchiveCatalog
from app.services.upload_provenance import canonical
from app.services.upload_snapshot import UploadSnapshot

TABLES = ("upload_intents", "upload_intent_jobs", "upload_receipts", "snapshot_cleanup_proofs", "snapshot_cleanup_inventories", "snapshot_reconciliation_blocks")


def assert_upload_provenance_migration(test, run, connect):
    checksum = sha256_b64(b"a")
    snapshot = UploadSnapshot(1, hashlib.sha256(b"a").hexdigest(), "FULL_OBJECT", checksum, DEFAULT_PART_SIZE, (checksum,)).as_json()
    result = run("0063_upload_verification")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
        connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'test','Test','/not-readable','bucket','docs')")
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(vault_id=2,path="a",file_type="regular",size=1,mtime_ns=100,observed_at="2026-11-06")
        file_id = connection.execute("SELECT id FROM vault_files").fetchone()["id"]
        versions = []
        for state in ("verified", "unverified", "mismatch"):
            av = catalog.record_archive_version(vault_id=2,path="a",object_key="docs/a",provider_version_id=state,size=1,storage_class="STANDARD",etag="not-proof",uploaded_at="2026-11-06",observed_at="2026-11-06",scan_id="seed")
            connection.execute("UPDATE archive_versions SET integrity=%s,plaintext_sha256=%s WHERE id=%s", (state,"a"*64,av))
            versions.append(av)
        ids = []
        for status, av in (("failed", None), ("failed", versions[1]), ("failed", versions[0]), ("cancelled", None), ("uploading", None)):
            ids.append(connection.execute("INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,requested_by,requested_at,updated_at,upload_plaintext_sha256) VALUES (2,%s,%s,'a','upload',%s,1,'2026-11-06','2026-11-06',%s) RETURNING id", (file_id,av,status,"b"*64)).fetchone()["id"])
        before = {table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in ("vaults", "jobs", "archive_versions")}
    for target, operation in (("0064_upload_provenance", "upgrade"), ("0063_upload_verification", "downgrade"), ("0064_upload_provenance", "upgrade")):
        # Subprocess migrations cannot invoke these adapters; the nonexistent
        # source plus no provider configuration additionally expose accidental I/O.
        with patch("boto3.client", side_effect=AssertionError("No provider I/O during DDL")):
            result = run(target, operation)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as connection:
            actual = {table: [{k:v for k,v in row.items() if k != "upload_reconcile_only"} for row in connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()] for table in before}
            test.assertEqual(actual, before)
            names = {r["table_name"] for r in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
            test.assertEqual(set(TABLES) <= names, operation == "upgrade")
    with connect() as connection:
        connection.execute("INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,target_json,snapshot_json,requested,inventory_json,send_started,created_at) VALUES ('ambiguous','distinct-cloud-token',2,%s,%s,%s,%s,'read_back',%s,TRUE,'2026-11-06')", (file_id, ids[3], canonical({"key":"docs/a","bucket":"bucket"}), snapshot, canonical({"versions":[],"markers":[]})))
        connection.execute("INSERT INTO upload_intent_jobs(job_id,intent_id) VALUES (%s,'ambiguous')", (ids[3],))
        connection.execute("INSERT INTO upload_receipts(intent_id,receipt_hash,response_json,received_at) VALUES ('ambiguous','hash','{\"VersionId\":\"actual-receipt\"}','2026-11-06')")
    refused = run("0063_upload_verification", "downgrade")
    test.assertNotEqual(refused.returncode, 0)
    with connect() as connection:
        test.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0064_upload_provenance")
        test.assertIsNotNone(connection.execute("SELECT * FROM upload_receipts WHERE intent_id='ambiguous'").fetchone())
        names = {r["table_name"] for r in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
        test.assertTrue(set(TABLES) <= names, "Downgrade must refuse before any DDL")
    # Each remaining guard is isolated: no unresolved send can accidentally
    # make these pass. This data belongs only to the disposable PG16 fixture.
    with connect() as connection:
        connection.execute("DELETE FROM upload_receipts")
        connection.execute("DELETE FROM upload_intent_jobs")
        connection.execute("DELETE FROM upload_intents")
    cases = (
        ("snapshot_cleanup_proofs", """INSERT INTO snapshot_cleanup_proofs(vault_file_id,bucket,provider,object_key,version_id,keeper_id,sha256,size,method,proved_at)
            VALUES (%s,'bucket','disposable','docs/a','retained-version',%s,%s,1,'exact_get_duplicate','2026-11-06')""", (file_id, versions[0], "a" * 64)),
        ("snapshot_cleanup_inventories", """INSERT INTO snapshot_cleanup_inventories(job_id,keeper_id,target_json,inventory_json,created_at)
            VALUES (%s,%s,%s,%s,'2026-11-06')""", (ids[2], versions[0], canonical({"bucket":"bucket","key":"docs/a"}), canonical({"docs/a":{"versions":["verified"],"markers":[]}}))),
        ("snapshot_reconciliation_blocks", "INSERT INTO snapshot_reconciliation_blocks(vault_file_id,reason,updated_at) VALUES (%s,'proof_unavailable','2026-11-06')", (file_id,)),
    )
    for table, sql, params in cases:
        with test.subTest(downgrade_guard=table):
            with connect() as connection:
                connection.execute(sql, params)
                retained = connection.execute(f"SELECT * FROM {table}").fetchall()
            refused = run("0063_upload_verification", "downgrade")
            test.assertNotEqual(refused.returncode, 0)
            with connect() as connection:
                test.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0064_upload_provenance")
                names = {r["table_name"] for r in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
                test.assertTrue(set(TABLES) <= names, "Each isolated guard must refuse before any DDL")
                test.assertEqual(connection.execute(f"SELECT * FROM {table}").fetchall(), retained)
                if table == "snapshot_cleanup_inventories":
                    # A completed batch is history, unlike pending authority.
                    connection.execute("UPDATE snapshot_cleanup_inventories SET completed_at='2026-11-06'")
                else:
                    connection.execute(f"DELETE FROM {table}")
    result = run("0063_upload_verification", "downgrade")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        test.assertEqual({table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in before}, before)
    result = run("0064_upload_provenance", "upgrade")
    test.assertEqual(result.returncode, 0, result.stderr)
