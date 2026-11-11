"""Issue #552: only runnable, unserved Vault turns earn aging credit."""
from datetime import datetime, timedelta, timezone
import unittest

from app.services.job_scheduler import select_fair_jobs


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
