"""The spend ceilings.

Pure integer arithmetic, so the rules are tested without a model, a network or
a key. The one thing these cannot test is whether the caller consults them,
which is what the agent and CLI tests are for.
"""

from __future__ import annotations

import pytest

from control_plane.domain.budget import (
    can_start_another_run,
    is_exhausted,
    validate_ceiling,
)
from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.model import MicroUsd


def micros(value: int) -> MicroUsd:
    return MicroUsd(value)


def test_a_ceiling_is_reached_not_exceeded() -> None:
    """Spending exactly the limit stops the work.

    The alternative, stopping only once spending is strictly greater, spends
    one more call than the operator asked for every single time.
    """
    assert is_exhausted(micros(50_000), micros(50_000))
    assert not is_exhausted(micros(49_999), micros(50_000))


def test_an_absent_ceiling_never_stops_a_run() -> None:
    # Every local run today: a free model has no ceiling, and the check must
    # cost it nothing.
    assert not is_exhausted(micros(10**9), None)


def test_a_ceiling_of_zero_is_refused() -> None:
    """Zero has two opposite readings, so it may not be guessed at.

    "Spend nothing" and "no limit" cannot both be zero. A harness that picks
    one will eventually pick the expensive one.
    """
    with pytest.raises(PreconditionViolated, match="positive"):
        validate_ceiling(micros(0), "--max-run-micros")

    with pytest.raises(PreconditionViolated, match="positive"):
        validate_ceiling(micros(-1), "--max-run-micros")

    validate_ceiling(None, "--max-run-micros")
    validate_ceiling(micros(1), "--max-run-micros")


def test_a_benchmark_will_not_start_a_run_it_cannot_afford_to_finish() -> None:
    """The two ceilings are one policy, not two numbers.

    With 60_000 left and a per-run ceiling of 100_000, starting another task
    means either abandoning it part-way or blowing through the suite ceiling.
    Refusing to start is the only option that keeps both promises.
    """
    assert not can_start_another_run(
        micros(940_000), benchmark_limit=micros(1_000_000), run_limit=micros(100_000)
    )
    assert can_start_another_run(
        micros(900_000), benchmark_limit=micros(1_000_000), run_limit=micros(100_000)
    )


def test_a_benchmark_with_no_ceiling_always_starts_another() -> None:
    assert can_start_another_run(
        micros(10**9), benchmark_limit=None, run_limit=micros(100)
    )


def test_a_benchmark_with_no_run_ceiling_can_only_check_what_is_left() -> None:
    """Without a per-run ceiling the next task's worst case is unbounded.

    The honest answer is then "is there anything left", and the overshoot is
    whatever one task costs. This is why a hosted run is required to name a
    per-run ceiling rather than being given a default.
    """
    assert can_start_another_run(
        micros(999_999), benchmark_limit=micros(1_000_000), run_limit=None
    )
    assert not can_start_another_run(
        micros(1_000_000), benchmark_limit=micros(1_000_000), run_limit=None
    )
