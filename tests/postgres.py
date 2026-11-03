"""Disposable PostgreSQL fixtures, never a dialect adapter.

TEST_POSTGRES_URL must name a *test server* with a CREATEDB role. Every fixture
uses its own uniquely named database; the configured database is never mutated.
Paths are opaque fixture identities, not database files. HEAD fixtures clone a
real Alembic-migrated template once per process. Pinned/populated upgrades and
all downgrades always run the actual migration graph.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

_PREFIX = "fvtest_" + uuid.uuid4().hex[:12] + "_"
_LOCK = threading.RLock()
_DATABASES: dict[str, tuple[str, Path | None]] = {}
_TEMPLATE: str | None = None
_ROOT = Path(__file__).resolve().parents[1]


def _server_dsn() -> str:
    value = os.getenv("TEST_POSTGRES_URL", "")
    if not value:
        raise RuntimeError(
            "TEST_POSTGRES_URL is required: use an isolated PostgreSQL 16 test "
            "server with a CREATEDB role (AGENTS.md). Database tests never skip."
        )
    value = value.replace("postgresql+psycopg://", "postgresql://", 1)
    conninfo_to_dict(value)
    return value


def _dsn(name: str) -> str:
    return make_conninfo(
        _server_dsn(), dbname=name,
        options="-c lock_timeout=15000 -c statement_timeout=120000",
        connect_timeout=10,
    )


def _drop(name: str) -> None:
    assert name.startswith(_PREFIX), "refusing to drop a non-fixture database"
    with psycopg.connect(_server_dsn(), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _cleanup() -> None:
    for name, _ in list(_DATABASES.values()):
        try:
            _drop(name)
        except psycopg.Error:
            pass


atexit.register(_cleanup)


def _create(key: str, parent: Path | None, template: str | None = None) -> str:
    # Reclaim completed TemporaryDirectory fixtures rather than accumulating
    # hundreds of full catalogs on CI's disk until process exit.
    for old_key, (name, old_parent) in list(_DATABASES.items()):
        if old_parent is not None and not old_parent.exists():
            _drop(name)
            del _DATABASES[old_key]
    name = _PREFIX + hashlib.sha256(key.encode()).hexdigest()[:20]
    with psycopg.connect(_server_dsn(), autocommit=True) as connection:
        statement = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(template or "template0")
        )
        connection.execute(statement)
    _DATABASES[key] = (name, parent)
    return name


def database_dsn(identity: str | Path) -> str:
    """Return/create one empty fixture database identified by a temporary path."""
    key = str(Path(identity).absolute())
    with _LOCK:
        if key not in _DATABASES:
            _create(key, Path(key).parent)
        return _dsn(_DATABASES[key][0])


def PostgresConnection(identity: str | Path) -> psycopg.Connection:
    """A raw psycopg connection: no SQL translation or SQLite compatibility."""
    return psycopg.connect(database_dsn(identity), row_factory=dict_row)


def database_state(identity: str | Path) -> dict[str, list[dict]]:
    """Snapshot all metadata rows to prove isolated restore never changes live state."""
    with PostgresConnection(identity) as connection:
        names = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()
        return {
            row["table_name"]: sorted(connection.execute(
                sql.SQL("SELECT * FROM {}").format(sql.Identifier(row["table_name"]))
            ).fetchall(), key=repr)
            for row in names
        }


def restore_database(identity: Path, content: bytes) -> None:
    """Restore a real custom-format pg_dump into another disposable database."""
    import tempfile
    from app.services.metadata_backups import _postgres_env
    dsn = database_dsn(identity)
    with tempfile.TemporaryDirectory() as directory:
        dump = Path(directory) / "restore.dump"
        dump.write_bytes(content)
        result = subprocess.run(
            ["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--dbname",
             conninfo_to_dict(dsn)["dbname"], str(dump)],
            env=_postgres_env(dsn), capture_output=True, check=False,
        )
        if result.returncode:
            raise AssertionError("Isolated PostgreSQL restore failed: " + result.stderr.decode())


def _alembic(dsn: str, revision: str, command: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    return subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


def run_alembic(identity: Path, revision: str = "head", command: str = "upgrade") -> subprocess.CompletedProcess[str]:
    global _TEMPLATE
    key = str(Path(identity).absolute())
    with _LOCK:
        if key not in _DATABASES and command == "upgrade" and revision == "head":
            if _TEMPLATE is None:
                name = _create("head-template", None)
                result = _alembic(_dsn(name), "head", "upgrade")
                if result.returncode:
                    return result
                _TEMPLATE = name
            _create(key, Path(key).parent, _TEMPLATE)
            return subprocess.CompletedProcess(["alembic", "upgrade", "head"], 0, "", "")
        return _alembic(database_dsn(identity), revision, command)
