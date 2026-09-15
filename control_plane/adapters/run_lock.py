"""One `af run` at a time per database.

Recovery treats every unfinished run as abandoned: it marks the run interrupted
and removes its container. That is only true when no other `af run` is using the
same database. Without this lock, a second run started beside a live one
recovers the live one -- it removes the container in the middle of validation.

The lock is `flock` on a file beside the database. The kernel releases it when
the holding process exits for any reason, including SIGKILL, so a crash never
leaves a stale lock, and acquiring it proves the previous holder is gone.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class DatabaseBusy(Exception):
    """Another process holds the run lock for this database."""


def lock_path_for(database: Path) -> Path:
    return database.with_name(f"{database.name}.lock")


@contextmanager
def exclusive_run_lock(database: Path) -> Iterator[None]:
    """Hold the database's run lock for the duration of the block.

    Raises `DatabaseBusy` at once, rather than waiting, when another process
    holds it: a queued run would start minutes later against state its operator
    no longer expects.
    """
    descriptor = os.open(lock_path_for(database), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DatabaseBusy(
                f"another af run is using {database}; wait for it to finish"
            ) from error
        yield
    finally:
        # Closing the descriptor releases the lock.
        os.close(descriptor)
