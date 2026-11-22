"""The fixture module keeps its promises: isolation, restore, one Owner (#801)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import psycopg

from app import config, database, main, storage
from app.schema_guard import SCHEMA_HEAD
from tests import fixtures, postgres
from tests.fixtures import (
    FrostVaultTestCase,
    alembic_upgrade_head,
    create_owner,
    empty_database,
    fresh_database,
    override_settings,
    seed_vault,
)


def _database_exists(name: str) -> bool:
    with psycopg.connect(fixtures._server_dsn(), autocommit=True) as connection:
        return connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
        ).fetchone() is not None


def _revision(database_: fixtures.Database) -> str:
    with database_.connect() as connection:
        return connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]


class OverrideSettingsTests(unittest.TestCase):
    def test_every_module_sees_the_override_and_exit_restores_it(self) -> None:
        before = config.settings.restore_days
        with override_settings(restore_days=before + 7) as overridden:
            self.assertIs(overridden, config.settings)
            for module in (config, database, main, storage):
                self.assertEqual(module.settings.restore_days, before + 7)
            self.assertEqual(main._runtime_settings().restore_days, before + 7)
            self.assertEqual(storage._runtime_settings().restore_days, before + 7)
        self.assertEqual(config.settings.restore_days, before)
        self.assertIs(main.settings, config.settings)

    def test_exit_restores_after_an_error(self) -> None:
        before = config.settings.cookie_secure
        with self.assertRaises(ZeroDivisionError):
            with override_settings(cookie_secure=not before):
                1 / 0
        self.assertEqual(config.settings.cookie_secure, before)

    def test_nested_overrides_restore_in_order(self) -> None:
        before = config.settings.restore_tier
        with override_settings(restore_tier="outer"):
            with override_settings(restore_tier="inner", restore_days=99):
                self.assertEqual(config.settings.restore_tier, "inner")
            self.assertEqual(config.settings.restore_tier, "outer")
        self.assertEqual(config.settings.restore_tier, before)

    def test_unknown_field_is_refused_before_any_change(self) -> None:
        before = config.settings.restore_days
        with self.assertRaisesRegex(TypeError, "no_such_setting"):
            with override_settings(restore_days=before + 1, no_such_setting=1):
                self.fail("the override must not start")
        self.assertEqual(config.settings.restore_days, before)

    def test_leaving_a_database_closes_its_pool(self) -> None:
        with fresh_database() as database_:
            with override_settings(database_dsn=database_.dsn):
                with database.db() as connection:
                    connection.execute("SELECT 1")
                self.assertIn(database_.dsn, database._pools)
            self.assertEqual(database._pools, {})

    def test_cleanup_restores_when_set_up_fails_halfway(self) -> None:
        seen: dict[str, object] = {}

        class FailingSetUp(FrostVaultTestCase):
            settings_overrides = {"restore_tier": "halfway"}

            def setUp(self) -> None:
                super().setUp()
                seen["tier"] = config.settings.restore_tier
                seen["database"] = self.database
                raise RuntimeError("setUp fails after the fixtures")

            def test_never_runs(self) -> None:
                seen["ran"] = True

        before = (config.settings.restore_tier, config.settings.database_dsn)
        result = unittest.TestResult()
        FailingSetUp("test_never_runs").run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertNotIn("ran", seen)
        self.assertEqual(seen["tier"], "halfway")
        self.assertEqual((config.settings.restore_tier, config.settings.database_dsn), before)
        self.assertFalse(_database_exists(seen["database"].name))


class DatabaseFixtureTests(unittest.TestCase):
    def test_importing_the_fixtures_does_not_read_the_application_settings(self) -> None:
        # The e2e seed script sets DATABASE_URL from a fixture database first.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys, tests.fixtures, tests.postgres; "
             "sys.exit(any(name == 'app' or name.startswith('app.') for name in sys.modules))"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_fresh_database_is_at_the_schema_head_and_isolated(self) -> None:
        with fresh_database() as first, fresh_database() as second:
            self.assertNotEqual(first.name, second.name)
            self.assertEqual(_revision(first), SCHEMA_HEAD)
            with first.connect() as connection:
                create_owner(connection)
            with second.connect() as connection:
                self.assertIsNone(connection.execute("SELECT 1 FROM owner").fetchone())
            self.assertEqual(len(first.rows()["owner"]), 1)
        self.assertFalse(_database_exists(first.name))
        self.assertFalse(_database_exists(second.name))

    def test_empty_database_has_no_schema_until_alembic_really_runs(self) -> None:
        with empty_database() as database_:
            self.assertEqual(database_.rows(), {})
            upgraded = alembic_upgrade_head(database_)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertEqual(_revision(database_), SCHEMA_HEAD)

    def test_path_aliases_keep_one_database_per_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            migrated = postgres.run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertEqual(postgres.database_dsn(path), postgres.database_dsn(str(path)))
            self.assertNotEqual(
                postgres.database_dsn(path), postgres.database_dsn(Path(directory) / "other.db")
            )
            with postgres.PostgresConnection(path) as connection:
                self.assertIsNone(connection.execute("SELECT 1 FROM owner").fetchone())
            self.assertEqual(postgres.database_state(Path(directory) / "other.db"), {})


class OwnerAndVaultTests(FrostVaultTestCase):
    owner = {"display_name": "Alice"}

    def test_set_up_gives_a_database_an_owner_and_settings(self) -> None:
        self.assertIs(self.settings, config.settings)
        self.assertEqual(config.settings.database_dsn, self.database.dsn)
        with database.db() as connection:
            owner = connection.execute("SELECT id, display_name FROM owner").fetchall()
        self.assertEqual(owner, [{"id": 1, "display_name": "Alice"}])

    def test_seed_vault_returns_the_row_and_shares_the_target(self) -> None:
        with self.database.connect() as connection:
            first = seed_vault(connection)
            second = seed_vault(connection, id=9, name="Photos", source_root="/sources/photos",
                                paused=True)
            other = seed_vault(connection, name="Other", source_root="/sources/other",
                               bucket="other-bucket")
        self.assertEqual((first["name"], first["source_root"]), ("Docs", "/sources/managed/docs"))
        self.assertEqual((second["id"], second["target_id"]), (9, first["target_id"]))
        self.assertIs(second["paused"], True)
        self.assertNotEqual(other["target_id"], first["target_id"])

    def test_sign_in_is_the_owner_session_of_the_client(self) -> None:
        self.client = self.http_client()
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        signed_in = self.sign_in()
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["csrf_token"], signed_in.csrf_token)
        self.assertEqual(signed_in.csrf_headers, {"X-CSRF-Token": signed_in.csrf_token})


if __name__ == "__main__":
    unittest.main()
