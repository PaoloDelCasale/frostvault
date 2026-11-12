"""Vault-wide, colder-only copies with durable exact-version replacement intent.

Jobs serialize per-file work. This ledger never widens cleanup to newly discovered
versions: only the captured source can be removed, after destination verification.
Provider metadata carries an opaque intent token across the response/DB crash gap.
"""
from __future__ import annotations

import base64
import uuid
from collections import Counter
from typing import Any

from .cloud_deletion import lock_vault
from .cost_estimates import estimate_storage_month, list_prices
from .lifecycle_policies import load_vault_plan
from .lifecycle_profiles import ALLOWED_TARGETS
from .s3_copy import (
    _copy_storage_class_version, _delete_s3_version, _head_object_with_checksum,
    _object_version_entries, _verify_storage_class_destination, restore_header_state,
    _multipart_source_checksum, S3_SINGLE_COPY_MAX_BYTES,
)

MINIMUM_DAYS = {"STANDARD_IA": 30, "ONEZONE_IA": 30, "GLACIER_IR": 90,
                "GLACIER": 90, "DEEP_ARCHIVE": 180}
TOKEN_KEY = "frostvault-move"


class MoveBlocked(ValueError):
    """The move lacks current, exact, verified authority; preserve every copy."""


def require_ready(vault: dict[str, Any]) -> None:
    if (not vault["enabled"] or vault["decommission_state"] != "active"
            or vault["relocation_state"] != "ready" or vault.get("root_released_at")):
        raise MoveBlocked("Vault must be protected and reconnect must be complete")


def eligibility(row: dict[str, Any], target: str) -> str | None:
    current = (row.get("storage_class") or "STANDARD").upper()
    if row.get("busy"):
        return "pending_work"
    if row.get("cloud_check_failed"):
        return "cloud_check_unavailable"
    if (row.get("integrity") != "verified" or row.get("availability") != "available"
            or not row.get("plaintext_sha256") or row.get("size") is None
            or not row.get("provider_version_id") or row["provider_version_id"] == "null"):
        return "unverified_or_unavailable"
    if target not in ALLOWED_TARGETS.get(current, ()):
        return "already_equal_or_colder"
    if current in {"GLACIER", "DEEP_ARCHIVE"} and row.get("restore_state") != "available":
        return "explicit_restore_required"
    return None


def candidates(connection: Any, vault_id: int) -> list[dict[str, Any]]:
    # Never select an older verified version behind a newer unverified copy.
    return connection.execute("""
        SELECT vf.id AS vault_file_id, fp.path, av.*,
            EXISTS(SELECT 1 FROM jobs j WHERE j.vault_file_id=vf.id
                AND j.status NOT IN ('completed','failed','cancelled'))
            OR EXISTS(SELECT 1 FROM upload_intents i WHERE i.vault_file_id=vf.id
                AND i.send_started AND NOT i.settled)
            OR vf.replacing_version_id IS NOT NULL AS busy
        FROM vault_files vf JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        LEFT JOIN archive_versions av ON av.id=vf.current_version_id
        WHERE vf.vault_id=%s AND vf.status='active' ORDER BY fp.path
    """, (vault_id,)).fetchall()


def refresh_cold_sources(vault: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """Observe an explicit external/recovery restore, never request one.

    Restore availability is temporary and listing does not expose it. A cached
    flag alone must neither admit expired restores nor hide a manually restored
    exact source, including when a Local Copy is already present.
    """
    from .. import storage as worker

    client = None
    for row in rows:
        if row.get("storage_class") != "GLACIER" or eligibility({**row,"restore_state":"available"}, "DEEP_ARCHIVE"):
            continue
        try:
            if not str(row.get("object_key") or "").startswith(vault["s3_prefix"].rstrip("/") + "/"):
                raise MoveBlocked("Source key is outside this Vault")
            if client is None:
                client = worker.s3_client()
            head = client.head_object(Bucket=vault["s3_bucket"], Key=row["object_key"], VersionId=row["provider_version_id"])
            if (str(head.get("VersionId")) != row["provider_version_id"] or head.get("DeleteMarker")
                    or head.get("ContentLength") != row["size"]):
                raise MoveBlocked("Exact cold source identity changed")
            row["storage_class"] = head.get("StorageClass") or "STANDARD"
            row["restore_state"] = restore_header_state(head.get("Restore"))[0]
        except Exception:
            row["cloud_check_failed"] = True


def estimate(rows: list[dict[str, Any]], target: str) -> dict[str, Any]:
    eligible = [r for r in rows if eligibility(r, target) is None]
    skipped = Counter(reason for r in rows if (reason := eligibility(r, target)))
    prices = list_prices()
    size = sum(int(r["size"]) for r in eligible)
    retrieval = sum(int(r["size"]) / 1024**3 *
                    prices.restore_rates.get(r.get("storage_class"), {}).get("Instant", 0) *
                    (2 if int(r["size"]) > S3_SINGLE_COPY_MAX_BYTES else 1)
                    for r in eligible)
    return {"target_class": target, "eligible_count": len(eligible), "total_bytes": size,
            "skipped": dict(skipped), "minimum_days": MINIMUM_DAYS.get(target, 0),
            "estimated_monthly_eur": estimate_storage_month(prices, size_bytes=size,
                                                            storage_class=target).estimated_cost_eur,
            "estimated_retrieval_eur": round(retrieval, 6),
            "pricing_effective_at": prices.effective_at, "region": prices.region,
            "assumptions": "AWS list prices (USD used 1:1 as EUR), not a bill. Monthly storage and source retrieval only; copy/GET requests, additional destination readback retrieval, minimum billable object sizes, archive metadata, temporary restored storage and early-deletion charges excluded. Exact source eligibility is rechecked by the worker."}


def preview(connection: Any, vault_id: int) -> dict[str, Any]:
    vault = lock_vault(connection, vault_id)
    require_ready(vault)
    plan = load_vault_plan(connection, vault_id)
    rows = candidates(connection, vault_id)
    if any(step.storage_class == "DEEP_ARCHIVE" for step in plan.transitions):
        refresh_cold_sources(vault, rows)
    return {"vault_id": vault_id, "targets": [estimate(rows, step.storage_class)
                        for step in plan.transitions]}


def schedule(connection: Any, *, vault_id: int, target: str, user_id: int,
             acknowledged: bool, timestamp: str) -> dict[str, Any]:
    vault = lock_vault(connection, vault_id)
    require_ready(vault)
    if not acknowledged:
        raise MoveBlocked("Acknowledge minimum-duration and early-deletion charges")
    if target not in {step.storage_class for step in load_vault_plan(connection, vault_id).transitions}:
        raise MoveBlocked("Choose a colder class on this Vault's saved Storage Plan")
    rows = candidates(connection, vault_id)
    if target == "DEEP_ARCHIVE":
        refresh_cold_sources(vault, rows)
    result = estimate(rows, target)
    group_id = str(uuid.uuid4())
    ids = []
    for row in rows:
        if eligibility(row, target):
            continue
        if not str(row["object_key"]).startswith(vault["s3_prefix"].rstrip("/") + "/"):
            raise MoveBlocked("Source key is outside this Vault")
        job = connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,
            action,status,requested_at,updated_at,group_id,group_path,
            total_bytes,transferred_bytes,target_storage_class,origin)
            VALUES (%s,%s,%s,%s,'storage-class','queued',%s,%s,%s,'',%s,0,%s,'manual')
            ON CONFLICT DO NOTHING RETURNING id""",
            (vault_id,row["vault_file_id"],row["id"],row["path"],timestamp,timestamp,
             group_id,row["size"],target)).fetchone()
        if not job:
            continue
        ids.append(int(job["id"]))
        connection.execute("""INSERT INTO vault_class_moves(job_id,token,source_archive_id,
            source_version_id,object_key,size,sha256) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (job["id"],str(uuid.uuid4()),row["id"],row["provider_version_id"],row["object_key"],
             row["size"],row["plaintext_sha256"]))
    if not ids:
        raise MoveBlocked("No eligible files: already colder, unverified, unrestored or pending work")
    return {**result, "group_id": group_id, "job_ids": ids, "item_count": len(ids)}


def load(connection: Any, job_id: int) -> dict[str, Any] | None:
    return connection.execute("SELECT * FROM vault_class_moves WHERE job_id=%s", (job_id,)).fetchone()


def exact_head(client: Any, job: dict[str, Any], move: dict[str, Any], version: str) -> dict[str, Any]:
    head = _head_object_with_checksum(client, Bucket=job["s3_bucket"], Key=move["object_key"], VersionId=version)
    if (str(head.get("VersionId")) != version or head.get("DeleteMarker")
            or head.get("ContentLength") != move["size"]):
        raise MoveBlocked("Exact cloud version or size changed; every copy retained")
    return head


def discover_destination(client: Any, job: dict[str, Any], move: dict[str, Any]) -> str | None:
    """Resolve an ambiguous CopyObject response using durable intent provenance.

    Never infer ownership from class, timestamp, size, ETag or current-key HEAD.
    Multiple matches (for example a delayed provider retry) are held, not purged.
    """
    versions, markers = _object_version_entries(client, bucket=job["s3_bucket"], object_key=move["object_key"])
    matches = []
    for version_id in versions:
        if version_id == move["source_version_id"]:
            continue
        head = exact_head(client, job, move, version_id)
        if head.get("Metadata", {}).get(TOKEN_KEY) == move["token"]:
            matches.append(version_id)
    if len(matches) > 1 or markers:
        raise MoveBlocked("Ambiguous copy outcome; copies retained for reconciliation")
    return matches[0] if matches else None


def require_current(client: Any, job: dict[str, Any], move: dict[str, Any], version: str) -> None:
    current = client.head_object(Bucket=job["s3_bucket"], Key=move["object_key"])
    if str(current.get("VersionId")) != version or current.get("DeleteMarker"):
        raise MoveBlocked("Cloud current version changed; no copy or deletion allowed")


def process(job: dict[str, Any], move: dict[str, Any]) -> None:
    # Imported lazily: the worker owns leases, cancellation, provider and DB seams.
    from .. import storage as worker
    from ..catalog import ArchiveCatalog

    def active(message: str = "Vault move stopped") -> None:
        worker.ensure_job_active(int(job["id"]), message)

    def fence(connection: Any) -> None:
        vault = lock_vault(connection, job["vault_id"])
        require_ready(vault)
        if connection.execute("SELECT 1 FROM upload_intents WHERE vault_file_id=%s AND send_started AND NOT settled LIMIT 1", (job["vault_file_id"],)).fetchone():
            raise MoveBlocked("Reconcile the pending upload before another cloud mutation")
        worker.ensure_job_claim_owned_in_transaction(connection, job, "Vault move claim lost")
        if worker.job_cancelled(int(job["id"])):
            raise worker.OperationCancelled("Vault move stopped")
        path_owner = connection.execute("""SELECT vault_file_id FROM file_paths
            WHERE vault_id=%s AND path=%s AND valid_to IS NULL""", (job["vault_id"],job["path"])).fetchone()
        if not path_owner or path_owner["vault_file_id"] != job["vault_file_id"]:
            raise MoveBlocked("Vault File path changed; every copy retained")
        current = connection.execute("""SELECT av.id,av.provider_version_id,av.object_key,av.integrity,av.availability,av.plaintext_sha256
            FROM vault_files vf JOIN archive_versions av ON av.id=vf.current_version_id
            WHERE vf.id=%s AND (vf.replacing_version_id IS NULL OR vf.replacing_version_id=%s)""",
            (job["vault_file_id"], move.get("destination_archive_id"))).fetchone()
        if not current:
            raise MoveBlocked("Current Snapshot changed; every copy retained")
        if current["id"] == move["source_archive_id"] and (
            current["provider_version_id"] != move["source_version_id"] or current["object_key"] != move["object_key"]
            or current["integrity"] != "verified" or current["availability"] != "available"
            or current["plaintext_sha256"] != move["sha256"]
        ):
            raise MoveBlocked("Captured source identity or verification changed; every copy retained")
        if current["id"] == move.get("destination_archive_id") and (
            current["provider_version_id"] != move.get("destination_version_id") or current["object_key"] != move["object_key"]
            or current["integrity"] != "verified" or current["availability"] != "available"
            or current["plaintext_sha256"] != move["sha256"]
        ):
            raise MoveBlocked("Verified keeper changed; every copy retained")
        if current["id"] not in {move["source_archive_id"], move.get("destination_archive_id")}:
            # A cloud scan may have catalogued our response-window copy. This
            # exception authorizes no deletion or trust: publication still needs
            # exact token + digest verification of that very provider version.
            if (not move.get("destination_version_id") or current["provider_version_id"] != move["destination_version_id"]
                    or current["object_key"] != move["object_key"]):
                raise MoveBlocked("Current Snapshot changed; every copy retained")

    active()
    worker.validate_cloud_vault(job)
    client = worker.s3_client()
    target = job["target_storage_class"]
    checksum = base64.b64encode(bytes.fromhex(move["sha256"])).decode("ascii")
    # Discover before the catalog fence: a scan can already know the provider
    # version created in the crash gap, without making it trusted content.
    destination = move.get("destination_version_id")
    if not destination and move["copy_started"]:
        destination = discover_destination(client, job, move)
        move["destination_version_id"] = destination
    with worker.db() as connection:
        fence(connection)
        # A saved plan change never grants authority to warm/re-copy a source.
        if not move["copy_started"] and target not in {s.storage_class for s in load_vault_plan(connection, job["vault_id"]).transitions}:
            raise MoveBlocked("Target is no longer on the saved Storage Plan")
    worker.set_job(job["id"], "uploading", message_key="job.storage_class_changing",
                   message_params={"storage_class": target})
    if not destination:
        head = exact_head(client, job, move, move["source_version_id"])
        current_class = head.get("StorageClass") or "STANDARD"
        if target not in ALLOWED_TARGETS.get(current_class, ()):
            raise MoveBlocked("Source is already equal or colder; warming is forbidden")
        if current_class in {"GLACIER", "DEEP_ARCHIVE"} and restore_header_state(head.get("Restore"))[0] != "available":
            raise MoveBlocked("Explicit restore of this exact source VersionId is required")
        require_current(client, job, move, move["source_version_id"])
        with worker.db() as connection:
            fence(connection)
            connection.execute("UPDATE vault_class_moves SET copy_started=TRUE WHERE job_id=%s", (job["id"],))
        if move["size"] > S3_SINGLE_COPY_MAX_BYTES and not move.get("composite_checksum"):
            move["composite_checksum"] = _multipart_source_checksum(
                client, bucket=job["s3_bucket"], object_key=move["object_key"],
                version_id=move["source_version_id"], size=move["size"],
                expected_sha256=checksum, check_active=active)
            with worker.db() as connection:
                fence(connection)
                connection.execute("UPDATE vault_class_moves SET composite_checksum=%s WHERE job_id=%s",
                                   (move["composite_checksum"],job["id"]))
        # Preserve source metadata/headers/tags and add an opaque restart token.
        destination = _copy_storage_class_version(
            client, bucket=job["s3_bucket"], object_key=move["object_key"],
            source_version_id=move["source_version_id"], target_class=target,
            source_size=move["size"], source_head=head, check_active=active,
            intent_token=move["token"])
        if not destination:
            destination = discover_destination(client, job, move)
        if not destination:
            raise MoveBlocked("Copy response lacks an exact destination; source retained")
        move["destination_version_id"] = destination
        # Persist the provider response BEFORE cancellation/verification checkpoints.
        # A lost claim cannot publish it; token discovery handles that crash gap.
        with worker.db() as connection:
            fence(connection)
            connection.execute("UPDATE vault_class_moves SET destination_version_id=%s WHERE job_id=%s",
                               (destination, job["id"]))
    active()
    dest_head = exact_head(client, job, move, destination)
    if dest_head.get("Metadata", {}).get(TOKEN_KEY) != move["token"]:
        raise MoveBlocked("Destination has no matching move provenance")
    destination, etag = _verify_storage_class_destination(
        client, bucket=job["s3_bucket"], object_key=move["object_key"],
        source_version_id=move["source_version_id"], candidate_version_id=destination,
        target_class=target, expected_size=move["size"], expected_sha256_checksum=checksum,
        check_active=active, expected_composite_checksum=move.get("composite_checksum"))
    require_current(client, job, move, destination)
    with worker.db() as connection:
        fence(connection)
        catalog = ArchiveCatalog(connection)
        stamp = worker.now_iso()
        if not move.get("destination_archive_id"):
            new_id = catalog.record_archive_version(vault_id=job["vault_id"], path=job["path"],
                object_key=move["object_key"], provider_version_id=destination, size=move["size"],
                storage_class=target, etag=etag, uploaded_at=stamp, observed_at=stamp,
                scan_id=stamp, origin="upload")
            owner = connection.execute("SELECT vault_file_id FROM archive_versions WHERE id=%s", (new_id,)).fetchone()
            if not owner or owner["vault_file_id"] != job["vault_file_id"]:
                raise MoveBlocked("Destination belongs to another Vault File; source retained")
            catalog.mark_version_verified(new_id, plaintext_sha256=move["sha256"], verified_at=stamp)
            # Same bytes, same release clock; a class move is not a new local edit.
            connection.execute("UPDATE archive_versions SET verified_at=(SELECT verified_at FROM archive_versions WHERE id=%s) WHERE id=%s",
                               (move["source_archive_id"], new_id))
            connection.execute("""UPDATE local_copies SET
                matched_archive_version_id=CASE WHEN matched_archive_version_id=%s THEN %s ELSE matched_archive_version_id END,
                cloud_only_version_id=CASE WHEN cloud_only_version_id=%s THEN %s ELSE cloud_only_version_id END
                WHERE vault_file_id=%s""",
                (move["source_archive_id"],new_id,move["source_archive_id"],new_id,job["vault_file_id"]))
            # A representation-only replacement must not release a Cloud-only
            # Conflict when the old provider version becomes purged (#411).
            connection.execute("""UPDATE catalog_incidents SET archive_version_id=%s
                WHERE vault_file_id=%s AND archive_version_id=%s
                  AND kind='cloud_only_conflict' AND status='open'""",
                (new_id,job["vault_file_id"],move["source_archive_id"]))
            connection.execute("UPDATE jobs SET archive_version_id=%s WHERE id=%s", (new_id,job["id"]))
            connection.execute("UPDATE vault_class_moves SET destination_archive_id=%s,destination_version_id=%s WHERE job_id=%s",
                               (new_id,destination,job["id"]))
            move["destination_archive_id"] = new_id
    # Keep cancellation/lease + Vault locks through this single irreversible call.
    # Never use the generic purge-all helper: consent is only for our exact source.
    with worker.db() as connection:
        fence(connection)
        versions, markers = _object_version_entries(client, bucket=job["s3_bucket"], object_key=move["object_key"])
        if destination not in versions or markers or set(versions) - {move["source_version_id"], destination}:
            raise MoveBlocked("Unknown cloud content or missing keeper; source retained")
        require_current(client, job, move, destination)
        # Listing/HEAD can take long enough for a same-process cancellation or
        # heartbeat failure to win. Recheck without a second DB connection while
        # this transaction owns the Job row, immediately before DeleteObject.
        fence(connection)
        if move["source_version_id"] in versions:
            _delete_s3_version(client, bucket=job["s3_bucket"], object_key=move["object_key"], version_id=move["source_version_id"])
        ArchiveCatalog(connection).mark_versions_purged([move["source_archive_id"]], checked_at=worker.now_iso())
        connection.execute("UPDATE vault_class_moves SET source_deleted=TRUE WHERE job_id=%s", (job["id"],))
    worker.set_job_progress(job["id"], move["size"])
    worker.set_job(job["id"], "completed", message_key="job.storage_class_completed", message_params={"storage_class":target})
