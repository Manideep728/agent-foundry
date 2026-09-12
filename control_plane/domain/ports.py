"""Ports the domain depends on, implemented by adapters.

This is dependency inversion: the interfaces live with the code that *uses*
them, and the outer layers conform. The domain may not import `time`, `uuid` or
`random`, so anything ambient -- the current instant, a fresh identifier --
arrives through one of these.

That restriction is not bureaucracy. A state machine that calls the clock
directly produces a different result on every execution, cannot have a
timestamp pinned in a test, and records runs that cannot be replayed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from control_plane.domain.ids import RunId


@runtime_checkable
class Clock(Protocol):
    """Two clocks, because they answer different questions (D12).

    Mixing them is the bug this interface is shaped to prevent: a duration
    derived from realtime can be negative or absurd, because realtime is
    adjustable -- NTP steps it, and a virtual machine's guest clock
    resynchronises after the host sleeps.
    """

    def now_unix_ms(self) -> int:
        """Milliseconds since the Unix epoch. Orders runs; never subtracted."""
        ...

    def monotonic_ms(self) -> int:
        """Milliseconds from an arbitrary fixed point. Only ever subtracted.

        Meaningless as an absolute time, and guaranteed not to go backwards.
        """
        ...


@runtime_checkable
class IdGenerator(Protocol):
    """Supplies run identifiers.

    Injected so that tests can produce a known sequence: an assertion about a
    stored run is worthless if the identifier changes on every execution.
    """

    def new_run_id(self) -> RunId:
        """Return an identifier not previously returned by this generator."""
        ...
