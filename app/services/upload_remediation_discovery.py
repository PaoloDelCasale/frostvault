"""Metadata-only materialized-target discovery, never publication/delete authority.

Exact app intents or independent AV baselines identify obligations. Discovery
observations alone cannot create a keeper. Completed episodes remain immutable
history when a later scan creates a new cleanup obligation for the same keeper.
"""
from __future__ import annotations

import json
import uuid
from time import monotonic
from datetime import datetime, timedelta
from typing import Any

from . import upload_provenance as provenance
from .upload_snapshot import UploadSnapshot
from .vault_paths import expected_cloud_key


# A just-started send may still be between its durable commit and publication.
# Active Jobs are excluded regardless of lease expiry. Targeted admission cannot
# wait for grace: it must see the outstanding intent before allowing a fresh PUT.
SEND_GRACE_SECONDS = 10


def obligation_query(*, vault_sql: str, stamp: str, file_id: str | None = None,
                     limit: int | None = None) -> tuple[str, tuple[Any, ...]]:
    """Indexed source-of-truth frontier, shared by sweep, lookup and telemetry.

    Filter active and already-enrolled targets BEFORE every LIMIT. Otherwise a
    bounded sweep can spend forever rediscovering its first cancelled/blocked
    targets. Closed episodes suppress only the old obligation: a durable block
    on their verified keeper is new cleanup work, regardless of clock ordering.
    No source row is consumed here.
    """
    cutoff = (datetime.fromisoformat(stamp) - timedelta(seconds=SEND_GRACE_SECONDS)).isoformat()
    params: list[Any] = []

    def scope(alias: str) -> str:
        predicate = f"{alias}.vault_id={vault_sql}"
        if file_id is not None:
            predicate += f" AND {alias}.vault_file_id=%s"
            params.append(file_id)
        return predicate

    def idle(alias: str) -> str:
        return f"""NOT EXISTS (SELECT 1 FROM jobs active
            WHERE active.vault_file_id={alias}.vault_file_id
              AND active.status NOT IN ('failed','cancelled','completed'))"""

    intent_scope = scope("i")
    grace = ""
    if file_id is None:
        grace = " AND i.created_at<=%s"
        params.append(cutoff)
    intents = f"""SELECT i.id intent_id,i.archive_version_id,i.vault_file_id,i.created_at obligation_at,i.id obligation_key
        FROM upload_intents i WHERE {intent_scope} AND i.send_started AND NOT i.settled{grace}
          AND {idle('i')}
          AND NOT EXISTS (SELECT 1 FROM upload_remediations r WHERE r.intent_id=i.id)"""
    version_scope = scope("av")
    versions = f"""SELECT NULL::text intent_id,av.id archive_version_id,av.vault_file_id,av.discovered_at obligation_at,av.id obligation_key
        FROM archive_versions av WHERE {version_scope} AND av.integrity='unverified'
          AND av.availability='available'
          AND (av.upload_snapshot_json IS NOT NULL OR (av.origin='upload' AND EXISTS (
              SELECT 1 FROM jobs evidence WHERE evidence.archive_version_id=av.id
                AND evidence.vault_file_id=av.vault_file_id AND evidence.action='upload'
                AND evidence.upload_plaintext_sha256 ~ '^[0-9a-f]{{64}}$'
              OFFSET 0)))
          AND {idle('av')}
          AND NOT EXISTS (SELECT 1 FROM upload_intents i WHERE i.vault_file_id=av.vault_file_id
              AND i.send_started AND NOT i.settled)
          AND NOT EXISTS (SELECT 1 FROM upload_remediations r WHERE r.archive_version_id=av.id)"""
    # Start at the small durable block table, not the historical verified catalog.
    block_scope = scope("vf").replace("vf.vault_file_id", "vf.id")
    blocks = f"""SELECT NULL::text intent_id,av.id archive_version_id,vf.id vault_file_id,b.updated_at obligation_at,av.id obligation_key
        FROM snapshot_reconciliation_blocks b JOIN vault_files vf ON vf.id=b.vault_file_id
        JOIN archive_versions av ON av.id=vf.current_version_id
            AND av.integrity='verified' AND av.availability='available'
        WHERE {block_scope} AND {idle('av')}
          AND NOT EXISTS (SELECT 1 FROM upload_intents i WHERE i.vault_file_id=vf.id
              AND i.send_started AND NOT i.settled)
          AND NOT EXISTS (SELECT 1 FROM upload_remediations r WHERE r.archive_version_id=av.id
              AND r.state<>'resolved')"""
    branches = [intents, versions, blocks]
    if limit is not None:
        # Cap each indexed source before the UNION sort; never sort an entire
        # vault's outstanding backlog just to return TARGET_BATCH candidates.
        branches = [f"({sql} ORDER BY obligation_at,obligation_key LIMIT {int(limit)})" for sql in branches]
    return " UNION ALL ".join(branches), tuple(params)


def discover(connection: Any, vault: dict[str, Any], stamp: str, limit: int, *,
             file_id: str | None = None, deadline: float | None = None) -> None:
    sql, params = obligation_query(vault_sql=str(int(vault['id'])), stamp=stamp, file_id=file_id, limit=limit)
    rows = connection.execute(f"""SELECT * FROM ({sql}) obligations
        ORDER BY obligation_at,obligation_key LIMIT %s""", (*params, limit)).fetchall()
    for obligation in rows:
        if deadline is not None and monotonic() >= deadline:
            break
        enroll(connection, None, vault, stamp, intent_id=obligation['intent_id'],
               version_id=obligation['archive_version_id'])


def _reject(connection: Any, original: dict[str, Any] | None, stamp: str, reason: str) -> None:
    if original is not None:
        changed = connection.execute("""INSERT INTO upload_remediation_discovery(job_id,vault_id,discovered_at,rejected_reason)
            VALUES (%s,%s,%s,%s) ON CONFLICT (job_id) DO UPDATE SET rejected_reason=excluded.rejected_reason
            WHERE upload_remediation_discovery.rejected_reason IS DISTINCT FROM excluded.rejected_reason RETURNING job_id""",
            (original["id"], original["vault_id"], stamp, reason)).fetchone()
        if changed:
            from .upload_remediation import _invalidate
            _invalidate(connection, original["vault_id"], stamp)


def enroll(connection: Any, original: dict[str, Any] | None, vault: dict[str, Any], stamp: str,
           *, intent_id: str | None = None, version_id: str | None = None) -> dict[str, Any] | None:
    from .upload_remediation import _invalidate, _sha
    if original is not None:
        if original["upload_reconcile_only"] or original["status"] != "failed":
            return None
        linked = connection.execute("""SELECT r.* FROM upload_remediations r
            JOIN upload_remediation_originals o ON o.remediation_id=r.id WHERE o.original_job_id=%s""", (original["id"],)).fetchone()
        if linked:
            return linked
        intent = provenance.load_for_job(connection, original["id"])
        version_id = original["archive_version_id"]
    else:
        intent = connection.execute("SELECT * FROM upload_intents WHERE id=%s AND vault_id=%s", (intent_id, vault["id"])).fetchone() if intent_id else None
        if intent and intent["settled"]:
            # A settled intent is finished provenance. Remaining cleanup is an
            # exact Archive Version obligation, never a replay of old inventory.
            version_id, intent = intent["archive_version_id"] or version_id, None
    if intent:
        if original and original["archive_version_id"] and intent["archive_version_id"] and original["archive_version_id"] != intent["archive_version_id"]:
            _reject(connection, original, stamp, "proof_mismatch")
            return None
        version_id = intent["archive_version_id"] or version_id
    version = connection.execute("SELECT * FROM archive_versions WHERE id=%s AND vault_id=%s", (version_id, vault["id"])).fetchone() if version_id else None
    if not intent and version:
        intent = connection.execute("SELECT * FROM upload_intents WHERE archive_version_id=%s AND vault_id=%s AND vault_file_id=%s AND send_started AND (%s OR NOT settled) ORDER BY created_at DESC LIMIT 1", (version["id"], vault["id"], version["vault_file_id"], original is not None)).fetchone()
    if not version and not (intent and intent["send_started"]):
        _reject(connection, original, stamp, "expected_evidence_missing")
        return None
    file_id = intent["vault_file_id"] if intent else version["vault_file_id"]
    if ((intent and intent["vault_id"] != vault["id"]) or (version and version["vault_file_id"] != file_id)
            or (original and original["vault_file_id"] != file_id)
            or not connection.execute("SELECT 1 FROM vault_files WHERE id=%s AND vault_id=%s", (file_id, vault["id"])).fetchone()):
        _reject(connection, original, stamp, "identity_changed")
        return None
    path = connection.execute("SELECT path,valid_from FROM file_paths WHERE vault_file_id=%s AND vault_id=%s AND valid_to IS NULL", (file_id, vault["id"])).fetchone()
    block = connection.execute("SELECT * FROM snapshot_reconciliation_blocks WHERE vault_file_id=%s", (file_id,)).fetchone()
    if original is None and (not version or version["integrity"] != "unverified") and not (intent and not intent["settled"]) and not block:
        return None  # Successful ordinary writes do not need redundant Jobs.
    reason = None
    snapshot_json = intent["snapshot_json"] if intent else version["upload_snapshot_json"] or (original["upload_snapshot_json"] if original else None)
    expected = None
    size = (version["size"] if version else original["total_bytes"] if original else 0) or 0
    mode = intent["requested"] if intent else version["upload_verification_requested"] or (original["upload_verification_requested"] if original else None) or "read_back"
    if snapshot_json:
        try:
            snapshot = UploadSnapshot.from_json(snapshot_json)
            expected, size = snapshot.plaintext_sha256, snapshot.size
            if version and version["size"] != size:
                reason = "proof_mismatch"
        except (ValueError, TypeError, KeyError, AttributeError):
            reason = "proof_mismatch"
    elif original:
        expected = original["upload_plaintext_sha256"]
    elif version["integrity"] == "verified":
        expected = version["plaintext_sha256"]  # Prior independent verification.
    else:
        evidence = connection.execute("""SELECT upload_plaintext_sha256,upload_verification_requested
            FROM jobs WHERE vault_file_id=%s AND archive_version_id=%s AND action='upload'
              AND NOT upload_reconcile_only AND upload_plaintext_sha256 IS NOT NULL
            ORDER BY id LIMIT 1""", (file_id, version["id"])).fetchone()
        if evidence and version["origin"] == "upload":
            expected, mode = evidence["upload_plaintext_sha256"], evidence["upload_verification_requested"] or "read_back"
    if not _sha(expected):
        reason = reason or "expected_evidence_missing"
    if original and original["upload_plaintext_sha256"] and original["upload_plaintext_sha256"] != expected:
        reason = "proof_mismatch"
    if version and version["integrity"] == "verified" and version["plaintext_sha256"] != expected:
        reason = "proof_mismatch"
    if intent:
        try:
            pinned = json.loads(intent["target_json"])
            if not isinstance(pinned, dict) or pinned.get("vault_id") != vault["id"] or pinned.get("vault_file_id") != file_id or not pinned.get("path") or not pinned.get("key"):
                raise ValueError("Invalid exact target")
        except (ValueError, TypeError):
            pinned, reason = {}, "proof_mismatch"  # Durable blocked diagnostic, never admission.
        key = "intent:" + intent["id"]
    else:
        admitted_path = original["path"] if original else path["path"] if path else ""
        pinned = {"vault_id": vault["id"], "vault_file_id": file_id,
            "bucket": vault["s3_bucket"], "prefix": vault["s3_prefix"], "key": version["object_key"],
            "path": admitted_path, "source_root": vault["source_root"],
            "root_identity": vault.get("root_identity"), "root_identity_version": vault.get("root_identity_version"),
            "path_since": path["valid_from"] if path else None}
        if version["object_key"] != expected_cloud_key(admitted_path, vault["s3_prefix"]):
            reason = "identity_changed"
        key = "version:" + version["id"]
    if not path or path["path"] != pinned.get("path"):
        reason = reason or "identity_changed"
    if version:
        superseded = connection.execute("""SELECT 1 FROM vault_files WHERE id=%s
            AND current_version_id IS NOT NULL AND current_version_id<>%s
            AND replacing_version_id IS DISTINCT FROM %s""", (file_id, version["id"], version["id"])).fetchone()
        if version["availability"] == "purged" or superseded:
            reason = reason or "target_superseded"
        elif version["integrity"] == "mismatch":
            reason = "proof_mismatch"
    cancelled = connection.execute("""SELECT 1 FROM jobs j WHERE j.vault_file_id=%s AND j.action='upload'
        AND j.status='cancelled' AND (j.archive_version_id=%s OR EXISTS (
            SELECT 1 FROM upload_intent_jobs b WHERE b.job_id=j.id AND b.intent_id=%s)) LIMIT 1""",
        (file_id, version["id"] if version else None, intent["id"] if intent else None)).fetchone()
    budget = int(original["retry_count"] or 0) if original else 0
    state = "cancelled" if cancelled else "blocked" if reason or budget >= 8 else "pending"
    if budget >= 8 and not reason and not cancelled:
        reason = "retry_budget_exhausted"
    target_json = provenance.canonical(pinned)
    candidates = connection.execute("""SELECT * FROM upload_remediations WHERE vault_file_id=%s
        AND (target_key=%s OR intent_id=%s OR archive_version_id=%s)
        ORDER BY (state<>'resolved') DESC,created_at DESC,id DESC""",
        (file_id, key, intent["id"] if intent else None, version["id"] if version else None)).fetchall()
    row = None
    for candidate in candidates:
        same = candidate["expected_sha256"] == expected and candidate["snapshot_json"] == snapshot_json and candidate["target_json"] == target_json and candidate["requested_mode"] == mode and candidate["size"] == size
        if candidate["state"] != "resolved":
            row = candidate
            if not same or reason == "proof_mismatch":
                connection.execute("UPDATE upload_remediations SET state='blocked',reason='proof_mismatch',updated_at=%s WHERE id=%s AND state<>'cancelled'", (stamp, row["id"]))
                _reject(connection, original, stamp, "proof_mismatch")
                return connection.execute("SELECT * FROM upload_remediations WHERE id=%s", (row["id"],)).fetchone()
            if reason and candidate["state"] != "cancelled":
                connection.execute("UPDATE upload_remediations SET state='blocked',reason=%s,updated_at=%s WHERE id=%s", (reason, stamp, row["id"]))
                row = connection.execute("SELECT * FROM upload_remediations WHERE id=%s", (row["id"],)).fetchone()
            break
        # A completed episode resolves only failures that precede its success.
        if same and not reason and (not original or connection.execute("SELECT %s::timestamptz <= %s::timestamptz AS precedes", (original["updated_at"], candidate["resolved_at"])).fetchone()["precedes"]):
            if original or not block:
                row = candidate
                break
    if row is None:
        if candidates:
            # New scan/failure obligation, not a budget reset for an open or
            # cancelled target. Old successful episode and links stay intact.
            key += ":cleanup:" + block["updated_at"] + ":" + str(uuid.uuid4()) if block else ":failure:" + str(original["id"]) if original else ":obligation:" + stamp
        row = connection.execute("""INSERT INTO upload_remediations(id,target_key,vault_id,vault_file_id,
            intent_id,archive_version_id,target_json,expected_sha256,snapshot_json,requested_mode,size,
            state,phase,reason,retry_count,due_at,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (str(uuid.uuid4()), key, vault["id"], file_id, intent["id"] if intent else None,
             version["id"] if version else None, target_json, expected, snapshot_json, mode, size,
             state, "cleanup" if version and version["integrity"] == "verified" else "verification", reason,
             budget, stamp, stamp, stamp)).fetchone()
    if original and reason == "proof_mismatch":
        _reject(connection, original, stamp, reason)
    elif original:
        connection.execute("""INSERT INTO upload_remediation_originals(original_job_id,remediation_id,vault_file_id,vault_id)
            VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (original["id"], row["id"], file_id, vault["id"]))
    _invalidate(connection, vault["id"], stamp)
    return row
