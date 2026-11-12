"""Populated metadata-only discovery contracts, without invented proof or I/O."""
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


def assert_obligation_discovery_migration(test, run, connect):
    from app.services.upload_remediation_discovery import obligation_query
    result = run("0072_upload_lineage_indexes")
    test.assertEqual(result.returncode, 0, result.stderr)
    stamp = "2026-11-06T00:00:00+00:00"
    with connect() as c:
        c.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
        c.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'docs','Docs','/nonexistent/no-io','bucket','docs')")
        catalog = ArchiveCatalog(c)
        versions = []
        for path in ("cancelled-send", "unverified", "cleanup", "ordinary"):
            av = catalog.record_archive_version(vault_id=2,path=path,object_key='docs/'+path,
                provider_version_id=path,size=1,storage_class='STANDARD',etag='not-proof',
                uploaded_at=stamp,observed_at=stamp,scan_id='seed',origin='upload')
            vf = c.execute("SELECT vault_file_id FROM archive_versions WHERE id=%s", (av,)).fetchone()['vault_file_id']
            c.execute("UPDATE archive_versions SET upload_snapshot_json='independent-fixture-baseline' WHERE id=%s", (av,))
            if path in {"cleanup", "ordinary"}:
                c.execute("UPDATE archive_versions SET integrity='verified',plaintext_sha256=%s,verified_at=%s WHERE id=%s", ('a'*64, stamp, av))
            if path == "cancelled-send":
                job = c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at)
                    VALUES (2,%s,%s,'upload','cancelled',1,%s,%s) RETURNING id""", (vf,path,stamp,stamp)).fetchone()['id']
                c.execute("""INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,
                    target_json,snapshot_json,requested,inventory_json,send_started,created_at)
                    VALUES ('cancelled-send','token',2,%s,%s,'{}','independent-fixture-baseline','read_back','{}',TRUE,%s)""", (vf,job,stamp))
            if path == 'cleanup':
                c.execute("INSERT INTO snapshot_reconciliation_blocks VALUES (%s,'inventory_changed',%s)", (vf,stamp))
            versions.append(av)
        # Include the exact noise this migration removes, as well as a lost-capture
        # source. Neither queue contents nor absence are discovery authority.
        c.execute("""INSERT INTO upload_remediation_facts
            SELECT 'version:'||id,vault_id,vault_file_id,NULL,id,discovered_at,discovered_at
            FROM archive_versions WHERE id=%s""", (versions[-1],))
        c.execute("UPDATE upload_remediation_sweep SET last_vault_id=42,last_tick_at=%s WHERE name='facts'", (stamp,))
        tables = ('vaults','vault_files','archive_versions','jobs','upload_intents','snapshot_reconciliation_blocks')
        before = {table:c.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() for table in tables}
    result = run('0073_obligation_discovery')
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as c:
        test.assertIsNone(c.execute("SELECT to_regclass('upload_remediation_facts') absent").fetchone()['absent'])
        test.assertEqual(c.execute("SELECT last_vault_id,last_tick_at FROM upload_remediation_sweep WHERE name='obligations'").fetchone(), {'last_vault_id':42, 'last_tick_at':stamp})
        test.assertEqual({table:c.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() for table in tables}, before)
        sql, params = obligation_query(vault_sql='2', stamp='2026-11-07T00:00:00+00:00')
        rows = c.execute(sql, params).fetchall()
        test.assertEqual({r['intent_id'] for r in rows if r['intent_id']}, {'cancelled-send'})
        test.assertEqual({r['archive_version_id'] for r in rows if r['archive_version_id']}, set(versions[1:3]))
        test.assertEqual(c.execute('SELECT count(*) n FROM upload_remediations').fetchone()['n'], 0, 'DDL grants no proof/admission')
        indexes = {r['indexname'] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public'").fetchall()}
        test.assertTrue({'upload_intents_open_send_idx','upload_intents_outstanding_file_idx',
            'archive_versions_unverified_idx','archive_versions_unverified_file_idx'}.issubset(indexes))
    for revision, command in (('0072_upload_lineage_indexes','downgrade'), ('0073_obligation_discovery','upgrade')):
        result = run(revision, command)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as c:
            test.assertEqual({table:c.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() for table in tables}, before)
            if command == 'downgrade':
                test.assertIsNotNone(c.execute("SELECT 1 FROM upload_remediation_facts WHERE intent_id='cancelled-send'").fetchone(), 'Cancelled uncertainty survives roundtrip')
