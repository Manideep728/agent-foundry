"""Taking one unit of work off the queue and turning it into a result.

A worker is the serial loop of `af bench` with the loop removed: the dispatcher
decided what to measure, and each worker measures one message at a time and
hands the record on. The order of the steps is the whole of the design.

**Take, finish, store the files, publish, flush, and only then commit.** The
files go to the artifact store before the result is published, so a result
the collector holds always has its files somewhere durable. The committed
offset is
this worker's claim that the message is done. Committed any earlier, a crash
between the commit and the publish loses the run for good: the offset has
moved past it and no worker will see it again. Committed last, the same crash
causes a redelivery, and the dispatcher-minted `run_id` makes that redelivery a
duplicate the collector recognises rather than a second measurement.

**A redelivered message is not measured twice.** The worker's own database
already holds any run it finished, so a message whose `run_id` is stored is
answered from storage. That includes a run the startup recovery scan marked
interrupted: the existing policy is that an interrupted run is recorded, not
retried, and the queue does not change it.

**Three kinds of refusal, and only one is acknowledged.** Bytes that are not a
message are committed past, because no worker can ever read them and leaving
them would stop the partition for good. A message this worker cannot honour --
another configuration, another checkout of the task, a task it does not have
-- raises `IncompatibleMessage` and is *not* committed, so the worker stops and
a correctly configured one takes the message. Anything else is a bug and is
not caught.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.record import RunRecord
from control_plane.domain.task import TaskDefinition
from control_plane.services.dispatch import (
    CommitRejected,
    IncompatibleMessage,
    MessageConsumer,
    MessagePublisher,
    RawDelivery,
    RunConfiguration,
    RunResultMessage,
    TaskRunMessage,
    UndecodableMessage,
)
from control_plane.services.ports import ArtifactStore


class Executor(Protocol):
    """What measures a task. `EvaluationService` in production."""

    def execute(
        self, task: TaskDefinition, run_id: RunId | None = None
    ) -> RunRecord: ...


class RecordLookup(Protocol):
    """The worker's own database, read to recognise a redelivered message."""

    def get(self, run_id: RunId) -> RunRecord | None: ...


class ResultNotDelivered(Exception):
    """The broker did not acknowledge a result before the timeout.

    The work message is left uncommitted. It will be delivered again, and the
    stored record answers it without another measurement.
    """


class Handling(StrEnum):
    MEASURED = "measured"
    REPUBLISHED = "republished"
    UNDECODABLE = "undecodable"


@dataclass(frozen=True, slots=True)
class Handled:
    """What one step did, for the caller to report."""

    handling: Handling
    delivery: RawDelivery
    record: RunRecord | None
    """None only when the delivery could not be decoded."""

    detail: str = ""
    """Why the delivery could not be decoded. Empty otherwise."""

    committed: bool = True
    """False when the group had already evicted this worker (`CommitRejected`).

    The result was still published; the message went to another worker, and
    the collector keeps whichever result arrives first.
    """


class Worker:
    def __init__(
        self,
        *,
        consumer: MessageConsumer,
        publisher: MessagePublisher,
        tasks: Mapping[TaskId, TaskDefinition],
        configuration: RunConfiguration,
        executor: Executor,
        records: RecordLookup,
        worker_id: str,
        decode: Callable[[bytes], TaskRunMessage],
        encode: Callable[[RunResultMessage], bytes],
        flush_timeout_s: float = 30.0,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._consumer = consumer
        self._publisher = publisher
        self._tasks = tasks
        self._configuration = configuration
        self._executor = executor
        self._records = records
        self._worker_id = worker_id
        self._decode = decode
        self._encode = encode
        self._flush_timeout_s = flush_timeout_s
        self._artifacts = artifacts

    def step(self, timeout_s: float) -> Handled | None:
        """Handle at most one message. None means the topic was quiet.

        Raises `ArtifactsNotStored` when the files did not reach the store,
        `IncompatibleMessage` for a message this worker must leave for
        another, and `ResultNotDelivered` when the result did not land. In each
        case nothing is committed.
        """
        delivery = self._consumer.poll(timeout_s)
        if delivery is None:
            return None

        try:
            message = self._decode(delivery.value)
        except UndecodableMessage as error:
            committed = self._commit(delivery)
            return Handled(
                Handling.UNDECODABLE, delivery, None, str(error), committed=committed
            )

        task = self._task_for(message)
        record = self._records.get(message.run_id)
        if record is None:
            record = self._executor.execute(task, run_id=message.run_id)
            handling = Handling.MEASURED
        elif record.run.is_terminal:
            handling = Handling.REPUBLISHED
        else:
            # Recovery runs under the lock before the first poll, so a stored
            # run that is still in progress means two workers share this
            # database. Publishing it would send a measurement that is not
            # finished; measuring again would collide on the primary key.
            raise RuntimeError(
                f"{message.run_id} is stored as {record.run.state}; another "
                f"process is using this worker's database"
            )

        # On the republish path too. A worker that stopped because an upload
        # failed meets the same message again, finds the run stored, and must
        # upload before it publishes, or the retry would skip the files.
        if self._artifacts is not None:
            self._artifacts.put_run(record)
        self._publish(message, record)
        return Handled(handling, delivery, record, committed=self._commit(delivery))

    def _commit(self, delivery: RawDelivery) -> bool:
        try:
            self._consumer.commit(delivery)
        except CommitRejected:
            return False
        return True

    def _task_for(self, message: TaskRunMessage) -> TaskDefinition:
        if message.configuration != self._configuration:
            raise IncompatibleMessage(
                f"{message.run_id} asks for {message.configuration} and this "
                f"worker measures {self._configuration}"
            )
        task = self._tasks.get(message.task_id)
        if task is None:
            raise IncompatibleMessage(
                f"{message.run_id} is for {message.task_id}, which this "
                f"checkout does not have"
            )
        message.accepts(task)
        return task

    def _publish(self, message: TaskRunMessage, record: RunRecord) -> None:
        result = RunResultMessage(
            record=record,
            worker_id=self._worker_id,
            dispatched_at_ms=message.dispatched_at_ms,
            # When the run finished, read off the stored run rather than a
            # clock, so a republished result reports the original completion
            # and not the moment it was sent again.
            completed_at_ms=record.run.updated_at_unix_ms,
        )
        self._publisher.publish(result.partition_key, self._encode(result))
        outstanding = self._publisher.flush(self._flush_timeout_s)
        if outstanding:
            raise ResultNotDelivered(
                f"the result of {message.run_id} was not acknowledged within "
                f"{self._flush_timeout_s:g}s"
            )
