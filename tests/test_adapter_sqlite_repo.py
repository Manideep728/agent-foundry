"""Persistence.

The read path is the validation path: rehydration goes through the domain's
own constructors, so a row that violates an invariant fails loudly instead of
becoming a plausible wrong number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.model import MicroUsd, ModelInvocation, ModelUsage
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.scoring import Score
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.task import Suite
from control_plane.domain.validation import (
    SCHEMA_VERSION,
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)
from tests.fakes import EPOCH_MS


@pytest.fixture
def repo(tmp_path: Path):  # type: ignore[no-untyped-def]
    with SqliteRunRepository(tmp_path / "af.sqlite3") as repository:
        yield repository


def new_run(run_id: str = "run-0001") -> EvaluationRun:
    return EvaluationRun.create(RunId(run_id), TaskId("csv-quoted-fields"), EPOCH_MS)


def base_record(run: EvaluationRun, **overrides: object) -> RunRecord:
    fields: dict[str, object] = {
        "run": run,
        "suite": Suite.SYNTHETIC,
        "fixture_sha256": "a" * 64,
        "runner_id": "local-subprocess",
        "runner_version": "local-subprocess 0.1.0 (no isolation)",
        "arch": "arm64",
        "pid1": False,
    }
    fields.update(overrides)
    return RunRecord(**fields)  # type: ignore[arg-type]


def a_report() -> ValidationReport:
    empty = StreamCapture(
        bytes_total=12,
        bytes_written=12,
        truncated=False,
        path="cmd-000.stdout.log",
        tail="ok\n",
    )
    return ValidationReport(
        schema_version=SCHEMA_VERSION,
        runner_version="local-subprocess 0.1.0 (no isolation)",
        arch="arm64",
        pid1=False,
        shell="/bin/sh",
        workdir="/work",
        started_at_unix_ms=EPOCH_MS,
        wall_ms=1234,
        exit_reason=ExitReason.COMPLETED,
        aborted=False,
        orphans_reaped_total=0,
        commands=(
            CommandResult(
                index=0,
                command="python3 -m unittest tests.test_quoting",
                started_at_unix_ms=EPOCH_MS,
                duration_ms=600,
                outcome=CommandOutcome.EXITED,
                stdout=empty,
                stderr=empty,
                exit_code=0,
            ),
            CommandResult(
                index=1,
                command="python3 -m unittest tests.test_basic",
                started_at_unix_ms=EPOCH_MS + 600,
                duration_ms=500,
                outcome=CommandOutcome.EXITED,
                stdout=empty,
                stderr=empty,
                exit_code=0,
            ),
        ),
    )


def a_score() -> Score:
    return Score(
        resolved=True,
        regressed=False,
        fail_to_pass_passed=1,
        fail_to_pass_total=1,
        pass_to_pass_passed=1,
        pass_to_pass_total=1,
        timed_out_commands=0,
    )


# --- create and idempotency --------------------------------------------------


def test_a_created_run_reads_back(repo: SqliteRunRepository) -> None:
    repo.create(base_record(new_run()))
    stored = repo.get(RunId("run-0001"))

    assert stored is not None
    assert stored.run.state is RunState.CREATED
    assert stored.suite is Suite.SYNTHETIC
    assert stored.pid1 is False


def test_recording_the_same_run_twice_is_refused(repo: SqliteRunRepository) -> None:
    from control_plane.services.errors import RunAlreadyExists

    repo.create(base_record(new_run()))
    with pytest.raises(RunAlreadyExists):
        repo.create(base_record(new_run()))


def test_a_refused_duplicate_leaves_the_original_intact(
    repo: SqliteRunRepository,
) -> None:
    # INSERT OR REPLACE would silently destroy a previous run's record, which
    # for an evaluation harness is the data loss the project exists to prevent.
    from control_plane.services.errors import RunAlreadyExists

    repo.create(base_record(new_run()))
    repo.advance(
        new_run().transition_to(RunState.PREPARING, EPOCH_MS + 1), RunState.CREATED
    )

    with pytest.raises(RunAlreadyExists):
        repo.create(base_record(new_run()))

    stored = repo.get(RunId("run-0001"))
    assert stored is not None and stored.run.state is RunState.PREPARING


def test_getting_an_unknown_run_returns_none(repo: SqliteRunRepository) -> None:
    assert repo.get(RunId("run-9999")) is None


# --- compare and swap --------------------------------------------------------


def test_a_state_advance_requires_the_expected_prior_state(
    repo: SqliteRunRepository,
) -> None:
    from control_plane.services.errors import ConcurrentModification

    repo.create(base_record(new_run()))
    moved = new_run().transition_to(RunState.PREPARING, EPOCH_MS + 1)
    repo.advance(moved, RunState.CREATED)

    # The scenario: a recovery scan decided this run was orphaned while the
    # original process was still alive. Without the rowcount check the second
    # write would silently succeed and a measurement from a process declared
    # dead would enter the benchmark.
    with pytest.raises(ConcurrentModification):
        repo.advance(moved, RunState.CREATED)


def test_an_update_matching_no_rows_raises_rather_than_passing(
    repo: SqliteRunRepository,
) -> None:
    # An UPDATE that matches nothing is *success* in SQLite. This asserts the
    # rowcount check is what turns that into a failure.
    from control_plane.services.errors import ConcurrentModification

    absent = EvaluationRun.create(RunId("run-absent"), TaskId("t"), EPOCH_MS)
    with pytest.raises(ConcurrentModification):
        repo.advance(
            absent.transition_to(RunState.PREPARING, EPOCH_MS + 1), RunState.CREATED
        )


# --- the terminal write ------------------------------------------------------


def advance_to_validating(repo: SqliteRunRepository) -> EvaluationRun:
    run = new_run()
    repo.create(base_record(run))
    for state, at in (
        (RunState.PREPARING, EPOCH_MS + 1),
        (RunState.AGENT_RUNNING, EPOCH_MS + 2),
        (RunState.VALIDATING, EPOCH_MS + 3),
    ):
        previous = run.state
        run = run.transition_to(state, at)
        repo.advance(run, previous)
    return run


def test_a_scored_run_round_trips_completely(repo: SqliteRunRepository) -> None:
    run = advance_to_validating(repo)
    scored = run.transition_to(RunState.SCORED, EPOCH_MS + 4)
    usage = ModelUsage(input_tokens=5000, output_tokens=500)
    invocation = ModelInvocation(
        provider_id="fake",
        model_id="scripted",
        usage=usage,
        cost=MicroUsd(48_000),
        latency_ms=1100,
    )

    repo.finish(
        base_record(
            scored,
            report=a_report(),
            score=a_score(),
            outcome=AgentOutcome.CLEAN_PATCH,
            patch_is_empty=False,
            patch_sha256="b" * 64,
            patch_bytes=421,
            invocations=(invocation,),
            total_cost_micros=MicroUsd(48_000),
            agent_ms=1100,
            validation_ms=1234,
        ),
        RunState.VALIDATING,
    )

    stored = repo.get(RunId("run-0001"))
    assert stored is not None
    assert stored.run.state is RunState.SCORED
    assert stored.outcome is AgentOutcome.CLEAN_PATCH
    assert stored.score == a_score()
    assert stored.total_cost_micros == 48_000
    assert stored.invocations == (invocation,)
    assert stored.report is not None
    assert len(stored.report.commands) == 2
    assert stored.report.commands[1].command.endswith("test_basic")
    assert stored.report.exit_reason is ExitReason.COMPLETED


def test_a_failed_run_keeps_its_reason_and_the_state_it_failed_from(
    repo: SqliteRunRepository,
) -> None:
    run = advance_to_validating(repo)
    failed = run.fail(FailureCategory.RUNNER_FAILED, EPOCH_MS + 4, "no result file")

    repo.finish(base_record(failed), RunState.VALIDATING)

    stored = repo.get(RunId("run-0001"))
    assert stored is not None
    assert stored.run.failure is not None
    assert stored.run.failure.kind is FailureCategory.RUNNER_FAILED
    assert stored.run.failure.failed_from is RunState.VALIDATING
    assert stored.run.failure.detail == "no result file"
    assert stored.outcome is None


def test_a_task_defect_keeps_its_evidence(repo: SqliteRunRepository) -> None:
    # FAILED implies no outcome, but report and score are evidence and must
    # survive -- otherwise the defect is unactionable.
    run = advance_to_validating(repo)
    failed = run.fail(
        FailureCategory.TASK_INVALID, EPOCH_MS + 4, "empty patch resolved"
    )

    repo.finish(
        base_record(
            failed,
            report=a_report(),
            score=a_score(),
            patch_is_empty=True,
            task_defect=True,
        ),
        RunState.VALIDATING,
    )

    stored = repo.get(RunId("run-0001"))
    assert stored is not None
    assert stored.outcome is None
    assert stored.score is not None
    assert stored.report is not None
    assert stored.task_defect


# --- the float guard ---------------------------------------------------------


def test_no_column_is_declared_real(repo: SqliteRunRepository) -> None:
    # The storage-layer sibling of the two AST guards. STRICT rejects 1.5 but
    # silently coerces 2.0, so the schema itself has to be checked.
    real_columns = [
        (table, column)
        for table, column, declared in repo.column_types()
        if declared.upper() in {"REAL", "FLOAT", "DOUBLE", "NUMERIC"}
    ]
    assert not real_columns


def test_a_float_measurement_is_rejected(repo: SqliteRunRepository) -> None:
    run = advance_to_validating(repo)
    scored = run.transition_to(RunState.SCORED, EPOCH_MS + 4)

    with pytest.raises(TypeError, match="must be an int"):
        repo.finish(
            base_record(
                scored,
                report=a_report(),
                score=a_score(),
                outcome=AgentOutcome.CLEAN_PATCH,
                patch_is_empty=False,
                agent_ms=1100.5,  # type: ignore[arg-type]
            ),
            RunState.VALIDATING,
        )


# --- listing -----------------------------------------------------------------


def test_unfinished_runs_are_listed_for_recovery(repo: SqliteRunRepository) -> None:
    advance_to_validating(repo)

    done = EvaluationRun.create(RunId("run-0002"), TaskId("t"), EPOCH_MS)
    repo.create(base_record(done))

    unfinished = repo.list_unfinished()
    assert {r.run.run_id.value for r in unfinished} == {"run-0001", "run-0002"}


def test_a_terminal_run_is_not_listed_as_unfinished(repo: SqliteRunRepository) -> None:
    run = advance_to_validating(repo)
    repo.finish(
        base_record(run.fail(FailureCategory.INTERRUPTED, EPOCH_MS + 9)),
        RunState.VALIDATING,
    )
    assert repo.list_unfinished() == ()


def test_a_database_from_a_newer_build_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "af.sqlite3"
    SqliteRunRepository(path).close()

    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 99")

    with pytest.raises(RuntimeError, match="newer build"):
        SqliteRunRepository(path)
