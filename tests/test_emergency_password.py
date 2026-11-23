"""Verifying the Emergency Password under throttle, without HTTP (#798)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from app import backoff
from app.backoff import OWNER_ACCOUNT_KEY, reauth_account_key, reauth_ip_key
from app.emergency_password import (
    REAUTHENTICATION,
    SIGN_IN,
    Accepted,
    BreakGlassDenied,
    Outcome,
    Rejected,
    Throttle,
    Throttled,
    verify_emergency_password,
)
from app.security import DUMMY_PASSWORD_HASH
from app.services.audit_events import audit_log
from tests.fixtures import FrostVaultTestCase, override_settings


PASSWORD = "correct-horse-battery"
WRONG_PASSWORD = "not-the-emergency-password"
LOOPBACK_IP = "127.0.0.1"
LAN_IP = "192.168.1.20"
OUTSIDE_IP = "203.0.113.7"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)

# The counters of each throttle for an attempt from loopback: address, Owner.
COUNTERS = {
    SIGN_IN: (("ip", LOOPBACK_IP), ("account", OWNER_ACCOUNT_KEY)),
    REAUTHENTICATION: (
        ("ip", reauth_ip_key(LOOPBACK_IP)),
        ("account", reauth_account_key()),
    ),
}


class EmergencyPasswordTests(FrostVaultTestCase):
    owner = {"password": PASSWORD}
    settings_overrides = {"break_glass_allowed_cidrs": ""}

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch("app.backoff._now", return_value=NOW))
        self.seen: list[Outcome] = []

    def verify(
        self, password: str, throttle: Throttle, *, client_ip: str | None = LOOPBACK_IP
    ) -> Outcome:
        def in_transaction(connection: Any, outcome: Outcome) -> None:
            self.seen.append(outcome)
            # A write of the caller, to see it commit with the counters.
            audit_log(
                "break_glass_failed",
                connection=connection,
                actor="emergency",
                seen=type(outcome).__name__,
            )

        return verify_emergency_password(
            password, client_ip=client_ip, throttle=throttle, in_transaction=in_transaction
        )

    def seed_counter(
        self, counter: tuple[str, str], failures: int, *, wait_seconds: int | None = None
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO auth_backoff(scope, key, failure_count, next_allowed_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    *counter,
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

    def caller_writes(self) -> list[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT detail->>'seen' AS seen FROM audit_events ORDER BY id"
            ).fetchall()
        return [row["seen"] for row in rows]

    def remove_emergency_password(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE owner SET password_hash=NULL WHERE id=1")

    # --- the network gate comes first --------------------------------------

    def test_outside_the_allowed_networks_nothing_is_read_counted_or_verified(self) -> None:
        for throttle in (SIGN_IN, REAUTHENTICATION):
            for client_ip in (OUTSIDE_IP, LAN_IP, None, "not-an-address"):
                with self.subTest(throttle=throttle, client_ip=client_ip):
                    with (
                        patch("app.emergency_password.db") as database,
                        patch("app.emergency_password.verify_password") as verify,
                    ):
                        outcome = self.verify(PASSWORD, throttle, client_ip=client_ip)

                    self.assertEqual(outcome, BreakGlassDenied())
                    database.assert_not_called()
                    verify.assert_not_called()
        self.assertEqual(self.seen, [])
        self.assertEqual(self.counters(), {})

    def test_an_allowed_lan_address_is_verified_and_counted_under_its_own_key(self) -> None:
        with override_settings(break_glass_allowed_cidrs="192.168.1.0/24"):
            self.assertEqual(self.verify(PASSWORD, SIGN_IN, client_ip=LAN_IP), Accepted())
            self.assertEqual(
                self.verify(WRONG_PASSWORD, SIGN_IN, client_ip=LAN_IP), Rejected()
            )
            self.assertEqual(
                self.verify(WRONG_PASSWORD, REAUTHENTICATION, client_ip=LAN_IP), Rejected()
            )
        self.assertEqual(
            self.counters(),
            {
                ("ip", LAN_IP): 1,
                ("account", OWNER_ACCOUNT_KEY): 1,
                ("ip", reauth_ip_key(LAN_IP)): 1,
                ("account", reauth_account_key()): 1,
            },
        )

    # --- then the throttle, before any verification ------------------------

    def test_a_blocked_attempt_is_neither_verified_nor_counted(self) -> None:
        # The sign-in stops at the first blocked counter; Reauthentication
        # reads both and reports the longest wait.
        for throttle, expected in ((SIGN_IN, 30), (REAUTHENTICATION, 90)):
            with self.subTest(throttle=throttle):
                address, owner = COUNTERS[throttle]
                self.seed_counter(address, backoff.THRESHOLD, wait_seconds=30)
                self.seed_counter(owner, backoff.THRESHOLD + 1, wait_seconds=90)

                with patch("app.emergency_password.verify_password") as verify:
                    outcome = self.verify(PASSWORD, throttle)

                self.assertEqual(outcome, Throttled(expected))
                self.assertFalse(outcome.password_rejected)
                verify.assert_not_called()
                self.assertEqual(self.counters()[address], backoff.THRESHOLD)
                self.assertEqual(self.counters()[owner], backoff.THRESHOLD + 1)

    def test_either_counter_alone_blocks(self) -> None:
        for throttle in (SIGN_IN, REAUTHENTICATION):
            for blocked in COUNTERS[throttle]:
                with self.subTest(throttle=throttle, blocked=blocked):
                    with self.database.connect() as connection:
                        connection.execute("DELETE FROM auth_backoff")
                    self.seed_counter(blocked, backoff.THRESHOLD, wait_seconds=45)

                    with patch("app.emergency_password.verify_password") as verify:
                        outcome = self.verify(PASSWORD, throttle)

                    self.assertEqual(outcome, Throttled(45))
                    verify.assert_not_called()
                    self.assertEqual(self.counters(), {blocked: backoff.THRESHOLD})

    # --- then one verification, and the counters ---------------------------

    def test_the_correct_password_is_accepted_and_forgets_the_failures(self) -> None:
        for throttle in (SIGN_IN, REAUTHENTICATION):
            with self.subTest(throttle=throttle):
                for counter in COUNTERS[throttle]:
                    self.seed_counter(counter, 3)

                self.assertEqual(self.verify(PASSWORD, throttle), Accepted())

                for counter in COUNTERS[throttle]:
                    self.assertNotIn(counter, self.counters())

    def test_a_wrong_password_is_rejected_and_counted_on_both_counters(self) -> None:
        for throttle in (SIGN_IN, REAUTHENTICATION):
            with self.subTest(throttle=throttle):
                self.assertEqual(self.verify(WRONG_PASSWORD, throttle), Rejected())
                self.assertEqual(self.verify(WRONG_PASSWORD, throttle), Rejected())

                for counter in COUNTERS[throttle]:
                    self.assertEqual(self.counters()[counter], 2)

    def test_without_an_emergency_password_the_dummy_hash_is_verified_and_rejected(
        self,
    ) -> None:
        self.remove_emergency_password()
        for throttle in (SIGN_IN, REAUTHENTICATION):
            with self.subTest(throttle=throttle):
                # Returning True proves the dummy verification never accepts.
                with patch(
                    "app.emergency_password.verify_password", return_value=True
                ) as verify:
                    outcome = self.verify(PASSWORD, throttle)

                self.assertEqual(outcome, Rejected())
                verify.assert_called_once_with(DUMMY_PASSWORD_HASH, PASSWORD)
                for counter in COUNTERS[throttle]:
                    self.assertEqual(self.counters()[counter], 1)

    def test_without_an_owner_the_dummy_hash_is_verified_and_rejected(self) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM owner")
        for throttle in (SIGN_IN, REAUTHENTICATION):
            with self.subTest(throttle=throttle):
                with patch(
                    "app.emergency_password.verify_password", return_value=True
                ) as verify:
                    outcome = self.verify(PASSWORD, throttle)

                self.assertEqual(outcome, Rejected())
                verify.assert_called_once_with(DUMMY_PASSWORD_HASH, PASSWORD)

    def test_every_verified_attempt_costs_exactly_one_verification(self) -> None:
        for throttle in (SIGN_IN, REAUTHENTICATION):
            for password in (PASSWORD, WRONG_PASSWORD):
                with self.subTest(throttle=throttle, password=password):
                    with patch(
                        "app.emergency_password.verify_password", return_value=False
                    ) as verify:
                        self.verify(password, throttle)

                    self.assertEqual(verify.call_count, 1)

    def test_the_failure_that_reaches_the_threshold(self) -> None:
        # The sign-in still answers "rejected"; Reauthentication answers with
        # the wait that this failure started.
        for throttle, expected in (
            (SIGN_IN, Rejected()),
            (REAUTHENTICATION, Throttled(backoff.BASE_SECONDS, password_rejected=True)),
        ):
            with self.subTest(throttle=throttle):
                for counter in COUNTERS[throttle]:
                    self.seed_counter(counter, backoff.THRESHOLD - 1)

                self.assertEqual(self.verify(WRONG_PASSWORD, throttle), expected)

                for counter in COUNTERS[throttle]:
                    self.assertEqual(self.counters()[counter], backoff.THRESHOLD)
                # From here on even the correct password waits.
                self.assertEqual(
                    self.verify(PASSWORD, throttle), Throttled(backoff.BASE_SECONDS)
                )

    def test_reauthentication_and_sign_in_never_touch_each_other_s_counters(self) -> None:
        for throttle, other in ((SIGN_IN, REAUTHENTICATION), (REAUTHENTICATION, SIGN_IN)):
            with self.subTest(throttle=throttle):
                with self.database.connect() as connection:
                    connection.execute("DELETE FROM auth_backoff")
                for counter in COUNTERS[other]:
                    self.seed_counter(counter, backoff.THRESHOLD, wait_seconds=600)

                # Not blocked by the other's counters, and a success keeps them.
                self.assertEqual(self.verify(WRONG_PASSWORD, throttle), Rejected())
                self.assertEqual(self.verify(PASSWORD, throttle), Accepted())

                self.assertEqual(
                    self.counters(),
                    {counter: backoff.THRESHOLD for counter in COUNTERS[other]},
                )

    # --- the caller's part of the transaction ------------------------------

    def test_the_caller_sees_every_outcome_in_the_committed_transaction(self) -> None:
        self.assertEqual(self.verify(WRONG_PASSWORD, SIGN_IN), Rejected())
        self.assertEqual(self.verify(PASSWORD, SIGN_IN), Accepted())
        self.seed_counter(COUNTERS[SIGN_IN][0], backoff.THRESHOLD, wait_seconds=30)
        self.assertEqual(self.verify(PASSWORD, SIGN_IN), Throttled(30))

        self.assertEqual(self.seen, [Rejected(), Accepted(), Throttled(30)])
        self.assertEqual(self.caller_writes(), ["Rejected", "Accepted", "Throttled"])

    def test_a_caller_that_raises_rolls_the_counted_failure_back(self) -> None:
        """Why callers map the outcome to an error only after the return."""
        def refuse(connection: Any, outcome: Outcome) -> None:
            raise RuntimeError("refused inside the transaction")

        with self.assertRaises(RuntimeError):
            verify_emergency_password(
                WRONG_PASSWORD, client_ip=LOOPBACK_IP, throttle=SIGN_IN, in_transaction=refuse
            )

        self.assertEqual(self.counters(), {})

    def test_the_caller_part_is_optional(self) -> None:
        outcome = verify_emergency_password(
            WRONG_PASSWORD, client_ip=LOOPBACK_IP, throttle=REAUTHENTICATION
        )

        self.assertEqual(outcome, Rejected())
        self.assertEqual(self.counters(), dict.fromkeys(COUNTERS[REAUTHENTICATION], 1))


if __name__ == "__main__":
    unittest.main()
