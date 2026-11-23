"""Automatic Duplicate Cleanup: a per-Vault, opt-in daily run (#726, ADR-0048).

Off by default. Once the Owner enables it (with a fresh authentication), each
active, unpaused Vault runs it once a day: the Cloud Duplicates whose Local
Copy was last seen at least ``grace_days`` ago, and that no other work already
covers, become one Delayed Cloud Deletion marked automatic. The usual seven-day
cancellable delay and the execution re-checks follow; at execution a member
that is no longer a Cloud Duplicate is skipped alone.

A run whose candidates exceed the Vault's cap (files or bytes) schedules
nothing and opens a Vault Problem asking the Owner to review them through the
manual «Delete from cloud» flow with «Only Cloud Duplicates». The Problem closes at
the next run under the cap, or when the cleanup is turned off.

``duplicate_cleanup_last_run_at`` is persisted with the run, so a restart
never runs a Vault again early.

The status also counts, live, the Cloud Duplicates there are now, those still
in their wait and those never seen locally (which the cleanup never deletes),
and reads the totals already deleted automatically, kept on the Vault so they
outlive the history retention of their Jobs (#729).

Deleting a version within its Minimum Storage Duration saves nothing: the
status says how many Cloud Duplicates are still within it and what deleting
them still bills (#731). With ``beyond_minimum`` (off by default) a run
schedules only the duplicates whose minimum has ended by the time they would
be deleted; the others stay held, at no extra cost, until it ends.
"""
from __future__ import annotations

from typing import Any

from . import cleanup_engine, cloud_deletion
from .notifications import ProblemFamily


class DuplicateCleanupError(cleanup_engine.CleanupError):
    """The settings cannot be changed: ``reason`` names the Owner-facing message."""

    prefix = "api.duplicate_cleanup"
    reasons = frozenset({"stopped"})


def _candidates(connection: Any, vault: dict[str, Any],
                settings: cleanup_engine.Settings) -> list[dict[str, Any]]:
    """The Cloud Duplicates there are now, as the manual «Only Cloud
    Duplicates» preview of the whole Vault counts them (#729)."""
    return cloud_deletion.cloud_duplicates(connection, vault)[0]


def _select(connection: Any, vault: dict[str, Any], settings: cleanup_engine.Settings, *,
            seen_before: str, now: Any):
    return cloud_deletion.automatic_duplicates(
        connection, vault, seen_before=seen_before,
        beyond_minimum=settings["beyond_minimum"], now=now)


CLEANUP = cleanup_engine.Cleanup(
    name="duplicate_cleanup", grace_days_range=(1, 90), error=DuplicateCleanupError,
    mirror=False, candidates=_candidates, select=_select)

POLL_SECONDS = cleanup_engine.POLL_SECONDS
NOTICE_EVENT = CLEANUP.notice_event
problem_key = CLEANUP.problem_key
# A capped Automatic Duplicate Cleanup (#726) closes with the next run under
# the cap (``duplicate_cleanup.run_vault``) or once the cleanup is off.
PROBLEM_FAMILY = ProblemFamily(frozenset({CLEANUP.problem_event}), """
    n.event='duplicate_cleanup_capped' AND NOT EXISTS (
        SELECT 1 FROM vaults v WHERE v.id=n.vault_id AND v.duplicate_cleanup_enabled
    )
""")
status = CLEANUP.status
update_settings = CLEANUP.update_settings
due_vaults = CLEANUP.due_vaults
run_vault = CLEANUP.run_vault
run_due = CLEANUP.run_due
