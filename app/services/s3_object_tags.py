"""Apply and read lifecycle policy tags on versioned S3 objects."""
from __future__ import annotations

from typing import Any

from .lifecycle_policies import POLICY_TAG_KEY, read_policy_id_from_tags


def apply_version_policy_tag(
    client: Any,
    *,
    bucket: str,
    key: str,
    version_id: str,
    policy_id: str,
) -> None:
    client.put_object_tagging(
        Bucket=bucket,
        Key=key,
        VersionId=version_id,
        Tagging={
            "TagSet": [{"Key": POLICY_TAG_KEY, "Value": policy_id}],
        },
    )


def clear_version_policy_tag(
    client: Any,
    *,
    bucket: str,
    key: str,
    version_id: str,
) -> None:
    client.delete_object_tagging(
        Bucket=bucket,
        Key=key,
        VersionId=version_id,
    )


def read_version_policy_tag(
    client: Any,
    *,
    bucket: str,
    key: str,
    version_id: str,
) -> str | None:
    response = client.get_object_tagging(
        Bucket=bucket,
        Key=key,
        VersionId=version_id,
    )
    tags = {
        item["Key"]: item["Value"] for item in response.get("TagSet", [])
    }
    return read_policy_id_from_tags(tags)
