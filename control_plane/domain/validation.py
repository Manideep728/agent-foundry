"""Typed mirror of the runner's `result.json`.

Until `docs/RUNNER_CONTRACT.md` exists (a Phase 4 prerequisite), this module is
the contract. The C++ supervisor writes the document below; these types are the
only thing that reads it, and the invariants are enforced in `__post_init__`
rather than merely documented -- schema drift between two languages is silent
otherwise, and shows up as a wrong measurement rather than an error.

Top level::

    schema_version        int    rejected unless known
    runner_version        str
    arch                  str    from uname(); the qemu tripwire, and the
                                 reason a benchmark table can refuse a row
                                 produced on the wrong architecture
    pid1                  bool   getpid() == 1; makes the PID-1 obligations
                                 observable in data rather than assumed
    shell                 str    which /bin/sh actually ran the commands
    workdir               str
    started_at_unix_ms    int    realtime -- orders runs
    wall_ms               int    monotonic -- measures duration (D12)
    exit_reason           str
    aborted               bool
    orphans_reaped_total  int
    commands              list

Per command::

    index                 int    line number in the command file
    command               str
    started_at_unix_ms    int
    duration_ms           int
    outcome               str    exited | signaled | timeout | spawn_failed
                                 | not_run
    exit_code             int?   set iff exited
    signal                int?   set iff signaled or timeout
    timed_out             bool   == (outcome is timeout)
    deadline_signal_sent  bool   the deadline fired, whatever happened next
    killed_after_grace    bool   SIGTERM grace elapsed, SIGKILL was sent
    group_escaped         bool   the process group did not die; SIGKILL
                                 cannot kill a process in uninterruptible
                                 sleep, and that has to be reported, not hung
                                 on
    drain_deadline_hit    bool   output was still arriving after the child was
                                 reaped, and the drain was cut short
    orphans_reaped        int
    spawn_error           str?   set iff spawn_failed
    stdout/stderr         bytes_total, bytes_written, truncated, path, tail

The scoring rule that depends on this shape: **the wait status is the authority
for reporting, the deadline is the authority for scoring.** A command that
exits cleanly microseconds after the deadline fired is honestly reported as
`exited` with a real exit code, and `deadline_signal_sent` is what stops it
being counted as a pass. See `scoring.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from control_plane.domain.errors import InvalidValidationReport

SCHEMA_VERSION = 1


class CommandOutcome(StrEnum):
    """How a command finished. Every consumer switches on this."""

    EXITED = "exited"
    """Ran to completion. `exit_code` says whether it succeeded."""

    SIGNALED = "signaled"
    """Killed by a signal the runner did not send (a crash, or an outside kill)."""

    TIMEOUT = "timeout"
    """Exceeded its deadline and was terminated by the runner."""

    SPAWN_FAILED = "spawn_failed"
    """Never started. fork or exec failed; `spawn_error` says why."""

    NOT_RUN = "not_run"
    """Skipped -- an earlier command failed under --stop-on-failure, or the
    run was aborted before reaching this one."""


class ExitReason(StrEnum):
    """Why the runner itself stopped."""

    COMPLETED = "completed"
    TOTAL_TIMEOUT = "total_timeout"
    SIGNAL = "signal"


@dataclass(frozen=True, slots=True)
class StreamCapture:
    """One captured output stream.

    Truncation is explicit rather than silent. Dropping output quietly would
    make a failure look like a different failure, and the byte counts are how
    a reader knows whether the log file is the whole story.
    """

    bytes_total: int
    bytes_written: int
    truncated: bool
    path: str
    tail: str

    def __post_init__(self) -> None:
        if self.bytes_total < 0 or self.bytes_written < 0:
            raise InvalidValidationReport(
                f"stream byte counts must be >= 0, got total={self.bytes_total} "
                f"written={self.bytes_written}"
            )
        # The runner keeps reading after hitting the cap and discards the
        # excess, so total is what the process produced and written is what
        # reached disk. written > total would mean the counters disagree.
        if self.bytes_written > self.bytes_total:
            raise InvalidValidationReport(
                f"bytes_written ({self.bytes_written}) exceeds bytes_total "
                f"({self.bytes_total})"
            )
        if self.truncated != (self.bytes_written < self.bytes_total):
            raise InvalidValidationReport(
                f"truncated={self.truncated} contradicts written="
                f"{self.bytes_written} of total={self.bytes_total}"
            )


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of one validation command."""

    index: int
    command: str
    started_at_unix_ms: int
    duration_ms: int
    outcome: CommandOutcome
    stdout: StreamCapture
    stderr: StreamCapture
    exit_code: int | None = None
    signal: int | None = None
    timed_out: bool = False
    deadline_signal_sent: bool = False
    killed_after_grace: bool = False
    group_escaped: bool = False
    drain_deadline_hit: bool = False
    orphans_reaped: int = 0
    spawn_error: str | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise InvalidValidationReport(
                f"command index must be >= 0, got {self.index}"
            )
        if self.duration_ms < 0:
            raise InvalidValidationReport(
                f"command {self.index} duration_ms must be >= 0, got {self.duration_ms}"
            )
        if self.orphans_reaped < 0:
            raise InvalidValidationReport(
                f"command {self.index} orphans_reaped must be >= 0"
            )

        # exit_code and signal are kept separate rather than merged into one
        # signed integer the way subprocess does it. With the negative-means-
        # signal convention, -9 and a program legitimately returning 247 become
        # confusable, and every consumer re-derives the same sign logic.
        self._require(
            (self.exit_code is not None) == (self.outcome is CommandOutcome.EXITED),
            f"exit_code must be set exactly when outcome is "
            f"{CommandOutcome.EXITED}; got outcome={self.outcome} "
            f"exit_code={self.exit_code}",
        )
        self._require(
            (self.signal is not None)
            == (self.outcome in (CommandOutcome.SIGNALED, CommandOutcome.TIMEOUT)),
            f"signal must be set exactly when outcome is "
            f"{CommandOutcome.SIGNALED} or {CommandOutcome.TIMEOUT}; got "
            f"outcome={self.outcome} signal={self.signal}",
        )
        self._require(
            (self.spawn_error is not None)
            == (self.outcome is CommandOutcome.SPAWN_FAILED),
            f"spawn_error must be set exactly when outcome is "
            f"{CommandOutcome.SPAWN_FAILED}; got outcome={self.outcome}",
        )
        # Redundant with outcome, and kept anyway: two consumers want a plain
        # boolean, and redundancy in a wire format is fine when it removes
        # branching. It cannot drift silently because of this check.
        self._require(
            self.timed_out == (self.outcome is CommandOutcome.TIMEOUT),
            f"timed_out={self.timed_out} contradicts outcome={self.outcome}",
        )
        # A timeout means the deadline fired, always. The converse does not
        # hold: the deadline can fire and the child still exit cleanly during
        # the grace period, which is the race deadline_signal_sent exists for.
        self._require(
            self.outcome is not CommandOutcome.TIMEOUT or self.deadline_signal_sent,
            f"command {self.index} timed out but deadline_signal_sent is False",
        )
        self._require(
            not self.killed_after_grace or self.deadline_signal_sent,
            f"command {self.index} was killed after grace without the deadline "
            f"having fired",
        )

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise InvalidValidationReport(message)

    @property
    def succeeded(self) -> bool:
        """Whether this command is evidence of success.

        Not simply `exit_code == 0`. A command that exits 0 after its deadline
        fired blew its budget, and counting it as a pass would inflate every
        success rate in the benchmark.
        """
        return (
            self.outcome is CommandOutcome.EXITED
            and self.exit_code == 0
            and not self.deadline_signal_sent
        )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """A complete runner result."""

    schema_version: int
    runner_version: str
    arch: str
    pid1: bool
    shell: str
    workdir: str
    started_at_unix_ms: int
    wall_ms: int
    exit_reason: ExitReason
    aborted: bool
    orphans_reaped_total: int
    commands: tuple[CommandResult, ...]

    def __post_init__(self) -> None:
        # An unknown schema version is refused rather than best-effort parsed.
        # A newer runner may have changed the meaning of a field, and guessing
        # produces a plausible wrong number instead of an error.
        if self.schema_version != SCHEMA_VERSION:
            raise InvalidValidationReport(
                f"unsupported schema_version {self.schema_version}; "
                f"this build understands {SCHEMA_VERSION}"
            )
        if not self.arch.strip():
            raise InvalidValidationReport(
                "arch must be recorded; a run that cannot name the "
                "architecture that produced it cannot enter a benchmark table"
            )
        if self.wall_ms < 0:
            raise InvalidValidationReport(f"wall_ms must be >= 0, got {self.wall_ms}")
        if self.orphans_reaped_total < 0:
            raise InvalidValidationReport("orphans_reaped_total must be >= 0")
        if not self.commands:
            raise InvalidValidationReport("a report must contain at least one command")

        expected = list(range(len(self.commands)))
        actual = [c.index for c in self.commands]
        if actual != expected:
            raise InvalidValidationReport(
                f"command indices must be {expected}, got {actual}"
            )

    def command_at(self, index: int) -> CommandResult:
        return self.commands[index]
