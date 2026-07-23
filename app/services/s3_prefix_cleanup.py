"""Delete every object version under a CI/test S3 prefix.

Used by layered S3 validation (issue #13) so leftover objects from failed
runs are surfaced and cleanup can be rerun safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CleanupReport:
    bucket: str
    prefix: str
    deleted_versions: int
    leftover_keys: tuple[str, ...]
    ok: bool
    message: str = ""


def cleanup_prefix_versions(
    client: Any,
    *,
    bucket: str,
    prefix: str,
) -> CleanupReport:
    """Delete all object versions and delete markers under ``prefix``.

    Returns a report. ``ok`` is True only when a follow-up listing finds no
    remaining versions or delete markers under the prefix.
    """
    normalized = prefix.strip("/")
    if not normalized:
        return CleanupReport(
            bucket=bucket,
            prefix=prefix,
            deleted_versions=0,
            leftover_keys=(),
            ok=False,
            message="Refusing to clean an empty prefix",
        )
    list_prefix = f"{normalized}/"

    deleted = 0
    continuation: dict[str, Any] = {}
    while True:
        response = client.list_object_versions(
            Bucket=bucket, Prefix=list_prefix, **continuation
        )
        to_delete: list[dict[str, str]] = []
        for version in response.get("Versions") or []:
            to_delete.append(
                {"Key": version["Key"], "VersionId": version["VersionId"]}
            )
        for marker in response.get("DeleteMarkers") or []:
            to_delete.append(
                {"Key": marker["Key"], "VersionId": marker["VersionId"]}
            )
        if to_delete:
            # S3 DeleteObjects accepts at most 1000 keys per call.
            for offset in range(0, len(to_delete), 1000):
                batch = to_delete[offset : offset + 1000]
                client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": batch, "Quiet": True},
                )
                deleted += len(batch)
        if not response.get("IsTruncated"):
            break
        continuation = {
            "KeyMarker": response.get("NextKeyMarker", ""),
            "VersionIdMarker": response.get("NextVersionIdMarker", ""),
        }

    leftover = client.list_object_versions(Bucket=bucket, Prefix=list_prefix)
    leftover_keys = tuple(
        sorted(
            {
                *(item["Key"] for item in leftover.get("Versions") or []),
                *(item["Key"] for item in leftover.get("DeleteMarkers") or []),
            }
        )
    )
    ok = not leftover_keys
    return CleanupReport(
        bucket=bucket,
        prefix=normalized,
        deleted_versions=deleted,
        leftover_keys=leftover_keys,
        ok=ok,
        message="" if ok else f"Leftover keys remain under {list_prefix}",
    )
