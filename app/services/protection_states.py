"""Read-only protection vocabulary shared by browse, directory rollups and stats.

These states are observations, never authorization for deleting either copy.
The canonical aliases in SQL are vf (Vault File), lc (Local Copy), av (the
explicit replacement, or Current Snapshot, INCLUDING missing copies).
"""
from __future__ import annotations

from typing import Any, Mapping

from .upload_path_rules import inline_eligibility_sql

# "excluded" is a Local Copy the automatic upload never admits (fixed noise such
# as ._* or .DS_Store, or a Vault exclude glob). It is no pending work and never
# counts against the Vault's protection; a manual upload stays possible.
# "unsupported" is a local entry FrostVault cannot archive (a symlink, which is
# never followed, or a FIFO, socket or device). It is not a failure of
# protection: like excluded it never counts against the Vault (#524).
STATES = ("pending", "protected", "problem", "unconfirmed", "cloud_only", "excluded", "unsupported")
LOCAL_EXISTS_SQL = "coalesce(lc.presence, 'missing') IN ('present', 'unsupported')"
PROTECTED_SQL = """(
    lc.presence='present' AND lc.file_type='regular'
    AND av.integrity='verified' AND av.availability='available'
    AND lc.matched_archive_version_id=av.id
    AND lc.plaintext_sha256 IS NOT NULL
    AND lc.plaintext_sha256=av.plaintext_sha256
)"""
FAILED_UPLOAD_SQL = """coalesce((
    SELECT j.status FROM jobs j
    WHERE j.vault_file_id=vf.id AND j.action='upload'
    ORDER BY j.id DESC LIMIT 1
), '') = 'failed'"""
PROBLEM_SQL = f"""(
    av.availability='missing' OR av.integrity='mismatch'
    OR lc.cloud_only_version_id IS NOT NULL
    OR (NOT coalesce({PROTECTED_SQL}, FALSE) AND ({FAILED_UPLOAD_SQL}))
)"""


def path_excluded_sql(policy: Any = None) -> str:
    """True for a path the automatic upload never admits (needs alias ``fp``)."""
    return f"NOT ({inline_eligibility_sql(policy)})"


def state_sql(policy: Any = None) -> str:
    """The shared state CASE; ``policy`` supplies the Vault's upload globs.

    None applies only the fixed noise exclusions. The expression embeds the
    Vault's path rules, so a caller must derive it from that Vault's policy and
    refresh derived projections when the policy's globs change.
    """
    return f"""CASE
    WHEN {PROBLEM_SQL} THEN 'problem'
    WHEN av.availability='unknown' THEN 'unconfirmed'
    WHEN lc.presence='unsupported' THEN 'unsupported'
    WHEN NOT ({LOCAL_EXISTS_SQL}) AND av.id IS NOT NULL THEN
        CASE WHEN av.integrity='verified' AND av.availability='available'
             THEN 'cloud_only' ELSE 'unconfirmed' END
    WHEN {PROTECTED_SQL} THEN 'protected'
    WHEN lc.presence='present' AND {path_excluded_sql(policy)} THEN 'excluded'
    ELSE 'pending'
END"""


# A missing snapshot must remain visible even with no Local Copy.
VISIBLE_SQL = f"(({LOCAL_EXISTS_SQL}) OR av.id IS NOT NULL)"
LATEST_JOIN_SQL = """
    LEFT JOIN archive_versions av
      ON av.id=COALESCE(vf.replacing_version_id, vf.current_version_id)
"""


def aggregate_state(counts: Mapping[str, int]) -> str | None:
    """Worst actionable observation; counts remain the authoritative summary.

    Excluded and unsupported entries never lower the result; they are reported
    only when nothing else is present, so a folder of ``.DS_Store`` files or
    symlinks is not "protected".
    """
    return next((state for state in ("problem", "unconfirmed", "pending", "protected", "cloud_only",
                                      "unsupported", "excluded")
                 if counts.get(state, 0) > 0), None)


def project_file(row: Mapping[str, Any], *, unconfirmed: bool = False) -> dict[str, Any]:
    """Overlay loss of observability without changing the catalog's last evidence."""
    known = str(row["protection_state"])
    last_known = known
    # Availability unknown does not erase the last full content verification.
    if known == "unconfirmed" and row.get("integrity") == "verified":
        last_known = "protected" if (
            row.get("local_presence") == "present"
            and row.get("matched_archive_version_id") == row.get("archive_version_id")
            and row.get("local_sha256") is not None
            and row.get("local_sha256") == row.get("version_sha256")
        ) else "cloud_only" if row.get("local_presence") not in {"present", "unsupported"} else (
            "unsupported" if row.get("local_presence") == "unsupported" else
            # Unknown availability hides the rule-based state; the path still tells.
            "excluded" if row.get("path_excluded") else "pending"
        )
    return {
        "state": "unconfirmed" if unconfirmed else known,
        "last_known_state": last_known,
        "last_known_at": row.get("protection_observed_at"),
        "previous_snapshot_protected": bool(
            known in ("pending", "excluded", "unsupported") and (
                int(row.get("recoverable_version_count") or 0) > 0
                or (row.get("integrity") == "verified" and row.get("availability") == "available")
            )
        ),
    }


def observation_is_unconfirmed(health: Mapping[str, Mapping[str, Any]]) -> bool:
    """A failed source scan or cloud request prevents confirming protection.

    An audit that successfully located a missing copy is a known problem, not
    a loss of observability. Only its request failure makes all files unknown.
    """
    return any(bool((health.get(component) or {}).get("last_error"))
               for component in ("source", "cloud", "audit"))
