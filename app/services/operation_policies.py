"""Per-Vault operation policies for automation and transfer controls.

This module is the operation-policy seam used by watcher/scan auto-upload,
automatic local cleanup, and HTTP configuration. A missing row means automatic
upload with a five-minute observed stability window and local release OFF.
Upload exclusions never restrict local release, which covers all Protected files.
Fixed upload noise exclusions cannot be overridden; manual operations remain explicit.
The only transfer throttle is the global ``BANDWIDTH_LIMIT_KIBPS`` for uploads.
"""

from __future__ import annotations

import json
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any, Sequence



# Fixed durations, not calendar-month arithmetic: 3/6 months, 1/2 years.
LOCAL_RELEASE_PERIODS = (90, 180, 365, 730)
DEFAULT_STABILITY_SECONDS = 300
AUTO_UPLOAD_EXCLUSIONS = (".DS_Store", "Thumbs.db", "._*", "*.lock", "*-wal", "*-journal")


def stopped_upload_holds_automatic_admission_sql(job: str, file: str) -> str:
    """SQL predicate: this upload Job keeps automatic admission off its File.

    A failed automatic upload, any cancelled automatic upload and any upload the
    Owner cancelled (``job.upload_stopped``, manual or automatic) hold the File
    until its content is observed again (#618). A manual upload closed for
    another reason (removed file, metadata restore) or a failed manual upload
    does not.
    """
    return f"""(
        {job}.action='upload'
        AND {job}.requested_at >= COALESCE({file}.local_content_observed_at, {file}.local_observed_at)
        AND (
            ({job}.origin='automatic' AND {job}.status IN ('failed', 'cancelled'))
            OR ({job}.status='cancelled' AND {job}.message_key='job.upload_stopped')
        )
    )"""


def stopped_upload_sql(file: str) -> str:
    """SQL predicate: ``file`` is a Stopped Upload (#630).

    Automatic admission skips the File because an upload Job holds it
    (:func:`stopped_upload_holds_automatic_admission_sql`, the same predicate
    admission and upload activity read) and no Job of the File is running or
    waiting, so nothing will start it until its content is observed again. A
    manual upload the Owner asks for is an active Job, so the File is no
    longer stopped while it runs. The subqueries are served by
    ``jobs_one_active_per_file_uq`` and ``jobs_file_idx``.
    """
    return f"""(
        NOT EXISTS (
            SELECT 1 FROM jobs stopped_active WHERE stopped_active.file_id={file}.id
              AND stopped_active.status NOT IN ('completed', 'failed', 'cancelled')
        )
        AND EXISTS (
            SELECT 1 FROM jobs stopped_hold WHERE stopped_hold.file_id={file}.id
              AND {stopped_upload_holds_automatic_admission_sql("stopped_hold", file)}
        )
    )"""


def auto_upload_path_is_included(path: str, policy: OperationPolicy) -> bool:
    """Fixed noise exclusions are additional to, never editable via, user globs."""
    return not any(
        fnmatchcase(part, pattern)
        for part in PurePosixPath(path.replace("\\", "/")).parts
        for pattern in AUTO_UPLOAD_EXCLUSIONS
    ) and path_is_included(path, policy)


@dataclass(frozen=True)
class OperationPolicy:
    auto_upload: bool = True
    local_release_after_days: int | None = None
    stability_seconds: int = DEFAULT_STABILITY_SECONDS
    include_globs: tuple[str, ...] = ()
    exclude_globs: tuple[str, ...] = ()
    upload_verification: str | None = None

    def effective_upload_verification(self) -> str:
        from ..config import settings
        return self.upload_verification or settings.upload_verification

    def as_dict(self) -> dict[str, Any]:
        return {
            "auto_upload": self.auto_upload,
            "local_release_after_days": self.local_release_after_days,
            "stability_seconds": self.stability_seconds,
            "include_globs": list(self.include_globs),
            "exclude_globs": list(self.exclude_globs),
            "upload_verification": self.upload_verification,
        }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_json_list(raw: str | None) -> tuple[Any, ...]:
    if not raw:
        return ()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("expected a JSON list")
    return tuple(data)


def _normalize_globs(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        text = str(value).strip().replace("\\", "/")
        if not text:
            continue
        if text.startswith("/"):
            raise ValueError("globs must be relative paths")
        normalized.append(text)
    return tuple(normalized)


def validate_policy(policy: OperationPolicy) -> None:
    if policy.upload_verification not in {None, "s3_checksum", "read_back"}:
        raise ValueError("upload_verification must be null, s3_checksum or read_back")
    if policy.stability_seconds < 0:
        raise ValueError("stability_seconds must be nonnegative")
    if policy.local_release_after_days is not None and (
        type(policy.local_release_after_days) is not int
        or policy.local_release_after_days not in LOCAL_RELEASE_PERIODS
    ):
        raise ValueError("local_release_after_days must be null, 90, 180, 365 or 730")
    _normalize_globs(policy.include_globs)
    _normalize_globs(policy.exclude_globs)


def validate_policy_globs(policy: OperationPolicy, *, stored: OperationPolicy | None = None) -> None:
    """Refuse a glob PostgreSQL cannot read safely (it also classifies files, #498).

    Only used when saving: globs already ``stored`` are not re-judged, so an older
    unsupported glob never blocks changing an unrelated setting, and removing it
    is always allowed.
    """
    from .upload_path_rules import MAX_GLOBS_PER_POLICY, UnsafeGlob, validate_glob
    kept = {*_normalize_globs(stored.include_globs), *_normalize_globs(stored.exclude_globs)} if stored else set()
    globs = (*_normalize_globs(policy.include_globs), *_normalize_globs(policy.exclude_globs))
    new = [glob for glob in globs if glob not in kept]
    if new and len(globs) > MAX_GLOBS_PER_POLICY:
        raise UnsafeGlob(f"at most {MAX_GLOBS_PER_POLICY} globs per policy")
    for glob in new:
        validate_glob(glob)


def get_policy(connection: Any, vault_id: int) -> OperationPolicy:
    row = connection.execute(
        "SELECT * FROM vault_operation_policies WHERE vault_id=%s",
        (vault_id,),
    ).fetchone()
    if not row:
        return OperationPolicy()
    return OperationPolicy(
        auto_upload=_as_bool(row["auto_upload"]),
        local_release_after_days=row["local_release_after_days"],
        stability_seconds=int(row["stability_seconds"]),
        include_globs=tuple(_load_json_list(row["include_globs_json"])),
        exclude_globs=tuple(_load_json_list(row["exclude_globs_json"])),
        upload_verification=row.get("upload_verification"),
    )


def set_policy(
    connection: Any, vault_id: int, policy: OperationPolicy
) -> OperationPolicy:
    validate_policy(policy)
    locked = connection.execute(
        """
        UPDATE vaults SET name=name
        WHERE id=%s
        RETURNING id, lifecycle
        """,
        (vault_id,),
    ).fetchone()
    if not locked:
        raise LookupError("vault_not_found")
    if locked["lifecycle"] != "active":
        raise LookupError("vault_quiesced")
    validate_policy_globs(policy, stored=get_policy(connection, vault_id))
    include_globs = _normalize_globs(policy.include_globs)
    exclude_globs = _normalize_globs(policy.exclude_globs)
    canonical = OperationPolicy(
        auto_upload=bool(policy.auto_upload),
        local_release_after_days=policy.local_release_after_days,
        stability_seconds=int(policy.stability_seconds),
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        upload_verification=policy.upload_verification,
    )
    connection.execute(
        """
        INSERT INTO vault_operation_policies(
            vault_id, auto_upload, local_release_after_days, stability_seconds,
            include_globs_json, exclude_globs_json, upload_verification, suspended_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN NULL ELSE %s::timestamptz END)
        ON CONFLICT(vault_id) DO UPDATE SET
            auto_upload=excluded.auto_upload,
            local_release_after_days=excluded.local_release_after_days,
            stability_seconds=excluded.stability_seconds,
            include_globs_json=excluded.include_globs_json,
            exclude_globs_json=excluded.exclude_globs_json,
            upload_verification=excluded.upload_verification,
            -- In the same statement: the suspension CHECK holds per row. Keep
            -- the start of one continuous suspension, including across restart.
            suspended_at=CASE WHEN excluded.auto_upload THEN NULL
                ELSE COALESCE(vault_operation_policies.suspended_at, excluded.suspended_at) END
        """,
        (
            vault_id,
            canonical.auto_upload,
            canonical.local_release_after_days,
            canonical.stability_seconds,
            json.dumps(list(canonical.include_globs)),
            json.dumps(list(canonical.exclude_globs)),
            canonical.upload_verification,
            canonical.auto_upload,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return canonical


def _path_matches(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    # Support ** across path segments the way operators expect for vault trees.
    if "**" in pattern:
        # Convert glob to a sequence of fnmatch checks over suffixes/prefixes.
        parts = pattern.split("/")
        if pattern.endswith("/**"):
            prefix = pattern[: -len("/**")]
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        if pattern.startswith("**/"):
            suffix = pattern[3:]
            if fnmatchcase(normalized, suffix):
                return True
            if any(
                fnmatchcase(str(PurePosixPath(*candidate.parts[index:])), suffix)
                for index in range(len(candidate.parts))
            ):
                return True
        return fnmatchcase(normalized, pattern)
    return fnmatchcase(normalized, pattern) or any(
        fnmatchcase(part, pattern) for part in candidate.parts
    )


def path_is_included(path: str, policy: OperationPolicy) -> bool:
    normalized = path.replace("\\", "/")
    if policy.include_globs and not any(
        _path_matches(normalized, pattern) for pattern in policy.include_globs
    ):
        return False
    if any(_path_matches(normalized, pattern) for pattern in policy.exclude_globs):
        return False
    return True


def preview_glob_rules(
    *,
    paths: Sequence[str],
    include_globs: Sequence[str] = (),
    exclude_globs: Sequence[str] = (),
) -> dict[str, list[str]]:
    policy = OperationPolicy(
        include_globs=_normalize_globs(include_globs),
        exclude_globs=_normalize_globs(exclude_globs),
    )
    included: list[str] = []
    excluded: list[str] = []
    for path in paths:
        if auto_upload_path_is_included(path, policy):
            included.append(path)
        else:
            excluded.append(path)
    return {"included": included, "excluded": excluded}


def file_is_stable(
    *,
    mtime_ns: int,
    now: datetime,
    stability_seconds: int,
    content_observed_at: str | datetime | None = None,
) -> bool:
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    mtime = datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc)
    # Copy tools can preserve an old mtime. The observed content must also
    # remain unchanged for a complete window, across scans and restarts.
    if content_observed_at:
        observed = (content_observed_at if isinstance(content_observed_at, datetime)
                    else datetime.fromisoformat(content_observed_at))
        mtime = max(mtime, _aware(observed))
    age = (current - mtime).total_seconds()
    return age >= stability_seconds


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def local_cleanup_is_due(
    *,
    verified_at: str | datetime,
    retention_days: int,
    now: datetime,
    local_mtime_ns: int | None = None,
    recovered_at: str | datetime | None = None,
    content_observed_at: str | datetime | None = None,
) -> bool:
    """Return True once the Local Copy has been both protected and untouched.

    The retention window counts from the latest of the Archive Version's
    verification, actual content observation, local modification and recovery.
    Repeated unchanged scans do not restart the window; old imported timestamps
    cannot shorten it.
    A recovered file therefore starts a fresh window from the moment it was
    written back, instead of being removed again on the next scan because its
    cloud copy was verified long ago.  The recovery time is explicit because a
    recovered file may keep its original (old) modification time.
    """
    def moment(value: str | datetime) -> datetime:
        return _aware(value if isinstance(value, datetime) else datetime.fromisoformat(value))

    reference = moment(verified_at)
    for observed in (recovered_at, content_observed_at):
        if observed is not None:
            reference = max(reference, moment(observed))
    if local_mtime_ns is not None:
        modified = datetime.fromtimestamp(int(local_mtime_ns) / 1e9, tz=timezone.utc)
        if modified > reference:
            reference = modified
    return _aware(now) >= reference + timedelta(days=retention_days)


def queue_auto_uploads(
    connection: Any,
    *,
    vault_id: int,
    source_root: str,
    now: datetime | None = None,
    retry_failed_only: bool = False,
) -> int:
    """Queue stable eligible uploads, optionally only latest failed attempts.

    Explicit retry does not edit history or revive cancelled work. The caller
    holds the Vault admission lock; the same catalog admission dedupes inserts.
    """
    from ..catalog import ArchiveCatalog, awaiting_rename_analysis_sql
    from .vault_paths import safe_local_path

    policy = get_policy(connection, vault_id)
    if not policy.auto_upload:
        return 0
    current = now or datetime.now(timezone.utc)
    vault = connection.execute(
        "SELECT lifecycle FROM vaults WHERE id=%s", (vault_id,),
    ).fetchone()
    if vault is None or vault["lifecycle"] != "active":
        return 0
    catalog = ArchiveCatalog(connection)
    # One row per File (#587): its Local Copy and its matched Archive Version.
    # A purge in progress is an active Job of the File, so it is excluded too.
    rows = connection.execute(
        f"""
        SELECT f.id AS file_id, f.path, f.local_size AS size, f.local_mtime_ns AS mtime_ns,
               COALESCE(f.local_content_observed_at, f.local_observed_at) AS content_observed_at,
               f.local_sha256 AS plaintext_sha256, f.matched_version_id AS matched_archive_version_id,
               av.integrity AS matched_integrity,
               av.availability AS matched_availability,
               av.plaintext_sha256 AS matched_sha256
        FROM files f
        LEFT JOIN archive_versions av ON av.id=f.matched_version_id
        WHERE f.vault_id=%s
          AND f.local_presence='present'
          AND f.local_file_type='regular'
          AND f.held_version_id IS NULL
          -- A possible rename is not a new file until its digest decides (#621).
          AND NOT {awaiting_rename_analysis_sql("f")}
          AND NOT (
              f.matched_version_id IS NOT NULL
              AND f.local_sha256 IS NOT NULL
              AND av.integrity IS NOT DISTINCT FROM 'verified'
              AND av.availability IS NOT DISTINCT FROM 'available'
              AND av.plaintext_sha256 IS NOT DISTINCT FROM f.local_sha256
          )
          AND NOT EXISTS (
              SELECT 1 FROM jobs active_job WHERE active_job.file_id=f.id
                AND active_job.status NOT IN ('completed', 'failed', 'cancelled')
          )
          AND (
              (%s AND (SELECT latest.status FROM jobs latest
                       WHERE latest.file_id=f.id AND latest.action='upload'
                       ORDER BY latest.id DESC LIMIT 1)='failed')
              OR (NOT %s AND NOT EXISTS (
                  SELECT 1 FROM jobs stopped WHERE stopped.file_id=f.id
                    AND {stopped_upload_holds_automatic_admission_sql("stopped", "f")}
              ))
          )
        ORDER BY lower(f.path)
        """,
        (vault_id, retry_failed_only, retry_failed_only),
    ).fetchall()
    queued = 0
    timestamp = current.isoformat()
    for row in rows:
        path = row["path"]
        if not auto_upload_path_is_included(path, policy):
            continue
        # Already protected only when the linked Archive Version is still
        # verified, available, and digest-matching. Missing/mismatched copies
        # must not be treated as uploaded.
        if (
            row["matched_archive_version_id"]
            and row["plaintext_sha256"]
            and row["matched_integrity"] == "verified"
            and row["matched_availability"] == "available"
            and row["matched_sha256"] == row["plaintext_sha256"]
        ):
            continue
        try:
            local_path = safe_local_path(source_root, path)
            stat_result = local_path.stat(follow_symlinks=False)
        except (OSError, ValueError, RuntimeError):
            continue
        if (
            not stat.S_ISREG(stat_result.st_mode)
            or row["size"] != stat_result.st_size
            or row["mtime_ns"] != stat_result.st_mtime_ns
        ):
            continue
        if not file_is_stable(
            mtime_ns=int(stat_result.st_mtime_ns),
            now=current,
            stability_seconds=policy.stability_seconds,
            content_observed_at=(row["content_observed_at"].isoformat()
                                 if row["content_observed_at"] else None),
        ):
            continue
        job_ids, _total, _eligible = catalog.queue_jobs(
            vault_id=vault_id,
            path=path,
            action="upload",
            requested_at=timestamp,
            group_id=uuid.uuid4().hex,
            is_directory=False,
            origin="automatic",
        )
        queued += len(job_ids)
    return queued


def local_release_is_confirmed(
    connection: Any, vault_id: int
) -> bool:
    """Fail closed on absent/untrusted sources or lost cloud observability.

    This is additional to, never a substitute for, the worker's exact-version
    HEAD and local digest checks. Disabling a Vault is not consent to deletion.
    """
    from .component_health import load_vault_health
    from .protection_states import observation_is_unconfirmed
    from .source_layout import vault_local_access

    vault = connection.execute(
        "SELECT source_root, lifecycle FROM vaults WHERE id=%s", (vault_id,),
    ).fetchone()
    # Only an active Vault: reconnect_required, stopping and tombstone never
    # release a Local Copy (#607).
    if not vault or vault["lifecycle"] != "active":
        return False
    return (vault_local_access(vault["source_root"]).local_operations_allowed
            and not observation_is_unconfirmed(load_vault_health(connection, vault_id)))


def queue_auto_local_cleanups(
    connection: Any,
    *,
    vault_id: int,
    now: datetime | None = None,
) -> int:
    """Queue safe Local Copy cleanup Jobs when the current policy is due."""
    from ..catalog import ArchiveCatalog
    from .audit_events import record_audit_event
    from .protection_states import CURRENT_JOIN_SQL, PROTECTED_SQL

    policy = get_policy(connection, vault_id)
    if policy.local_release_after_days is None:
        return 0
    validate_policy(policy)
    if not local_release_is_confirmed(connection, vault_id):
        return 0
    current = now or datetime.now(timezone.utc)
    # The present Local Copy is matched to its verified, available Current:
    # the explicit pointer, never the latest version by ordering (ADR-0035).
    rows = connection.execute(
        f"""
        SELECT f.path, cur.id::text AS archive_version_id, cur.verified_at,
               f.local_mtime_ns,
               coalesce(f.local_content_observed_at, f.local_observed_at) AS content_observed_at,
               f.local_recovered_at
        FROM files f {CURRENT_JOIN_SQL}
        WHERE f.vault_id=%s
          AND {PROTECTED_SQL}
          AND f.held_version_id IS NULL
          AND cur.verified_at IS NOT NULL
        ORDER BY lower(f.path), f.path
        """,
        (vault_id,),
    ).fetchall()
    catalog = ArchiveCatalog(connection)
    timestamp = current.isoformat()
    queued = 0
    for row in rows:
        if not local_cleanup_is_due(
            verified_at=row["verified_at"],
            retention_days=policy.local_release_after_days,
            now=current,
            local_mtime_ns=row["local_mtime_ns"],
            recovered_at=row["local_recovered_at"],
            content_observed_at=row["content_observed_at"],
        ):
            continue
        job_ids, _total, _eligible = catalog.queue_jobs(
            vault_id=vault_id,
            path=row["path"],
            action="free-space",
            requested_at=timestamp,
            group_id=uuid.uuid4().hex,
            is_directory=False,
            archive_version_id=row["archive_version_id"],
            origin="automatic",
        )
        for job_id in job_ids:
            record_audit_event(
                connection,
                event="local_cleanup.auto_queued",
                actor="system",
                vault_id=vault_id,
                job_id=job_id,
                outcome="queued",
                path=row["path"],
                archive_version_id=row["archive_version_id"],
            )
        queued += len(job_ids)
    return queued
