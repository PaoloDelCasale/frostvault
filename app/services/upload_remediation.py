"""Upload Remediation guided by open obligations, never a queue of facts (#592).

An episode (``upload_remediations``) exists only for a Vault File whose upload
ended (failed or cancelled) while cloud work was still open. The work is the
file's live, durable state, never a copy and never Job history:

- a started send whose outcome is unknown (an unsettled ``upload_attempts`` row);
- a materialized replacement not yet verified (``files.replacing_version_id``);
- an open cleanup batch or superseded versions (``cleanup_batches``, versions
  that are neither Current nor the replacement);
- a reconciliation block (``files.reconciliation_block``) or a late receipt
  that contradicts a settled attempt.

The Job transition that ends with that work opens (or joins) the episode in the
same transaction; a successful upload writes nothing here, so the normal case
leaves no backlog. Lookup per file and the admission sweep read only the open
episodes (partial indexes), never the accumulated history.

Admission creates only reconcile-only Jobs. The upload worker does the exact
work with its own fences (ADR-0024/0025/0029/0036): it adopts the uncertain
attempt and decides it from S3 reads (a proven no-write lets that same Job
send the same snapshot as a new attempt with a new token), verifies the
replacement against its attempt's snapshot, resumes the frozen cleanup batch.
Nothing here performs provider or filesystem I/O.

An episode resolves only when its Job completes with the work closed: no
started send, no replacement in progress, no cleanup batch and no superseded
version left (versions disappear only with their completed cleanup proof) and
the verified keeper is Current. A timestamp is never enough. The retry budget
only grows, an Owner cancel is final, and the episode outlives the collection
of its Jobs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

from . import upload_provenance as provenance
from .upload_retry import UPLOAD_RETRY_MAX_ATTEMPTS

POLL_SECONDS = 10
# A failure is admitted again only after this grace: the original Job's own
# retries, the Owner's cancel or a quick correction come first.
GRACE_SECONDS = POLL_SECONDS
SWEEP_NAMES = ("admission",)
SWEEP_STALE_SECONDS = 5 * POLL_SECONDS
VAULT_BATCH = 10
TARGET_BATCH = 10
MAX_RETRIES = UPLOAD_RETRY_MAX_ATTEMPTS
TERMINAL = ("completed", "failed", "cancelled")
_TERMINAL_SQL = "('completed', 'failed', 'cancelled')"
# Contradictory or missing evidence: the Owner investigates, nothing retries.
FINAL_REASONS = frozenset({"proof_mismatch", "expected_evidence_missing", "target_superseded"})
# Conditions the Owner cannot fix by asking for a retry.
_UNRESUMABLE = FINAL_REASONS | {"source_volume_unavailable", "vault_suspended"}
# A void episode (its work closed by itself, e.g. a send proven absent and
# never restarted) is not swept: the next Job that ends on the file decides it.
_NEVER = "infinity"

# Notification closure, alias ``original`` for a failed Job: its episode was
# resolved with the exact postcondition, or closed because the file is gone.
RESOLUTION_SQL = """EXISTS (
    SELECT 1 FROM upload_remediation_jobs closing_link
    JOIN upload_remediations closing ON closing.id = closing_link.remediation_id
    WHERE closing_link.job_id = original.id AND closing.state = 'resolved'
)"""
FILE_REMOVED_SQL = """EXISTS (
    SELECT 1 FROM upload_remediation_jobs removed_link
    JOIN upload_remediations removed ON removed.id = removed_link.remediation_id
    WHERE removed_link.job_id = original.id
      AND removed.state = 'cancelled' AND removed.reason = 'file_removed'
)"""
CLOSED_SQL = f"({RESOLUTION_SQL} OR {FILE_REMOVED_SQL})"
# The Job belongs to an episode (alias ``original``).
LINKED_SQL = """EXISTS (
    SELECT 1 FROM upload_remediation_jobs linked WHERE linked.job_id = original.id
)"""

_WORK_SQL = f"""
SELECT f.id AS file_id, f.vault_id, f.path, f.path_since, f.held_version_id,
       f.current_version_id, f.replacing_version_id, f.reconciliation_block,
       cur.integrity AS current_integrity,
       (SELECT a.id FROM upload_attempts a WHERE a.file_id = f.id
          AND a.settled_at IS NULL AND a.send_started_at IS NOT NULL) AS attempt_id,
       EXISTS (SELECT 1 FROM cleanup_batches b WHERE b.file_id = f.id
                 AND b.keeper_id IS NOT NULL) AS cleanup_open,
       EXISTS (SELECT 1 FROM jobs d WHERE d.file_id = f.id AND d.action = 'cloud-purge'
                 AND d.status NOT IN {_TERMINAL_SQL}) AS pending_deletion,
       EXISTS (SELECT 1 FROM archive_versions av WHERE av.file_id = f.id
                 AND av.id IS DISTINCT FROM f.current_version_id
                 AND av.id IS DISTINCT FROM f.replacing_version_id) AS superseded,
       {provenance._CONTRADICTED} AS contradicted
FROM files f LEFT JOIN archive_versions cur ON cur.id = f.current_version_id
WHERE f.id = %(file)s"""


def _later(stamp: Any, seconds: int = POLL_SECONDS) -> str:
    moment = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
    return (moment + timedelta(seconds=seconds)).isoformat()


def _invalidate(connection: Any, vault_id: int, stamp: Any) -> None:
    from .catalog_events import CatalogEventStore
    # A bounded, durable invalidation only; protection evidence is untouched.
    CatalogEventStore(connection).append_event(
        vault_id=vault_id, domain="catalog", scope="",
        payload={"invalidate": ["files"], "reason": "upload_remediation"},
        created_at=str(stamp))


# -- the file's live work ------------------------------------------------------

def work(connection: Any, file_id: Any) -> dict[str, Any] | None:
    """The file's open cloud work, read from its live state (indexed per file)."""
    row = connection.execute(_WORK_SQL, {"file": str(file_id)}).fetchone()
    return dict(row) if row else None


def is_open(state: dict[str, Any] | None) -> bool:
    return bool(state and (
        state["attempt_id"] or state["replacing_version_id"] or state["cleanup_open"]
        or state["superseded"] or state["reconciliation_block"] or state["contradicted"]))


def _closed_by(state: dict[str, Any] | None, job: dict[str, Any]) -> bool:
    """The exact postcondition of a resolution: nothing left, keeper verified.

    The cleanup batch is completed exactly when neither the batch nor any
    superseded version is left: their rows are removed only together with the
    recorded exact deletions (``archive_versions_removed_with_proof``).
    """
    return bool(
        state and not is_open(state)
        and state["current_version_id"] is not None
        and state["current_integrity"] == "verified"
        and (job["archive_version_id"] is None
             or str(job["archive_version_id"]) == str(state["current_version_id"])))


def _blocking_reason(state: dict[str, Any]) -> str | None:
    return state["reconciliation_block"] or ("ambiguous_receipt" if state["contradicted"] else None)


def _work_refs(state: dict[str, Any]) -> tuple[Any, Any]:
    """What the episode is about: the uncertain attempt or the exact version."""
    if state["attempt_id"]:
        return state["attempt_id"], None
    return None, state["replacing_version_id"] or state["current_version_id"]


def _same_work(episode: dict[str, Any], state: dict[str, Any]) -> bool:
    attempt, version = _work_refs(state)
    return bool((episode["attempt_id"] and attempt and str(episode["attempt_id"]) == str(attempt))
                or (episode["archive_version_id"] and version
                    and str(episode["archive_version_id"]) == str(version)))


# -- episodes --------------------------------------------------------------------

def _open_episode(connection: Any, file_id: Any, *, lock: bool = True) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT * FROM upload_remediations WHERE file_id=%s AND closed_at IS NULL"
        + (" FOR UPDATE" if lock else ""), (str(file_id),)).fetchone()


def _latest_cancelled(connection: Any, file_id: Any) -> dict[str, Any] | None:
    return connection.execute(
        """SELECT * FROM upload_remediations WHERE file_id=%s AND state='cancelled'
           ORDER BY created_at DESC, id DESC LIMIT 1""", (str(file_id),)).fetchone()


def _link(connection: Any, job_id: int, episode_id: Any) -> None:
    connection.execute(
        "INSERT INTO upload_remediation_jobs(job_id, remediation_id) VALUES (%s, %s) "
        "ON CONFLICT (job_id) DO NOTHING", (job_id, episode_id))


def _update(connection: Any, episode: dict[str, Any], stamp: Any, **values: Any) -> None:
    values.setdefault("updated_at", stamp)
    if values.get("state") in {"cancelled", "resolved"}:
        values.setdefault("closed_at", stamp)
    columns = ", ".join(f"{name}=%s" for name in values)
    connection.execute(f"UPDATE upload_remediations SET {columns} WHERE id=%s",
                       (*values.values(), episode["id"]))
    if any(values.get(k, episode.get(k)) != episode.get(k) for k in ("state", "reason")):
        _invalidate(connection, episode["vault_id"], stamp)


def _create(connection: Any, state: dict[str, Any], stamp: Any, *, phase: str,
            reason: str | None, retry_count: int, due_at: Any) -> dict[str, Any]:
    attempt, version = _work_refs(state)
    row = connection.execute(
        """INSERT INTO upload_remediations(vault_id, file_id, attempt_id, archive_version_id,
               state, reason, retry_count, due_at, created_at, updated_at, closed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (state["vault_id"], state["file_id"], attempt, version, phase, reason, retry_count,
         due_at, stamp, stamp, stamp if phase in {"cancelled", "resolved"} else None)).fetchone()
    _invalidate(connection, state["vault_id"], stamp)
    return row


def _record_end(connection: Any, job: dict[str, Any], status: str, stamp: Any,
                closing_reason: str | None) -> None:
    """An unlinked upload ended (failed or cancelled): keep its open work.

    Opens the file's episode, or joins the open one, or the cancelled episode
    of the same work: an Owner cancel is never undone by a later failure.
    Nothing is written when no cloud work is left (an ordinary failure).
    """
    state = work(connection, job["file_id"])
    if not is_open(state):
        return
    retries = int(job["retry_count"] or 0)
    episode = _open_episode(connection, job["file_id"])
    if episode is None:
        cancelled = _latest_cancelled(connection, job["file_id"])
        if cancelled is not None and _same_work(cancelled, state):
            _link(connection, job["id"], cancelled["id"])
            return
    reason = _blocking_reason(state)
    if status == "cancelled":
        phase, reason = "cancelled", "file_removed" if closing_reason == "file_removed" else "owner_cancelled"
    elif reason in FINAL_REASONS:
        phase = "blocked"
    elif max(retries, int(episode["retry_count"]) if episode else 0) >= MAX_RETRIES:
        phase, reason = "blocked", reason or "retry_budget_exhausted"
    else:
        phase = "pending"
    if episode is None:
        episode = _create(connection, state, stamp, phase=phase, reason=reason,
                          retry_count=retries, due_at=_later(stamp, GRACE_SECONDS))
    else:
        attempt, version = _work_refs(state)
        values: dict[str, Any] = {"retry_count": max(retries, int(episode["retry_count"])),
                                  "attempt_id": attempt, "archive_version_id": version}
        if episode["state"] != "blocked" or phase in {"blocked", "cancelled"}:
            values.update(state=phase, reason=reason, due_at=_later(stamp, GRACE_SECONDS))
        _update(connection, episode, stamp, **values)
    _link(connection, job["id"], episode["id"])


def _resolve(connection: Any, episode: dict[str, Any], job: dict[str, Any], stamp: Any) -> None:
    _link(connection, job["id"], episode["id"])
    _update(connection, episode, stamp, state="resolved", reason=None,
            current_job_id=job["id"], resolved_by_job_id=job["id"],
            archive_version_id=job["archive_version_id"] or episode["archive_version_id"])


def transition(connection: Any, job_id: int, status: str, stamp: Any, *,
               suspension: str | None = None, closing_reason: str | None = None) -> None:
    """Move the episode with its Job, in the Job's authoritative transaction."""
    job = connection.execute(
        "SELECT id, file_id, vault_id, action, status, retry_count, run_after, archive_version_id "
        "FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if job is None or job["action"] != "upload":
        return
    episode = connection.execute(
        """SELECT r.* FROM upload_remediation_jobs b JOIN upload_remediations r
           ON r.id=b.remediation_id WHERE b.job_id=%s FOR UPDATE OF r""", (job_id,)).fetchone()
    if episode is None or (episode["closed_at"] is None and episode["current_job_id"] != job_id):
        # Not this episode's attempt: an ordinary upload of the file.
        if status in {"failed", "cancelled"} and episode is None:
            _record_end(connection, job, status, stamp, closing_reason)
        elif status == "completed":
            current = _open_episode(connection, job["file_id"])
            if current is not None and _closed_by(work(connection, job["file_id"]), job):
                _resolve(connection, current, job, stamp)
        return
    if episode["closed_at"] is not None:
        if status == "completed" and episode["resolved_by_job_id"] != job_id:
            raise RuntimeError("Completed remediation history cannot be rewritten")
        return
    retries = max(int(job["retry_count"] or 0), int(episode["retry_count"]))
    if status == "completed":
        if not _closed_by(work(connection, job["file_id"]), job):
            raise RuntimeError("Upload remediation completion requires exact cleanup postcondition")
        _link(connection, job_id, episode["id"])
        _update(connection, episode, stamp, state="resolved", reason=None, retry_count=retries,
                resolved_by_job_id=job_id, archive_version_id=job["archive_version_id"]
                or episode["archive_version_id"])
        return
    if status == "cancelled":
        _update(connection, episode, stamp, state="cancelled", retry_count=retries,
                reason="file_removed" if closing_reason == "file_removed" else "owner_cancelled")
        return
    if status == "failed":
        state = work(connection, job["file_id"])
        reason = (_blocking_reason(state) if state else None) or (
            "retry_budget_exhausted" if retries >= MAX_RETRIES else "operation_failed")
        _update(connection, episode, stamp, state="blocked", reason=reason, retry_count=retries)
        return
    if suspension and status in {"queued", "retrying"}:
        values = {"state": "suspended", "reason": suspension}
    elif status == "retrying":
        values = {"state": "retrying", "reason": "transient"}
    else:
        values = {"state": "queued", "reason": None}
    _update(connection, episode, stamp, retry_count=retries,
            due_at=job["run_after"] or stamp, **values)


# -- admission -------------------------------------------------------------------

def _pinned(connection: Any, state: dict[str, Any]) -> provenance.AdmittedSnapshot | None:
    """The frozen snapshot the work was admitted for (expected proof and target)."""
    if state["attempt_id"]:
        attempt = provenance.load_attempt(connection, str(state["attempt_id"]))
        return attempt.admitted if attempt else None
    version = state["replacing_version_id"] or state["current_version_id"]
    return provenance.expected_for_version(connection, str(version)) if version else None


def _suspension(connection: Any, state: dict[str, Any], vault: dict[str, Any],
                pinned: provenance.AdmittedSnapshot | None, *, automatic: bool) -> str | None:
    if vault["lifecycle"] != "active" or vault.get("paused"):
        return "vault_suspended"
    if pinned is not None and (
            pinned.target["path"] != state["path"]
            or pinned.target["path_since"] != state["path_since"]
            or pinned.target["source_root"] != vault["source_root"]
            or pinned.target["root_identity"] != vault.get("root_identity")):
        return "identity_changed"
    if state["held_version_id"]:
        return "cloud_only_conflict"
    if state.get("pending_deletion"):
        # A Delayed Cloud Deletion of the file is pending or running: its
        # consent or the revocation by a reappearance decides first.
        return "pending_deletion"
    health = connection.execute(
        "SELECT last_error FROM vault_component_health WHERE vault_id=%s AND component='source'",
        (vault["id"],)).fetchone()
    if health and health["last_error"]:
        return "source_volume_unavailable"
    if automatic:
        from .operation_policies import auto_upload_path_is_included, get_policy
        policy = get_policy(connection, vault["id"])
        if not policy.auto_upload or not auto_upload_path_is_included(state["path"], policy):
            return "protection_paused"
    return None


def _cancelled_snapshot(connection: Any, state: dict[str, Any]) -> bool:
    """An Owner-cancelled Job of this attempt's snapshot: never resurrected."""
    if not state["attempt_id"]:
        return False
    return connection.execute(
        """SELECT 1 FROM upload_attempts a JOIN jobs j ON j.status='cancelled'
             AND (j.id=a.job_id OR j.id IN (SELECT b.job_id FROM job_uploads b
                                            WHERE b.snapshot_id=a.snapshot_id))
           WHERE a.id=%s LIMIT 1""", (state["attempt_id"],)).fetchone() is not None


def admit(connection: Any, episode: dict[str, Any], vault: dict[str, Any], *, stamp: Any,
          automatic: bool = True, explicit: bool = False, group_id: str | None = None,
          group_path: str | None = None) -> dict[str, Any] | None:
    """Admit one reconcile-only Job for the episode. Caller holds the Vault lock.

    The schema's one-active-operation-per-file index protects every action;
    nothing is admitted while another Job of the file is active, for a closed,
    blocked (unless explicit) or exhausted (unless explicit) episode, or while
    the work is void. Explicit retry keeps the budget spent so far.
    """
    row = connection.execute("SELECT * FROM upload_remediations WHERE id=%s FOR UPDATE",
                             (episode["id"],)).fetchone()
    if row is None or row["closed_at"] is not None:
        return None
    if explicit and row["reason"] in FINAL_REASONS:
        return None
    if row["state"] == "blocked" and not explicit:
        return None
    if row["retry_count"] >= MAX_RETRIES and not explicit:
        _update(connection, row, stamp, state="blocked",
                reason=row["reason"] if row["state"] == "blocked" else "retry_budget_exhausted")
        return None
    active = connection.execute(
        f"SELECT id FROM jobs WHERE file_id=%s AND status NOT IN {_TERMINAL_SQL}",
        (row["file_id"],)).fetchone()
    if active is not None:
        if active["id"] != row["current_job_id"]:
            # Expired leases still belong to their active Job.
            _update(connection, row, stamp, state="suspended", reason="active_operation",
                    due_at=_later(stamp))
        return None
    state = work(connection, row["file_id"])
    if not is_open(state):
        if row["due_at"] != _NEVER:
            connection.execute("UPDATE upload_remediations SET due_at=%s WHERE id=%s",
                               (_NEVER, row["id"]))
        return None
    blocking = _blocking_reason(state)
    if blocking in FINAL_REASONS or (blocking == "ambiguous_receipt" and not explicit):
        _update(connection, row, stamp, state="blocked", reason=blocking)
        return None
    if _cancelled_snapshot(connection, state):
        _update(connection, row, stamp, state="cancelled", reason="owner_cancelled")
        return None
    pinned = _pinned(connection, state)
    if state["replacing_version_id"] or (not state["attempt_id"] and state["current_version_id"]):
        if pinned is None:
            _update(connection, row, stamp, state="blocked", reason="expected_evidence_missing")
            return None
    reason = _suspension(connection, state, vault, pinned, automatic=automatic)
    if reason:
        _update(connection, row, stamp, state="suspended", reason=reason, due_at=_later(stamp))
        return None
    group = group_id or str(uuid.uuid4())
    connection.execute(
        "INSERT INTO job_groups(id, vault_id, path, created_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (group, vault["id"], state["path"] if group_path is None else group_path, stamp))
    version = None if state["attempt_id"] else (state["replacing_version_id"] or state["current_version_id"])
    size = pinned.snapshot.size if pinned is not None else 0
    job = connection.execute(
        """INSERT INTO jobs(vault_id, file_id, archive_version_id, group_id, action, status,
               origin, total_bytes, retry_count, requested_at, updated_at)
           VALUES (%s,%s,%s,%s,'upload','queued',%s,%s,%s,%s,%s)
           ON CONFLICT DO NOTHING RETURNING id""",
        (vault["id"], row["file_id"], version, group, "automatic" if automatic else "manual",
         size, row["retry_count"], stamp, stamp)).fetchone()
    if job is None:
        return None
    mode = pinned.verification if pinned is not None else "read_back"
    connection.execute(
        "INSERT INTO job_uploads(job_id, verification_mode, reconcile_only) VALUES (%s, %s, true)",
        (job["id"], mode))
    if state["attempt_id"]:
        attempt = provenance.load_attempt(connection, str(state["attempt_id"]))
        provenance.bind_for_resume(connection, job["id"], attempt)
    _link(connection, job["id"], row["id"])
    _update(connection, row, stamp, state="queued", reason=None, current_job_id=job["id"],
            attempt_id=state["attempt_id"], archive_version_id=version or row["archive_version_id"])
    _invalidate(connection, vault["id"], stamp)
    return {"job_id": job["id"], "group_id": group}


def _vault(connection: Any, vault_id: int) -> dict[str, Any]:
    return connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()


def episode_files(connection: Any, file_ids: list[Any]) -> set[str]:
    """The files among ``file_ids`` that have any episode (one indexed query)."""
    if not file_ids:
        return set()
    return {str(row["file_id"]) for row in connection.execute(
        "SELECT DISTINCT file_id FROM upload_remediations WHERE file_id = ANY(%s::uuid[])",
        ([str(f) for f in file_ids],)).fetchall()}


def intercept_upload(connection: Any, *, vault_id: int, file_id: Any, stamp: Any,
                     automatic: bool, group_id: str | None = None, group_path: str | None = None,
                     user_id: int | None = None) -> tuple[bool, dict[str, Any] | None]:
    """An upload request for a file with open remediation work goes to the episode.

    Returns (handled, admitted). A void episode, or none, lets the ordinary
    upload through; a cancelled episode whose work is still open refuses it.
    """
    episode = _open_episode(connection, file_id)
    if episode is None:
        cancelled = _latest_cancelled(connection, file_id)
        if cancelled is None:
            return False, None
        state = work(connection, file_id)
        return (is_open(state) and _same_work(cancelled, state)), None
    if not is_open(work(connection, file_id)):
        return False, None
    return True, admit(connection, episode, _vault(connection, vault_id), stamp=stamp,
                       automatic=automatic, group_id=group_id, group_path=group_path)


def _refusal_reason(connection: Any, episode_id: Any) -> str:
    """Why explicit admission refused: the stored cause, never a fake identity change."""
    row = connection.execute("SELECT * FROM upload_remediations WHERE id=%s", (episode_id,)).fetchone()
    if row["state"] == "cancelled":
        return "owner_cancelled"
    if row["state"] == "resolved":
        return "already_resolved"
    busy = connection.execute(
        f"SELECT 1 FROM jobs WHERE file_id=%s AND status NOT IN {_TERMINAL_SQL} LIMIT 1",
        (row["file_id"],)).fetchone()
    if busy or row["state"] in {"queued", "retrying"} or row["reason"] == "active_operation":
        return "active_operation"
    if not is_open(work(connection, row["file_id"])):
        return "already_resolved"
    return row["reason"] if row["reason"] in provenance.REMEDIES else "proof_unavailable"


def _lock_vault(connection: Any, vault_id: int) -> dict[str, Any]:
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s FOR NO KEY UPDATE",
                               (vault_id,)).fetchone()
    if vault is None or vault["lifecycle"] != "active":
        raise provenance.UploadBlocked("vault_suspended")
    return vault


def schedule_resume(connection: Any, *, vault: dict[str, Any], path: str, user_id: int | None,
                    timestamp: Any, remediation_id: str | None = None) -> dict[str, Any]:
    """The Owner's Resume of one file: an explicit, budget-keeping admission."""
    current = _lock_vault(connection, vault["id"])
    file = connection.execute("SELECT id FROM files WHERE vault_id=%s AND path=%s",
                              (current["id"], path)).fetchone()
    if file is None:
        raise provenance.UploadBlocked("identity_changed")
    if remediation_id:
        episode = connection.execute(
            "SELECT * FROM upload_remediations WHERE id=%s AND file_id=%s",
            (remediation_id, file["id"])).fetchone()
        if episode is None:
            raise provenance.UploadBlocked("proof_unavailable")
        if episode["closed_at"] is not None:
            raise provenance.UploadBlocked(_refusal_reason(connection, episode["id"]))
    else:
        episode = _open_episode(connection, file["id"])
    if episode is None:
        state = work(connection, file["id"])
        if not is_open(state):
            raise provenance.UploadBlocked("already_resolved")
        cancelled = _latest_cancelled(connection, file["id"])
        if cancelled is not None and _same_work(cancelled, state):
            raise provenance.UploadBlocked("owner_cancelled")
        if connection.execute(
                f"SELECT 1 FROM jobs WHERE file_id=%s AND status NOT IN {_TERMINAL_SQL}",
                (file["id"],)).fetchone():
            raise provenance.UploadBlocked("active_operation")
        # Open work no Job ended with (e.g. a scan-found block): its own episode.
        episode = _create(connection, state, timestamp, phase="pending",
                          reason=_blocking_reason(state), retry_count=0, due_at=timestamp)
    result = admit(connection, episode, current, stamp=timestamp, automatic=False, explicit=True)
    if not result:
        raise provenance.UploadBlocked(_refusal_reason(connection, episode["id"]))
    return result


def retry_vault(connection: Any, vault: dict[str, Any], user_id: int | None, stamp: Any) -> int:
    """Bounded explicit retry of the Vault's open episodes (same arbiter)."""
    rows = connection.execute(
        f"""SELECT r.* FROM upload_remediations r WHERE r.vault_id=%s AND r.closed_at IS NULL
            AND r.state IN ('pending', 'blocked', 'suspended') AND r.due_at <> 'infinity'
            AND COALESCE(r.reason, '') NOT IN ('proof_mismatch', 'expected_evidence_missing',
                                                'target_superseded')
            AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id=r.current_job_id
                            AND j.status NOT IN {_TERMINAL_SQL})
            ORDER BY r.updated_at, r.id LIMIT %s""", (vault["id"], TARGET_BATCH)).fetchall()
    return sum(bool(admit(connection, row, vault, stamp=stamp, automatic=False, explicit=True))
               for row in rows)


# -- the admission sweep -----------------------------------------------------------

_DUE_SQL = f"""r.closed_at IS NULL AND (
    (r.state IN ('pending', 'suspended') AND r.due_at <= %(now)s::timestamptz)
    OR (r.state IN ('queued', 'retrying') AND NOT EXISTS (
        SELECT 1 FROM jobs current WHERE current.id = r.current_job_id
          AND current.status NOT IN {_TERMINAL_SQL})))"""


def _settle_stale(connection: Any, row: dict[str, Any], stamp: Any) -> None:
    """Its Job ended outside a transition (restart, collection): catch up once."""
    job = connection.execute("SELECT * FROM jobs WHERE id=%s", (row["current_job_id"],)).fetchone() \
        if row["current_job_id"] else None
    if job is None:
        _update(connection, row, stamp, state="pending", reason=None, current_job_id=None,
                due_at=stamp)
    elif job["status"] == "cancelled":
        _update(connection, row, stamp, state="cancelled", reason="owner_cancelled")
    elif job["status"] == "failed":
        transition(connection, job["id"], "failed", stamp)
    elif job["status"] == "completed":
        if _closed_by(work(connection, row["file_id"]), job):
            _resolve(connection, row, job, stamp)
        else:
            _update(connection, row, stamp, state="pending", reason=None, due_at=stamp)


def _fair_vaults(connection: Any, name: str, stamp: Any) -> list[dict[str, Any]]:
    # Restore a cursor lost by a partial restore; never reset an existing one or
    # mistake another sweep's row lock for a missing row.
    connection.execute("INSERT INTO upload_remediation_sweep(name) VALUES (%s) ON CONFLICT DO NOTHING",
                       (name,))
    cursor = connection.execute(
        "SELECT * FROM upload_remediation_sweep WHERE name=%s FOR UPDATE SKIP LOCKED",
        (name,)).fetchone()
    if not cursor:
        return []
    rows = connection.execute(
        f"""SELECT v.id FROM vaults v WHERE EXISTS (
                SELECT 1 FROM upload_remediations r WHERE r.vault_id = v.id AND {_DUE_SQL})
            ORDER BY (v.id > %(last)s) DESC, v.id LIMIT %(limit)s""",
        {"now": stamp, "last": cursor["last_vault_id"], "limit": VAULT_BATCH}).fetchall()
    connection.execute("UPDATE upload_remediation_sweep SET last_tick_at=%s WHERE name=%s",
                       (stamp, name))
    return rows


def sweep(db: Any, stamp: Any = None) -> int:
    """Bounded round-robin over Vaults with due open episodes; DB only."""
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    count = 0
    for name in SWEEP_NAMES:
        deadline = monotonic() + 1.0
        with db() as connection:
            connection.execute("SET LOCAL lock_timeout='100ms'")
            connection.execute("SET LOCAL statement_timeout='2s'")
            vaults = _fair_vaults(connection, name, stamp)
        for candidate in vaults:
            if monotonic() >= deadline:
                break
            with db() as connection:
                connection.execute("UPDATE upload_remediation_sweep SET last_vault_id=%s WHERE name=%s",
                                   (candidate["id"], name))
            with db() as connection:
                connection.execute("SET LOCAL lock_timeout='100ms'")
                connection.execute("SET LOCAL statement_timeout='2s'")
                vault = connection.execute(
                    "SELECT * FROM vaults WHERE id=%s FOR NO KEY UPDATE SKIP LOCKED",
                    (candidate["id"],)).fetchone()
                if not vault:
                    continue
                rows = connection.execute(
                    f"""SELECT r.* FROM upload_remediations r WHERE r.vault_id=%(vault)s AND {_DUE_SQL}
                        ORDER BY r.due_at, r.id LIMIT %(limit)s""",
                    {"vault": vault["id"], "now": stamp, "limit": TARGET_BATCH}).fetchall()
                for row in rows:
                    if monotonic() >= deadline:
                        break
                    if row["state"] in {"queued", "retrying"}:
                        _settle_stale(connection, row, stamp)
                        continue
                    count += bool(admit(connection, row, vault, stamp=stamp))
    return count


# -- projections -------------------------------------------------------------------

def project(connection: Any, row: dict[str, Any]) -> dict[str, Any]:
    state = row["state"]
    void = row["closed_at"] is None and not is_open(work(connection, row["file_id"]))
    jobs = [r["job_id"] for r in connection.execute(
        "SELECT job_id FROM upload_remediation_jobs WHERE remediation_id=%s ORDER BY job_id LIMIT 50",
        (row["id"],)).fetchall()]
    return {
        "id": str(row["id"]), "state": state, "reason": row["reason"],
        "retry_count": row["retry_count"],
        "due_at": None if void or row["due_at"] is None else row["due_at"],
        "current_job_id": row["current_job_id"], "resolved_by_job_id": row["resolved_by_job_id"],
        "resolved_at": row["closed_at"] if state == "resolved" else None,
        "archive_version_id": str(row["archive_version_id"]) if row["archive_version_id"] else None,
        "can_resume": state in {"pending", "blocked", "suspended"}
            and row["reason"] not in _UNRESUMABLE and not void,
        "original_job_ids": [job for job in jobs if job != row["current_job_id"]
                             or state == "resolved" and job != row["resolved_by_job_id"]],
        "remedy": provenance.REMEDIES.get(
            row["reason"],
            "Keep copies. Correct the reported condition and retry exact reconciliation; "
            "no fresh upload is authorized."),
    }


def file_projection(connection: Any, vault_id: int, path: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT r.* FROM upload_remediations r JOIN files f ON f.id=r.file_id
           WHERE f.vault_id=%s AND f.path=%s ORDER BY r.created_at DESC, r.id DESC LIMIT 50""",
        (vault_id, path)).fetchall()
    return [project(connection, row) for row in rows]


def project_jobs(connection: Any, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not jobs:
        return jobs
    links = connection.execute(
        """SELECT b.job_id, r.* FROM upload_remediation_jobs b
           JOIN upload_remediations r ON r.id=b.remediation_id WHERE b.job_id=ANY(%s)""",
        ([row["id"] for row in jobs],)).fetchall()
    by_job = {r["job_id"]: project(connection, r) for r in links}
    return [{**row, "remediation": by_job.get(row["id"])} for row in jobs]


# -- health ------------------------------------------------------------------------

def sweep_status(connection: Any, current: datetime | None = None) -> dict[str, Any]:
    """Read durable heartbeats, never substitute the observer's own timestamp."""
    current = current or datetime.now(timezone.utc)
    ticks = {row["name"]: row["last_tick_at"] for row in connection.execute(
        "SELECT name, last_tick_at FROM upload_remediation_sweep").fetchall()}
    stamps = [ticks[name] for name in SWEEP_NAMES if ticks.get(name)]
    missing = any(not ticks.get(name) for name in SWEEP_NAMES)
    oldest = min(stamps) if stamps else None
    age = max(0.0, (current - oldest).total_seconds()) if oldest else None
    return {"available": True, "stale": missing or age > SWEEP_STALE_SECONDS,
            "missing_ticks": missing, "oldest_tick_age_seconds": age,
            "last_tick_unixtime": oldest.timestamp() if oldest and not missing else 0.0}


def sweep_health(db: Any) -> dict[str, Any]:
    try:
        with db() as connection:
            return sweep_status(connection)
    except Exception:
        return {"available": False, "stale": True}


def observe(db: Any) -> None:
    from . import health, metrics, notifications, upload_pipeline
    current = datetime.now(timezone.utc)
    with db() as connection:
        due = connection.execute(
            f"SELECT count(*) AS n, min(r.due_at) AS oldest FROM upload_remediations r "
            f"WHERE {_DUE_SQL}", {"now": current}).fetchone()
        states = connection.execute(
            "SELECT state, count(*) AS n FROM upload_remediations WHERE closed_at IS NULL "
            "GROUP BY state").fetchall()
        status = sweep_status(connection, current)
        journal = upload_pipeline.journal_status()
        notifications.set_problem(connection, key="upload_remediation_stale",
                                  event="upload_remediation_stale", active=status["stale"],
                                  email=False)
        notifications.set_problem(connection, key="upload_journal_unavailable",
                                  event="upload_journal_unavailable", active=journal["held"],
                                  email=False)
    health.set_upload_remediation_status(status)
    oldest = due["oldest"]
    metrics.set_gauge("upload_remediation_due_queue", due["n"])
    metrics.set_gauge("upload_remediation_due_oldest_age_seconds",
                      max(0.0, (current - oldest).total_seconds()) if oldest else 0.0)
    counts = {r["state"]: r["n"] for r in states}
    for state in ("pending", "queued", "retrying", "blocked", "suspended"):
        metrics.set_gauge("upload_remediation_states", counts.get(state, 0), state=state)
    metrics.set_gauge("upload_remediation_last_tick_unixtime", status["last_tick_unixtime"])
    metrics.set_gauge("upload_remediation_sweep_stale", float(status["stale"]))
    metrics.set_gauge("upload_journal_held", float(journal["held"]))
