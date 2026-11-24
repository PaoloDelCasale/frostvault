"""The test fixtures: isolated database, Owner, Vault, Settings override (#801).

Everything a test needs before its first assertion lives here, once:

- ``fresh_database()``: a new database cloned from the migrated template.
  ``empty_database()`` has no schema; ``alembic_upgrade_head()`` really runs
  Alembic on a database that already exists.
- ``override_settings(...)``: the only place that overrides ``Settings``.
- ``create_owner()``, ``seed_vault()``, ``seed_job()``: the Owner row, a Vault
  with its Vault Target, a Job on a Vault File.
- ``sign_in()``: the Owner's Session on an HTTP test client.
- ``http_client()``: the HTTP test client; it checks every JSON success body
  against the model its route declares (ADR-0056).
- ``FrostVaultTestCase``: fresh database, Owner and Settings in one setUp.

``TEST_POSTGRES_URL`` must name a *test server* with a CREATEDB role. Every
fixture database has its own unique name; the configured database is never
mutated. Database tests never skip: missing configuration is an error.

Importing this module imports nothing of the application: ``app.config`` reads
the environment when first imported, and the e2e seed script sets
``DATABASE_URL`` from a fixture database before that.
"""
from __future__ import annotations

import atexit
import functools
import itertools
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from app import config

_PREFIX = "fvtest_" + uuid.uuid4().hex[:12] + "_"
_LOCK = threading.RLock()
_SEQUENCE = itertools.count(1)
_LIVE: set[str] = set()
_TEMPLATE: str | None = None
_ROOT = Path(__file__).resolve().parents[1]


# -- Isolated databases ---------------------------------------------------------

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


def _create(label: str, template: str | None = None) -> str:
    # The process id keeps a forked child from reusing its parent's names.
    name = f"{_PREFIX}{label}_{os.getpid()}_{next(_SEQUENCE)}"
    with psycopg.connect(_server_dsn(), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(template or "template0")
        ))
    _LIVE.add(name)
    return name


def _drop(name: str) -> None:
    assert name.startswith(_PREFIX), "refusing to drop a non-fixture database"
    with psycopg.connect(_server_dsn(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
    _LIVE.discard(name)


def _drop_remaining() -> None:
    for name in list(_LIVE):
        try:
            _drop(name)
        except psycopg.Error:
            pass


atexit.register(_drop_remaining)


def _alembic_upgrade(dsn: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


def _migrated_template() -> str:
    """The template every fresh database is cloned from: one real Alembic run
    of the whole chain, once per process."""
    global _TEMPLATE
    if _TEMPLATE is None:
        name = _create("template")
        result = _alembic_upgrade(_dsn(name))
        if result.returncode:
            _drop(name)
            raise AssertionError("alembic upgrade head failed on the template: " + result.stderr)
        _TEMPLATE = name
    return _TEMPLATE


@dataclass(frozen=True)
class Database:
    """One disposable database of the test server, known by its name."""

    name: str

    @property
    def dsn(self) -> str:
        return _dsn(self.name)

    def connect(self) -> psycopg.Connection:
        """A raw psycopg connection with dict rows; ``with`` commits on exit."""
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def rows(self) -> dict[str, list[dict]]:
        """Every row of every table, to prove that nothing changed."""
        with self.connect() as connection:
            names = connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            ).fetchall()
            return {
                row["table_name"]: sorted(connection.execute(
                    sql.SQL("SELECT * FROM {}").format(sql.Identifier(row["table_name"]))
                ).fetchall(), key=repr)
                for row in names
            }

    def restore_dump(self, content: bytes) -> None:
        """Restore a custom-format ``pg_dump`` into this (empty) database."""
        from app.services.metadata_backups import _postgres_env
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "restore.dump"
            dump.write_bytes(content)
            result = subprocess.run(
                ["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--dbname",
                 self.name, str(dump)],
                env=_postgres_env(self.dsn), capture_output=True, check=False,
            )
        if result.returncode:
            raise AssertionError("Isolated PostgreSQL restore failed: " + result.stderr.decode())

    def drop(self) -> None:
        with _LOCK:
            _drop(self.name)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.drop()


def fresh_database() -> Database:
    """A new database at the schema head, cloned from the migrated template.

    Use it as a context manager (``self.enterContext(fresh_database())``) to
    drop it at the end of the test; databases left behind are dropped at exit.
    """
    _install_problem_families()
    with _LOCK:
        return Database(_create("db", _migrated_template()))


def _install_problem_families() -> None:
    """Give the inbox its Problem families, as ``app.main`` does (#804).

    Only once the application is imported: importing it here would read the
    environment before the e2e seed script has set it (see above).
    """
    if "app.config" in sys.modules:
        from app.services import problem_families
        problem_families.install()


def empty_database() -> Database:
    """A new database with no schema at all, as a first start finds it."""
    with _LOCK:
        return Database(_create("empty"))


def alembic_upgrade_head(database: Database) -> subprocess.CompletedProcess[str]:
    """Really run ``alembic upgrade head`` on ``database`` (a subprocess)."""
    return _alembic_upgrade(database.dsn)


# -- Settings -------------------------------------------------------------------

@contextmanager
def override_settings(**changes: Any) -> Iterator[config.Settings]:
    """Override fields of ``app.config.settings`` and restore them on exit.

    The application has one ``Settings`` instance, imported by value in its
    modules. This replaces the given fields on that instance, so every module
    sees them and none needs a patch. Use it as a context manager or with
    ``self.enterContext(override_settings(...))``; overrides can be nested.

    A changed ``database_dsn`` also retires what the process keeps per DSN
    (connection pools, upload pipeline coordinators) on exit. Other runtime
    state (Source Device slots, metrics, runtime status) keeps its own reset.
    """
    from app import config
    _install_problem_families()
    unknown = sorted(set(changes) - {field.name for field in fields(config.Settings)})
    if unknown:
        raise TypeError("not a Settings field: " + ", ".join(unknown))
    settings = config.settings
    previous = {name: getattr(settings, name) for name in changes}
    try:
        for name, value in changes.items():
            object.__setattr__(settings, name, value)
        yield settings
    finally:
        overridden_dsn = settings.database_dsn
        for name, value in previous.items():
            object.__setattr__(settings, name, value)
        if "database_dsn" in changes and overridden_dsn != settings.database_dsn:
            _forget_database(overridden_dsn)


def _forget_database(dsn: str) -> None:
    from app import database
    from app.services import upload_pipeline
    database.close_pools()
    with upload_pipeline._registry_lock:
        upload_pipeline._registry.pop(dsn, None)


# -- Owner, Vault, Session ------------------------------------------------------

def create_owner(
    connection: Any,
    *,
    display_name: str = "Owner",
    password: str | None = None,
    password_hash: str | None = "hash",
) -> None:
    """Create the single Owner (id 1), with an Emergency Password when given."""
    if password is not None:
        from app.security import hash_password
        password_hash = hash_password(password)
    connection.execute(
        "INSERT INTO owner(id, display_name, password_hash) VALUES (1, %s, %s)",
        (display_name, password_hash),
    )


def seed_vault(
    connection: Any,
    *,
    name: str = "Docs",
    source_root: str = "/sources/managed/docs",
    bucket: str = "bucket",
    target: dict[str, Any] | None = None,
    **columns: Any,
) -> dict[str, Any]:
    """Insert one Vault and return its row: the one Vault seeder.

    The Vault Target of ``bucket`` is created on first use (S3, eu-south-1,
    ``vault_prefix`` layout) and shared by the Vaults seeded on it; ``target``
    gives other ``vault_targets`` columns for a Target created here.
    ``columns`` are further ``vaults`` columns, for example an explicit ``id``.
    """
    found = connection.execute(
        "SELECT id FROM vault_targets WHERE bucket=%s", (bucket,)
    ).fetchone()
    if found is None:
        found = _insert(connection, "vault_targets", {
            "provider": "s3", "bucket": bucket, "region": "eu-south-1",
            "key_layout": "vault_prefix", **(target or {}),
        })
    elif target:
        raise ValueError(f"the Vault Target of {bucket!r} already exists")
    return _insert(connection, "vaults", {
        "name": name, "source_root": source_root, "target_id": found["id"], **columns,
    })


def seed_job(
    connection: Any,
    *,
    vault_id: int,
    path: str = "a.txt",
    action: str = "upload",
    status: str = "completed",
    origin: str = "manual",
) -> int:
    """Create a Vault File at ``path`` (once) and one Job on it; return its id."""
    file = connection.execute(
        "SELECT id FROM files WHERE vault_id=%s AND path=%s", (vault_id, path)
    ).fetchone() or connection.execute(
        "INSERT INTO files(vault_id, path, path_since) VALUES (%s, %s, now()) RETURNING id",
        (vault_id, path),
    ).fetchone()
    return connection.execute(
        """
        INSERT INTO jobs(vault_id, file_id, action, status, origin)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (vault_id, file["id"], action, status, origin),
    ).fetchone()["id"]


def bump_session_version(connection: Any) -> None:
    """Bump the Owner's session version: every Session made before it is stale."""
    connection.execute("UPDATE owner SET session_version = session_version + 1 WHERE id = 1")


def _insert(connection: Any, table: str, values: dict[str, Any]) -> dict[str, Any]:
    return connection.execute(
        sql.SQL("INSERT INTO {}({}) VALUES ({}) RETURNING *").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() * len(values)),
        ),
        tuple(values.values()),
    ).fetchone()


@dataclass(frozen=True)
class SignedIn:
    """The Owner's Session as the browser holds it."""

    token: str
    csrf_token: str
    # The answer of ``POST /api/login`` when the Emergency Password signed in.
    response: Any = None

    @property
    def csrf_headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf_token}


def sign_in(
    client: Any,
    database: Database | None = None,
    *,
    password: str | None = None,
    username: str | None = None,
    auth_method: str = "oidc",
    reauthenticated_until: str | None = None,
    send_csrf_header: bool = False,
) -> SignedIn:
    """Give ``client`` a live Owner Session: the one login of HTTP tests.

    Without ``password`` the Session is created in ``database``, as after a
    completed sign-in with ``auth_method``, and its Session and CSRF cookies
    are set on the client. ``reauthenticated_until`` (an ISO timestamp) moves
    its reauthentication time.

    With ``password`` the client posts the Emergency Password to
    ``/api/login`` (``username`` is sent only when given) and the answer must
    be 200; it is kept in ``.response``.

    ``send_csrf_header`` also sends the CSRF header with every later request;
    otherwise pass ``.csrf_headers`` where a request needs it.
    """
    from app.config import settings
    if password is not None:
        fields_ = {"password": password} if username is None else {
            "username": username, "password": password}
        response = client.post("/api/login", json=fields_)
        if response.status_code != 200:
            raise AssertionError(f"sign-in failed: {response.status_code} {response.text}")
        signed_in = SignedIn(
            client.cookies.get(settings.session_cookie_name),
            client.cookies.get(settings.csrf_cookie_name),
            response,
        )
    else:
        from app.sessions import _hash_token, create_session, csrf_token_for
        if database is None:
            raise TypeError("sign_in needs the database, or the Emergency Password")
        with database.connect() as connection:
            token = create_session(connection, auth_method=auth_method)
            if reauthenticated_until is not None:
                connection.execute(
                    "UPDATE sessions SET reauth_at=%s WHERE token_hash=%s",
                    (reauthenticated_until, _hash_token(token)),
                )
            csrf_token = csrf_token_for(connection, token)
        client.cookies.set(settings.session_cookie_name, token)
        client.cookies.set(settings.csrf_cookie_name, csrf_token)
        signed_in = SignedIn(token, csrf_token)
    if send_csrf_header:
        client.headers["X-CSRF-Token"] = signed_in.csrf_token
    return signed_in


def http_client(**options: Any) -> Any:
    """A test client of the application, calling from loopback.

    It checks every JSON success body against the model its route declares.
    """
    from app.main import app
    options.setdefault("client", ("127.0.0.1", 50000))
    return _contract_checking_client()(app, **options)


# -- The response contract (ADR-0056) -------------------------------------------
#
# Production passes a route's dictionary through unvalidated. The model a route
# declares with ``passthrough(Model)`` is therefore checked here, on every
# answer the tests receive through ``http_client``.

_CONTRACT_REPORT_ENV = "FROSTVAULT_RESPONSE_CONTRACT_REPORT"
_CONTRACTS_SEEN: set[str] = set()


def _response_contract(route: Any) -> Any:
    return getattr(getattr(route, "response_model", None), "__contract__", None)


def check_response_contract(app: Any, response: Any) -> None:
    """Fail when a JSON success body is not what its route declares.

    A missing field, a wrong type and an undeclared field of a closed model all
    fail; an open model is checked on its declared fields only.
    """
    from pydantic import ValidationError
    from starlette.routing import Match

    content_type = response.headers.get("content-type", "").partition(";")[0]
    if not response.is_success or content_type != "application/json" or not response.content:
        return
    request = response.request
    scope = {"type": "http", "method": request.method, "path": request.url.path}
    route = next(
        (route for route in app.router.routes if route.matches(scope)[0] is Match.FULL), None)
    contract = _response_contract(route)
    if contract is None:
        return
    _CONTRACTS_SEEN.add(f"{request.method} {route.path}")
    try:
        contract.model_validate_json(response.content, strict=True)
    except ValidationError as error:
        raise AssertionError(
            f"{request.method} {route.path} answered {response.status_code} with a body "
            f"that is not the {contract.__name__} it declares:\n{error}"
        ) from None


@functools.cache
def _contract_checking_client() -> type:
    from fastapi.testclient import TestClient

    class ContractCheckingClient(TestClient):
        def request(self, *args: Any, **kwargs: Any) -> Any:
            response = super().request(*args, **kwargs)
            check_response_contract(self.app, response)
            return response

    return ContractCheckingClient


def response_contract_report(app: Any) -> list[str]:
    """One line per operation that declares a model: ``seen`` or ``unseen`` by this process."""
    lines = []
    for route in app.router.routes:
        if _response_contract(route) is None:
            continue
        for method in sorted(route.methods):
            operation = f"{method} {route.path}"
            lines.append(f"{'seen' if operation in _CONTRACTS_SEEN else 'unseen'}\t{operation}")
    return sorted(lines)


@atexit.register
def _write_response_contract_report() -> None:
    # An operation no test calls is documented but unverified: CI collects these
    # files from every shard and lists the operations none of them saw.
    target = os.getenv(_CONTRACT_REPORT_ENV)
    main = sys.modules.get("app.main")
    if target and main is not None:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(
            "".join(line + "\n" for line in response_contract_report(main.app)), encoding="utf-8")


# -- The common setUp -----------------------------------------------------------

class FrostVaultTestCase(unittest.TestCase):
    """Fresh database, Owner and Settings pointing at it, before every test.

    Class attributes describe the variation:

    - ``owner``: keyword arguments of ``create_owner`` (``None``: no Owner);
    - ``settings_overrides``: further Settings overrides, by field name.

    ``self.database`` is the database, ``self.settings`` the overridden
    Settings, ``self.tmp_path`` a temporary directory made on first use.
    Everything is undone by cleanups, also when setUp fails later.
    """

    owner: dict[str, Any] | None = {}
    settings_overrides: dict[str, Any] = {}

    def setUp(self) -> None:
        from unittest.mock import patch

        from app.services.runtime_status import runtime_status

        super().setUp()
        # Scan state is process-wide and keyed by Vault id, and every fresh
        # database numbers its Vaults from 1 again: a test neither sees the
        # scan state another test left nor leaves its own behind.
        self.enterContext(patch.dict(runtime_status, clear=True))
        self.database = self.enterContext(fresh_database())
        if self.owner is not None:
            with self.database.connect() as connection:
                create_owner(connection, **self.owner)
        self.settings = self.enterContext(override_settings(
            database_dsn=self.database.dsn, cookie_secure=False, **self.settings_overrides,
        ))

    @functools.cached_property
    def tmp_path(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def http_client(self, **options: Any) -> Any:
        return http_client(**options)

    def sign_in(self, client: Any = None, **options: Any) -> SignedIn:
        """Sign the Owner in on ``client`` (default: ``self.client``).

        The Session is also kept in ``self.signed_in``, for its CSRF headers.
        """
        self.signed_in = sign_in(
            self.client if client is None else client, self.database, **options)
        return self.signed_in
