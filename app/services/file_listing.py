"""Reads of the catalog: browse, search, file detail, versions, summary (#803, #593).

What the interface shows of a Vault's files is read here and nowhere else:

- one page of a folder or of a search (:meth:`FileListing.list_files_page`),
  the search across Vaults (:meth:`FileListing.search_protected_vaults`) and
  one file (:meth:`FileListing.get_file_item`);
- the Archive Versions a file offers (:meth:`FileListing.list_versions`) and
  the rollup of a Vault (:meth:`FileListing.summary`);
- the SQL of a listing, for the Modules that must select exactly what a list
  view shows (:meth:`FileListing.direct_child_file_clauses`,
  :meth:`FileListing.protection_state_sql`,
  :meth:`FileListing.file_row_select_sql`,
  :meth:`FileListing.materialize_file_items`).

A ``FileListing`` is bound to one connection and to what is unconfirmed in the
Vault it reads (ADR-0042): that scope is state of the reads only. It writes
nothing, except that a listing asks the directory aggregates to be there.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .catalog_shared import digest_hex, id_text
from .directory_aggregates import (
    count_child_directories,
    ensure_directory_aggregates,
    list_child_directory_rows,
    local_copy_is_upload_eligible,
    stopped_upload_counts,
)
from .job_queue import OPEN_JOB_SQL
from .operation_policies import stopped_upload_sql
from .protection_states import (
    CURRENT_JOIN_SQL, UNCONFIRMED_EXEMPT, VISIBLE_SQL, aggregate_state,
    current_is_recoverable, masked_state_sql,
    local_copy_matches_current, path_excluded_sql, project_file, state_sql,
)
from .recovery_progress import LATEST_RECOVERY_JOIN_SQL, project_recovery


# The verification the Owner requested for Current's upload, read from the
# Upload Snapshot its landed attempt sent (#588), never copied onto the version.
_REQUESTED_VERIFICATION_SQL = """
    SELECT snapshot.verification FROM upload_attempts attempt
    JOIN upload_snapshots snapshot ON snapshot.id = attempt.snapshot_id
    WHERE attempt.id = cur.upload_attempt_id
"""


def _cleanup_state(row: Mapping[str, Any]) -> str | None:
    """Exact cleanup of superseded versions: in progress, blocked or waiting.

    A frozen batch (#591) is in progress; a reconciliation block on a file
    with superseded versions or a batch means the cleanup is blocked and needs
    the Owner. Superseded versions without a batch wait for the next upload
    Job of the file. None: only Current (and a replacement) remain.
    """
    if row["cleanup_batch_id"] is None and not row["has_superseded"]:
        return None
    if row["reconciliation_block"] is not None:
        return "blocked"
    return "in_progress" if row["cleanup_batch_id"] is not None else "waiting"


class FileListing:
    """Read Vault Files as the interface shows them.

    One ``files`` row per Vault File and its explicit Current (``cur``); the
    replacement in progress is the explicit pending pointer. Nothing is ever
    chosen as "the most recent version" by ordering.
    """

    def __init__(self, connection: Any, *, protection_unconfirmed: Any = False):
        """``protection_unconfirmed``: a bool (the whole Vault or nothing) or
        an :class:`UnconfirmedScope`, the per-path rule of ADR-0042 (#684)."""
        from .operation_policies import UnconfirmedScope

        self.connection = connection
        if not isinstance(protection_unconfirmed, UnconfirmedScope):
            protection_unconfirmed = UnconfirmedScope(None if protection_unconfirmed else ())
        self.unconfirmed = protection_unconfirmed
        # The whole Vault is unconfirmed: no per-path work is needed.
        self.protection_unconfirmed = protection_unconfirmed.whole_vault
        self.last_listing_rows_materialized = 0

    def list_versions(self, vault_id: int, path: str) -> list[dict[str, Any]]:
        """Current and the replacement in progress of the file at ``path``.

        These are the only Archive Versions a Vault File can have besides
        superseded versions awaiting exact cleanup, which are never offered.
        Each row names its role explicitly; only Current can be recoverable.
        """
        rows = self.connection.execute(
            f"""
            SELECT role.role, av.id, av.object_key, av.provider_version_id,
                   av.size, av.storage_class, av.plaintext_sha256, av.integrity,
                   av.availability, av.availability_checked_at, av.verified_at,
                   av.restore_state, av.restore_expiry, av.verification_method, av.verification_fallback,
                   av.verification_egress_bytes, av.created_at, av.storage_class_since,
                   av.copy_attempt_id IS NOT NULL AS copied,
                   ({_REQUESTED_VERIFICATION_SQL.replace("cur.", "av.")})
                       AS upload_verification_requested
            FROM files f
            CROSS JOIN LATERAL (VALUES
                ('current', f.current_version_id),
                ('pending', f.replacing_version_id)
            ) AS role(role, version_id)
            JOIN archive_versions av ON av.id = role.version_id
            WHERE f.vault_id=%s AND f.path=%s
            ORDER BY role.role
            """,
            (vault_id, path),
        ).fetchall()
        versions = []
        entries = self._class_entries(vault_id, rows)
        for row, entry in zip(rows, entries):
            recoverable = (
                row["role"] == "current"
                and row["integrity"] == "verified"
                and row["availability"] == "available"
            )
            versions.append({
                "id": str(row["id"]),
                "role": row["role"],
                "object_key": row["object_key"],
                "provider_version_id": row["provider_version_id"],
                "size": row["size"],
                "storage_class": row["storage_class"],
                "plaintext_sha256": digest_hex(row["plaintext_sha256"]),
                "integrity": row["integrity"],
                "availability": row["availability"],
                "availability_checked_at": row["availability_checked_at"],
                "verified_at": row["verified_at"],
                "restore_state": row["restore_state"],
                "restore_expiry": row["restore_expiry"],
                "created_at": row["created_at"],
                # Class Entry and Minimum Storage Duration (#731).
                "storage_class_since": entry.since,
                "storage_class_since_estimated": entry.estimated,
                "expected_storage_class": entry.storage_class if entry.expected else None,
                "minimum_storage_until": entry.minimum_until,
                "upload_verification_requested": row["upload_verification_requested"],
                "verification_method": row["verification_method"],
                "verification_fallback_reason": row["verification_fallback"],
                "verification_egress_bytes": row["verification_egress_bytes"],
                "recoverable": recoverable,
                "not_selectable_reason": None if recoverable else (
                    "pending" if row["role"] == "pending"
                    else row["availability"] if row["availability"] != "available"
                    else row["integrity"]
                ),
            })
        return versions

    def _class_entries(self, vault_id: int, rows: list[dict[str, Any]]) -> list[Any]:
        """The Class Entry of each version row, estimated from the Vault's
        Storage Plan when none is recorded (#731)."""
        from .lifecycle_profiles import profile_from_json
        from .minimum_storage import class_entry

        if not rows:
            return []
        vault = self.connection.execute(
            "SELECT storage_plan::text AS plan FROM vaults WHERE id=%s", (vault_id,)).fetchone()
        plan = profile_from_json(vault["plan"] if vault else None)
        now = datetime.now(timezone.utc)
        # Only the current version and the one replacing it can transition.
        return [class_entry(storage_class=row["storage_class"], since=row["storage_class_since"],
                            created_at=row["created_at"], size=row["size"], plan=plan, now=now,
                            current=row["role"] in {"current", "pending"},
                            copied=bool(row["copied"]))
                for row in rows]

    def get_file_item(self, vault_id: int, path: str) -> dict[str, Any] | None:
        """The browse item of the visible file at exactly ``path``."""
        rows = self.connection.execute(
            self.file_row_select_sql(["f.vault_id=%s", "f.path=%s", VISIBLE_SQL], vault_id),
            (vault_id, path),
        ).fetchall()
        items = self.materialize_file_items(vault_id, rows, name_from_path=True)
        return items[0] if items else None

    def list_files_page(
        self,
        vault_id: int,
        *,
        search: str = "",
        directory: str = "",
        state: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """Return one page of browse/search items without full-catalog materialization.

        Browse mode reads durable directory aggregates for child folders and
        pages direct child files in SQL. Search mode filters and pages Vault
        File rows in SQL. Both paths keep ``last_listing_rows_materialized``
        equal to the returned page cardinality (not the descendant total).
        """
        aggregate_status = ensure_directory_aggregates(self.connection, vault_id)
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        if search:
            items, total = self._search_files_page(
                vault_id,
                search=search,
                state=state,
                page=page,
                page_size=page_size,
            )
            mode = "search"
        else:
            items, total = self._browse_directory_page(
                vault_id,
                directory=directory or "",
                state=state,
                page=page,
                page_size=page_size,
            )
            mode = "browse"
        self.last_listing_rows_materialized = len(items)
        return {
            "items": items,
            "total": total,
            "page": page,
            "directory": directory or "",
            "mode": mode,
            # ready | loading | stale — listing never blocks on full rebuild.
            "aggregate_status": aggregate_status,
        }

    def search_protected_vaults(
        self, vaults: list[dict[str, Any]], *, search: str, state: str,
        page: int, page_size: int,
    ) -> dict[str, Any]:
        """SQL-bounded cross-Vault search; never change Session selection.

        Each branch uses that Vault's current source observation. Filtering before
        LIMIT and COUNT prevents false Cloud-only matches on unavailable mounts.
        Only the requested page is materialized, including duplicate paths.
        """
        branches, params = [], []
        for vault in vaults:
            listing = FileListing(self.connection, protection_unconfirmed=vault["unconfirmed"])
            clauses, values = listing.direct_child_file_clauses(
                vault["id"], directory="", state=state, search=search or "%",
            )
            sql = listing.file_row_select_sql(clauses, vault["id"])
            branches.append(f"SELECT scoped.*, %s::text AS vault_name FROM ({sql}) scoped")
            params.extend([vault["name"], *values])
        if not branches:
            return {"items": [], "total": 0, "page": page, "mode": "search", "directory": "", "aggregate_status": "ready"}
        union = " UNION ALL ".join(branches)
        total = self.connection.execute(f"SELECT count(*) AS total FROM ({union}) matches", params).fetchone()["total"]
        rows = self.connection.execute(
            f"SELECT * FROM ({union}) matches ORDER BY lower(path), path, vault_id LIMIT %s OFFSET %s",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        by_id = {vault["id"]: vault for vault in vaults}
        items = []
        for row in rows:
            vault = by_id[row["vault_id"]]
            listing = FileListing(self.connection, protection_unconfirmed=vault["unconfirmed"])
            for item in listing.materialize_file_items(vault["id"], [row], name_from_path=True):
                items.append({**item, "vault_id": vault["id"], "vault_name": vault["name"]})
        return {"items": items, "total": total, "page": page, "mode": "search", "directory": "", "aggregate_status": "ready"}

    def _browse_directory_page(
        self,
        vault_id: int,
        *,
        directory: str,
        state: str,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        parent_path = directory or ""
        folder_state = state
        stopped: dict[str, int] = {}
        if not self.protection_unconfirmed:
            # The projection holds Stopped Uploads as pending (#630).
            from .operation_policies import get_policy
            stopped = stopped_upload_counts(
                self.connection, vault_id, parent_path=parent_path,
                policy=get_policy(self.connection, vault_id),
            )
        folder_total = count_child_directories(
            self.connection,
            vault_id,
            parent_path=parent_path,
            state_filter=folder_state,
            stopped=stopped,
            unconfirmed=self.unconfirmed,
        )
        file_total = self._count_direct_child_files(
            vault_id,
            directory=parent_path,
            state=state,
        )
        total = folder_total + file_total
        offset = (page - 1) * page_size
        items: list[dict[str, Any]] = []
        if offset < folder_total:
            folders = list_child_directory_rows(
                self.connection,
                vault_id,
                parent_path=parent_path,
                state_filter=folder_state,
                limit=page_size,
                offset=offset,
                unconfirmed=self.unconfirmed,
                stopped=stopped,
            )
            items.extend(folders)
            remaining = page_size - len(items)
            if remaining > 0:
                items.extend(
                    self._list_direct_child_files(
                        vault_id,
                        directory=parent_path,
                        state=state,
                        limit=remaining,
                        offset=0,
                    )
                )
        else:
            file_offset = offset - folder_total
            items.extend(
                self._list_direct_child_files(
                    vault_id,
                    directory=parent_path,
                    state=state,
                    limit=page_size,
                    offset=file_offset,
                )
            )
        return items, total

    def direct_child_file_clauses(
        self,
        vault_id: int,
        *,
        directory: str,
        state: str,
        search: str = "",
    ) -> tuple[list[str], list[Any]]:
        clauses = ["f.vault_id=%s"]
        params: list[Any] = [vault_id]
        if search:
            clauses.append("lower(f.path) LIKE lower(%s)")
            params.append(f"%{search}%")
        elif directory:
            # Immediate children only; starts_with treats wildcards literally.
            clauses.append("starts_with(f.path, %s)")
            params.append(f"{directory}/")
            clauses.append("strpos(substr(f.path, %s), '/') = 0")
            params.append(len(directory) + 2)
        else:
            clauses.append("strpos(f.path, '/') = 0")
        clauses.append(VISIBLE_SQL)
        if state:
            if self.protection_unconfirmed or self.unconfirmed.by_path:
                # Only the blocked paths are unconfirmed (ADR-0042, #684);
                # Excluded and Unsupported keep their state there (#711).
                blocked, blocked_params = self.unconfirmed.sql("f.path")
                state_expression = self.protection_state_sql(vault_id)
                clauses.append(f"{masked_state_sql(blocked, state_expression)} = %s")
                params.extend([*blocked_params, state])
            else:
                clauses.append(f"({self.protection_state_sql(vault_id)}) = %s")
                params.append(state)
        return clauses, params

    def protection_state_sql(self, vault_id: int) -> str:
        """The protection CASE with this Vault's upload exclusions embedded."""
        from .operation_policies import get_policy
        return state_sql(get_policy(self.connection, vault_id))

    def _path_excluded_sql(self, vault_id: int) -> str:
        from .operation_policies import get_policy
        return path_excluded_sql(get_policy(self.connection, vault_id))

    def file_row_select_sql(self, clauses: list[str], vault_id: int) -> str:
        return f"""
            SELECT
                {self.protection_state_sql(vault_id)} AS protection_state,
                {self._path_excluded_sql(vault_id)} AS path_excluded,
                {stopped_upload_sql("f")} AS upload_stopped,
                CASE WHEN cur.availability='unknown'
                     THEN greatest(f.local_observed_at, cur.verified_at)
                     ELSE greatest(f.local_observed_at, cur.verified_at, cur.availability_checked_at)
                END AS protection_observed_at,
                f.id AS file_id,
                f.vault_id,
                f.path,
                f.local_presence,
                f.local_file_type,
                f.local_size,
                f.local_mtime_ns,
                f.local_sha256,
                f.matched_version_id,
                f.held_version_id,
                f.reconciliation_block,
                f.current_version_id,
                f.replacing_version_id,
                cur.size AS cloud_size,
                cur.storage_class,
                cur.plaintext_sha256 AS version_sha256,
                cur.integrity,
                cur.availability,
                cur.verification_method,
                cur.verification_fallback,
                cur.verification_egress_bytes,
                cur.restore_state,
                cur.restore_expiry,
                recovery.recovery_job_status,
                recovery.recovery_requested_at,
                recovery.recovery_tier,
                recovery.recovery_days,
                recovery_origin.origin_requested_at,
                recovery_origin.origin_tier,
                recovery_origin.origin_days,
                ({_REQUESTED_VERIFICATION_SQL}) AS upload_verification_requested,
                cleanup.id AS cleanup_batch_id,
                EXISTS (
                    SELECT 1 FROM archive_versions superseded
                    WHERE superseded.file_id = f.id
                      AND superseded.id IS DISTINCT FROM f.current_version_id
                      AND superseded.id IS DISTINCT FROM f.replacing_version_id
                ) AS has_superseded
            FROM files f
            {CURRENT_JOIN_SQL}
            {LATEST_RECOVERY_JOIN_SQL}
            LEFT JOIN cleanup_batches cleanup ON cleanup.file_id = f.id
            WHERE {" AND ".join(clauses)}
        """

    def materialize_file_items(
        self,
        vault_id: int,
        rows: list[dict[str, Any]],
        *,
        name_from_path: bool,
        directory: str = "",
    ) -> list[dict[str, Any]]:
        from . import excluded_folders as excluded_folder_rules

        prefix = f"{directory}/" if directory else ""
        items: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        # Derived from the current policy, like Excluded itself (ADR-0044).
        excluded = excluded_folder_rules.load(self.connection, vault_id) if rows else None
        for row in rows:
            local_exists = row["local_presence"] in {"present", "unsupported"}
            current_id = row["current_version_id"]
            cloud_exists = current_id is not None and row["availability"] != "missing"
            unconfirmed = self.unconfirmed.covers(row["path"])
            protection = project_file(row, unconfirmed=unconfirmed)
            upload_eligible = local_copy_is_upload_eligible(row)
            recoverable = current_is_recoverable(row)
            recovery = project_recovery(row, local_exists=local_exists, now=now)
            if name_from_path:
                name = row["path"]
            else:
                name = row["path"][len(prefix):] if prefix else row["path"]
            items.append(
                {
                    "type": "file",
                    "id": str(row["file_id"]),
                    "name": name,
                    "path": row["path"],
                    "local_exists": int(local_exists),
                    "local_size": row["local_size"] if local_exists else None,
                    "modified_at": (
                        datetime.fromtimestamp(int(row["local_mtime_ns"]) / 1e9, timezone.utc).isoformat()
                        if row["local_mtime_ns"] is not None else None
                    ),
                    "local_file_type": row["local_file_type"],
                    "cloud_exists": int(cloud_exists),
                    "cloud_size": row["cloud_size"],
                    "storage_class": row["storage_class"],
                    "integrity": row["integrity"],
                    "availability": row["availability"],
                    "current_version_id": id_text(current_id),
                    "pending_version_id": id_text(row["replacing_version_id"]),
                    "recoverable": recoverable,
                    "upload_verification_requested": row["upload_verification_requested"],
                    "verification_method": row["verification_method"],
                    "verification_fallback_reason": row["verification_fallback"],
                    "verification_egress_bytes": row["verification_egress_bytes"],
                    **protection,
                    "cloud_only_conflict": row["held_version_id"] is not None,
                    # Not observed: the Vault does not look inside this folder.
                    "in_excluded_folder": bool(excluded and excluded.contains_path(row["path"])),
                    "reconciliation_block": row["reconciliation_block"],
                    "cleanup": _cleanup_state(row),
                    "upload_eligible": upload_eligible and not unconfirmed,
                    "recovery": recovery,
                    # A Recovery in progress is resumed, never requested again.
                    "recover_eligible": (
                        not local_exists and recoverable
                        and not (recovery is not None and recovery["active"])
                    ),
                    "cleanup_eligible": (
                        local_copy_matches_current(row) and recoverable
                        and protection["state"] == "protected"
                    ),
                }
            )
        return items

    def _count_direct_child_files(
        self,
        vault_id: int,
        *,
        directory: str,
        state: str,
    ) -> int:
        clauses, params = self.direct_child_file_clauses(
            vault_id, directory=directory, state=state
        )
        row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM files f
            {CURRENT_JOIN_SQL}
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchone()
        return int(row["total"] or 0) if row else 0

    def _list_direct_child_files(
        self,
        vault_id: int,
        *,
        directory: str,
        state: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        clauses, params = self.direct_child_file_clauses(
            vault_id, directory=directory, state=state
        )
        sql = self.file_row_select_sql(clauses, vault_id)
        rows = self.connection.execute(
            f"""
            {sql}
            ORDER BY lower(f.path) ASC, f.path ASC
            LIMIT %s OFFSET %s
            """,
            [*params, int(limit), max(0, int(offset))],
        ).fetchall()
        return self.materialize_file_items(
            vault_id,
            rows,
            name_from_path=False,
            directory=directory,
        )

    def _search_files_page(
        self,
        vault_id: int,
        *,
        search: str,
        state: str,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses, params = self.direct_child_file_clauses(
            vault_id,
            directory="",
            state=state,
            search=search,
        )
        # The shared clauses already apply search, visibility and protection.
        total_row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM files f
            {CURRENT_JOIN_SQL}
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchone()
        total = int(total_row["total"] or 0) if total_row else 0
        offset = (page - 1) * page_size
        sql = self.file_row_select_sql(clauses, vault_id)
        rows = self.connection.execute(
            f"""
            {sql}
            ORDER BY lower(f.path) ASC, f.path ASC
            LIMIT %s OFFSET %s
            """,
            [*params, int(page_size), max(0, int(offset))],
        ).fetchall()
        items = self.materialize_file_items(
            vault_id,
            rows,
            name_from_path=True,
        )
        return items, total

    def summary(self, vault_id: int) -> dict[str, Any]:
        """Bounded SQL rollup of the same observations used by file browsing.

        Files in a blocked path count as unconfirmed, the others keep their
        state (ADR-0042, #684); ``last_known_states`` never overlays.
        """
        blocked, blocked_params = (self.unconfirmed.sql("f.path") if self.unconfirmed.by_path
                                   else ("FALSE", []))
        rows = self.connection.execute(
            f"""
            SELECT classified.state, classified.blocked, COUNT(*) AS total,
                   coalesce(SUM(classified.local_bytes), 0) AS local_bytes,
                   coalesce(SUM(classified.cloud_bytes), 0) AS cloud_bytes
            FROM (
                SELECT {self.protection_state_sql(vault_id)} AS state,
                    {blocked} AS blocked,
                    CASE WHEN f.local_presence IN ('present', 'unsupported')
                         THEN coalesce(f.local_size, 0) ELSE 0 END AS local_bytes,
                    CASE WHEN cur.availability <> 'missing'
                         THEN coalesce(cur.size, 0) ELSE 0 END AS cloud_bytes
                FROM files f
                {CURRENT_JOIN_SQL}
                WHERE f.vault_id=%s AND {VISIBLE_SQL}
            ) classified GROUP BY classified.state, classified.blocked
            """, [*blocked_params, vault_id],
        ).fetchall()
        states: dict[str, int] = {}
        last_known_counts: dict[str, int] = {}
        local_bytes = 0
        cloud_bytes = 0
        for row in rows:
            state = row["state"]
            if not state:
                continue
            total = int(row["total"] or 0)
            last_known_counts[str(state)] = last_known_counts.get(str(state), 0) + total
            shown = ("unconfirmed" if row["blocked"] and state not in UNCONFIRMED_EXEMPT
                     else str(state))
            states[shown] = states.get(shown, 0) + total
            local_bytes += int(row["local_bytes"] or 0)
            cloud_bytes += int(row["cloud_bytes"] or 0)
        active_jobs = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total FROM jobs
            WHERE vault_id=%s
              AND {OPEN_JOB_SQL}
            """,
            (vault_id,),
        ).fetchone()["total"]
        if self.protection_unconfirmed and states:
            masked = sum(count for name, count in states.items() if name not in UNCONFIRMED_EXEMPT)
            states = {name: count for name, count in states.items() if name in UNCONFIRMED_EXEMPT}
            if masked:
                states["unconfirmed"] = masked
        protection_state = aggregate_state(states)
        if self.unconfirmed.by_path and protection_state != "problem":
            # The Vault root contains every blocked path, like a folder above
            # one, even when no catalogued file lies in it.
            protection_state = "unconfirmed"
        return {
            "states": states,
            "protection_state": protection_state,
            "last_known_states": last_known_counts,
            "storage": {
                "local_bytes": local_bytes,
                "cloud_bytes": cloud_bytes,
            },
            "active_jobs": int(active_jobs),
        }
