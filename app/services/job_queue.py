"""Job queue: the one vocabulary of Job states (#794).

A Job is *open* until it reaches a terminal status; an open Job that runs holds
a live lease (``job_leases``), every other open Job waits for a claim. The
terminal set and the SQL predicate "open Job" are defined here and nowhere
else, so admission, claim, cancellation and every reader agree on which Jobs
still count as work (ADR-0026, ADR-0031).

This Module imports nothing of the application: the catalog, the storage
worker and every service can use it.
"""
from __future__ import annotations

# A terminal Job never moves again and holds no lease (the schema drops it).
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
# A Job in one of these states is running and holds a live lease
# (``job_leases``); every other non-terminal state waits for a claim.
JOB_LEASE_HELD_STATUSES = frozenset(
    {"downloading", "uploading", "verifying", "cleaning"}
)

# The terminal set as a SQL list: ``('completed', 'failed', 'cancelled')``.
TERMINAL_JOB_STATUSES_SQL = (
    "(" + ", ".join(f"'{status}'" for status in TERMINAL_JOB_STATUSES) + ")"
)
# "Open Job" on an unqualified ``jobs`` row.
OPEN_JOB_SQL = f"status NOT IN {TERMINAL_JOB_STATUSES_SQL}"
TERMINAL_JOB_SQL = f"status IN {TERMINAL_JOB_STATUSES_SQL}"


def open_job_sql(alias: str) -> str:
    """"Open Job" on the ``jobs`` row named ``alias`` in the caller's query."""
    return f"{alias}.{OPEN_JOB_SQL}"


def terminal_job_sql(alias: str) -> str:
    """"Terminal Job" on the ``jobs`` row named ``alias`` in the caller's query."""
    return f"{alias}.{TERMINAL_JOB_SQL}"
