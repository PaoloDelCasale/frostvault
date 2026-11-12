"""#459 remaining acceptance through real admission/claim/worker on CI PG16.

Expected evidence is derived independently from fixture bytes/local snapshots;
provider observations never manufacture ownership or expected content.
"""
import hashlib
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import notifications, upload_remediation as remediation
from app.services import upload_remediation_discovery as discovery
from app.services.upload_snapshot import prepare_upload_snapshot
from app.services.vault_relocation import enroll_vault_root_identity
from tests import test_upload_remediation as previous

A, B = previous.fixtures.A, previous.fixtures.B


class UploadRemediationFrontierTests(unittest.TestCase):
    setUp = previous.UploadRemediationTests.setUp
    queue = previous.UploadRemediationTests.queue
    run_job = previous.UploadRemediationTests.run_job
    row = previous.UploadRemediationTests.row
    db = previous.UploadRemediationTests.db
    admit = previous.UploadRemediationTests.admit
    fail_after_ack = previous.UploadRemediationTests.fail_after_ack
    assert_keeper = previous.UploadRemediationTests.assert_keeper
    http_client = previous.UploadRemediationTests.http_client
    scan = previous.fixtures.UploadProvenanceWorkerTests.scan

    def seed_owned(self, *, vault_id=2, path="report.txt", snapshot=True):
        root = self.source if vault_id == 2 else self.root / f"vault-{vault_id}"
        root.mkdir(exist_ok=True)
        source = root / path
        source.write_bytes(A)
        expected, _ = prepare_upload_snapshot(source, lambda: None)
        stamp = storage.now_iso()
        prefix = "docs" if vault_id == 2 else f"vault-{vault_id}"
        provider_id = f"owned-{uuid.uuid4().hex}"
        self.provider.add(f"{prefix}/{path}", A, version=provider_id)
        with self.db() as connection:
            if vault_id != 2:
                connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix)
                    VALUES (%s,%s,%s,%s,'bucket',%s) ON CONFLICT DO NOTHING""",
                    (vault_id, prefix, prefix, str(root), prefix))
                enroll_vault_root_identity(connection, vault_id, str(root))
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(vault_id=vault_id,path=path,file_type="regular",
                size=len(A),mtime_ns=source.stat().st_mtime_ns,observed_at=stamp)
            av = catalog.record_archive_version(vault_id=vault_id,vault_file_id=file_id,path=path,
                object_key=f"{prefix}/{path}",provider_version_id=provider_id,size=len(A),
                storage_class="STANDARD",etag="not-proof",uploaded_at=stamp,observed_at=stamp,
                scan_id=stamp,origin="upload")
            if snapshot:
                catalog.set_version_upload_snapshot(av,snapshot_json=expected.as_json(),requested="s3_checksum")
        return av, file_id

    def completion_gap(self):
        original = self.queue()
        real = storage._set_job_in_transaction
        def before_completion(connection, job_id, status, *args, **kwargs):
            if status == "completed":
                raise RuntimeError("injected completion boundary")
            return real(connection, job_id, status, *args, **kwargs)
        with patch("app.storage._set_job_in_transaction", side_effect=before_completion):
            self.run_job(original)
        self.assertEqual(self.row(original)["status"], "failed")
        with self.db() as connection:
            self.assertTrue(connection.execute("SELECT settled FROM upload_intents").fetchone()["settled"])
            self.assertIsNotNone(connection.execute("SELECT completed_at FROM snapshot_cleanup_inventories").fetchone()["completed_at"])
        return original

    def test_empty_sweep_table_recovers_all_cursors_and_discovers_durable_version(self):
        av, _ = self.seed_owned()
        with self.db() as connection:
            connection.execute("DELETE FROM upload_remediation_sweep")
            self.assertIsNone(connection.execute("SELECT to_regclass('upload_remediation_facts') absent").fetchone()["absent"])
        with patch("app.storage.s3_client", side_effect=AssertionError("Sweep must remain DB-only")):
            self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            cursors = connection.execute("SELECT name,last_tick_at FROM upload_remediation_sweep ORDER BY name").fetchall()
            self.assertEqual([r["name"] for r in cursors], ["admission", "discovery", "obligations"])
            self.assertTrue(all(r["last_tick_at"] for r in cursors))
            self.assertEqual(connection.execute("SELECT integrity FROM archive_versions WHERE id=%s", (av,)).fetchone()["integrity"], "unverified", "Discovery is not proof")
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
        self.run_job(resumed)
        self.assertEqual(self.assert_keeper(resumed)["id"], av)
        self.assertEqual(self.provider.puts, 0)

    def test_missing_and_stale_sweep_ticks_are_visible_and_recovery_resolves_problem(self):
        from app.services import metrics
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(seconds=remediation.SWEEP_STALE_SECONDS + 1)).isoformat()
        with self.db() as connection:
            connection.execute("UPDATE upload_remediation_sweep SET last_tick_at=%s", (stale,))
        self.assertTrue(remediation.sweep_health(self.db)["stale"])
        remediation.observe(self.db)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE event='upload_remediation_stale'").fetchone()["problem_status"], "open")
            connection.execute("DELETE FROM upload_remediation_sweep WHERE name='obligations'")
        self.assertTrue(remediation.sweep_health(self.db)["missing_ticks"])
        remediation.observe(self.db)
        self.assertIn("upload_remediation_last_tick_unixtime 0", metrics.render_prometheus())
        remediation.sweep(self.db)
        remediation.observe(self.db)
        self.assertFalse(remediation.sweep_health(self.db)["stale"])
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE event='upload_remediation_stale'").fetchone()["problem_status"], "resolved")
        self.assertIn("upload_remediation_sweep_stale 0", metrics.render_prometheus())

    def test_locked_sweep_cursor_is_not_reset_or_confused_with_a_missing_cursor(self):
        av, _ = self.seed_owned()
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with self.db() as connection:
            connection.execute("UPDATE upload_remediation_sweep SET last_vault_id=99,last_tick_at=%s WHERE name='obligations'", (stale,))
        with self.db() as held:
            held.execute("SELECT name FROM upload_remediation_sweep WHERE name='obligations' FOR UPDATE")
            self.assertEqual(remediation.sweep(self.db), 0)
            row = held.execute("SELECT last_vault_id,last_tick_at FROM upload_remediation_sweep WHERE name='obligations'").fetchone()
            self.assertEqual(row, {"last_vault_id": 99, "last_tick_at": stale})
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT archive_version_id FROM upload_remediations").fetchone()["archive_version_id"], av)
        self.assertEqual(self.provider.puts, 0)

    def test_owned_unverified_without_any_failed_original_is_admitted_without_put(self):
        av, _ = self.seed_owned()
        with patch("app.storage.s3_client", side_effect=AssertionError("Discovery must be DB-only")):
            self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_originals").fetchone()["n"], 0)
        self.run_job(row["current_job_id"])
        self.assertEqual(self.assert_keeper(row["current_job_id"])["id"], av)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_legacy_unverified_with_completed_independent_digest_uses_exact_readback(self):
        av, file_id = self.seed_owned(snapshot=False)
        with self.db() as connection:
            original = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,
                action,status,requested_at,updated_at,upload_plaintext_sha256,upload_verification_requested)
                VALUES (2,%s,%s,'report.txt','upload','completed',%s,%s,%s,'read_back') RETURNING id""",
                (file_id,av,storage.now_iso(),storage.now_iso(),hashlib.sha256(A).hexdigest())).fetchone()["id"]
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
        self.run_job(resumed)
        self.assertEqual(self.assert_keeper(resumed)["verification_method"], "read_back")
        self.assertEqual(self.row(original)["status"], "completed")
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,1,0))

    def test_open_send_without_failed_original_resumes_without_second_put(self):
        original = self.fail_after_ack()
        with self.db() as connection:
            # Populated legacy fixture: materialized intent, terminal original,
            # no failed-origin discovery. Do not change provider facts/evidence.
            connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (original,))
            connection.execute("DELETE FROM upload_remediation_discovery")
            connection.execute("UPDATE upload_intents SET created_at=%s", ((datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat(),))
        before = self.row(original)
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_originals").fetchone()["n"], 0)
        self.run_job(resumed)
        self.assert_keeper(resumed)
        for key in ("status","updated_at","message","archive_version_id"):
            self.assertEqual(self.row(original)[key], before[key])
        self.assertEqual(self.provider.puts, 1)

    def test_unknown_unverified_discovery_never_creates_expected_evidence_or_io(self):
        self.provider.add("docs/report.txt",B,version="unknown-B")
        self.scan()
        before = dict(self.provider.objects)
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"], 0)
            self.assertEqual(connection.execute("SELECT integrity FROM archive_versions WHERE provider_version_id='unknown-B'").fetchone()["integrity"], "unverified")
        self.assertEqual(self.provider.objects, before)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_scan_only_cleanup_of_verified_success_has_its_own_automatic_episode(self):
        original = self.queue(); self.run_job(original)
        keeper = self.assert_keeper(original)
        before = self.row(original)
        self.provider.add("docs/report.txt",A,version="scan-duplicate")
        self.scan()
        self.assertEqual(self.provider.deletes, 0)
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_originals").fetchone()["n"], 0)
        self.run_job(row["current_job_id"])
        self.assertEqual(self.assert_keeper(row["current_job_id"])["id"], keeper["id"])
        self.assertEqual(self.row(original), before)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,1))

    def test_later_manual_cleanup_keeps_previous_resolution_and_late_notifications(self):
        original = self.fail_after_ack()
        first = self.admit(); self.run_job(first); self.assert_keeper(first)
        with self.db() as connection:
            history = connection.execute("SELECT * FROM upload_remediations").fetchone()
        self.provider.add("docs/report.txt",A,version="later-duplicate")
        self.scan()
        second = self.admit()  # Reproduces the former AV unique violation.
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT * FROM upload_remediations WHERE id=%s", (history["id"],)).fetchone(), history)
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"], 2)
            connection.execute("DELETE FROM notification_deliveries")
            connection.execute("DELETE FROM notifications")
            notifications.enqueue_job_terminal_notification(connection,job_id=original)
            late = connection.execute("SELECT problem_status,read_at FROM notifications WHERE job_id=%s", (original,)).fetchone()
            self.assertEqual(late["problem_status"], "resolved")
            self.assertIsNotNone(late["read_at"])
        self.run_job(second); self.assert_keeper(second)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,1))

    def test_new_cleanup_blocks_survive_equal_or_older_timestamps_of_resolved_episodes(self):
        self.fail_after_ack()
        first = self.admit(); self.run_job(first); self.assert_keeper(first)
        for index in range(2):
            self.provider.add('docs/report.txt', A, version=f'clock-skew-duplicate-{index}')
            self.scan()
            with self.db() as connection:
                connection.execute("UPDATE snapshot_reconciliation_blocks SET updated_at='2020-01-01T00:00:00+00:00'")
                history = connection.execute("SELECT id,resolved_at FROM upload_remediations WHERE state='resolved' ORDER BY id").fetchall()
            self.assertEqual(remediation.sweep(self.db), 1)
            with self.db() as connection:
                resumed = connection.execute("SELECT current_job_id FROM upload_remediations WHERE state='queued'").fetchone()['current_job_id']
                self.assertEqual(connection.execute("SELECT id,resolved_at FROM upload_remediations WHERE state='resolved' ORDER BY id").fetchall(), history)
            self.run_job(resumed); self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.provider.deletes, 2)

    def test_intent_and_verified_reuse_originals_deduplicate_exact_av(self):
        original = self.completion_gap()
        with self.db() as connection:
            av = connection.execute("SELECT * FROM archive_versions").fetchone()
            other = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,
                action,status,requested_at,updated_at,upload_plaintext_sha256,upload_snapshot_json)
                VALUES (2,%s,%s,'report.txt','upload','failed',%s,%s,%s,%s) RETURNING id""",
                (self.file_id,av["id"],storage.now_iso(),storage.now_iso(),av["plaintext_sha256"],av["upload_snapshot_json"])).fetchone()["id"]
            remediation.capture_failure(connection,other,storage.now_iso())
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediations").fetchone()["n"], 1)
            self.assertEqual({r["original_job_id"] for r in connection.execute("SELECT original_job_id FROM upload_remediation_originals").fetchall()}, {original,other})
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
        self.run_job(resumed); self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)

    def test_wrong_original_proof_is_durably_rejected_not_linked_to_resolved_a(self):
        self.fail_after_ack()
        first = self.admit(); self.run_job(first); keeper = self.assert_keeper(first)
        with self.db() as connection:
            wrong = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,
                action,status,requested_at,updated_at,upload_plaintext_sha256)
                VALUES (2,%s,%s,'report.txt','upload','failed',%s,%s,%s) RETURNING id""",
                (self.file_id,keeper["id"],storage.now_iso(),storage.now_iso(),hashlib.sha256(B).hexdigest())).fetchone()["id"]
            remediation.capture_failure(connection,wrong,storage.now_iso())
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT rejected_reason FROM upload_remediation_discovery WHERE job_id=%s", (wrong,)).fetchone()["rejected_reason"], "proof_mismatch")
            self.assertIsNone(connection.execute("SELECT 1 FROM upload_remediation_originals WHERE original_job_id=%s", (wrong,)).fetchone())
            projected = remediation.project_jobs(connection,[self.row(wrong)])[0]
            self.assertIsNone(projected["remediation"])
            self.assertEqual(projected["remediation_rejection"], "proof_mismatch")
        self.assertEqual(self.provider.puts, 1)

    def test_original_exhausted_budget_is_not_reset_by_first_discovery(self):
        original = self.fail_after_ack()
        with self.db() as connection:
            connection.execute("UPDATE jobs SET retry_count=8 WHERE id=%s", (original,))
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual((row["state"],row["reason"],row["retry_count"]), ("blocked","retry_budget_exhausted",8))
        resumed = self.admit()
        self.assertEqual(self.row(resumed)["retry_count"], 8)
        self.run_job(resumed); self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)

    def test_round_robin_discovers_multiple_vault_batches_without_provider_io(self):
        for vault_id in range(2, 14):
            self.seed_owned(vault_id=vault_id)
        with patch.object(remediation,"VAULT_BATCH",2), patch.object(remediation,"TARGET_BATCH",1), patch("app.storage.s3_client",side_effect=AssertionError("Sweep cannot construct provider")):
            for tick in range(6):
                stamp = (datetime.now(timezone.utc)+timedelta(seconds=11*tick)).isoformat()
                self.assertEqual(remediation.sweep(self.db,stamp), 2)
        with self.db() as connection:
            self.assertEqual({r["vault_id"] for r in connection.execute("SELECT vault_id FROM upload_remediations").fetchall()}, set(range(2,14)))
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_remediation_jobs").fetchone()["n"], 12)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_locked_vault_does_not_starve_next_vault_or_lose_its_obligations(self):
        self.seed_owned(); self.seed_owned(vault_id=3)
        with patch.object(remediation,"VAULT_BATCH",1):
            with self.db() as held:
                held.execute("SELECT id FROM vaults WHERE id=2 FOR UPDATE")
                self.assertEqual(remediation.sweep(self.db), 0)
                self.assertEqual(remediation.sweep(self.db), 1)
            self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            self.assertEqual({r["vault_id"] for r in connection.execute("SELECT vault_id FROM upload_remediations").fetchall()}, {2,3})

    def test_timeslice_keeps_unprocessed_obligations_for_next_tick(self):
        self.seed_owned(); self.seed_owned(path="other.txt")
        clock = [0.0]
        enroll = discovery.enroll
        def exhaust(*args, **kwargs):
            row = enroll(*args, **kwargs)
            clock[0] = 10.0
            return row
        with patch.object(remediation,"monotonic",side_effect=lambda:clock[0]), patch.object(discovery,"monotonic",side_effect=lambda:clock[0]), patch.object(discovery,"enroll",side_effect=exhaust):
            self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            sql, params = discovery.obligation_query(vault_sql="2", stamp=storage.now_iso())
            self.assertEqual(len(connection.execute(sql, params).fetchall()), 1)
            self.assertEqual(connection.execute("SELECT count(*) n FROM archive_versions WHERE integrity='unverified'").fetchone()["n"], 2)
        self.assertEqual(remediation.sweep(self.db), 1)

    def test_completed_ordinary_upload_leaves_no_discovery_backlog(self):
        from app.services import metrics
        original = self.queue(); self.run_job(original); self.assert_keeper(original)
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            sql, params = discovery.obligation_query(vault_sql='2', stamp=storage.now_iso())
            self.assertEqual(connection.execute(sql, params).fetchall(), [])
            self.assertEqual(connection.execute('SELECT count(*) n FROM upload_remediations').fetchone()['n'], 0)
        remediation.observe(self.db)
        self.assertIn('upload_remediation_discovery_backlog 0', metrics.render_prometheus())
        self.assertEqual(self.provider.puts, 1)

    def test_global_send_grace_does_not_hide_targeted_no_second_send_obligation(self):
        original = self.fail_after_ack()
        stamp = storage.now_iso()
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (original,))
            connection.execute('DELETE FROM upload_remediation_discovery')
            connection.execute('UPDATE upload_intents SET created_at=%s', (stamp,))
            sql, params = discovery.obligation_query(vault_sql='2', stamp=stamp)
            self.assertEqual(connection.execute(sql, params).fetchall(), [])
            sql, params = discovery.obligation_query(vault_sql='2', stamp=stamp, file_id=self.file_id)
            self.assertEqual(len(connection.execute(sql, params).fetchall()), 1)
        self.assertEqual(remediation.sweep(self.db, stamp), 0)
        later = (datetime.fromisoformat(stamp)+timedelta(seconds=discovery.SEND_GRACE_SECONDS+1)).isoformat()
        self.assertEqual(remediation.sweep(self.db, later), 1)
        with self.db() as connection:
            episode = connection.execute('SELECT * FROM upload_remediations').fetchone()
            self.assertEqual(episode['due_at'], later)
        self.run_job(episode['current_job_id']); self.assert_keeper(episode['current_job_id'])
        self.assertEqual(self.provider.puts, 1)

    def test_cancelled_open_send_is_discovered_but_never_autonomously_resumed(self):
        original = self.fail_after_ack()
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (original,))
            connection.execute('DELETE FROM upload_remediation_discovery')
            connection.execute("UPDATE upload_intents SET created_at='2020-01-01T00:00:00+00:00'")
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            row = connection.execute('SELECT * FROM upload_remediations').fetchone()
            self.assertEqual(row['state'], 'cancelled')
            self.assertIsNone(row['current_job_id'])
            self.assertTrue(connection.execute('SELECT send_started FROM upload_intents').fetchone()['send_started'])
            self.assertEqual(connection.execute('SELECT count(*) n FROM jobs').fetchone()['n'], 1)
        self.assertEqual(self.provider.puts, 1)

    def test_settled_transition_leaves_unverified_version_discoverable(self):
        original = self.fail_after_ack()
        av, _ = self.seed_owned()
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (original,))
            connection.execute('DELETE FROM upload_remediation_discovery')
            connection.execute('UPDATE upload_intents SET archive_version_id=%s,settled=TRUE', (av,))
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            episode = connection.execute('SELECT * FROM upload_remediations').fetchone()
            self.assertIsNone(episode['intent_id'], 'Never replay a settled inventory')
            self.assertEqual(episode['archive_version_id'], av)
        self.assertEqual(self.provider.puts, 1)

    def test_enrollment_rollback_loses_no_durable_source(self):
        av, _ = self.seed_owned()
        enroll = discovery.enroll
        def crash(*args, **kwargs):
            enroll(*args, **kwargs)
            raise RuntimeError('crash before discovery commit')
        with patch.object(discovery, 'enroll', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'before discovery commit'):
                remediation.sweep(self.db)
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT count(*) n FROM upload_remediations').fetchone()['n'], 0)
            self.assertEqual(connection.execute('SELECT integrity FROM archive_versions WHERE id=%s', (av,)).fetchone()['integrity'], 'unverified')
        self.assertEqual(remediation.sweep(self.db), 1)

    def test_small_batch_skips_enrolled_cancelled_blocked_active_and_not_due_targets(self):
        stamp = storage.now_iso()
        for path, state in [('active','active'), ('cancelled','cancelled'), ('blocked','blocked'),
                            ('waiting','suspended'), ('runnable','pending')]:
            av, file_id = self.seed_owned(path=path)
            with self.db() as connection:
                if state == 'active':
                    active_version = av
                    # Even an expired lease belongs to its original active Job.
                    connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                        requested_at,updated_at,claim_expires_at)
                        VALUES (2,%s,%s,'upload','uploading',%s,%s,'2020-01-01T00:00:00+00:00')""", (file_id,path,stamp,stamp))
                elif state != 'pending':
                    vault = connection.execute('SELECT * FROM vaults WHERE id=2').fetchone()
                    row = discovery.enroll(connection, None, vault, stamp, version_id=av)
                    connection.execute("UPDATE upload_remediations SET state=%s,due_at='2099-01-01T00:00:00+00:00' WHERE id=%s", (state,row['id']))
                else:
                    runnable = av
        from app.services import metrics
        remediation.observe(self.db)
        self.assertIn('upload_remediation_discovery_backlog 1', metrics.render_prometheus())
        with patch.object(remediation, 'TARGET_BATCH', 1):
            self.assertEqual(remediation.sweep(self.db), 1)
        remediation.observe(self.db)
        self.assertIn('upload_remediation_discovery_backlog 0', metrics.render_prometheus())
        with self.db() as connection:
            rows = connection.execute("SELECT archive_version_id FROM upload_remediations WHERE state='queued'").fetchall()
            self.assertEqual(rows, [{'archive_version_id':runnable}])
            self.assertEqual(connection.execute("SELECT due_at FROM upload_remediations WHERE state='suspended'").fetchone()['due_at'], '2099-01-01T00:00:00+00:00')
            connection.execute("UPDATE jobs SET status='completed' WHERE path='active'")
        with patch.object(remediation, 'TARGET_BATCH', 1):
            self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT state FROM upload_remediations WHERE archive_version_id=%s', (active_version,)).fetchone()['state'], 'queued')
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_per_file_frontier_has_index_ranges_on_history_even_with_generic_plan(self):
        import json
        from tests.test_small_upload_cost import _whole_relation_reads
        _, file_id = self.seed_owned()
        sql, params = discovery.obligation_query(vault_sql='2', stamp=storage.now_iso(), file_id=file_id, limit=1)
        # Three separate equality binds, never a nullable OR that loses index
        # ranges when PostgreSQL switches to a generic prepared plan.
        for index in range(1, len(params)+1):
            sql = sql.replace('%s', f'${index}', 1)
        with self.db() as connection:
            connection.execute('SET LOCAL enable_seqscan=off')
            # This is an indexability contract, not a plan-cost assertion on a
            # tiny fixture. Do not let an empty-side hash/merge anti-join turn
            # an available point lookup into a full index walk.
            connection.execute('SET LOCAL enable_hashjoin=off')
            connection.execute('SET LOCAL enable_mergejoin=off')
            connection.execute('SET LOCAL plan_cache_mode=force_generic_plan')
            connection.execute(f'PREPARE obligation_lookup(text,text,text) AS {sql}')
            plan = connection.execute(f"EXPLAIN (FORMAT JSON) EXECUTE obligation_lookup('{file_id}','{file_id}','{file_id}')").fetchone()['QUERY PLAN']
        found = set()
        for branch in plan:
            _whole_relation_reads(branch['Plan'], found)
        self.assertFalse(found & {'jobs','upload_intents','archive_versions','upload_remediations','vault_files'}, json.dumps(plan))

    def test_namespace_changed_after_admission_fails_before_provider_observation(self):
        self.seed_owned()
        self.assertEqual(remediation.sweep(self.db), 1)
        with self.db() as connection:
            resumed = connection.execute("SELECT current_job_id FROM upload_remediations").fetchone()["current_job_id"]
            connection.execute("UPDATE vaults SET s3_bucket='different-bucket' WHERE id=2")
        events = []
        self.provider.hook = lambda stage,**kwargs:events.append(stage)
        self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual(events, [])
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (0,0,0))

    def test_cleanup_checkpoint_survives_failure_before_completion_and_explicit_retry(self):
        self.fail_after_ack()
        resumed = self.admit()
        real = storage._set_job_in_transaction
        def fault(connection,job_id,status,*args,**kwargs):
            if status == "completed":
                raise RuntimeError("injected before completion")
            return real(connection,job_id,status,*args,**kwargs)
        with patch("app.storage._set_job_in_transaction",side_effect=fault):
            self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "failed")
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            # Cleanup itself is durable, but its remediation linkage shares the
            # completion/progress commit. Never leave a partial completion unit.
            self.assertEqual(row["phase"], "cleanup")
            self.assertIsNone(row["cleanup_inventory_job_id"])
            self.assertIsNotNone(connection.execute("SELECT completed_at FROM snapshot_cleanup_inventories WHERE keeper_id=%s", (self.row(resumed)["archive_version_id"],)).fetchone()["completed_at"])
        final = self.admit(); self.run_job(final); self.assert_keeper(final)
        self.assertEqual(self.row(resumed)["status"], "failed")
        self.assertEqual((self.provider.puts,self.provider.gets),(1,0))

    def test_completion_commit_abort_retries_only_db_and_resolves_atomically(self):
        self.fail_after_ack(); resumed = self.admit()
        with self.db() as connection:
            connection.execute("CREATE SEQUENCE remediation_commit_attempt")
            connection.execute("""CREATE FUNCTION abort_remediation_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.status='completed' AND NEW.upload_reconcile_only THEN
                    IF NEW.transferred_bytes IS DISTINCT FROM NEW.total_bytes OR NOT EXISTS (
                        SELECT 1 FROM upload_remediations r
                        JOIN snapshot_cleanup_inventories c ON c.job_id=r.cleanup_inventory_job_id
                        WHERE r.resolved_by_job_id=NEW.id AND r.state='resolved'
                          AND c.keeper_id=NEW.archive_version_id AND c.completed_at IS NOT NULL
                    ) THEN
                        RAISE EXCEPTION 'partial completion/progress/linkage' USING ERRCODE='23514';
                    END IF;
                    IF nextval('remediation_commit_attempt')=1 THEN
                        RAISE EXCEPTION 'injected completion commit' USING ERRCODE='40001';
                    END IF;
                END IF; RETURN NEW; END $$""")
            connection.execute("""CREATE CONSTRAINT TRIGGER abort_remediation_completion AFTER UPDATE ON jobs
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION abort_remediation_commit()""")
        self.run_job(resumed); self.assert_keeper(resumed)
        with self.db() as connection:
            row = connection.execute("SELECT state,resolved_by_job_id FROM upload_remediations").fetchone()
            self.assertEqual(row, {"state":"resolved","resolved_by_job_id":resumed})
            self.assertGreaterEqual(connection.execute("SELECT last_value FROM remediation_commit_attempt").fetchone()["last_value"], 2)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,0,0))

    def test_cancel_during_duplicate_read_suppresses_restart_and_all_delete(self):
        self.provider.add("docs/report.txt",A,version="old-duplicate")
        self.fail_after_ack(); resumed = self.admit()
        def cancel(stage, **kwargs):
            if stage == "get":
                storage.cancel_jobs([resumed])
        self.provider.hook = cancel
        self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "cancelled")
        self.provider.hook = lambda *args,**kwargs:None
        for _ in range(3):
            self.assertEqual(remediation.sweep(self.db), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT state FROM upload_remediations").fetchone()["state"], "cancelled")
        self.assertEqual(len(self.provider.objects), 2)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,0))

    def test_delete_commit_gap_resumes_same_job_without_get_put_or_second_delete(self):
        self.provider.add("docs/report.txt",A,version="old-duplicate")
        self.fail_after_ack(); resumed = self.admit()
        def die(stage, **kwargs):
            if stage == "after_delete":
                raise KeyboardInterrupt("process death after exact delete")
        self.provider.hook = die
        with self.assertRaises(KeyboardInterrupt):
            self.run_job(resumed)
        self.provider.hook = lambda *args,**kwargs:None
        with self.db() as connection:
            connection.execute("UPDATE jobs SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id=%s", (resumed,))
        storage.reconcile_interrupted_jobs()
        self.assertEqual(remediation.sweep(self.db), 0)
        self.run_job(resumed); self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts,self.provider.gets,self.provider.deletes), (1,1,1))


if __name__ == "__main__":
    unittest.main()
