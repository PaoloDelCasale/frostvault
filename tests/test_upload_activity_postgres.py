"""Read-only aggregate parity and scale against disposable PostgreSQL16 in CI."""
from datetime import datetime, timezone
import unittest
from app.services.operation_policies import OperationPolicy, auto_upload_path_is_included, set_policy
from app.services.upload_activity import _eligibility_sql, upload_activity
from tests import test_protection_states as fixtures

NOW = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)


class UploadActivityPostgresTests(unittest.TestCase):
    setUp = fixtures.ProtectionStatePostgresTests.setUp
    seed = fixtures.ProtectionStatePostgresTests.seed

    def job(self, name, status, *, total=12, transferred=0, reconcile=False):
        file_id, _ = self.seed(name)
        self.connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
            requested_at,updated_at,total_bytes,transferred_bytes,upload_reconcile_only)
            VALUES (9,%s,%s,'upload',%s,%s,%s,%s,%s,%s)""",
            (file_id, f'photos/{name}.jpg', status, NOW.isoformat(), NOW.isoformat(), total, transferred, reconcile))
        return file_id

    def test_four_stages_progress_and_stability_uses_observed_content_not_pending_age(self):
        wait, _ = self.seed('wait')
        self.connection.execute("UPDATE local_copies SET content_observed_at=%s WHERE vault_file_id=%s", ('2026-11-06T09:58:00+00:00', wait))
        self.connection.execute("INSERT INTO protection_pending VALUES (%s,%s)", (wait, '2026-11-01T10:00:00Z'))
        # A future preserved mtime also extends, never shortens, the deadline.
        future, _ = self.seed('future')
        self.connection.execute("UPDATE local_copies SET mtime_ns=%s WHERE vault_file_id=%s", (int(NOW.timestamp() * 1e9) + 60_000_000_000, future))
        self.job('queue', 'queued')
        self.job('retry', 'retrying')
        self.job('transfer', 'uploading', transferred=5)
        self.job('verify', 'verifying', transferred=100)
        self.job('reconcile', 'uploading', reconcile=True)
        self.job('reconcile-wait', 'queued', reconcile=True)
        self.seed('ready')
        self.seed('protected', archive=True, verified=True)
        for status in ('completed', 'failed', 'cancelled'):
            file_id = self.job(status, status)
            self.connection.execute("UPDATE jobs SET origin='automatic' WHERE vault_file_id=%s", (file_id,))
        result = upload_activity(self.connection, now=NOW)
        total = result['global']
        self.assertEqual(total['stability_wait']['files'], 2)
        self.assertEqual(total['next_stable_at'], '2026-11-06T10:03:00+00:00')
        self.assertEqual(total['uploading'], {'files': 1, 'bytes': 12, 'transferred_bytes': 5})
        self.assertEqual(total['verifying'], {'files': 2, 'bytes': 24, 'transferred_bytes': 12})
        # Ready observation + completed attempt with unmatched local content;
        # failed/cancelled automatic attempts do not advertise a new admission.
        self.assertEqual(total['queued']['files'], 5)
        self.assertEqual(total['awaiting_admission_files'], 2)
        self.assertEqual(len(result['vaults']), 1)

    def block_gate(self, status='blocked'):
        self.connection.execute(
            "INSERT INTO cloud_preflight_state(id,status,remediation,failure_count,next_probe_at) "
            "VALUES (1,%s,'S3 access was refused',1,%s) ON CONFLICT(id) DO UPDATE SET status=EXCLUDED.status",
            (status, NOW.isoformat()))

    def test_blocked_preflight_reports_leased_jobs_without_transfer_as_waiting_not_uploading(self):
        # Leases of a stopped process keep status 'uploading' (hashing or just
        # leased) until they expire; nothing was sent and no transfer can start.
        for index in range(16):
            self.job(f'leased-{index}', 'uploading')
        self.job('moved', 'uploading', transferred=5)
        self.job('reconcile', 'uploading', reconcile=True)
        self.job('queue', 'queued')
        for gate in (None, 'ready'):
            if gate:
                self.block_gate(gate)
            with self.subTest(gate=gate):
                total = upload_activity(self.connection, now=NOW)['global']
                self.assertEqual(total['uploading']['files'], 17)
                self.assertEqual(total['queued']['files'], 1)
        self.block_gate('blocked')
        result = upload_activity(self.connection, now=NOW)
        total = result['global']
        # Bytes that really moved stay visible as uploading; zero-byte leases wait.
        self.assertEqual(total['uploading'], {'files': 1, 'bytes': 12, 'transferred_bytes': 5})
        self.assertEqual(total['queued']['files'], 17)
        self.assertEqual(total['queued']['transferred_bytes'], 0)
        self.assertEqual(total['verifying']['files'], 1)
        self.assertEqual(result['vaults'][0]['queued']['files'], 17)
        self.block_gate('ready')
        self.assertEqual(upload_activity(self.connection, now=NOW)['global']['uploading']['files'], 17)

    def test_global_vault_isolation_suspension_exclusions_and_read_only_scale(self):
        self.connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (10,'second','Second','/second','bucket','second')")
        self.seed('waiting')
        self.seed('skip')
        self.seed('protected', archive=True, verified=True)
        set_policy(self.connection, 9, OperationPolicy(exclude_globs=('skip.jpg',)))
        self.connection.execute("""INSERT INTO vault_files(id,vault_id,created_at)
            SELECT 'activity-' || n,10,%s FROM generate_series(1,3000) n""", (NOW.isoformat(),))
        self.connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at,total_bytes,transferred_bytes)
            SELECT 10,id,id,'upload','uploading',%s,%s,100,30 FROM vault_files WHERE vault_id=10""", (NOW.isoformat(), NOW.isoformat()))
        before = self.connection.execute('SELECT count(*) AS n FROM archive_versions').fetchone()['n']
        result = upload_activity(self.connection, now=NOW)
        self.assertEqual(result['global']['uploading']['files'], 3000)
        self.assertEqual(result['global']['uploading']['bytes'], 300000)
        self.assertEqual(result['global']['queued']['files'], 1)
        by_id = {row['vault_id']: row for row in result['vaults']}
        self.assertEqual(by_id[9]['uploading']['files'], 0)
        self.assertEqual(by_id[10]['uploading']['transferred_bytes'], 90000)
        set_policy(self.connection, 9, OperationPolicy(auto_upload=False))
        self.assertEqual(upload_activity(self.connection, now=NOW)['global']['queued']['files'], 0)
        self.assertEqual(self.connection.execute('SELECT count(*) AS n FROM archive_versions').fetchone()['n'], before)
        self.assertEqual(self.connection.execute("SELECT count(*) AS n FROM archive_versions WHERE availability='available'").fetchone()['n'], before)

    def test_postgres_regex_path_rules_match_real_admission_rules(self):
        paths = ['a.jpg', 'photos/a.jpg', 'photos/sub/a.jpg', 'a/b/c', 'x/a/b/c/y',
                 'foo', 'x/foo/y', '.DS_Store', 'x/._foo', 'a\nb.jpg', 'a[1].jpg',
                 'foo/a/bar/x', 'x/foo/a/bar/x', 'foo.lock/a.jpg',
                 # A Linux name may contain a backslash; admission reads it as a separator.
                 'foo\\._bar', 'a\\b.jpg', ':', 'a1', ']', '[', '>x', ':x', '(x', '?x']
        for pattern in ['*.jpg', 'photos/**', '**/*.jpg', '**/a/b/**', 'a*b*c',
                        'foo*bar', 'foo', '?', '[!x]*', 'a[[]1].jpg',
                        # Sets PostgreSQL would read as classes/collations unless escaped (#498).
                        '[a-z[:digit:]]*', '[[:alpha:]]*', '[[.a.]]*', '[[=a=]]*', '[]abc]*',
                        '[ab', "it's/**", '100%*.raw',
                        # Set contents that look like regex syntax outside a set.
                        '[(?>]x', '[(?!)]x', '[(?:]x', '(?>x)*',
                        # The longest accepted glob must stay within PostgreSQL's regex limits.
                        '*a' * 512, 'a?*' * 340, '*[ab]' * 204, '*[!a]' * 204, '*?' * 512,
                        # "**/" and "/**" repeat the body in three alternatives.
                        '**/' + '*a' * 509 + '/**']:
            policy = OperationPolicy(include_globs=(pattern,))
            sql, params = _eligibility_sql(policy)
            for path in paths:
                observed = self.connection.execute(f'SELECT {sql} AS included FROM (SELECT %s::text AS path) fp', [*params, path]).fetchone()['included']
                self.assertEqual(observed, auto_upload_path_is_included(path, policy), (path, pattern))
