"""The fake bus is load-bearing, so it is tested like production code.

Every later test of a dispatcher, a worker or a collector trusts this file to
model at-least-once delivery. A fake that quietly delivered exactly once would
make the duplicate-handling tests pass without exercising anything.
"""

from __future__ import annotations

import pytest

from tests.bus_fakes import FakeTopic


def test_the_same_key_always_lands_on_the_same_partition() -> None:
    topic = FakeTopic(partitions=6)

    topic.publish(b"csv-parser-001", b"first")
    topic.publish(b"csv-parser-001", b"second")

    partitions = {delivery.partition for delivery in topic.delivered}
    assert len(partitions) == 1


def test_different_keys_reach_more_than_one_partition() -> None:
    """A fake that sent everything to partition 0 would model nothing."""
    topic = FakeTopic(partitions=6)

    for index in range(40):
        topic.publish(f"task-{index:03d}".encode(), b"payload")

    assert len({delivery.partition for delivery in topic.delivered}) > 1


def test_offsets_increase_within_a_partition_and_start_at_zero() -> None:
    topic = FakeTopic(partitions=1)

    topic.publish(b"a", b"one")
    topic.publish(b"b", b"two")

    assert [delivery.offset for delivery in topic.delivered] == [0, 1]


def test_offsets_are_counted_per_partition_not_globally() -> None:
    """A global counter would make two partitions look like one long log."""
    topic = FakeTopic(partitions=6)
    for index in range(40):
        topic.publish(f"task-{index:03d}".encode(), b"payload")

    by_partition: dict[int, list[int]] = {}
    for delivery in topic.delivered:
        by_partition.setdefault(delivery.partition, []).append(delivery.offset)

    for offsets in by_partition.values():
        assert offsets == list(range(len(offsets)))


def test_a_consumer_reads_every_message_then_returns_none() -> None:
    topic = FakeTopic()
    topic.publish(b"a", b"one")
    consumer = topic.consumer()

    first = consumer.poll(0.1)
    assert first is not None and first.value == b"one"
    assert consumer.poll(0.1) is None


def test_a_restart_redelivers_what_was_never_committed() -> None:
    """This is the duplicate the whole design exists to survive."""
    topic = FakeTopic()
    topic.publish(b"a", b"one")
    consumer = topic.consumer()
    delivery = consumer.poll(0.1)
    assert delivery is not None

    consumer.restart()

    again = consumer.poll(0.1)
    assert again is not None
    assert again.value == b"one"
    assert again.offset == delivery.offset


def test_a_restart_does_not_redeliver_a_committed_message() -> None:
    topic = FakeTopic()
    topic.publish(b"a", b"one")
    consumer = topic.consumer()
    delivery = consumer.poll(0.1)
    assert delivery is not None
    consumer.commit(delivery)

    consumer.restart()

    assert consumer.poll(0.1) is None


def test_a_restart_replays_only_the_uncommitted_tail() -> None:
    topic = FakeTopic()
    for value in (b"one", b"two", b"three"):
        topic.publish(b"a", value)
    consumer = topic.consumer()
    first = consumer.poll(0.1)
    assert first is not None
    consumer.commit(first)
    consumer.poll(0.1)
    consumer.poll(0.1)

    consumer.restart()

    replayed = [consumer.poll(0.1), consumer.poll(0.1), consumer.poll(0.1)]
    assert [d.value for d in replayed if d is not None] == [b"two", b"three"]


def test_flushing_is_recorded_so_a_caller_that_never_flushes_is_visible() -> None:
    """A dispatcher that exits without flushing loses work silently."""
    topic = FakeTopic()

    assert topic.flush_calls == []
    assert topic.flush(5.0) == 0
    assert topic.flush_calls == [5.0]


def test_a_closed_consumer_refuses_to_poll() -> None:
    topic = FakeTopic()
    consumer = topic.consumer()
    consumer.close()

    with pytest.raises(RuntimeError, match="closed consumer"):
        consumer.poll(0.1)


def test_a_topic_needs_at_least_one_partition() -> None:
    with pytest.raises(ValueError, match="at least one partition"):
        FakeTopic(partitions=0)
