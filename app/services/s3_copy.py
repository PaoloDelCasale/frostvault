"""Storage Class copies of one exact provider VersionId.

CopyObject up to 5 GiB and multipart UploadPartCopy above, with the read-back
of the destination. Nothing here touches the catalog or the Job row: callers
pass ``check_active`` when a long copy must observe cancellation and lease
loss between provider calls. The other exact-VersionId operations live in
``s3``.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any, Callable
from urllib.parse import urlencode

from .s3 import full_object_sha256_checksum, head_object_with_checksum, sha256_version


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


COPY_TOKEN_KEY = "frostvault-move"


def copy_metadata(source_head: dict[str, Any], token: str) -> dict[str, str]:
    """The source's metadata with this copy's token instead of its upload token.

    The source's own upload token names the attempt that wrote the source, not
    this copy: it is dropped so no reconciliation can attribute the copy to it.
    """
    metadata = {k: v for k, v in (source_head.get("Metadata") or {}).items()
                if k != "frostvault-upload"}
    metadata[COPY_TOKEN_KEY] = token
    return metadata


def multipart_source_checksum(
    client: Any, *, bucket: str, object_key: str, version_id: str,
    size: int, expected_sha256: str, check_active: Callable[[str], None],
) -> str:
    """Independent SHA256 COMPOSITE proof anchored to the verified full digest.

    AWS supports FULL_OBJECT for CRC algorithms, not multipart SHA256. Compute
    the expected SHA256-of-part-digests while reading the exact source once;
    arbitrary stream chunk boundaries must not change multipart boundaries.
    """
    part_size = max(S3_MULTIPART_COPY_MIN_PART_BYTES, S3_MULTIPART_COPY_PART_BYTES,
                    (size + S3_MULTIPART_COPY_MAX_PARTS - 1) // S3_MULTIPART_COPY_MAX_PARTS)
    response = client.get_object(Bucket=bucket, Key=object_key, VersionId=version_id)
    body = response["Body"]
    full, part, composite = hashlib.sha256(), hashlib.sha256(), hashlib.sha256()
    count = total = in_part = 0
    try:
        while chunk := body.read(S3_OBJECT_HASH_CHUNK_BYTES):
            check_active("Storage class change stopped")
            full.update(chunk)
            total += len(chunk)
            view = memoryview(chunk)
            while view:
                length = min(len(view), part_size - in_part)
                part.update(view[:length])
                view = view[length:]
                in_part += length
                if in_part == part_size:
                    composite.update(part.digest())
                    count += 1
                    part, in_part = hashlib.sha256(), 0
        if in_part:
            composite.update(part.digest())
            count += 1
    finally:
        body.close()
    if total != size or base64.b64encode(full.digest()).decode("ascii") != expected_sha256:
        raise RuntimeError("Multipart source does not match the verified plaintext digest")
    return base64.b64encode(composite.digest()).decode("ascii") + f"-{count}"


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
    intent_token: str | None = None,
    source_key: str | None = None,
    on_upload_id: Callable[[str], None] | None = None,
    on_response: Callable[[dict[str, Any]], None] | None = None,
) -> str | None:
    """Copy one exact S3 Version with multipart UploadPartCopy operations.

    ``source_key`` (default ``object_key``) is the exact source key; the
    UploadId is handed to ``on_upload_id`` before the first part, so it can be
    journaled as the only authority to abort it, and the completion response to
    ``on_response`` as soon as it returns.
    """
    source_key = source_key or object_key
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
        object_key=source_key,
        version_id=source_version_id,
    )
    create_kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Key": object_key,
        "StorageClass": target_class,
        "ChecksumAlgorithm": S3_COPY_CHECKSUM_ALGORITHM,
        "ChecksumType": "COMPOSITE" if intent_token else S3_COPY_CHECKSUM_TYPE,
    }
    if intent_token:
        create_kwargs["Metadata"] = copy_metadata(source_head, intent_token)
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
        if source_head.get(field) is not None and not (field == "Metadata" and intent_token):
            create_kwargs[field] = source_head[field]

    upload_id: str | None = None
    try:
        initiated = client.create_multipart_upload(**create_kwargs)
        upload_id = initiated.get("UploadId")
        if not upload_id:
            raise RuntimeError("S3 did not return a multipart UploadId")
        if on_upload_id is not None:
            on_upload_id(str(upload_id))

        parts: list[dict[str, Any]] = []
        copy_source = {
            "Bucket": bucket,
            "Key": source_key,
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
            ChecksumType="COMPOSITE" if intent_token else S3_COPY_CHECKSUM_TYPE,
        )
        # Completion makes the upload no longer abortable.  A missing VersionId
        # is handled by the destination read-back, not by guessing the source.
        upload_id = None
        if on_response is not None:
            on_response(dict(completed))
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


def copy_storage_class_version(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    source_version_id: str,
    target_class: str,
    source_size: int,
    source_head: dict[str, Any],
    check_active: Callable[[str], None] | None = None,
    intent_token: str | None = None,
    source_key: str | None = None,
    on_upload_id: Callable[[str], None] | None = None,
    on_response: Callable[[dict[str, Any]], None] | None = None,
) -> str | None:
    """Use CopyObject for small objects and exact multipart copy for large ones.

    The source is always the exact ``source_version_id`` of ``source_key``
    (default ``object_key``: a copy in place), never latest.
    """
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
            intent_token=intent_token,
            source_key=source_key,
            on_upload_id=on_upload_id,
            on_response=on_response,
        )
    extra: dict[str, Any] = {}
    if intent_token:
        extra["Metadata"] = copy_metadata(source_head, intent_token)
        for field in ("CacheControl", "ContentDisposition", "ContentEncoding", "ContentLanguage", "ContentType", "Expires"):
            if source_head.get(field) is not None:
                extra[field] = source_head[field]
    if check_active:
        check_active("Storage class change stopped")
    copy_result = client.copy_object(
        Bucket=bucket,
        Key=object_key,
        CopySource={
            "Bucket": bucket,
            "Key": source_key or object_key,
            "VersionId": source_version_id,
        },
        StorageClass=target_class,
        MetadataDirective="REPLACE" if intent_token else "COPY",
        TaggingDirective="COPY",
        ChecksumAlgorithm=S3_COPY_CHECKSUM_ALGORITHM,
        **extra,
    )
    if on_response is not None:
        on_response(dict(copy_result))
    return copy_result.get("VersionId")


def verify_storage_class_destination(
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
    expected_composite_checksum: str | None = None,
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
    destination = head_object_with_checksum(client, **kwargs)
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
    destination_checksum = full_object_sha256_checksum(destination)
    if expected_composite_checksum and destination.get("ChecksumType") == "COMPOSITE":
        # Some compatible providers expose a non-AWS composite encoding. Never
        # accept that metadata: only an independent exact-VersionId full SHA256
        # readback can substitute for the expected composite. Cold unreadable or
        # corrupt bytes still fail closed, without requesting a restore.
        destination_checksum = (
            expected_sha256_checksum
            if destination.get("ChecksumSHA256") == expected_composite_checksum
            else None
        )
        # The matching composite is independently anchored to the verified full
        # source digest, not an ETag or provider-supplied part hash alone.
    if destination_checksum is None:
        destination_checksum = sha256_version(
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
