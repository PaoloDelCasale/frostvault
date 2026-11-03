"""Fixed #425 alert contracts; database cases run on disposable PostgreSQL in CI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from app.config import settings
from app.services import notifications as inbox
from app.services import protection_notifications as monitor
from app.services.source_layout import VaultLocalAccess

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
ONLINE = VaultLocalAccess(True, True, None, 'ok')


class DelayRuleTests(unittest.TestCase):
    def row(self, hours=3, *, moving=False):
        return {'pending_since': (NOW-timedelta(hours=hours)).isoformat(),
                'content_observed_at': (NOW-timedelta(seconds=10) if moving else NOW-timedelta(hours=hours)).isoformat(),
                'mtime_ns': int((NOW-timedelta(seconds=10) if moving else NOW-timedelta(hours=hours)).timestamp()*1e9)}

    def counts(self, rows, progress=None):
        return monitor.delay_counts(rows, now=NOW, last_progress=progress, stability_seconds=300)

    def test_stall_requires_two_hours_after_stability_not_import_start(self):
        self.assertEqual(self.counts([self.row(2)])['stalled'], 0)
        self.assertEqual(self.counts([self.row(3)])['stalled'], 1)

    def test_large_import_advancing_has_no_alert_but_24_hour_ceiling_remains(self):
        progress = (NOW-timedelta(minutes=30)).isoformat()
        for age, expected in ((8, 0), (25, 1)):
            with self.subTest(age=age):
                counts = self.counts([self.row(age)], progress)
                self.assertEqual(counts['stalled'], 0)
                self.assertEqual(counts['overdue'], expected)

    def test_never_stable_is_not_an_upload_error_and_does_not_restart_24h_age(self):
        self.assertEqual(self.counts([self.row(8, moving=True)])['stalled'], 0)
        counts = self.counts([self.row(25, moving=True)])
        self.assertEqual(counts, {'pending': 1, 'overdue': 1, 'unstable': 1, 'stalled': 0})

    def test_empty_queue_and_recent_arrivals_do_not_alert(self):
        self.assertEqual(self.counts([])['stalled'], 0)
        self.assertEqual(self.counts([self.row(.5)])['stalled'], 0)

    def test_migration_sql_has_no_accidental_sqlalchemy_bind_parameters(self):
        import importlib
        import sqlalchemy as sa
        migration = importlib.import_module('migrations.versions.0056_notification_problems')
        with patch.object(migration, 'op') as operations:
            migration.upgrade()
        for call in operations.execute.call_args_list:
            self.assertEqual(sa.text(call.args[0]).compile().params, {})

    def test_summary_renders_dates_states_and_metrics_in_both_languages(self):
        data = {'states': {'pending': 3, 'protected': 2, 'problem': 1, 'unconfirmed': 4, 'cloud_only': 5},
                'last_verified_backup': '2026-11-02', 'open_problems': 2,
                'freed_bytes': 2*1024**3, 'freed_files': 6, 'cleanup_blocked': 1,
                'monthly_cost_estimate': 1.23, 'currency': 'EUR',
                'vaults': [{'vault_id': 9, 'last_scan': 'scan-date', 'last_audit': 'audit-date', 'suspended': True}]}
        en = inbox.render_weekly_summary(data, locale='en')
        it = inbox.render_weekly_summary(data, locale='it')
        for text in (en, it):
            for value in ('2026-11-02', 'scan-date', 'audit-date', '2.00 GiB', '1.23 EUR'):
                self.assertIn(value, text)
        self.assertIn('Pending: 3', en)
        self.assertIn('In attesa: 3', it)
        self.assertIn('Solo cloud: 5', it)


class FixedNotificationPostgresTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the established disposable database fixture, not its tests.
        from tests import test_notifications as fixture
        fixture.NotificationDeliveryTests.setUp(self)
        self.email = replace(settings, smtp_host='smtp.example.test',
                             smtp_from_address='alerts@example.test', smtp_to_address='owner@example.test')
        self.email_patch = patch.object(inbox, 'settings', self.email)
        self.email_patch.start()
        self.addCleanup(self.email_patch.stop)

    def connect(self):
        from tests.postgres import PostgresConnection
        return PostgresConnection(str(self.path))

    def test_every_terminal_event_fixed_routing_and_no_routine_events(self):
        with self.connect() as c:
            for action, status, origin, event in (
                ('upload','completed','manual',None), ('free-space','completed','automatic',None),
                ('free-space','failed','automatic',None), ('rename','completed','automatic',None),
                ('upload','retrying','manual',None), ('upload','failed','manual','job_failed'),
                ('recover','completed','manual','recovery_completed'), ('recover','failed','manual','recovery_failed'),
                ('recover','completed','automatic',None),
            ):
                with self.subTest(action=action, status=status, origin=origin):
                    c.execute('UPDATE jobs SET action=%s,status=%s,origin=%s WHERE id=%s', (action,status,origin,self.job_id))
                    count = inbox.enqueue_job_terminal_notification(c, job_id=self.job_id)
                    self.assertEqual(count, int(event is not None))
                    self.assertEqual(inbox.enqueue_job_terminal_notification(c, job_id=self.job_id), 0)
            self.assertEqual({row['event'] for row in c.execute('SELECT event FROM notifications').fetchall()},
                             {'job_failed','recovery_completed','recovery_failed'})
            self.assertEqual(c.execute('SELECT count(*) AS n FROM notification_deliveries').fetchone()['n'], 3)

    def test_episode_dedup_read_resolve_recur_and_independent_counters(self):
        with self.connect() as c:
            for event in ('protection_delayed','protection_blocked'):
                args = dict(key=event,event=event,vault_id=self.vault_id,
                            params={'component':'cloud','pending':1,'stalled':1,'overdue':0,'unstable':0})
                inbox.set_problem(c, active=True, **args)
                inbox.set_problem(c, active=True, **args)
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),2)
            self.assertEqual(inbox.count_unread_notifications(c,user_id=self.user_id),2)
            inbox.mark_all_notifications_read(c,user_id=self.user_id)
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),2)
            self.assertEqual(inbox.count_unread_notifications(c,user_id=self.user_id),0)
            inbox.set_problem(c, active=False, **args)
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),1)
            inbox.set_problem(c, active=True, **args)
            self.assertEqual(inbox.count_unread_notifications(c,user_id=self.user_id),1)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM notifications').fetchone()['n'],2)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM notification_deliveries').fetchone()['n'],2)
            inbox.set_problem(c, active=False, **args)
            self.assertEqual(inbox.count_unread_notifications(c,user_id=self.user_id),0)
            resolved = inbox.list_in_app_notifications(c,user_id=self.user_id,status='read')
            self.assertIn('resolved', {row['problem_status'] for row in resolved})

    def test_terminal_failure_resolves_on_retry_before_email_or_inbox_read(self):
        with self.connect() as c:
            c.execute("UPDATE jobs SET status='failed' WHERE id=%s",(self.job_id,))
            inbox.enqueue_job_terminal_notification(c,job_id=self.job_id)
            c.execute("UPDATE jobs SET status='retrying' WHERE id=%s",(self.job_id,))
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),0)
            self.assertEqual(inbox.count_unread_notifications(c,user_id=self.user_id),0)
            smtp = unittest.mock.Mock()
            self.assertEqual(inbox.deliver_pending_notifications(c,smtp_client=smtp)['attempted'],0)
            smtp.send.assert_not_called()

    def test_delay_is_aggregated_deduplicated_and_never_stable_age_survives_writes(self):
        from app.catalog import ArchiveCatalog
        with self.connect() as c, patch('app.services.source_layout.vault_local_access',return_value=ONLINE):
            catalog = ArchiveCatalog(c)
            for name in ('one.jpg','two.jpg','live.lrcat'):
                catalog.observe_local_copy(vault_id=self.vault_id,path=name,file_type='regular',size=1,
                    mtime_ns=1,observed_at=(NOW-timedelta(hours=25)).isoformat())
            vault = c.execute('SELECT * FROM vaults WHERE id=%s',(self.vault_id,)).fetchone()
            monitor.monitor_vault(c,vault,now=NOW)
            # Keep changing the live file; timer must remain continuous.
            catalog.observe_local_copy(vault_id=self.vault_id,path='live.lrcat',file_type='regular',size=2,
                    mtime_ns=int(NOW.timestamp()*1e9),observed_at=NOW.isoformat())
            monitor.monitor_vault(c,vault,now=NOW+timedelta(seconds=30))
            notes = c.execute("SELECT * FROM notifications WHERE event='protection_delayed'").fetchall()
            self.assertEqual(len(notes),1)
            self.assertEqual(json.loads(notes[0]['message_params'])['unstable'],1)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM notification_deliveries').fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM archive_versions').fetchone()['n'],0)
            c.execute("UPDATE local_copies SET presence='missing'")
            monitor.monitor_vault(c,vault,now=NOW+timedelta(minutes=1))
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),0)
            self.assertEqual(inbox.count_unread_notifications(c,user_id=self.user_id),0)

    def test_source_and_suspension_are_blocked_with_no_secret_details(self):
        with self.connect() as c:
            c.execute("INSERT INTO vault_operation_policies(vault_id,auto_upload,suspended_at) VALUES (%s,FALSE,%s)",
                      (self.vault_id,(NOW-timedelta(hours=25)).isoformat()))
            vault = c.execute('SELECT * FROM vaults WHERE id=%s',(self.vault_id,)).fetchone()
            with patch('app.services.source_layout.vault_local_access',return_value=VaultLocalAccess(False,True,None,'absent')):
                monitor.monitor_vault(c,vault,now=NOW)
                monitor.monitor_vault(c,vault,now=NOW)
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),2)
            c.execute('UPDATE vault_operation_policies SET auto_upload=TRUE WHERE vault_id=%s',(self.vault_id,))
            with patch('app.services.source_layout.vault_local_access',return_value=ONLINE):
                monitor.monitor_vault(c,vault,now=NOW)
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),0)

    def test_summary_is_weekly_and_contains_required_persisted_evidence(self):
        with self.connect() as c, patch('app.services.source_layout.vault_local_access',return_value=ONLINE), patch.object(inbox,'now_iso',return_value=NOW.isoformat()):
            monitor.maintain_notifications(c,now=NOW)
            monitor.maintain_notifications(c,now=NOW+timedelta(days=6))
            rows = c.execute("SELECT * FROM notifications WHERE event='weekly_summary'").fetchall()
            self.assertEqual(len(rows),1)
            data = json.loads(rows[0]['message_params'])
            self.assertEqual(set(data['states']),set(monitor.STATES))
            self.assertIsNone(data['last_verified_backup'])
            for key in ('last_scan','last_audit','suspended'):
                self.assertIn(key,data['vaults'][0])
            for key in ('open_problems','freed_bytes','monthly_cost_estimate','cleanup_blocked'):
                self.assertIn(key,data)
            monitor.maintain_notifications(c,now=NOW+timedelta(days=7))
            self.assertEqual(c.execute("SELECT count(*) AS n FROM notifications WHERE event='weekly_summary'").fetchone()['n'],2)

    def test_resolved_catalog_incident_is_read_without_dismissing_open_problems(self):
        from app.catalog import ArchiveCatalog
        from app.services.catalog_incidents import remember_missing_archives, resolve_archive_version_incidents
        with self.connect() as c:
            catalog = ArchiveCatalog(c)
            version = catalog.record_archive_version(vault_id=self.vault_id,path='lost.jpg',object_key='docs/lost.jpg',
                provider_version_id='v1',size=2,storage_class='STANDARD',etag='e',uploaded_at=NOW.isoformat(),
                observed_at=NOW.isoformat(),scan_id='scan')
            row = c.execute('SELECT av.*, fp.path FROM archive_versions av JOIN file_paths fp ON fp.vault_file_id=av.vault_file_id WHERE av.id=%s',(version,)).fetchone()
            remember_missing_archives(c,[row],reason='missing_in_cloud',observed_at=NOW.isoformat())
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),1)
            resolve_archive_version_incidents(c,[version],observed_at=NOW.isoformat())
            self.assertEqual(inbox.count_unread_notifications(c,user_id=self.user_id),0)
            self.assertEqual(inbox.list_in_app_notifications(c,user_id=self.user_id)[0]['problem_status'],'resolved')
            # Notification resolution never changes or removes the sole cloud copy.
            self.assertEqual(c.execute('SELECT count(*) AS n FROM archive_versions WHERE id=%s',(version,)).fetchone()['n'],1)

    def test_delivery_failure_is_redacted_bounded_and_does_not_change_open_problem(self):
        with self.connect() as c:
            inbox.set_problem(c,key='blocked',event='protection_blocked',vault_id=self.vault_id,
                              active=True,params={'component':'cloud'})
            smtp = unittest.mock.Mock()
            smtp.send.side_effect=RuntimeError('SECRET smtp://credentials')
            for _ in range(3):
                c.execute('UPDATE notification_deliveries SET next_attempt_at=NULL')
                inbox.deliver_pending_notifications(c,smtp_client=smtp)
            delivery = c.execute('SELECT * FROM notification_deliveries').fetchone()
            self.assertEqual(delivery['status'],'failed')
            self.assertEqual(delivery['attempt_count'],3)
            self.assertNotIn('SECRET',str(delivery))
            self.assertEqual(inbox.count_open_problems(c,user_id=self.user_id),1)
