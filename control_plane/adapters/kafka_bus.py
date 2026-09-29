"""The Kafka client, behind the publisher and consumer ports.

`confluent-kafka` is an optional extra, not a dependency: `af run` and `af
bench` are the serial path and must keep working on a machine with no broker
and no librdkafka. The import therefore happens inside the constructors, and a
missing client produces an instruction rather than a traceback.

Every configuration value set here is set for a reason, and the reasons are in
the comments. The defaults this adapter overrides are the ones that would
quietly change what a benchmark measures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from control_plane.services.dispatch import CommitRejected, RawDelivery

if TYPE_CHECKING:  # pragma: no cover - import only for the type checker
    from confluent_kafka import Consumer as _Consumer
    from confluent_kafka import Producer as _Producer

DEFAULT_BOOTSTRAP = "localhost:9092"


class KafkaUnavailable(Exception):
    """The client is not installed, or no broker answered."""


def _require_client() -> Any:
    try:
        import confluent_kafka
    except ImportError as error:  # pragma: no cover - depends on the install
        raise KafkaUnavailable(
            "the Kafka client is not installed; run `uv sync --extra kafka`"
        ) from error
    return confluent_kafka


class KafkaPublisher:
    """Publishes bytes to one topic.

    The topic is bound here rather than passed per call, so a worker holding a
    results publisher cannot produce onto the work topic.
    """

    def __init__(self, topic: str, bootstrap: str = DEFAULT_BOOTSTRAP) -> None:
        client = _require_client()
        self._topic = topic
        self._producer: _Producer = client.Producer(
            {
                "bootstrap.servers": bootstrap,
                # Wait for every in-sync replica. The default of 1 in older
                # clients acknowledges as soon as the leader has the message,
                # which loses work if that leader fails before it replicates.
                # A dispatched run costs minutes to reproduce.
                "acks": "all",
                # The producer retries internally on a transient error, and a
                # retry after a lost acknowledgement writes the message twice.
                # Idempotence makes the broker recognise the retry. It does not
                # replace the `run_id` key -- that covers redelivery to a
                # consumer, which is a different duplicate with a different
                # cause.
                "enable.idempotence": True,
            }
        )

    def publish(self, key: bytes, value: bytes) -> None:
        # Asynchronous: this only queues the message. Nothing is on the broker
        # until `flush` returns 0, which is why the port makes flushing the
        # caller's explicit responsibility.
        self._producer.produce(self._topic, key=key, value=value)
        # Serves the delivery callbacks for messages already acknowledged.
        # Without it the internal queue grows for the whole dispatch and every
        # error surfaces at the end, attached to no particular message.
        self._producer.poll(0)

    def flush(self, timeout_s: float) -> int:
        """Return the number of messages still unacknowledged."""
        remaining: int = self._producer.flush(timeout_s)
        return remaining


class KafkaConsumer:
    """Consumes bytes from one topic as a member of a group."""

    def __init__(
        self,
        topic: str,
        group_id: str,
        bootstrap: str = DEFAULT_BOOTSTRAP,
        *,
        max_poll_interval_ms: int = 900_000,
    ) -> None:
        client = _require_client()
        self._client = client
        self._topic = topic
        self._consumer: _Consumer = client.Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": group_id,
                # Off. The whole contract is that an offset is committed after
                # the work is durable; an automatic commit on a timer would
                # acknowledge a message whose run never finished, and that run
                # would simply never be measured.
                "enable.auto.commit": False,
                # A new group reads the topic from the start. The alternative,
                # `latest`, silently skips everything dispatched before the
                # worker started, which looks exactly like a lost message.
                "auto.offset.reset": "earliest",
                # The default is five minutes, and a validation run is allowed
                # to take longer than that. A consumer that exceeds it is
                # judged dead, its partitions are reassigned, and the work is
                # delivered to somebody else while the first worker is still
                # doing it. Fifteen minutes is above the per-task ceiling this
                # project sets; it is a starting value to measure, not a
                # settled one.
                "max.poll.interval.ms": max_poll_interval_ms,
            }
        )
        self._consumer.subscribe([topic])

    def poll(self, timeout_s: float) -> RawDelivery | None:
        message = self._consumer.poll(timeout_s)
        if message is None:
            return None
        error = message.error()
        if error is not None:
            # `_PARTITION_EOF` is not a fault. It says the consumer reached
            # the end of a partition, which for a worker waiting on work is
            # the ordinary quiet case and must read the same as no message.
            if error.code() == self._client.KafkaError._PARTITION_EOF:
                return None
            # Nor is `_MAX_POLL_EXCEEDED`. It is delivered once, on the first
            # poll after this consumer held a message past the interval and
            # was evicted, and that same poll rejoins the group. Measured
            # 2026-09-28: raising here stopped a worker that had just
            # published its result and could go on working.
            if error.code() == self._client.KafkaError._MAX_POLL_EXCEEDED:
                return None
            raise KafkaUnavailable(f"consume failed: {error}")

        partition, offset = message.partition(), message.offset()
        if partition is None or offset is None:
            # A message with no position cannot be committed, so consuming it
            # would mean reading it again on every restart. Nothing known
            # produces one; refusing loudly beats looping quietly.
            raise KafkaUnavailable("a consumed message carried no partition or offset")

        value = message.value()
        if value is None:
            # A tombstone: Kafka's null-valued message, which exists for log
            # compaction and has no meaning on a work topic. Delivered as
            # empty bytes rather than raised, so the caller rejects it as
            # undecodable and commits past it. Raising here would make a
            # single stray tombstone stop the worker for ever.
            value = b""

        key = message.key()
        return RawDelivery(
            partition=partition,
            offset=offset,
            key=key if key is None or isinstance(key, bytes) else bytes(key),
            value=value if isinstance(value, bytes) else bytes(value),
        )

    def commit(self, delivery: RawDelivery) -> None:
        # The committed offset is the *next* message to read, so it is one
        # past the one just finished. Committing the delivery's own offset
        # would redeliver it on every restart for ever.
        try:
            self._consumer.commit(
                offsets=[
                    self._client.TopicPartition(
                        self._topic, delivery.partition, delivery.offset + 1
                    )
                ],
                asynchronous=False,
            )
        except self._client.KafkaException as raised:
            error = raised.args[0]
            if error.code() in self._evicted_codes():
                raise CommitRejected(
                    f"commit of partition {delivery.partition} offset "
                    f"{delivery.offset} refused: {error.str()}"
                ) from raised
            raise KafkaUnavailable(f"commit failed: {error}") from raised

    def _evicted_codes(self) -> frozenset[int]:
        """The errors that mean the group has moved on without this member."""
        codes = self._client.KafkaError
        return frozenset(
            {
                codes.UNKNOWN_MEMBER_ID,
                codes.ILLEGAL_GENERATION,
                codes.REBALANCE_IN_PROGRESS,
                codes._MAX_POLL_EXCEEDED,
            }
        )

    def close(self) -> None:
        self._consumer.close()
