"""The worker's step: take, measure, publish, flush, commit.

Each test guards a way a queue loses or double-counts a measurement: an offset
committed before the result landed, a redelivery measured a second time, a
misconfigured worker quietly measuring the wrong thing, or one bad message
stopping a partition for good.
"""

from __future__ import annotations

import dataclasses

import pytest

from control_plane.adapters import dispatch_json, record_json
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.record import RunRecord
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.task import TaskDefinition
from control_plane.services.dispatch import IncompatibleMessage, TaskRunMessage
from control_plane.services.polling import poll_until
from control_plane.services.worker import (
    Handled,
    Handling,
    ResultNotDelivered,
    Worker,
)
from tests.bus_fakes import FakeConsumer, FakeTopic
from tests.fakes import EPOCH_MS, FakeClock
from tests.test_domain_record import record, scored_run
from tests.test_domain_task import make_task
from tests.test_service_dispatch import (
    UnflushableTopic,
    make_configuration,
    make_message,
)

FINISHED_AT_MS = EPOCH_MS + 90_000


class FakeExecutor:
    """Measures instantly, and doubles as the worker's database."""

    def __init__(self) -> None:
        self.stored: dict[RunId, RunRecord] = {}
        self.executed: list[RunId] = []

    def execute(self, task: TaskDefinition, run_id: RunId | None = None) -> RunRecord:
        assert run_id is not None, "a worker must pass the dispatched run_id"
        self.executed.append(run_id)
        run = dataclasses.replace(
            scored_run(),
            run_id=run_id,
            task_id=task.task_id,
            updated_at_unix_ms=FINISHED_AT_MS,
        )
        stored = record(run=run, outcome=AgentOutcome.NO_PATCH_PRODUCED)
        self.stored[run_id] = stored
        return stored

    def get(self, run_id: RunId) -> RunRecord | None:
        return self.stored.get(run_id)


class Harness:
    """A work topic, a results topic, and a worker between them."""

    def __init__(self, results: FakeTopic | None = None, **config: object) -> None:
        self.work = FakeTopic()
        self.results = results or FakeTopic(partitions=1)
        self.consumer: FakeConsumer = self.work.consumer()
        self.executor = FakeExecutor()
        task = make_task()
        self.worker = Worker(
            consumer=self.consumer,
            publisher=self.results,
            tasks={task.task_id: task},
            configuration=make_configuration(**config),
            executor=self.executor,
            records=self.executor,
            worker_id="worker-01",
            decode=dispatch_json.decode,
            encode=record_json.encode,
        )

    def send(self, message: TaskRunMessage) -> None:
        self.work.publish(message.partition_key, dispatch_json.encode(message))

    def published(self) -> list[RunRecord]:
        return [record_json.decode(d.value).record for d in self.results.delivered]


def test_a_message_is_measured_under_the_dispatched_run_id() -> None:
    harness = Harness()
    harness.send(make_message(run_id=RunId("dispatched-0001")))

    handled = harness.worker.step(0)

    assert handled is not None
    assert handled.handling is Handling.MEASURED
    assert harness.executor.executed == [RunId("dispatched-0001")]
    [result] = harness.published()
    assert result.run.run_id == RunId("dispatched-0001")
    assert harness.consumer.committed == 1


def test_the_result_carries_when_the_run_finished_and_who_ran_it() -> None:
    harness = Harness()
    harness.send(make_message(dispatched_at_ms=EPOCH_MS))

    harness.worker.step(0)

    result = record_json.decode(harness.results.delivered[0].value)
    assert result.dispatched_at_ms == EPOCH_MS
    assert result.completed_at_ms == FINISHED_AT_MS
    assert result.worker_id == "worker-01"


def test_a_quiet_topic_is_not_the_end_of_the_work() -> None:
    assert Harness().worker.step(0) is None


def test_an_unacknowledged_result_leaves_the_work_uncommitted() -> None:
    harness = Harness(results=UnflushableTopic(outstanding=1))
    harness.send(make_message())

    with pytest.raises(ResultNotDelivered, match="run-0001"):
        harness.worker.step(0)

    assert harness.consumer.committed == 0


def test_a_redelivered_message_is_republished_not_measured_again() -> None:
    # The crash this ordering exists for: the run finished, the result did not
    # land, and the message comes back.
    unflushable = Harness(results=UnflushableTopic(outstanding=1))
    unflushable.send(make_message())
    with pytest.raises(ResultNotDelivered):
        unflushable.worker.step(0)

    harness = Harness()
    harness.executor.stored = unflushable.executor.stored
    harness.send(make_message())
    handled = harness.worker.step(0)

    assert handled is not None
    assert handled.handling is Handling.REPUBLISHED
    assert harness.executor.executed == []
    assert [r.run.run_id for r in harness.published()] == [RunId("run-0001")]
    assert harness.consumer.committed == 1


def test_a_stored_run_that_is_still_in_progress_is_not_published() -> None:
    harness = Harness()
    in_progress = EvaluationRun.create(
        RunId("run-0001"), TaskId("csv-parser-001"), EPOCH_MS
    ).transition_to(RunState.PREPARING, EPOCH_MS)
    harness.executor.stored[RunId("run-0001")] = dataclasses.replace(
        record(outcome=AgentOutcome.NO_PATCH_PRODUCED),
        run=in_progress,
        report=None,
        score=None,
        outcome=None,
    )
    harness.send(make_message())

    with pytest.raises(RuntimeError, match="another process"):
        harness.worker.step(0)

    assert harness.results.delivered == ()
    assert harness.consumer.committed == 0


def test_bytes_that_are_not_a_message_are_committed_past() -> None:
    # Left uncommitted, one bad message would be redelivered for ever and
    # every message behind it on the partition would wait.
    harness = Harness()
    harness.work.publish(b"csv-parser-001", b"not json")

    handled = harness.worker.step(0)

    assert handled is not None
    assert handled.handling is Handling.UNDECODABLE
    assert "not JSON" in handled.detail
    assert harness.consumer.committed == 1
    assert harness.results.delivered == ()


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (make_message(model_id="qwen2.5-coder:3b"), "this worker measures"),
        (make_message(image="afrunner:other"), "this worker measures"),
        (make_message(prompt_id=None), "this worker measures"),
        (make_message(task_id=TaskId("unknown-001")), "does not have"),
        (make_message(fixture_sha256="b" * 64), "do not hold the same task"),
    ],
)
def test_a_message_this_worker_cannot_honour_is_left_for_another(
    message: TaskRunMessage, reason: str
) -> None:
    harness = Harness()
    harness.send(message)

    with pytest.raises(IncompatibleMessage, match=reason):
        harness.worker.step(0)

    assert harness.executor.executed == []
    assert harness.results.delivered == ()
    assert harness.consumer.committed == 0


class PollingClock(FakeClock):
    """Moves one second every time the loop reads it, as polling would."""

    def monotonic_ms(self) -> int:
        self.advance(1_000)
        return super().monotonic_ms()


def test_the_loop_stops_at_the_message_limit_and_leaves_the_rest() -> None:
    harness = Harness()
    for number in range(1, 4):
        harness.send(make_message(run_id=RunId(f"run-000{number}")))
    seen: list[Handled] = []

    handled = poll_until(harness.worker, PollingClock(), seen.append, max_messages=2)

    assert handled == 2
    assert [h.record.run.run_id for h in seen if h.record] == [
        RunId("run-0001"),
        RunId("run-0002"),
    ]
    assert harness.consumer.committed == 2


def test_the_loop_ends_once_the_topic_has_been_quiet_long_enough() -> None:
    harness = Harness()
    harness.send(make_message())
    seen: list[Handled] = []

    handled = poll_until(
        harness.worker, PollingClock(), seen.append, idle_exit_ms=5_000
    )

    assert handled == 1
    assert len(seen) == 1


def test_a_worker_evicted_mid_run_still_publishes_and_keeps_working() -> None:
    # Measured 2026-09-28: a worker that holds a message past
    # max.poll.interval.ms is evicted, and its commit fails with
    # UNKNOWN_MEMBER_ID after it has published. That used to escape as a raw
    # KafkaException and kill the worker; the message had already gone to
    # another worker, and the collector keeps whichever result lands first.
    harness = Harness()
    harness.send(make_message())
    harness.consumer.evicted = True

    handled = harness.worker.step(0)

    assert handled is not None
    assert handled.handling is Handling.MEASURED
    assert not handled.committed
    assert len(harness.published()) == 1
