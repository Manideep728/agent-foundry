"""Whether the tests that need a real PostgreSQL can run on this machine.

The default URL is the development server `make pg-up` starts. Each test gets a
schema of its own, dropped afterwards, so tests share a server without sharing
tables and a failed test leaves nothing behind for the next.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

PG_URL = os.environ.get(
    "AF_PG_URL", "postgresql://agentfoundry@localhost:5432/agentfoundry"
)


def _server_available() -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    try:
        psycopg.connect(PG_URL, connect_timeout=3).close()
    except psycopg.Error:
        return False
    return True


requires_postgres = pytest.mark.skipif(
    not _server_available(),
    reason=f"no PostgreSQL at {PG_URL}; run `make pg-up`",
)


def with_search_path(url: str, schema: str) -> str:
    """The same server, with `schema` first on the search path."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}options=-csearch_path%3D{schema}"


@contextmanager
def fresh_schema() -> Iterator[str]:
    """A URL whose tables live in an empty schema, dropped afterwards."""
    import psycopg
    from psycopg import sql

    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield with_search_path(PG_URL, name)
        finally:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name))
            )
