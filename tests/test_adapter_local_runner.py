"""The local runner.

Two kinds of test. The outcome mapping is pure and is tested exhaustively --
it is what matters and it must not be flaky. The process behaviour is tested
with wide margins, as a smoke test: a timing-sensitive test in a project about
reproducibility is a bad look and an avoidable one.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from control_plane.adapters.local_runner import (
    MAX_LOG_BYTES,
    RUNNER_VERSION,
    LocalRunner,
    decide_outcome,
)
from control_plane.domain.ids import RunId
from control_plane.domain.validation import CommandOutcome, ExitReason
from control_plane.services.ports import ValidationRequest


class RealClock:
    """The runner measures real durations, so these tests use a real clock."""

    def now_unix_ms(self) -> int:
        import time

        return int(time.time() * 1000)

    def monotonic_ms(self) -> int:
        import time

        return int(time.monotonic() * 1000)


def request_for(
    tmp_path: Path,
    commands: tuple[str, ...],
    per_command_ms: int = 30_000,
    total_ms: int = 120_000,
    stop_on_failure: bool = False,
) -> ValidationRequest:
    workdir = tmp_path / "repo"
    workdir.mkdir(exist_ok=True)
    out = tmp_path / "out"
    return ValidationRequest(
        run_id=RunId("run-0001"),
        workdir=workdir,
        output_dir=out,
        log_dir=out / "logs",
        commands=commands,
        per_command_timeout_ms=per_command_ms,
        total_timeout_ms=total_ms,
        stop_on_failure=stop_on_failure,
    )


# --- the mapping, exhaustively and deterministically -------------------------


def test_a_clean_exit_maps_to_exited() -> None:
    assert decide_outcome(0, False, False) == (CommandOutcome.EXITED, 0, None)


def test_a_failing_exit_keeps_its_code() -> None:
    assert decide_outcome(1, False, False) == (CommandOutcome.EXITED, 1, None)
    assert decide_outcome(247, False, False) == (CommandOutcome.EXITED, 247, None)


def test_death_by_signal_maps_to_signaled() -> None:
    # subprocess reports a signal death as a negative returncode. exit_code and
    # signal stay separate fields so 247 and -9 cannot be confused.
    assert decide_outcome(-9, False, False) == (CommandOutcome.SIGNALED, None, 9)
    assert decide_outcome(-11, False, False) == (CommandOutcome.SIGNALED, None, 11)


def test_a_command_we_killed_maps_to_timeout() -> None:
    outcome, exit_code, signal_number = decide_outcome(-9, True, True)
    assert outcome is CommandOutcome.TIMEOUT
    assert exit_code is None
    assert signal_number == 9


def test_a_clean_exit_after_the_deadline_stays_an_honest_exit() -> None:
    # The race the contract is explicit about. The child exited on its own in
    # the window between the deadline firing and the signal landing. The wait
    # status is the authority for reporting, so this is EXITED with its real
    # code -- and the caller sets deadline_signal_sent, which is what stops it
    # counting as a pass. Rewriting it to TIMEOUT here would lose the honest
    # status and make the result depend on a microsecond race.
    assert decide_outcome(0, True, False) == (CommandOutcome.EXITED, 0, None)


def test_a_killed_command_with_a_zero_returncode_still_reports_a_signal() -> None:
    outcome, _, signal_number = decide_outcome(0, True, True)
    assert outcome is CommandOutcome.TIMEOUT
    assert signal_number == signal.SIGKILL


# --- real processes, wide margins --------------------------------------------


def test_a_passing_command_is_reported_as_a_pass(tmp_path: Path) -> None:
    report = LocalRunner(RealClock()).run(request_for(tmp_path, ("true",)))
    result = report.commands[0]

    assert result.outcome is CommandOutcome.EXITED
    assert result.exit_code == 0
    assert result.succeeded
    assert report.exit_reason is ExitReason.COMPLETED
    assert not report.aborted


def test_a_failing_command_keeps_its_exit_code(tmp_path: Path) -> None:
    report = LocalRunner(RealClock()).run(request_for(tmp_path, ("exit 3",)))
    result = report.commands[0]

    assert result.exit_code == 3
    assert not result.succeeded


def test_the_report_marks_itself_as_not_coming_from_the_real_runner(
    tmp_path: Path,
) -> None:
    # The in-data labelling. pid1 is computed, not hardcoded, and a benchmark
    # table can require pid1 to be true before admitting a row.
    report = LocalRunner(RealClock()).run(request_for(tmp_path, ("true",)))

    assert report.pid1 is False
    assert report.runner_version == RUNNER_VERSION
    assert "no isolation" in report.runner_version
    # Structurally zero: a non-PID-1 process cannot reap orphans at all.
    assert report.orphans_reaped_total == 0


def test_output_is_captured_to_per_command_files(tmp_path: Path) -> None:
    request = request_for(tmp_path, ("echo out; echo err >&2",))
    report = LocalRunner(RealClock()).run(request)
    result = report.commands[0]

    assert "out" in result.stdout.tail
    assert "err" in result.stderr.tail
    assert (request.log_dir / result.stdout.path).is_file()


def test_oversized_output_is_truncated_and_says_so(tmp_path: Path) -> None:
    # Without a cap, `truncated` would always be False and an entire branch of
    # the schema would have no producer.
    size = MAX_LOG_BYTES + 50_000
    request = request_for(tmp_path, (f"head -c {size} /dev/zero | tr '\\0' 'x'",))
    result = LocalRunner(RealClock()).run(request).commands[0]

    assert result.stdout.truncated
    assert result.stdout.bytes_total >= size
    assert result.stdout.bytes_written == MAX_LOG_BYTES


def test_a_command_that_blows_its_budget_times_out(tmp_path: Path) -> None:
    request = request_for(tmp_path, ("sleep 30",), per_command_ms=300)
    result = LocalRunner(RealClock()).run(request).commands[0]

    assert result.outcome is CommandOutcome.TIMEOUT
    assert result.timed_out
    assert result.deadline_signal_sent
    assert not result.succeeded


def test_a_backgrounded_grandchild_does_not_hold_the_run_open(
    tmp_path: Path,
) -> None:
    # D5's caveat: dash optimises `sh -c '<simple command>'` into a bare exec,
    # so a test written as `sh -c 'sleep 30'` creates no grandchild and would
    # pass for the wrong reason. A compound command forces a real process
    # layer. Without start_new_session plus a group kill, the grandchild
    # survives and this wedges.
    request = request_for(tmp_path, ("sleep 30 & wait",), per_command_ms=300)
    result = LocalRunner(RealClock()).run(request).commands[0]

    assert result.outcome is CommandOutcome.TIMEOUT
    assert result.duration_ms < 20_000


def test_exhausting_the_total_budget_leaves_later_commands_not_run(
    tmp_path: Path,
) -> None:
    # total_ms equals per_command_ms so the first command alone exhausts the
    # budget. An earlier version left 100ms spare, which `true` fitted inside
    # -- so nothing was skipped and the test asserted against a run that had
    # legitimately completed.
    request = request_for(
        tmp_path, ("sleep 5", "true", "true"), per_command_ms=300, total_ms=300
    )
    report = LocalRunner(RealClock()).run(request)

    assert report.exit_reason is ExitReason.TOTAL_TIMEOUT
    assert report.aborted
    assert report.commands[1].outcome is CommandOutcome.NOT_RUN
    assert report.commands[2].outcome is CommandOutcome.NOT_RUN


def test_stop_on_failure_skips_the_rest(tmp_path: Path) -> None:
    request = request_for(tmp_path, ("exit 1", "true"), stop_on_failure=True)
    report = LocalRunner(RealClock()).run(request)

    assert report.commands[0].exit_code == 1
    assert report.commands[1].outcome is CommandOutcome.NOT_RUN


def test_commands_run_in_the_requested_directory(tmp_path: Path) -> None:
    request = request_for(tmp_path, ("pwd",))
    (request.workdir / "marker.txt").write_text("here\n")
    report = LocalRunner(RealClock()).run(request_for(tmp_path, ("cat marker.txt",)))

    assert report.commands[0].exit_code == 0
    assert "here" in report.commands[0].stdout.tail


def test_indices_match_the_order_commands_were_given(tmp_path: Path) -> None:
    # The index-to-role mapping the orchestrator relies on lives entirely in
    # this ordering.
    report = LocalRunner(RealClock()).run(
        request_for(tmp_path, ("echo zero", "echo one", "echo two"))
    )

    assert [c.index for c in report.commands] == [0, 1, 2]
    assert "one" in report.commands[1].stdout.tail


@pytest.mark.parametrize("command", ["true", "exit 1", "sleep 0"])
def test_every_report_satisfies_the_domain_invariants(
    tmp_path: Path, command: str
) -> None:
    # ValidationReport and CommandResult validate in __post_init__, so merely
    # constructing these proves the runner fills the contract consistently.
    report = LocalRunner(RealClock()).run(request_for(tmp_path, (command,)))
    assert report.schema_version == 1


def test_a_command_that_obeys_sigterm_is_still_a_timeout(tmp_path: Path) -> None:
    # Regression. `sleep` dies on SIGTERM inside the grace period, so the
    # runner never escalates to SIGKILL. An earlier version read "did not need
    # SIGKILL" as "we did not kill it" and reported SIGNALED -- a command that
    # blew its deadline looked like a spontaneous crash, and the timeout rate
    # would have been silently zero.
    request = request_for(tmp_path, ("sleep 30",), per_command_ms=300)
    result = LocalRunner(RealClock()).run(request).commands[0]

    assert result.outcome is CommandOutcome.TIMEOUT
    assert result.deadline_signal_sent
    assert not result.killed_after_grace  # SIGTERM was enough


def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period(
    tmp_path: Path,
) -> None:
    # The other side of the same distinction, and the reason the two facts are
    # recorded separately.
    request = request_for(tmp_path, ('trap "" TERM; sleep 30',), per_command_ms=300)
    result = LocalRunner(RealClock()).run(request).commands[0]

    assert result.outcome is CommandOutcome.TIMEOUT
    assert result.deadline_signal_sent
    assert result.killed_after_grace


def test_the_local_runner_reports_no_image_digest() -> None:
    """None, never a sentinel string.

    "" or "none" would satisfy a benchmark filter of `image_digest IS NOT
    NULL`, letting a run with no isolation at all into a table of measured
    ones -- the same failure `isolated` and `pid1` exist to prevent.
    """
    assert LocalRunner(RealClock()).image_digest is None
