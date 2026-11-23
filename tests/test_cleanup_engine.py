"""#799 The daily run shared by both cleanups (ADR-0048, ADR-0052).

One contract, run against Automatic Duplicate Cleanup and Mirror Cleanup on a
disposable PostgreSQL 16 database (fixtures of ``tests.test_mirror_cleanup``):
when a Vault is due, what a run records, the capped Problem, the pending
deletions and the statements a run sends for its own bookkeeping. Where the two
cleanups differ, each subclass states its side.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from app.services import cloud_deletion, duplicate_cleanup, mirror_cleanup
from tests.test_mirror_cleanup import MirrorCleanupFixture, utcnow

FIRST = b"first candidate of 799"
SECOND = b"second candidate of 799, longer"
LOCK = ("SELECT v.*, t.bucket AS s3_bucket, t.region AS s3_region, t.key_layout AS s3_key_layout, "
        "t.retention_lock_days AS s3_retention_lock_days FROM vaults v "
        "JOIN vault_targets t ON t.id = v.target_id WHERE v.id=%s FOR UPDATE OF v")
WAITS = {"waiting_count": 0, "waiting_bytes": 0, "unseen_count": 0}
STATUS_KEYS = ["grace_days_min", "grace_days_max", "last_run_at", "next_run_at", "last_outcome",
               "current", "ready", "waiting", "unseen", "held", "minimum_storage", "retention_lock",
               "totals", "pending"]


def flat(sql: object) -> str:
    return " ".join(str(sql).split())


class EngineContract:
    """What both cleanups do alike; the subclasses name the cleanup."""

    name: str
    module: object
    other: object
    mirror: bool
    settings_keys: list[str]
    pending_filter: str

    # -- helpers -------------------------------------------------------------

    def candidate(self, name: str, payload: bytes) -> str:
        raise NotImplementedError

    def enable(self, **values) -> dict:
        with self.db() as connection:
            return self.module.update_settings(
                connection, vault_id=2, actor="owner", values={"enabled": True, **values})

    def run_once(self, now: datetime) -> dict | None:
        with self.db() as connection:
            return self.module.run_vault(connection, 2, now=now)

    def cleanup_status(self) -> dict:
        with self.db() as connection:
            return self.module.status(connection, 2)

    def capped_problem(self) -> dict | None:
        rows = self.rows("SELECT event, problem_status, message_params FROM notifications WHERE dedupe_key=%s",
                         self.module.problem_key(2))
        return rows[0] if rows else None

    def recorded(self) -> dict:
        [row] = self.rows(f"SELECT {self.name}_last_run_at AS at, {self.name}_last_outcome AS outcome "
                          "FROM vaults WHERE id=2")
        return row

    def audit(self, event: str) -> list[dict]:
        return self.rows("SELECT actor, outcome, vault_id, detail FROM audit_events WHERE event=%s ORDER BY id",
                         f"{self.name}.{event}")

    def statements(self, call) -> tuple[object, list[str]]:
        """What ``call`` returns and every statement it sends, whitespace folded."""
        sent: list[str] = []
        execute = psycopg.Cursor.execute

        def recording(cursor, query, *args, **kwargs):
            sent.append(flat(query))
            return execute(cursor, query, *args, **kwargs)
        with self.db() as connection, patch.object(psycopg.Cursor, "execute", recording):
            result = call(connection)
        return result, sent

    def own(self, sent: list[str]) -> list[str]:
        """The statements that read or write this cleanup's own state."""
        return [sql for sql in sent if f"{self.name}_" in sql or "cloud_purge_requests r ON" in sql]

    @property
    def due_sql(self) -> str:
        return (f"SELECT id FROM vaults WHERE {self.name}_enabled AND lifecycle='active' AND NOT paused "
                f"AND root_released_at IS NULL AND ({self.name}_last_run_at IS NULL "
                f"OR {self.name}_last_run_at <= %s::timestamptz) ORDER BY id")

    @property
    def finish_sql(self) -> str:
        return f"UPDATE vaults SET {self.name}_last_run_at=%s, {self.name}_last_outcome=%s WHERE id=%s"

    @property
    def pending_sql(self) -> str:
        return ("SELECT j.group_id, count(*) AS file_count, COALESCE(sum(j.total_bytes), 0) AS byte_count, "
                "min(j.run_after) AS run_after, bool_and(j.status = 'pending_delay') AS cancellable "
                "FROM jobs j JOIN cloud_purge_requests r ON r.group_id = j.group_id "
                f"WHERE j.vault_id=%s AND r.automatic AND {self.pending_filter} AND j.action='cloud-purge' "
                "AND j.status NOT IN ('completed', 'failed', 'cancelled') "
                "GROUP BY j.group_id ORDER BY min(j.run_after), j.group_id")

    # -- when a Vault is due -------------------------------------------------

    def test_a_vault_with_the_cleanup_off_is_never_due_and_costs_one_statement(self) -> None:
        self.candidate("a.bin", FIRST)
        now = utcnow()
        due, sent = self.statements(lambda c: self.module.due_vaults(c, now=now))
        self.assertEqual((due, sent), ([], [self.due_sql]))
        outcome, sent = self.statements(lambda c: self.module.run_vault(c, 2, now=now))
        self.assertIsNone(outcome)
        self.assertEqual(sent, [LOCK])
        self.assertEqual(self.recorded(), {"at": None, "outcome": None})

    def test_a_vault_is_due_again_exactly_one_day_after_its_last_run(self) -> None:
        self.enable()
        now = utcnow()
        self.assertEqual(self.run_once(now)["outcome"], "none")
        almost = now + timedelta(days=1) - timedelta(seconds=1)
        with self.db() as connection:
            self.assertEqual(self.module.due_vaults(connection, now=almost), [])
            self.assertEqual(self.module.due_vaults(connection, now=now + timedelta(days=1)), [2])
        outcome, sent = self.statements(lambda c: self.module.run_vault(c, 2, now=almost))
        self.assertIsNone(outcome)
        self.assertEqual(sent, [LOCK])
        self.assertEqual(self.recorded()["at"], now, "a run that is not due records nothing")
        self.assertEqual(self.run_once(now + timedelta(days=1))["outcome"], "none")

    # -- what a run records --------------------------------------------------

    def test_a_run_without_candidates_records_none_and_opens_no_problem(self) -> None:
        self.enable()
        now = utcnow()
        outcome, sent = self.statements(lambda c: self.module.run_vault(c, 2, now=now))
        expected = {"outcome": "none", "file_count": 0, "byte_count": 0, **WAITS, "at": now.isoformat()}
        self.assertEqual(outcome, expected)
        self.assertEqual(list(outcome), list(expected), "the keys keep their order")
        self.assertEqual(self.recorded(), {"at": now, "outcome": expected})
        self.assertEqual((sent[0], self.own(sent)), (LOCK, [self.finish_sql]))
        self.assertIsNone(self.capped_problem())
        self.assertEqual(self.audit("capped") + self.audit("skipped"), [])

    def test_a_run_over_the_cap_schedules_nothing_and_opens_the_problem(self) -> None:
        self.candidate("a.bin", FIRST)
        self.candidate("b.bin", SECOND)
        size = len(FIRST) + len(SECOND)
        self.enable(cap_files=1)
        now = utcnow()
        outcome, sent = self.statements(lambda c: self.module.run_vault(c, 2, now=now))
        expected = {"outcome": "capped", "file_count": 2, "byte_count": size, **WAITS, "at": now.isoformat()}
        self.assertEqual(outcome, expected)
        self.assertEqual(list(outcome), list(expected))
        self.assertEqual(self.recorded(), {"at": now, "outcome": expected})
        self.assertEqual(self.own(sent), [self.finish_sql])
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)
        self.assertEqual(self.capped_problem(), {
            "event": f"{self.name}_capped", "problem_status": "open",
            "message_params": {"count": 2, "byte_count": size, "cap_files": 1,
                               "cap_bytes": 50 * 1024 ** 3, self.name: True}})
        [event] = self.audit("capped")
        self.assertEqual(event, {"actor": "system", "outcome": "capped", "vault_id": 2, "detail": {
            "file_count": 2, "byte_count": size, "cap_files": 1, "cap_bytes": 50 * 1024 ** 3}})

    def test_the_bytes_cap_holds_alone_and_equal_to_the_cap_is_under_it(self) -> None:
        self.candidate("a.bin", FIRST)
        self.enable(cap_files=1, cap_bytes=len(FIRST) - 1)
        now = utcnow()
        self.assertEqual(self.run_once(now)["outcome"], "capped")
        self.enable(cap_bytes=len(FIRST))
        self.assertEqual(self.run_once(now + timedelta(days=1))["outcome"], "scheduled")

    def test_a_scheduled_run_closes_the_problem_and_announces_one_deletion(self) -> None:
        self.candidate("a.bin", FIRST)
        self.candidate("b.bin", SECOND)
        size = len(FIRST) + len(SECOND)
        self.enable(cap_files=1)
        now = utcnow()
        self.assertEqual(self.run_once(now)["outcome"], "capped")
        self.enable(cap_files=2)
        later = now + timedelta(days=1)
        outcome, sent = self.statements(lambda c: self.module.run_vault(c, 2, now=later))
        self.assertEqual(list(outcome), ["outcome", "file_count", "byte_count", "group_id", "run_after",
                                         "waiting_count", "waiting_bytes", "unseen_count", "at"])
        group = outcome["group_id"]
        self.assertEqual(outcome, {"outcome": "scheduled", "file_count": 2, "byte_count": size,
                                   "group_id": group, "run_after": outcome["run_after"], **WAITS,
                                   "at": later.isoformat()})
        self.assertEqual(self.recorded(), {"at": later, "outcome": outcome})
        self.assertEqual(self.own(sent), [self.finish_sql])
        self.assertEqual(self.capped_problem()["problem_status"], "resolved")
        [request] = self.rows("SELECT automatic, mirror_cleanup, reason FROM cloud_purge_requests WHERE group_id=%s",
                              group)
        self.assertEqual((request["automatic"], request["mirror_cleanup"]), (True, self.mirror))
        self.assertEqual(request["reason"], self.reason)
        [notice] = self.rows("SELECT event, title_key, body_key, message_params FROM notifications WHERE dedupe_key=%s",
                             f"{self.name}_scheduled:{group}")
        self.assertEqual(notice, {
            "event": f"{self.name}_scheduled", "title_key": f"notification.{self.name}_scheduled.title",
            "body_key": f"notification.{self.name}_scheduled.body",
            "message_params": {"count": 2, "byte_count": size, "executes_at": outcome["run_after"],
                               "group_id": group, self.name: True}})

        status, sent = self.statements(lambda c: self.module.status(c, 2))
        self.assertEqual(status["pending"], [{"group_id": group, "file_count": 2, "byte_count": size,
                                              "run_after": outcome["run_after"], "cancellable": True}])
        self.assertEqual(self.own(sent), [self.pending_sql])
        with self.db() as connection:
            self.assertEqual(self.other.status(connection, 2)["pending"], [], "each cleanup lists its own")

    def test_a_run_that_can_prove_nothing_is_skipped_and_leaves_the_problem_alone(self) -> None:
        self.candidate("a.bin", FIRST)
        self.candidate("b.bin", SECOND)
        self.enable(cap_files=1)
        now = utcnow()
        self.assertEqual(self.run_once(now)["outcome"], "capped")
        later = now + timedelta(days=1)
        with patch("app.services.source_layout.vault_local_access",
                   return_value=SimpleNamespace(local_operations_allowed=False)):
            outcome, sent = self.statements(lambda c: self.module.run_vault(c, 2, now=later))
        self.assertEqual(outcome, {"outcome": "skipped", "at": later.isoformat()})
        self.assertEqual(self.recorded(), {"at": later, "outcome": outcome})
        self.assertEqual(self.own(sent), [self.finish_sql])
        self.assertEqual(self.capped_problem()["problem_status"], "open", "a skipped run does not close it")
        [event] = self.audit("skipped")
        self.assertEqual((event["actor"], event["outcome"], event["vault_id"], list(event["detail"])),
                         ("system", "skipped", 2, ["reason"]))
        self.assertTrue(event["detail"]["reason"])

    def test_the_totals_are_read_from_the_vault(self) -> None:
        self.assertIsNone(self.cleanup_status()["totals"])
        stamp = utcnow() - timedelta(hours=3)
        with self.db() as connection:
            connection.execute(
                f"UPDATE vaults SET {self.name}_deleted_count=3, {self.name}_deleted_bytes=77, "
                f"{self.name}_last_deleted_at=%s WHERE id=2", (stamp,))
        self.assertEqual(self.cleanup_status()["totals"],
                         {"deleted_count": 3, "deleted_bytes": 77, "last_deleted_at": stamp.isoformat()})
        with self.db() as connection:
            self.assertIsNone(self.other.status(connection, 2)["totals"])

    # -- the status ----------------------------------------------------------

    def test_the_status_keeps_its_shape_and_its_next_run(self) -> None:
        status = self.cleanup_status()
        self.assertEqual(list(status), self.settings_keys + self.status_keys)
        self.assertEqual((status["last_run_at"], status["next_run_at"], status["last_outcome"]),
                         (None, None, None))
        self.assertIsNone(status["waiting"]["earliest_deletion_at"], "off: nothing is ever deleted")
        self.enable()
        now = utcnow()
        outcome = self.run_once(now)
        status = self.cleanup_status()
        self.assertEqual((status["last_run_at"], status["next_run_at"], status["last_outcome"]),
                         (now.isoformat(), (now + timedelta(days=1)).isoformat(), outcome))
        with self.db() as connection:
            self.module.update_settings(connection, vault_id=2, actor="owner", values={"enabled": False})
        status = self.cleanup_status()
        self.assertEqual((status["last_run_at"], status["next_run_at"]), (now.isoformat(), None))
        with self.db() as connection, self.assertRaises(LookupError):
            self.module.status(connection, 999)

    # -- every due Vault, each in its own transaction -----------------------

    def second_vault(self) -> int:
        with self.db() as connection:
            row = connection.execute(
                """INSERT INTO vaults(name, source_root, target_id)
                   SELECT 'Second 799', source_root || '-799', target_id FROM vaults WHERE id=2
                   RETURNING id""").fetchone()
            connection.execute(
                f"UPDATE vaults SET {self.name}_enabled=true WHERE id=%s", (row["id"],))
        return int(row["id"])

    def counting_db(self) -> tuple[object, list[int]]:
        opened: list[int] = []

        def database():
            opened.append(1)
            return self.db()
        return database, opened

    def test_run_due_opens_one_transaction_to_list_and_one_per_due_vault(self) -> None:
        database, opened = self.counting_db()
        self.assertEqual(self.module.run_due(database), 0)
        self.assertEqual(len(opened), 1, "nothing due: only the listing")
        self.enable()
        del opened[:]
        now = utcnow().replace(microsecond=123456)
        self.assertEqual(self.module.run_due(database, now=now), 1)
        self.assertEqual(len(opened), 2)
        self.assertEqual(self.recorded()["at"], now.replace(microsecond=0), "whole seconds")
        del opened[:]
        self.assertEqual(self.module.run_due(database, now=now + timedelta(hours=1)), 0)
        self.assertEqual(len(opened), 1)

    def test_run_due_tries_every_vault_and_raises_the_first_error_afterwards(self) -> None:
        self.enable()
        second = self.second_vault()
        lock = cloud_deletion.lock_vault

        def failing(connection, vault_id):
            if vault_id == 2:
                raise RuntimeError("first of 799")
            return lock(connection, vault_id)
        database, opened = self.counting_db()
        now = utcnow()
        with patch.object(cloud_deletion, "lock_vault", failing), self.assertRaises(RuntimeError) as error:
            self.module.run_due(database, now=now)
        self.assertEqual(str(error.exception), "first of 799")
        self.assertEqual(len(opened), 3)
        self.assertIsNone(self.recorded()["at"], "the failing Vault is tried again at the next poll")
        [other] = self.rows(f"SELECT {self.name}_last_run_at AS at FROM vaults WHERE id=%s", second)
        self.assertEqual(other["at"], now, "the other Vault ran")
        with self.db() as connection:
            self.assertEqual(self.module.due_vaults(connection, now=now), [2])

    # -- the settings --------------------------------------------------------

    def test_the_settings_change_one_row_and_are_audited_only_when_they_change(self) -> None:
        self.enable(grace_days=3)
        [event] = self.audit("settings_updated")
        self.assertEqual((event["actor"], event["detail"]["before"]["grace_days"],
                          event["detail"]["after"]["grace_days"]), ("owner", 7, 3))
        self.assertEqual(list(self.enable(grace_days=3))[:len(self.settings_keys)], self.settings_keys)
        self.assertEqual(len(self.audit("settings_updated")), 1, "an unchanged write is not audited")
        with self.db() as connection, self.assertRaises(self.error) as unknown:
            self.module.update_settings(connection, vault_id=2, actor="owner", values={"surprise": 1})
        self.assertEqual((str(unknown.exception), unknown.exception.code),
                         ("Unknown settings: ['surprise']", "invalid"))
        with self.db() as connection, self.assertRaises(cloud_deletion.CloudDeletionError):
            self.module.update_settings(connection, vault_id=999, actor="owner", values={})

    def test_each_invalid_setting_names_its_field(self) -> None:
        low, high = self.grace_range
        cases = [({"enabled": "yes"}, "enabled must be true or false", "invalid"),
                 ({"beyond_minimum": 1}, "beyond_minimum must be true or false", "invalid"),
                 ({"grace_days": low - 1}, f"grace_days must be between {low} and {high}", "grace_days"),
                 ({"grace_days": high + 1}, f"grace_days must be between {low} and {high}", "grace_days"),
                 ({"grace_days": True}, f"grace_days must be between {low} and {high}", "grace_days"),
                 ({"cap_files": 1_000_001}, "cap_files must be between 1 and 1000000", "cap_files"),
                 ({"cap_bytes": 1024 ** 5 + 1}, f"cap_bytes must be between 1 and {1024 ** 5}", "cap_bytes")]
        for values, message, code in cases:
            with self.subTest(values=values), self.db() as connection, self.assertRaises(self.error) as error:
                self.module.update_settings(connection, vault_id=2, actor="owner", values=values)
            self.assertEqual((str(error.exception), error.exception.code), (message, code))
        self.assertEqual(self.enable(grace_days=high, cap_files=1_000_000, cap_bytes=1024 ** 5)["grace_days"], high)


class DuplicateCleanupEngineTests(EngineContract, MirrorCleanupFixture):
    name = "duplicate_cleanup"
    module = duplicate_cleanup
    other = mirror_cleanup
    error = duplicate_cleanup.DuplicateCleanupError
    mirror = False
    grace_range = (1, 90)
    reason = "Automatic cleanup of cloud duplicates"
    settings_keys = ["enabled", "grace_days", "cap_files", "cap_bytes", "beyond_minimum"]
    status_keys = STATUS_KEYS
    pending_filter = "NOT r.mirror_cleanup"

    def candidate(self, name: str, payload: bytes) -> str:
        return self.moved(name, payload)

    def test_two_invalid_settings_report_the_wait_before_beyond_minimum(self) -> None:
        with self.db() as connection, self.assertRaises(self.error) as error:
            self.module.update_settings(connection, vault_id=2, actor="owner",
                                        values={"beyond_minimum": 1, "grace_days": 0})
        self.assertEqual(error.exception.code, "grace_days")


class MirrorCleanupEngineTests(EngineContract, MirrorCleanupFixture):
    name = "mirror_cleanup"
    module = mirror_cleanup
    other = duplicate_cleanup
    error = mirror_cleanup.MirrorCleanupError
    mirror = True
    grace_range = (1, 180)
    reason = "Cloud deletion of files gone from the server"
    settings_keys = ["enabled", "grace_days", "cap_files", "cap_bytes", "folders", "beyond_minimum"]
    status_keys = STATUS_KEYS[:5] + ["cloud_life"] + STATUS_KEYS[5:]
    pending_filter = "r.mirror_cleanup"

    def candidate(self, name: str, payload: bytes) -> str:
        return self.gone(f"backup/{name}", payload)

    def test_two_invalid_settings_report_beyond_minimum_before_the_wait(self) -> None:
        with self.db() as connection, self.assertRaises(self.error) as error:
            self.module.update_settings(connection, vault_id=2, actor="owner",
                                        values={"beyond_minimum": 1, "grace_days": 0})
        self.assertEqual((str(error.exception), error.exception.code),
                         ("beyond_minimum must be true or false", "invalid"))

    def test_a_loosening_change_asks_for_the_fresh_authentication_before_any_write(self) -> None:
        asked: list[int] = []
        with self.db() as connection:
            self.module.update_settings(connection, vault_id=2, actor="owner", values={"enabled": True},
                                        require_reauth=lambda: asked.append(1))
            self.module.update_settings(connection, vault_id=2, actor="owner", values={"grace_days": 30},
                                        require_reauth=lambda: asked.append(2))
        self.assertEqual(asked, [1], "turning it on loosens; a longer wait does not")

        def refuse() -> None:
            raise PermissionError("fresh authentication")
        with self.assertRaises(PermissionError), self.db() as connection:
            self.module.update_settings(connection, vault_id=2, actor="owner", values={"grace_days": 1},
                                        require_reauth=refuse)
        self.assertEqual(self.cleanup_status()["grace_days"], 30)
