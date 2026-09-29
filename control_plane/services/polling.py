"""The loop shared by everything that consumes a topic one step at a time.

A worker and a collector differ in what one step does and agree on when to
stop, so the stopping lives here once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from control_plane.domain.ports import Clock


class Steps[T](Protocol):
    def step(self, timeout_s: float) -> T | None:
        """Handle at most one delivery. None means the topic was quiet."""
        ...


def poll_until[T](
    consumer: Steps[T],
    clock: Clock,
    report: Callable[[T], None],
    *,
    poll_timeout_s: float = 1.0,
    max_messages: int | None = None,
    idle_exit_ms: int | None = None,
) -> int:
    """Step until a limit is reached, and return how many deliveries were handled.

    With neither limit the loop runs until it is stopped, which is what a pool
    does. The limits exist so that a demonstration or a test ends by itself:
    `idle_exit_ms` ends the loop once the topic has been quiet that long,
    measured on the monotonic clock because it is a duration (D12).
    """
    handled = 0
    quiet_since_ms = clock.monotonic_ms()
    while max_messages is None or handled < max_messages:
        outcome = consumer.step(poll_timeout_s)
        if outcome is None:
            quiet_ms = clock.monotonic_ms() - quiet_since_ms
            if idle_exit_ms is not None and quiet_ms >= idle_exit_ms:
                break
            continue
        handled += 1
        report(outcome)
        quiet_since_ms = clock.monotonic_ms()
    return handled
