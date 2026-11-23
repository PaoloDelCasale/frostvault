"""How every family of Problems closes: the one list, given to the inbox (#804).

Each module that opens a family of Problems declares, next to the code that
opens them, the predicate that says one is resolved. The inbox
(:mod:`app.services.notifications`) runs the single UPDATE and imports none of
those modules; this module is the explicit place where they meet.

``install()`` is called by the application (:mod:`app.main`). Nothing is
registered as a side effect of an import, so the families never depend on
which module was loaded first; an inbox that was given none refuses to
reconcile instead of leaving Problems open.
"""
from __future__ import annotations

from . import (
    audit_failures, catalog_incidents, directory_aggregates, duplicate_cleanup, mirror_cleanup,
    notifications, problems, upload_failure_problems, upload_provenance, vault_buckets,
)

# The order is the order of the predicates in the reconciling UPDATE.
FAMILIES: tuple[notifications.ProblemFamily, ...] = (
    upload_failure_problems.JOB_FAILED_FAMILY,
    upload_failure_problems.JOB_FAILED_GROUP_FAMILY,
    catalog_incidents.PROBLEM_FAMILY,
    problems.FILE_UNREADABLE_FAMILY,
    audit_failures.PROBLEM_FAMILY,
    upload_provenance.RECONCILIATION_PROBLEM_FAMILY,
    duplicate_cleanup.PROBLEM_FAMILY,
    mirror_cleanup.PROBLEM_FAMILY,
    vault_buckets.PROBLEM_FAMILY,
)


def install() -> None:
    """Give the inbox the families and what else it needs from their modules."""
    notifications.install_problem_domain(notifications.ProblemDomain(
        families=FAMILIES,
        closed_upload_failure_sql=upload_failure_problems.CLOSED_UPLOAD_FAILURE_SQL,
        forget_unit_dirty_marks=directory_aggregates.forget_unit_dirty_marks,
    ))
