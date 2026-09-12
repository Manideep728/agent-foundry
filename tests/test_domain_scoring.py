"""Scoring rules.

Two families here. The deadline rule, which decides whether the benchmark is
reproducible across machines; and the completeness rules, which decide whether
infrastructure problems get recorded as agent failures.
"""

from __future__ import annotations

import pytest

from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.ids import TaskId
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.scoring import (
    Score,
    check_report_matches_task,
    classify,
    is_task_defect,
    score,
)
from control_plane.domain.task import CommandRole, Suite, TaskDefinition
from control_plane.domain.validation import (
    SCHEMA_VERSION,
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)

EMPTY_STREAM = StreamCapture(
    bytes_total=0, bytes_written=0, truncated=False, path="logs/x", tail=""
)


def make_task(*roles: CommandRole) -> TaskDefinition:
    roles = roles or (CommandRole.FAIL_TO_PASS, CommandRole.PASS_TO_PASS)
    return TaskDefinition(
        task_id=TaskId("csv-parser-001"),
        suite=Suite.SYNTHETIC,
        description="Fix the quoted-field parser.",
        fixture_id="csv-parser",
        fixture_sha256="a" * 64,
        commands=TaskDefinition.index_commands(
            [(f"pytest -q tests/t{i}.py", role) for i, role in enumerate(roles)]
        ),
        per_command_timeout_ms=120_000,
        total_timeout_ms=600_000,
    )


def result_for(task: TaskDefinition, index: int, **overrides: object) -> CommandResult:
    fields: dict[str, object] = {
        "index": index,
        "command": task.commands[index].command,
        "started_at_unix_ms": 1_700_000_000_000,
        "duration_ms": 100,
        "outcome": CommandOutcome.EXITED,
        "stdout": EMPTY_STREAM,
        "stderr": EMPTY_STREAM,
        "exit_code": 0,
    }
    fields.update(overrides)
    return CommandResult(**fields)  # type: ignore[arg-type]


def report_for(
    task: TaskDefinition,
    results: tuple[CommandResult, ...] | None = None,
    **overrides: object,
) -> ValidationReport:
    if results is None:
        results = tuple(result_for(task, i) for i in range(len(task.commands)))
    fields: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "runner_version": "0.1.0",
        "arch": "x86_64",
        "pid1": True,
        "shell": "/bin/dash",
        "workdir": "/work",
        "started_at_unix_ms": 1_700_000_000_000,
        "wall_ms": 500,
        "exit_reason": ExitReason.COMPLETED,
        "aborted": False,
        "orphans_reaped_total": 0,
        "commands": results,
    }
    fields.update(overrides)
    return ValidationReport(**fields)  # type: ignore[arg-type]


# --- the deadline rule -------------------------------------------------------


def test_a_clean_exit_after_the_deadline_fired_does_not_count_as_a_pass() -> None:
    # The single most important test in this module. Whether this command is
    # reported as exited(0) or as timeout is a race between the process and the
    # signal, and it resolves differently on a fast machine than on a slow one.
    # Scoring on exit_code alone would make the same run resolved on the PC and
    # unresolved on the Mac, and the benchmark would not be reproducible.
    task = make_task(CommandRole.FAIL_TO_PASS)
    report = report_for(
        task,
        (result_for(task, 0, exit_code=0, deadline_signal_sent=True),),
    )

    result = score(task, report)
    assert result.resolved is False
    assert result.fail_to_pass_passed == 0
    assert result.timed_out_commands == 1


def test_a_clean_exit_within_budget_counts_as_a_pass() -> None:
    task = make_task(CommandRole.FAIL_TO_PASS)
    result = score(task, report_for(task))
    assert result.resolved is True
    assert result.timed_out_commands == 0


# --- resolved and regressed --------------------------------------------------


def test_resolved_requires_targets_to_pass_and_nothing_to_regress() -> None:
    task = make_task()
    result = score(task, report_for(task))
    assert result.resolved is True
    assert result.regressed is False


def test_a_failing_regression_test_means_regressed_and_not_resolved() -> None:
    task = make_task()
    report = report_for(
        task,
        (result_for(task, 0), result_for(task, 1, exit_code=1)),
    )
    result = score(task, report)

    assert result.regressed is True
    assert result.resolved is False
    assert result.pass_to_pass_passed == 0


def test_the_common_case_is_neither_resolved_nor_regressed() -> None:
    # The patch fixed nothing and broke nothing. resolved and regressed are not
    # complements, and treating them as such is a natural mistake.
    task = make_task()
    report = report_for(
        task,
        (result_for(task, 0, exit_code=1), result_for(task, 1)),
    )
    result = score(task, report)

    assert result.resolved is False
    assert result.regressed is False


def test_resolved_and_regressed_cannot_both_hold() -> None:
    # Catches an any/all inversion in a refactor, which is otherwise invisible
    # because both versions return a plausible boolean.
    with pytest.raises(PreconditionViolated, match="both resolved and regressed"):
        Score(
            resolved=True,
            regressed=True,
            fail_to_pass_passed=1,
            fail_to_pass_total=1,
            pass_to_pass_passed=0,
            pass_to_pass_total=1,
            timed_out_commands=0,
        )


def test_regressed_cannot_contradict_the_counts() -> None:
    with pytest.raises(PreconditionViolated, match="contradicts"):
        Score(
            resolved=False,
            regressed=True,
            fail_to_pass_passed=0,
            fail_to_pass_total=1,
            pass_to_pass_passed=2,
            pass_to_pass_total=2,
            timed_out_commands=0,
        )


def test_a_task_with_no_regression_tests_scores_but_cannot_detect_regressions() -> None:
    task = make_task(CommandRole.FAIL_TO_PASS)
    result = score(task, report_for(task))

    assert result.regressed is False
    assert result.regression_coverage == 0


# --- incomplete evidence is not a low score ----------------------------------


def test_a_command_that_never_ran_is_a_harness_failure_not_a_failed_test() -> None:
    # A docker stop during a benchmark would otherwise turn every interrupted
    # run into a row saying the agent failed.
    task = make_task()
    report = report_for(
        task,
        (
            result_for(task, 0),
            result_for(
                task, 1, outcome=CommandOutcome.NOT_RUN, exit_code=None, duration_ms=0
            ),
        ),
    )

    mismatch = check_report_matches_task(task, report)
    assert mismatch is not None
    assert mismatch.category is FailureCategory.VALIDATION_INCOMPLETE
    with pytest.raises(PreconditionViolated):
        score(task, report)


def test_a_command_that_never_started_is_a_runner_failure() -> None:
    # EAGAIN against --pids-limit is a realistic recurring event on a 1.9 GiB
    # VM. Counting it as a failed test would depress the resolve rate on one
    # machine and not the other, and the conclusion drawn would be that the
    # agent is worse on arm64.
    task = make_task()
    report = report_for(
        task,
        (
            result_for(task, 0),
            result_for(
                task,
                1,
                outcome=CommandOutcome.SPAWN_FAILED,
                exit_code=None,
                spawn_error="EAGAIN",
            ),
        ),
    )

    mismatch = check_report_matches_task(task, report)
    assert mismatch is not None
    assert mismatch.category is FailureCategory.RUNNER_FAILED


def test_an_aborted_report_is_not_scored() -> None:
    task = make_task()
    report = report_for(task, aborted=True, exit_reason=ExitReason.SIGNAL)

    assert check_report_matches_task(task, report) is not None
    with pytest.raises(PreconditionViolated):
        score(task, report)


def test_a_report_with_the_wrong_number_of_commands_is_not_scored() -> None:
    task = make_task()
    report = report_for(task, (result_for(task, 0),))
    assert check_report_matches_task(task, report) is not None


def test_a_report_whose_command_text_drifted_is_not_scored() -> None:
    # The task was edited between the run and the scoring, so the roles no
    # longer describe what actually executed.
    task = make_task()
    results = (
        result_for(task, 0, command="pytest -q tests/something_else.py"),
        result_for(task, 1),
    )
    mismatch = check_report_matches_task(task, report_for(task, results))

    assert mismatch is not None
    assert "task" in mismatch.reason


def test_a_matching_report_produces_no_mismatch() -> None:
    task = make_task()
    assert check_report_matches_task(task, report_for(task)) is None


# --- classification ----------------------------------------------------------


def clean_score(**overrides: object) -> Score:
    fields: dict[str, object] = {
        "resolved": True,
        "regressed": False,
        "fail_to_pass_passed": 1,
        "fail_to_pass_total": 1,
        "pass_to_pass_passed": 1,
        "pass_to_pass_total": 1,
        "timed_out_commands": 0,
    }
    fields.update(overrides)
    return Score(**fields)  # type: ignore[arg-type]


def test_an_empty_patch_outranks_every_other_outcome() -> None:
    assert (
        classify(clean_score(), patch_is_empty=True) is AgentOutcome.NO_PATCH_PRODUCED
    )


def test_a_regression_outranks_a_timeout() -> None:
    result = clean_score(
        resolved=False,
        regressed=True,
        pass_to_pass_passed=0,
        timed_out_commands=1,
    )
    assert classify(result, patch_is_empty=False) is AgentOutcome.PATCH_WITH_REGRESSIONS


def test_a_timeout_outranks_a_plain_failure() -> None:
    result = clean_score(resolved=False, fail_to_pass_passed=0, timed_out_commands=1)
    assert classify(result, patch_is_empty=False) is AgentOutcome.VALIDATION_TIMEOUT


def test_tests_still_failing_is_the_plain_failure() -> None:
    result = clean_score(resolved=False, fail_to_pass_passed=0)
    assert classify(result, patch_is_empty=False) is AgentOutcome.TESTS_STILL_FAILING


def test_a_clean_patch_classifies_as_clean() -> None:
    assert classify(clean_score(), patch_is_empty=False) is AgentOutcome.CLEAN_PATCH


# --- the task audit ----------------------------------------------------------


def test_an_empty_patch_that_resolves_is_a_task_defect() -> None:
    # The fail-to-pass commands were already passing on the untouched fixture,
    # so the task measures nothing and every agent scores a free success. This
    # runs on every single run, making it a continuous audit of the benchmark.
    assert is_task_defect(clean_score(), patch_is_empty=True) is True


def test_a_real_patch_that_resolves_is_not_a_defect() -> None:
    assert is_task_defect(clean_score(), patch_is_empty=False) is False


def test_an_empty_patch_that_fails_is_not_a_defect() -> None:
    result = clean_score(resolved=False, fail_to_pass_passed=0)
    assert is_task_defect(result, patch_is_empty=True) is False
