"""Recovering runs the control plane died in the middle of.

A real SQLite repository, so the compare-and-swap under test is the real one,
and a recording remover in place of Docker.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from control_plane.adapters.docker_runner import RUNNER_ID as DOCKER_RUNNER_ID
from control_plane.adapters.local_runner import RUNNER_ID as LOCAL_RUNNER_ID
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.outcomes import FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.states import EvaluationRun, RecoveryAction, RunState
from control_plane.domain.task import Suite
from control_plane.services.errors import RunnerUnavailable
from control_plane.services.recovery import Recovered, recover_unfinished
from tests.fakes import EPOCH_MS, FakeClock

PATH_TO_STATE = {
    RunState.PREPARING: (RunState.PREPARING,),
    RunState.AGENT_RUNNING: (RunState.PREPARING, RunState.AGENT_RUNNING),
    RunState.VALIDATING: (
        RunState.PREPARING,
        RunState.AGENT_RUNNING,
        RunState.VALIDATING,
    ),
}


class RecordingRemover:
    def __init__(self, error: Exception | None = None) -> None:
        self.removed: list[RunId] = []
        self.error = error

    def __call__(self, run_id: RunId) -> None:
        self.removed.append(run_id)
        if self.error is not None:
            raise self.error


@pytest.fixture(name="repository")
def repository_fixture(tmp_path: Path) -> Iterator[SqliteRunRepository]:
    with SqliteRunRepository(tmp_path / "af.sqlite3") as repository:
        yield repository


def _left_in(
    repository: SqliteRunRepository,
    state: RunState,
    run_id: str = "run-0001",
    runner_id: str = DOCKER_RUNNER_ID,
) -> EvaluationRun:
    """Create a run and walk it to `state`, as a process that then died would."""
    run = EvaluationRun.create(RunId(run_id), TaskId("csv-quoted-fields"), EPOCH_MS)
    repository.create(
        RunRecord(
            run=run,
            suite=Suite.SYNTHETIC,
            fixture_sha256="a" * 64,
            runner_id=runner_id,
            runner_version="test",
            arch="x86_64",
            pid1=False,
        )
    )
    for next_state in PATH_TO_STATE[state]:
        moved = run.transition_to(next_state, EPOCH_MS)
        repository.advance(moved, expected=run.state)
        run = moved
    return run


def _recover(
    repository: SqliteRunRepository, tmp_path: Path, remover: RecordingRemover
) -> tuple[Recovered, ...]:
    return recover_unfinished(
        repository, WorkspaceFactory(tmp_path / "runs"), FakeClock(), remover
    )


def test_a_docker_run_left_validating_has_its_container_removed(
    repository: SqliteRunRepository, tmp_path: Path
) -> None:
    _left_in(repository, RunState.VALIDATING)
    remover = RecordingRemover()

    recovered = _recover(repository, tmp_path, remover)

    assert remover.removed == [RunId("run-0001")]
    assert len(recovered) == 1
    assert recovered[0].action is RecoveryAction.REMOVE_CONTAINER
    assert recovered[0].cleanup_error is None
    stored = repository.get(RunId("run-0001"))
    assert stored is not None
    assert stored.run.state is RunState.FAILED
    assert stored.run.failure is not None
    assert stored.run.failure.kind is FailureCategory.INTERRUPTED


def test_a_local_run_left_validating_removes_no_container(
    repository: SqliteRunRepository, tmp_path: Path
) -> None:
    _left_in(repository, RunState.VALIDATING, runner_id=LOCAL_RUNNER_ID)
    remover = RecordingRemover()

    recovered = _recover(repository, tmp_path, remover)

    assert remover.removed == []
    assert len(recovered) == 1


@pytest.mark.parametrize("state", [RunState.PREPARING, RunState.AGENT_RUNNING])
def test_a_run_that_never_validated_removes_no_container(
    repository: SqliteRunRepository, tmp_path: Path, state: RunState
) -> None:
    _left_in(repository, state)
    remover = RecordingRemover()

    _recover(repository, tmp_path, remover)

    assert remover.removed == []


def test_a_second_pass_finds_nothing(
    repository: SqliteRunRepository, tmp_path: Path
) -> None:
    _left_in(repository, RunState.VALIDATING)
    remover = RecordingRemover()

    _recover(repository, tmp_path, remover)
    second = _recover(repository, tmp_path, remover)

    assert second == ()
    assert remover.removed == [RunId("run-0001")]


def test_a_failed_removal_still_marks_the_run_and_reports_why(
    repository: SqliteRunRepository, tmp_path: Path
) -> None:
    _left_in(repository, RunState.VALIDATING)
    remover = RecordingRemover(RunnerUnavailable("docker is not installed"))

    recovered = _recover(repository, tmp_path, remover)

    assert recovered[0].cleanup_error == "docker is not installed"
    stored = repository.get(RunId("run-0001"))
    assert stored is not None
    assert stored.run.state is RunState.FAILED


class FinishesDuringRecovery(SqliteRunRepository):
    """Lists a run as unfinished, then lets its original process finish it.

    That is the race the compare-and-swap exists for: the list is read, and
    the run reaches a terminal state before recovery writes.
    """

    def list_unfinished(self) -> tuple[RunRecord, ...]:
        records = super().list_unfinished()
        for record in records:
            finished = record.run.fail(FailureCategory.RUNNER_FAILED, EPOCH_MS)
            self.advance(finished, expected=record.run.state)
        return records


def test_a_run_that_finishes_first_keeps_its_result_and_its_container(
    tmp_path: Path,
) -> None:
    with FinishesDuringRecovery(tmp_path / "af.sqlite3") as repository:
        _left_in(repository, RunState.VALIDATING)
        remover = RecordingRemover()

        recovered = _recover(repository, tmp_path, remover)

        assert recovered == ()
        # Removing the container before losing the compare-and-swap would
        # have killed a run that was about to report.
        assert remover.removed == []
        stored = repository.get(RunId("run-0001"))
        assert stored is not None
        assert stored.run.failure is not None
        assert stored.run.failure.kind is FailureCategory.RUNNER_FAILED
