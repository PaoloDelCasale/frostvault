"""Job admission: its per-Vault serialization, its rules, the Job status vocabulary.

``lock_vault`` takes a PostgreSQL row lock before reading Job state, so
concurrent admissions never decide from
one stale snapshot. It also rejects normal work for a Vault whose single
``lifecycle`` is not ``active`` (reconnect required, stopping, tombstone).

``queue_jobs`` admits one request of the Owner (upload, Recovery, local
release): it validates it, chooses the Archive Version, prices it, holds a
high-impact restore for Restore Approval (ADR-0032) and queues its Jobs.
``estimate_recovery`` is the Cost Estimate of the same request, from the same
code. A refusal is an ``AdmissionError``; the HTTP routes choose its status.
"""
from __future__ import annotations

import posixpath
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..database import db
from ..i18n import DEFAULT_LOCALE, translate
from . import catalog_incidents as catalog_incident_service
from . import notifications as notification_service
from . import protection_stop as protection_stop_service
from . import recovery_approval
from . import vault_relocation as vault_relocation_service
from . import write_access
from .job_queue import open_job_sql
from .cost_estimates import CostEstimate, PriceList, estimate_restore_cost, list_prices
from .operation_policies import blocking_path_for, local_release_blocking_paths
from .restore_estimates import is_high_impact_restore, normalize_restore_tier
from .s3 import storage_class_requires_restore
from .source_layout import vault_local_access
from .vault_paths import safe_relative_path


def lock_vault(
    connection: Any, vault_id: int
) -> None:
    """Serialize admission and reject normal work for a non-active Vault.

    ``FOR NO KEY UPDATE`` serializes admissions on one Vault without
    rewriting its row and still lets claims hold their shared ``KEY SHARE``
    fence (ADR-0031).
    """
    row = connection.execute(
        "SELECT id, lifecycle FROM vaults WHERE id=%s FOR NO KEY UPDATE",
        (vault_id,),
    ).fetchone()
    if not row:
        raise LookupError("vault_not_found")
    if row["lifecycle"] != "active":
        raise LookupError("vault_quiesced")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def lock_vault_decisions(connection: Any, vault_id: int) -> None:
    """Serialize the decisions on a Vault's queued work: approve, cancel.

    The exclusive row lock also waits for a claim's ``KEY SHARE`` fence, so a
    decision and a claim never act on the same Jobs at once.
    """
    connection.execute("SELECT id FROM vaults WHERE id=%s FOR UPDATE", (vault_id,))


# -- Admission of a request: validate, choose the version, price, hold, queue ----

class AdmissionError(Exception):
    """A request Job admission refuses; ``str()`` is the reason, in English."""


class InvalidAdmission(AdmissionError):
    """The request contradicts itself or names an unknown operation."""


class AdmissionTargetNotFound(AdmissionError):
    """The Vault or the recoverable Archive Version asked for does not exist."""


class AdmissionRefused(AdmissionError):
    """The request is well formed, but the Vault's state does not allow it now."""


class LocalStorageUnavailable(AdmissionError):
    """The Source Volume of the Vault cannot take local work."""


class LocalizedAdmissionRefused(AdmissionRefused):
    """A refusal whose reason is a catalog message with parameters."""

    def __init__(self, message_key: str, **params: Any) -> None:
        super().__init__(translate(message_key, locale=DEFAULT_LOCALE, **params))
        self.message_key = message_key
        self.message_params = params


# A Vault directory may mix GLACIER and DEEP_ARCHIVE versions, so one request
# tier must be valid for every class that needs a restore (no Expedited).
DIRECTORY_RESTORE_TIERS = ("Standard", "Bulk")


def resolve_directory_restore(
    tier: str | None, days: int | None
) -> tuple[str, int]:
    """Resolve the tier and days one directory recovery applies to every Job.

    The directory Cost Estimate and the queued Jobs share this so the dialog,
    the estimate and the actual restore agree (#532).
    """
    runtime = settings
    requested = (tier or "").strip()
    if requested:
        if requested not in DIRECTORY_RESTORE_TIERS:
            raise InvalidAdmission("Choose Standard or Bulk")
        resolved_tier = requested
    else:
        resolved_tier = normalize_restore_tier(
            runtime.restore_tier, storage_class="DEEP_ARCHIVE"
        )
    resolved_days = int(days if days is not None else runtime.restore_days)
    return resolved_tier, resolved_days


# A version whose cloud restore is already requested or done costs nothing more
# to recover, so it never counts toward a Restore Approval (#534).
_RESTORE_ALREADY_REQUESTED = frozenset({"restoring", "available"})


def _is_high_impact(size_bytes: int, estimated_cost_eur: float) -> bool:
    return is_high_impact_restore(
        size_bytes=size_bytes,
        estimated_cost_eur=estimated_cost_eur,
        size_threshold_gib=settings.restore_high_impact_gib,
        cost_threshold_eur=settings.restore_high_impact_eur,
    )


def recovery_request_impact(
    rows: Iterable[Mapping[str, Any]],
    *,
    tier: str | None = None,
    days: int | None = None,
    prices: PriceList | None = None,
) -> dict[str, Any]:
    """Price one whole recovery request and decide if it needs Restore Approval.

    ``rows`` carry ``bytes``, ``storage_class`` and ``restore_state`` (one per
    file or aggregated per class), optionally with their own ``restore_tier``
    and ``restore_days``. The thresholds apply to the request's total, so a
    folder of many small archived files is held like one large file (#533).
    The directory Cost Estimate and queueing share this so the dialog and the
    queued Jobs agree on ``high_impact``.

    Every row is priced once. ``archived`` and ``archived_hours`` describe all
    the rows of an archived class; the amounts, ``requires_restore`` and
    ``high_impact`` only those whose restore is not requested yet.
    """
    runtime = settings
    if prices is None:
        prices = list_prices(runtime.aws_region)
    size_bytes = 0
    cost = 0.0
    hours = 0.0
    priced = False
    archived = False
    archived_hours: float = 0
    for row in rows:
        storage_class = row.get("storage_class")
        if not storage_class_requires_restore(storage_class):
            continue
        estimate = estimate_restore_cost(
            prices,
            size_bytes=int(row.get("bytes") or 0),
            storage_class=str(storage_class),
            tier=str(row.get("restore_tier") or tier or runtime.restore_tier or "Bulk"),
            days=int(row.get("restore_days") or days or runtime.restore_days or 3),
        )
        archived = True
        archived_hours = max(archived_hours, estimate.estimated_hours or 0)
        if (row.get("restore_state") or "") in _RESTORE_ALREADY_REQUESTED:
            continue
        priced = True
        size_bytes += estimate.size_bytes
        cost += estimate.estimated_cost_eur
        hours = max(hours, float(estimate.estimated_hours or 0))
    return {
        "requires_restore": priced,
        "size_bytes": size_bytes,
        "estimated_cost_eur": cost,
        "estimated_hours": hours,
        "high_impact": priced and _is_high_impact(size_bytes, cost),
        "archived": archived,
        "archived_hours": archived_hours,
    }


def _price_current(
    version: Mapping[str, Any], *, restore_tier: str | None, restore_days: int | None,
    prices: PriceList | None,
) -> tuple[str, int, CostEstimate | None]:
    """Tier, days and Cost Estimate of recovering one file's Current.

    The Cost Estimate dialog and the queued Job share this. There is no
    estimate, and no price list is read, without ``prices``.
    """
    storage_class = str(version.get("storage_class") or "")
    days = int(restore_days if restore_days is not None else settings.restore_days)
    tier = normalize_restore_tier(
        restore_tier or settings.restore_tier, storage_class=storage_class,
    )
    if prices is None:
        return tier, days, None
    return tier, days, estimate_restore_cost(
        prices,
        size_bytes=int(version.get("size") or 0),
        storage_class=storage_class,
        tier=tier,
        days=days,
    )


def estimate_recovery(
    vault_id: int,
    *,
    path: str = "",
    is_directory: bool = False,
    selection: list[tuple[str, bool]] | None = None,
    archive_version_id: str | None = None,
    restore_tier: str | None = None,
    restore_days: int | None = None,
) -> dict[str, Any]:
    """The Cost Estimate of one recovery request, before it is queued."""
    from ..catalog import ArchiveCatalog

    selection = [
        (safe_relative_path(item).as_posix(), directory) for item, directory in selection
    ] if selection else None
    logical_path = "" if selection else safe_relative_path(path).as_posix()
    if is_directory or selection:
        # Current only: an explicit reference, never the latest available
        # version; its restore progress keeps a pending restore unpriced.
        scope_sql, scope_params = ArchiveCatalog.recovery_selection_scope(
            selection or [(logical_path, True)], column="f.path",
        )
        with db() as connection:
            classes = connection.execute(
                f"""SELECT cur.storage_class, cur.restore_state, sum(cur.size) AS bytes
                FROM files f JOIN archive_versions cur ON cur.id=f.current_version_id
                WHERE f.vault_id=%s AND {scope_sql}
                  AND cur.integrity='verified' AND cur.availability='available'
                  AND NOT EXISTS (SELECT 1 FROM jobs pending WHERE pending.file_id=f.id
                      AND {open_job_sql("pending")})
                  AND coalesce(f.local_presence, 'missing')<>'present'
                GROUP BY cur.storage_class, cur.restore_state
                """,
                [vault_id, *scope_params],
            ).fetchall()
        tier, days = resolve_directory_restore(restore_tier, restore_days)
        # Only new cloud restores are charged; already-requested versions must
        # not inflate the selection's price or approval threshold (#534).
        impact = recovery_request_impact(classes, tier=tier, days=days)
        archived = impact["archived"]
        return {
            "path": logical_path,
            "archive_version_id": None,
            "requires_restore": archived,
            "restore_object_irreversible": archived,
            "high_impact": impact["high_impact"],
            "estimate": {
                "tier": tier,
                "days": days,
                "estimated_cost_eur": impact["estimated_cost_eur"],
                "estimated_hours": impact["archived_hours"],
            } if archived else None,
        }
    with db() as connection:
        versions = ArchiveCatalog(connection).list_versions(vault_id, logical_path)
    selected = next((row for row in versions if row["recoverable"]), None)
    if selected is None:
        raise AdmissionTargetNotFound("No recoverable Archive Version found")
    if archive_version_id and archive_version_id != selected["id"]:
        raise AdmissionRefused("The current snapshot changed; retry recovery")
    requires_restore = storage_class_requires_restore(selected.get("storage_class"))
    tier, days, priced = _price_current(
        selected, restore_tier=restore_tier, restore_days=restore_days,
        prices=list_prices(settings.aws_region) if requires_restore else None,
    )
    estimate_payload = None
    high_impact = False
    if priced is not None:
        high_impact = (
            (selected.get("restore_state") or "") not in _RESTORE_ALREADY_REQUESTED
            and _is_high_impact(priced.size_bytes, priced.estimated_cost_eur)
        )
        estimate_payload = {
            "tier": priced.tier,
            "days": days,
            "estimated_cost_eur": priced.estimated_cost_eur,
            "estimated_hours": priced.estimated_hours,
            "pricing_note": str(
                priced.assumptions.get(
                    "disclaimer",
                    "Estimate from AWS list prices; not an AWS Billing quote.",
                )
            ),
            "region": priced.region,
            "pricing_effective_at": priced.pricing_effective_at,
            "assumptions": priced.assumptions,
            "restore_object_irreversible": True,
        }
    return {
        "path": logical_path,
        "archive_version_id": selected["id"],
        "storage_class": selected.get("storage_class"),
        "requires_restore": requires_restore,
        "restore_object_irreversible": requires_restore,
        "high_impact": high_impact,
        "estimate": estimate_payload,
    }


HIGH_IMPACT_RESTORE_HELD_MESSAGE = (
    "High-impact Glacier restore held for primary-owner approval; "
    "RestoreObject cannot be cancelled after AWS accepts it"
)


def _hold_high_impact_recovery(
    connection: Any, job_ids: list[int], group_id: str,
    prices: PriceList | None = None,
) -> dict[str, Any]:
    """Evaluate Restore Approval once, at queue time, for a whole group.

    Runs in the queueing transaction, so no worker can claim a Job before the
    group is either released as ``queued`` or held as ``pending_approval``.
    The worker never re-evaluates the thresholds (#534): one approval per
    ``group_id`` releases every Job.
    """
    rows = connection.execute(
        """
        SELECT av.storage_class, av.restore_state, r.tier AS restore_tier,
               r.days AS restore_days, sum(j.total_bytes) AS bytes
        FROM jobs j JOIN archive_versions av ON av.id=j.archive_version_id
        LEFT JOIN job_restores r ON r.job_id=j.id
        WHERE j.id=ANY(%s)
        GROUP BY av.storage_class, av.restore_state, r.tier, r.days
        """,
        (job_ids,),
    ).fetchall()
    impact = recovery_request_impact(rows, prices=prices)
    if not impact["requires_restore"]:
        return impact
    timestamp = _now_iso()
    # The request carries one Cost Estimate, which the group shows.
    recovery_approval.record_estimate(
        connection, group_id=group_id,
        estimated_cost_eur=impact["estimated_cost_eur"],
        estimated_hours=impact["estimated_hours"],
    )
    if impact["high_impact"]:
        hold_seconds = int(settings.restore_approval_hold_seconds)
        deadline = (
            datetime.now(timezone.utc) + timedelta(seconds=hold_seconds)
        ).isoformat()
        recovery_approval.hold(
            connection, group_id=group_id, job_ids=job_ids, deadline=deadline,
            message=HIGH_IMPACT_RESTORE_HELD_MESSAGE, now=timestamp,
        )
        notification_service.enqueue_recovery_approval_best_effort(connection, job_ids=job_ids)
    return impact


def require_local_release(connection: Any, vault_id: int, path: str) -> None:
    """Refuse a local release the gate does not confirm, naming the path (#681)."""
    blocking = local_release_blocking_paths(connection, vault_id)
    if blocking is None:
        raise LocalizedAdmissionRefused("api.local_release_unconfirmed")
    blocked_by = blocking_path_for(path, blocking)
    if blocked_by is not None:
        raise LocalizedAdmissionRefused("api.local_release_blocked_path", path=blocked_by)


def require_writable_destinations(connection: Any, source_root: str, job_ids: list[int]) -> None:
    """Refuse local work in a folder FrostVault may not write (#681).

    Checked when the request is queued, so a Recovery never asks S3 for a
    restore that its final step could not publish; the worker checks again.
    """
    rows = connection.execute(
        "SELECT DISTINCT f.path FROM jobs j JOIN files f ON f.id=j.file_id WHERE j.id=ANY(%s)",
        (job_ids,),
    ).fetchall()
    root = Path(source_root)
    folders = {
        write_access.nearest_existing_directory(root / parent / "entry", root)
        for parent in {posixpath.dirname(str(row["path"])) for row in rows}
    }
    for folder in sorted(folders):
        try:
            write_access.require_writable_directory(folder)
        except write_access.DestinationNotWritable as exc:
            raise LocalizedAdmissionRefused(exc.message_key, **exc.message_params) from exc


def queue_jobs(
    path: str,
    action: str,
    vault_id: int,
    is_directory: bool = False,
    *,
    archive_version_id: str | None = None,
    restore_tier: str | None = None,
    restore_days: int | None = None,
    recovery_selection: list[tuple[str, bool]] | None = None,
) -> dict[str, Any]:
    """Admit one upload, Recovery or local release request as a group of Jobs."""
    from ..catalog import ArchiveCatalog

    if recovery_selection:
        recovery_selection = [(safe_relative_path(p).as_posix(), directory) for p, directory in recovery_selection]
        if action != "recover" or archive_version_id:
            raise InvalidAdmission("Invalid recovery selection")
        logical_path = ""
    else:
        logical_path = safe_relative_path(path).as_posix()
    if action not in {"upload", "recover", "free-space"}:
        raise InvalidAdmission("Invalid operation")
    with db() as connection:
        vault_row = connection.execute(
            "SELECT source_root, lifecycle FROM vaults WHERE id=%s",
            (vault_id,),
        ).fetchone()
    if vault_row is None:
        raise AdmissionTargetNotFound("Vault not found")
    if protection_stop_service.local_work_suspended(vault_row):
        raise AdmissionRefused("Vault is no longer protected")
    if vault_relocation_service.local_work_suspended(vault_row):
        raise AdmissionRefused("Local work is suspended pending reconnect scan")
    access = vault_local_access(vault_row["source_root"])
    if not access.local_operations_allowed:
        raise LocalStorageUnavailable(
            "Local storage for this vault is unavailable"
            + (f" ({access.volume_health})" if access.volume_health else ""),
        )
    if archive_version_id and (action != "recover" or is_directory):
        raise InvalidAdmission(
            "archive_version_id is only valid for single-file recover",
        )
    group_id = uuid.uuid4().hex
    estimated_cost_eur = None
    estimated_hours = None
    resolved_tier = None
    resolved_days = None
    # One price list for the whole request: the pinned file and its approval.
    prices = None
    directory_recover = action == "recover" and (is_directory or bool(recovery_selection))
    if directory_recover:
        resolved_tier, resolved_days = resolve_directory_restore(
            restore_tier, restore_days
        )
    with db() as connection:
        if action == "free-space":
            require_local_release(connection, vault_id, logical_path)
        catalog = ArchiveCatalog(connection)
        if action == "recover" and not is_directory and not recovery_selection:
            versions = catalog.list_versions(vault_id, logical_path)
            selectable = [row for row in versions if row["recoverable"]]
            if archive_version_id:
                selected = next(
                    (
                        row
                        for row in versions
                        if row["id"] == archive_version_id
                    ),
                    None,
                )
                if selected is None or not selectable or selected["id"] != selectable[0]["id"]:
                    raise AdmissionRefused(
                        "The selected Archive Version is not recoverable"
                    )
            elif selectable:
                selected = selectable[0]
                archive_version_id = selected["id"]
            else:
                selected = None
            if selected is not None and storage_class_requires_restore(
                selected.get("storage_class")
            ):
                prices = list_prices(settings.aws_region)
                resolved_tier, resolved_days, estimate = _price_current(
                    selected, restore_tier=restore_tier, restore_days=restore_days,
                    prices=prices,
                )
                # What the Job records before its request is priced as a whole
                # has always been rounded; it stays when the restore is
                # already requested and the request adds no charge.
                estimated_cost_eur = round(estimate.estimated_cost_eur, 6)
                estimated_hours = estimate.estimated_hours
        job_ids, total_bytes, eligible_count = catalog.queue_jobs(
            vault_id=vault_id,
            path=logical_path,
            action=action,
            requested_at=_now_iso(),
            group_id=group_id,
            is_directory=is_directory,
            archive_version_id=archive_version_id,
            restore_tier=resolved_tier,
            restore_days=resolved_days,
            estimated_cost_eur=estimated_cost_eur,
            estimated_hours=estimated_hours,
            recovery_selection=recovery_selection,
        )
        if action in {"recover", "free-space"} and job_ids:
            require_writable_destinations(connection, vault_row["source_root"], job_ids)
        if directory_recover and not (
            job_ids
            and connection.execute(
                "SELECT 1 FROM job_restores WHERE job_id=ANY(%s) LIMIT 1",
                (job_ids,),
            ).fetchone()
        ):
            # Nothing queued needs a cloud restore; echo what was persisted.
            resolved_tier = resolved_days = None
        approval_required = False
        if action == "recover" and job_ids:
            impact = _hold_high_impact_recovery(connection, job_ids, group_id, prices)
            approval_required = bool(impact["high_impact"])
            if impact["requires_restore"]:
                estimated_cost_eur = impact["estimated_cost_eur"]
                estimated_hours = impact["estimated_hours"]
        if not eligible_count:
            raise AdmissionRefused("No files are eligible for this operation")
    if not job_ids:
        raise AdmissionRefused("An operation is already running on the selected files")
    result = {
        "group_id": group_id,
        "job_ids": job_ids,
        "item_count": len(job_ids),
        "total_bytes": total_bytes,
        "archive_version_id": archive_version_id,
        "restore_tier": resolved_tier,
        "restore_days": resolved_days,
        "estimated_cost_eur": estimated_cost_eur,
        "estimated_hours": estimated_hours,
    }
    if action == "recover":
        result["approval_required"] = approval_required
    return result


def confirm_cloud_only_replacement(
    path: str, vault_id: int, actor: str
) -> dict[str, Any]:
    """Release held-back content and queue the upload that replaces the copy.

    The previous Archive Version was the only cloud copy; Current Snapshot
    deletes it only after the new upload is verified (#411, #363 D7).
    """
    logical_path = safe_relative_path(path).as_posix()
    with db() as connection:
        try:
            catalog_incident_service.confirm_cloud_only_replacement(
                connection,
                vault_id=vault_id,
                path=logical_path,
                actor=actor,
            )
        except catalog_incident_service.NoCloudOnlyConflict as exc:
            raise AdmissionRefused(
                "No conflict with a cloud-only file at this path"
            ) from exc
    return queue_jobs(logical_path, "upload", vault_id)
