"""Worker failures become generic internal-error problems, not a diagnostic store.

The Owner inbox stays free of exception data (it may carry credentials or
URLs) and tells the Owner to check the server log, so the full exception with
its traceback is written there, once, at the point of recording.
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


def record_worker_error(
    connection: Any, *, component: str, exc: BaseException,
    vault_id: int | None = None, log: bool = True, **detail: Any,
) -> dict[str, Any] | None:
    # Log first: the notification points the Owner at the server log, and the
    # exception must be there even when no Owner exists yet or the DB write fails.
    if log:
        log_worker_error(component, exc, vault_id)
    # Startup can fail before the singleton is bootstrapped. Do not invent a
    # recipient then. This exceptional path is not a per-file Owner lookup.
    if not connection.execute("SELECT 1 FROM owner WHERE id=1").fetchone():
        return None
    # Keep exception data out of the inbox; the component is enough to identify
    # the failing worker without a dedicated error page or exception taxonomy.
    return report_problem(
        connection, event="internal_error", vault_id=vault_id, component=component,
    )
