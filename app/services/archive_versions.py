"""Archive Versions of a Vault File: the replacement in progress and Current (#803).

A Vault File has at most one replacement in progress and one Current. This
Module owns the Archive Version rows behind them and nothing else writes them:

- the replacement in progress and its only way to become Current
  (:meth:`ArchiveVersions.begin_replacement`,
  :meth:`ArchiveVersions.promote_replacement`,
  :meth:`ArchiveVersions.abandon_replacement`);
- what a complete listing proves about availability
  (:meth:`ArchiveVersions.mark_archive_versions_listed`,
  :meth:`ArchiveVersions.mark_unseen_archive_versions_missing`);
- Reconnect after a metadata restore
  (:meth:`ArchiveVersions.adopt_cloud_path`,
  :meth:`ArchiveVersions.close_absent_superseded_versions`);
- the Archive Version a Job works on and its evidence
  (:meth:`ArchiveVersions.link_job_version`,
  :meth:`ArchiveVersions.get_job_target`,
  :meth:`ArchiveVersions.record_verification_evidence`,
  :meth:`ArchiveVersions.update_restore_state`).

The observation of Local Copies belongs to ``local_copies``; what an upload, a
verification or a restore does in the cloud belongs to the worker.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Sequence

from .catalog_shared import (
    as_timestamp,
    id_text,
    required_timestamp,
    sha256_digest,
)
from .directory_aggregates import (
    mark_file_id_dirty,
    mark_path_dirty,
    request_vault_rebuild,
)
from .job_queue import TERMINAL_JOB_STATUSES
from .vault_locks import vault_lock_touch


class ReplacementInProgress(RuntimeError):
    """The Vault File already has its one replacement in progress."""


class StaleReplacement(LookupError):
    """The named Archive Version is no longer this file's replacement in progress."""


def _restore_expiry(value: Any) -> datetime | None:
    """The provider's restore expiry (an HTTP date or ISO text), or None."""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ArchiveVersions:
    """The Archive Version rows of a Vault File, on the caller's transaction."""

    def __init__(self, connection: Any):
        self.connection = connection

    # -- Current Snapshot and the replacement in progress --------------------

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
        """Record the uploaded object as this file's one replacement in progress.

        The Archive Version is born unverified and available at its exact
        provider identity; Current is unchanged until
        :meth:`promote_replacement`. Raises :class:`ReplacementInProgress` when
        another replacement is already in progress (nothing is written).
        ``upload_attempt_id`` names the landed upload attempt that produced it:
        its snapshot is the version's expected proof (#588), and the database
        refuses any key, size or VersionId other than the attempt's.
        A version written straight into ``storage_class`` enters it when it
        is created: that is its Class Entry (``storage_class_since``, #731).
        ``class_entry_recorded=False`` (a version adopted from a listing after
        a metadata restore, #743) leaves the Class Entry unrecorded, so it is
        estimated from ``created_at`` and the Storage Plan.
        """
        if not provider_version_id:
            raise ValueError("A provider VersionId is required")
        stamp = as_timestamp(created_at) or datetime.now(timezone.utc)
        row = self.connection.execute(
            """
            WITH target AS (
                SELECT id FROM files
                WHERE id=%s AND replacing_version_id IS NULL
                FOR UPDATE
            ), version AS (
                INSERT INTO archive_versions(
                    file_id, object_key, provider_version_id, size, storage_class,
                    storage_class_since, availability, availability_checked_at,
                    created_at, upload_attempt_id
                )
                SELECT target.id, %s, %s, %s, %s,
                       CASE WHEN %s::text IS NOT NULL THEN %s::timestamptz END,
                       'available', %s, %s, %s FROM target
                RETURNING id, file_id
            )
            UPDATE files f
            SET replacing_version_id = version.id,
                cloud_since = COALESCE(f.cloud_since, %s)
            FROM version
            WHERE f.id = version.file_id
            RETURNING version.id AS version_id, f.vault_id, f.path
            """,
            (
                vault_file_id, object_key, provider_version_id, size,
                storage_class, storage_class if class_entry_recorded else None,
                stamp, stamp, stamp, upload_attempt_id, stamp,
            ),
        ).fetchone()
        if row is None:
            raise ReplacementInProgress("The Vault File already has a replacement in progress")
        mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])
        return str(row["version_id"])

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
        """Verify the replacement in progress and make it Current, atomically.

        This is the only way Current changes. The superseded version stays
        unreferenced until its exact cleanup removes it (#591). The Local Copy
        becomes matched only when it is still the observation
        ``local_generation`` and its fingerprint equals the verified digest.
        Returns whether the Local Copy is matched to the new Current.

        ``replacement_locked``: the caller read this file row FOR UPDATE in
        this transaction and saw ``archive_version_id`` as its replacement.
        """
        digest = sha256_digest(plaintext_sha256)
        stamp = required_timestamp(verified_at)
        if not replacement_locked:
            target = self.connection.execute(
                "SELECT replacing_version_id FROM files WHERE id=%s FOR UPDATE",
                (vault_file_id,),
            ).fetchone()
            if target is None or id_text(target["replacing_version_id"]) != str(archive_version_id):
                raise StaleReplacement("The Archive Version is not the replacement in progress")
        verified = self.connection.execute(
            """
            UPDATE archive_versions
            SET integrity='verified', verified_at=%s, plaintext_sha256=%s
            WHERE id=%s AND file_id=%s AND integrity='unverified'
              AND (plaintext_sha256 IS NULL OR plaintext_sha256=%s)
            RETURNING id
            """,
            (stamp, digest, archive_version_id, vault_file_id, digest),
        ).fetchone()
        if verified is None:
            raise StaleReplacement("The replacement cannot be verified with this digest")
        matches = """(
            %(generation)s::bigint IS NOT NULL
            AND f.generation = %(generation)s::bigint
            AND f.local_presence = 'present'
            AND f.local_sha256 = %(digest)s
        )"""
        row = self.connection.execute(
            f"""
            UPDATE files f
            SET current_version_id = f.replacing_version_id,
                replacing_version_id = NULL,
                held_version_id = NULL,
                matched_version_id = CASE WHEN {matches} THEN f.replacing_version_id END,
                pending_since = CASE WHEN {matches} THEN NULL ELSE f.pending_since END
            WHERE f.id = %(id)s AND f.replacing_version_id = %(version)s
            RETURNING f.vault_id, f.path, f.matched_version_id
            """,
            {
                "generation": local_generation,
                "digest": digest,
                "id": vault_file_id,
                "version": archive_version_id,
            },
        ).fetchone()
        if row is None:
            raise StaleReplacement("The Archive Version is not the replacement in progress")
        mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])
        return row["matched_version_id"] is not None

    def abandon_replacement(
        self,
        vault_file_id: str,
        *,
        archive_version_id: str,
        mismatch_sha256: str | bytes | None = None,
        checked_at: Any | None = None,
        mismatch: bool = False,
    ) -> None:
        """End the replacement in progress without touching Current.

        With ``mismatch_sha256`` (the digest a read-back proved) or
        ``mismatch`` (contradictory provider checksum evidence, no digest) the
        version is recorded as a proven mismatch. The version stays as
        superseded until its exact cleanup (#591): a failed upload or
        verification always preserves the previous copy.
        """
        if mismatch_sha256 is not None or mismatch:
            self.connection.execute(
                """
                UPDATE archive_versions
                SET integrity='mismatch', plaintext_sha256=%s, verified_at=%s
                WHERE id=%s AND file_id=%s AND integrity='unverified'
                """,
                (
                    None if mismatch_sha256 is None else sha256_digest(mismatch_sha256),
                    required_timestamp(checked_at), archive_version_id, vault_file_id,
                ),
            )
        row = self.connection.execute(
            """
            UPDATE files SET replacing_version_id=NULL
            WHERE id=%s AND replacing_version_id=%s
            RETURNING vault_id, path
            """,
            (vault_file_id, archive_version_id),
        ).fetchone()
        if row is None:
            raise StaleReplacement("The Archive Version is not the replacement in progress")
        mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])

    # -- what a complete listing proves ---------------------------------------

    def mark_archive_versions_listed(self, version_ids: Sequence[str], *, scan_id: Any) -> None:
        """A complete listing saw these exact versions: they are available."""
        ids = [str(item) for item in version_ids if item]
        if not ids:
            return
        rows = self.connection.execute(
            """
            UPDATE archive_versions av
            SET availability='available', availability_checked_at=%s
            FROM files f
            WHERE av.id = ANY(%s::uuid[]) AND f.id = av.file_id
            RETURNING f.vault_id, f.path
            """,
            (required_timestamp(scan_id), ids),
        ).fetchall()
        for row in rows:
            mark_path_dirty(self.connection, int(row["vault_id"]), row["path"])

    def mark_unseen_archive_versions_missing(
        self, *, vault_id: int, scan_id: Any, scan_started_at: Any,
        superseded: bool = True,
    ) -> list[dict[str, Any]]:
        """Mark unseen versions missing and return the new transitions.

        Versions with a concurrent upload Job on their file, versions seen in
        this scan and versions created after it started are left unchanged so
        they cannot open false loss incidents. With ``superseded=False`` only
        Current and the replacement in progress can become missing: a
        superseded version that is gone is not a loss (#639).
        """
        terminal = list(TERMINAL_JOB_STATUSES)
        rows = self.connection.execute(
            """
            UPDATE archive_versions av
            SET availability='missing', availability_checked_at=%s
            FROM files f
            WHERE f.id = av.file_id
              AND f.vault_id = %s
              AND av.availability <> 'missing'
              AND av.availability_checked_at IS DISTINCT FROM %s
              AND av.created_at <= %s
              AND (%s OR av.id = f.current_version_id
                   OR av.id = f.replacing_version_id)
              AND NOT EXISTS (
                  SELECT 1 FROM jobs active_upload
                  WHERE active_upload.file_id = av.file_id
                    AND active_upload.action = 'upload'
                    AND NOT (active_upload.status = ANY(%s::text[]))
              )
            RETURNING av.id, f.vault_id, av.file_id, av.object_key,
                      av.provider_version_id, f.path
            """,
            (
                required_timestamp(scan_id), vault_id, required_timestamp(scan_id),
                required_timestamp(scan_started_at), bool(superseded), terminal,
            ),
        ).fetchall()
        # Bulk reconciliation can touch an unbounded set of paths.
        request_vault_rebuild(self.connection, vault_id)
        return [
            {
                "id": str(row["id"]),
                "vault_id": int(row["vault_id"]),
                "vault_file_id": str(row["file_id"]),
                "object_key": row["object_key"],
                "provider_version_id": row["provider_version_id"],
                "path": row["path"],
            }
            for row in rows
        ]

    # -- Reconnect after a metadata restore ---------------------------------

    def adopt_cloud_path(self, vault_id: int, path: str, *, at: Any) -> str:
        """The Vault File at ``path``, created with no Local Copy if absent.

        Used only by the cloud reconciliation of a metadata restore (#638):
        content uploaded after the backup has no row in the restored catalog.
        The Local Copy stays unobserved until the local rehash finds it.
        """
        vault_lock_touch(self.connection, vault_id)
        row = self.connection.execute(
            """
            INSERT INTO files(vault_id, path, path_since) VALUES (%s, %s, %s)
            ON CONFLICT (vault_id, path) DO NOTHING
            RETURNING id
            """,
            (vault_id, path, required_timestamp(at)),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT id FROM files WHERE vault_id=%s AND path=%s", (vault_id, path),
            ).fetchone()
        mark_path_dirty(self.connection, vault_id, path)
        return str(row["id"])

    def close_absent_superseded_versions(
        self, *, vault_id: int, scan_id: Any, provider: str,
        source_root: str, root_identity: str | None,
    ) -> list[str]:
        """Close superseded versions a complete listing proved gone (#639).

        After a metadata restore the catalog still holds versions that the
        Current Snapshot cleanup deleted after the backup. Their cleanup is
        complete: they are recorded as found absent in a cleanup batch kept
        by the verified Current and removed with that proof, exactly as a
        finished cleanup does. Only landed application writes of a file
        without an open cleanup batch qualify; any other superseded version
        is left untouched. Returns the closed Archive Version ids.
        """
        stamp = required_timestamp(scan_id)
        rows = self.connection.execute(
            """
            SELECT av.id, f.id AS file_id, f.current_version_id, f.path_since
            FROM files f
            JOIN archive_versions cur
              ON cur.id = f.current_version_id AND cur.integrity = 'verified'
            JOIN archive_versions av ON av.file_id = f.id
            LEFT JOIN upload_attempts a ON a.id = av.upload_attempt_id
            LEFT JOIN copy_attempts ca ON ca.id = av.copy_attempt_id
            WHERE f.vault_id = %s
              AND av.id <> f.current_version_id
              AND av.id IS DISTINCT FROM f.replacing_version_id
              AND av.integrity <> 'mismatch'
              AND av.availability_checked_at IS DISTINCT FROM %s
              AND av.created_at <= %s
              AND ((a.outcome = 'landed' AND a.landed_version_id = av.provider_version_id)
                   OR (ca.outcome = 'landed'
                       AND ca.landed_version_id = av.provider_version_id))
              AND NOT EXISTS (SELECT 1 FROM cleanup_batches b WHERE b.file_id = f.id)
            ORDER BY f.id, av.id
            FOR UPDATE OF f
            """,
            (vault_id, stamp, stamp),
        ).fetchall()
        by_file: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_file.setdefault(str(row["file_id"]), []).append(row)
        closed: list[str] = []
        for file_id, versions in by_file.items():
            batch = self.connection.execute(
                """
                INSERT INTO cleanup_batches(vault_id, file_id, keeper_id, provider,
                                            path_since, source_root, root_identity)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (
                    vault_id, file_id, versions[0]["current_version_id"], provider,
                    versions[0]["path_since"], source_root, root_identity,
                ),
            ).fetchone()["id"]
            ids = [str(row["id"]) for row in versions]
            self.connection.execute(
                """
                INSERT INTO cleanup_candidates(batch_id, file_id, archive_version_id)
                SELECT %s, %s, unnest(%s::uuid[])
                """,
                (batch, file_id, ids),
            )
            # Found absent by the complete listing: the deletion is done.
            self.connection.execute(
                "UPDATE cleanup_candidates SET deleted_at=now() WHERE batch_id=%s",
                (batch,),
            )
            self.connection.execute(
                "DELETE FROM archive_versions WHERE id = ANY(%s::uuid[])", (ids,),
            )
            self.connection.execute("DELETE FROM cleanup_batches WHERE id=%s", (batch,))
            mark_file_id_dirty(self.connection, vault_id, file_id)
            closed.extend(ids)
        return closed

    # -- the Archive Version of a Job and its evidence -----------------------

    def link_job_version(self, job_id: int, archive_version_id: str) -> None:
        self.connection.execute(
            "UPDATE jobs SET archive_version_id=%s WHERE id=%s",
            (archive_version_id, job_id),
        )

    def record_verification_evidence(
        self, version_id: str, *, method: str, fallback: str | None,
        egress_bytes: int | None,
    ) -> None:
        """How the version's digest is being proven: written while unverified.

        Recorded before a read-back GET so its cost is visible even when the
        read fails; once verified, the evidence is history, never rewritten.
        """
        self.connection.execute(
            """
            UPDATE archive_versions
            SET verification_method=%s, verification_fallback=%s,
                verification_egress_bytes=%s
            WHERE id=%s AND integrity='unverified'
            """,
            (method, fallback, egress_bytes, version_id),
        )

    def get_job_target(self, job_id: int) -> dict[str, Any] | None:
        """The Job's File, Local Copy and linked Archive Version in one row.

        Digests are hexadecimal text. ``upload_snapshot_json`` is the linked
        version's expected proof, reached through its upload attempt (#588);
        it is never copied onto the version.
        """
        row = self.connection.execute(
            """
            SELECT
                j.id AS job_id,
                j.origin AS job_origin,
                j.admitted_generation AS job_admitted_generation,
                j.vault_id,
                j.file_id,
                j.file_id AS vault_file_id,
                j.archive_version_id,
                ju.verification_mode AS job_upload_verification_requested,
                COALESCE(ju.reconcile_only, false) AS upload_reconcile_only,
                ju.snapshot_id AS upload_snapshot_id,
                f.path,
                f.generation AS local_generation,
                f.local_presence,
                f.local_file_type,
                f.local_size,
                f.local_mtime_ns,
                f.local_recovered_at,
                COALESCE(f.local_content_observed_at, f.local_observed_at) AS content_observed_at,
                encode(f.local_sha256, 'hex') AS local_sha256,
                f.matched_version_id AS matched_archive_version_id,
                f.held_version_id AS cloud_only_version_id,
                f.current_version_id,
                f.replacing_version_id,
                av.object_key,
                av.provider_version_id,
                av.size AS cloud_size,
                av.storage_class,
                encode(av.plaintext_sha256, 'hex') AS version_sha256,
                av.integrity,
                av.availability,
                av.verified_at,
                av.verification_method,
                av.verification_fallback AS verification_fallback_reason,
                av.verification_egress_bytes,
                av.upload_attempt_id,
                av.restore_state,
                av.restore_expiry,
                jr.tier AS restore_tier,
                jr.days AS restore_days
            FROM jobs j
            JOIN files f ON f.id = j.file_id
            LEFT JOIN job_uploads ju ON ju.job_id = j.id
            LEFT JOIN job_restores jr ON jr.job_id = j.id
            LEFT JOIN archive_versions av ON av.id = j.archive_version_id
            WHERE j.id=%s
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        target = dict(row)
        target["upload_snapshot_json"] = None
        target["upload_verification_requested"] = None
        if target["archive_version_id"] is not None:
            from .upload_provenance import expected_for_version
            expected = expected_for_version(self.connection, str(target["archive_version_id"]))
            if expected is not None:
                target["upload_snapshot_json"] = expected.snapshot.as_json()
                target["upload_verification_requested"] = expected.verification
        return target

    def update_restore_state(
        self,
        archive_version_id: str,
        *,
        state: str | None,
        expiry: Any,
        checked_at: Any,
        storage_class: str | None = None,
    ) -> None:
        """Record the observed cloud restore of one exact Archive Version.

        ``state`` is ``restoring``, ``available`` or None (no restore needed or
        observed); ``expiry`` is the provider's expiry of the temporary copy,
        kept only while it is available. Integrity and availability are never
        touched (#594). A ``storage_class`` other than the recorded one moves
        the Class Entry to ``checked_at`` (#731).
        """
        if state == "not_requested":
            state = None
        from .minimum_storage import observed_since_sql

        since = observed_since_sql(
            old_class="av.storage_class", new_class="obs.storage_class",
            old_since="av.storage_class_since", observed_at="obs.checked_at",
            created_at="av.created_at", size="av.size", plan="v.storage_plan")
        row = self.connection.execute(
            f"""
            UPDATE archive_versions av
            SET restore_state=%s,
                restore_expiry=CASE WHEN %s::text='available' THEN %s::timestamptz END,
                restore_checked_at=obs.checked_at,
                storage_class_since={since},
                storage_class=COALESCE(obs.storage_class, av.storage_class)
            FROM files f
            JOIN vaults v ON v.id = f.vault_id
            CROSS JOIN (SELECT %s::text AS storage_class,
                               %s::timestamptz AS checked_at) AS obs
            WHERE av.id=%s AND f.id=av.file_id
            RETURNING f.vault_id, f.id AS file_id
            """,
            (state, state, _restore_expiry(expiry), storage_class,
             required_timestamp(checked_at), archive_version_id),
        ).fetchone()
        if row is not None:
            mark_file_id_dirty(self.connection, int(row["vault_id"]), str(row["file_id"]))
