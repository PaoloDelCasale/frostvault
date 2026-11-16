"""Session options every FrostVault database connection starts with (#718).

Parallel query plans give each worker a dynamic shared memory segment, and a
small ``/dev/shm`` (64 MB by default in Docker) cannot hold them: a vault-wide
query then fails with ``DiskFull`` (#707, #718). Every application session
plans serially instead. The setting is a startup option, so it is the
session's default and survives the pool's ``DISCARD ALL``.
"""
from __future__ import annotations

import os

from psycopg.conninfo import conninfo_to_dict

SERIAL_QUERY_OPTION = "-c max_parallel_workers_per_gather=0"


def session_options(dsn: str) -> str:
    """libpq ``options`` for ``dsn``: its own (or ``PGOPTIONS``) plus serial plans.

    An explicit ``options`` replaces ``PGOPTIONS`` in libpq, so both are kept.
    """
    values = conninfo_to_dict(dsn.replace("postgresql+psycopg://", "postgresql://", 1))
    existing = str(values.get("options") or os.environ.get("PGOPTIONS", "")).strip()
    return f"{existing} {SERIAL_QUERY_OPTION}".strip()
