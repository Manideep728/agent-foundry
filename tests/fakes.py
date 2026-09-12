"""Test doubles for the domain ports.

Shared rather than redefined per test module, because later phases assert on
stored runs and those assertions need a *known* sequence of timestamps and
identifiers.
"""

from __future__ import annotations

from control_plane.domain.ids import RunId

# An arbitrary fixed instant. Nothing depends on the value; it exists so that
# expected timestamps in tests are written as literals rather than computed.
EPOCH_MS = 1_700_000_000_000


class FakeClock:
    """A clock that only moves when a test moves it.

    Monotonic and realtime advance together here, which is fine for tests. The
    two are separate in the protocol because in production they diverge.
    """

    def __init__(self, unix_ms: int = EPOCH_MS, monotonic_ms: int = 0) -> None:
        self._unix_ms = unix_ms
        self._monotonic_ms = monotonic_ms

    def now_unix_ms(self) -> int:
        return self._unix_ms

    def monotonic_ms(self) -> int:
        return self._monotonic_ms

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("a clock does not go backwards")
        self._unix_ms += ms
        self._monotonic_ms += ms


class FakeIdGenerator:
    """Produces run-0001, run-0002, ... so assertions can name an identifier."""

    def __init__(self, prefix: str = "run") -> None:
        self._prefix = prefix
        self._issued = 0

    def new_run_id(self) -> RunId:
        self._issued += 1
        return RunId(f"{self._prefix}-{self._issued:04d}")
