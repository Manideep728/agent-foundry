"""Runner-result invariants.

These are enforced in __post_init__ rather than documented because the producer
is a C++ program in another repository directory. Schema drift between two
languages is silent: it surfaces as a wrong measurement, not as an error.
"""

from __future__ import annotations

import pytest

from control_plane.domain.errors import InvalidValidationReport
from control_plane.domain.validation import (
    SCHEMA_VERSION,
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)


def stream(total: int = 0, written: int | None = None, tail: str = "") -> StreamCapture:
    written = total if written is None else written
    return StreamCapture(
        bytes_total=total,
        bytes_written=written,
        truncated=written < total,
        path="logs/000.stdout",
        tail=tail,
    )


def make_result(**overrides: object) -> CommandResult:
    fields: dict[str, object] = {
        "index": 0,
        "command": "pytest -q",
        "started_at_unix_ms": 1_700_000_000_000,
        "duration_ms": 1_200,
        "outcome": CommandOutcome.EXITED,
        "stdout": stream(),
        "stderr": stream(),
        "exit_code": 0,
    }
    fields.update(overrides)
    return CommandResult(**fields)  # type: ignore[arg-type]


def make_report(**overrides: object) -> ValidationReport:
    fields: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "runner_version": "0.1.0",
        "arch": "x86_64",
        "pid1": True,
        "shell": "/bin/dash",
        "workdir": "/work",
        "started_at_unix_ms": 1_700_000_000_000,
        "wall_ms": 1_500,
        "exit_reason": ExitReason.COMPLETED,
        "aborted": False,
        "orphans_reaped_total": 0,
        "commands": (make_result(),),
    }
    fields.update(overrides)
    return ValidationReport(**fields)  # type: ignore[arg-type]


# --- exit_code and signal are never merged -----------------------------------


def test_exit_code_is_required_exactly_when_the_command_exited() -> None:
    with pytest.raises(InvalidValidationReport, match="exit_code"):
        make_result(outcome=CommandOutcome.EXITED, exit_code=None)
    with pytest.raises(InvalidValidationReport, match="exit_code"):
        make_result(
            outcome=CommandOutcome.SIGNALED, exit_code=0, signal=11, timed_out=False
        )


def test_signal_is_required_exactly_when_the_command_was_signalled() -> None:
    with pytest.raises(InvalidValidationReport, match="signal"):
        make_result(outcome=CommandOutcome.SIGNALED, exit_code=None, signal=None)
    with pytest.raises(InvalidValidationReport, match="signal"):
        make_result(outcome=CommandOutcome.EXITED, exit_code=0, signal=9)


def test_a_signalled_command_and_a_high_exit_code_stay_distinguishable() -> None:
    # The reason the two fields are not merged into one signed integer the way
    # subprocess does it: under that convention -9 and a program legitimately
    # returning 247 collapse into the same value.
    killed = make_result(
        outcome=CommandOutcome.SIGNALED, exit_code=None, signal=9, spawn_error=None
    )
    returned_247 = make_result(outcome=CommandOutcome.EXITED, exit_code=247)

    assert killed.signal == 9
    assert killed.exit_code is None
    assert returned_247.exit_code == 247
    assert returned_247.signal is None


def test_spawn_error_is_required_exactly_when_the_command_never_started() -> None:
    with pytest.raises(InvalidValidationReport, match="spawn_error"):
        make_result(outcome=CommandOutcome.SPAWN_FAILED, exit_code=None)
    with pytest.raises(InvalidValidationReport, match="spawn_error"):
        make_result(spawn_error="EAGAIN")


# --- the timeout / deadline race ---------------------------------------------


def test_timed_out_cannot_contradict_the_outcome() -> None:
    with pytest.raises(InvalidValidationReport, match="timed_out"):
        make_result(timed_out=True)


def test_a_timeout_must_record_that_the_deadline_fired() -> None:
    with pytest.raises(InvalidValidationReport, match="deadline_signal_sent"):
        make_result(
            outcome=CommandOutcome.TIMEOUT,
            exit_code=None,
            signal=9,
            timed_out=True,
            deadline_signal_sent=False,
        )


def test_a_clean_exit_after_the_deadline_fired_is_representable() -> None:
    # The race the schema exists to express: the child exited normally in the
    # microseconds between the deadline firing and the signal landing. The wait
    # status is the authority for reporting, so this is honestly `exited` with
    # a real exit code -- and deadline_signal_sent is what stops the scorer
    # counting it as a pass.
    result = make_result(exit_code=0, deadline_signal_sent=True)

    assert result.outcome is CommandOutcome.EXITED
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.succeeded is False


def test_killed_after_grace_implies_the_deadline_fired() -> None:
    with pytest.raises(InvalidValidationReport, match="grace"):
        make_result(killed_after_grace=True, deadline_signal_sent=False)


# --- what counts as success --------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"exit_code": 0}, True),
        ({"exit_code": 1}, False),
        ({"exit_code": 0, "deadline_signal_sent": True}, False),
        (
            {
                "outcome": CommandOutcome.NOT_RUN,
                "exit_code": None,
                "duration_ms": 0,
            },
            False,
        ),
        (
            {
                "outcome": CommandOutcome.SPAWN_FAILED,
                "exit_code": None,
                "spawn_error": "EAGAIN",
            },
            False,
        ),
    ],
)
def test_succeeded_requires_a_clean_exit_within_budget(
    overrides: dict[str, object], expected: bool
) -> None:
    assert make_result(**overrides).succeeded is expected


# --- output capture ----------------------------------------------------------


def test_truncation_flag_cannot_contradict_the_byte_counts() -> None:
    with pytest.raises(InvalidValidationReport, match="truncated"):
        StreamCapture(
            bytes_total=100, bytes_written=50, truncated=False, path="p", tail=""
        )
    with pytest.raises(InvalidValidationReport, match="truncated"):
        StreamCapture(
            bytes_total=50, bytes_written=50, truncated=True, path="p", tail=""
        )


def test_written_bytes_cannot_exceed_produced_bytes() -> None:
    # The runner keeps reading past the cap and discards, so total is what the
    # process produced and written is what reached disk. The reverse means the
    # counters disagree and neither can be trusted.
    with pytest.raises(InvalidValidationReport, match="exceeds"):
        StreamCapture(
            bytes_total=10, bytes_written=20, truncated=False, path="p", tail=""
        )


def test_a_truncated_stream_still_reports_the_full_size() -> None:
    captured = stream(total=5_000_000, written=1_048_576, tail="... 3 failed")
    assert captured.truncated is True
    assert captured.bytes_total == 5_000_000


# --- report level ------------------------------------------------------------


def test_an_unknown_schema_version_is_refused() -> None:
    # Refused rather than best-effort parsed: a newer runner may have changed
    # what a field means, and guessing yields a plausible wrong number.
    with pytest.raises(InvalidValidationReport, match="schema_version"):
        make_report(schema_version=SCHEMA_VERSION + 1)


def test_a_report_without_an_architecture_is_refused() -> None:
    # arch is the qemu tripwire and the reason a benchmark table can reject a
    # row produced on the wrong machine.
    with pytest.raises(InvalidValidationReport, match="arch"):
        make_report(arch="  ")


def test_report_command_indices_must_be_contiguous() -> None:
    with pytest.raises(InvalidValidationReport, match="indices"):
        make_report(commands=(make_result(index=0), make_result(index=2)))


def test_an_empty_report_is_refused() -> None:
    with pytest.raises(InvalidValidationReport, match="at least one command"):
        make_report(commands=())


def test_a_valid_report_round_trips_its_fields() -> None:
    report = make_report(pid1=True, shell="/bin/dash", orphans_reaped_total=2)
    assert report.pid1 is True
    assert report.shell == "/bin/dash"
    assert report.orphans_reaped_total == 2
    assert report.command_at(0).command == "pytest -q"
