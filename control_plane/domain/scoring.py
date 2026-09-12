"""Turning a runner report into a verdict.

Two rules carry the weight here.

**A command passed only if it exited cleanly within its budget.** Not
`exit_code == 0`. Whether a command that finishes microseconds after its
deadline is reported as `exited(0)` or as `timeout` is a race between the
process and the signal, and that race resolves differently on a fast x86-64
machine than on an emulated arm64 VM. A scoring rule that depends on it
produces a benchmark that cannot be reproduced -- and reproducibility is the
product. `deadline_signal_sent` is the deterministic fact (did *we* decide the
budget was over?), so it is the one scoring may depend on.

**Incomplete evidence is a harness failure, not a low score.** A command that
never ran cannot be said to have failed; the patch was never given the chance.
A command whose `fork` failed with EAGAIN says something about the host's
process limit, not about the patch. Scoring either as "the agent failed" is
the same class of error as reading exit code 137 as a test failure, and it
would systematically depress the resolve rate on the smaller machine -- from
which one would eventually "discover" that the agent is worse on arm64.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.task import CommandRole, TaskDefinition
from control_plane.domain.validation import (
    CommandOutcome,
    CommandResult,
    ValidationReport,
)


@dataclass(frozen=True, slots=True)
class ReportMismatch:
    """Why a report cannot be scored against a task.

    Returned rather than raised, so a service can turn it into a FAILED run
    with a reason. `score()` additionally raises on a mismatched report, so a
    service that forgets to check crashes instead of producing a number.
    """

    reason: str
    category: FailureCategory


def check_report_matches_task(
    task: TaskDefinition, report: ValidationReport
) -> ReportMismatch | None:
    """Return why this report cannot be scored, or None if it can."""
    if len(report.commands) != len(task.commands):
        return ReportMismatch(
            f"report has {len(report.commands)} commands, task declares "
            f"{len(task.commands)}",
            FailureCategory.VALIDATION_INCOMPLETE,
        )

    for spec, result in zip(task.commands, report.commands, strict=True):
        # Index alignment is the whole basis for knowing a result's role. A
        # shift scores command 3's result against command 4's role, silently
        # inverting the verdict.
        if spec.index != result.index:
            return ReportMismatch(
                f"command index {result.index} does not match declared {spec.index}",
                FailureCategory.VALIDATION_INCOMPLETE,
            )
        # Catches a task edited between the run and the scoring.
        if spec.command != result.command:
            return ReportMismatch(
                f"command {spec.index} was {result.command!r} in the report but "
                f"is {spec.command!r} in the task",
                FailureCategory.VALIDATION_INCOMPLETE,
            )

    if report.aborted:
        return ReportMismatch(
            "the runner was aborted before finishing",
            FailureCategory.VALIDATION_INCOMPLETE,
        )

    for result in report.commands:
        if result.outcome is CommandOutcome.NOT_RUN:
            return ReportMismatch(
                f"command {result.index} never ran, so there is no evidence about it",
                FailureCategory.VALIDATION_INCOMPLETE,
            )
        if result.outcome is CommandOutcome.SPAWN_FAILED:
            # fork or exec failed: ENOMEM, EAGAIN against --pids-limit, or no
            # shell in the image. None of those are the patch's doing.
            return ReportMismatch(
                f"command {result.index} never started ({result.spawn_error})",
                FailureCategory.RUNNER_FAILED,
            )

    return None


@dataclass(frozen=True, slots=True)
class Score:
    """What a completed validation showed."""

    resolved: bool
    regressed: bool
    fail_to_pass_passed: int
    fail_to_pass_total: int
    pass_to_pass_passed: int
    pass_to_pass_total: int
    timed_out_commands: int

    def __post_init__(self) -> None:
        # resolved and regressed are not complements -- the common case is
        # neither, a patch that fixed nothing and broke nothing. But they
        # cannot both hold, and asserting it catches an any/all inversion
        # during a refactor, which is otherwise invisible because both versions
        # return a plausible boolean.
        if self.resolved and self.regressed:
            raise PreconditionViolated("a run cannot be both resolved and regressed")
        if self.resolved and self.fail_to_pass_passed != self.fail_to_pass_total:
            raise PreconditionViolated(
                "resolved requires every fail-to-pass command to have passed"
            )
        if self.regressed == (self.pass_to_pass_passed == self.pass_to_pass_total):
            raise PreconditionViolated("regressed contradicts the pass-to-pass counts")

    @property
    def regression_coverage(self) -> int:
        """How many commands could have detected a regression.

        Surfaced because zero is legal and means the task cannot detect one --
        a reader should be able to see that rather than infer it.
        """
        return self.pass_to_pass_total


def score(task: TaskDefinition, report: ValidationReport) -> Score:
    """Score a complete report against its task.

    Raises `PreconditionViolated` if the report does not match the task. Call
    `check_report_matches_task` first if you need to handle that as a run
    outcome rather than as a bug.
    """
    mismatch = check_report_matches_task(task, report)
    if mismatch is not None:
        raise PreconditionViolated(
            f"{task.task_id} cannot be scored: {mismatch.reason}"
        )

    by_role: dict[CommandRole, list[CommandResult]] = {role: [] for role in CommandRole}
    for spec, result in zip(task.commands, report.commands, strict=True):
        by_role[spec.role].append(result)

    f2p = by_role[CommandRole.FAIL_TO_PASS]
    p2p = by_role[CommandRole.PASS_TO_PASS]

    f2p_passed = sum(1 for r in f2p if r.succeeded)
    p2p_passed = sum(1 for r in p2p if r.succeeded)
    regressed = p2p_passed != len(p2p)

    return Score(
        # TaskDefinition refuses to construct without a fail-to-pass command,
        # so `all([])` cannot make an empty patch resolve every task here.
        resolved=f2p_passed == len(f2p) and not regressed,
        regressed=regressed,
        fail_to_pass_passed=f2p_passed,
        fail_to_pass_total=len(f2p),
        pass_to_pass_passed=p2p_passed,
        pass_to_pass_total=len(p2p),
        timed_out_commands=sum(1 for r in report.commands if r.deadline_signal_sent),
    )


def classify(result: Score, *, patch_is_empty: bool) -> AgentOutcome:
    """Summarise a score as a single outcome.

    Precedence, highest first, because more than one can be true at once:

    1. an empty patch -- the agent changed nothing, whatever the tests say
    2. a regression -- trading one failure for another is not a success
    3. a timeout -- the patch may hang rather than be wrong, and timeout rate
       is reported separately
    4. tests still failing
    5. clean

    `AgentOutcome.MALFORMED_OUTPUT` is not produced here. It describes the
    model's response rather than the validation, so the agent adapter sets it.
    """
    if patch_is_empty:
        return AgentOutcome.NO_PATCH_PRODUCED
    if result.regressed:
        return AgentOutcome.PATCH_WITH_REGRESSIONS
    if not result.resolved and result.timed_out_commands > 0:
        return AgentOutcome.VALIDATION_TIMEOUT
    if not result.resolved:
        return AgentOutcome.TESTS_STILL_FAILING
    return AgentOutcome.CLEAN_PATCH


def is_task_defect(result: Score, *, patch_is_empty: bool) -> bool:
    """Whether this run proves the task itself is broken.

    A patch that changes nothing and still resolves the task means the
    fail-to-pass commands were already passing on the unmodified fixture, so
    the task measures nothing and every agent scores a free success on it.
    Checked on every run, which makes it a continuous audit of the benchmark
    rather than a one-off review.
    """
    return patch_is_empty and result.resolved
