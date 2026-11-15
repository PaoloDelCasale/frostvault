"""The displayed protection state follows the release gate's per-path rule (#684).

ADR-0042: an unreadable path or unsupported name leaves unconfirmed only that
path, its contents and the folders above it; global causes the whole Vault.
Browse, file detail, folder badges, Vault statistics and the weekly summary
read one shared rule. Disposable PostgreSQL 16 only.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.config import settings
from app.i18n import DEFAULT_LOCALE, translate
from app.services import directory_aggregates as aggregates
from app.services.component_health import record_component_health
from app.services.operation_policies import (
    SOURCE_SCAN_INCOMPLETE, UnconfirmedScope, blocking_path_for, local_release_blocking_paths,
    unconfirmed_scope,
)
from app.services.problems import report_problem
from app.services.source_layout import VaultLocalAccess
from tests.catalog_support import fingerprint, protect, seed_vault, sha256_hex
from tests.owner_fixtures import insert_owner
from tests.postgres import PostgresConnection, database_dsn
from tests.test_database import run_alembic

ONLINE = VaultLocalAccess(True, True, "source", "ok")
FILES = ("a.txt", "other/d.txt", "priv/b.txt", "priv/secret/c.txt")


class UnconfirmedScopeRuleTests(unittest.TestCase):
    def test_python_rule_matches_the_gate(self) -> None:
        scope = UnconfirmedScope(("priv/secret",))
        for path in ("", "priv", "priv/secret", "priv/secret/c.txt", "a.txt", "priv/b.txt",
                     "priv/secretx", "privx/secret"):
            with self.subTest(path=path):
                self.assertEqual(scope.covers(path),
                                 blocking_path_for(path, ("priv/secret",)) is not None)
        self.assertTrue(UnconfirmedScope(None).covers("a.txt"))
        self.assertFalse(UnconfirmedScope(()).covers(""))
        self.assertEqual(UnconfirmedScope(()).sql("f.path"), ("FALSE", []))
        self.assertEqual(UnconfirmedScope(None).sql("f.path"), ("TRUE", []))


class PathScopedDisplayedStateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "catalog.db"
        self.assertEqual(run_alembic(self.path).returncode, 0)
        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection)
            seed_vault(connection, source_root="/source")
            catalog = ArchiveCatalog(connection)
            for index, name in enumerate(FILES):
                payload = name.encode()
                catalog.observe_local_copy(
                    vault_id=2, path=name, file_type="regular", size=len(payload), mtime_ns=1,
                    observed_at="2026-11-15T10:00:00+00:00")
                fingerprint(catalog, 2, name, sha256_hex(payload))
                protect(catalog, 2, name, sha256_hex(payload), provider_version_id=f"v{index}")
            aggregates.rebuild_vault_directory_aggregates(connection, 2)
            record_component_health(connection, vault_id=2, component="source", ok=False,
                                    error=SOURCE_SCAN_INCOMPLETE)
            report_problem(connection, event="file_unreadable", vault_id=2, path="priv/secret")

    def scope(self, connection) -> UnconfirmedScope:
        return unconfirmed_scope(connection, 2, lifecycle="active", local_operations_allowed=True)

    def test_only_the_blocked_path_its_contents_and_ancestors_are_unconfirmed(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            scope = self.scope(connection)
            self.assertEqual(scope.paths, ("priv/secret",))
            catalog = ArchiveCatalog(connection, protection_unconfirmed=scope)
            summary = catalog.summary(2)
            root = {item["path"]: item for item in catalog.list_files_page(2)["items"]}
            priv = {item["path"]: item for item in catalog.list_files_page(2, directory="priv")["items"]}
            inside = catalog.get_file_item(2, "priv/secret/c.txt")
            outside = catalog.get_file_item(2, "priv/b.txt")
            unconfirmed_root = [item["path"] for item in
                                catalog.list_files_page(2, state="unconfirmed")["items"]]
            protected_root = [item["path"] for item in
                              catalog.list_files_page(2, state="protected")["items"]]
            protected_priv = [item["path"] for item in
                              catalog.list_files_page(2, directory="priv", state="protected")["items"]]
            unconfirmed_priv = [item["path"] for item in
                                catalog.list_files_page(2, directory="priv", state="unconfirmed")["items"]]
            searched = [item["path"] for item in
                        catalog.list_files_page(2, search="txt", state="protected")["items"]]
            searched_unconfirmed = [item["path"] for item in
                                    catalog.list_files_page(2, search="txt", state="unconfirmed")["items"]]
            # The gate and the displayed state read the same rule.
            with patch("app.services.source_layout.vault_local_access", return_value=ONLINE):
                self.assertEqual(local_release_blocking_paths(connection, 2), scope.paths)

        self.assertEqual(summary["states"], {"protected": 3, "unconfirmed": 1})
        self.assertEqual(summary["last_known_states"], {"protected": 4})
        self.assertEqual(summary["protection_state"], "unconfirmed")
        # Folders: the ancestor of the blocked path is unconfirmed, the others not.
        self.assertEqual(root["priv"]["state"], "unconfirmed")
        self.assertEqual(root["priv"]["available_actions"]["free-space"], 0)
        self.assertEqual(root["other"]["state"], "protected")
        self.assertEqual(root["a.txt"]["state"], "protected")
        self.assertTrue(root["a.txt"]["cleanup_eligible"])
        self.assertEqual(priv["priv/secret"]["state"], "unconfirmed")
        self.assertEqual(priv["priv/b.txt"]["state"], "protected")
        # Files: inside the blocked path unconfirmed with its last known state.
        self.assertEqual(inside["state"], "unconfirmed")
        self.assertEqual(inside["last_known_state"], "protected")
        self.assertFalse(inside["cleanup_eligible"])
        self.assertEqual(outside["state"], "protected")
        self.assertTrue(outside["cleanup_eligible"])
        # State filters agree with the badges.
        self.assertEqual(unconfirmed_root, ["priv"])
        self.assertEqual(protected_root, ["other", "a.txt"])
        self.assertEqual(protected_priv, ["priv/b.txt"])
        self.assertEqual(unconfirmed_priv, ["priv/secret"])
        self.assertEqual(searched, ["a.txt", "other/d.txt", "priv/b.txt"])
        self.assertEqual(searched_unconfirmed, ["priv/secret/c.txt"])

    def test_global_causes_keep_the_whole_vault_unconfirmed(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            record_component_health(connection, vault_id=2, component="cloud", ok=False,
                                    error="listing failed")
            scope = self.scope(connection)
            catalog = ArchiveCatalog(connection, protection_unconfirmed=scope)
            summary = catalog.summary(2)
            item = catalog.get_file_item(2, "a.txt")
            self.assertTrue(scope.whole_vault)
            # Resolving the path Problem while the scan is incomplete fails closed.
            record_component_health(connection, vault_id=2, component="cloud", ok=True)
            connection.execute("UPDATE notifications SET problem_status='resolved', resolved_at=now() "
                               "WHERE event='file_unreadable'")
            self.assertTrue(self.scope(connection).whole_vault)
            self.assertTrue(unconfirmed_scope(connection, 2, lifecycle="reconnect_required",
                                              local_operations_allowed=True).whole_vault)
            self.assertTrue(unconfirmed_scope(connection, 2, lifecycle="active",
                                              local_operations_allowed=False).whole_vault)
        self.assertEqual(summary["states"], {"unconfirmed": 4})
        self.assertEqual(item["state"], "unconfirmed")

    def test_stats_say_how_many_paths_are_unreadable(self) -> None:
        from app.main import stats

        cfg = replace(settings, database_dsn=database_dsn(str(self.path)))
        with (
            patch("app.database.settings", cfg),
            patch("app.main.settings", cfg),
            patch("app.main.vault_local_access", return_value=ONLINE),
        ):
            payload = stats({"id": 2, "source_root": "/source"})
        self.assertEqual(payload["states"], {"protected": 3, "unconfirmed": 1})
        self.assertEqual(payload["runtime"]["unreadable_paths"], 1)
        self.assertEqual(payload["runtime"]["last_error"],
                         translate("vault.health.unreadable_paths", locale=DEFAULT_LOCALE, count=1))
        self.assertEqual(translate("vault.health.unreadable_paths", locale="it", count=2)[:30],
                         "2 percorsi non osservati (non ")

    def test_weekly_summary_counts_by_path(self) -> None:
        from app.services import protection_notifications as monitor

        with (
            PostgresConnection(str(self.path)) as connection,
            patch("app.services.source_layout.vault_local_access", return_value=ONLINE),
            patch("app.services.metadata_backups._last_verification_attempt_at", None),
        ):
            summary = monitor.weekly_summary(connection, now=datetime(2026, 11, 16, tzinfo=timezone.utc))
        states = summary["vaults"][0]["states"]
        self.assertEqual((states["protected"], states["unconfirmed"]), (3, 1))


if __name__ == "__main__":
    unittest.main()
