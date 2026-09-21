"""An in-memory stand-in for one Kafka topic.

Its purpose is not only to avoid a broker in `make check`. It is to make the
behaviour that matters *reachable from a test*: at-least-once delivery. A real
duplicate needs a worker to die between finishing its work and committing its
offset, which is difficult to arrange and slow to run. Here it is
`consumer.restart()`.

What the fake models faithfully:

- A key always lands on the same partition, and offsets increase within one.
- Offsets are committed only when the caller says so, and a restart rewinds to
  the last commit.

What it does not model, and no test may depend on:

- **Which** partition a key lands on. Kafka hashes the key with murmur2; this
  uses the standard library. Same-key-same-partition holds, the number does
  not.
- Consumer groups, rebalancing, and more than one consumer at a time.
- Ordering between partitions. Messages come back in publish order, which a
  real broker does not promise across partitions.
"""

from __future__ import annotations

import hashlib

from control_plane.services.dispatch import RawDelivery


class FakeTopic:
    """One topic. Implements `MessagePublisher`."""

    def __init__(self, partitions: int = 6) -> None:
        if partitions < 1:
            raise ValueError("a topic has at least one partition")
        self._partitions = partitions
        self._log: list[RawDelivery] = []
        self._next_offset = [0] * partitions
        self.flush_calls: list[float] = []

    def publish(self, key: bytes, value: bytes) -> None:
        partition = self._partition_for(key)
        offset = self._next_offset[partition]
        self._next_offset[partition] = offset + 1
        self._log.append(
            RawDelivery(partition=partition, offset=offset, key=key, value=value)
        )

    def flush(self, timeout_s: float) -> int:
        """Nothing is ever in flight in memory, so nothing is ever outstanding.

        The calls are recorded because a caller that never flushes is a real
        defect: a dispatcher that exits without flushing loses whatever the
        producer had not yet sent, and the work is silently never done.
        """
        self.flush_calls.append(timeout_s)
        return 0

    def consumer(self) -> FakeConsumer:
        return FakeConsumer(self)

    @property
    def delivered(self) -> tuple[RawDelivery, ...]:
        """Everything published, in publish order. For assertions."""
        return tuple(self._log)

    def _partition_for(self, key: bytes) -> int:
        # Hashed rather than round-robined, so that "the same key goes to the
        # same partition" is a property of the fake and not of the test's
        # publish order. sha256 because Python's hash() is salted per process.
        digest = hashlib.sha256(key).digest()
        return int.from_bytes(digest[:4], "big") % self._partitions


class FakeConsumer:
    """Reads a `FakeTopic`. Implements `MessageConsumer`."""

    def __init__(self, topic: FakeTopic) -> None:
        self._topic = topic
        self._position = 0
        self._committed = 0
        self.closed = False

    def poll(self, timeout_s: float) -> RawDelivery | None:
        if self.closed:
            raise RuntimeError("poll on a closed consumer")
        if self._position >= len(self._topic.delivered):
            return None
        delivery = self._topic.delivered[self._position]
        self._position += 1
        return delivery

    def commit(self, delivery: RawDelivery) -> None:
        if self.closed:
            raise RuntimeError("commit on a closed consumer")
        self._committed = self._position

    def close(self) -> None:
        self.closed = True

    def restart(self) -> None:
        """Die and come back: rewind to the last commit.

        This is the duplicate. Everything polled but not committed is
        delivered again, which is what a real worker sees after it dies
        mid-task or is evicted from its group for holding a message too long.
        """
        self._position = self._committed
        self.closed = False

    @property
    def committed(self) -> int:
        """How many messages are acknowledged. For assertions."""
        return self._committed
