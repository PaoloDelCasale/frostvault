"""The removal of a Vault's catalog, from its two callers (#804, ADR-0047).

A Vault Deletion whose prefix is proven empty and the cancellation of a Vault
creation both remove every catalog row of one Vault, in foreign-key order, in
the transaction that removes the Vault. These tests fix what the database is
asked to do, statement by statement, and that no other Vault loses a row.

Real catalog on a disposable PostgreSQL 16 database; the S3 transports are the
synthetic providers of the Vault Deletion and Vault Bucket tests.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from app.services import notifications as inbox
from app.services import source_layout, vault_buckets, vault_deletion
from app.services import vaults as vaults_service
from tests.fake_bucket_s3 import fake_vault_buckets
from tests.fixtures import FrostVaultTestCase, seed_job, seed_vault
from tests.test_vault_deletion import VaultDeletionFixture

BEGIN = "BEGIN"
# Every statement that removes the catalog of one Vault, in the order sent.
REMOVAL = [
    "DELETE FROM notifications WHERE vault_id=%(v)s OR job_id IN"
    " (SELECT id FROM jobs WHERE vault_id=%(v)s)",
    "DELETE FROM cleanup_candidates WHERE file_id IN"
    " (SELECT id FROM files WHERE vault_id=%(v)s)",
    "DELETE FROM cleanup_batches WHERE vault_id=%(v)s",
    "DELETE FROM upload_remediations WHERE vault_id=%(v)s",
    "DELETE FROM jobs WHERE vault_id=%(v)s",
    "DELETE FROM job_groups WHERE vault_id=%(v)s",
    "UPDATE files SET current_version_id=NULL, replacing_version_id=NULL,"
    " matched_version_id=NULL, held_version_id=NULL"
    " WHERE vault_id=%(v)s AND (current_version_id IS NOT NULL"
    " OR replacing_version_id IS NOT NULL OR matched_version_id IS NOT NULL"
    " OR held_version_id IS NOT NULL)",
    "DELETE FROM archive_versions WHERE file_id IN"
    " (SELECT id FROM files WHERE vault_id=%(v)s)",
    "DELETE FROM upload_attempts WHERE file_id IN"
    " (SELECT id FROM files WHERE vault_id=%(v)s)",
    "DELETE FROM upload_snapshots WHERE vault_id=%(v)s",
    "DELETE FROM copy_attempts WHERE vault_id=%(v)s",
    "DELETE FROM files WHERE vault_id=%(v)s",
]
COUNTS = (
    "SELECT (SELECT count(*) FROM files WHERE vault_id=%(v)s) AS files,"
    " (SELECT count(*) FROM archive_versions av JOIN files f ON f.id=av.file_id"
    " WHERE f.vault_id=%(v)s) AS archive_versions,"
    " (SELECT count(*) FROM jobs WHERE vault_id=%(v)s) AS jobs"
)
# One row per table a Vault's catalog spans, for the Vault left alone.
KEPT_TABLES = ("vaults", "files", "jobs", "notifications")


class _Recorded:
    """A connection that notes each statement, whitespace collapsed, then runs it."""

    def __init__(self, connection: Any, log: list[str]) -> None:
        self._connection = connection
        self._log = log

    def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self._log.append(" ".join(str(query).split()))
        return self._connection.execute(query, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def recording(connect: Callable[[], Any], log: list[str]) -> Callable[[], Any]:
    """``connect`` as a database whose transactions are noted in ``log``."""

    @contextmanager
    def database() -> Iterator[_Recorded]:
        with connect() as connection:
            log.append(BEGIN)
            yield _Recorded(connection, log)

    return database


def removing_transaction(log: list[str]) -> list[str]:
    """The statements of the one transaction that removed the Vault row."""
    transactions: list[list[str]] = []
    for statement in log:
        if statement == BEGIN:
            transactions.append([])
        else:
            transactions[-1].append(statement)
    [found] = [t for t in transactions if any(s.startswith("DELETE FROM vaults") for s in t)]
    return found


def writes(statements: list[str]) -> list[str]:
    return [s for s in statements if s.split(" ", 1)[0] in ("INSERT", "UPDATE", "DELETE")]


def seed_other_vault(connection: Any) -> int:
    """A second Vault with a Vault File, a Job and a Problem of its own."""
    other = seed_vault(connection, name="Photos", source_root="/sources/managed/photos",
                       bucket="other-bucket")
    job_id = seed_job(connection, vault_id=other["id"], path="kept.jpg")
    inbox.set_problem(connection, key=f"kept:{other['id']}", event="protection_delayed",
                      vault_id=other["id"], active=True, email=False,
                      params={"pending": 1, "stalled": 0, "overdue": 0, "unstable": 0})
    connection.execute(
        "INSERT INTO notifications(event, title, job_id) VALUES ('job_failed', 'Kept', %s)",
        (job_id,))
    return int(other["id"])


def kept_rows(connection: Any, vault_id: int) -> dict[str, int]:
    counts = {table: connection.execute(
        f"SELECT count(*) AS n FROM {table} WHERE {'id' if table == 'vaults' else 'vault_id'}=%s",
        (vault_id,)).fetchone()["n"] for table in KEPT_TABLES}
    counts["job notifications"] = connection.execute(
        "SELECT count(*) AS n FROM notifications n JOIN jobs j ON j.id=n.job_id "
        "WHERE j.vault_id=%s", (vault_id,)).fetchone()["n"]
    return counts


class VaultDeletionCatalogRemovalTests(VaultDeletionFixture):

    def test_an_emptied_prefix_removes_the_catalog_in_foreign_key_order(self) -> None:
        with self.db() as connection:
            other = seed_other_vault(connection)
            inbox.set_problem(connection, key="delay:2", event="protection_delayed", vault_id=2,
                              active=True, email=False,
                              params={"pending": 1, "stalled": 0, "overdue": 0, "unstable": 0})
            before = kept_rows(connection, other)
        self.assertEqual(before, {"vaults": 1, "files": 1, "jobs": 1, "notifications": 1,
                                  "job notifications": 1})
        self.stop()
        self.request()
        log: list[str] = []
        self.assertEqual(
            vault_deletion.run_due(recording(self.db, log), lambda: self.provider), 1)

        statements = removing_transaction(log)
        self.assertEqual(statements.count(COUNTS), 1)
        self.assertEqual(writes(statements)[:1],
                         ["UPDATE vault_deletions SET proven_empty_at=now() WHERE vault_id=%s"])
        self.assertEqual(writes(statements)[1:13], REMOVAL)
        self.assertEqual(len(writes(statements)), 15)
        self.assertTrue(writes(statements)[13].startswith("INSERT INTO audit_events"))
        self.assertEqual(writes(statements)[14], "DELETE FROM vaults WHERE id=%s")
        self.assertLess(statements.index(COUNTS), statements.index(REMOVAL[0]))
        # The Vault row is locked before anything is counted or removed.
        self.assertIn("FOR UPDATE OF v", statements[0])

        for table in ("vaults WHERE id=2", "files WHERE vault_id=2", "jobs WHERE vault_id=2",
                      "notifications WHERE vault_id=2", "upload_snapshots WHERE vault_id=2",
                      "copy_attempts WHERE vault_id=2", "vault_deletions"):
            self.assertEqual(self.rows(f"SELECT 1 FROM {table}"), [], table)
        with self.db() as connection:
            self.assertEqual(kept_rows(connection, other), before)
        [audit] = self.rows("SELECT detail FROM audit_events "
                            "WHERE event='folder_protection.vault_deleted'")
        self.assertEqual((audit["detail"]["files"], audit["detail"]["archive_versions"],
                          audit["detail"]["jobs"]), (1, 1, 1))


class CancelledCreationCatalogRemovalTests(FrostVaultTestCase):

    owner = {"display_name": "Alice"}
    settings_overrides = {"aws_region": "eu-south-1"}

    def setUp(self) -> None:
        super().setUp()
        sources_root = self.tmp_path / "sources"
        sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(sources_root)
        source_layout.ensure_managed_directory()
        self.s3 = fake_vault_buckets(self)

    def test_a_cancelled_creation_removes_the_catalog_in_foreign_key_order(self) -> None:
        self.s3.fail["create_bucket"] = "AccessDenied"
        vault = vaults_service.create_vault("Docs")
        with self.database.connect() as connection:
            other = seed_other_vault(connection)
            before = kept_rows(connection, other)
            problems = connection.execute(
                "SELECT count(*) AS n FROM notifications WHERE vault_id=%s",
                (vault["id"],)).fetchone()["n"]
        self.assertEqual(problems, 1, "the failed creation has its open Problem")
        calls = len(self.s3.calls)

        log: list[str] = []
        vault_buckets.cancel_creation(
            vault["id"], database=recording(self.database.connect, log))

        statements = removing_transaction(log)
        self.assertEqual(statements.count(COUNTS), 1)
        self.assertEqual(writes(statements)[:12], REMOVAL)
        self.assertEqual(writes(statements)[12], "DELETE FROM vaults WHERE id=%s")
        self.assertEqual(len(writes(statements)), 14)
        self.assertTrue(writes(statements)[13].startswith("INSERT INTO audit_events"))
        self.assertIn("FOR UPDATE OF v", statements[0])
        self.assertEqual(statements.index(COUNTS), 1)

        with self.database.connect() as connection:
            self.assertEqual(kept_rows(connection, other), before)
            gone = kept_rows(connection, vault["id"])
        self.assertEqual(set(gone.values()), {0})
        self.assertEqual(len(self.s3.calls), calls, "a cancellation sends nothing to S3")
