"""What signing in and Reauthentication do with the Emergency Password (#798).

Each test fixes one branch of ``POST /api/login`` or ``POST /api/reauth`` from
the outside: the answer, the statements sent to the authentication tables with
their commits, the audit events and the throttle counters left behind.
"""
from __future__ import annotations

import re
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import psycopg

from app import backoff
from app.backoff import OWNER_ACCOUNT_KEY, reauth_account_key, reauth_ip_key
from tests.fixtures import FrostVaultTestCase


PASSWORD = "correct-horse-battery"
WRONG_PASSWORD = "not-the-emergency-password"
LOOPBACK_IP = "127.0.0.1"
OUTSIDE_IP = "203.0.113.7"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
STALE_REAUTH_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)

_TABLES = ("auth_backoff", "owner", "sessions", "audit_events")
_TARGET = re.compile(r"\b(?:FROM|INTO|UPDATE)\s+(\w+)")

GUARD = "SELECT auth_backoff"
OWNER_ROW = "SELECT owner"
FAILURE = "INSERT auth_backoff"
SUCCESS = "DELETE auth_backoff"
AUDIT = "INSERT audit_events"
COMMIT = "COMMIT"


@contextmanager
def authentication_statements() -> Iterator[list[str]]:
    """Collect, in order, the statements on the authentication tables and the commits."""
    seen: list[str] = []
    execute = psycopg.Cursor.execute
    commit = psycopg.Connection.commit
    rollback = psycopg.Connection.rollback

    def recording_execute(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        text = " ".join(str(query).split())
        target = _TARGET.search(text)
        if target and target.group(1) in _TABLES:
            seen.append(f"{text.split()[0]} {target.group(1)}")
        return execute(cursor, query, *args, **kwargs)

    def recording_commit(connection: Any) -> None:
        seen.append(COMMIT)
        commit(connection)

    def recording_rollback(connection: Any) -> None:
        seen.append("ROLLBACK")
        rollback(connection)

    with (
        patch.object(psycopg.Cursor, "execute", recording_execute),
        patch.object(psycopg.Connection, "commit", recording_commit),
        patch.object(psycopg.Connection, "rollback", recording_rollback),
    ):
        yield seen


class EmergencyPasswordHttpCase(FrostVaultTestCase):
    owner = {"display_name": "Alice", "password": PASSWORD}
    settings_overrides = {"break_glass_allowed_cidrs": ""}

    def seed_counter(
        self, scope: str, key: str, failures: int, *, wait_seconds: int | None = None
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO auth_backoff(scope, key, failure_count, next_allowed_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    scope,
                    key,
                    failures,
                    NOW + timedelta(seconds=wait_seconds) if wait_seconds else None,
                    NOW,
                ),
            )

    def counters(self) -> dict[tuple[str, str], int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT scope, key, failure_count FROM auth_backoff"
            ).fetchall()
        return {(row["scope"], row["key"]): row["failure_count"] for row in rows}

    def audit_events(self) -> list[tuple[str, str, str | None, dict[str, Any]]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT event, actor, outcome, detail FROM audit_events ORDER BY id"
            ).fetchall()
        return [(row["event"], row["actor"], row["outcome"], row["detail"]) for row in rows]

    def session_count(self) -> int:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) AS total FROM sessions"
            ).fetchone()["total"]


class SignInTests(EmergencyPasswordHttpCase):
    ip_key = ("ip", LOOPBACK_IP)
    account_key = ("account", OWNER_ACCOUNT_KEY)

    def login(self, password: str, *, ip: str = LOOPBACK_IP):
        client = self.http_client(client=(ip, 50000))
        with authentication_statements() as statements, patch(
            "app.backoff._now", return_value=NOW
        ):
            response = client.post("/api/login", json={"password": password})
        return response, statements, client

    def test_outside_the_allowed_networks_only_the_denial_is_recorded(self) -> None:
        response, statements, client = self.login(PASSWORD, ip=OUTSIDE_IP)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "The emergency password is not accepted from this network"},
        )
        self.assertEqual(statements, [AUDIT, COMMIT])
        self.assertEqual(
            self.audit_events(),
            [("break_glass_denied", "emergency", None, {"ip": OUTSIDE_IP})],
        )
        self.assertEqual(self.counters(), {})
        self.assertEqual(self.session_count(), 0)
        self.assertIsNone(client.cookies.get(self.settings.session_cookie_name))

    def test_wrong_password_counts_one_failure_per_dimension_and_commits(self) -> None:
        response, statements, _client = self.login(WRONG_PASSWORD)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Incorrect emergency password"})
        self.assertNotIn("Retry-After", response.headers)
        self.assertEqual(
            statements,
            [GUARD, GUARD, OWNER_ROW, FAILURE, FAILURE, AUDIT, COMMIT],
        )
        self.assertEqual(
            self.audit_events(),
            [("break_glass_failed", "emergency", None, {"ip": LOOPBACK_IP})],
        )
        self.assertEqual(self.counters(), {self.ip_key: 1, self.account_key: 1})
        self.assertEqual(self.session_count(), 0)

    def test_owner_without_an_emergency_password_fails_like_a_wrong_password(self) -> None:
        for change in ("UPDATE owner SET password_hash=NULL WHERE id=1", "DELETE FROM owner"):
            with self.subTest(change=change):
                with self.database.connect() as connection:
                    connection.execute(change)
                    connection.execute("DELETE FROM auth_backoff")
                    connection.execute("TRUNCATE audit_events")

                response, statements, _client = self.login(PASSWORD)

                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": "Incorrect emergency password"})
                self.assertEqual(
                    statements,
                    [GUARD, GUARD, OWNER_ROW, FAILURE, FAILURE, AUDIT, COMMIT],
                )
                self.assertEqual(
                    self.audit_events(),
                    [("break_glass_failed", "emergency", None, {"ip": LOOPBACK_IP})],
                )
                self.assertEqual(self.counters(), {self.ip_key: 1, self.account_key: 1})

    def test_correct_password_clears_the_counters_and_starts_a_session(self) -> None:
        self.seed_counter(*self.ip_key, 2)
        self.seed_counter(*self.account_key, 3)
        # Reauthentication counters are not the sign-in's to clear.
        self.seed_counter("ip", reauth_ip_key(LOOPBACK_IP), 2)

        response, statements, client = self.login(PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message_key"], "api.signed_in")
        self.assertEqual(
            statements,
            [
                GUARD,
                GUARD,
                OWNER_ROW,
                SUCCESS,
                SUCCESS,
                "INSERT sessions",
                "SELECT sessions",
                COMMIT,
            ],
        )
        self.assertEqual(self.audit_events(), [])
        self.assertEqual(self.counters(), {("ip", reauth_ip_key(LOOPBACK_IP)): 2})
        self.assertEqual(self.session_count(), 1)
        self.assertTrue(client.cookies.get(self.settings.session_cookie_name))
        self.assertTrue(client.cookies.get(self.settings.csrf_cookie_name))

    def test_the_failure_that_reaches_the_threshold_is_still_a_401(self) -> None:
        self.seed_counter(*self.ip_key, backoff.THRESHOLD - 1)
        self.seed_counter(*self.account_key, backoff.THRESHOLD - 1)

        response, statements, _client = self.login(WRONG_PASSWORD)

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("Retry-After", response.headers)
        self.assertEqual(
            statements,
            [GUARD, GUARD, OWNER_ROW, FAILURE, FAILURE, AUDIT, COMMIT],
        )
        self.assertEqual(
            self.counters(),
            {self.ip_key: backoff.THRESHOLD, self.account_key: backoff.THRESHOLD},
        )

    def test_a_blocked_address_is_refused_before_the_account_is_looked_at(self) -> None:
        self.seed_counter(*self.ip_key, backoff.THRESHOLD, wait_seconds=30)
        self.seed_counter(*self.account_key, backoff.THRESHOLD + 1, wait_seconds=90)

        # Even the correct password waits, and the wait is the address's own.
        response, statements, client = self.login(PASSWORD)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"detail": "Too many attempts; try again later"})
        self.assertEqual(response.headers["Retry-After"], "30")
        # The block is audited in a second transaction, after the first commit.
        self.assertEqual(statements, [GUARD, COMMIT, AUDIT, COMMIT])
        self.assertEqual(
            self.audit_events(),
            [(
                "auth_backoff_blocked",
                "emergency",
                None,
                {"flow": "break_glass", "ip": LOOPBACK_IP, "retry_after": 30},
            )],
        )
        # A blocked attempt is not counted and the password is never checked.
        self.assertEqual(
            self.counters(),
            {self.ip_key: backoff.THRESHOLD, self.account_key: backoff.THRESHOLD + 1},
        )
        self.assertEqual(self.session_count(), 0)
        self.assertIsNone(client.cookies.get(self.settings.session_cookie_name))

    def test_a_blocked_account_refuses_an_address_that_is_free(self) -> None:
        self.seed_counter(*self.account_key, backoff.THRESHOLD + 1, wait_seconds=90)

        response, statements, _client = self.login(PASSWORD)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "90")
        self.assertEqual(statements, [GUARD, GUARD, COMMIT, AUDIT, COMMIT])
        self.assertEqual(
            [(event, detail) for event, _actor, _outcome, detail in self.audit_events()],
            [(
                "auth_backoff_blocked",
                {"flow": "break_glass", "ip": LOOPBACK_IP, "retry_after": 90},
            )],
        )
        self.assertEqual(self.counters(), {self.account_key: backoff.THRESHOLD + 1})


class ReauthenticationTests(EmergencyPasswordHttpCase):
    ip_key = ("ip", reauth_ip_key(LOOPBACK_IP))
    account_key = ("account", reauth_account_key())
    # What the CSRF check and the Session dependency send before the route body runs.
    SESSION = ["SELECT sessions", COMMIT, "SELECT sessions", "UPDATE sessions", COMMIT]

    def setUp(self) -> None:
        super().setUp()
        self.client = self.http_client(client=(LOOPBACK_IP, 50000))
        self.sign_in(auth_method="local", reauthenticated_until=STALE_REAUTH_AT.isoformat())

    def reauth(self, password: str, *, ip: str = LOOPBACK_IP):
        client = self.http_client(client=(ip, 50000))
        client.cookies.update(self.client.cookies)
        with authentication_statements() as statements, patch(
            "app.backoff._now", return_value=NOW
        ):
            response = client.post(
                "/api/reauth",
                json={"password": password},
                headers=self.signed_in.csrf_headers,
            )
        self.assertEqual(statements[:len(self.SESSION)], self.SESSION, statements)
        return response, statements[len(self.SESSION):]

    def reauth_at(self) -> datetime:
        with self.database.connect() as connection:
            return connection.execute("SELECT reauth_at FROM sessions").fetchone()["reauth_at"]

    def test_outside_the_allowed_networks_nothing_is_recorded(self) -> None:
        response, statements = self.reauth(PASSWORD, ip=OUTSIDE_IP)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(), {"detail": "Password reauthentication is not allowed here"}
        )
        self.assertEqual(statements, [])
        self.assertEqual(self.audit_events(), [])
        self.assertEqual(self.counters(), {})
        self.assertEqual(self.reauth_at(), STALE_REAUTH_AT)

    def test_wrong_password_counts_one_failure_per_dimension_and_commits(self) -> None:
        response, statements = self.reauth(WRONG_PASSWORD)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Incorrect password"})
        self.assertNotIn("Retry-After", response.headers)
        # After counting, both dimensions are read again for a fresh block.
        self.assertEqual(
            statements,
            [GUARD, GUARD, OWNER_ROW, FAILURE, FAILURE, GUARD, GUARD, AUDIT, COMMIT],
        )
        self.assertEqual(
            self.audit_events(),
            [("reauth_failed", "emergency", "failure", {"flow": "reauth", "ip": LOOPBACK_IP})],
        )
        self.assertEqual(self.counters(), {self.ip_key: 1, self.account_key: 1})
        self.assertEqual(self.reauth_at(), STALE_REAUTH_AT)

    def test_owner_without_an_emergency_password_fails_like_a_wrong_password(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE owner SET password_hash=NULL WHERE id=1")

        response, statements = self.reauth(PASSWORD)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Incorrect password"})
        self.assertEqual(
            statements,
            [GUARD, GUARD, OWNER_ROW, FAILURE, FAILURE, GUARD, GUARD, AUDIT, COMMIT],
        )
        self.assertEqual(
            self.audit_events(),
            [("reauth_failed", "emergency", "failure", {"flow": "reauth", "ip": LOOPBACK_IP})],
        )
        self.assertEqual(self.counters(), {self.ip_key: 1, self.account_key: 1})
        self.assertEqual(self.reauth_at(), STALE_REAUTH_AT)

    def test_correct_password_clears_the_counters_and_marks_the_session(self) -> None:
        self.seed_counter(*self.ip_key, 2)
        self.seed_counter(*self.account_key, 3)
        # Sign-in counters are not Reauthentication's to clear.
        self.seed_counter("account", OWNER_ACCOUNT_KEY, 2)

        response, statements = self.reauth(PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Reauthenticated"})
        self.assertEqual(
            statements,
            [GUARD, GUARD, OWNER_ROW, SUCCESS, SUCCESS, "UPDATE sessions", AUDIT, COMMIT],
        )
        self.assertEqual(
            self.audit_events(),
            [("reauth_succeeded", "emergency", "success", {"flow": "reauth", "ip": LOOPBACK_IP})],
        )
        self.assertEqual(self.counters(), {("account", OWNER_ACCOUNT_KEY): 2})
        self.assertNotEqual(self.reauth_at(), STALE_REAUTH_AT)

    def test_the_failure_that_reaches_the_threshold_is_itself_throttled(self) -> None:
        self.seed_counter(*self.ip_key, backoff.THRESHOLD - 1)
        self.seed_counter(*self.account_key, backoff.THRESHOLD - 1)

        response, statements = self.reauth(WRONG_PASSWORD)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"detail": "Too many attempts; try again later"})
        self.assertEqual(response.headers["Retry-After"], str(backoff.BASE_SECONDS))
        # The failure and the block are audited in the one transaction.
        self.assertEqual(
            statements,
            [GUARD, GUARD, OWNER_ROW, FAILURE, FAILURE, GUARD, GUARD, AUDIT, AUDIT, COMMIT],
        )
        self.assertEqual(
            self.audit_events(),
            [
                ("reauth_failed", "emergency", "failure", {"flow": "reauth", "ip": LOOPBACK_IP}),
                (
                    "auth_backoff_blocked",
                    "emergency",
                    None,
                    {"flow": "reauth", "ip": LOOPBACK_IP, "retry_after": backoff.BASE_SECONDS},
                ),
            ],
        )
        self.assertEqual(
            self.counters(),
            {self.ip_key: backoff.THRESHOLD, self.account_key: backoff.THRESHOLD},
        )
        self.assertEqual(self.reauth_at(), STALE_REAUTH_AT)

    def test_a_blocked_dimension_refuses_with_the_longest_wait(self) -> None:
        self.seed_counter(*self.ip_key, backoff.THRESHOLD, wait_seconds=30)
        self.seed_counter(*self.account_key, backoff.THRESHOLD + 1, wait_seconds=90)

        # Even the correct password waits; both dimensions are always read.
        response, statements = self.reauth(PASSWORD)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"detail": "Too many attempts; try again later"})
        self.assertEqual(response.headers["Retry-After"], "90")
        self.assertEqual(statements, [GUARD, GUARD, AUDIT, COMMIT])
        self.assertEqual(
            self.audit_events(),
            [(
                "auth_backoff_blocked",
                "emergency",
                None,
                {"flow": "reauth", "ip": LOOPBACK_IP, "retry_after": 90},
            )],
        )
        # A blocked attempt is not counted and the password is never checked.
        self.assertEqual(
            self.counters(),
            {self.ip_key: backoff.THRESHOLD, self.account_key: backoff.THRESHOLD + 1},
        )
        self.assertEqual(self.reauth_at(), STALE_REAUTH_AT)


if __name__ == "__main__":
    unittest.main()
