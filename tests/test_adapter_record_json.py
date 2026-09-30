"""The `RunRecord` wire format.

The record is the project's output. Everything here guards a way a worker
could measure correctly and the collector still store something else: a field
that never travels, a `None` that arrives as zero, an enum that arrives as a
string nobody converts back.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from control_plane.adapters.record_json import (
    _FAILURE_FIELDS,
    _INVOCATION_FIELDS,
    _RECORD_FIELDS,
    _RUN_FIELDS,
    _SCORE_FIELDS,
    _USAGE_FIELDS,
    MalformedRecord,
    decode,
    encode,
)
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.model import MicroUsd, ModelInvocation, ModelUsage
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.scoring import Score
from control_plane.domain.states import EvaluationRun, HarnessFailure, RunState
from control_plane.services.dispatch import IncompatibleMessage, RunResultMessage
from tests.fakes import EPOCH_MS
from tests.test_domain_record import record

WORKER = "worker-01"


def message(**overrides: object) -> RunResultMessage:
    fields: dict[str, object] = {
        # A scored run must carry a report, a score and an outcome, so the
        # factory's default needs the third.
        "record": record(outcome=AgentOutcome.NO_PATCH_PRODUCED),
        "worker_id": WORKER,
        "dispatched_at_ms": EPOCH_MS,
        "completed_at_ms": EPOCH_MS + 90_000,
    }
    fields.update(overrides)
    return RunResultMessage(**fields)  # type: ignore[arg-type]


def failed_run() -> EvaluationRun:
    run = EvaluationRun.create(RunId("run-0002"), TaskId("template-render"), EPOCH_MS)
    for state in (RunState.PREPARING, RunState.AGENT_RUNNING, RunState.VALIDATING):
        run = run.transition_to(state, EPOCH_MS)
    return run.fail(
        FailureCategory.RUNNER_FAILED,
        EPOCH_MS + 10,
        detail="the result file was not written",
    )


def test_a_scored_record_survives_a_round_trip() -> None:
    sent = message()

    assert decode(encode(sent)) == sent


def test_the_report_survives_a_round_trip_inside_the_record() -> None:
    """Half the graph travels over the runner's own format; it must arrive."""
    sent = message()

    restored = decode(encode(sent)).record

    assert restored.report == sent.record.report
    assert restored.report is not None
    assert restored.report.commands == sent.record.report.commands  # type: ignore[union-attr]


def test_a_record_with_no_report_and_no_score_survives_a_round_trip() -> None:
    """A harness failure never reaches validation, so both are absent."""
    sent = message(record=record(run=failed_run(), report=None, score=None))

    restored = decode(encode(sent))

    assert restored == sent
    assert restored.record.report is None
    assert restored.record.score is None


def test_a_harness_failure_survives_a_round_trip() -> None:
    """Its two enums are different types and must not be swapped."""
    sent = message(record=record(run=failed_run(), report=None, score=None))

    failure = decode(encode(sent)).record.run.failure

    assert failure is not None
    assert failure.kind is FailureCategory.RUNNER_FAILED
    assert failure.failed_from is RunState.VALIDATING
    assert failure.detail == "the result file was not written"


def test_invocations_survive_a_round_trip_in_order() -> None:
    """An agent that edits over several turns makes several measurements."""
    invocations = (
        ModelInvocation(
            provider_id="ollama",
            model_id="qwen2.5-coder:7b",
            usage=ModelUsage(input_tokens=1200, output_tokens=340),
            cost=MicroUsd(0),
            latency_ms=8_100,
        ),
        ModelInvocation(
            provider_id="ollama",
            model_id="qwen2.5-coder:7b",
            usage=ModelUsage(
                input_tokens=2400,
                output_tokens=90,
                cache_write_tokens=16,
                cache_read_tokens=32,
            ),
            cost=MicroUsd(4),
            latency_ms=2_400,
        ),
    )
    sent = message(
        record=record(
            invocations=invocations,
            total_cost_micros=MicroUsd(4),
            outcome=AgentOutcome.NO_PATCH_PRODUCED,
        )
    )

    restored = decode(encode(sent)).record

    assert restored.invocations == invocations
    assert restored.total_cost_micros == 4


def test_a_record_with_no_invocations_survives_a_round_trip() -> None:
    """The fake agent calls no model at all."""
    restored = decode(
        encode(
            message(
                record=record(invocations=(), outcome=AgentOutcome.NO_PATCH_PRODUCED)
            )
        )
    )

    assert restored.record.invocations == ()


def test_the_outcome_enum_survives_as_an_enum() -> None:
    sent = message(record=record(outcome=AgentOutcome.MALFORMED_OUTPUT))

    assert decode(encode(sent)).record.outcome is AgentOutcome.MALFORMED_OUTPUT


def test_a_null_measurement_does_not_arrive_as_zero() -> None:
    """`agent_ms` of None means not measured. Zero means instant.

    Those are different facts, and a report that averages them is wrong in a
    direction nobody would notice.
    """
    sent = message(
        record=record(
            agent_ms=None,
            validation_ms=None,
            patch_bytes=0,
            outcome=AgentOutcome.NO_PATCH_PRODUCED,
        )
    )

    restored = decode(encode(sent)).record

    assert restored.agent_ms is None
    assert restored.validation_ms is None
    assert restored.patch_bytes == 0


def test_money_crosses_the_wire_as_an_integer() -> None:
    """No floats in measurements. A float micro-dollar would round silently."""
    # The total must equal the sum of the invocations, so the cost goes where
    # it is actually earned rather than onto the envelope alone.
    paid = ModelInvocation(
        provider_id="ollama",
        model_id="qwen2.5-coder:7b",
        usage=ModelUsage(input_tokens=10, output_tokens=5),
        cost=MicroUsd(7),
        latency_ms=120,
    )
    encoded = json.loads(
        encode(
            message(
                record=record(
                    invocations=(paid,),
                    total_cost_micros=MicroUsd(7),
                    outcome=AgentOutcome.NO_PATCH_PRODUCED,
                )
            )
        )
    )

    assert encoded["record"]["total_cost_micros"] == 7
    assert type(encoded["record"]["total_cost_micros"]) is int
    assert type(encoded["record"]["invocations"][0]["cost"]) is int


def test_encoding_is_byte_stable() -> None:
    assert encode(message()) == encode(message())


def test_bytes_that_are_not_json_are_refused() -> None:
    with pytest.raises(MalformedRecord, match="not JSON"):
        decode(b"{oops")


def test_a_record_missing_a_field_is_refused() -> None:
    """Silently defaulting it would store a measurement that was never made."""
    payload = json.loads(encode(message()))
    del payload["record"]["patch_bytes"]

    with pytest.raises(MalformedRecord, match="missing patch_bytes"):
        decode(json.dumps(payload).encode("utf-8"))


def test_a_record_with_an_unknown_field_is_refused() -> None:
    payload = json.loads(encode(message()))
    payload["record"]["budget_micros"] = 500

    with pytest.raises(MalformedRecord, match="unknown key"):
        decode(json.dumps(payload).encode("utf-8"))


def test_a_traversing_run_id_is_refused_as_a_malformed_record() -> None:
    """`run_id` becomes a directory name, so it must not escape as another type."""
    payload = json.loads(encode(message()))
    payload["record"]["run"]["run_id"] = "../../etc/passwd"

    with pytest.raises(MalformedRecord, match=r"\.\."):
        decode(json.dumps(payload).encode("utf-8"))


def test_a_boolean_where_an_integer_belongs_is_refused() -> None:
    """`True` is an `int` in Python, so an isinstance check alone passes it."""
    payload = json.loads(encode(message()))
    payload["record"]["patch_bytes"] = True

    with pytest.raises(MalformedRecord, match="patch_bytes must be an integer"):
        decode(json.dumps(payload).encode("utf-8"))


def test_an_unknown_state_is_refused() -> None:
    payload = json.loads(encode(message()))
    payload["record"]["run"]["state"] = "reticulating"

    with pytest.raises(MalformedRecord, match="state must be one of"):
        decode(json.dumps(payload).encode("utf-8"))


def test_a_newer_schema_version_is_left_for_a_newer_collector() -> None:
    payload = json.loads(encode(message()))
    payload["schema_version"] = 99

    with pytest.raises(IncompatibleMessage, match="understands at most"):
        decode(json.dumps(payload).encode("utf-8"))


def test_an_older_schema_version_is_refused() -> None:
    payload = json.loads(encode(message()))
    payload["schema_version"] = 0

    with pytest.raises(MalformedRecord, match="this build reads 1 to"):
        decode(json.dumps(payload).encode("utf-8"))


def test_the_harness_version_survives_a_round_trip() -> None:
    sent = message(
        record=record(
            outcome=AgentOutcome.NO_PATCH_PRODUCED,
            harness_version="0.1.0+62540b902356",
        )
    )

    assert decode(encode(sent)).record.harness_version == "0.1.0+62540b902356"


def as_version_1(sent: RunResultMessage) -> dict[str, Any]:
    """A result as a worker built before `harness_version` would send it."""
    payload: dict[str, Any] = json.loads(encode(sent))
    del payload["record"]["harness_version"]
    payload["schema_version"] = 1
    return payload


def test_a_version_1_result_is_still_stored_with_no_harness_version() -> None:
    """Results already on the topic when the collector is upgraded.

    Refusing them as malformed would commit past finished measurements. The
    one key they lack has an honest default: None, the column's own "this run
    did not name its build".
    """
    sent = message()

    restored = decode(json.dumps(as_version_1(sent)).encode("utf-8"))

    assert restored.record.harness_version is None
    assert restored.record == sent.record


def test_a_decoded_version_1_result_encodes_back_to_the_same_bytes() -> None:
    """`encode` must write what `decode` reads, at every readable version."""
    raw = json.dumps(as_version_1(message()), sort_keys=True, separators=(",", ":"))

    assert encode(decode(raw.encode("utf-8"))) == raw.encode("utf-8")


def test_a_version_1_message_cannot_be_given_a_harness_version() -> None:
    sent = message(
        record=record(
            outcome=AgentOutcome.NO_PATCH_PRODUCED,
            harness_version="0.1.0+62540b902356",
        ),
        schema_version=1,
    )

    with pytest.raises(RuntimeError, match="cannot carry harness_version"):
        encode(sent)


def test_a_version_1_result_carrying_a_harness_version_is_refused() -> None:
    # A key the message's own version did not have is as unknown as any other:
    # a producer that claims version 1 and sends version 2 keys is broken.
    payload = as_version_1(message())
    payload["record"]["harness_version"] = "0.1.0+62540b902356"

    with pytest.raises(MalformedRecord, match=r"unknown key\(s\) harness_version"):
        decode(json.dumps(payload).encode("utf-8"))


def test_a_current_result_without_a_harness_version_is_refused() -> None:
    # None travels as an explicit null. A missing key at version 2 is a
    # producer that forgot the field, which is the defect this codec's field
    # list exists to catch.
    payload = json.loads(encode(message()))
    del payload["record"]["harness_version"]

    with pytest.raises(MalformedRecord, match="missing harness_version"):
        decode(json.dumps(payload).encode("utf-8"))


def test_a_malformed_report_is_refused_as_a_malformed_record() -> None:
    """`result_json` raises its own type; a caller here must not miss it."""
    payload = json.loads(encode(message()))
    del payload["record"]["report"]["wall_ms"]

    with pytest.raises(MalformedRecord, match="report:"):
        decode(json.dumps(payload).encode("utf-8"))


def test_the_field_tuples_name_every_field_of_the_type_they_describe() -> None:
    """The guard that makes the derived encoder safe.

    A field added to a domain type and forgotten here would be computed
    correctly, never sent, and stored as a default. That is the defect shape
    this project has already hit four times, so it gets a guard rather than
    care.
    """
    for names, cls in (
        (_RECORD_FIELDS, RunRecord),
        (_RUN_FIELDS, EvaluationRun),
        (_FAILURE_FIELDS, HarnessFailure),
        (_SCORE_FIELDS, Score),
        (_INVOCATION_FIELDS, ModelInvocation),
        (_USAGE_FIELDS, ModelUsage),
    ):
        assert set(names) == {field.name for field in dataclasses.fields(cls)}, (
            f"{cls.__name__} and its field tuple disagree"
        )
