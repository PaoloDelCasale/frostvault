"""Read-only protection vocabulary shared by browse, directory rollups and stats.

These states are observations, never authorization for deleting either copy.
The canonical aliases in SQL are ``f`` (the one ``files`` row of a Vault File,
with its Local Copy and explicit states) and ``cur`` (its Current Snapshot,
joined through ``f.current_version_id`` with :data:`CURRENT_JOIN_SQL`).

Current is an explicit pointer, never "the latest version" chosen by ordering
(ADR-0035): a file without Current has no recoverable cloud content, whatever
other Archive Version rows (a replacement in progress, superseded versions
awaiting exact cleanup) it still has.
"""
from __future__ import annotations

from typing import Any, Mapping

from .operation_policies import stopped_upload_sql
from .upload_path_rules import inline_eligibility_sql

# "excluded" is a Local Copy the automatic upload never admits (fixed noise such
# as ._* or .DS_Store, or a Vault exclude glob). It is no pending work and never
# counts against the Vault's protection; a manual upload stays possible.
# "unsupported" is a local entry FrostVault cannot archive (a symlink, which is
# never followed, or a FIFO, socket or device). It is not a failure of
# protection: like excluded it never counts against the Vault (#524).
# "stopped" is a Stopped Upload (#630): a present Local Copy whose upload the
# Owner stopped, kept out of automatic upload until its content changes. It is
# neither pending work nor protected; a manual upload stays possible.
STATES = ("pending", "protected", "problem", "unconfirmed", "cloud_only", "stopped", "excluded",
          "unsupported")
CURRENT_JOIN_SQL = "LEFT JOIN archive_versions cur ON cur.id = f.current_version_id"
LOCAL_EXISTS_SQL = "coalesce(f.local_presence, 'missing') IN ('present', 'unsupported')"
# Present implies a regular file (files_local_type_presence_ck); matched implies
# a fingerprinted Local Copy equal to Current (files_matched_ck).
PROTECTED_SQL = """(
    f.local_presence='present'
    AND cur.integrity='verified' AND cur.availability='available'
    AND f.matched_version_id=cur.id
    AND f.local_sha256 IS NOT NULL
    AND f.local_sha256=cur.plaintext_sha256
)"""
# The file's most recent upload Job failed (a Job, not an Archive Version: no
# recoverable history is ever derived from this ordering).
FAILED_UPLOAD_SQL = """coalesce((
    SELECT j.status FROM jobs j
    WHERE j.file_id=f.id AND j.action='upload'
    ORDER BY j.id DESC LIMIT 1
), '') = 'failed'"""
PROBLEM_SQL = f"""(
    cur.availability='missing'
    OR f.held_version_id IS NOT NULL
    OR f.reconciliation_block IS NOT NULL
    OR (NOT coalesce({PROTECTED_SQL}, FALSE) AND ({FAILED_UPLOAD_SQL}))
)"""
# A file is listed while it has a Local Copy, a Current, a replacement in
# progress or a blocked reconciliation: a missing Current stays visible.
VISIBLE_SQL = f"""(({LOCAL_EXISTS_SQL})
    OR f.current_version_id IS NOT NULL
    OR f.replacing_version_id IS NOT NULL
    OR f.reconciliation_block IS NOT NULL)"""


def path_excluded_sql(policy: Any = None) -> str:
    """True for a path the automatic upload never admits (needs alias ``f``)."""
    return f"NOT ({inline_eligibility_sql(policy)})"


def state_sql(policy: Any = None, *, stopped: bool = True) -> str:
    """The shared state CASE; ``policy`` supplies the Vault's upload globs.

    None applies only the fixed noise exclusions. The expression embeds the
    Vault's path rules, so a caller must derive it from that Vault's policy and
    refresh derived projections when the policy's globs change.

    ``stopped`` is the predicate that keeps automatic admission off the File
    (#630), so "will not start by itself" and "shown as stopped" cannot
    disagree. It depends on Jobs, which do not invalidate the directory
    projection: that projection passes ``stopped=False`` (a Stopped Upload is
    counted as pending there) and readers move the live Stopped Uploads out
    of pending (``directory_aggregates.stopped_upload_counts``).
    """
    stopped_branch = (
        f"WHEN f.local_presence='present' AND {stopped_upload_sql('f')} THEN 'stopped'"
        if stopped else ""
    )
    return f"""CASE
    WHEN {PROBLEM_SQL} THEN 'problem'
    WHEN cur.availability='unknown' THEN 'unconfirmed'
    WHEN f.local_presence='unsupported' THEN 'unsupported'
    WHEN NOT ({LOCAL_EXISTS_SQL}) AND cur.id IS NOT NULL THEN 'cloud_only'
    WHEN {PROTECTED_SQL} THEN 'protected'
    WHEN f.local_presence='present' AND {path_excluded_sql(policy)} THEN 'excluded'
    {stopped_branch}
    ELSE 'pending'
END"""


def aggregate_state(counts: Mapping[str, int]) -> str | None:
    """Worst actionable observation; counts remain the authoritative summary.

    Excluded and unsupported entries never lower the result; they are reported
    only when nothing else is present, so a folder of ``.DS_Store`` files or
    symlinks is not "protected". A Stopped Upload ranks below pending work and
    above protected: it is no queue, but its content is not in the cloud.
    """
    return next((state for state in ("problem", "unconfirmed", "pending", "stopped", "protected",
                                      "cloud_only", "unsupported", "excluded")
                 if counts.get(state, 0) > 0), None)


def current_is_recoverable(row: Mapping[str, Any]) -> bool:
    """Current exists, is verified and can be located: the one recoverable copy."""
    return (
        row.get("current_version_id") is not None
        and row.get("integrity") == "verified"
        and row.get("availability") == "available"
    )


def local_copy_matches_current(row: Mapping[str, Any]) -> bool:
    """The present Local Copy is fingerprinted and matched to a verified Current."""
    current = row.get("current_version_id")
    return (
        current is not None
        and row.get("local_presence") == "present"
        and row.get("matched_version_id") == current
        and row.get("local_sha256") is not None
        and row.get("local_sha256") == row.get("version_sha256")
    )


def project_file(row: Mapping[str, Any], *, unconfirmed: bool = False) -> dict[str, Any]:
    """Overlay loss of observability without changing the catalog's last evidence."""
    known = str(row["protection_state"])
    last_known = known
    # Availability unknown does not erase the last full content verification.
    if known == "unconfirmed" and row.get("integrity") == "verified":
        presence = row.get("local_presence")
        if local_copy_matches_current(row):
            last_known = "protected"
        elif presence not in {"present", "unsupported"}:
            last_known = "cloud_only"
        elif presence == "unsupported":
            last_known = "unsupported"
        else:
            # Unknown availability hides the rule-based state; the path and
            # the stopping Job still tell.
            last_known = ("excluded" if row.get("path_excluded")
                          else "stopped" if row.get("upload_stopped") else "pending")
    return {
        "state": "unconfirmed" if unconfirmed else known,
        "last_known_state": last_known,
        "last_known_at": row.get("protection_observed_at"),
        "previous_snapshot_protected": bool(
            known in ("pending", "stopped", "excluded", "unsupported") and current_is_recoverable(row)
        ),
    }


def observation_is_unconfirmed(health: Mapping[str, Mapping[str, Any]]) -> bool:
    """A failed source scan or cloud request prevents confirming protection.

    An audit that successfully located a missing copy is a known problem, not
    a loss of observability. Only its request failure makes all files unknown.
    """
    return any(bool((health.get(component) or {}).get("last_error"))
               for component in ("source", "cloud", "audit"))
