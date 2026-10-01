"""Opening the run store named by `--db`.

One function, so every command opens a database the same way and takes the
same lock: a command that opened the repository itself would be the one that
forgot the lock.

`--db` is a SQLite file path, or a `postgresql://` URL (D33).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from control_plane.adapters.run_lock import DatabaseBusy, exclusive_run_lock
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.services.ports import RunRepository

_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def is_postgres(target: str) -> bool:
    return target.startswith(_POSTGRES_SCHEMES)


def database_exists(target: str) -> bool:
    """Whether a read-only command has anything to read.

    A server is assumed to exist; connecting says otherwise, with the reason.
    """
    return is_postgres(target) or Path(target).exists()


def display_name(target: str) -> str:
    """The database, safe to print or to write into a report.

    A URL loses any password it carries. The documented way to pass one is
    `PGPASSWORD`, but a URL with a password in it is still a valid URL, and a
    report that quoted it would publish the credential.
    """
    if not is_postgres(target):
        return target
    parts = urlsplit(target)
    if parts.password is None:
        return target
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    user = f"{parts.username}@" if parts.username else ""
    return urlunsplit(parts._replace(netloc=f"{user}{host}"))


@contextmanager
def open_repository(target: str, *, exclusive: bool) -> Iterator[RunRepository]:
    """The repository at `target`, closed when the block ends.

    `exclusive` holds the run lock (D17) for the whole block, taken before
    anything is read: recovery, which runs first, assumes every unfinished run
    it finds was abandoned, and that is only true while nothing else writes.
    It raises `DatabaseBusy` at once when another process holds it.
    """
    with ExitStack() as stack:
        if is_postgres(target):
            yield _open_postgres(stack, target, exclusive=exclusive)
            return
        path = Path(target)
        if exclusive:
            stack.enter_context(exclusive_run_lock(path))
        yield stack.enter_context(SqliteRunRepository(path))


def _open_postgres(stack: ExitStack, target: str, *, exclusive: bool) -> RunRepository:
    try:
        # Imported here, so SQLite users never need the driver installed.
        from control_plane.adapters.postgres_repo import PostgresRunRepository
    except ImportError as error:
        raise RuntimeError(
            "a postgresql:// database needs the driver: uv sync --extra postgres"
        ) from error
    repository = stack.enter_context(PostgresRunRepository(target))
    if exclusive and not repository.try_hold_run_lock():
        raise DatabaseBusy(
            f"another af process is using {display_name(target)}; wait for it to finish"
        )
    return repository
