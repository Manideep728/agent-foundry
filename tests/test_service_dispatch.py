"""The dispatch contract's own invariants.

Each of these guards a way a dispatched run would produce a plausible but
wrong benchmark number: a duplicate counted twice, two checkouts measuring
different task bodies under one name, or a pool of workers reporting a mix of
configurations as one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.dispatch_json import decode, encode
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.task import Suite, TaskDefinition
from control_plane.services.dispatch import (
    RUN_RESULTS_TOPIC,
    SCHEMA_VERSION,
    TASK_RUNS_TOPIC,
    DispatchIncomplete,
    IncompatibleMessage,
    RunConfiguration,
    TaskRunMessage,
    dispatch,
)
from tests.bus_fakes import FakeTopic
from tests.fakes import FakeClock, FakeIdGenerator
from tests.test_domain_task import VALID_SHA, make_task

TOPICS_SCRIPT = Path(__file__).resolve().parent.parent / "infra/kafka/topics.sh"


def make_message(**overrides: object) -> TaskRunMessage:
    """A valid message, with named fields overridden per test."""
    fields: dict[str, object] = {
        "run_id": RunId("run-0001"),
        "task_id": TaskId("csv-parser-001"),
        "suite": Suite.SYNTHETIC,
        "fixture_sha256": VALID_SHA,
        "agent_id": "single-shot",
        "provider_id": "ollama",
        "model_id": "qwen2.5-coder:7b",
        "prompt_id": "tools-v2",
        "image": "afrunner:local",
        "dispatched_at_ms": 1_726_000_000_000,
    }
    fields.update(overrides)
    return TaskRunMessage(**fields)  # type: ignore[arg-type]


def test_a_valid_message_is_accepted() -> None:
    message = make_message()

    assert message.schema_version == SCHEMA_VERSION
    assert message.prompt_id == "tools-v2"


def test_the_partition_key_is_the_task_not_the_run() -> None:
    """Keying on the run would spread every message and order nothing.

    Kafka orders within a partition only. Two dispatches of the same task must
    land on the same partition so the second cannot overtake the first; two
    runs of it have different `run_id`s, so a run-keyed message is effectively
    randomly placed.
    """
    first = make_message(run_id=RunId("run-0001"))
    second = make_message(run_id=RunId("run-0002"))

    assert first.partition_key == b"csv-parser-001"
    assert first.partition_key == second.partition_key


def test_a_worker_refuses_a_task_whose_fixture_differs() -> None:
    """Two internally consistent checkouts holding different task bodies.

    Loading already refuses a fixture that drifted from the hash its own task
    file pins. That check cannot see this case, because both sides pass it.
    """
    message = make_message(fixture_sha256="b" * 64)

    with pytest.raises(IncompatibleMessage, match="do not hold the same task"):
        message.accepts(make_task())


def test_a_worker_refuses_a_message_for_another_task() -> None:
    message = make_message(task_id=TaskId("template-render-001"))

    with pytest.raises(IncompatibleMessage, match="template-render-001"):
        message.accepts(make_task())


def test_a_matching_task_is_accepted() -> None:
    make_message().accepts(make_task())


@pytest.mark.parametrize("field", ["agent_id", "provider_id", "model_id", "image"])
def test_an_empty_configuration_field_is_rejected(field: str) -> None:
    """A blank model or image would group runs under a configuration nobody chose."""
    with pytest.raises(IncompatibleMessage, match=field):
        make_message(**{field: "   "})


def test_a_negative_dispatch_time_is_rejected() -> None:
    with pytest.raises(IncompatibleMessage, match="dispatched_at_ms"):
        make_message(dispatched_at_ms=-1)


def test_a_prompt_is_optional() -> None:
    """The fake agent reads no instructions; None is the honest answer."""
    assert make_message(prompt_id=None).prompt_id is None


@pytest.mark.parametrize("topic", [TASK_RUNS_TOPIC, RUN_RESULTS_TOPIC])
def test_the_setup_script_creates_the_topic_the_code_produces_to(topic: str) -> None:
    """Auto-creation is off, so a drift here is not a warning but silence.

    A dispatcher renamed on one side would produce to a topic that does not
    exist and no worker consumes, and nothing in either file would say so.
    """
    assert topic in TOPICS_SCRIPT.read_text(encoding="utf-8")


@pytest.mark.parametrize("topic", [TASK_RUNS_TOPIC, RUN_RESULTS_TOPIC])
def test_a_topic_name_contains_no_dot_or_underscore(topic: str) -> None:
    """Kafka warns on either, because both become '_' in a metric name.

    A warning printed on every start is a warning nobody reads, so the names
    avoid the characters rather than the project learning to ignore it.
    """
    assert "." not in topic
    assert "_" not in topic


class UnflushableTopic(FakeTopic):
    """A broker that never acknowledges. Models a dispatch that did not land."""

    def __init__(self, outstanding: int) -> None:
        super().__init__()
        self._outstanding = outstanding

    def flush(self, timeout_s: float) -> int:
        super().flush(timeout_s)
        return self._outstanding


def make_configuration(**overrides: object) -> RunConfiguration:
    fields: dict[str, object] = {
        "agent_id": "single-shot",
        "provider_id": "ollama",
        "model_id": "qwen2.5-coder:7b",
        "prompt_id": "tools-v2",
        "image": "afrunner:local",
    }
    fields.update(overrides)
    return RunConfiguration(**fields)  # type: ignore[arg-type]


def three_tasks() -> list[TaskDefinition]:
    return [
        make_task(task_id=TaskId("alpha-001"), fixture_id="alpha"),
        make_task(task_id=TaskId("beta-001"), fixture_id="beta"),
        make_task(task_id=TaskId("gamma-001"), fixture_id="gamma"),
    ]


def run_dispatch(
    tasks: list[TaskDefinition],
    topic: FakeTopic,
    **kwargs: object,
) -> tuple[TaskRunMessage, ...]:
    return dispatch(
        tasks,
        make_configuration(),
        topic,
        FakeIdGenerator(),
        FakeClock(),
        encode,
        **kwargs,  # type: ignore[arg-type]
    )


def test_one_message_is_published_for_each_task() -> None:
    topic = FakeTopic()

    sent = run_dispatch(three_tasks(), topic)

    assert len(sent) == 3
    assert len(topic.delivered) == 3
    assert [m.task_id.value for m in sent] == ["alpha-001", "beta-001", "gamma-001"]


def test_what_is_published_decodes_to_what_was_returned() -> None:
    """The returned messages must describe the bytes, not a parallel record."""
    topic = FakeTopic()

    sent = run_dispatch(three_tasks(), topic)

    assert [decode(d.value) for d in topic.delivered] == list(sent)


def test_every_message_carries_the_configuration() -> None:
    """A worker reads these instead of its own flags, so they must be present."""
    topic = FakeTopic()

    sent = run_dispatch(three_tasks(), topic)

    for message in sent:
        assert message.agent_id == "single-shot"
        assert message.model_id == "qwen2.5-coder:7b"
        assert message.prompt_id == "tools-v2"
        assert message.image == "afrunner:local"


def test_each_message_carries_the_fixture_hash_of_its_own_task() -> None:
    topic = FakeTopic()
    tasks = three_tasks()

    sent = run_dispatch(tasks, topic)

    assert [m.fixture_sha256 for m in sent] == [t.fixture_sha256 for t in tasks]


def test_repeats_produce_separate_runs_of_the_same_task() -> None:
    """Each repeat is its own measurement, so each needs its own identifier."""
    topic = FakeTopic()

    sent = run_dispatch(three_tasks(), topic, repeats=2)

    assert len(sent) == 6
    assert len({m.run_id.value for m in sent}) == 6
    assert [m.task_id.value for m in sent].count("alpha-001") == 2


def test_repeats_of_one_task_share_a_partition() -> None:
    """Repeats are keyed by task, so a range is read from one partition."""
    topic = FakeTopic()

    run_dispatch([make_task(task_id=TaskId("alpha-001"))], topic, repeats=4)

    assert len({d.partition for d in topic.delivered}) == 1


def test_the_dispatch_is_flushed() -> None:
    """Without a flush the process can exit before the broker has the work."""
    topic = FakeTopic()

    run_dispatch(three_tasks(), topic)

    assert topic.flush_calls != []


def test_an_unacknowledged_dispatch_is_an_error_naming_the_count() -> None:
    topic = UnflushableTopic(outstanding=2)

    with pytest.raises(DispatchIncomplete, match="2 of 3 messages"):
        run_dispatch(three_tasks(), topic)


def test_no_repeats_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        run_dispatch(three_tasks(), FakeTopic(), repeats=0)


def test_dispatching_nothing_publishes_nothing() -> None:
    topic = FakeTopic()

    assert run_dispatch([], topic) == ()
    assert topic.delivered == ()


class TickingClock(FakeClock):
    """Moves a second every time it is read, so two reads cannot coincide."""

    def now_unix_ms(self) -> int:
        value = super().now_unix_ms()
        self.advance(1000)
        return value


def test_each_message_is_timestamped_when_it_is_built() -> None:
    """One timestamp for the whole suite would hide how long work waited."""
    sent = dispatch(
        three_tasks(),
        make_configuration(),
        FakeTopic(),
        FakeIdGenerator(),
        TickingClock(),
        encode,
    )

    stamps = [m.dispatched_at_ms for m in sent]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 3
