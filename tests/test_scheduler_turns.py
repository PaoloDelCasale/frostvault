"""Issue #552: only runnable, unserved Vault turns earn aging credit."""
from datetime import datetime, timedelta, timezone
import unittest

from concurrent.futures import ThreadPoolExecutor
import threading

from app.catalog import ArchiveCatalog
from app.services.job_scheduler import effective_priority, job_priority, select_fair_jobs
from tests.postgres import PostgresConnection
from tests.test_job_scheduler import _SchedulerDbMixin
from tests.test_database import run_alembic


class VaultTurnSelectionTests(unittest.TestCase):
    def test_manual_26000_backlog_does_not_make_auto_wait_thirty_minutes(self):
        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        manual = [dict(id=i, vault_id=9, action="upload", origin="manual",
                       requested_at=(now - timedelta(hours=1)).isoformat(),
                       scheduler_wait_seconds=0, scheduler_last_served_at=now.isoformat())
                  for i in range(1, 26001)]
        auto = dict(id=30000, vault_id=7, action="upload", origin="automatic",
                    requested_at=(now - timedelta(seconds=900)).isoformat(),
                    scheduler_wait_seconds=900, scheduler_last_served_at=None)
        selected = select_fair_jobs(manual + [auto], limit=1, active_by_vault={9: 15}, now=now)
        self.assertEqual(selected[0]["id"], auto["id"])

    def test_expired_turn_is_one_opportunity_not_inherited_by_backlog(self):
        jobs = [dict(id=i, vault_id=20, action='upload', origin='automatic',
                     scheduler_wait_seconds=1800) for i in (1, 2)]
        jobs.append(dict(id=3, vault_id=10, action='rename', origin='manual'))
        self.assertEqual([j['id'] for j in select_fair_jobs(jobs, limit=3)], [1, 3, 2])

    def test_idle_single_worker_uses_last_served_even_when_active_counts_are_zero(self):
        jobs = [dict(id=1, vault_id=10, action='upload', origin='manual',
                     requested_at='2000-01-01', scheduler_last_served_at='2026-11-11T12:00:00Z'),
                dict(id=2, vault_id=20, action='upload', origin='manual',
                     requested_at='2026-11-11', scheduler_last_served_at='2026-11-11T11:00:00Z')]
        self.assertEqual(select_fair_jobs(jobs, limit=1, active_by_vault={})[0]['id'], 2)

    def test_owner_precedence_except_documented_aged_turn(self):
        auto = dict(id=1, vault_id=20, action='upload', origin='automatic', scheduler_wait_seconds=900)
        owner = dict(id=2, vault_id=10, action='recover', origin='manual')
        self.assertEqual(select_fair_jobs([auto, owner], limit=1)[0]['id'], 2)
        auto['scheduler_wait_seconds'] = 1800
        self.assertEqual(select_fair_jobs([auto, owner], limit=1)[0]['id'], 1)
        owner['vault_id'] = 20
        self.assertEqual(select_fair_jobs([auto, owner], limit=1)[0]['id'], 2)

    def test_very_old_jobs_without_accounted_credit_keep_base_tier(self):
        job = dict(action='upload', origin='manual', requested_at='2000-01-01')
        self.assertEqual(effective_priority(job, datetime.now(timezone.utc)), job_priority(job))


class DurableVaultTurnTests(_SchedulerDbMixin, unittest.TestCase):
    start = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)

    def stamp(self, seconds=0):
        return (self.start + timedelta(seconds=seconds)).isoformat()

    def observe(self, seconds=0, excluded=()):
        with PostgresConnection(self.path) as connection:
            catalog = ArchiveCatalog(connection)
            jobs = catalog.list_claimable_jobs(now=self.stamp(seconds), restore_due_before=self.stamp(seconds),
                                               limit=1, per_vault_limit=1, exclude_job_ids=excluded)
            turns = catalog.observe_scheduler_turns(now=self.stamp(seconds),
                                                    runnable_vault_ids=[j['vault_id'] for j in jobs])
            for job in jobs:
                job.update(turns.get(job['vault_id'], {}))
            return jobs

    def claim(self, job_id, seconds=0, generation=None):
        with PostgresConnection(self.path) as connection:
            return ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=f'claim-{job_id}-{seconds}', claimed_at=self.stamp(seconds),
                claim_expires_at=self.stamp(seconds + 300), now=self.stamp(seconds),
                restore_due_before=self.stamp(seconds), scheduler_generation=generation)

    def state(self, vault_id=20):
        with PostgresConnection(self.path) as connection:
            return connection.execute('SELECT * FROM scheduler_vault_turns WHERE vault_id=%s',
                                      (vault_id,)).fetchone()

    def auto_job(self, vault_id=20, path='a.txt'):
        job = self._insert_upload_job(vault_id=vault_id, path=path)
        with PostgresConnection(self.path) as connection:
            connection.execute("UPDATE jobs SET origin='automatic' WHERE id=%s", (job,))
        return job

    def test_continuously_served_manual_backlog_yields_after_900_not_1800(self):
        manual = self._insert_upload_job(vault_id=10, path='a.txt')
        auto = self.auto_job()
        self.observe()
        for second in range(0, 901, 60):
            # Keep replenishing a real manual queue while giving it service.
            if second:
                with PostgresConnection(self.path) as connection:
                    connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (manual,))
                manual = self._insert_upload_job(vault_id=10, path='a.txt')
            self.assertIsNotNone(self.claim(manual, second))
            jobs = self.observe(second)
        self.assertEqual(self.state(10)['wait_seconds'], 0)
        self.assertEqual(self.state()['wait_seconds'], 900)
        self.assertEqual(select_fair_jobs(jobs, limit=1, active_by_vault={10: 1})[0]['id'], auto)

    def test_restart_preserves_turn_but_claimed_backlog_starts_fresh(self):
        auto = self.auto_job()
        self.auto_job(path='b.txt')
        self.observe()
        self.observe(900)  # each observe uses a fresh connection/catalog
        self.assertEqual(self.state()['wait_seconds'], 900)
        self.assertIsNotNone(self.claim(auto, 900, generation=0))
        self.assertEqual(self.state()['wait_seconds'], 0)
        self.observe(1000)
        self.assertIsNone(self.state()['eligible_since'])  # active service
        with PostgresConnection(self.path) as connection:
            connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (auto,))
        jobs = self.observe(1100)
        self.assertEqual(jobs[0]['scheduler_wait_seconds'], 0)
        self.observe(1160)
        self.assertEqual(self.state()['wait_seconds'], 60)

    def test_pause_between_polls_cannot_earn_credit_and_preserves_bank(self):
        self.auto_job()
        self.observe()
        self.observe(600)
        with PostgresConnection(self.path) as connection:
            connection.execute('UPDATE vaults SET paused=TRUE WHERE id=20')
            connection.execute('UPDATE vaults SET paused=FALSE WHERE id=20')
        self.observe(4000)
        self.assertEqual(self.state()['wait_seconds'], 600)
        self.observe(4300)
        self.assertEqual(self.state()['wait_seconds'], 900)

    def test_retry_backoff_between_polls_does_not_count_as_unserved_time(self):
        auto = self.auto_job()
        self.observe()
        self.observe(600)
        with PostgresConnection(self.path) as connection:
            connection.execute("UPDATE jobs SET status='retrying', retry_after=%s WHERE id=%s",
                               (self.stamp(4000), auto))
        self.assertEqual(self.observe(3000), [])
        self.observe(4000)
        self.assertEqual(self.state()['wait_seconds'], 600)
        self.observe(4300)
        self.assertEqual(self.state()['wait_seconds'], 900)

    def test_local_deferral_freezes_bank_instead_of_earning_credit(self):
        auto = self.auto_job()
        self.observe()
        self.observe(600)
        self.observe(1000, excluded=[auto])
        self.observe(5000, excluded=[auto])
        self.observe(6000)
        self.assertEqual(self.state()['wait_seconds'], 600)
        self.observe(6300)
        self.assertEqual(self.state()['wait_seconds'], 900)

    def test_failed_or_stale_claim_never_consumes_credit(self):
        auto = self.auto_job()
        self.observe()
        self.observe(1800)
        before = self.state()
        self.assertIsNone(self.claim(auto, 1800, generation=999))
        self.assertEqual(self.state(), before)
        with PostgresConnection(self.path) as connection:
            connection.execute('UPDATE vaults SET paused=TRUE WHERE id=20')
        before = self.state()
        self.assertIsNone(self.claim(auto, 1800, generation=0))
        self.assertEqual(self.state(), before)
        self.assertIsNotNone(self.state()['wait_seconds'])

    def test_concurrent_different_jobs_can_consume_an_aged_turn_only_once(self):
        ids = [self.auto_job(), self.auto_job(path='b.txt')]
        self.observe()
        self.observe(1800)
        barrier = threading.Barrier(2)

        def run(job):
            barrier.wait(timeout=10)
            return self.claim(job, 1800, generation=0)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(run, ids))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.state()['generation'], 1)
        self.assertEqual(self.state()['wait_seconds'], 0)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute('SELECT count(*) AS n FROM jobs WHERE claim_token IS NOT NULL')
                             .fetchone()['n'], 1)

    def test_claim_waiting_for_accounting_never_holds_a_bulk_writers_next_job(self):
        first, second = self.auto_job(), self.auto_job(path='b.txt')
        self.observe()
        self.observe(600)
        waiting = threading.Event()
        test = self

        def claim_after_writer():
            with PostgresConnection(test.path) as connection:
                class ObserveLock:
                    def execute(self, sql, params=()):
                        if 'SELECT generation FROM scheduler_vault_turns' in sql:
                            waiting.set()
                        return connection.execute(sql, params)
                return ArchiveCatalog(ObserveLock()).claim_job(
                    job_id=second, claim_token='contender', claimed_at=test.stamp(700),
                    claim_expires_at=test.stamp(1000), now=test.stamp(700),
                    restore_due_before=test.stamp(700))

        with ThreadPoolExecutor(max_workers=1) as pool:
            with PostgresConnection(self.path) as writer:
                writer.execute("SET LOCAL lock_timeout='2s'")
                writer.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (first,))
                future = pool.submit(claim_after_writer)
                self.assertTrue(waiting.wait(timeout=10))
                # This would deadlock/time out if claim held second's Job row
                # while waiting for the writer's accounting lock.
                writer.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (second,))
            self.assertIsNone(future.result(timeout=10))
        self.assertEqual(self.state()['generation'], 0)
        self.assertEqual(self.state()['wait_seconds'], 600)

    def test_contended_purge_member_cannot_split_a_group_or_consume_a_turn(self):
        ids = [self.auto_job(), self.auto_job(path='b.txt')]
        with PostgresConnection(self.path) as connection:
            connection.execute("UPDATE jobs SET action='cloud-purge', origin='manual', group_id='turn-552'")
        self.observe()
        self.observe(900)
        before = self.state()

        def claim_group():
            with PostgresConnection(self.path) as connection:
                return ArchiveCatalog(connection).claim_purge_group(
                    lead_job_id=ids[0], claim_token='purge', claimed_at=self.stamp(900),
                    claim_expires_at=self.stamp(1200), now=self.stamp(900),
                    message='deleting', message_key='job.cloud_purge_deleting')

        with PostgresConnection(self.path) as blocker:
            blocker.execute('SELECT id FROM jobs WHERE id=%s FOR UPDATE', (ids[1],))
            self.assertEqual(claim_group(), [])
        self.assertEqual(self.state(), before)
        self.assertEqual(len(claim_group()), 2)
        self.assertEqual(self.state()['generation'], 1)  # one group, not two Jobs
        self.assertEqual(self.state()['wait_seconds'], 0)

    def test_concurrent_base_claims_both_succeed_without_a_vault_quota(self):
        ids = [self.auto_job(), self.auto_job(path='b.txt')]
        barrier = threading.Barrier(2)

        def run(job):
            barrier.wait(timeout=10)
            return self.claim(job)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(run, ids))
        self.assertTrue(all(result is not None for result in results))
        self.assertEqual(self.state()['generation'], 2)
        self.assertEqual(self.state()['wait_seconds'], 0)

    def test_all_idle_single_worker_claims_alternate_equal_tier_turns(self):
        ids = {vault: [self._insert_upload_job(vault_id=vault, path=path)
                       for path in ('a.txt', 'b.txt')] for vault in (10, 20)}
        order = []
        for second in range(4):
            jobs = self.observe(second)
            picked = select_fair_jobs(jobs, limit=1, active_by_vault={})[0]
            self.assertIsNotNone(self.claim(picked['id'], second))
            order.append(picked['vault_id'])
            # Actual idle boundary, not the old test's accumulating active map.
            with PostgresConnection(self.path) as connection:
                connection.execute("UPDATE jobs SET status='completed' WHERE id=%s", (picked['id'],))
        self.assertEqual(order, [10, 20, 10, 20])
        self.assertEqual(sum(map(len, ids.values())), 4)

    def test_contended_turn_skips_without_consuming_credit_or_leasing_job(self):
        auto = self.auto_job()
        self.observe()
        self.observe(1800)
        before = self.state()
        with PostgresConnection(self.path) as blocker:
            blocker.execute('SELECT * FROM scheduler_vault_turns WHERE vault_id=20 FOR UPDATE')
            self.assertIsNone(self.claim(auto, 1800, generation=0))
        self.assertEqual(self.state(), before)

    def test_claim_and_accounting_rollback_together(self):
        auto = self.auto_job()
        self.observe()
        self.observe(1800)
        before = self.state()
        with PostgresConnection(self.path) as connection:
            result = ArchiveCatalog(connection).claim_job(
                job_id=auto, claim_token='rolled-back', claimed_at=self.stamp(1800),
                claim_expires_at=self.stamp(2100), now=self.stamp(1800),
                restore_due_before=self.stamp(1800), scheduler_generation=0)
            self.assertIsNotNone(result)
            connection.rollback()
        self.assertEqual(self.state(), before)
        with PostgresConnection(self.path) as connection:
            self.assertIsNone(connection.execute('SELECT claim_token FROM jobs WHERE id=%s', (auto,))
                              .fetchone()['claim_token'])

    def test_incompatible_downgrade_refusal_and_reentry_preserve_scheduler_credit(self):
        auto = self.auto_job()
        self.observe()
        self.observe(900)
        before = self.state()
        result = run_alembic(self.path, revision='0070_vault_pause', command='downgrade')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('incompatible Current Snapshot baseline cannot be downgraded', result.stderr)
        with PostgresConnection(self.path) as connection:
            self.assertIsNotNone(connection.execute("SELECT to_regclass('scheduler_vault_turns') AS name")
                                 .fetchone()['name'])
            self.assertEqual(connection.execute('SELECT origin FROM jobs WHERE id=%s', (auto,))
                             .fetchone()['origin'], 'automatic')
        self.assertEqual(self.state(), before, 'Refusal must not erase starvation credit')
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state(), before)

    def test_busy_observation_does_not_scan_already_served_bulk_windows(self):
        manual = self._insert_upload_job(vault_id=10, path='a.txt')
        self._insert_upload_job(vault_id=10, path='b.txt')
        auto = self.auto_job()
        self.assertIsNotNone(self.claim(manual))
        with PostgresConnection(self.path) as connection:
            rows = ArchiveCatalog(connection).list_claimable_jobs(
                now=self.stamp(), restore_due_before=self.stamp(),
                limit=1, per_vault_limit=1, unserved_only=True)
        self.assertEqual([row['id'] for row in rows], [auto])

    def test_global_candidate_limit_cannot_hide_starved_automatic_vault(self):
        manual = self._insert_upload_job(vault_id=10, path='a.txt')
        auto = self.auto_job()
        rows = self.observe()
        self.assertEqual({row['id'] for row in rows}, {manual, auto})
        rows = self.observe(1800)
        self.assertEqual(select_fair_jobs(rows, limit=1)[0]['id'], manual)
        # Once the manual Vault gets service its remaining old backlog has no
        # credit, and the automatic turn is still visible even with limit=1.
        self.assertIsNotNone(self.claim(manual, 1800))
        self._insert_upload_job(vault_id=10, path='b.txt')
        rows = self.observe(1800)
        self.assertEqual(select_fair_jobs(rows, limit=1)[0]['id'], auto)
