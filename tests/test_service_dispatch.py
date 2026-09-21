"""The dispatch contract's own invariants.

Each of these guards a way a dispatched run would produce a plausible but
wrong benchmark number: a duplicate counted twice, two checkouts measuring
different task bodies under one name, or a pool of workers reporting a mix of
configurations as one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.task import Suite
from control_plane.services.dispatch import (
    RUN_RESULTS_TOPIC,
    SCHEMA_VERSION,
    TASK_RUNS_TOPIC,
    IncompatibleMessage,
    TaskRunMessage,
)
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
