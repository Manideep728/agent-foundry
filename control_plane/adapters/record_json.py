"""The wire encoding of a finished `RunRecord`.

A worker measures and a collector stores, so the whole record crosses a
network. That record is the project's output: twenty-five fields on the
envelope alone, over a graph of nine dataclasses, and every one of them is a
measurement somebody will later read off a table.

**The field names are declared once per type and guarded.** Each `_FIELDS`
tuple below is checked against `dataclasses.fields()` by a test. This is not
ceremony: a field added to `RunRecord` and forgotten here would be computed
correctly, sent as nothing, and stored as a default, and the number would be
missing from the report with nothing anywhere saying so. That defect shape has
already happened four times in this project, which is why the storage layer
has a schema guard. This is the same guard for the wire.

Scalars are read by name from those tuples. Everything structured -- the
nested objects, the enums, the tuples -- is written out explicitly, because
those are the places where a reflective encoder would silently produce
something `json.dumps` accepts and the decoder cannot read back.

The validation report is not re-encoded here. It already has a wire format:
the C++ runner writes it and `result_json` reads it. Reusing that format means
half this graph travels over bytes that are already specified and already
tested against a real runner's output.
"""

from __future__ import annotations

import json
from typing import Any

from control_plane.adapters.result_json import (
    MalformedResult,
    decode_report,
    encode_report,
)
from control_plane.domain.errors import DomainError
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.model import MicroUsd, ModelInvocation, ModelUsage
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.scoring import Score
from control_plane.domain.states import EvaluationRun, HarnessFailure, RunState
from control_plane.domain.task import Suite
from control_plane.services.dispatch import (
    OLDEST_READABLE_VERSION,
    SCHEMA_VERSION,
    IncompatibleMessage,
    RunResultMessage,
    UndecodableMessage,
)


class MalformedRecord(UndecodableMessage):
    """The bytes cannot be turned into a `RunResultMessage`."""


# Every field of each type, in the order the type declares them. A test
# asserts each tuple equals `dataclasses.fields()` for the type it names.
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
)
_SCORE_FIELDS = (
    "resolved",
    "regressed",
    "fail_to_pass_passed",
    "fail_to_pass_total",
    "pass_to_pass_passed",
    "pass_to_pass_total",
    "timed_out_commands",
)
_INVOCATION_FIELDS = ("provider_id", "model_id", "usage", "cost", "latency_ms")
_FAILURE_FIELDS = ("kind", "failed_from", "detail")
_RUN_FIELDS = (
    "run_id",
    "task_id",
    "state",
    "created_at_unix_ms",
    "updated_at_unix_ms",
    "failure",
)
_RECORD_FIELDS = (
    "run",
    "suite",
    "fixture_sha256",
    "runner_id",
    "runner_version",
    "arch",
    "pid1",
    "report",
    "score",
    "outcome",
    "task_defect",
    "patch_sha256",
    "patch_bytes",
    "patch_is_empty",
    "agent_id",
    "provider_id",
    "model_id",
    "prompt_id",
    "invocations",
    "total_cost_micros",
    "agent_ms",
    "validation_ms",
    "workspace_path",
    "image_digest",
    "agent_note",
    "harness_version",
)

_RECORD_FIELD_ADDED_IN = {"harness_version": 2}
"""The message version each later record key first appeared in.

A key absent from this map has been in the record since version 1. A message
must carry exactly the keys of its own version: an older one lacking a newer
key decodes to that field's default, and a newer key in an older message is
as unknown as any other.
"""

# The fields of each type that are plain JSON scalars, and so can be read and
# written by name. Everything else is handled explicitly below.
_STRUCTURED = {
    "run",
    "suite",
    "report",
    "score",
    "outcome",
    "invocations",
    "run_id",
    "task_id",
    "state",
    "failure",
    "kind",
    "failed_from",
    "usage",
}


def encode(message: RunResultMessage) -> bytes:
    """Serialise a result message to the bytes that go on the results topic.

    Sorted keys and a compact separator, as on the work topic, so the same
    message always produces the same bytes.
    """
    payload = {
        "schema_version": message.schema_version,
        "worker_id": message.worker_id,
        "dispatched_at_ms": message.dispatched_at_ms,
        "completed_at_ms": message.completed_at_ms,
        "record": _for_version(_encode_record(message.record), message.schema_version),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _for_version(record: dict[str, Any], version: int) -> dict[str, Any]:
    """The record keys a message of `version` carries, and no others.

    Without this, re-encoding a decoded version 1 result would write a key
    version 1 never had, and `decode` would refuse the bytes `encode` wrote.
    A value in a key the version cannot carry is a caller bug, not data to
    drop quietly.
    """
    kept = dict(record)
    for name, added_in in _RECORD_FIELD_ADDED_IN.items():
        if added_in <= version:
            continue
        if kept.pop(name) is not None:
            raise RuntimeError(
                f"a version {version} message cannot carry {name}, "
                f"which arrived in version {added_in}"
            )
    return kept


def decode(raw: bytes) -> RunResultMessage:
    """Parse one result, or raise `MalformedRecord` or `IncompatibleMessage`.

    `IncompatibleMessage` means a newer build wrote it, and the collector
    leaves it on the topic for a collector that can store it.
    """
    fields = _object(raw, "message")
    version = _check_version(fields)

    try:
        return RunResultMessage(
            record=_decode_record(_object_at(fields, "record"), version),
            worker_id=_string(fields, "worker_id"),
            dispatched_at_ms=_integer(fields, "dispatched_at_ms"),
            completed_at_ms=_integer(fields, "completed_at_ms"),
            schema_version=_integer(fields, "schema_version"),
        )
    except MalformedResult as error:
        # The report is decoded by `result_json`, which raises its own type.
        # A caller catching `MalformedRecord` must not miss it.
        raise MalformedRecord(f"report: {error}") from error
    except DomainError as error:
        # Identifier rules and every dataclass invariant land here. Letting
        # them escape would mean a caller that rejects bad messages misses
        # exactly the values that are dangerous, as a `run_id` becomes a
        # directory name.
        raise MalformedRecord(str(error)) from error


def _encode_record(record: RunRecord) -> dict[str, Any]:
    encoded = _scalars(record, _RECORD_FIELDS)
    encoded["run"] = _encode_run(record.run)
    encoded["suite"] = record.suite.value
    encoded["report"] = None if record.report is None else encode_report(record.report)
    encoded["score"] = (
        None
        if record.score is None
        else _complete(_scalars(record.score, _SCORE_FIELDS), _SCORE_FIELDS, "Score")
    )
    encoded["outcome"] = None if record.outcome is None else record.outcome.value
    encoded["invocations"] = [
        _encode_invocation(invocation) for invocation in record.invocations
    ]
    # A NewType over int at runtime, so it needs no conversion -- but it is
    # named here rather than left to the scalar sweep, because a reader
    # checking that money crosses the wire as an integer should find it.
    encoded["total_cost_micros"] = int(record.total_cost_micros)
    return _complete(encoded, _RECORD_FIELDS, "RunRecord")


def _encode_run(run: EvaluationRun) -> dict[str, Any]:
    encoded = _scalars(run, _RUN_FIELDS)
    encoded["run_id"] = run.run_id.value
    encoded["task_id"] = run.task_id.value
    encoded["state"] = run.state.value
    encoded["failure"] = (
        None
        if run.failure is None
        else _complete(
            _scalars(run.failure, _FAILURE_FIELDS)
            | {
                "kind": run.failure.kind.value,
                "failed_from": run.failure.failed_from.value,
            },
            _FAILURE_FIELDS,
            "HarnessFailure",
        )
    )
    return _complete(encoded, _RUN_FIELDS, "EvaluationRun")


def _encode_invocation(invocation: ModelInvocation) -> dict[str, Any]:
    return _complete(
        _scalars(invocation, _INVOCATION_FIELDS)
        | {
            "usage": _complete(
                _scalars(invocation.usage, _USAGE_FIELDS), _USAGE_FIELDS, "ModelUsage"
            ),
            "cost": int(invocation.cost),
        },
        _INVOCATION_FIELDS,
        "ModelInvocation",
    )


def _scalars(source: object, names: tuple[str, ...]) -> dict[str, Any]:
    """Read the plain fields by name. The caller must add the structured ones.

    Structured names are left out rather than written as null. If they were
    written as null, a caller that forgot to overwrite one would encode a
    measurement as absent and nothing would say so -- which is the defect this
    module's field tuples exist to prevent. Leaving them out means `_complete`
    catches the omission instead.
    """
    return {name: getattr(source, name) for name in names if name not in _STRUCTURED}


def _complete(
    encoded: dict[str, Any], names: tuple[str, ...], where: str
) -> dict[str, Any]:
    """Refuse to emit an object that does not carry every declared field.

    A programming error, not a bad message, so it raises `RuntimeError`: the
    field tuple and the encoder that reads it are both in this file, and the
    two disagreeing is a bug to fix rather than input to reject.
    """
    missing = sorted(set(names) - set(encoded))
    extra = sorted(set(encoded) - set(names))
    if missing or extra:
        raise RuntimeError(
            f"{where} encoder does not match its field list: "
            f"missing {missing}, unexpected {extra}"
        )
    return encoded


def _decode_record(fields: dict[str, Any], version: int) -> RunRecord:
    expected = {
        name
        for name in _RECORD_FIELDS
        if _RECORD_FIELD_ADDED_IN.get(name, 1) <= version
    }
    missing = sorted(expected - set(fields))
    if missing:
        raise MalformedRecord(f"record is missing {', '.join(missing)}")
    unknown = sorted(set(fields) - expected)
    if unknown:
        raise MalformedRecord(f"record has unknown key(s) {', '.join(unknown)}")

    report_raw = fields["report"]
    score_raw = fields["score"]
    return RunRecord(
        run=_decode_run(_object_at(fields, "run")),
        suite=_enum(Suite, fields["suite"], "suite"),
        fixture_sha256=_string(fields, "fixture_sha256"),
        runner_id=_string(fields, "runner_id"),
        runner_version=_string(fields, "runner_version"),
        arch=_string(fields, "arch"),
        pid1=_boolean(fields, "pid1"),
        report=(
            None
            if report_raw is None
            else decode_report(json.dumps(report_raw).encode("utf-8"))
        ),
        score=None if score_raw is None else _decode_score(score_raw),
        outcome=_optional_enum(AgentOutcome, fields["outcome"], "outcome"),
        task_defect=_boolean(fields, "task_defect"),
        patch_sha256=_optional_string(fields, "patch_sha256"),
        patch_bytes=_integer(fields, "patch_bytes"),
        patch_is_empty=_optional_boolean(fields, "patch_is_empty"),
        agent_id=_optional_string(fields, "agent_id"),
        provider_id=_optional_string(fields, "provider_id"),
        model_id=_optional_string(fields, "model_id"),
        prompt_id=_optional_string(fields, "prompt_id"),
        invocations=tuple(
            _decode_invocation(entry, position)
            for position, entry in enumerate(_list_at(fields, "invocations"))
        ),
        total_cost_micros=MicroUsd(_integer(fields, "total_cost_micros")),
        agent_ms=_optional_integer(fields, "agent_ms"),
        validation_ms=_optional_integer(fields, "validation_ms"),
        workspace_path=_optional_string(fields, "workspace_path"),
        image_digest=_optional_string(fields, "image_digest"),
        agent_note=_optional_string(fields, "agent_note"),
        # Absent from a version 1 message, where `.get` makes it None: the
        # worker that sent it could not name its build, which is what None
        # means in the column too.
        harness_version=_optional_string(fields, "harness_version"),
    )


def _decode_run(fields: dict[str, Any]) -> EvaluationRun:
    failure_raw = fields.get("failure")
    return EvaluationRun(
        run_id=RunId(_string(fields, "run_id")),
        task_id=TaskId(_string(fields, "task_id")),
        state=_enum(RunState, fields.get("state"), "state"),
        created_at_unix_ms=_integer(fields, "created_at_unix_ms"),
        updated_at_unix_ms=_integer(fields, "updated_at_unix_ms"),
        failure=(
            None
            if failure_raw is None
            else HarnessFailure(
                kind=_enum(
                    FailureCategory, _object_at(fields, "failure").get("kind"), "kind"
                ),
                failed_from=_enum(
                    RunState,
                    _object_at(fields, "failure").get("failed_from"),
                    "failed_from",
                ),
                detail=_string(_object_at(fields, "failure"), "detail"),
            )
        ),
    )


def _decode_score(raw: Any) -> Score:
    fields = _as_object(raw, "score")
    return Score(
        resolved=_boolean(fields, "resolved"),
        regressed=_boolean(fields, "regressed"),
        fail_to_pass_passed=_integer(fields, "fail_to_pass_passed"),
        fail_to_pass_total=_integer(fields, "fail_to_pass_total"),
        pass_to_pass_passed=_integer(fields, "pass_to_pass_passed"),
        pass_to_pass_total=_integer(fields, "pass_to_pass_total"),
        timed_out_commands=_integer(fields, "timed_out_commands"),
    )


def _decode_invocation(raw: Any, position: int) -> ModelInvocation:
    fields = _as_object(raw, f"invocations[{position}]")
    usage = _as_object(fields.get("usage"), f"invocations[{position}].usage")
    return ModelInvocation(
        provider_id=_string(fields, "provider_id"),
        model_id=_string(fields, "model_id"),
        usage=ModelUsage(
            input_tokens=_integer(usage, "input_tokens"),
            output_tokens=_integer(usage, "output_tokens"),
            cache_write_tokens=_integer(usage, "cache_write_tokens"),
            cache_read_tokens=_integer(usage, "cache_read_tokens"),
        ),
        cost=MicroUsd(_integer(fields, "cost")),
        latency_ms=_integer(fields, "latency_ms"),
    )


def _object(raw: bytes, where: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise MalformedRecord(f"{where} is not UTF-8: {error}") from error
    except json.JSONDecodeError as error:
        raise MalformedRecord(f"{where} is not JSON: {error}") from error
    return _as_object(parsed, where)


def _as_object(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MalformedRecord(f"{where} must be an object, got {type(raw).__name__}")
    return raw


def _object_at(fields: dict[str, Any], key: str) -> dict[str, Any]:
    return _as_object(fields.get(key), key)


def _list_at(fields: dict[str, Any], key: str) -> list[Any]:
    value = fields.get(key)
    if not isinstance(value, list):
        raise MalformedRecord(f"{key} must be a list, got {type(value).__name__}")
    return value


def _string(fields: dict[str, Any], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise MalformedRecord(f"{key} must be a string, got {type(value).__name__}")
    return value


def _optional_string(fields: dict[str, Any], key: str) -> str | None:
    value = fields.get(key)
    if value is None or isinstance(value, str):
        return value
    raise MalformedRecord(f"{key} must be a string or null, got {type(value).__name__}")


def _integer(fields: dict[str, Any], key: str) -> int:
    value = fields.get(key)
    # `bool` is a subclass of `int`, so `"patch_bytes": true` would otherwise
    # be read as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedRecord(f"{key} must be an integer, got {value!r}")
    return value


def _optional_integer(fields: dict[str, Any], key: str) -> int | None:
    return None if fields.get(key) is None else _integer(fields, key)


def _boolean(fields: dict[str, Any], key: str) -> bool:
    value = fields.get(key)
    if not isinstance(value, bool):
        raise MalformedRecord(f"{key} must be a boolean, got {value!r}")
    return value


def _optional_boolean(fields: dict[str, Any], key: str) -> bool | None:
    return None if fields.get(key) is None else _boolean(fields, key)


def _enum[E: (Suite, RunState, FailureCategory, AgentOutcome)](
    kind: type[E], value: Any, key: str
) -> E:
    try:
        return kind(value)
    except ValueError:
        raise MalformedRecord(
            f"{key} must be one of "
            f"{', '.join(member.value for member in kind)}, got {value!r}"
        ) from None


def _optional_enum[E: (Suite, RunState, FailureCategory, AgentOutcome)](
    kind: type[E], value: Any, key: str
) -> E | None:
    return None if value is None else _enum(kind, value, key)


def _check_version(fields: dict[str, Any]) -> int:
    version = fields.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise MalformedRecord(f"schema_version must be an integer, got {version!r}")
    if version > SCHEMA_VERSION:
        # Left on the topic for a newer collector rather than committed past:
        # a result this build cannot store is a measurement that would
        # otherwise be lost (D28).
        raise IncompatibleMessage(
            f"message is schema_version {version} and this build understands "
            f"at most {SCHEMA_VERSION}"
        )
    if version < OLDEST_READABLE_VERSION:
        raise MalformedRecord(
            f"message is schema_version {version} and this build reads "
            f"{OLDEST_READABLE_VERSION} to {SCHEMA_VERSION}"
        )
    return version
