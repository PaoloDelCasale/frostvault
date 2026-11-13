"""Read-only, bounded-result upload activity; never enumerates Jobs or files.

Counts describe currently observed work, not a lifetime/batch completion ratio.
A stability deadline is conditional on unchanged content, not an upload ETA.
Only SQL aggregates cross the service boundary (O(Vaults), not O(files/Jobs)).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .operation_policies import get_policy
from .upload_path_rules import eligibility_sql as _eligibility_sql, path_pattern as _path_pattern  # noqa: F401

STAGES = ("stability_wait", "queued", "uploading", "verifying")


def _empty() -> dict[str, Any]:
    return {**{stage: {"files": 0, "bytes": 0, "transferred_bytes": 0} for stage in STAGES},
            "awaiting_admission_files": 0, "next_stable_at": None,
            "bulk_admission": {"requests": 0, "selected_files": 0, "processed_files": 0, "admitted_files": 0}}


def upload_activity(connection: Any, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    vaults = connection.execute(
        """SELECT v.id,v.name,v.lifecycle,
            a.requests,a.selected_files,a.processed_files,a.admitted_files
        FROM vaults v LEFT JOIN (
            SELECT vault_id,count(*) AS requests,sum(selected_count) AS selected_files,
                sum(processed_count) AS processed_files,sum(admitted_count) AS admitted_files
            FROM bulk_upload_admissions WHERE status='pending' GROUP BY vault_id
        ) a ON a.vault_id=v.id ORDER BY lower(v.name),v.id"""
    ).fetchall()
    items = {row["id"]: {"vault_id": row["id"], "vault_name": row["name"], **_empty()} for row in vaults}
    for row in vaults:
        items[row["id"]]["bulk_admission"] = {
            key: int(row.get(key) or 0) for key in _empty()["bulk_admission"]}
    # A single grouped query, with progress clamped per Job before summation.
    # Reconciliation/cleanup is verification work, not another network upload.
    # While the installation S3 gate is blocked no transfer can start: a Job still
    # 'uploading' with nothing sent is a lease left by a stopped process (or one
    # about to be suspended), so it waits like a queued Job (#487). Sent bytes
    # stay visible as uploading until the worker or lease reconciliation moves them.
    for row in connection.execute("""
        WITH gate AS (SELECT COALESCE(
            (SELECT status='blocked' FROM cloud_preflight_state WHERE id=1), false) AS blocked)
        SELECT j.vault_id, CASE
            WHEN j.status IN ('verifying','cleaning') OR (j.status='uploading' AND
                 (coalesce(u.reconcile_only, false) OR j.message_key='job.purging_previous_snapshot')) THEN 'verifying'
            WHEN j.status='uploading' AND NOT (gate.blocked AND COALESCE(j.transferred_bytes,0)<=0)
                 THEN 'uploading' ELSE 'queued' END AS stage,
            count(*) AS files, COALESCE(sum(GREATEST(j.total_bytes,0)),0) AS bytes,
            COALESCE(sum(LEAST(GREATEST(j.transferred_bytes,0),GREATEST(j.total_bytes,0))),0) AS transferred_bytes
        FROM jobs j LEFT JOIN job_uploads u ON u.job_id=j.id CROSS JOIN gate
        WHERE j.action='upload' AND j.status NOT IN ('completed','failed','cancelled')
        GROUP BY 1,2
    """).fetchall():
        if row["vault_id"] in items:
            items[row["vault_id"]][row["stage"]] = {
                key: int(row[key]) for key in ("files", "bytes", "transferred_bytes")}

    for vault in vaults:
        policy = get_policy(connection, vault["id"])
        item = items[vault["id"]]
        if not (policy.auto_upload and vault["lifecycle"] == "active"):
            continue
        rules, params = _eligibility_sql(policy)
        # Match the durable admission observations, not the continuous
        # pending age (files.pending_since, which never resets on writes).
        rows = connection.execute(f"""
            WITH candidates AS (
                SELECT f.local_size AS size, GREATEST(to_timestamp(f.local_mtime_ns::double precision / 1000000000),
                    COALESCE(f.local_content_observed_at,f.local_observed_at))
                    + %s * interval '1 second' AS stable_at
                FROM files f
                LEFT JOIN archive_versions av ON av.id=f.matched_version_id
                WHERE f.vault_id=%s AND f.local_presence='present'
                    AND f.local_file_type='regular' AND f.held_version_id IS NULL
                    AND NOT (f.matched_version_id IS NOT NULL AND f.local_sha256 IS NOT NULL
                        AND av.integrity IS NOT DISTINCT FROM 'verified'
                        AND av.availability IS NOT DISTINCT FROM 'available'
                        AND av.plaintext_sha256 IS NOT DISTINCT FROM f.local_sha256)
                    AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.file_id=f.id
                        AND j.status NOT IN ('completed','failed','cancelled'))
                    AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.file_id=f.id AND j.action='upload'
                        AND j.origin='automatic' AND j.status IN ('failed','cancelled')
                        AND j.requested_at>=COALESCE(f.local_content_observed_at,f.local_observed_at))
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
        for key in total["bulk_admission"]:
            total["bulk_admission"][key] += item["bulk_admission"][key]
        deadline = item["next_stable_at"]
        if deadline and (not total["next_stable_at"] or deadline < total["next_stable_at"]):
            total["next_stable_at"] = deadline
    return {"observed_at": stamp, "global": total, "vaults": list(items.values())}
