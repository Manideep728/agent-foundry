"""Run lifecycle: transitions, failure provenance, and recovery.

Table-driven over every (state, target) pair rather than spot-checked, so a
state added later cannot quietly inherit permissive behaviour.
"""

from __future__ import annotations

import itertools

import pytest

from control_plane.domain.errors import InvalidTransition
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.outcomes import FailureCategory
from control_plane.domain.states import (
    TERMINAL_STATES,
    TRANSITIONS,
    EvaluationRun,
    HarnessFailure,
    RecoveryAction,
    RunState,
    recovery_for,
)
from tests.fakes import EPOCH_MS

HAPPY_PATH = [
    RunState.CREATED,
    RunState.PREPARING,
    RunState.AGENT_RUNNING,
    RunState.VALIDATING,
    RunState.SCORED,
]


def new_run(at: int = EPOCH_MS) -> EvaluationRun:
    return EvaluationRun.create(RunId("run-0001"), TaskId("csv-parser-001"), at)


def advance_to(state: RunState) -> EvaluationRun:
    run = new_run()
    at = EPOCH_MS
    for step in HAPPY_PATH[1 : HAPPY_PATH.index(state) + 1]:
        at += 1_000
        run = run.transition_to(step, at)
    return run


# --- the table itself --------------------------------------------------------


def test_every_state_has_a_transition_entry() -> None:
    # A state missing from the table would raise KeyError deep inside a
    # transition instead of being rejected cleanly.
    assert set(TRANSITIONS) == set(RunState)


@pytest.mark.parametrize(
    ("source", "target"), list(itertools.product(RunState, RunState))
)
def test_only_declared_transitions_are_allowed(
    source: RunState, target: RunState
) -> None:
    run = advance_to(source) if source in HAPPY_PATH else _terminal(source)
    later = run.updated_at_unix_ms + 1_000
    # failed_from is fixed to a non-terminal state rather than to `source`:
    # HarnessFailure rejects a terminal origin on construction, which would
    # raise here during setup instead of inside the assertion below. What this
    # test is about is the transition rule, not the failure's provenance.
    failure = (
        HarnessFailure(FailureCategory.RUNNER_FAILED, failed_from=RunState.VALIDATING)
        if target is RunState.FAILED
        else None
    )

    if target in TRANSITIONS[source]:
        moved = run.transition_to(target, later, failure=failure)
        assert moved.state is target
    else:
        with pytest.raises(InvalidTransition):
            run.transition_to(target, later, failure=failure)


def _terminal(state: RunState) -> EvaluationRun:
    run = advance_to(RunState.VALIDATING)
    at = run.updated_at_unix_ms + 1_000
    if state is RunState.SCORED:
        return run.transition_to(RunState.SCORED, at)
    if state is RunState.FAILED:
        return run.fail(FailureCategory.RUNNER_FAILED, at)
    if state is RunState.ABORTED:
        return run.abort(at)
    raise AssertionError(f"{state} is not terminal")


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_have_no_outgoing_transitions(state: RunState) -> None:
    # Without this, a retry path could re-open a SCORED run and overwrite a
    # real measurement with a second attempt.
    assert TRANSITIONS[state] == frozenset()
    run = _terminal(state)
    assert run.is_terminal
    with pytest.raises(InvalidTransition, match="terminal"):
        run.transition_to(RunState.VALIDATING, run.updated_at_unix_ms + 1)


def test_the_happy_path_runs_end_to_end() -> None:
    run = advance_to(RunState.SCORED)
    assert run.state is RunState.SCORED
    assert run.failure is None
    assert run.updated_at_unix_ms > run.created_at_unix_ms


# --- failure provenance ------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        RunState.CREATED,
        RunState.PREPARING,
        RunState.AGENT_RUNNING,
        RunState.VALIDATING,
    ],
)
def test_failing_records_the_state_it_failed_from(state: RunState) -> None:
    # The number this exists to support: which phase harness failures cluster
    # in. Recording only the reason throws that away.
    run = advance_to(state)
    failed = run.fail(FailureCategory.INFRASTRUCTURE_FAILED, run.updated_at_unix_ms + 1)

    assert failed.state is RunState.FAILED
    assert failed.failure is not None
    assert failed.failure.failed_from is state
    assert failed.failure.kind is FailureCategory.INFRASTRUCTURE_FAILED


def test_a_failed_run_without_a_reason_cannot_be_constructed() -> None:
    with pytest.raises(InvalidTransition, match="without a reason"):
        EvaluationRun(
            run_id=RunId("run-0001"),
            task_id=TaskId("t"),
            state=RunState.FAILED,
            created_at_unix_ms=EPOCH_MS,
            updated_at_unix_ms=EPOCH_MS,
        )


def test_a_non_failed_run_cannot_carry_a_reason() -> None:
    with pytest.raises(InvalidTransition, match="carries a failure reason"):
        EvaluationRun(
            run_id=RunId("run-0001"),
            task_id=TaskId("t"),
            state=RunState.SCORED,
            created_at_unix_ms=EPOCH_MS,
            updated_at_unix_ms=EPOCH_MS,
            failure=HarnessFailure(
                FailureCategory.RUNNER_FAILED, failed_from=RunState.VALIDATING
            ),
        )


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_a_failure_cannot_claim_to_come_from_a_terminal_state(
    state: RunState,
) -> None:
    with pytest.raises(InvalidTransition, match="terminal"):
        HarnessFailure(FailureCategory.RUNNER_FAILED, failed_from=state)


def test_abort_is_not_a_failure() -> None:
    # Merging the two would pollute harness reliability with every cancelled
    # run: an operator who stopped forty runs would read a red report.
    run = advance_to(RunState.AGENT_RUNNING)
    aborted = run.abort(run.updated_at_unix_ms + 1)
    assert aborted.state is RunState.ABORTED
    assert aborted.failure is None


# --- immutability and time ---------------------------------------------------


def test_transitioning_leaves_the_original_untouched() -> None:
    # Catches an aliasing bug where a store writes the post-transition object
    # under the pre-transition row.
    run = new_run()
    moved = run.transition_to(RunState.PREPARING, EPOCH_MS + 1_000)

    assert run.state is RunState.CREATED
    assert moved.state is RunState.PREPARING
    assert moved is not run


def test_a_transition_cannot_move_time_backwards() -> None:
    run = advance_to(RunState.PREPARING)
    with pytest.raises(InvalidTransition, match="before its last update"):
        run.transition_to(RunState.AGENT_RUNNING, run.updated_at_unix_ms - 1)


def test_a_run_cannot_be_updated_before_it_was_created() -> None:
    with pytest.raises(InvalidTransition, match="before it was created"):
        EvaluationRun(
            run_id=RunId("run-0001"),
            task_id=TaskId("t"),
            state=RunState.CREATED,
            created_at_unix_ms=EPOCH_MS,
            updated_at_unix_ms=EPOCH_MS - 1,
        )


# --- recovery ----------------------------------------------------------------


@pytest.mark.parametrize("state", list(RunState))
def test_every_state_has_a_recovery_action(state: RunState) -> None:
    # Iterating the enum, so a state added without a recovery entry fails the
    # build rather than leaking whatever that state leaves behind.
    assert recovery_for(state) in RecoveryAction


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RunState.CREATED, RecoveryAction.NOTHING),
        (RunState.PREPARING, RecoveryAction.DELETE_WORKSPACE),
        (RunState.AGENT_RUNNING, RecoveryAction.NOTHING),
        (RunState.VALIDATING, RecoveryAction.REMOVE_CONTAINER),
        (RunState.SCORED, RecoveryAction.NOTHING),
        (RunState.FAILED, RecoveryAction.NOTHING),
        (RunState.ABORTED, RecoveryAction.NOTHING),
    ],
)
def test_recovery_matches_what_each_state_leaves_behind(
    state: RunState, expected: RecoveryAction
) -> None:
    assert recovery_for(state) is expected


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_runs_need_no_recovery(state: RunState) -> None:
    # A finished run must never be touched by a restart scan; cleaning up after
    # a SCORED run could delete the artifacts that *are* the measurement.
    assert recovery_for(state) is RecoveryAction.NOTHING


def test_an_interrupted_run_fails_rather_than_retrying() -> None:
    # Recovery marks INTERRUPTED and stops. Retrying automatically would hide a
    # crash loop, and from AGENT_RUNNING it can spend the money a second time.
    run = advance_to(RunState.AGENT_RUNNING)
    assert run.recovery_action() is RecoveryAction.NOTHING

    recovered = run.fail(FailureCategory.INTERRUPTED, run.updated_at_unix_ms + 1)
    assert recovered.is_terminal
    assert recovered.failure is not None
    assert recovered.failure.kind is FailureCategory.INTERRUPTED
    assert recovered.failure.failed_from is RunState.AGENT_RUNNING


def test_recovering_a_recovered_run_is_rejected_not_repeated() -> None:
    # Idempotence, expressed as: the second pass finds a terminal run and has
    # nothing to do, rather than failing it twice with a new timestamp.
    run = advance_to(RunState.VALIDATING)
    recovered = run.fail(FailureCategory.INTERRUPTED, run.updated_at_unix_ms + 1)

    assert recovery_for(recovered.state) is RecoveryAction.NOTHING
    with pytest.raises(InvalidTransition):
        recovered.fail(FailureCategory.INTERRUPTED, recovered.updated_at_unix_ms + 1)
