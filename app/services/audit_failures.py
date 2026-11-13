"""A failed catalog audit: its visible Problem and its retry backoff (#620).

A failed audit leaves every file of its Vault unconfirmed and blocks Free
space. The Owner sees one Problem per Vault with a safe reason, until an audit
of that Vault succeeds. The audit is retried within minutes, never only at the
next weekly pass.
"""
from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from ..i18n import translate
from . import cloud_preflight_gate
from .notifications import set_problem

EVENT = "catalog_audit_failed"
CLOUD_REQUEST_KEY = "audit.failure.cloud_request"
UNEXPECTED_KEY = "audit.failure.unexpected"
RETRY_MIN_SECONDS = 5 * 60
RETRY_MAX_SECONDS = 60 * 60


def retry_seconds(failures: int) -> int:
    """5, 10, 20, 40 minutes, then hourly while the audit keeps failing."""
    return min(RETRY_MAX_SECONDS, RETRY_MIN_SECONDS * 2 ** min(max(failures - 1, 0), 4))


def problem_params(exc: BaseException) -> dict[str, str]:
    """The reason as safe structured context, never the provider's message.

    A blocked readiness gate carries its own locally composed remediation; any
    other failure is named by its kind only, since provider messages may hold
    URLs or credentials.
    """
    if isinstance(exc, cloud_preflight_gate.CloudPreflightBlocked):
        return cloud_preflight_gate._problem_params(str(exc))
    key = CLOUD_REQUEST_KEY if isinstance(exc, (ClientError, BotoCoreError)) else UNEXPECTED_KEY
    return {"remediation": translate(key, locale="en"), "remediation_key": key}


def set_audit_problem(
    connection: Any, *, vault_id: int, active: bool, params: dict[str, str] | None = None,
) -> None:
    # In-app only: a blocked gate already sends its one installation email.
    set_problem(
        connection, key=f"audit:{int(vault_id)}", event=EVENT, vault_id=int(vault_id),
        active=active, params=params or {}, email=False,
    )
