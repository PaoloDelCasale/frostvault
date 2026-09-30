"""Exact-VersionId S3 helpers shared by the worker.

Listing, deletion, checksum read-back and storage-class copies for one
provider VersionId.  Nothing here touches the catalog or the Job row: callers
pass ``check_active`` when a long copy must observe cancellation and lease
loss between provider calls.
"""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Callable
from urllib.parse import urlencode

from botocore.exceptions import ClientError


# S3 CopyObject is limited to objects up to 5 GiB.  Multipart copy keeps the
# source VersionId on every UploadPartCopy request and uses a deliberately
# conservative part size so even the largest supported object stays below the
# provider's 10,000-part limit.
S3_SINGLE_COPY_MAX_BYTES = 5 * 1024**3


S3_MULTIPART_COPY_MIN_PART_BYTES = 5 * 1024**2


S3_MULTIPART_COPY_PART_BYTES = 128 * 1024**2


S3_MULTIPART_COPY_MAX_PARTS = 10_000


S3_COPY_CHECKSUM_ALGORITHM = "SHA256"


S3_COPY_CHECKSUM_TYPE = "FULL_OBJECT"


S3_OBJECT_HASH_CHUNK_BYTES = 1024 * 1024


def _object_version_entries(
    client: Any,
    *,
    bucket: str,
    object_key: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read exact-version postconditions without treating a retry as evidence.

    S3's list API is used rather than a current-key HEAD because a delete marker
    and a noncurrent Archive Version are both meaningful durable outcomes.
    """
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": object_key}
    versions: dict[str, dict[str, Any]] = {}
    markers: dict[str, dict[str, Any]] = {}
    while True:
        page = client.list_object_versions(**kwargs)
        for item in page.get("Versions") or []:
            if item.get("Key") == object_key and item.get("VersionId"):
                versions[str(item["VersionId"])] = item
        for item in page.get("DeleteMarkers") or []:
            if item.get("Key") == object_key and item.get("VersionId"):
                markers[str(item["VersionId"])] = item
        if not page.get("IsTruncated"):
            break
        next_key = page.get("NextKeyMarker")
        next_version = page.get("NextVersionIdMarker")
        if not next_key:
            raise RuntimeError("S3 version listing omitted its continuation marker")
        kwargs["KeyMarker"] = next_key
        if next_version:
            kwargs["VersionIdMarker"] = next_version
    return versions, markers


def _s3_version_already_gone(exc: BaseException) -> bool:
    if not isinstance(exc, ClientError):
        return False
    code = str(exc.response.get("Error", {}).get("Code") or "")
    return code in {"NoSuchVersion", "NoSuchKey", "NotFound", "404"}


def _delete_s3_version(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    version_id: str,
) -> None:
    try:
        client.delete_object(
            Bucket=bucket, Key=object_key, VersionId=version_id
        )
    except Exception as exc:
        if _s3_version_already_gone(exc):
            return
        raise


def _verification_version_head(
    client: Any,
    job: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    """Reconfirm the exact provider VersionId around a streamed read."""
    object_key = str(target.get("object_key") or "")
    expected_version = str(target.get("provider_version_id") or "")
    if not object_key or not expected_version:
        raise RuntimeError("Upload verification target has no exact S3 VersionId")
    head = client.head_object(Bucket=job["s3_bucket"], Key=object_key)
    observed_version = head.get("VersionId")
    if not observed_version:
        raise RuntimeError("Upload verification found no S3 VersionId")
    if str(observed_version) != expected_version:
        raise RuntimeError("Archive Version changed during upload verification")
    expected_size = target.get("cloud_size")
    observed_size = head.get("ContentLength")
    if expected_size is not None and observed_size is not None:
        if int(observed_size) != int(expected_size):
            raise RuntimeError("Archive Version size changed during upload verification")
    return head


def _head_object_with_checksum(client: Any, **kwargs: Any) -> dict[str, Any]:
    """Read a version with provider checksum fields when supported.

    Some S3-compatible providers reject ``ChecksumMode``. Falling back to a
    normal HEAD keeps the operation portable; the caller then obtains an
    equivalent proof by hashing the exact VersionId instead of publishing on
    size/ETag metadata alone.
    """
    try:
        return client.head_object(**kwargs, ChecksumMode="ENABLED")
    except Exception:
        return client.head_object(**kwargs)


def _full_object_sha256_checksum(head: dict[str, Any]) -> str | None:
    checksum = head.get("ChecksumSHA256")
    if not checksum:
        return None
    checksum_type = str(head.get("ChecksumType") or "FULL_OBJECT").upper()
    if checksum_type != S3_COPY_CHECKSUM_TYPE:
        return None
    return str(checksum)


def _sha256_s3_version(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    version_id: str,
    check_active: Callable[[str], None] | None = None,
) -> str:
    """Hash one exact provider VersionId and return its S3 checksum encoding."""
    response = client.get_object(
        Bucket=bucket,
        Key=object_key,
        VersionId=version_id,
    )
    body = response.get("Body") if isinstance(response, dict) else None
    if body is None:
        raise RuntimeError("S3 did not return a body for integrity verification")
    digest = hashlib.sha256()
    try:
        iter_chunks = getattr(body, "iter_chunks", None)
        if callable(iter_chunks):
            chunks = iter_chunks(chunk_size=S3_OBJECT_HASH_CHUNK_BYTES)
        else:
            read = getattr(body, "read", None)
            if not callable(read):
                raise RuntimeError("S3 returned an unreadable body")

            def read_chunks():
                while True:
                    chunk = read(S3_OBJECT_HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk

            chunks = read_chunks()
        for chunk in chunks:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise RuntimeError("S3 returned a non-binary body")
            digest.update(chunk)
            if check_active is not None:
                check_active("Storage class change stopped")
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return base64.b64encode(digest.digest()).decode("ascii")


def _source_object_tagging(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    version_id: str,
) -> str | None:
    """Return source Version tags in CreateMultipartUpload's wire format."""
    response = client.get_object_tagging(
        Bucket=bucket,
        Key=object_key,
        VersionId=version_id,
    )
    if not isinstance(response, dict):
        raise RuntimeError("S3 returned an invalid object-tag response")
    tag_set = response.get("TagSet") or []
    if not isinstance(tag_set, (list, tuple)):
        raise RuntimeError("S3 returned an invalid object-tag set")
    tags: list[tuple[str, str]] = []
    for tag in tag_set:
        if not isinstance(tag, dict) or tag.get("Key") is None or tag.get("Value") is None:
            raise RuntimeError("S3 returned an invalid object tag")
        tags.append((str(tag["Key"]), str(tag["Value"])))
    return urlencode(tags) if tags else None


def _multipart_copy_storage_class(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    source_version_id: str,
    target_class: str,
    size_bytes: int,
    source_head: dict[str, Any],
    check_active: Callable[[str], None] | None = None,
) -> str | None:
    """Copy one exact S3 Version with multipart UploadPartCopy operations."""
    if size_bytes <= S3_SINGLE_COPY_MAX_BYTES:
        raise ValueError("Multipart storage-class copy requires an oversized object")

    part_size = max(
        S3_MULTIPART_COPY_MIN_PART_BYTES,
        S3_MULTIPART_COPY_PART_BYTES,
        (size_bytes + S3_MULTIPART_COPY_MAX_PARTS - 1)
        // S3_MULTIPART_COPY_MAX_PARTS,
    )
    part_count = (size_bytes + part_size - 1) // part_size
    if part_count > S3_MULTIPART_COPY_MAX_PARTS:
        raise RuntimeError("S3 multipart copy would exceed the part limit")

    tagging = _source_object_tagging(
        client,
        bucket=bucket,
        object_key=object_key,
        version_id=source_version_id,
    )
    create_kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Key": object_key,
        "StorageClass": target_class,
        "ChecksumAlgorithm": S3_COPY_CHECKSUM_ALGORITHM,
        "ChecksumType": S3_COPY_CHECKSUM_TYPE,
    }
    if tagging:
        # UploadPartCopy does not inherit tags from its source Version. The
        # tag set must be supplied when the multipart upload is initiated.
        create_kwargs["Tagging"] = tagging
    # Multipart initiation does not have CopyObject's metadata directives.  Set
    # the source headers that S3 exposes so the new representation does not
    # unexpectedly lose content metadata while its bytes are copied.
    for field in (
        "CacheControl",
        "ContentDisposition",
        "ContentEncoding",
        "ContentLanguage",
        "ContentType",
        "Expires",
        "Metadata",
    ):
        if source_head.get(field) is not None:
            create_kwargs[field] = source_head[field]

    upload_id: str | None = None
    try:
        initiated = client.create_multipart_upload(**create_kwargs)
        upload_id = initiated.get("UploadId")
        if not upload_id:
            raise RuntimeError("S3 did not return a multipart UploadId")

        parts: list[dict[str, Any]] = []
        copy_source = {
            "Bucket": bucket,
            "Key": object_key,
            "VersionId": source_version_id,
        }
        for part_number in range(1, part_count + 1):
            if check_active is not None:
                check_active("Storage class change stopped")
            start = (part_number - 1) * part_size
            end = min(size_bytes, start + part_size) - 1
            result = client.upload_part_copy(
                Bucket=bucket,
                Key=object_key,
                UploadId=upload_id,
                PartNumber=part_number,
                CopySource=copy_source,
                CopySourceRange=f"bytes={start}-{end}",
            )
            copy_result = result.get("CopyPartResult") or {}
            etag = copy_result.get("ETag") or result.get("ETag")
            if not etag:
                raise RuntimeError(
                    f"S3 did not return an ETag for multipart part {part_number}"
                )
            part = {"PartNumber": part_number, "ETag": etag}
            for checksum_name in (
                "ChecksumCRC32",
                "ChecksumCRC32C",
                "ChecksumSHA1",
                "ChecksumSHA256",
                "ChecksumCRC64NVME",
            ):
                checksum = copy_result.get(checksum_name)
                if checksum:
                    part[checksum_name] = checksum
            parts.append(part)

        if check_active is not None:

            check_active("Storage class change stopped")
        completed = client.complete_multipart_upload(
            Bucket=bucket,
            Key=object_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
            ChecksumType=S3_COPY_CHECKSUM_TYPE,
        )
        # Completion makes the upload no longer abortable.  A missing VersionId
        # is handled by the destination read-back, not by guessing the source.
        upload_id = None
        return completed.get("VersionId")
    except BaseException as exc:
        if upload_id:
            try:
                client.abort_multipart_upload(
                    Bucket=bucket,
                    Key=object_key,
                    UploadId=upload_id,
                )
            except Exception as abort_exc:
                raise RuntimeError(
                    "Multipart storage-class copy failed and its upload could not be aborted"
                ) from abort_exc
        raise


def _copy_storage_class_version(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    source_version_id: str,
    target_class: str,
    source_size: int,
    source_head: dict[str, Any],
    check_active: Callable[[str], None] | None = None,
) -> str | None:
    """Use CopyObject for small objects and exact multipart copy for large ones."""
    if source_size > S3_SINGLE_COPY_MAX_BYTES:
        return _multipart_copy_storage_class(
            client,
            check_active=check_active,
            bucket=bucket,
            object_key=object_key,
            source_version_id=source_version_id,
            target_class=target_class,
            size_bytes=source_size,
            source_head=source_head,
        )
    copy_result = client.copy_object(
        Bucket=bucket,
        Key=object_key,
        CopySource={
            "Bucket": bucket,
            "Key": object_key,
            "VersionId": source_version_id,
        },
        StorageClass=target_class,
        MetadataDirective="COPY",
        TaggingDirective="COPY",
        ChecksumAlgorithm=S3_COPY_CHECKSUM_ALGORITHM,
    )
    return copy_result.get("VersionId")


def _verify_storage_class_destination(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    source_version_id: str,
    candidate_version_id: str | None,
    target_class: str,
    expected_size: int,
    expected_sha256_checksum: str | None = None,
    check_active: Callable[[str], None] | None = None,
) -> tuple[str, str | None]:
    """Read back the exact destination before publishing catalog state.

    Size and ETag are useful metadata checks but are not content proofs (in
    particular, multipart ETags are not object digests). The destination must
    expose the same full-object SHA-256 checksum as the source, or be streamed
    and hashed when the provider does not expose checksum metadata.
    """
    if not expected_sha256_checksum:
        raise RuntimeError("Storage class copy lacks a source integrity proof")
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": object_key}
    if candidate_version_id:
        kwargs["VersionId"] = candidate_version_id
    destination = _head_object_with_checksum(client, **kwargs)
    observed_version_id = destination.get("VersionId") or candidate_version_id
    if not observed_version_id:
        raise RuntimeError(
            "Storage class copy did not produce a verifiable S3 VersionId"
        )
    if candidate_version_id and str(observed_version_id) != str(candidate_version_id):
        raise RuntimeError(
            "Storage class copy read-back returned a different S3 VersionId"
        )
    if str(observed_version_id) == str(source_version_id):
        raise RuntimeError(
            "Storage class copy read-back still points at the source S3 VersionId"
        )
    if destination.get("DeleteMarker"):
        raise RuntimeError("Storage class copy read-back returned a Delete Marker")
    observed_class = (destination.get("StorageClass") or "STANDARD").upper()
    if observed_class != target_class:
        raise RuntimeError(
            "Storage class copy read-back returned the wrong storage class"
        )
    content_length = destination.get("ContentLength")
    if content_length is None or int(content_length) != int(expected_size):
        raise RuntimeError(
            "Storage class copy read-back returned the wrong object size"
        )
    destination_checksum = _full_object_sha256_checksum(destination)
    if destination_checksum is None:
        destination_checksum = _sha256_s3_version(
            client,
            bucket=bucket,
            object_key=object_key,
            version_id=str(observed_version_id),
            check_active=check_active,
        )
    if destination_checksum != expected_sha256_checksum:
        raise RuntimeError(
            "Storage class copy read-back failed its content-integrity check"
        )
    return str(observed_version_id), (
        str(destination.get("ETag")).strip('"')
        if destination.get("ETag")
        else None
    )


def restore_header_state(value: str | None) -> tuple[str, str | None]:
    if not value:
        return "not_requested", None
    if 'ongoing-request="true"' in value:
        return "restoring", None
    match = re.search(r'expiry-date="([^"]+)"', value)
    return "available", match.group(1) if match else None


ARCHIVE_RESTORE_REQUIRED = frozenset({"GLACIER", "DEEP_ARCHIVE"})


def storage_class_requires_restore(storage_class: str | None) -> bool:
    return (storage_class or "").upper() in ARCHIVE_RESTORE_REQUIRED
