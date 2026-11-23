"""What a failed read of the aggregate status means for the file listing (#804).

``ensure_directory_aggregates`` reads ``directory_aggregate_status`` first; the
file listing of a Vault is its only caller. The read is made to fail here by
taking the table away, the "partial schema" the old fallback was written for.
"""
from __future__ import annotations

import psycopg
from psycopg.pq import TransactionStatus

from app.services import directory_aggregates
from tests.fixtures import FrostVaultTestCase, seed_vault


class AggregateStatusReadFailureTests(FrostVaultTestCase):
    def setUp(self) -> None:
        super().setUp()
        directory_aggregates.set_maintenance_scheduling_enabled(False)
        self.addCleanup(directory_aggregates.set_maintenance_scheduling_enabled, True)
        with self.database.connect() as connection:
            seed_vault(connection, id=1, name="Alpha", source_root=str(self.tmp_path / "alpha"))
        self.take_status_table_away()

    def take_status_table_away(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "ALTER TABLE directory_aggregate_status RENAME TO directory_aggregate_status_away")

    def give_status_table_back(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "ALTER TABLE directory_aggregate_status_away RENAME TO directory_aggregate_status")

    def test_failed_status_read_is_swallowed_and_the_next_statement_fails(self) -> None:
        """Today the caller never gets a fallback value: PostgreSQL has aborted
        the transaction, so the statement after the swallowed error raises
        ``InFailedSqlTransaction`` and the real cause is lost."""
        connection = self.enterContext(self.database.connect())
        with self.assertRaises(psycopg.errors.InFailedSqlTransaction):
            directory_aggregates.ensure_directory_aggregates(connection, 1)
        self.assertEqual(connection.info.transaction_status, TransactionStatus.INERROR)
        connection.rollback()

    def test_file_listing_answers_500_and_recovers_with_the_table(self) -> None:
        client = self.http_client(raise_server_exceptions=False)
        self.addCleanup(client.close)
        self.sign_in(client)

        failed = client.get("/api/vaults/1/files")
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(failed.text, "Internal Server Error")

        self.give_status_table_back()
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) AS n FROM directory_aggregate_status").fetchone()["n"],
                0,
            )
        listed = client.get("/api/vaults/1/files")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["aggregate_status"], "loading")
        self.assertEqual(listed.json()["items"], [])
