"""The collector's step: decode, store once, commit.

Run against the real SQLite repository, because the duplicate check compares
a record read back from storage with one decoded off the wire, and that
equality is only worth anything if both sides are the real ones.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest

from control_plane.adapters import record_json
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.model import MicroUsd, ModelInvocation, ModelUsage
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.record import RunRecord
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.services.collect import Collection, Collector
from control_plane.services.dispatch import RunResultMessage
from tests.bus_fakes import FakeConsumer, FakeTopic
from tests.fakes import EPOCH_MS
from tests.test_adapter_record_json import failed_run
from tests.test_domain_record import record

INVOCATIONS = (
    ModelInvocation(
        provider_id="ollama",
        model_id="qwen2.5-coder:7b",
        usage=ModelUsage(input_tokens=1200, output_tokens=340),
        cost=MicroUsd(3),
        latency_ms=8_100,
    ),
)


def measured() -> RunRecord:
    """A scored run with a report and an invocation: the most to get wrong."""
    return record(
        invocations=INVOCATIONS,
        total_cost_micros=MicroUsd(3),
        outcome=AgentOutcome.NO_PATCH_PRODUCED,
    )


class Harness:
    def __init__(self, repository: SqliteRunRepository) -> None:
        self.results = FakeTopic(partitions=1)
        self.consumer: FakeConsumer = self.results.consumer()
        self.repository = repository
        self.collector = Collector(
            consumer=self.consumer, store=repository, decode=record_json.decode
        )

    def send(self, stored: RunRecord, worker_id: str = "worker-01") -> None:
        message = RunResultMessage(
            record=stored,
            worker_id=worker_id,
            dispatched_at_ms=EPOCH_MS,
            completed_at_ms=EPOCH_MS + 90_000,
        )
        self.results.publish(message.partition_key, record_json.encode(message))


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    with SqliteRunRepository(tmp_path / "collector.sqlite3") as repository:
        yield Harness(repository)


@pytest.mark.parametrize(
    "sent",
    [measured(), record(run=failed_run(), report=None, score=None, outcome=None)],
    ids=["scored", "failed"],
)
def test_a_result_is_stored_whole(harness: Harness, sent: RunRecord) -> None:
    collected = harness.collector.step(0)
    assert collected is None  # nothing sent yet

    harness.send(sent)
    collected = harness.collector.step(0)

    assert collected is not None
    assert collected.collection is Collection.STORED
    assert harness.repository.get(sent.run.run_id) == sent
    assert harness.consumer.committed == 1


def test_an_identical_duplicate_is_acknowledged_and_not_stored_again(
    harness: Harness,
) -> None:
    harness.send(measured())
    harness.send(measured())

    first = harness.collector.step(0)
    second = harness.collector.step(0)

    assert first is not None and first.collection is Collection.STORED
    assert second is not None and second.collection is Collection.DUPLICATE
    assert len(harness.repository.list_runs()) == 1
    assert harness.consumer.committed == 2


def test_a_conflicting_duplicate_keeps_the_first_and_says_so(harness: Harness) -> None:
    first = measured()
    second = dataclasses.replace(first, agent_ms=(first.agent_ms or 0) + 1)
    harness.send(first)
    harness.send(second, worker_id="worker-02")

    harness.collector.step(0)
    conflict = harness.collector.step(0)

    assert conflict is not None
    assert conflict.collection is Collection.CONFLICTING_DUPLICATE
    assert "worker-02" in conflict.detail
    assert harness.repository.get(first.run.run_id) == first
    assert harness.consumer.committed == 2


def test_bytes_that_are_not_a_result_are_committed_past(harness: Harness) -> None:
    harness.results.publish(b"run-0001", b"{}")

    collected = harness.collector.step(0)

    assert collected is not None
    assert collected.collection is Collection.UNDECODABLE
    assert harness.repository.list_runs() == ()
    assert harness.consumer.committed == 1


def test_an_unfinished_run_is_refused_rather_than_stored(harness: Harness) -> None:
    # Stored, the recovery scan here would treat it as this machine's abandoned
    # run and delete a workspace path that names another machine's directory.
    in_progress = EvaluationRun.create(
        RunId("run-0009"), TaskId("csv-quoted-fields"), EPOCH_MS
    ).transition_to(RunState.PREPARING, EPOCH_MS)
    harness.send(
        record(
            run=in_progress,
            report=None,
            score=None,
            outcome=None,
            workspace_path="/elsewhere/runs/run-0009",
        )
    )

    collected = harness.collector.step(0)

    assert collected is not None
    assert collected.collection is Collection.UNFINISHED
    assert harness.repository.get(RunId("run-0009")) is None
    assert harness.consumer.committed == 1


def test_the_repository_refuses_to_store_an_unfinished_run_whole(
    harness: Harness,
) -> None:
    in_progress = EvaluationRun.create(
        RunId("run-0009"), TaskId("csv-quoted-fields"), EPOCH_MS
    )

    with pytest.raises(ValueError, match="only a finished run"):
        harness.repository.store(
            record(run=in_progress, report=None, score=None, outcome=None)
        )


def test_a_store_that_fails_partway_leaves_no_row(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason `store` exists: the row and its report commit together."""

    def refuse(*_: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(harness.repository, "_write_report", refuse)

    with pytest.raises(RuntimeError, match="disk full"):
        harness.repository.store(measured())

    assert harness.repository.get(measured().run.run_id) is None


def test_a_result_that_could_not_be_stored_is_not_acknowledged(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Store, then commit: a failed write must leave the result on the topic."""

    def refuse(_: RunRecord) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(harness.repository, "store", refuse)
    harness.send(measured())

    with pytest.raises(RuntimeError, match="locked"):
        harness.collector.step(0)

    assert harness.consumer.committed == 0
