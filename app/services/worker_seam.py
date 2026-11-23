"""The seam between the storage worker and the Upload/copy Modules (#793).

``upload_execution``, ``snapshot_cleanup`` and ``copy_provenance`` run inside a
Job the storage worker claimed. What they ask of that worker is declared here,
one small Interface per responsibility; they import nothing of storage and use
no other name of it. The protocol itself is unchanged (ADR-0036).

``app.storage.worker_adapter`` is the production Adapter: one object, built
once, whose methods resolve the storage functions when called (a patched
``app.storage.X`` is what runs). Tests of the Modules alone use the fake
Adapter ``tests.upload_support.fake_worker``.
"""
from __future__ import annotations

import os
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Protocol


class JobClaim(Protocol):
    """The claim of the running Job and its durable state."""

    # A stop the caller must not turn into a failure: cancellation, a lost
    # claim (a subclass of the first) and the shutdown of the process.
    OperationCancelled: type[Exception]
    JobLeaseLost: type[Exception]
    WorkerStopping: type[BaseException]

    def claim(self, job: dict[str, Any]) -> AbstractContextManager[None]:
        """Make the Job's claim and its loss signal current for this thread."""

    def claim_lost_event(self, job_id: int) -> threading.Event | None:
        """The in-memory loss signal of the current claim of ``job_id``, if any."""

    def stopped_in_process(self, job_id: int) -> bool:
        """In-memory stop signals only, for effect-free loops: raise
        ``WorkerStopping`` on process shutdown, then say whether this process
        cancelled the Job."""

    def job_cancelled(self, job_id: int) -> bool:
        """Whether this process cancelled the Job (in memory, no shutdown check)."""

    def ensure_job_active(self, job_id: int, message: str) -> None:
        """Durable check: raise on shutdown, cancellation or a lost claim."""

    def ensure_job_claim_owned_in_transaction(
        self, connection: Any, job: dict[str, Any], message: str,
    ) -> None:
        """Fence ``connection``'s transaction behind the owned lease."""

    def set_job(self, job_id: int, status: str, message: str = "", *,
                message_key: str | None = None) -> bool:
        """Claim-fenced transition in its own DB unit; False when the claim lost."""

    def set_job_in_transaction(
        self, connection: Any, job_id: int, status: str, *,
        message_key: str | None = None, transferred_bytes: int | None = None,
        fenced_unit: bool = False,
    ) -> bool:
        """The same transition inside the caller's DB unit."""

    def set_job_message_in_transaction(
        self, connection: Any, job_id: int, message_key: str,
    ) -> None:
        """Change only the visible message of the claimed, running Job."""


class SourceVolume(Protocol):
    """Checks on the Source Volume and on the Local Copy's place in it."""

    UploadSourceRemoved: type[Exception]

    def safe_local_path(self, source_root: str, path: str) -> Path:
        """The Local Copy's path, confined to the Vault's source root."""

    def source_volume_problem_open(self, connection: Any, job: dict[str, Any]) -> bool:
        """Whether the Job's Vault has an open Source Volume Problem."""

    def resolve_source_volume_problem(self, job: dict[str, Any]) -> None:
        """Close that Problem: the volume answered."""

    def check_upload_source_root(self, job: dict[str, Any], vault: dict[str, Any] | None) -> None:
        """Refuse the Upload when the source root is not the enrolled one."""

    def require_upload_source_identity(self, job: dict[str, Any]) -> None:
        """Refuse a provider effect when the Vault's root identity changed."""


class UploadCatalog(Protocol):
    """The catalog side of a claimed Upload or copy: its DB units and records."""

    def db(self) -> Any:
        """A catalog connection (context manager), as the worker opens it."""

    def now_iso(self) -> str:
        """The worker's clock, in the representation of Job rows."""

    def catalog_unit(self, job: dict[str, Any], mutation: Callable[[Any], Any], *,
                     shared: bool = False) -> Any:
        """One claim- and target-fenced DB unit, replayed alone on a
        serialization failure; ``mutation`` receives the ArchiveCatalog."""

    def upload_key(self, job: dict[str, Any]) -> str:
        """The object key of the Job's path under its Vault Target's prefix."""

    def record_reconciliation_block(self, job: dict[str, Any], reason: str) -> None:
        """Record why the file's Upload needs reconciliation (Problem included)."""

    def clear_reconciliation_problem(self, job: dict[str, Any]) -> None:
        """Close the file's Upload reconciliation Problem."""

    def record_verification_failure(self, reason: str) -> None:
        """Count one failed verification by reason."""


class AutomaticUploadGate(Protocol):
    """Whether an automatic Upload may still run, and its quiet endings."""

    UPLOAD_READY: str
    UPLOAD_SOURCE_REMOVED: str
    UPLOAD_OBSERVATION_WITHDRAWN: str

    def automatic_upload_state(self, job: dict[str, Any]) -> str:
        """One of the three states above, or a ``job.*`` waiting message key."""

    def close_removed_upload(self, job: dict[str, Any]) -> bool:
        """End the Job of a removed source; False when the claim was lost."""

    def close_withdrawn_upload(self, job: dict[str, Any]) -> bool:
        """End the Job of a withdrawn observation; False when the claim was lost."""

    def upload_protection_paused(self, job: dict[str, Any]) -> bool:
        """Whether the Vault or its policy no longer protects this path."""


class UploadTransport(Protocol):
    """The provider side: the client, the send, the proofs and the cleanup."""

    def s3_client(self) -> Any:
        """The worker's shared S3 client."""

    def validate_cloud_vault(self, job: dict[str, Any]) -> None:
        """Refuse work on a Vault whose cloud target is not usable."""

    def is_cloud_readiness_error(self, error: BaseException) -> bool:
        """Whether a provider failure is a readiness (credentials) failure."""

    def prepare_upload_snapshot(
        self, source: Path, check_active: Callable[[], None], *, payload: Any = None,
    ) -> tuple[Any, os.stat_result]:
        """Hash the stable Local Copy into its Upload Snapshot."""

    def upload_s3_file(self, job: dict[str, Any], *, source: Path, key: str) -> dict[str, Any]:
        """Send the snapshot's bytes; the provider's response."""

    def validated_s3_proof(
        self, proof: dict[str, Any], *, version_id: str, snapshot: Any, source: str,
        completion: bool = False,
    ) -> dict[str, str] | None:
        """The provider's checksum evidence, checked against the snapshot."""

    def verification_version_head(
        self, client: Any, job: dict[str, Any], target: dict[str, Any],
    ) -> dict[str, Any]:
        """HEAD of the exact VersionId under verification."""

    def stream_plaintext_for_verification(
        self, job: dict[str, Any], *, target: dict[str, Any], source_size: int,
        check_active: Callable[[], None] | None = None, floor_reported: bool = False,
    ) -> tuple[str, int]:
        """Read the exact VersionId back; its SHA-256 and size."""

    def cleanup_superseded_upload(
        self, job: dict[str, Any], *, keeper_id: str | None, client: Any = None,
    ) -> None:
        """Exact cleanup of what the keeper superseded (``snapshot_cleanup``)."""

    def require_sole_keeper(
        self, job: dict[str, Any], keeper: dict[str, Any], *, client: Any, bucket: str,
    ) -> None:
        """Postcondition of a keeper that superseded nothing."""


class CopyWorker(JobClaim, SourceVolume, UploadCatalog, UploadTransport, Protocol):
    """What ``copy_provenance`` and ``snapshot_cleanup`` ask of the worker."""


class UploadWorker(CopyWorker, AutomaticUploadGate, Protocol):
    """What ``upload_execution`` asks of the worker."""
