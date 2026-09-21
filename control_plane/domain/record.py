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

    agent_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    """Which agent, provider and model the run was configured to use.

    Three fields because no two of them determine the third: the single-shot
    agent and the tool loop can drive the same model through the same provider,
    and merging those two into one rate is the mistake a benchmark most easily
    makes. Together they are the grouping key a summary uses.

    Written when the run is created rather than derived from `invocations`
    afterwards. The invocations say what answered and stay the better evidence
    wherever there are any, but a run that failed on its way to the first call
    has none -- and those are the runs a harness-reliability figure is about.
    A configuration that could not be named for exactly the runs that failed
    would put every failure into one anonymous bucket.

    None only for a run stored before the harness recorded this. No sentinel
    string, for the reason `image_digest` gives: an unrecorded configuration
    must not satisfy a filter looking for a real one.
    """

    prompt_id: str | None = None
    """Which named prompt the agent sent, when it sent one.

    A fourth dimension of the configuration, and it earns its place the way the
    other three did. Measured on 2026-09-20, one model under two prompts goes
    from writing nothing to writing patches; merged into one row, those are a
    rate describing neither, which is the defect D20 exists to prevent.

    Deliberately not folded into the all-three-or-none rule above. The fake
    agent has a configuration and no prompt, because it asks no model anything,
    and that is a true state rather than an unrecorded one.
    """

    invocations: tuple[ModelInvocation, ...] = field(default_factory=tuple)
    total_cost_micros: MicroUsd = ZERO_MICRO_USD
    agent_ms: int | None = None
    validation_ms: int | None = None
    workspace_path: str | None = None

    image_digest: str | None = None
    """The content address of the image validation ran in.

    None for a runner that has no image, and for a run that died before any
    container started. Both are honestly "no image", and a sentinel string
    would let an unisolated development run satisfy a benchmark filter of
    `image_digest IS NOT NULL` -- the same failure `pid1` exists to prevent.

    The spelling is not checked here. Docker writes `sha256:...` and another
    runtime would write something else; this record is runner-agnostic, and the
    adapter that produced the value is the layer that knows its format.
    """

    agent_note: str | None = None
    """One line from the agent about its own output, when it has one.

    Today that means why a reply could not be read as an edit. It explains an
    outcome rather than scoring one, so nothing depends on it and a run without
    it is complete. The reply itself is far too large for a row and is kept as
    a file beside the patch.
    """

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
            if scored and self.outcome is AgentOutcome.MALFORMED_OUTPUT:
                # The one outcome the validation cannot derive. `classify`
                # never returns it, because it describes the model's reply
                # rather than the test results, so only the agent adapter can
                # know. It is still constrained rather than trusted: a reply
                # that could not be read as an edit writes nothing, so the
                # patch must be empty. Every other disagreement between the
                # stored outcome and the stored score stays a load-time
                # exception, which is the point of the check.
                self._require(
                    self.patch_is_empty,
                    "malformed_output requires an empty patch: a reply that "
                    "could not be read as an edit cannot have changed a file",
                )
            elif scored:
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

        # All three or none. A half-named configuration -- the agent without
        # the model -- is a wiring bug, and it would group as its own row in
        # every benchmark table rather than failing anywhere.
        named = (self.agent_id, self.provider_id, self.model_id)
        self._require(
            all(part is None for part in named)
            or all(part not in (None, "") for part in named),
            "a run names its whole configuration or none of it",
        )

        # None means "no image". The empty string means an adapter looked and
        # wrote down whatever it got, which is the one answer this must not
        # store as provenance.
        self._require(
            self.prompt_id != "",
            "prompt_id is a name or None, never the empty string: an empty "
            "name would satisfy a filter looking for a real one",
        )

        self._require(
            self.image_digest != "",
            "image_digest is None or a digest, never the empty string",
        )

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise PreconditionViolated(f"{self.run.run_id}: {message}")
