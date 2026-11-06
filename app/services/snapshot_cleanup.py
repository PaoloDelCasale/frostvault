"""One authority gate for upload/rename replacement, never catalog membership.

Historical evidence is an exact GET duplicate of an independently verified keeper.
Adoption lives in a separate durable catalog of proofs, so it cannot become Current
Snapshot or alter local matches/clocks. External writers are not serialized: only
pinned exact IDs are ever deleted; observable inventory changes block completion.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .database_retry import owned_connection, retry_database_unit
from .upload_provenance import UploadBlocked, canonical, inventory, provider_identity, target, require_target
from .s3_copy import _delete_s3_version
from .vault_paths import expected_cloud_key


def exact_duplicate(client: Any, *, bucket: str, key: str, version: str,
                    digest: str, size: int, check_active: Any) -> None:
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version)
    body = response.get("Body")
    if body is None:
        raise UploadBlocked("proof_unavailable")
    count, hashed = 0, hashlib.sha256()
    try:
        if (str(response.get("VersionId") or "") != version or response.get("DeleteMarker")
                or response.get("ContentLength") != size):
            raise UploadBlocked("proof_unavailable")
        metadata = response.get("Metadata") or {}
        if metadata.get("frostvault-upload") or metadata.get("frostvault-move"):
            raise UploadBlocked("ambiguous_receipt")
        while True:
            check_active()
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise UploadBlocked("proof_unavailable")
            count += len(chunk)
            if count > size:
                raise UploadBlocked("orphan_different")
            hashed.update(chunk)
    finally:
        body.close()
    if count != size:
        raise UploadBlocked("proof_unavailable")
    if hashed.hexdigest() != digest:
        raise UploadBlocked("orphan_different")


def _keeper(connection: Any, job: dict[str, Any], keeper_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM archive_versions WHERE id=%s AND vault_file_id=%s AND vault_id=%s", (keeper_id, job["vault_file_id"], job["vault_id"])).fetchone()
    if (not row or row["integrity"] != "verified" or row["availability"] != "available"
            or not row["provider_version_id"] or row["provider_version_id"] == "null"
            or not row["object_key"] or row["size"] is None or not row["plaintext_sha256"]):
        raise UploadBlocked("keeper_missing")
    if row["object_key"] != expected_cloud_key(job["path"], job["s3_prefix"]):
        raise UploadBlocked("identity_changed")
    try:
        if len(bytes.fromhex(row["plaintext_sha256"])) != 32:
            raise ValueError("invalid digest")
    except (TypeError, ValueError):
        raise UploadBlocked("proof_unavailable")
    newer = connection.execute("SELECT * FROM archive_versions WHERE vault_file_id=%s AND version_number>%s AND availability='available'", (job["vault_file_id"], row["version_number"])).fetchall()
    if any(_has_lineage(connection, item, job["vault_file_id"]) for item in newer):
        # A stale recovery of A is never authority to purge a later app attempt B.
        raise UploadBlocked("identity_changed")
    latest = connection.execute("SELECT id FROM archive_versions WHERE vault_file_id=%s AND integrity='verified' AND availability='available' ORDER BY version_number DESC LIMIT 1", (job["vault_file_id"],)).fetchone()
    if not latest or str(latest["id"]) != str(keeper_id):
        raise UploadBlocked("identity_changed")
    return row


def _has_lineage(connection: Any, row: dict[str, Any] | None, file_id: str) -> bool:
    if not row or str(row["vault_file_id"]) != str(file_id):
        return False
    # Explicit upload/rename job linkage + independently captured expected digest.
    # Scan/origin string/membership/remote Metadata alone never meet this condition.
    if row["integrity"] == "verified" and row["plaintext_sha256"]:
        return bool(connection.execute("SELECT 1 FROM jobs WHERE archive_version_id=%s AND vault_file_id=%s AND action IN ('upload','rename') AND upload_plaintext_sha256=%s LIMIT 1", (row["id"], file_id, row["plaintext_sha256"])).fetchone())
    # Unverified app-owned superseded writes need exact durable receipt lineage,
    # not a discovery row linked later or an arbitrary origin='upload' string.
    return bool(connection.execute("SELECT 1 FROM upload_intents i JOIN upload_receipts r ON r.intent_id=i.id WHERE i.archive_version_id=%s AND i.vault_file_id=%s AND i.snapshot_json=%s AND r.response_json::jsonb->>'VersionId'=%s LIMIT 1", (row["id"], file_id, row.get("upload_snapshot_json"), row["provider_version_id"])).fetchone())


def _require_namespace_owner(connection: Any, job: dict[str, Any], keys: set[str]) -> None:
    for key in keys:
        prefix = job["s3_prefix"].rstrip("/") + "/"
        if not key.startswith(prefix):
            raise UploadBlocked("identity_changed")
        path = key[len(prefix):]
        foreign = connection.execute("SELECT 1 FROM file_paths fp JOIN vault_files vf ON vf.id=fp.vault_file_id WHERE fp.vault_id=%s AND fp.path=%s AND fp.valid_to IS NULL AND vf.id<>%s LIMIT 1", (job["vault_id"], path, job["vault_file_id"])).fetchone()
        if foreign:
            raise UploadBlocked("identity_changed")


def _require_version_owner(connection: Any, job: dict[str, Any], key: str, version: str) -> None:
    foreign = connection.execute("SELECT 1 FROM archive_versions WHERE vault_id=%s AND object_key=%s AND provider_version_id=%s AND vault_file_id<>%s LIMIT 1", (job["vault_id"], key, version, job["vault_file_id"])).fetchone()
    if foreign:
        raise UploadBlocked("identity_changed")


def pin_inventory(worker: Any, job: dict[str, Any], client: Any, *, keeper_id: str | None = None) -> dict[str, Any]:
    """Commit the exact replacement namespace BEFORE any new provider write."""
    with worker.db() as connection:
        bound = connection.execute("SELECT i.original_job_id FROM upload_intents i JOIN upload_intent_jobs b ON b.intent_id=i.id WHERE b.job_id=%s", (job["id"],)).fetchone()
        inventory_job = bound["original_job_id"] if bound else job["id"]
        old = connection.execute("SELECT * FROM snapshot_cleanup_inventories WHERE job_id=%s", (inventory_job,)).fetchone()
        if old:
            return old
        linked_id = keeper_id or job.get("archive_version_id")
        if linked_id:
            prior = connection.execute("SELECT b.* FROM snapshot_cleanup_inventories b JOIN jobs j ON j.id=b.job_id WHERE b.keeper_id=%s OR (b.keeper_id IS NULL AND j.archive_version_id=%s AND j.vault_file_id=%s)", (linked_id, linked_id, job["vault_file_id"])).fetchall()
            if prior:
                if len({(r["target_json"],r["inventory_json"]) for r in prior}) != 1:
                    raise UploadBlocked("inventory_changed")
                return prior[0]
        rows = connection.execute("SELECT object_key FROM archive_versions WHERE vault_file_id=%s AND availability<>'purged'", (job["vault_file_id"],)).fetchall()
        paths = connection.execute("SELECT path FROM file_paths WHERE vault_file_id=%s", (job["vault_file_id"],)).fetchall()
        pinned = target(connection, job, key=worker.expected_cloud_key(job["path"], job["s3_prefix"]), provider=provider_identity(client))
    keys = {pinned["key"]} | {r["object_key"] for r in rows if r["object_key"]}
    keys |= {worker.expected_cloud_key(r["path"], job["s3_prefix"]) for r in paths}
    with worker.db() as connection:
        _require_namespace_owner(connection, job, keys)
    baseline = {k: inventory(client, job["s3_bucket"], k) for k in sorted(keys)}
    if baseline != {k: inventory(client, job["s3_bucket"], k) for k in sorted(keys)}:
        raise UploadBlocked("inventory_changed")
    @retry_database_unit
    def save() -> dict[str, Any]:
        with owned_connection(worker.db()) as connection:
            require_target(connection, job, pinned)
            worker.ensure_job_claim_owned_in_transaction(connection, job, "Snapshot inventory claim lost")
            connection.execute("INSERT INTO snapshot_cleanup_inventories(job_id,target_json,inventory_json,created_at) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (inventory_job, canonical(pinned), canonical(baseline), worker.now_iso()))
            return connection.execute("SELECT * FROM snapshot_cleanup_inventories WHERE job_id=%s", (inventory_job,)).fetchone()
    return save()


def cleanup(worker: Any, job: dict[str, Any], keeper_id: str, client: Any) -> None:
    active = lambda: worker.ensure_job_active(job["id"], "Snapshot cleanup stopped")
    worker._require_upload_source_identity(job)
    provider = provider_identity(client)
    # Pin DB ownership first. Runtime source/root checks are outside replayable units.
    with worker.db() as connection:
        keeper = _keeper(connection, job, keeper_id)
        pinned = target(connection, job, key=keeper["object_key"], provider=provider)
        rows = connection.execute("SELECT * FROM archive_versions WHERE vault_file_id=%s AND id<>%s AND availability<>'purged'", (job["vault_file_id"], keeper_id)).fetchall()
        paths = connection.execute("SELECT path FROM file_paths WHERE vault_file_id=%s", (job["vault_file_id"],)).fetchall()
    keys = {keeper["object_key"]} | {r["object_key"] for r in rows if r["object_key"]}
    keys |= {worker.expected_cloud_key(r["path"], job["s3_prefix"]) for r in paths}
    batch = pin_inventory(worker, job, client, keeper_id=keeper_id)
    @retry_database_unit
    def bind_keeper() -> None:
        with owned_connection(worker.db()) as connection:
            require_target(connection, job, pinned)
            worker.ensure_job_claim_owned_in_transaction(connection, job, "Snapshot inventory claim lost")
            result = connection.execute("UPDATE snapshot_cleanup_inventories SET keeper_id=%s WHERE job_id=%s AND (keeper_id IS NULL OR keeper_id=%s) RETURNING job_id", (keeper_id, batch["job_id"], keeper_id)).fetchone()
            if not result:
                raise UploadBlocked("identity_changed")
    bind_keeper()
    baseline = json.loads(batch["inventory_json"])
    require_pinned = json.loads(batch["target_json"])
    with worker.db() as connection:
        require_target(connection, job, require_pinned)
    if provider != require_pinned["provider"] or set(baseline) != keys:
        raise UploadBlocked("identity_changed")
    expected = {key: inventory(client, job["s3_bucket"], key) for key in sorted(keys)}
    keep_key, keep_version = keeper["object_key"], keeper["provider_version_id"]
    if keep_version not in expected[keep_key]["versions"]:
        raise UploadBlocked("keeper_missing")
    for key, value in expected.items():
        admitted = set(baseline[key]["versions"])
        if key == keep_key:
            admitted.add(keep_version)
        if set(value["versions"]) - admitted or value["markers"] != baseline[key]["markers"]:
            raise UploadBlocked("inventory_changed")
        missing = set(baseline[key]["versions"]) - set(value["versions"])
        with worker.db() as connection:
            for version in missing:
                prior = connection.execute("SELECT 1 FROM snapshot_cleanup_proofs WHERE vault_file_id=%s AND bucket=%s AND provider=%s AND object_key=%s AND version_id=%s AND keeper_id=%s AND sha256=%s", (job["vault_file_id"], job["s3_bucket"], provider, key, version, keeper_id, keeper["plaintext_sha256"])).fetchone()
                if not prior:
                    raise UploadBlocked("inventory_changed")
    if any(v["markers"] for v in expected.values()):
        # Replacement has no marker consent. Delayed deletion has its own exact set.
        raise UploadBlocked("markers_present")
    if expected != {key: inventory(client, job["s3_bucket"], key) for key in sorted(keys)}:
        raise UploadBlocked("inventory_changed")
    known = {(r["object_key"], r["provider_version_id"]): r for r in rows}
    candidates = {(key, version) for key, value in expected.items() for version in value["versions"] if (key, version) != (keep_key, keep_version)}
    for key, version in sorted(candidates):
        active()
        with worker.db() as connection:
            _require_namespace_owner(connection, job, keys)
            _require_version_owner(connection, job, key, version)
            proof = connection.execute("SELECT * FROM snapshot_cleanup_proofs WHERE vault_file_id=%s AND bucket=%s AND provider=%s AND object_key=%s AND version_id=%s", (job["vault_file_id"], job["s3_bucket"], provider, key, version)).fetchone()
            lineage = _has_lineage(connection, known.get((key, version)), job["vault_file_id"])
        if proof and (str(proof["keeper_id"]), proof["sha256"], proof["size"]) == (str(keeper_id), keeper["plaintext_sha256"], keeper["size"]):
            continue
        method = "independent_lineage" if lineage else "exact_get_duplicate"
        if not lineage:
            try:
                exact_duplicate(client, bucket=job["s3_bucket"], key=key, version=version,
                                digest=keeper["plaintext_sha256"], size=keeper["size"], check_active=active)
            except UploadBlocked:
                raise
            except (worker.OperationCancelled, worker.JobLeaseLost):
                raise
            except Exception as exc:
                raise UploadBlocked("proof_unavailable") from exc
        @retry_database_unit
        def persist_proof() -> None:
            with owned_connection(worker.db()) as connection:
                require_target(connection, job, pinned)
                _require_namespace_owner(connection, job, keys)
                _require_version_owner(connection, job, key, version)
                worker.ensure_job_claim_owned_in_transaction(connection, job, "Snapshot cleanup claim lost")
                if _keeper(connection, job, keeper_id) != keeper:
                    raise UploadBlocked("identity_changed")
                connection.execute("INSERT INTO snapshot_cleanup_proofs(vault_file_id,bucket,provider,object_key,version_id,keeper_id,sha256,size,method,proved_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (vault_file_id,bucket,provider,object_key,version_id) DO UPDATE SET keeper_id=excluded.keeper_id,sha256=excluded.sha256,size=excluded.size,method=excluded.method,proved_at=excluded.proved_at WHERE snapshot_cleanup_proofs.deleted_at IS NULL", (job["vault_file_id"], job["s3_bucket"], provider, key, version, keeper_id, keeper["plaintext_sha256"], keeper["size"], method, worker.now_iso()))
        persist_proof()
    for key, version in sorted(candidates):
        active()
        observed = {k: inventory(client, job["s3_bucket"], k) for k in sorted(keys)}
        if observed != expected:
            raise UploadBlocked("inventory_changed")
        # Destructive call has no DB retry. Hold application fences for this single
        # exact ID; external S3/SMB writers remain outside the locking boundary.
        with worker.db() as connection:
            require_target(connection, job, pinned)
            _require_namespace_owner(connection, job, keys)
            _require_version_owner(connection, job, key, version)
            worker.ensure_job_claim_owned_in_transaction(connection, job, "Snapshot cleanup claim lost")
            current = _keeper(connection, job, keeper_id)
            if current != keeper:
                raise UploadBlocked("identity_changed")
            vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (job["vault_id"],)).fetchone()
            access = worker.source_layout.vault_local_access(vault["source_root"])
            if not access.local_operations_allowed:
                raise UploadBlocked("identity_changed")
            worker._validate_enrolled_scan_root(vault)
            if worker.job_cancelled(job["id"]):
                raise worker.OperationCancelled("Snapshot cleanup stopped")
            proof = connection.execute("SELECT 1 FROM snapshot_cleanup_proofs WHERE vault_file_id=%s AND bucket=%s AND provider=%s AND object_key=%s AND version_id=%s AND keeper_id=%s AND sha256=%s", (job["vault_file_id"], job["s3_bucket"], provider, key, version, keeper_id, keeper["plaintext_sha256"])).fetchone()
            if not proof or (key, version) == (keep_key, keep_version):
                raise UploadBlocked("ambiguous_receipt")
            _delete_s3_version(client, bucket=job["s3_bucket"], object_key=key, version_id=version)
        expected[key]["versions"].remove(version)
    active()
    final = {key: inventory(client, job["s3_bucket"], key) for key in sorted(keys)}
    if final != expected or any(v["versions"] for k, v in final.items() if k != keep_key) or final[keep_key]["versions"] != [keep_version]:
        raise UploadBlocked("inventory_changed")
    worker._require_upload_source_identity(job)
    @retry_database_unit
    def persist_purge() -> None:
        with owned_connection(worker.db()) as connection:
            require_target(connection, job, pinned)
            _require_namespace_owner(connection, job, keys)
            worker.ensure_job_claim_owned_in_transaction(connection, job, "Snapshot cleanup claim lost")
            if _keeper(connection, job, keeper_id) != keeper:
                raise UploadBlocked("identity_changed")
            # Absent previously authorized IDs reconcile delete->DB gaps without
            # another GET/delete. Missing unrelated rows are not falsely purged.
            proved = connection.execute("SELECT object_key,version_id FROM snapshot_cleanup_proofs WHERE vault_file_id=%s AND bucket=%s AND provider=%s AND keeper_id=%s AND sha256=%s", (job["vault_file_id"], job["s3_bucket"], provider, keeper_id, keeper["plaintext_sha256"])).fetchall()
            absent = {(r["object_key"], r["version_id"]) for r in proved if r["object_key"] in final and r["version_id"] not in final[r["object_key"]]["versions"]}
            worker.ArchiveCatalog(connection).mark_versions_purged([r["id"] for r in rows if (r["object_key"], r["provider_version_id"]) in absent], checked_at=worker.now_iso())
            for k, v in absent:
                connection.execute("UPDATE snapshot_cleanup_proofs SET deleted_at=COALESCE(deleted_at,%s) WHERE vault_file_id=%s AND bucket=%s AND provider=%s AND object_key=%s AND version_id=%s", (worker.now_iso(), job["vault_file_id"], job["s3_bucket"], provider, k, v))
            connection.execute("UPDATE upload_intents SET settled=TRUE,blocked_reason=NULL WHERE archive_version_id=%s AND vault_file_id=%s", (keeper_id, job["vault_file_id"]))
            connection.execute("DELETE FROM snapshot_reconciliation_blocks WHERE vault_file_id=%s", (job["vault_file_id"],))
    persist_purge()
