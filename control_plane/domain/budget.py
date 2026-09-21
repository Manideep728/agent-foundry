"""Spend ceilings, in whole micro-dollars.

A ceiling is a stop rule, not a failure. Reaching one means the harness ended
the work it was doing and reported what it had, exactly as the turn budget
already does. It does not mean the harness broke, and it must not remove the
run from the benchmark -- a run that spent money and produced nothing is
precisely what a spend figure is for.

**What a ceiling cannot promise.** On a metered API the cost of the next call
is unknown until it returns, so the rule here is "stop once spend has reached
the limit", and the overshoot is bounded by one call. That is why `cost_micros`
rounds up per line item: every estimate leans towards spending less. A promise
of "never exceeds" would be a lie the first time a long reply came back.

The two ceilings compose deliberately. A benchmark refuses to start a task it
could not afford to *finish*, using the per-run ceiling as the worst case for
that task, so the suite's overshoot is zero rather than one run. Two independent
numbers would let a benchmark stop halfway through a task it had already paid
to start.
"""

from __future__ import annotations

from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.model import MicroUsd


def validate_ceiling(limit: MicroUsd | None, name: str) -> None:
    """A ceiling is absent or positive, never zero and never negative.

    Zero is refused rather than treated as "spend nothing", because the two
    readings of it are opposite: a caller means either "do not spend" or "no
    limit", and a harness that guesses will one day guess the expensive way.
    """
    if limit is None:
        return
    if limit <= 0:
        raise PreconditionViolated(
            f"{name} must be a positive number of micro-USD, got {limit}. "
            f"Omit it for no ceiling."
        )


def is_exhausted(spent: MicroUsd, limit: MicroUsd | None) -> bool:
    """Whether spending has reached the ceiling.

    Reached, not exceeded. The call that would take spending past the limit has
    not been made yet, and refusing to make it is the whole point.
    """
    if limit is None:
        return False
    return spent >= limit


def can_start_another_run(
    spent: MicroUsd,
    benchmark_limit: MicroUsd | None,
    run_limit: MicroUsd | None,
) -> bool:
    """Whether a benchmark can afford one more task.

    The per-run ceiling is the worst case for the task about to start, so a
    suite never begins work it would have to abandon. With no per-run ceiling
    the worst case is unbounded and the only honest answer is whether anything
    remains at all -- which is why a hosted run is required to name one.
    """
    if benchmark_limit is None:
        return True
    if spent >= benchmark_limit:
        return False
    if run_limit is None:
        return True
    return spent + run_limit <= benchmark_limit
