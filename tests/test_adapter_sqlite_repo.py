"""Persistence.

The read path is the validation path: rehydration goes through the domain's
own constructors, so a row that violates an invariant fails loudly instead of
becoming a plausible wrong number.
"""

from __future__ import annotations

from dataclasses import MISSING, fields, replace
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


def test_a_created_run_records_its_workspace_before_it_exists(
    repo: SqliteRunRepository,
) -> None:
    """`create`, not just `finish`, persists the workspace path.

    The recovery scan reads this column to find the working copy of a run that
    died during setup. Written only by `finish`, it is null for every run that
    never finished -- which is exactly the set recovery exists for.
    """
    repo.create(base_record(new_run(), workspace_path="/runs/run-0001"))
    stored = repo.get(RunId("run-0001"))

    assert stored is not None
    assert stored.workspace_path == "/runs/run-0001"


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


# --- everything the record carries -------------------------------------------


CONFIGURATION: dict[str, object] = {
    "agent_id": "tools",
    "provider_id": "ollama",
    "model_id": "llama3.2:3b",
    "prompt_id": "tools-v2",
}
"""What `create()` writes and `finish()` must not touch.

The populated record below repeats it unchanged. A create-only column the two
records disagree about would fail the round trip for the right reason in the
wrong place.
"""

DERIVED_FIELDS = frozenset({"task_defect"})
"""Fields that no valid record can move off their default.

`task_defect` is `patch_is_empty and resolved`, so a record that carries it as
True is a record with an empty patch. test_a_task_defect_keeps_its_evidence
stores that shape on its own.
"""


def configured_run(repo: SqliteRunRepository) -> EvaluationRun:
    """Create a run the way the service does, then take it to VALIDATING.

    The created row holds the configuration and a guessed environment: no
    container exists yet, so arch is a placeholder and there are no
    measurements. Everything the finished record adds must therefore come from
    `finish()`, or be lost.
    """
    run = new_run()
    repo.create(base_record(run, arch="unknown", pid1=False, **CONFIGURATION))
    for state, at in (
        (RunState.PREPARING, EPOCH_MS + 1),
        (RunState.AGENT_RUNNING, EPOCH_MS + 2),
        (RunState.VALIDATING, EPOCH_MS + 3),
    ):
        previous = run.state
        run = run.transition_to(state, at)
        repo.advance(run, previous)
    return run


def fully_populated(run: EvaluationRun) -> RunRecord:
    """A scored record with every field carrying a distinct, real value."""
    invocation = ModelInvocation(
        provider_id="ollama",
        model_id="llama3.2:3b",
        usage=ModelUsage(
            input_tokens=432,
            output_tokens=11,
            cache_write_tokens=7,
            cache_read_tokens=405,
        ),
        cost=MicroUsd(48_000),
        latency_ms=286,
    )
    return base_record(
        run.transition_to(RunState.SCORED, EPOCH_MS + 4),
        # The environment the container reported, not the one create() guessed.
        runner_version="afrunner 0.1.0",
        arch="x86_64",
        pid1=True,
        report=a_report(),
        score=a_score(),
        outcome=AgentOutcome.CLEAN_PATCH,
        patch_sha256="b" * 64,
        patch_bytes=421,
        patch_is_empty=False,
        invocations=(invocation,),
        total_cost_micros=MicroUsd(48_000),
        agent_ms=1100,
        validation_ms=1234,
        workspace_path="/home/pendy/runs/run-0001",
        image_digest="sha256:" + "c" * 64,
        agent_note="3 tool call(s) but no file written",
        **CONFIGURATION,
    )


def test_a_finished_run_reads_back_exactly_as_it_was_written(
    repo: SqliteRunRepository,
) -> None:
    """The guard for a value computed correctly and then not written down.

    Three defects have had this shape -- `arch` and `pid1`, `workspace_path`,
    the agent result on two failure paths -- and each survived a suite that
    asserted on fields the test chose. `RunRecord` is frozen and compares
    structurally, so one equality covers every field there is, including the
    fields added after this test was written.
    """
    record = fully_populated(configured_run(repo))

    repo.finish(record, RunState.VALIDATING)

    assert repo.get(RunId("run-0001")) == record


def test_the_round_trip_record_leaves_no_field_at_its_default(
    repo: SqliteRunRepository,
) -> None:
    """The hole in the test above: a field whose two sides are both None.

    An equality check cannot tell a stored value from an unstored default when
    the fixture never set the field. This makes the fixture prove it moved.
    """
    record = fully_populated(configured_run(repo))

    for spec in fields(record):
        if spec.name in DERIVED_FIELDS:
            continue
        value = getattr(record, spec.name)
        if spec.default is not MISSING:
            assert value != spec.default, f"{spec.name} is still its default"
        elif spec.default_factory is not MISSING:
            assert value != spec.default_factory(), f"{spec.name} is still its default"


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
                agent_ms=1100.5,
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


def test_the_stored_environment_is_the_one_validation_ran_on(
    repo: SqliteRunRepository,
) -> None:
    """finish() must rewrite the environment columns create() could only guess.

    create() runs before any container exists, so the service writes
    arch 'unknown' and pid1 False -- there is genuinely nothing better to write
    yet. finish() never rewrote them, so every stored run kept the placeholder
    for ever: af show reported arch=unknown pid1=False for a run that measured
    on x86_64 as PID 1, while validation_reports, one join away, held the truth.

    The row is created with the placeholder on purpose. Reusing
    advance_to_validating() would create it with 'arm64' already in place, and
    the test would then pass against the broken code -- which is exactly how
    this survived.
    """
    run = new_run()
    repo.create(base_record(run, arch="unknown", pid1=False))
    for state, at in (
        (RunState.PREPARING, EPOCH_MS + 1),
        (RunState.AGENT_RUNNING, EPOCH_MS + 2),
        (RunState.VALIDATING, EPOCH_MS + 3),
    ):
        previous = run.state
        run = run.transition_to(state, at)
        repo.advance(run, previous)

    scored = run.transition_to(RunState.SCORED, EPOCH_MS + 4)
    repo.finish(
        base_record(
            scored,
            report=replace(a_report(), arch="x86_64", pid1=True),
            score=a_score(),
            outcome=AgentOutcome.CLEAN_PATCH,
            patch_is_empty=False,
            runner_version="afrunner in docker image afrunner:local",
            arch="x86_64",
            pid1=True,
        ),
        RunState.VALIDATING,
    )

    stored = repo.get(RunId("run-0001"))
    assert stored is not None
    assert stored.arch == "x86_64"
    assert stored.pid1 is True
    assert stored.runner_version == "afrunner in docker image afrunner:local"
