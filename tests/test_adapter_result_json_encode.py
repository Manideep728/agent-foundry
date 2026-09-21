"""Encoding a report, checked against the decoder that already existed.

The runner writes this shape and `decode_report` reads it. The encoder has to
produce the same shape, because a worker sends a finished report to a
collector over it. The decoder is the oracle: it is already tested against
bytes a real runner produced, so a round trip through it is a stronger
statement than any assertion on the encoder's own output.
"""

from __future__ import annotations

import dataclasses
import json

from control_plane.adapters.result_json import (
    _COMMAND_TYPES,
    _REPORT_TYPES,
    _STREAM_TYPES,
    decode_report,
    encode_report,
)
from control_plane.domain.validation import (
    CommandResult,
    StreamCapture,
    ValidationReport,
)
from tests.result_samples import encode, sample_command, sample_report


def a_report() -> ValidationReport:
    return decode_report(encode(sample_report()))


def test_a_report_survives_a_round_trip() -> None:
    report = a_report()

    assert decode_report(json.dumps(encode_report(report)).encode("utf-8")) == report


def test_a_report_with_several_commands_survives_a_round_trip() -> None:
    """Commands are a list, and their order is what matches a result to a role."""
    raw = sample_report()
    raw["commands"] = [
        {**sample_command(), "index": 0},
        {**sample_command(), "index": 1, "command": "pytest -q tests/test_regress.py"},
        {
            # A timed-out command carries no exit code and does carry a
            # signal: the domain requires an exit code exactly when the
            # outcome is `exited`, and a signal when it is `signaled` or
            # `timeout`.
            **sample_command(),
            "index": 2,
            "outcome": "timeout",
            "exit_code": None,
            "signal": 15,
            "timed_out": True,
            "deadline_signal_sent": True,
        },
    ]
    report = decode_report(encode(raw))

    restored = decode_report(json.dumps(encode_report(report)).encode("utf-8"))

    assert restored == report
    assert [command.index for command in restored.commands] == [0, 1, 2]


def test_the_nullable_command_fields_survive_as_null() -> None:
    """`None` must not come back as 0 or as the string "None".

    `exit_code` is the one that matters: a timed-out command that arrived with
    `exit_code` 0 would read as a command that passed.
    """
    raw = sample_report()
    raw["commands"] = [
        {
            **sample_command(),
            "outcome": "signaled",
            "exit_code": None,
            "signal": 9,
        }
    ]
    report = decode_report(encode(raw))

    restored = decode_report(json.dumps(encode_report(report)).encode("utf-8"))

    assert restored.commands[0].exit_code is None
    assert restored.commands[0].signal == 9
    assert restored == report


def test_the_encoded_object_holds_no_enum_objects() -> None:
    """`json.dumps` would accept a StrEnum silently and write its value.

    That works today and would keep working, right up to the first enum that
    is not a `StrEnum`. Converting explicitly means the encoder does not
    depend on which enum base a domain type happens to use.
    """
    encoded = encode_report(a_report())

    assert type(encoded["exit_reason"]) is str
    assert type(encoded["commands"][0]["outcome"]) is str


def test_a_stream_is_nested_as_an_object_not_flattened() -> None:
    encoded = encode_report(a_report())

    assert set(encoded["commands"][0]["stdout"]) == set(_STREAM_TYPES)


def test_the_key_tables_name_every_field_of_the_type_they_describe() -> None:
    """The guard that makes the derived encoder safe.

    The encoder reads exactly the keys these tables name. A field added to a
    domain type and forgotten here would therefore never be sent, and a
    collector would store a record missing a measurement that was computed
    correctly. This is the defect shape this project has already hit four
    times, so it gets a guard rather than care.
    """
    for table, cls in (
        (_REPORT_TYPES, ValidationReport),
        (_COMMAND_TYPES, CommandResult),
        (_STREAM_TYPES, StreamCapture),
    ):
        assert set(table) == {field.name for field in dataclasses.fields(cls)}, (
            f"{cls.__name__} and its key table disagree"
        )
