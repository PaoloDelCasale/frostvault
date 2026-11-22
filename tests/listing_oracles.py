"""Reference implementations of the browse and Job listings (#791).

The server pages folders and groups Jobs in SQL. These build the same views
from flat rows in Python, so tests can compare the SQL result with a simple
statement of the expected semantics.
"""
from __future__ import annotations

from typing import Any

from app.i18n import DEFAULT_LOCALE, present_job_message, translate
from app.services.protection_states import aggregate_state


def build_directory_items(
    rows: list[dict[str, Any]], directory: str, state_filter: str = ""
) -> list[dict[str, Any]]:
    """Build one file-manager level from the flat file catalog."""
    prefix = f"{directory}/" if directory else ""
    folders: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []

    for row in rows:
        relative = row["path"][len(prefix):]
        folder_name, separator, _ = relative.partition("/")
        if separator:
            folder = folders.setdefault(
                folder_name,
                {
                    "item_count": 0,
                    "total_size": 0,
                    "local_size": 0,
                    "cloud_size": 0,
                    "state_counts": {},
                    "action_counts": {
                        "upload": 0,
                        "recover": 0,
                        "free-space": 0,
                        "cloud-purge": 0,
                    },
                    "storage_classes": set(),
                    "matches_filter": False,
                },
            )
            folder["item_count"] += 1
            local_size = row.get("local_size")
            cloud_size = row.get("cloud_size")
            folder["total_size"] += local_size if local_size is not None else (cloud_size or 0)
            folder["local_size"] += local_size or 0
            folder["cloud_size"] += cloud_size or 0
            folder["state_counts"][row["state"]] = folder["state_counts"].get(row["state"], 0) + 1
            action_flags = {
                "upload": row.get("upload_eligible", row["state"] == "pending"),
                "recover": row.get("recover_eligible", row["state"] == "cloud_only"),
                "free-space": row.get("cleanup_eligible", row["state"] == "protected"),
                "cloud-purge": row["state"] == "cloud_only",
            }
            for action, eligible in action_flags.items():
                if eligible:
                    folder["action_counts"][action] += 1
            if row.get("storage_class"):
                folder["storage_classes"].add(row["storage_class"])
            if not state_filter or row["state"] == state_filter:
                folder["matches_filter"] = True
            continue
        if not state_filter or row["state"] == state_filter:
            files.append({**row, "type": "file", "name": relative})

    folder_items = []
    for name, aggregate in folders.items():
        if not aggregate["matches_filter"]:
            continue
        states = aggregate["state_counts"]
        state = aggregate_state(states)
        storage_classes = sorted(aggregate["storage_classes"])
        folder_items.append({
            "type": "directory",
            "name": name,
            "path": f"{prefix}{name}",
            "item_count": aggregate["item_count"],
            "total_size": aggregate["total_size"],
            "local_size": aggregate["local_size"],
            "cloud_size": aggregate["cloud_size"],
            "state": state,
            "state_counts": states,
            "storage_class": storage_classes[0] if len(storage_classes) == 1 else None,
            "storage_class_count": len(storage_classes),
            "available_actions": aggregate["action_counts"],
        })
    folder_items.sort(key=lambda item: item["name"].casefold())
    files.sort(key=lambda item: item["name"].casefold())
    return [*folder_items, *files]


def build_job_groups(
    rows: list[dict[str, Any]],
    *,
    locale: str = DEFAULT_LOCALE,
) -> list[dict[str, Any]]:
    """Aggregate file jobs by operation, weighting progress by transferred bytes."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group_id = row.get("group_id") or f"job-{row['id']}"
        display_message = present_job_message(row, locale)
        group = groups.setdefault(
            group_id,
            {
                "id": group_id,
                "path": row.get("group_path") or row["path"],
                "action": row["action"],
                "status": "completed",
                "message": "",
                "message_key": row.get("message_key"),
                "total_bytes": 0,
                "transferred_bytes": 0,
                "item_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "resolved_failed_count": 0,
                "unresolved_failed_count": 0,
                "cancelled_count": 0,
                "updated_at": row["updated_at"],
                "pending_until": row.get("pending_until"),
                "estimated_cost_eur": row.get("estimated_cost_eur"),
                "estimated_hours": row.get("estimated_hours"),
                "restore_tier": row.get("restore_tier"),
                "restore_days": row.get("restore_days"),
            },
        )
        if row.get("pending_until"):
            group["pending_until"] = row["pending_until"]
        if row.get("estimated_cost_eur") is not None:
            group["estimated_cost_eur"] = row["estimated_cost_eur"]
        if row.get("estimated_hours") is not None:
            group["estimated_hours"] = row["estimated_hours"]
        if row.get("restore_tier"):
            group["restore_tier"] = row["restore_tier"]
        if row.get("restore_days") is not None:
            group["restore_days"] = row["restore_days"]
        total_bytes = int(row.get("total_bytes") or 0)
        transferred = int(row.get("transferred_bytes") or 0)
        group["total_bytes"] += total_bytes
        group["transferred_bytes"] += total_bytes if row["status"] == "completed" else min(transferred, total_bytes)
        group["item_count"] += 1
        if row["status"] == "completed":
            group["completed_count"] += 1
            if not group["message"]:
                group["message"] = display_message
                group["message_key"] = row.get("message_key")
        elif row["status"] == "failed":
            group["failed_count"] += 1
            resolved = (row.get("remediation") or {}).get("state") == "resolved"
            group["resolved_failed_count" if resolved else "unresolved_failed_count"] += 1
            group["message"] = display_message or translate(
                "job.operation_failed", locale=locale
            )
            group["message_key"] = row.get("message_key") or "job.operation_failed"
        elif row["status"] == "cancelled":
            group["cancelled_count"] += 1
            group["message"] = display_message or translate(
                "job.operation_stopped", locale=locale
            )
            group["message_key"] = row.get("message_key") or "job.operation_stopped"
        else:
            group["status"] = row["status"]
            group["message"] = display_message
            group["message_key"] = row.get("message_key")
        if str(row["updated_at"]) > str(group["updated_at"]):
            group["updated_at"] = row["updated_at"]

    for group in groups.values():
        active_count = (
            group["item_count"]
            - group["completed_count"]
            - group["failed_count"]
            - group["cancelled_count"]
        )
        if active_count == 0:
            group["status"] = (
                "failed"
                if group["failed_count"]
                else "cancelled"
                if group["cancelled_count"]
                else "completed"
            )
        total_bytes = group["total_bytes"]
        finished_count = (
            group["completed_count"] + group["failed_count"] + group["cancelled_count"]
        )
        group["percent"] = round(
            100 * group["transferred_bytes"] / total_bytes
            if total_bytes
            else 100 * finished_count / group["item_count"]
        )
    return list(groups.values())
