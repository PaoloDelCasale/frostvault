"""Worker failures become generic internal-error problems, not a diagnostic store."""
from __future__ import annotations

from typing import Any

from .problems import report_problem


def record_worker_error(
    connection: Any, *, component: str, exc: BaseException,
    vault_id: int | None = None, **detail: Any,
) -> dict[str, Any] | None:
    # Keep exception data out of the inbox; the component is enough to identify
    # the failing worker without a dedicated error page or exception taxonomy.
    return report_problem(
        connection, event="internal_error", vault_id=vault_id, component=component,
    )
