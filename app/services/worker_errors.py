"""Worker failures become generic internal-error problems, not a diagnostic store.

The Owner inbox stays free of exception messages (they may carry credentials
or URLs): it names only the exception type, and tells the Owner to check the
server log, where the full exception with its traceback is written, once, at
the point of recording.
"""
from __future__ import annotations

import logging
from typing import Any

from .problems import report_problem

_logger = logging.getLogger(__name__)


def log_worker_error(
    component: str, exc: BaseException, vault_id: int | None = None,
) -> None:
    """Write one worker failure, with traceback, to the server log.

    Only the component, the Vault id and the exception itself are logged;
    caller-supplied ``detail`` values are deliberately never included.
    """
    _logger.error(
        "Worker %s failed (vault_id=%s)", component, vault_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def log_report_failure(
    component: str, error: BaseException, *, resolving: bool = False,
) -> None:
    """Log that the inbox problem itself could not be written (or closed)."""
    _logger.error(
        "Could not %s the internal-error problem for worker %s",
        "resolve" if resolving else "record", component,
        exc_info=(type(error), error, error.__traceback__),
    )


def error_type(exc: BaseException) -> str:
    """The safe identity of a failure: its class, and SQLSTATE for PostgreSQL.

    Never the message, which may carry credentials, URLs or paths (#711).
    """
    name = type(exc).__name__
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate.isalnum() and len(sqlstate) == 5:
        return f"{name}, SQLSTATE {sqlstate}"
    return name


def record_worker_error(
    connection: Any, *, component: str, exc: BaseException,
    vault_id: int | None = None, log: bool = True, **detail: Any,
) -> dict[str, Any] | None:
    # Log first: the notification points the Owner at the server log, and the
    # exception must be there even when no Owner exists yet or the DB write fails.
    if log:
        log_worker_error(component, exc, vault_id)
    # Keep exception data out of the inbox; the component is enough to identify
    # the failing worker without a dedicated error page or exception taxonomy.
    return report_problem(
        connection, event="internal_error", vault_id=vault_id, component=component,
        error_type=error_type(exc),
    )
