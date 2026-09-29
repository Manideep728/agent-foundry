"""Storing what the workers measured, in the one database that owns the results.

One collector reads the single-partition results topic, so the database keeps
exactly one writer -- the invariant the run lock already enforces for `af run`
and `af bench` -- and the results arrive in one total order.

**Store, then commit.** The offset is acknowledged only after the SQLite
transaction has committed. A crash between the two redelivers the result, and
the stored `run_id` recognises it.

**The first arrival wins.** A duplicate is expected: a worker that published
and died before committing sends its result again. A duplicate whose content
*differs* is not expected -- it means one run was measured twice, typically
after a worker was evicted from its group mid-run -- and it is reported rather
than dropped, because a silent choice between two measurements of one run is a
number nobody can defend. The first is kept so that replaying the topic always
produces the same database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from control_plane.domain.ids import RunId
from control_plane.domain.record import RunRecord
from control_plane.services.dispatch import (
    MessageConsumer,
    RawDelivery,
    RunResultMessage,
    UndecodableMessage,
)


class ResultStore(Protocol):
    """The shared database. `SqliteRunRepository` in production."""

    def get(self, run_id: RunId) -> RunRecord | None: ...

    def store(self, record: RunRecord) -> None: ...


class Collection(StrEnum):
    STORED = "stored"
    DUPLICATE = "duplicate"
    CONFLICTING_DUPLICATE = "conflicting-duplicate"
    UNDECODABLE = "undecodable"
    UNFINISHED = "unfinished"


@dataclass(frozen=True, slots=True)
class Collected:
    """What one step did, for the caller to report."""

    collection: Collection
    delivery: RawDelivery
    message: RunResultMessage | None
    """None only when the delivery could not be decoded."""

    detail: str = ""


class Collector:
    def __init__(
        self,
        *,
        consumer: MessageConsumer,
        store: ResultStore,
        decode: Callable[[bytes], RunResultMessage],
    ) -> None:
        self._consumer = consumer
        self._store = store
        self._decode = decode

    def step(self, timeout_s: float) -> Collected | None:
        """Store at most one result. None means the topic was quiet.

        Every outcome is committed, including the refusals: unlike a work
        message, a result that cannot be stored here cannot be stored by any
        other collector either, and leaving it would stop the only partition.
        """
        delivery = self._consumer.poll(timeout_s)
        if delivery is None:
            return None

        try:
            message = self._decode(delivery.value)
        except UndecodableMessage as error:
            self._consumer.commit(delivery)
            return Collected(Collection.UNDECODABLE, delivery, None, str(error))

        collected = self._collect(delivery, message)
        self._consumer.commit(delivery)
        return collected

    def _collect(self, delivery: RawDelivery, message: RunResultMessage) -> Collected:
        record = message.record
        if not record.run.is_terminal:
            # A worker publishes only finished runs, so this is a defect
            # somewhere upstream -- and storing it would let this machine's
            # recovery scan delete a workspace path from another machine.
            return Collected(
                Collection.UNFINISHED,
                delivery,
                message,
                f"{record.run.run_id} arrived as {record.run.state.value}",
            )

        existing = self._store.get(record.run.run_id)
        if existing is None:
            self._store.store(record)
            return Collected(Collection.STORED, delivery, message)
        if existing == record:
            return Collected(Collection.DUPLICATE, delivery, message)
        return Collected(
            Collection.CONFLICTING_DUPLICATE,
            delivery,
            message,
            f"{record.run.run_id} arrived again from {message.worker_id} with "
            f"different content; the first measurement is kept",
        )
