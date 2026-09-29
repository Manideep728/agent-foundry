"""How the adapter reports a commit the broker refuses. No broker needed.

The real case takes 45 seconds to provoke -- `max.poll.interval.ms` may not be
set below `session.timeout.ms` -- so the client's commit is replaced with one
that raises the error the broker sent when it was provoked on 2026-09-28.
"""

from __future__ import annotations

from typing import Any

import pytest

from control_plane.adapters.kafka_bus import KafkaConsumer, KafkaUnavailable
from control_plane.services.dispatch import CommitRejected, RawDelivery

kafka = pytest.importorskip("confluent_kafka")

DELIVERY = RawDelivery(partition=3, offset=41, key=b"csv-parser-001", value=b"{}")


def consumer_whose_commit_raises(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> KafkaConsumer:
    class RefusingConsumer:
        def __init__(self, _config: dict[str, Any]) -> None:
            pass

        def subscribe(self, _topics: list[str]) -> None:
            pass

        def commit(self, **_: Any) -> None:
            raise kafka.KafkaException(kafka.KafkaError(code))

    monkeypatch.setattr(kafka, "Consumer", RefusingConsumer)
    return KafkaConsumer("agentfoundry-task-runs", "agentfoundry-workers")


@pytest.mark.parametrize(
    "code",
    [
        kafka.KafkaError.UNKNOWN_MEMBER_ID,
        kafka.KafkaError.ILLEGAL_GENERATION,
        kafka.KafkaError.REBALANCE_IN_PROGRESS,
        kafka.KafkaError._MAX_POLL_EXCEEDED,
    ],
)
def test_an_eviction_is_a_rejected_commit(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    consumer = consumer_whose_commit_raises(monkeypatch, code)

    with pytest.raises(CommitRejected, match="partition 3 offset 41"):
        consumer.commit(DELIVERY)


def test_any_other_commit_failure_is_the_broker_being_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = consumer_whose_commit_raises(monkeypatch, kafka.KafkaError._TRANSPORT)

    with pytest.raises(KafkaUnavailable, match="commit failed"):
        consumer.commit(DELIVERY)


class ErrorEvent:
    """What librdkafka's poll returns for an event rather than a message."""

    def __init__(self, code: int) -> None:
        self._error = kafka.KafkaError(code)

    def error(self) -> Any:
        return self._error


def consumer_whose_poll_returns(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> KafkaConsumer:
    class EventConsumer:
        def __init__(self, _config: dict[str, Any]) -> None:
            pass

        def subscribe(self, _topics: list[str]) -> None:
            pass

        def poll(self, _timeout: float) -> ErrorEvent:
            return ErrorEvent(code)

    monkeypatch.setattr(kafka, "Consumer", EventConsumer)
    return KafkaConsumer("agentfoundry-task-runs", "agentfoundry-workers")


@pytest.mark.parametrize(
    "code", [kafka.KafkaError._MAX_POLL_EXCEEDED, kafka.KafkaError._PARTITION_EOF]
)
def test_an_eviction_notice_on_poll_reads_as_a_quiet_topic(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    assert consumer_whose_poll_returns(monkeypatch, code).poll(0) is None


def test_any_other_poll_error_is_the_broker_being_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = consumer_whose_poll_returns(monkeypatch, kafka.KafkaError._TRANSPORT)

    with pytest.raises(KafkaUnavailable, match="consume failed"):
        consumer.poll(0)
