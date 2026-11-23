"""Path-keyed aliases over ``tests.fixtures``, for files not yet migrated (#801).

A temporary path stands for a database here: an opaque identity, never a
file. New tests use ``tests.fixtures`` (``fresh_database()`` and friends);
this module goes away with the last file that still names a ``.db`` path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import psycopg

from tests import fixtures
from tests.fixtures import _server_dsn  # noqa: F401  (imported by tests)

_LOCK = fixtures._LOCK
_DATABASES: dict[str, tuple[fixtures.Database, Path]] = {}


def _reclaim_finished() -> None:
    # A path's database ends with its TemporaryDirectory: reclaim it rather
    # than accumulating hundreds of full catalogs on CI's disk until exit.
    for key, (database, parent) in list(_DATABASES.items()):
        if not parent.exists():
            database.drop()
            del _DATABASES[key]


def _key(identity: str | Path) -> str:
    return str(Path(identity).absolute())


def _database(identity: str | Path) -> fixtures.Database:
    key = _key(identity)
    with _LOCK:
        if key not in _DATABASES:
            _reclaim_finished()
            _DATABASES[key] = (fixtures.empty_database(), Path(key).parent)
        return _DATABASES[key][0]


def adopt(identity: Path, database: fixtures.Database) -> Path:
    """Let ``identity`` name a database a fixture already created."""
    with _LOCK:
        _DATABASES[_key(identity)] = (database, Path(_key(identity)).parent)
    return identity


def database_dsn(identity: str | Path) -> str:
    """The DSN of the path's database, created empty on first use."""
    return _database(identity).dsn


def PostgresConnection(identity: str | Path) -> psycopg.Connection:
    return _database(identity).connect()


def database_state(identity: str | Path) -> dict[str, list[dict]]:
    return _database(identity).rows()


def restore_database(identity: Path, content: bytes) -> None:
    _database(identity).restore_dump(content)


def run_alembic(identity: Path) -> subprocess.CompletedProcess[str]:
    """A fresh migrated database for a new path; real Alembic for a known one."""
    key = _key(identity)
    with _LOCK:
        if key in _DATABASES:
            return fixtures.alembic_upgrade_head(_DATABASES[key][0])
        _reclaim_finished()
        try:
            database = fixtures.fresh_database()
        except AssertionError as error:
            return subprocess.CompletedProcess(["alembic", "upgrade", "head"], 1, "", str(error))
        _DATABASES[key] = (database, Path(key).parent)
        return subprocess.CompletedProcess(["alembic", "upgrade", "head"], 0, "", "")
