"""The Kafka adapter against a real broker.

The unit tests use `FakeTopic`, which models the semantics this project
depends on. These tests exist to check the one thing a fake cannot: that the
real client and the real broker agree with that model. Each test creates its
own topic and its own group, so a leftover offset from a previous run cannot
change a result.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from control_plane.adapters.dispatch_json import decode, encode
from control_plane.adapters.kafka_bus import KafkaConsumer, KafkaPublisher
from control_plane.services.dispatch import RawDelivery
from tests.kafka_support import BOOTSTRAP, requires_broker
from tests.test_service_dispatch import make_message

pytestmark = [pytest.mark.kafka, requires_broker]

POLL_ATTEMPTS = 40
POLL_TIMEOUT_S = 1.0


@pytest.fixture
def topic() -> Iterator[str]:
    """A topic of this test's own, removed afterwards."""
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
    name = f"af-test-{uuid.uuid4().hex[:12]}"
    spec = NewTopic(name, num_partitions=3, replication_factor=1)
    admin.create_topics([spec])[name].result(timeout=30)
    try:
        yield name
    finally:
        admin.delete_topics([name])


def group() -> str:
    return f"af-test-group-{uuid.uuid4().hex[:12]}"


def consume_one(consumer: KafkaConsumer) -> RawDelivery | None:
    for _ in range(POLL_ATTEMPTS):
        delivery = consumer.poll(POLL_TIMEOUT_S)
        if delivery is not None:
            return delivery
    return None


def test_a_message_survives_the_broker(topic: str) -> None:
    sent = make_message()
    publisher = KafkaPublisher(topic, BOOTSTRAP)
    publisher.publish(sent.partition_key, encode(sent))

    assert publisher.flush(30.0) == 0

    consumer = KafkaConsumer(topic, group(), BOOTSTRAP)
    try:
        delivery = consume_one(consumer)
        assert delivery is not None
        assert delivery.key == sent.partition_key
        assert decode(delivery.value) == sent
    finally:
        consumer.close()


def test_a_committed_message_is_not_redelivered_to_the_same_group(topic: str) -> None:
    """The commit has to reach the broker, not only the client's memory."""
    sent = make_message()
    publisher = KafkaPublisher(topic, BOOTSTRAP)
    publisher.publish(sent.partition_key, encode(sent))
    assert publisher.flush(30.0) == 0

    shared_group = group()
    first = KafkaConsumer(topic, shared_group, BOOTSTRAP)
    try:
        delivery = consume_one(first)
        assert delivery is not None
        first.commit(delivery)
    finally:
        first.close()

    second = KafkaConsumer(topic, shared_group, BOOTSTRAP)
    try:
        assert consume_one(second) is None
    finally:
        second.close()


def test_an_uncommitted_message_is_redelivered_to_the_same_group(topic: str) -> None:
    """At-least-once, against the real broker.

    A worker that reads a message and dies without committing must see it
    again. This is the property `FakeConsumer.restart` models, and the reason
    `run_id` is minted by the dispatcher.
    """
    sent = make_message()
    publisher = KafkaPublisher(topic, BOOTSTRAP)
    publisher.publish(sent.partition_key, encode(sent))
    assert publisher.flush(30.0) == 0

    shared_group = group()
    first = KafkaConsumer(topic, shared_group, BOOTSTRAP)
    try:
        assert consume_one(first) is not None
    finally:
        first.close()

    second = KafkaConsumer(topic, shared_group, BOOTSTRAP)
    try:
        again = consume_one(second)
        assert again is not None
        assert decode(again.value) == sent
    finally:
        second.close()


def test_a_new_group_reads_from_the_start(topic: str) -> None:
    """`auto.offset.reset` is `earliest`; `latest` would silently skip work."""
    sent = make_message()
    publisher = KafkaPublisher(topic, BOOTSTRAP)
    publisher.publish(sent.partition_key, encode(sent))
    assert publisher.flush(30.0) == 0

    consumer = KafkaConsumer(topic, group(), BOOTSTRAP)
    try:
        assert consume_one(consumer) is not None
    finally:
        consumer.close()


def test_a_tombstone_arrives_as_empty_bytes_rather_than_stopping_the_worker(
    topic: str,
) -> None:
    """A null-valued message has no meaning on a work topic.

    It must be undecodable, so the caller commits past it, and must not raise
    out of `poll` -- a single stray tombstone would otherwise stop the worker
    for ever.
    """
    from confluent_kafka import Producer

    producer = Producer({"bootstrap.servers": BOOTSTRAP, "acks": "all"})
    producer.produce(topic, key=b"csv-parser-001", value=None)
    assert producer.flush(30.0) == 0

    consumer = KafkaConsumer(topic, group(), BOOTSTRAP)
    try:
        delivery = consume_one(consumer)
        assert delivery is not None
        assert delivery.value == b""
    finally:
        consumer.close()
