"""Running validation commands directly on the host.

**This is not an isolation boundary and must never be treated as one.**

It exists so the pipeline can be exercised before the C++ supervisor does. It
runs commands as the current user, on the real filesystem, with the real
network, and with no resource limits at all. A validation command here can read
the developer's home directory and reach the internet. Point it only at
fixtures whose contents are already trusted -- never at an OSS fixture, and
never at a patch from a model you are actually evaluating.

Every report it produces carries `pid1: false` and a `runner_version` naming
it, so a row from this runner is identifiable in the data rather than only in
the documentation.

What it deliberately does not do, and why: no output cap during execution (it
redirects to files, which cannot deadlock, instead of draining pipes -- the
pipe-buffer problem is the C++ runner's to solve properly), no resource
limits, no network namespace, and no measurement of `group_escaped`. That last
one it *could* observe, but publishing a number invites comparison against a
value the real runner will produce under entirely different namespace
semantics, so it reports nothing rather than something misleading.

What it does do: a real wall-clock deadline, a real process group so a
backgrounded grandchild cannot hold the run open, and real truncation
accounting.
"""

from __future__ import annotations

import contextlib
import os
import platform
import signal
import subprocess
from pathlib import Path

from control_plane.domain.ports import Clock
from control_plane.domain.validation import (
    SCHEMA_VERSION,
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)
from control_plane.services.ports import ValidationRequest

RUNNER_ID = "local-subprocess"
RUNNER_VERSION = "local-subprocess 0.1.0 (no isolation)"

MAX_LOG_BYTES = 256 * 1024
TAIL_BYTES = 4096
GRACE_MS = 5_000


def decide_outcome(
    returncode: int, deadline_fired: bool, killed_by_us: bool
) -> tuple[CommandOutcome, int | None, int | None]:
    """Map a wait status onto the contract's outcome vocabulary.

    Pure, and separated from the process handling on purpose: the *mapping* is
    what matters and it must be testable without racing a real process, while
    the race itself is only worth a smoke test.

    The subtle case is `deadline_fired and not killed_by_us`: the child exited
    on its own in the window between the deadline firing and the signal
    landing. The wait status is the authority for reporting, so that is an
    honest `EXITED` with its real exit code -- and the caller records
    `deadline_signal_sent`, which is what stops the scorer counting it as a
    pass.
    """
    if killed_by_us:
        return CommandOutcome.TIMEOUT, None, abs(returncode) or signal.SIGKILL
    if returncode < 0:
        return CommandOutcome.SIGNALED, None, -returncode
    return CommandOutcome.EXITED, returncode, None


def _capture(path: Path, cap: int = MAX_LOG_BYTES) -> StreamCapture:
    """Account for one captured stream, truncating the file if it is oversized.

    `bytes_total` is what the process produced; `bytes_written` is what was
    kept. Recording both is what makes a truncated log honest rather than
    merely short.
    """
    if not path.exists():
        return StreamCapture(
            bytes_total=0, bytes_written=0, truncated=False, path=path.name, tail=""
        )

    total = path.stat().st_size
    if total > cap:
        with path.open("rb") as handle:
            kept = handle.read(cap)
        path.write_bytes(kept)
        written = cap
    else:
        written = total

    with path.open("rb") as handle:
        # The tail, because test runners put their summaries at the end. A head
        # excerpt of a large log is collection noise.
        handle.seek(max(0, written - TAIL_BYTES))
        tail_bytes = handle.read()

    return StreamCapture(
        bytes_total=total,
        bytes_written=written,
        truncated=written < total,
        path=path.name,
        tail=tail_bytes.decode("utf-8", errors="replace"),
    )


def _terminate_group(process: subprocess.Popen[bytes], grace_ms: int) -> bool:
    """SIGTERM the group, then SIGKILL it. Returns whether we killed it.

    The group, not the process. `sh -c 'pytest &'` leaves a grandchild that
    would otherwise survive and hold the run open. This is hygiene, not
    isolation -- on the host it cannot contain anything, it only stops a
    backgrounded process wedging the developer's machine.
    """
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return False

    for signal_number in (signal.SIGTERM, signal.SIGCONT):
        # SIGCONT because a stopped process cannot run to handle SIGTERM, and
        # the whole grace period would be spent waiting on one that never can.
        try:
            os.killpg(group, signal_number)
        except ProcessLookupError:
            return False

    try:
        process.wait(timeout=grace_ms / 1000)
        return False
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        return False
    # A process in uninterruptible sleep does not die on SIGKILL. The real
    # runner reports that as group_escaped; here we simply stop waiting.
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2)
    return True


class LocalRunner:
    """A `ValidationRunner` with no isolation. Development only."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    @property
    def runner_id(self) -> str:
        return RUNNER_ID

    @property
    def runner_version(self) -> str:
        return RUNNER_VERSION

    @property
    def isolated(self) -> bool:
        return False

    def run(self, request: ValidationRequest) -> ValidationReport:
        request.log_dir.mkdir(parents=True, exist_ok=True)

        started_at = self._clock.now_unix_ms()
        run_started = self._clock.monotonic_ms()
        results: list[CommandResult] = []
        exit_reason = ExitReason.COMPLETED
        stop = False

        for index, command in enumerate(request.commands):
            elapsed = self._clock.monotonic_ms() - run_started
            remaining = request.total_timeout_ms - elapsed

            if stop or remaining <= 0:
                if remaining <= 0:
                    exit_reason = ExitReason.TOTAL_TIMEOUT
                results.append(self._not_run(index, command))
                continue

            budget = min(request.per_command_timeout_ms, remaining)
            result = self._run_one(request, index, command, budget)
            results.append(result)

            if request.stop_on_failure and not result.succeeded:
                stop = True

        return ValidationReport(
            schema_version=SCHEMA_VERSION,
            runner_version=RUNNER_VERSION,
            # Not normalised: arm64 and x86_64 are different platforms, and D2
            # depends on being able to tell them apart.
            arch=platform.machine(),
            # Computed, not hardcoded. It is False here, and that is the
            # clearest in-data marker that this row is not from the real runner.
            pid1=os.getpid() == 1,
            shell=os.path.realpath("/bin/sh"),
            workdir=str(request.workdir),
            started_at_unix_ms=started_at,
            wall_ms=self._clock.monotonic_ms() - run_started,
            exit_reason=exit_reason,
            aborted=exit_reason is not ExitReason.COMPLETED,
            # Structurally zero: a process that is not PID 1 cannot reap
            # orphans, they reparent to host init. This is the field that shows
            # most plainly why the real runner has to be PID 1.
            orphans_reaped_total=0,
            commands=tuple(results),
        )

    def _not_run(self, index: int, command: str) -> CommandResult:
        empty = StreamCapture(
            bytes_total=0, bytes_written=0, truncated=False, path="", tail=""
        )
        return CommandResult(
            index=index,
            command=command,
            started_at_unix_ms=self._clock.now_unix_ms(),
            duration_ms=0,
            outcome=CommandOutcome.NOT_RUN,
            stdout=empty,
            stderr=empty,
        )

    def _run_one(
        self, request: ValidationRequest, index: int, command: str, budget_ms: int
    ) -> CommandResult:
        stdout_path = request.log_dir / f"cmd-{index:03d}.stdout.log"
        stderr_path = request.log_dir / f"cmd-{index:03d}.stderr.log"

        started_at = self._clock.now_unix_ms()
        started = self._clock.monotonic_ms()
        deadline_fired = False
        killed = False

        # Redirected to files rather than pipes. A pipe would need draining to
        # avoid the 64 KiB deadlock, and solving that properly is the C++
        # runner's job; files cannot deadlock.
        with (
            stdout_path.open("wb") as out,
            stderr_path.open("wb") as err,
            subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=request.workdir,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            ) as process,
        ):
            try:
                process.wait(timeout=budget_ms / 1000)
            except subprocess.TimeoutExpired:
                deadline_fired = True
                killed = _terminate_group(process, GRACE_MS)

        duration_ms = self._clock.monotonic_ms() - started
        returncode = process.returncode if process.returncode is not None else 0
        outcome, exit_code, signal_number = decide_outcome(
            returncode, deadline_fired, killed
        )

        return CommandResult(
            index=index,
            command=command,
            started_at_unix_ms=started_at,
            duration_ms=duration_ms,
            outcome=outcome,
            stdout=_capture(stdout_path),
            stderr=_capture(stderr_path),
            exit_code=exit_code,
            signal=signal_number,
            timed_out=outcome is CommandOutcome.TIMEOUT,
            deadline_signal_sent=deadline_fired,
            killed_after_grace=killed,
            group_escaped=False,
            drain_deadline_hit=False,
            orphans_reaped=0,
        )
