"""Populated 0064 -> 0065 metadata-only discovery; pending downgrade is closed."""
from app.catalog import ArchiveCatalog


def assert_upload_remediation_migration(test, run, connect):
    result = run("0064_upload_provenance")
    test.assertEqual(result.returncode,0,result.stderr)
    stamp = "2026-11-06T00:00:00+00:00"
    with connect() as c:
        c.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
        c.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'docs','Docs','/nonexistent/no-filesystem-ddl','bucket','docs')")
        catalog = ArchiveCatalog(c)
        file_id = catalog.observe_local_copy(vault_id=2,path="a",file_type="regular",size=1,mtime_ns=100,observed_at=stamp)
        av = catalog.record_archive_version(vault_id=2,path="a",object_key="docs/a",provider_version_id="exact-A",size=1,storage_class="STANDARD",etag="not-proof",uploaded_at=stamp,observed_at=stamp,scan_id="seed")
        c.execute("UPDATE archive_versions SET integrity='verified',plaintext_sha256=%s,verified_at=%s WHERE id=%s",("a"*64,stamp,av))
        originals = []
        for status,target in (("failed",av),("failed",None),("cancelled",av)):
            originals.append(c.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,
                requested_by,requested_at,updated_at,upload_plaintext_sha256)
                VALUES (2,%s,%s,'a','upload',%s,1,%s,%s,%s) RETURNING id""",(file_id,target,status,stamp,stamp,"a"*64)).fetchone()["id"])
        before = {table:c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in ("vaults","jobs","archive_versions")}
    upgraded = run("0065_upload_remediation")
    test.assertEqual(upgraded.returncode,0,upgraded.stderr)
    with connect() as c:
        test.assertEqual({table:c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in before},before)
        test.assertEqual([r["job_id"] for r in c.execute("SELECT job_id FROM upload_remediation_discovery ORDER BY job_id").fetchall()],[originals[0]])
        test.assertEqual(c.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"],0,"DDL grants no proof or admission")
    refused = run("0064_upload_provenance","downgrade")
    test.assertNotEqual(refused.returncode,0)
    test.assertIn("Resolve retained upload remediation obligations",refused.stderr)
    with connect() as c:
        test.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"],"0065_upload_remediation")
        test.assertIsNotNone(c.execute("SELECT * FROM upload_remediation_discovery").fetchone(),"Refusal precedes DDL")
        # Disposable fixture only: remove the inbox to exercise a representable roundtrip.
        c.execute("DELETE FROM upload_remediation_discovery")
    for revision,command in (("0064_upload_provenance","downgrade"),("0065_upload_remediation","upgrade")):
        result = run(revision,command)
        test.assertEqual(result.returncode,0,result.stderr)
        with connect() as c:
            test.assertEqual({table:c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in before},before)
