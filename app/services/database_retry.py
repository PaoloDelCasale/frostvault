"""Bounded replay of complete DB-only units, never provider/filesystem I/O."""
from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import psycopg

P = ParamSpec("P")
T = TypeVar("T")
DATABASE_RETRY_MAX_ATTEMPTS = 8


def exception_chain(error: BaseException) -> Iterator[BaseException]:
    """Walk structured wrappers (including suppressed context), cycle-safely."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for nested in (current.__cause__, current.__context__):
            if nested is not None:
                pending.append(nested)


def is_retryable_transaction_error(error: BaseException) -> bool:
    return any(
        isinstance(item, psycopg.Error) and item.sqlstate in {"40P01", "40001"}
        for item in exception_chain(error)
    )


@contextmanager
def owned_connection(connection: Any) -> Iterator[Any]:
    """Always close an owned handle, including when its COMMIT raises.

    psycopg's Connection.__exit__ commits before close; a commit-time 40001
    otherwise bypasses close. ``db()`` handles qualify: a pooled one returns
    its connection once, so the second close is a no-op. Never use this for
    caller-owned handles. Lightweight recording seams may implement only the
    context protocol.
    """
    try:
        with connection as active:
            yield active
    finally:
        close = getattr(connection, "close", None)
        if close is not None:
            close()


def retry_database_unit(operation: Callable[P, T]) -> Callable[P, T]:
    """Replay only operations that own and close their entire DB transaction.

    Callers must NOT include S3/FS effects, outer caller-owned transactions or
    results mutated outside the transaction. The connection context must have
    rolled back/closed before an exception reaches this wrapper. Commit errors
    are included; statement-only/savepoint retries are intentionally forbidden.
    Exhaustion propagates unchanged; it must not trigger whole-upload replay.
    """
    @wraps(operation)
    def run(*args: P.args, **kwargs: P.kwargs) -> T:
        for attempt in range(DATABASE_RETRY_MAX_ATTEMPTS):
            try:
                return operation(*args, **kwargs)
            except Exception as error:
                if (not is_retryable_transaction_error(error)
                        or attempt + 1 == DATABASE_RETRY_MAX_ATTEMPTS):
                    raise
                time.sleep(random.uniform(0.005, min(0.25, 0.01 * 2 ** attempt)))
        raise AssertionError("unreachable")
    return run
