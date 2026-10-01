"""Opening the run store named by `--db`.

One function, so every command opens a database the same way and takes the
same lock: a command that opened the repository itself would be the one that
forgot the lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from control_plane.adapters.run_lock import exclusive_run_lock
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.services.ports import RunRepository


def database_exists(target: str) -> bool:
    """Whether a read-only command has anything to read."""
    return Path(target).exists()


@contextmanager
def open_repository(target: str, *, exclusive: bool) -> Iterator[RunRepository]:
    """The repository at `target`, closed when the block ends.

    `exclusive` holds the run lock (D17) for the whole block, taken before the
    database is opened: recovery, which runs first, assumes every unfinished
    run it finds was abandoned, and that is only true while nothing else writes.
    It raises `DatabaseBusy` at once when another process holds it.
    """
    with ExitStack() as stack:
        path = Path(target)
        if exclusive:
            stack.enter_context(exclusive_run_lock(path))
        yield stack.enter_context(SqliteRunRepository(path))
