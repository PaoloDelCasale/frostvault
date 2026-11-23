"""Verifying the Emergency Password under throttle (ADR-0004, ADR-0005).

Signing in and Reauthentication both ask the same question, and
:func:`verify_emergency_password` is its only implementation. The order of
its checks is the security property:

1. the network gate: outside loopback and the allowed LAN CIDRs nothing is
   read, counted or verified;
2. the throttle: a blocked address or Owner waits, whatever the password;
3. one Argon2 verification, against the Owner's hash or against a dummy hash
   when there is none, so every attempt that gets here costs the same time;
4. the counters: a failure is counted, a success forgets the failures.

Steps 2 to 4 run in one transaction, committed before the outcome is
returned: the caller maps the outcome to its answer afterwards, so refusing
never rolls back the failure that was just counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .backoff import (
    OWNER_ACCOUNT_KEY,
    BackoffError,
    guard,
    reauth_account_key,
    reauth_ip_key,
    record_failure,
    record_success,
)
from .breakglass import is_break_glass_allowed
from .database import db
from .security import DUMMY_PASSWORD_HASH, verify_password


@dataclass(frozen=True)
class Accepted:
    """The Emergency Password is the Owner's."""


@dataclass(frozen=True)
class Rejected:
    """Wrong password, or an Owner without one; the failure is counted."""


@dataclass(frozen=True)
class Throttled:
    """The address or the Owner must wait ``retry_after`` seconds."""

    retry_after: int
    # True when this attempt was verified, rejected and counted, and that very
    # failure started the wait. False when it was refused before verification.
    password_rejected: bool = False


@dataclass(frozen=True)
class BreakGlassDenied:
    """The Emergency Password is not accepted from this network."""


Outcome = Accepted | Rejected | Throttled | BreakGlassDenied


@dataclass(frozen=True)
class Throttle:
    """Which counters an attempt uses, and how their waits are reported."""

    ip_key: Callable[[str], str]
    account_key: str
    # Read every counter and report the longest wait, rather than stopping at
    # the first blocked one.
    longest_wait: bool
    # Answer the failure that reaches the threshold with its own wait.
    throttles_the_tripping_failure: bool


# The two differ on purpose, as they did before sharing this module: their
# counters are separate, so a Reauthentication can never reset the sign-in's.
SIGN_IN = Throttle(
    ip_key=str,
    account_key=OWNER_ACCOUNT_KEY,
    longest_wait=False,
    throttles_the_tripping_failure=False,
)
REAUTHENTICATION = Throttle(
    ip_key=reauth_ip_key,
    account_key=reauth_account_key(),
    longest_wait=True,
    throttles_the_tripping_failure=True,
)


def verify_emergency_password(
    password: str,
    *,
    client_ip: str | None,
    throttle: Throttle,
    in_transaction: Callable[[Any, Outcome], None] | None = None,
) -> Outcome:
    """Verify ``password`` for ``client_ip`` and return what happened.

    ``in_transaction(connection, outcome)`` runs in the transaction of the
    counters, after them: it is where the caller starts or marks the Session
    and writes its audit events, so they commit together. It must not raise to
    refuse the attempt, or the counted failure is rolled back with it. It is
    not called when the network gate denies, because no transaction is opened.
    """
    if not is_break_glass_allowed(client_ip):
        return BreakGlassDenied()
    counters = (
        ("ip", throttle.ip_key(client_ip)),
        ("account", throttle.account_key),
    )
    with db() as connection:
        outcome = _verify_under_throttle(connection, password, counters, throttle)
        if in_transaction is not None:
            in_transaction(connection, outcome)
    return outcome


def _verify_under_throttle(
    connection: Any,
    password: str,
    counters: tuple[tuple[str, str], ...],
    throttle: Throttle,
) -> Outcome:
    retry_after = _wait(connection, counters, longest=throttle.longest_wait)
    if retry_after is not None:
        return Throttled(retry_after)
    owner = connection.execute(
        "SELECT password_hash FROM owner WHERE id=1"
    ).fetchone()
    eligible = bool(owner and owner["password_hash"])
    password_valid = verify_password(
        owner["password_hash"] if eligible else DUMMY_PASSWORD_HASH, password
    )
    # Always reject the dummy path, even if its hash ever matches the
    # submitted password (or verification is replaced in a test).
    if eligible and password_valid:
        for scope, key in counters:
            record_success(connection, scope=scope, key=key)
        return Accepted()
    for scope, key in counters:
        record_failure(connection, scope=scope, key=key)
    if throttle.throttles_the_tripping_failure:
        retry_after = _wait(connection, counters, longest=throttle.longest_wait)
        if retry_after is not None:
            return Throttled(retry_after, password_rejected=True)
    return Rejected()


def _wait(
    connection: Any, counters: tuple[tuple[str, str], ...], *, longest: bool
) -> int | None:
    """Seconds the attempt must wait, or ``None`` when no counter blocks it."""
    waits: list[int] = []
    for scope, key in counters:
        try:
            guard(connection, scope=scope, key=key)
        except BackoffError as blocked:
            if not longest:
                return blocked.retry_after
            waits.append(blocked.retry_after)
    return max(waits) if waits else None
