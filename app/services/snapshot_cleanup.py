"""Exact cleanup of what a verified keeper superseded (#591, ADR-0016/0024/0035).

Interface: ``cleanup(worker, job, keeper_id, client)`` after the keeper became
the file's verified Current and before the Job completes. It deletes from S3,
by exact VersionId, the superseded application writes of this Vault File and
then removes their Archive Version rows together with the completed proof.

Authority is a frozen batch (``cleanup_batches`` / ``cleanup_candidates``):

- candidates are the file's Archive Versions that are neither Current nor the
  replacement in progress and that a landed upload attempt of this file wrote
  (exact VersionId and token). Nothing is adopted by reading equal bytes, no
  batch is selected by time and nothing is rebuilt from Job history;
- the batch is committed once, with the keeper and the pinned target, before
  any DELETE; a resumed cleanup (another Job, after the original one was
  collected) reuses it unchanged and never sends again;
- every listing of the batch's keys must show only the keeper and frozen
  candidates and no Delete Marker: anything unknown stops the cleanup and
  nothing is deleted;
- each DELETE runs inside a transaction that holds the Job row, its lease
  (renewed), the Vault and the file's Current, after a fresh Source Volume/root
  check: cancellation, takeover, expiry, a root change or an invalid Vault stop
  the effect even after the keeper is committed. The DELETE is never inside a
  replayed DB unit; recording it is a separate DB-only unit;
- the final postcondition (the keeper is the only version left at its key, the
  other frozen keys are empty) is checked before the rows are removed and the
  Job may complete.

Without superseded versions and without ``keeper_id`` nothing is done. With a
keeper the postcondition is still checked: an unknown object at the keeper's
key blocks completion. With nothing to delete that check is one complete
listing of the keeper's key (#622).

No provider request here runs while an upload catalog permit is held: every
DB unit is entered and left around the listings and DELETEs, never across
them, so S3 latency never occupies a Vault's catalog slot (#622).
"""
from __future__ import annotations

from typing import Any

import psycopg

from . import upload_provenance as provenance
from .database_retry import owned_connection
from .s3_copy import _delete_s3_version
from .upload_provenance import UploadBlocked


def _superseded(connection: Any, file_id: str) -> list[dict[str, Any]]:
    """The file's versions that are neither Current nor the replacement in progress."""
    return connection.execute(
        """SELECT av.id, av.object_key, av.provider_version_id, av.integrity,
                  COALESCE(av.upload_attempt_id, av.copy_attempt_id) AS producer_id,
                  COALESCE(a.outcome, ca.outcome) AS outcome,
                  COALESCE(a.landed_version_id, ca.landed_version_id) AS landed_version_id,
                  c.batch_id, c.deleted_at
           FROM archive_versions av
           JOIN files f ON f.id = av.file_id
           LEFT JOIN upload_attempts a ON a.id = av.upload_attempt_id
           LEFT JOIN copy_attempts ca ON ca.id = av.copy_attempt_id
           LEFT JOIN cleanup_candidates c ON c.archive_version_id = av.id
           WHERE av.file_id = %s
             AND av.id IS DISTINCT FROM f.current_version_id
             AND av.id IS DISTINCT FROM f.replacing_version_id
           ORDER BY av.object_key, av.provider_version_id""",
        (file_id,)).fetchall()


def _keeper(connection: Any, job: dict[str, Any], keeper_id: str, key: str) -> dict[str, Any]:
    """The keeper must be the file's verified, available Current at the pinned key."""
    row = connection.execute(
        """SELECT av.id, av.object_key, av.provider_version_id
           FROM files f JOIN archive_versions av ON av.id = f.current_version_id
           WHERE f.id = %s AND av.id = %s AND av.integrity = 'verified'
             AND av.availability = 'available' AND f.replacing_version_id IS NULL
           FOR SHARE OF f""",
        (job["file_id"], keeper_id)).fetchone()
    if row is None or not row["provider_version_id"]:
        raise UploadBlocked("keeper_missing")
    if row["object_key"] != key:
        raise UploadBlocked("identity_changed")
    return dict(row)


def _current_id(connection: Any, file_id: str) -> str | None:
    row = connection.execute(
        "SELECT current_version_id FROM files WHERE id=%s", (file_id,)).fetchone()
    return str(row["current_version_id"]) if row and row["current_version_id"] else None


# Whether ``cleanup(keeper_id=None)`` has anything to do, over a ``files f``
# row: an open batch, or a superseded version while a Current exists. The
# same decision ``cleanup`` takes from its own load, for callers (the upload's
# first unit) that read the file row anyway.
RESUME_NEEDED_SQL = """(EXISTS (SELECT 1 FROM cleanup_batches WHERE file_id = f.id)
                  OR (f.current_version_id IS NOT NULL AND EXISTS (
                      SELECT 1 FROM archive_versions av
                      WHERE av.file_id = f.id
                        AND av.id IS DISTINCT FROM f.current_version_id
                        AND av.id IS DISTINCT FROM f.replacing_version_id)))"""


def _require_candidate(row: dict[str, Any]) -> None:
    """Only a superseded application write of this file can be deleted."""
    if row["integrity"] == "mismatch":
        raise UploadBlocked("proof_mismatch")
    # A landed upload attempt or a landed server-side copy (#595) of this file.
    if (row["producer_id"] is None or row["outcome"] != "landed"
            or row["landed_version_id"] != row["provider_version_id"]
            or not row["provider_version_id"]):
        raise UploadBlocked("expected_evidence_missing")


def _require_pin(batch: dict[str, Any], pinned: dict[str, Any]) -> None:
    frozen = (batch["provider"], batch["path_since"], batch["source_root"], batch["root_identity"])
    live = (pinned["provider"], pinned["path_since"], pinned["source_root"], pinned["root_identity"])
    if frozen != live:
        raise UploadBlocked("identity_changed")


def _listing(client: Any, bucket: str, keys: set[str]) -> dict[str, dict[str, list[str]]]:
    """Two equal complete listings of every key, or the inventory is unconfirmed."""
    first = {key: provenance.inventory(client, bucket, key) for key in sorted(keys)}
    if first != {key: provenance.inventory(client, bucket, key) for key in sorted(keys)}:
        raise UploadBlocked("inventory_changed")
    return first


def _require_known(observed: dict[str, dict[str, list[str]]], allowed: set[tuple[str, str]],
                   keeper: tuple[str, str]) -> None:
    if any(value["markers"] for value in observed.values()):
        # Replacement has no Delete Marker consent.
        raise UploadBlocked("markers_present")
    if keeper[1] not in observed[keeper[0]]["versions"]:
        raise UploadBlocked("keeper_missing")
    for key, value in observed.items():
        if any((key, version) not in allowed for version in value["versions"]):
            raise UploadBlocked("inventory_changed")


def _require_sole(observed: dict[str, list[str]], keeper_version: str) -> None:
    """The postcondition: the keeper is the only version at its key, no marker."""
    if observed["markers"]:
        raise UploadBlocked("markers_present")
    if keeper_version not in observed["versions"]:
        raise UploadBlocked("keeper_missing")
    if observed["versions"] != [keeper_version]:
        raise UploadBlocked("inventory_changed")


def require_sole_keeper(worker: Any, job: dict[str, Any], keeper: dict[str, Any], *,
                        client: Any, bucket: str) -> None:
    """Check the postcondition of a keeper that superseded nothing (#622).

    The publication unit that made ``keeper`` Current found no other version
    of the file and no open batch, so there is nothing to freeze or delete:
    one complete listing of the keeper's key must show the keeper alone. An
    unknown object there (an external writer, a send of another file that may
    have landed) still blocks completion, as in ``cleanup``.
    """
    if keeper["object_key"] != worker._upload_key(job):
        raise UploadBlocked("identity_changed")
    _require_sole(provenance.inventory(client, bucket, keeper["object_key"]),
                  keeper["provider_version_id"])


def cleanup(worker: Any, job: dict[str, Any], keeper_id: str | None, client: Any) -> None:
    """Delete exactly what ``keeper_id`` superseded, then remove the rows with the proof.

    ``keeper_id=None`` resumes an open cleanup of the file's Current (a Job that
    did not produce the keeper itself) and does nothing when none is needed.
    """
    w = worker
    active = lambda: w.ensure_job_active(job["id"], "Snapshot cleanup stopped")
    provider = provenance.provider_identity(client) if client is not None else None
    key = w._upload_key(job)

    def load(catalog: Any) -> tuple[Any, ...]:
        connection = catalog.connection
        batch = connection.execute(
            "SELECT * FROM cleanup_batches WHERE file_id=%s", (job["file_id"],)).fetchone()
        # A published keeper is read in the same unit (#597): no second unit
        # only to find the row the publication just committed.
        keeper = _keeper(connection, job, str(keeper_id), key) if keeper_id is not None else None
        # The keeper is the file's Current by construction.
        current = str(keeper["id"]) if keeper is not None else _current_id(connection, job["file_id"])
        return (provenance.validated_pin(connection, job), current,
                batch, _superseded(connection, job["file_id"]), keeper)

    pinned, current, batch, superseded, keeper = w._upload_catalog_unit(job, load, shared=True)
    if keeper_id is None:
        if batch is None and (not superseded or current is None):
            # Nothing to resume, or no Current to keep yet: the next verified
            # keeper's own cleanup decides (a proven mismatch then blocks it).
            return
        if current is None:
            raise UploadBlocked("keeper_missing")
        keeper_id = current
    if client is None:
        client = w.s3_client()
        provider = provenance.provider_identity(client)
    pinned = {**pinned, "provider": provider}
    if batch is not None:
        if str(batch["keeper_id"]) != str(keeper_id):
            # Another keeper's batch is still open: it is never re-pointed.
            raise UploadBlocked("target_superseded")
        _require_pin(batch, pinned)
    if any(row["batch_id"] is None for row in superseded) and batch is not None:
        # Nothing joins a frozen batch.
        raise UploadBlocked("inventory_changed")
    for row in superseded:
        _require_candidate(row)
    if superseded or batch is not None:
        # Before the batch is frozen or anything is deleted. Each DELETE checks
        # the Source Volume/root again; listings alone are no effect.
        w._require_upload_source_identity(job)
    if keeper is None:
        def keeper_row(catalog: Any) -> dict[str, Any]:
            return _keeper(catalog.connection, job, str(keeper_id), key)

        keeper = w._upload_catalog_unit(job, keeper_row, shared=True)
    keep = (keeper["object_key"], keeper["provider_version_id"])
    candidates = {(row["object_key"], row["provider_version_id"]): row for row in superseded}
    allowed = set(candidates) | {keep}
    keys = {keep[0]} | {object_key for object_key, _ in candidates}
    bucket = pinned["bucket"]
    if not candidates and batch is None:
        # Nothing to freeze or delete: the first and the final listing would
        # be the same check back to back. One listing is the postcondition.
        _require_sole(provenance.inventory(client, bucket, keep[0]), keep[1])
        return
    observed = _listing(client, bucket, keys)
    _require_known(observed, allowed, keep)

    if candidates and batch is None:
        def freeze(catalog: Any) -> dict[str, Any]:
            connection = catalog.connection
            _keeper(connection, job, str(keeper_id), key)
            if {(r["object_key"], r["provider_version_id"]) for r in _superseded(connection, job["file_id"])} != set(candidates):
                raise UploadBlocked("inventory_changed")
            frozen = connection.execute(
                """INSERT INTO cleanup_batches(vault_id, file_id, keeper_id, job_id, provider,
                                               path_since, source_root, root_identity)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (job["vault_id"], job["file_id"], keeper_id, job["id"], provider,
                 pinned["path_since"], pinned["source_root"], pinned["root_identity"])).fetchone()
            connection.execute(
                """INSERT INTO cleanup_candidates(batch_id, file_id, archive_version_id)
                   SELECT %s, %s, unnest(%s::uuid[])""",
                (frozen["id"], job["file_id"], [str(r["id"]) for r in candidates.values()]))
            return frozen

        batch = w._upload_catalog_unit(job, freeze)

    for (object_key, version), row in sorted(candidates.items()):
        if row["deleted_at"] is not None or version not in observed[object_key]["versions"]:
            continue
        active()
        w._require_upload_source_identity(job)
        w.validate_cloud_vault(job)
        current_listing = _listing(client, bucket, keys)
        _require_known(current_listing, allowed, keep)
        if version in current_listing[object_key]["versions"]:
            _delete_fenced(w, job, pinned, batch, keep, client, object_key, version)
        _record_deleted(w, job, batch, [str(row["id"])])
        observed = current_listing
        observed[object_key] = {**observed[object_key],
                                "versions": [v for v in observed[object_key]["versions"] if v != version]}

    if candidates:
        active()
    final = _listing(client, bucket, keys)
    if any(value["markers"] for value in final.values()):
        raise UploadBlocked("markers_present")
    if final[keep[0]]["versions"] != [keep[1]] or any(
            value["versions"] for object_key, value in final.items() if object_key != keep[0]):
        raise UploadBlocked("inventory_changed")
    if batch is None:
        return
    w._require_upload_source_identity(job)

    def complete(catalog: Any) -> None:
        connection = catalog.connection
        _keeper(connection, job, str(keeper_id), key)
        live = connection.execute(
            "SELECT * FROM cleanup_batches WHERE id=%s AND keeper_id=%s FOR UPDATE",
            (batch["id"], keeper_id)).fetchone()
        if live is None:
            raise UploadBlocked("identity_changed")
        # The frozen versions are all absent from the complete final listing.
        connection.execute(
            "UPDATE cleanup_candidates SET deleted_at=now() WHERE batch_id=%s AND deleted_at IS NULL",
            (batch["id"],))
        connection.execute(
            """DELETE FROM archive_versions
               WHERE id IN (SELECT archive_version_id FROM cleanup_candidates WHERE batch_id=%s)""",
            (batch["id"],))
        connection.execute("DELETE FROM cleanup_batches WHERE id=%s", (batch["id"],))
        catalog._mark_file_aggregates_dirty(int(job["vault_id"]), str(job["file_id"]))

    w._upload_catalog_unit(job, complete)


def _delete_fenced(w: Any, job: dict[str, Any], pinned: dict[str, Any], batch: dict[str, Any],
                   keep: tuple[str, str], client: Any, object_key: str, version: str) -> None:
    """One exact DELETE while every fence is held; never replayed by a DB retry.

    The transaction locks the Vault (SHARE), the Job row and its lease (a
    conditional renewal that fails after cancellation, takeover or expiry) and
    the file's Current, so none of them can change before the DELETE returns.
    A failure to COMMIT afterwards (the transaction only renewed the lease)
    neither repeats the DELETE nor hides it: recording it is the next,
    separately fenced DB unit, and a later run finds the frozen version absent.
    """
    deleted = False
    try:
        with owned_connection(w.db()) as connection:
            deleted = _delete_holding_fences(w, connection, job, pinned, batch, keep, client,
                                             object_key, version)
    except psycopg.Error:
        if not deleted:
            raise


def _delete_holding_fences(w: Any, connection: Any, job: dict[str, Any], pinned: dict[str, Any],
                           batch: dict[str, Any], keep: tuple[str, str], client: Any,
                           object_key: str, version: str) -> bool:
    provenance.require_target(connection, job, pinned, shared=True)
    w.ensure_job_claim_owned_in_transaction(connection, job, "Snapshot cleanup claim lost")
    keeper_id = str(batch["keeper_id"])
    _keeper(connection, job, keeper_id, keep[0])
    grant = connection.execute(
        """SELECT 1 FROM cleanup_candidates c
           JOIN cleanup_batches b ON b.id = c.batch_id
           JOIN archive_versions av ON av.id = c.archive_version_id
           WHERE b.id=%s AND b.keeper_id=%s AND b.file_id=%s
             AND av.object_key=%s AND av.provider_version_id=%s""",
        (batch["id"], keeper_id, job["file_id"], object_key, version)).fetchone()
    if grant is None or (object_key, version) == keep:
        raise UploadBlocked("ambiguous_receipt")
    if w.job_cancelled(job["id"]):
        raise w.OperationCancelled("Snapshot cleanup stopped")
    _delete_s3_version(client, bucket=pinned["bucket"], object_key=object_key, version_id=version)
    return True


def _record_deleted(w: Any, job: dict[str, Any], batch: dict[str, Any], version_ids: list[str]) -> None:
    def record(catalog: Any) -> None:
        catalog.connection.execute(
            """UPDATE cleanup_candidates SET deleted_at=now()
               WHERE batch_id=%s AND archive_version_id = ANY(%s::uuid[]) AND deleted_at IS NULL""",
            (batch["id"], version_ids))

    w._upload_catalog_unit(job, record, shared=True)
