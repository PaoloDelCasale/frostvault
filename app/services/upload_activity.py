"""Read-only, bounded-result upload activity; never enumerates Jobs or files.

Counts describe currently observed work, not a lifetime/batch completion ratio.
A stability deadline is conditional on unchanged content, not an upload ETA.
Only SQL aggregates cross the service boundary (O(Vaults), not O(files/Jobs)).
"""
from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from typing import Any

from .operation_policies import AUTO_UPLOAD_EXCLUSIONS, get_policy

STAGES = ("stability_wait", "queued", "uploading", "verifying")


def _glob_body(pattern: str) -> str:
    # Python's atomic groups are an optimization, not part of the glob language.
    # PostgreSQL ARE supports noncapturing groups and a leading (?s) flag.
    return fnmatch.translate(pattern)[4:-3].replace("(?>", "(?:")


def _path_pattern(pattern: str) -> str:
    body = _glob_body(pattern)
    alternatives = [f"^(?:{body})$"]
    if "**" in pattern and pattern.endswith("/**"):
        # _path_matches treats this prefix literally, even if it contains '*'.
        import re
        alternatives.append(f"^{re.escape(pattern[:-3])}(?:/|$)")
    if pattern.startswith("**/"):
        alternatives.append(f"(?:^|/)(?:{_glob_body(pattern[3:])})$")
    return "(?s)(?:" + "|".join(alternatives) + ")"


def _eligibility_sql(policy: Any) -> tuple[str, list[str]]:
    """Informational projection of the admission path rules, parametrized AREs."""
    clauses: list[str] = []
    params: list[str] = []
    def match(pattern: str) -> str:
        regex = _path_pattern(pattern)
        params.append(regex)
        if "**" in pattern:
            return "fp.path ~ %s"
        params.append(regex)
        return ("(fp.path ~ %s OR EXISTS (SELECT 1 FROM unnest(string_to_array(fp.path,'/')) "
                "AS parts(part) WHERE part ~ %s))")

    if policy.include_globs:
        clauses.append("(" + " OR ".join(match(pattern) for pattern in policy.include_globs) + ")")
    for pattern in (*AUTO_UPLOAD_EXCLUSIONS, *policy.exclude_globs):
        clauses.append("NOT (" + match(pattern) + ")")
    return " AND ".join(clauses), params


def _empty() -> dict[str, Any]:
    return {**{stage: {"files": 0, "bytes": 0, "transferred_bytes": 0} for stage in STAGES},
            "awaiting_admission_files": 0, "next_stable_at": None}


def upload_activity(connection: Any, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    vaults = connection.execute(
        "SELECT id,name,enabled,decommission_state,relocation_state FROM vaults ORDER BY lower(name),id"
    ).fetchall()
    items = {row["id"]: {"vault_id": row["id"], "vault_name": row["name"], **_empty()} for row in vaults}
    # A single grouped query, with progress clamped per Job before summation.
    # Reconciliation/cleanup is verification work, not another network upload.
    # While the installation S3 gate is blocked no transfer can start: a Job still
    # 'uploading' with nothing sent is a lease left by a stopped process (or one
    # about to be suspended), so it waits like a queued Job (#487). Sent bytes
    # stay visible as uploading until the worker or lease reconciliation moves them.
    for row in connection.execute("""
        WITH gate AS (SELECT COALESCE(
            (SELECT status='blocked' FROM cloud_preflight_state WHERE id=1), false) AS blocked)
        SELECT vault_id, CASE
            WHEN status IN ('verifying','cleaning') OR (status='uploading' AND
                 (upload_reconcile_only OR message_key='job.purging_previous_snapshot')) THEN 'verifying'
            WHEN status='uploading' AND NOT (gate.blocked AND COALESCE(transferred_bytes,0)<=0)
                 THEN 'uploading' ELSE 'queued' END AS stage,
            count(*) AS files, COALESCE(sum(GREATEST(total_bytes,0)),0) AS bytes,
            COALESCE(sum(LEAST(GREATEST(transferred_bytes,0),GREATEST(total_bytes,0))),0) AS transferred_bytes
        FROM jobs CROSS JOIN gate WHERE action='upload' AND status NOT IN ('completed','failed','cancelled')
        GROUP BY 1,2
    """).fetchall():
        if row["vault_id"] in items:
            items[row["vault_id"]][row["stage"]] = {
                key: int(row[key]) for key in ("files", "bytes", "transferred_bytes")}

    for vault in vaults:
        policy = get_policy(connection, vault["id"])
        item = items[vault["id"]]
        if not (policy.auto_upload and vault["enabled"] and vault["decommission_state"] == "active"
                and vault["relocation_state"] == "ready"):
            continue
        rules, params = _eligibility_sql(policy)
        # Match the durable admission observations, not the continuous
        # protection_pending age (which intentionally never resets on writes).
        rows = connection.execute(f"""
            WITH candidates AS (
                SELECT lc.size, GREATEST(to_timestamp(lc.mtime_ns::double precision / 1000000000),
                    COALESCE(lc.content_observed_at,lc.observed_at)::timestamptz)
                    + %s * interval '1 second' AS stable_at
                FROM vault_files vf JOIN local_copies lc ON lc.vault_file_id=vf.id
                JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                LEFT JOIN archive_versions av ON av.id=lc.matched_archive_version_id
                WHERE vf.vault_id=%s AND vf.status='active' AND lc.presence='present'
                    AND lc.file_type='regular' AND lc.cloud_only_version_id IS NULL
                    AND (av.availability IS NULL OR av.availability<>'purged')
                    AND NOT (lc.matched_archive_version_id IS NOT NULL AND lc.plaintext_sha256 IS NOT NULL
                        AND av.integrity IS NOT DISTINCT FROM 'verified'
                        AND av.availability IS NOT DISTINCT FROM 'available'
                        AND av.plaintext_sha256 IS NOT DISTINCT FROM lc.plaintext_sha256)
                    AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.vault_file_id=vf.id
                        AND j.status NOT IN ('completed','failed','cancelled'))
                    AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.vault_file_id=vf.id AND j.action='upload'
                        AND j.origin='automatic' AND j.status IN ('failed','cancelled')
                        AND j.requested_at>=COALESCE(lc.content_observed_at,lc.observed_at))
                    AND NOT EXISTS (SELECT 1 FROM cloud_deletion_items d WHERE d.vault_file_id=vf.id AND d.status='pending')
                    AND {rules}
            ) SELECT CASE WHEN stable_at > %s::timestamptz THEN 'stability_wait' ELSE 'queued' END AS stage,
                count(*) AS files, COALESCE(sum(GREATEST(size,0)),0) AS bytes,
                min(stable_at) AS next_stable_at FROM candidates GROUP BY 1
        """, [policy.stability_seconds, vault["id"], *params, stamp]).fetchall()
        for row in rows:
            stage = row["stage"]
            item[stage]["files"] += int(row["files"])
            item[stage]["bytes"] += int(row["bytes"])
            if stage == "queued":
                item["awaiting_admission_files"] = int(row["files"])
            else:
                item["next_stable_at"] = row["next_stable_at"].isoformat()
    total = _empty()
    for item in items.values():
        for stage in STAGES:
            for key in ("files", "bytes", "transferred_bytes"):
                total[stage][key] += item[stage][key]
        total["awaiting_admission_files"] += item["awaiting_admission_files"]
        deadline = item["next_stable_at"]
        if deadline and (not total["next_stable_at"] or deadline < total["next_stable_at"]):
            total["next_stable_at"] = deadline
    return {"observed_at": stamp, "global": total, "vaults": list(items.values())}
