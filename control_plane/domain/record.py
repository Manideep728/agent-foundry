"""The whole of one run, as a value.

This is what storage round-trips. Its `__post_init__` runs on every
construction path, including rehydration from a database row -- which is the
untyped path a bad row would otherwise slip through, so the read path and the
validation path are the same path and there is no second validator to drift.

The invariant worth naming: `FAILED` implies no `outcome`, but **may** carry a
report and a score. `outcome` is what feeds a success-rate aggregate and must
be absent from a harness failure. A report and a score are *evidence*, and the
one failure that has them -- a task whose empty patch resolved it -- needs them
kept, or the defect is unactionable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.model import (
    ZERO_MICRO_USD,
    MicroUsd,
    ModelInvocation,
    total_micros,
)
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.scoring import Score, classify, is_task_defect
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.task import Suite
from control_plane.domain.validation import ValidationReport


@dataclass(frozen=True, slots=True)
class RunRecord:
    run: EvaluationRun
    suite: Suite
    """Denormalised onto the run because D7 forbids averaging suites, and a
    report must be able to honour that without joining a task file that may
    have changed since."""

    fixture_sha256: str
    runner_id: str
    runner_version: str
    arch: str
    pid1: bool

    report: ValidationReport | None = None
    score: Score | None = None
    outcome: AgentOutcome | None = None
    task_defect: bool = False

    patch_sha256: str | None = None
    patch_bytes: int = 0
    patch_is_empty: bool | None = None

    invocations: tuple[ModelInvocation, ...] = field(default_factory=tuple)
    total_cost_micros: MicroUsd = ZERO_MICRO_USD
    agent_ms: int | None = None
    validation_ms: int | None = None
    workspace_path: str | None = None

    def __post_init__(self) -> None:
        scored = self.run.state is RunState.SCORED

        if scored:
            self._require(
                self.report is not None
                and self.score is not None
                and self.outcome is not None,
                "a scored run must carry a report, a score and an outcome",
            )
        else:
            # A non-scored run must not carry a verdict. Without this, a service
            # could write state=SCORED-shaped data under a FAILED row and the
            # success rate would count it.
            self._require(
                self.outcome is None,
                f"a {self.run.state} run must not carry an agent outcome",
            )

        if self.score is not None and self.patch_is_empty is not None:
            # The load-bearing one. It turns "the stored outcome drifted from
            # the stored score" from a silent reporting error into a load-time
            # exception.
            if scored:
                expected = classify(self.score, patch_is_empty=self.patch_is_empty)
                self._require(
                    self.outcome is expected,
                    f"outcome {self.outcome} disagrees with the score, which "
                    f"classifies as {expected}",
                )
            expected_defect = is_task_defect(
                self.score, patch_is_empty=self.patch_is_empty
            )
            self._require(
                self.task_defect == expected_defect,
                f"task_defect={self.task_defect} disagrees with the score",
            )

        expected_total = total_micros(i.cost for i in self.invocations)
        self._require(
            self.total_cost_micros == expected_total,
            f"total_cost_micros {self.total_cost_micros} is not the sum of the "
            f"invocations ({expected_total})",
        )

        for name in ("patch_bytes", "agent_ms", "validation_ms"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise PreconditionViolated(f"{name} must be >= 0")

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise PreconditionViolated(f"{self.run.run_id}: {message}")
