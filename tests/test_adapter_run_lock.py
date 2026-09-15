"""The per-database run lock, and the recovery it protects."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

import pytest

from control_plane.adapters.run_lock import (
    DatabaseBusy,
    exclusive_run_lock,
    lock_path_for,
)
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.record import RunRecord
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.task import Suite
from control_plane.interface.cli import main
from tests.fakes import EPOCH_MS

REPO = Path(__file__).resolve().parent.parent


def test_a_second_holder_is_refused_while_the_first_holds_it(tmp_path: Path) -> None:
    database = tmp_path / "af.sqlite3"

    # flock locks belong to the open file description, so a second open in this
    # process contends exactly as a second process would.
    with (
        exclusive_run_lock(database),
        pytest.raises(DatabaseBusy, match="another af run"),
        exclusive_run_lock(database),
    ):
        pass


def test_the_lock_is_free_again_after_the_block(tmp_path: Path) -> None:
    database = tmp_path / "af.sqlite3"

    with exclusive_run_lock(database):
        pass
    with exclusive_run_lock(database):
        pass


def test_the_lock_is_released_when_the_block_raises(tmp_path: Path) -> None:
    database = tmp_path / "af.sqlite3"

    with pytest.raises(RuntimeError), exclusive_run_lock(database):
        raise RuntimeError("the run failed")
    with exclusive_run_lock(database):
        pass


def test_the_lock_file_is_private(tmp_path: Path) -> None:
    database = tmp_path / "af.sqlite3"

    with exclusive_run_lock(database):
        mode = stat.S_IMODE(lock_path_for(database).stat().st_mode)

    assert mode & 0o077 == 0


def _leave_a_run_validating(database: Path) -> RunId:
    run = EvaluationRun.create(
        RunId("live-0001"), TaskId("csv-quoted-fields"), EPOCH_MS
    )
    with SqliteRunRepository(database) as repository:
        repository.create(
            RunRecord(
                run=run,
                suite=Suite.SYNTHETIC,
                fixture_sha256="a" * 64,
                runner_id="docker-afrunner",
                runner_version="test",
                arch="x86_64",
                pid1=True,
            )
        )
        for state in (RunState.PREPARING, RunState.AGENT_RUNNING, RunState.VALIDATING):
            moved = run.transition_to(state, EPOCH_MS)
            repository.advance(moved, expected=run.state)
            run = moved
    return run.run_id


def test_af_run_does_not_recover_a_run_another_process_is_executing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The regression: a second `af run` beside a live one used to mark the live
    # run interrupted and remove its container mid-validation.
    database = tmp_path / "af.sqlite3"
    live = _leave_a_run_validating(database)
    held = os.open(lock_path_for(database), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        code = main(
            [
                "run",
                str(REPO / "tasks" / "csv-quoted-fields.toml"),
                "--db",
                str(database),
                "--runs",
                str(tmp_path / "runs"),
                "--fixtures",
                str(REPO / "tasks" / "fixtures"),
                "--solutions",
                str(REPO / "tasks" / "solutions"),
                "--runner",
                "local",
            ]
        )
    finally:
        os.close(held)

    assert code == 1
    assert "another af run" in capsys.readouterr().err
    with SqliteRunRepository(database) as repository:
        stored = repository.get(live)
    assert stored is not None
    assert stored.run.state is RunState.VALIDATING
