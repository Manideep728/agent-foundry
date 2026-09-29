"""What travels from a dispatcher to a worker.

The serial scheduler in `af bench` holds one lock and runs every task itself.
Dispatching splits that in two: a producer decides *what* to measure, and a
pool of workers each measure one thing. This module is the contract between
them, and it is deliberately the smallest thing that can be.

**The task body does not travel.** A task is already immutable and pinned by a
content hash, and the fixture it names is on disk in the repository. Sending
the fixture would create a second copy that could disagree with the first, and
the disagreement would be invisible -- the worker would measure something no
task file describes. The message therefore names the task and carries the hash
the dispatcher saw; a worker whose checkout hashes differently refuses the
message instead of producing a number nobody can reproduce.

**The run identifier is minted by the dispatcher.** A log-based queue delivers
at least once: a worker that finishes the work and then dies before committing
its offset will see the same message again, and so will the worker that takes
over its partition after a rebalance. If each worker minted its own identifier,
that redelivery would store a second run and inflate every rate the benchmark
reports. Minting at dispatch makes the identifier the natural idempotency key:
the same message always produces the same `run_id`, so the collector can treat
the second arrival as the duplicate it is.

This module is pure. It builds no connection and knows no broker; the wire
encoding lives in `adapters/dispatch_json.py` and the client in
`adapters/kafka_bus.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from control_plane.domain.errors import DomainError
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.ports import Clock, IdGenerator
from control_plane.domain.record import RunRecord
from control_plane.domain.task import Suite, TaskDefinition

TASK_RUNS_TOPIC = "agentfoundry-task-runs"
RUN_RESULTS_TOPIC = "agentfoundry-run-results"
"""The topic names, defined once here and created by `infra/kafka/topics.sh`.

Hyphenated, never dotted. Kafka warns on any topic name containing a `.` or a
`_`, because both become `_` in a metric name and two topics can collide
there. A test asserts that the script creates exactly these names: topic
auto-creation is off, so a drift between the two would not be a warning but a
dispatcher that produces to a topic nobody consumes.
"""

WORKER_GROUP = "agentfoundry-workers"
"""The consumer group every worker joins.

One group, so the partitions of the work topic are divided among the pool and
each message is measured by one worker. A worker in a group of its own would
read every message the others read, and the suite would be measured twice.
"""

COLLECTOR_GROUP = "agentfoundry-collector"
"""The consumer group the one collector joins, and the offsets it resumes from."""

SCHEMA_VERSION = 1
"""The version every message this build writes carries.

Bumped when a field changes meaning or disappears, never when one is added
with a default. A consumer refuses a version above its own rather than
guessing, because a worker that silently ignores a field it does not
understand measures the configuration it knows about and reports it under the
configuration it was asked for.
"""


class IncompatibleMessage(DomainError):
    """A message this build cannot honour, for a reason that is not a bug."""


class UndecodableMessage(Exception):
    """The bytes on a topic are not a message at all.

    Defined here, and subclassed by each codec's own error, so that a service
    can tell a poison message from a bug without importing an adapter. The
    distinction decides whether an offset is committed: bytes that no build
    can read are acknowledged and skipped, while a message this build merely
    cannot honour is left for a worker that can.
    """


@dataclass(frozen=True, slots=True)
class TaskRunMessage:
    """One unit of work: measure this task under this configuration.

    Every field is part of the measurement's identity except `dispatched_at_ms`
    -- the configuration fields are exactly the benchmark's grouping key, which
    is why they travel with the work rather than being read from each worker's
    own flags. A pool whose workers were started with different `--model`
    values would otherwise produce one suite of runs that silently mixes two
    models, and nothing in the stored record would say so.
    """

    run_id: RunId
    task_id: TaskId
    suite: Suite
    fixture_sha256: str

    agent_id: str
    provider_id: str
    model_id: str
    prompt_id: str | None

    image: str
    """The validation image, by tag or digest.

    Sent rather than configured per worker for the same reason as the model: a
    heterogeneous pool must not be able to produce a homogeneous-looking suite.
    """

    dispatched_at_ms: int
    """Realtime, for ordering and for measuring how long work waited.

    Never subtracted from a monotonic reading (D12). Queue latency is the gap
    between this and the worker's own realtime clock, which is an estimate
    across two machines and is reported as one.
    """

    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise IncompatibleMessage(
                f"schema_version must be >= 1, got {self.schema_version}"
            )
        if self.dispatched_at_ms < 0:
            raise IncompatibleMessage(
                f"dispatched_at_ms must not be negative, got {self.dispatched_at_ms}"
            )
        for name in ("agent_id", "provider_id", "model_id", "image"):
            if not getattr(self, name).strip():
                raise IncompatibleMessage(f"{name} must not be empty")

    @property
    def partition_key(self) -> bytes:
        """What Kafka hashes to choose a partition.

        The task, not the run. Kafka guarantees order only within a partition,
        and the order worth having here is per task: every run of
        `template-render` lands on one partition, so a re-dispatch of a task
        cannot overtake the original and the two are consumed in the order they
        were produced. Keying on `run_id` instead would spread each task evenly
        and guarantee nothing, because every key would be unique.

        The cost is honest and worth naming: a suite whose tasks are unevenly
        expensive will not balance perfectly across partitions. Throughput is
        not what this project measures.
        """
        return self.task_id.value.encode("utf-8")

    def accepts(self, task: TaskDefinition) -> None:
        """Raise unless the worker's own copy of the task matches the sender's.

        This is the fixture-immutability invariant applied across a network
        boundary. Loading the task already refuses a fixture whose contents
        have drifted from the hash the task file pins; this refuses the case
        that check cannot see, where both sides are internally consistent and
        are two different versions of the task.
        """
        if task.task_id != self.task_id:
            raise IncompatibleMessage(
                f"message is for {self.task_id} but {task.task_id} was loaded"
            )
        if task.fixture_sha256 != self.fixture_sha256:
            raise IncompatibleMessage(
                f"{self.task_id} fixture is {task.fixture_sha256} here and "
                f"{self.fixture_sha256} at the dispatcher; the two checkouts "
                f"do not hold the same task"
            )

    @property
    def configuration(self) -> RunConfiguration:
        """The grouping key this message asks to be measured under."""
        return RunConfiguration(
            agent_id=self.agent_id,
            provider_id=self.provider_id,
            model_id=self.model_id,
            prompt_id=self.prompt_id,
            image=self.image,
        )


@dataclass(frozen=True, slots=True)
class RunResultMessage:
    """One finished measurement, on its way to the collector.

    The whole `RunRecord` travels. The alternative -- a notification, with the
    worker writing the database itself -- would put N writers on one SQLite
    file and give up the single-writer invariant the run lock exists to hold.
    Pointing at a file the worker wrote is no better: it assumes the two share
    a filesystem, which is exactly the assumption distribution removes.
    """

    record: RunRecord
    worker_id: str
    """Which worker produced this. Provenance, not identity.

    A measurement is identified by its `run_id`. This says where it was made,
    which is what a suite whose numbers disagree between machines needs in
    order to be read at all.
    """

    dispatched_at_ms: int
    """Copied from the work message, unchanged."""

    completed_at_ms: int
    """The worker's own realtime clock when it finished.

    Both of these are timestamps and neither is subtracted from the other to
    produce a stored duration (D12). The gap between them spans two machines'
    realtime clocks, so it is an estimate of how long work waited, and this
    project does not record estimates as measurements. `agent_ms` and
    `validation_ms` on the record are the real durations, taken from a
    monotonic clock on one machine.
    """

    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.worker_id.strip():
            raise IncompatibleMessage("worker_id must not be empty")
        for name in ("dispatched_at_ms", "completed_at_ms"):
            if getattr(self, name) < 0:
                raise IncompatibleMessage(f"{name} must not be negative")

    @property
    def partition_key(self) -> bytes:
        """The run, because that is the identity a duplicate shares.

        Unlike the work topic, this is not about ordering between runs of one
        task: the results topic has a single partition and the collector reads
        it in order. The key is here so that a duplicate of one run is
        recognisable by key alone, and so that adding partitions later would
        keep every arrival of a run together.
        """
        return self.record.run.run_id.value.encode("utf-8")


@dataclass(frozen=True, slots=True)
class RawDelivery:
    """One message as it arrived, before anything tries to read it.

    Bytes rather than a `TaskRunMessage`, because a message that cannot be
    decoded still has to be acknowledged. A consumer that raised on bad bytes
    would never commit past them, and the same message would be redelivered
    for ever -- the poison message. Keeping the delivery and its decoding
    separate lets the caller record the defect and commit anyway.

    `partition` and `offset` identify the message in the log and are what
    `commit` needs. They are also the only honest way to name a bad message in
    a log line, since its contents are by definition unreadable.
    """

    partition: int
    offset: int
    key: bytes | None
    value: bytes


class MessagePublisher(Protocol):
    """Sends bytes to one topic, chosen when the publisher is built.

    The topic is bound at construction rather than passed per call, so there
    is no argument to get wrong: a dispatcher holds a publisher for the work
    topic and a worker holds one for the results topic, and neither can
    publish to the other's.
    """

    def publish(self, key: bytes, value: bytes) -> None:
        """Hand one message over. May return before the broker has it."""
        ...

    def flush(self, timeout_s: float) -> int:
        """Wait for outstanding messages and return how many remain.

        A non-zero return means the broker did not acknowledge everything in
        time. It is a return value rather than an exception because the caller
        knows what it dispatched and can say which work is in doubt; the
        publisher only knows a count.
        """
        ...


class MessageConsumer(Protocol):
    """Reads bytes from one topic as a member of a consumer group.

    Offsets are committed explicitly, never automatically. The order is the
    whole contract: take the message, finish the work, make the result
    durable, and only then commit. A consumer that committed on receipt would
    lose any run whose worker died mid-task, which is at-most-once delivery
    and the wrong trade for a measurement that costs minutes to produce.

    The price of that order is duplicates, and they are not rare: a worker
    that dies after publishing its result but before committing will see the
    message again, and so will the worker that takes over its partition after
    a rebalance. `TaskRunMessage.run_id` is minted by the dispatcher so that
    the duplicate is recognisable rather than counted twice.
    """

    def poll(self, timeout_s: float) -> RawDelivery | None:
        """Return the next message, or None if none arrived in time.

        None is ordinary and means only that the topic was quiet. A caller
        must not treat it as the end of the work.
        """
        ...

    def commit(self, delivery: RawDelivery) -> None:
        """Record that everything up to and including `delivery` is done."""
        ...

    def close(self) -> None:
        """Leave the group and release the partitions.

        Worth calling rather than relying on the session timeout: an orderly
        departure lets the group rebalance at once, where an abandoned member
        holds its partitions until the broker gives up on it.
        """
        ...


@dataclass(frozen=True, slots=True)
class RunConfiguration:
    """What every message in one dispatch says about how to measure.

    Read from a built agent rather than from the flags a second time. The
    mapping from `--agent ollama --model qwen2.5-coder:7b` to the identity
    trio already exists inside the agent, and a dispatcher that re-derived it
    would be a second place that knows -- with the first bug being the two
    disagreeing about what was measured.
    """

    agent_id: str
    provider_id: str
    model_id: str
    prompt_id: str | None
    image: str


class DispatchIncomplete(Exception):
    """The broker did not acknowledge every message before the timeout."""


def dispatch(
    tasks: Sequence[TaskDefinition],
    configuration: RunConfiguration,
    publisher: MessagePublisher,
    ids: IdGenerator,
    clock: Clock,
    encode: Callable[[TaskRunMessage], bytes],
    *,
    repeats: int = 1,
    flush_timeout_s: float = 30.0,
) -> tuple[TaskRunMessage, ...]:
    """Publish one message per task per repeat, and return what was sent.

    `encode` is injected rather than imported. A service may use an adapter's
    interface and not an adapter, and the wire format is an adapter.

    `repeats` exists for the reproducibility question this project has open:
    two identical suites disagreed on two of seven tasks at temperature 0.
    Repeats are nearly free to dispatch and expensive to run, so the decision
    of how many belongs here, at the point where the work is created, and each
    repeat gets its own `run_id` because each is a separate measurement.

    Raises `DispatchIncomplete` if the broker does not acknowledge everything.
    The messages already returned may still have been delivered, so the error
    names a count and does not claim the dispatch did nothing.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be at least 1, got {repeats}")

    published: list[TaskRunMessage] = []
    for task in tasks:
        for _ in range(repeats):
            message = TaskRunMessage(
                run_id=ids.new_run_id(),
                task_id=task.task_id,
                suite=task.suite,
                fixture_sha256=task.fixture_sha256,
                agent_id=configuration.agent_id,
                provider_id=configuration.provider_id,
                model_id=configuration.model_id,
                prompt_id=configuration.prompt_id,
                image=configuration.image,
                # Read per message, not once per dispatch. The gap between
                # this and the worker's own clock is the queue latency, and a
                # single timestamp for the whole suite would make the last
                # task look as though it waited as long as the first.
                dispatched_at_ms=clock.now_unix_ms(),
            )
            publisher.publish(message.partition_key, encode(message))
            published.append(message)

    # Publishing is asynchronous: nothing is on the broker until this returns
    # zero. A dispatcher that exited without flushing would print a suite it
    # had not actually sent.
    outstanding = publisher.flush(flush_timeout_s)
    if outstanding:
        raise DispatchIncomplete(
            f"{outstanding} of {len(published)} messages were not acknowledged "
            f"within {flush_timeout_s:g}s; some work may still have been sent"
        )
    return tuple(published)
