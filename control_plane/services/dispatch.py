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

from dataclasses import dataclass

from control_plane.domain.errors import DomainError
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.task import Suite, TaskDefinition

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
