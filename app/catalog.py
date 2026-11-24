from __future__ import annotations

from typing import Any, Sequence

from .services.catalog_shared import (
    VaultFileNotFound,
    vault_lock_touch,
)
# The names the clusters still here call them by; a cluster takes the public
# name when it moves to its own Module (#803).
from .services.catalog_shared import (
    as_timestamp as _timestamp,
    required_timestamp as _required_timestamp,
    sha256_digest as _digest,
)
# ReplacementInProgress and StaleReplacement stay importable from here: the
# callers and the tests name them through the catalog.
from .services.archive_versions import (
    ArchiveVersions,
    ReplacementInProgress,
    StaleReplacement,
)
from .services.file_listing import FileListing
from .services.file_renames import FileRenames
from .services.directory_aggregates import (
    mark_file_id_dirty,
    mark_path_dirty,
    request_vault_rebuild,
)


class ArchiveCatalog:
    """Keep Vault File invariants behind one persistence interface.

    One ``files`` row per Vault File holds its current path, Local Copy and
    explicit protection state (Current, replacement in progress, matched,
    held, pending, reconciliation block); ``file_path_history`` keeps only the
    closed paths (#587, ADR-0035).
    """

    def __init__(self, connection: Any):
        self.connection = connection

    # -- shared seams -------------------------------------------------------

    def _mark_path_aggregates_dirty(self, vault_id: int, path: str | None) -> None:
        mark_path_dirty(self.connection, vault_id, path)

    def _mark_file_aggregates_dirty(
        self, vault_id: int, vault_file_id: str | None
    ) -> None:
        mark_file_id_dirty(self.connection, vault_id, vault_file_id)

    def _request_aggregate_rebuild(self, vault_id: int) -> None:
        request_vault_rebuild(self.connection, vault_id)

    def _vault_lock_touch(self, vault_id: int) -> None:
        vault_lock_touch(self.connection, vault_id)

    # -- Archive Versions ------------------------------------------------------
    #
    # The replacement in progress, Current and the Archive Version rows live in
    # ``services.archive_versions`` (#803). These names stay for the callers
    # that still reach them through the catalog.

    def begin_replacement(
        self,
        vault_file_id: str,
        *,
        object_key: str,
        provider_version_id: str,
        size: int,
        storage_class: str | None = None,
        created_at: Any | None = None,
        upload_attempt_id: str | None = None,
        class_entry_recorded: bool = True,
    ) -> str:
        return ArchiveVersions(self.connection).begin_replacement(
            vault_file_id, object_key=object_key,
            provider_version_id=provider_version_id, size=size,
            storage_class=storage_class, created_at=created_at,
            upload_attempt_id=upload_attempt_id,
            class_entry_recorded=class_entry_recorded,
        )

    def promote_replacement(
        self,
        vault_file_id: str,
        *,
        archive_version_id: str,
        plaintext_sha256: str | bytes,
        verified_at: Any,
        local_generation: int | None = None,
        replacement_locked: bool = False,
    ) -> bool:
        return ArchiveVersions(self.connection).promote_replacement(
            vault_file_id, archive_version_id=archive_version_id,
            plaintext_sha256=plaintext_sha256, verified_at=verified_at,
            local_generation=local_generation, replacement_locked=replacement_locked,
        )

    def abandon_replacement(
        self,
        vault_file_id: str,
        *,
        archive_version_id: str,
        mismatch_sha256: str | bytes | None = None,
        checked_at: Any | None = None,
        mismatch: bool = False,
    ) -> None:
        ArchiveVersions(self.connection).abandon_replacement(
            vault_file_id, archive_version_id=archive_version_id,
            mismatch_sha256=mismatch_sha256, checked_at=checked_at, mismatch=mismatch,
        )

    def mark_archive_versions_listed(self, version_ids: Sequence[str], *, scan_id: Any) -> None:
        ArchiveVersions(self.connection).mark_archive_versions_listed(
            version_ids, scan_id=scan_id,
        )

    def mark_unseen_archive_versions_missing(
        self, *, vault_id: int, scan_id: Any, scan_started_at: Any,
        superseded: bool = True,
    ) -> list[dict[str, Any]]:
        return ArchiveVersions(self.connection).mark_unseen_archive_versions_missing(
            vault_id=vault_id, scan_id=scan_id, scan_started_at=scan_started_at,
            superseded=superseded,
        )

    def adopt_cloud_path(self, vault_id: int, path: str, *, at: Any) -> str:
        return ArchiveVersions(self.connection).adopt_cloud_path(vault_id, path, at=at)

    def close_absent_superseded_versions(
        self, *, vault_id: int, scan_id: Any, provider: str,
        source_root: str, root_identity: str | None,
    ) -> list[str]:
        return ArchiveVersions(self.connection).close_absent_superseded_versions(
            vault_id=vault_id, scan_id=scan_id, provider=provider,
            source_root=source_root, root_identity=root_identity,
        )

    def link_job_version(self, job_id: int, archive_version_id: str) -> None:
        ArchiveVersions(self.connection).link_job_version(job_id, archive_version_id)

    def record_verification_evidence(
        self, version_id: str, *, method: str, fallback: str | None,
        egress_bytes: int | None,
    ) -> None:
        ArchiveVersions(self.connection).record_verification_evidence(
            version_id, method=method, fallback=fallback, egress_bytes=egress_bytes,
        )

    def get_job_target(self, job_id: int) -> dict[str, Any] | None:
        return ArchiveVersions(self.connection).get_job_target(job_id)

    def update_restore_state(
        self,
        archive_version_id: str,
        *,
        state: str | None,
        expiry: Any,
        checked_at: Any,
        storage_class: str | None = None,
    ) -> None:
        ArchiveVersions(self.connection).update_restore_state(
            archive_version_id, state=state, expiry=expiry, checked_at=checked_at,
            storage_class=storage_class,
        )

    # -- renames --------------------------------------------------------------
    #
    # The cluster lives in ``services.file_renames`` (#803). These names stay
    # for the callers that still reach it through the catalog.

    def rename_file(
        self, vault_file_id: str, *, new_path: str, changed_at: Any, vault_id: int,
    ) -> None:
        FileRenames(self.connection).rename_file(
            vault_file_id, new_path=new_path, changed_at=changed_at, vault_id=vault_id,
        )

    def list_rename_candidates(
        self, vault_id: int, *, digest: str | bytes | None = None
    ) -> list[dict[str, Any]]:
        return FileRenames(self.connection).list_rename_candidates(vault_id, digest=digest)

    def confirm_file_rename(
        self, *, vault_file_id: str, new_path: str, changed_at: Any, vault_id: int,
    ) -> str:
        return FileRenames(self.connection).confirm_file_rename(
            vault_file_id=vault_file_id, new_path=new_path, changed_at=changed_at,
            vault_id=vault_id,
        )

    # -- reads ----------------------------------------------------------------
    #
    # The cluster lives in ``services.file_listing`` (#803). These names stay
    # for the callers that still reach it through the catalog; they read the
    # Vault as fully confirmed, as a catalog built from a connection alone did.

    def list_versions(self, vault_id: int, path: str) -> list[dict[str, Any]]:
        return FileListing(self.connection).list_versions(vault_id, path)

    def get_file_item(self, vault_id: int, path: str) -> dict[str, Any] | None:
        return FileListing(self.connection).get_file_item(vault_id, path)

    def list_files_page(
        self, vault_id: int, *, search: str = "", directory: str = "", state: str = "",
        page: int = 1, page_size: int = 100,
    ) -> dict[str, Any]:
        return FileListing(self.connection).list_files_page(
            vault_id, search=search, directory=directory, state=state,
            page=page, page_size=page_size,
        )

    def search_protected_vaults(
        self, vaults: list[dict[str, Any]], *, search: str, state: str,
        page: int, page_size: int,
    ) -> dict[str, Any]:
        return FileListing(self.connection).search_protected_vaults(
            vaults, search=search, state=state, page=page, page_size=page_size,
        )

    def summary(self, vault_id: int) -> dict[str, Any]:
        return FileListing(self.connection).summary(vault_id)
