"""The one vocabulary of Job states (#794): terminal set and "open Job" predicate."""
from __future__ import annotations

import unittest

import psycopg

from app.services.job_queue import (
    JOB_LEASE_HELD_STATUSES,
    OPEN_JOB_SQL,
    TERMINAL_JOB_SQL,
    TERMINAL_JOB_STATUSES,
    open_job_sql,
    terminal_job_sql,
)
from tests.fixtures import FrostVaultTestCase, seed_job, seed_vault

# One Job per status a plain row can hold (the consent and approval states
# need their request rows and are open by the same rule).
SEEDED = {
    "queued": "upload",
    "retrying": "upload",
    "uploading": "upload",
    "verifying": "upload",
    "cleaning": "free-space",
    "restoring": "recover",
    "downloading": "recover",
    "completed": "upload",
    "failed": "upload",
    "cancelled": "upload",
}


class JobStateVocabularyTests(unittest.TestCase):
    def test_the_predicate_is_the_text_of_the_schema_index(self):
        # ``jobs_one_active_per_file_uq`` is declared with this exact text.
        self.assertEqual(OPEN_JOB_SQL, "status NOT IN ('completed', 'failed', 'cancelled')")
        self.assertEqual(TERMINAL_JOB_SQL, "status IN ('completed', 'failed', 'cancelled')")

    def test_an_alias_only_qualifies_the_column(self):
        self.assertEqual(open_job_sql("j"), "j." + OPEN_JOB_SQL)
        self.assertEqual(terminal_job_sql("peer"), "peer." + TERMINAL_JOB_SQL)

    def test_a_held_status_is_never_terminal(self):
        self.assertEqual(TERMINAL_JOB_STATUSES, ("completed", "failed", "cancelled"))
        self.assertFalse(JOB_LEASE_HELD_STATUSES & set(TERMINAL_JOB_STATUSES))


class OpenJobPredicateTests(FrostVaultTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            self.vault = seed_vault(connection)
            for status, action in SEEDED.items():
                seed_job(connection, vault_id=self.vault["id"], path=f"{status}.bin",
                         action=action, status=status)

    def statuses(self, where: str) -> set[str]:
        with self.database.connect() as connection:
            return {row["status"] for row in connection.execute(
                f"SELECT j.status FROM jobs j WHERE {where}")}

    def test_open_and_terminal_split_every_status_like_the_python_set(self):
        terminal = set(TERMINAL_JOB_STATUSES)
        self.assertEqual(self.statuses(open_job_sql("j")), set(SEEDED) - terminal)
        self.assertEqual(self.statuses(terminal_job_sql("j")), terminal)
        self.assertEqual(self.statuses(OPEN_JOB_SQL), set(SEEDED) - terminal)
        self.assertEqual(self.statuses(TERMINAL_JOB_SQL), terminal)

    def test_a_file_holds_one_open_job_and_any_number_of_terminal_ones(self):
        with self.database.connect() as connection:
            for status in TERMINAL_JOB_STATUSES:
                seed_job(connection, vault_id=self.vault["id"], path="completed.bin",
                         status=status)
            seed_job(connection, vault_id=self.vault["id"], path="completed.bin",
                     status="queued")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.database.connect() as connection:
                seed_job(connection, vault_id=self.vault["id"], path="queued.bin",
                         status="queued")


if __name__ == "__main__":
    unittest.main()
