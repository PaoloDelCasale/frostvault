from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row

from app.database import SQLiteConnection
from app.lookup_rate_limit import check_lookup_rate_limit
from tests.test_database import run_alembic


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL", "")
REPOSITORY_ROOT = Path(__file__).parents[1]


def _psycopg_url() -> str:
    return POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _migration_url(schema: str) -> str:
    parts = urlsplit(POSTGRES_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def run_postgresql_alembic(
    schema: str,
    revision: str = "head",
    command: str = "upgrade",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            f"database_url={_migration_url(schema)}",
            command,
            revision,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class SharedLookupRateLimitTests(unittest.TestCase):
    def test_independent_app_instances_share_the_ten_attempt_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lookup-rate-limit.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(id, username, display_name, password_hash, is_admin)
                    VALUES (7, 'owner', 'Owner', 'hash', TRUE)
                    """
                )

            def lookup_from_app_instance(now: float) -> int | None:
                # Each call represents an application instance/request with a
                # separate database connection. Only the database is shared.
                with SQLiteConnection(str(path)) as connection:
                    return check_lookup_rate_limit(
                        connection,
                        user_id=7,
                        client_ip="203.0.113.10",
                        backend="sqlite",
                        now=now,
                    )

            results = [
                lookup_from_app_instance(1000.0 + number / 10)
                for number in range(11)
            ]

            self.assertEqual(results[:10], [None] * 10)
            self.assertEqual(results[10], 59)

    def test_concurrent_workers_cannot_admit_more_than_ten_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent-lookup-rate-limit.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(id, username, display_name, password_hash, is_admin)
                    VALUES (7, 'owner', 'Owner', 'hash', TRUE)
                    """
                )

            def lookup_from_worker(_: int) -> int | None:
                with SQLiteConnection(str(path)) as connection:
                    return check_lookup_rate_limit(
                        connection,
                        user_id=7,
                        client_ip="203.0.113.10",
                        backend="sqlite",
                        now=1000.0,
                    )

            with ThreadPoolExecutor(max_workers=20) as workers:
                results = list(workers.map(lookup_from_worker, range(20)))

            self.assertEqual(results.count(None), 10)
            self.assertEqual(sum(result is not None for result in results), 10)


@unittest.skipUnless(POSTGRES_URL, "TEST_POSTGRES_URL is not configured")
class PostgreSQLSharedLookupRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = f"lookup_rate_limit_{uuid.uuid4().hex}"
        try:
            with psycopg.connect(
                _psycopg_url(),
                connect_timeout=3,
                autocommit=True,
            ) as connection:
                connection.execute(f'CREATE SCHEMA "{self.schema}"')
        except psycopg.OperationalError as exc:
            self.skipTest(f"PostgreSQL service unavailable: {exc}")

        try:
            migrated = run_postgresql_alembic(self.schema)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO users(id, username, display_name, password_hash, is_admin)
                    VALUES
                        (7, 'owner', 'Owner', 'hash', TRUE),
                        (8, 'other-owner', 'Other Owner', 'hash', TRUE)
                    """
                )
        except Exception:
            self._drop_schema()
            raise

    def tearDown(self) -> None:
        self._drop_schema()

    def _drop_schema(self) -> None:
        try:
            with psycopg.connect(
                _psycopg_url(),
                connect_timeout=3,
                autocommit=True,
            ) as connection:
                connection.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        except psycopg.Error:
            # The service may have gone away after the test; there is then
            # nothing this test can clean up locally.
            pass

    def _connection(self) -> psycopg.Connection:
        return psycopg.connect(
            _psycopg_url(),
            row_factory=dict_row,
            options=f"-csearch_path={self.schema},public",
        )

    def _lookup(self, user_id: int, client_ip: str) -> int | None:
        with self._connection() as connection:
            return check_lookup_rate_limit(
                connection,
                user_id=user_id,
                client_ip=client_ip,
                backend="postgresql",
                now=1000.0,
            )

    def test_independent_users_and_ips_have_separate_postgresql_budgets(self) -> None:
        shared_ip = "203.0.113.10"
        other_ip = "203.0.113.11"

        saturated_pair = [self._lookup(7, shared_ip) for _ in range(10)]
        self.assertEqual(saturated_pair, [None] * 10)
        self.assertEqual(self._lookup(7, shared_ip), 60)

        for user_id, client_ip in ((8, shared_ip), (7, other_ip)):
            with self.subTest(user_id=user_id, client_ip=client_ip):
                attempts = [self._lookup(user_id, client_ip) for _ in range(10)]
                self.assertEqual(attempts, [None] * 10)
                self.assertEqual(self._lookup(user_id, client_ip), 60)

        self.assertEqual(self._lookup(7, shared_ip), 60)

    def test_independent_workers_enforce_ten_attempts_in_one_postgresql_window(self) -> None:
        connections: list[psycopg.Connection] = []
        try:
            for _ in range(20):
                connections.append(self._connection())
            barrier = threading.Barrier(len(connections))

            def lookup_from_app_instance(connection: psycopg.Connection) -> int | None:
                try:
                    with connection:
                        barrier.wait(timeout=30)
                        return check_lookup_rate_limit(
                            connection,
                            user_id=7,
                            client_ip="203.0.113.10",
                            backend="postgresql",
                            now=1000.0,
                        )
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=len(connections)) as workers:
                results = list(workers.map(lookup_from_app_instance, connections))
        finally:
            for connection in connections:
                if not connection.closed:
                    connection.close()

        self.assertEqual(results.count(None), 10)
        self.assertEqual(sum(result is not None for result in results), 10)


if __name__ == "__main__":
    unittest.main()
