"""Opening the run store: which commands take the lock, and when it is released."""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.run_lock import DatabaseBusy
from control_plane.adapters.storage import database_exists, open_repository


def test_a_second_writer_is_refused_while_the_first_holds_the_database(
    tmp_path: Path,
) -> None:
    target = str(tmp_path / "af.sqlite3")

    with (
        open_repository(target, exclusive=True),
        pytest.raises(DatabaseBusy),
        open_repository(target, exclusive=True),
    ):
        pass


def test_a_reader_is_not_blocked_by_a_writer(tmp_path: Path) -> None:
    """`af report` beside a running suite reads what is stored so far."""
    target = str(tmp_path / "af.sqlite3")

    with (
        open_repository(target, exclusive=True),
        open_repository(target, exclusive=False) as reader,
    ):
        assert reader.list_runs() == ()


def test_the_lock_is_released_when_the_block_ends(tmp_path: Path) -> None:
    target = str(tmp_path / "af.sqlite3")

    with open_repository(target, exclusive=True):
        pass
    with open_repository(target, exclusive=True) as repository:
        assert repository.list_unfinished() == ()


def test_a_path_that_holds_nothing_is_not_a_database(tmp_path: Path) -> None:
    assert not database_exists(str(tmp_path / "missing.sqlite3"))
