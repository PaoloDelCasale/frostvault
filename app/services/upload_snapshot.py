"""Independent Upload Snapshot evidence, not provider observations or Local Copies.

A bounded stable hashing pass defines plaintext and multipart boundaries together.
The transport validates each bounded buffer against this baseline BEFORE sending
it, so reopening a changed path cannot silently upload B under A's evidence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from s3transfer.utils import ChunksizeAdjuster

CHUNK_SIZE = 1024 * 1024
MULTIPART_THRESHOLD = 8 * CHUNK_SIZE
DEFAULT_PART_SIZE = 8 * CHUNK_SIZE
MAX_PART_SIZE = 1024 * CHUNK_SIZE
MAX_OBJECT_SIZE = 5 * 1024**4
MODES = frozenset({"s3_checksum", "read_back"})


def fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def sha256_b64(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


@dataclass(frozen=True)
class UploadSnapshot:
    size: int
    plaintext_sha256: str
    checksum_type: str
    checksum_sha256: str
    part_size: int
    part_checksums: tuple[str, ...]

    def as_json(self) -> str:
        return json.dumps({"format": 1, "size": self.size, "plaintext_sha256": self.plaintext_sha256,
                           "algorithm": "SHA256", "checksum_type": self.checksum_type,
                           "checksum_sha256": self.checksum_sha256, "part_size": self.part_size,
                           "part_checksums": list(self.part_checksums)}, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> UploadSnapshot:
        data = json.loads(raw)
        if data.get("format") != 1 or data.get("algorithm") != "SHA256":
            raise ValueError("Unsupported Upload Snapshot evidence")
        snapshot = cls(data["size"], data["plaintext_sha256"], data["checksum_type"],
                       data["checksum_sha256"], data["part_size"], tuple(data["part_checksums"]))
        if not 0 <= snapshot.size <= MAX_OBJECT_SIZE or not DEFAULT_PART_SIZE <= snapshot.part_size <= MAX_PART_SIZE:
            raise ValueError("Invalid Upload Snapshot layout")
        digest = bytes.fromhex(snapshot.plaintext_sha256)
        parts = [base64.b64decode(value, validate=True) for value in snapshot.part_checksums]
        if len(digest) != 32 or not parts or any(len(part) != 32 for part in parts):
            raise ValueError("Invalid Upload Snapshot digest")
        multipart = snapshot.size >= MULTIPART_THRESHOLD
        count = max(1, (snapshot.size + snapshot.part_size - 1) // snapshot.part_size) if multipart else 1
        expected = composite_checksum(snapshot.part_checksums) if multipart else base64.b64encode(digest).decode("ascii")
        if (len(parts) != count or count > 10000 or snapshot.checksum_type != ("COMPOSITE" if multipart else "FULL_OBJECT")
                or snapshot.checksum_sha256 != expected or (not multipart and parts[0] != digest)):
            raise ValueError("Invalid Upload Snapshot proof")
        return snapshot

    def part_length(self, number: int) -> int:
        if not 1 <= number <= len(self.part_checksums):
            raise RuntimeError("Upload Snapshot part number mismatch")
        return min(self.part_size, self.size - (number - 1) * self.part_size)


def composite_checksum(parts: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(base64.b64decode(part, validate=True))
    return base64.b64encode(digest.digest()).decode("ascii") + f"-{len(parts)}"


def prepare_upload_snapshot(source: Path, check_active: Callable[[], None]) -> tuple[UploadSnapshot, os.stat_result]:
    check_active()
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("Upload Snapshot requires a regular file")
    if before.st_size > MAX_OBJECT_SIZE:
        raise RuntimeError("Upload Snapshot exceeds S3 object size limit")
    part_size = ChunksizeAdjuster().adjust_chunksize(DEFAULT_PART_SIZE, before.st_size)
    if part_size > MAX_PART_SIZE:
        raise RuntimeError("Upload Snapshot exceeds bounded part size")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    plaintext = hashlib.sha256()
    parts: list[str] = []
    size = 0
    with os.fdopen(os.open(source, flags), "rb") as stream:
        if fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
            raise RuntimeError("Local file changed since fingerprinting")
        while size < before.st_size or not parts:
            part = hashlib.sha256()
            left = min(part_size, before.st_size - size)
            while left:
                check_active()
                chunk = stream.read(min(CHUNK_SIZE, left))
                if not chunk:
                    raise RuntimeError("Local file changed since fingerprinting")
                part.update(chunk)
                plaintext.update(chunk)
                size += len(chunk)
                left -= len(chunk)
            parts.append(base64.b64encode(part.digest()).decode("ascii"))
        check_active()
        if stream.read(1) or fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
            raise RuntimeError("Local file changed since fingerprinting")
    if fingerprint(source.stat(follow_symlinks=False)) != fingerprint(before):
        raise RuntimeError("Local file changed since fingerprinting")
    multipart = size >= MULTIPART_THRESHOLD
    snapshot = UploadSnapshot(size, plaintext.hexdigest(), "COMPOSITE" if multipart else "FULL_OBJECT",
                              composite_checksum(tuple(parts)) if multipart else base64.b64encode(plaintext.digest()).decode("ascii"),
                              part_size, tuple(parts))
    return snapshot, before


class UploadProofMismatch(RuntimeError):
    """Present but contradictory proof; never eligible for read-back downgrade."""


def check_s3_proof(proof: dict[str, Any], *, version_id: str, snapshot: UploadSnapshot,
                   completion: bool = False) -> bool:
    """True for cryptographic proof, False for absent proof; contradictions throw.

    PUT responses may omit ChecksumType (the API defines whole-object SHA256).
    HEAD and multipart completion must explicitly confirm the expected type.
    """
    if str(proof.get("VersionId") or "") != str(version_id) or proof.get("DeleteMarker"):
        raise UploadProofMismatch("S3 checksum proof returned a different VersionId")
    if proof.get("ContentLength") is not None and int(proof["ContentLength"]) != snapshot.size:
        raise UploadProofMismatch("S3 checksum proof size mismatch")
    kind = proof.get("ChecksumType")
    if kind is not None and kind != snapshot.checksum_type:
        raise UploadProofMismatch("S3 checksum proof type mismatch")
    value = proof.get("ChecksumSHA256")
    if value is not None and value != snapshot.checksum_sha256:
        raise UploadProofMismatch("S3 checksum proof digest mismatch")
    return bool(value and (kind == snapshot.checksum_type or (completion and snapshot.checksum_type == "FULL_OBJECT")))


def validated_s3_proof(proof: dict[str, Any], *, version_id: str,
                       snapshot: UploadSnapshot, source: str,
                       completion: bool = False) -> dict[str, str] | None:
    """Normalize observed evidence only after checking the independent baseline."""
    if not check_s3_proof(proof, version_id=version_id, snapshot=snapshot, completion=completion):
        return None
    return {"algorithm": "SHA256", "checksum_type": snapshot.checksum_type,
            "checksum": proof["ChecksumSHA256"], "version_id": version_id,
            "source": source}


def unsupported_checksum_api(exc: Exception) -> bool:
    """Accept explicit capability errors, never generic malformed requests.

    Some compatible providers identify an unsupported checksum-mode argument
    structurally. Do not infer capability from arbitrary provider messages or
    from InvalidRequest/InvalidArgument alone: that could hide configuration
    errors and incur an unexpected read-back.
    """
    from botocore.exceptions import ClientError
    if not isinstance(exc, ClientError) or exc.operation_name != "HeadObject":
        return False
    error = exc.response.get("Error", {})
    code = str(error.get("Code"))
    if code in {"NotImplemented", "UnsupportedOperation", "XNotImplemented", "501"}:
        return True
    return code in {"InvalidRequest", "InvalidArgument"} and error.get("ArgumentName") in {
        "ChecksumMode", "x-amz-checksum-mode"
    }
