"""Turning many runs into one verdict per suite.

This lives in the domain, not beside the code that prints it, for two reasons.

The arithmetic is the most float-tempting in the project -- a resolved rate, a
mean latency and a cost per run are each one careless `/` away -- and the AST
guard that forbids floats only scans `domain/`. Putting the sums anywhere else
would leave the guard watching the code that never divides while ignoring the
code that does.

And the exclusion rules are already domain rules, written in domain docstrings:
a failed run contributes to no rate, no cost total and no latency figure
(`FailureCategory`); suites are never averaged (`Suite`, D7); providers are
never blended (D6). A rule stated only inside a print function is a rule the
next reporting surface will re-implement differently.

Rendering stays in the interface. This module computes; it does not format.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.model import ZERO_MICRO_USD, MicroUsd, total_micros
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.states import RunState
from control_plane.domain.task import Suite


@dataclass(frozen=True, slots=True)
class Durations:
    """The range a set of durations covered, and how many there were.

    A range rather than a mean, for two reasons that have nothing to do with
    floats -- `//` would be legal here. Over eight tasks a mean is one slow run
    away from meaningless and hides the spread entirely, which is exactly the
    number people quote and then cannot reproduce. And every figure here is a
    duration some run actually had, traceable to that run; a mean is a number
    no run produced, and flooring it discards a remainder so it no longer
    multiplies back to the total.
    """

    fastest_ms: int
    median_ms: int
    slowest_ms: int
    counted: int


@dataclass(frozen=True, slots=True)
class Configuration:
    """Which agent, provider and model a set of runs was measuring.

    Three parts, because no two of them determine the third. The single-shot
    agent and the tool loop drive the same model through the same provider and
    score very differently, so a key of `(provider, model)` merges the one
    comparison the suite exists to make.

    Any part is None only for a run stored before the harness recorded this.
    Such a run groups on its own rather than joining a named configuration:
    "we do not know what produced these" is a different statement from any
    particular answer, and merging it into one would corrupt that row instead.
    """

    agent_id: str | None
    provider_id: str | None
    model_id: str | None

    @property
    def is_named(self) -> bool:
        """Whether this run says what produced it."""
        return self.agent_id is not None

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """A total order, so the same runs summarise in the same order.

        None sorts first, as the empty string. A report whose sections move
        between two runs over the same database cannot be diffed.
        """
        return (
            self.agent_id or "",
            self.provider_id or "",
            self.model_id or "",
        )


UNRECORDED = Configuration(None, None, None)


@dataclass(frozen=True, slots=True)
class Unmeasured:
    """A run that produced no measurement, and why."""

    task_id: str
    run_id: str
    kind: FailureCategory
    failed_from: RunState
    detail: str


@dataclass(frozen=True, slots=True)
class SuiteSummary:
    """What one suite's runs showed. One suite, never a total across them."""

    suite: Suite
    configuration: Configuration
    runs: int
    measured: int
    aborted: int

    resolved: int
    regressed: int
    without_regression_check: int

    outcomes: Mapping[AgentOutcome, int]
    unmeasured: tuple[Unmeasured, ...]

    timed_out_commands: int
    runs_with_timeouts: int

    measured_cost_micros: MicroUsd
    unmeasured_cost_micros: MicroUsd
    unmeasured_runs_that_spent: int

    agent_ms: Durations | None
    validation_ms: Durations | None

    image_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.measured + len(self.unmeasured) + self.aborted != self.runs:
            raise PreconditionViolated(
                "every run is measured, unmeasured or aborted, and exactly one"
            )
        if self.resolved + self.regressed > self.measured:
            raise PreconditionViolated(
                "resolved and regressed cannot both hold on one run"
            )
        if set(self.outcomes) != set(AgentOutcome):
            raise PreconditionViolated(
                "every outcome is a key, so a rate that is zero says so"
            )
        # The load-bearing one. An outcome added to the enum and forgotten in
        # the counter would otherwise vanish from every benchmark table without
        # anything failing.
        if sum(self.outcomes.values()) != self.measured:
            raise PreconditionViolated(
                "the outcome counts must account for every measured run"
            )


def spread(values: Iterable[int]) -> Durations | None:
    """The range a set of durations covered, or None when there were none.

    None rather than zero: "no run was measured" and "every run took 0ms" are
    different statements, and only one of them is ever true.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    return Durations(
        fastest_ms=ordered[0],
        # The upper of the two middle values when the count is even. Averaging
        # them would report a duration no run had.
        median_ms=ordered[len(ordered) // 2],
        slowest_ms=ordered[-1],
        counted=len(ordered),
    )


def configuration_of(record: RunRecord) -> Configuration:
    """Which configuration a run belongs to (D6).

    The record's own columns first. They are written before the agent runs, so
    a run that failed on its way to the model still names what was being
    measured -- and those are exactly the runs a reliability figure is about.

    The invocations are the fallback, for runs stored before the harness
    recorded the columns. They say what actually answered, which recovers the
    provider and the model honestly; the strategy that drove them was never
    written down and stays None rather than being guessed at.
    """
    if record.agent_id is not None:
        return Configuration(record.agent_id, record.provider_id, record.model_id)
    if record.invocations:
        first = record.invocations[0]
        return Configuration(None, first.provider_id, first.model_id)
    return UNRECORDED


def _distinct(values: Iterable[str | None]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if value is not None and value not in seen:
            seen.append(value)
    return tuple(seen)


def summarise(records: Iterable[RunRecord]) -> tuple[SuiteSummary, ...]:
    """One summary per suite and configuration, in a fixed order.

    Grouped rather than totalled, on both axes. D7 forbids averaging a
    synthetic fixture with a real repository and D6 forbids blending providers,
    and a function that *can* blend them eventually will -- so the grouping is
    structural rather than a rule someone has to remember.

    The configuration is half of the key, not a label printed beside one. A
    database holding a single-shot run and a tool-loop run of the same model is
    the ordinary case the moment anything reports on stored runs rather than on
    the runs one command just produced, and one `resolved 5/16` across them
    describes neither.

    Suite order comes from the enum and configuration order from `sort_key`,
    so the same set of runs summarises identically whichever one is first.
    """
    collected = tuple(records)
    summaries = []
    for suite in Suite:
        in_suite = tuple(r for r in collected if r.suite is suite)
        by_configuration: dict[Configuration, list[RunRecord]] = {}
        for record in in_suite:
            by_configuration.setdefault(configuration_of(record), []).append(record)
        for configuration in sorted(by_configuration, key=lambda c: c.sort_key):
            summaries.append(
                _summarise_one(
                    suite, configuration, tuple(by_configuration[configuration])
                )
            )
    return tuple(summaries)


def _summarise_one(
    suite: Suite, configuration: Configuration, records: tuple[RunRecord, ...]
) -> SuiteSummary:
    scored = tuple(r for r in records if r.run.state is RunState.SCORED)
    aborted = tuple(r for r in records if r.run.state is RunState.ABORTED)
    failed = tuple(r for r in records if r.run.state is RunState.FAILED)

    counts = dict.fromkeys(AgentOutcome, 0)
    for record in scored:
        if record.outcome is not None:
            counts[record.outcome] += 1

    unmeasured = tuple(
        Unmeasured(
            task_id=str(record.run.task_id),
            run_id=str(record.run.run_id),
            kind=record.run.failure.kind,
            failed_from=record.run.failure.failed_from,
            detail=record.run.failure.detail,
        )
        for record in failed
        if record.run.failure is not None
    )

    # Money spent by runs that produced nothing is reported, never added in.
    # FailureCategory is explicit that such a run contributes to no cost total,
    # and the rule is "never blend", not "never report" -- the price of harness
    # unreliability is itself worth knowing, and is invisible any other way.
    spent_without_measuring = tuple(
        r for r in records if r.run.state is not RunState.SCORED
    )

    return SuiteSummary(
        suite=suite,
        configuration=configuration,
        runs=len(records),
        measured=len(scored),
        aborted=len(aborted),
        resolved=sum(1 for r in scored if r.score is not None and r.score.resolved),
        regressed=sum(1 for r in scored if r.score is not None and r.score.regressed),
        # Surfaced because zero pass-to-pass commands is legal and means the
        # task cannot detect a regression. Without it "regressed 1/7" invites
        # the reader to conclude six tasks were checked when some could not be.
        without_regression_check=sum(
            1 for r in scored if r.score is not None and not r.score.pass_to_pass_total
        ),
        outcomes=MappingProxyType(counts),
        unmeasured=unmeasured,
        timed_out_commands=sum(
            r.score.timed_out_commands for r in scored if r.score is not None
        ),
        runs_with_timeouts=sum(
            1 for r in scored if r.score is not None and r.score.timed_out_commands
        ),
        measured_cost_micros=total_micros(r.total_cost_micros for r in scored),
        unmeasured_cost_micros=total_micros(
            r.total_cost_micros for r in spent_without_measuring
        ),
        unmeasured_runs_that_spent=sum(
            1 for r in spent_without_measuring if r.total_cost_micros > ZERO_MICRO_USD
        ),
        agent_ms=spread(r.agent_ms for r in scored if r.agent_ms is not None),
        validation_ms=spread(
            r.validation_ms for r in scored if r.validation_ms is not None
        ),
        image_digests=_distinct(r.image_digest for r in records),
    )
