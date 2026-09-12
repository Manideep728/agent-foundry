"""The injected ports, and the test doubles that stand in for them."""

from __future__ import annotations

import pytest

from control_plane.domain.ids import RunId
from control_plane.domain.ports import Clock, IdGenerator
from tests.fakes import EPOCH_MS, FakeClock, FakeIdGenerator


def test_fakes_satisfy_the_ports() -> None:
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeIdGenerator(), IdGenerator)


def test_a_type_missing_a_method_does_not_satisfy_the_port() -> None:
    # Without this, the assertions above would pass for anything at all and
    # prove nothing about the protocols.
    class HalfAClock:
        def now_unix_ms(self) -> int:
            return 0

    assert not isinstance(HalfAClock(), Clock)


def test_fake_clock_only_moves_when_told() -> None:
    clock = FakeClock()
    assert clock.now_unix_ms() == EPOCH_MS
    assert clock.now_unix_ms() == EPOCH_MS  # reading it does not advance it

    clock.advance(1_500)
    assert clock.now_unix_ms() == EPOCH_MS + 1_500
    assert clock.monotonic_ms() == 1_500


def test_fake_clock_refuses_to_go_backwards() -> None:
    # A monotonic clock that can be rewound is not a monotonic clock, and a
    # test that rewinds one would be asserting something impossible.
    with pytest.raises(ValueError):
        FakeClock().advance(-1)


def test_fake_id_generator_never_repeats() -> None:
    generator = FakeIdGenerator()
    issued = [generator.new_run_id() for _ in range(100)]

    assert len(set(issued)) == 100
    assert issued[0] == RunId("run-0001")


def test_generated_ids_pass_identifier_validation() -> None:
    # The generator returns RunId, whose constructor validates -- so this would
    # already have raised. Asserted explicitly because a future real generator
    # (uuid4 hex, a ULID, a timestamp-prefixed id) must also satisfy the
    # character set that adapters rely on when building paths.
    assert FakeIdGenerator("task.group").new_run_id().value == "task.group-0001"
