"""The wire encoding, treated as untrusted input.

A message is produced by another process, possibly an older build, and sits in
a log that outlives both. Every test here is a way those bytes can be wrong,
and the thing each one guards against is a worker acting on a message it did
not actually understand.
"""

from __future__ import annotations

import json

import pytest

from control_plane.adapters.dispatch_json import (
    MalformedMessage,
    decode,
    encode,
)
from control_plane.domain.ids import RunId
from control_plane.services.dispatch import SCHEMA_VERSION, IncompatibleMessage
from tests.test_service_dispatch import make_message


def payload(**overrides: object) -> bytes:
    """The encoding of a valid message, with named keys altered per test."""
    fields = json.loads(encode(make_message()))
    for key, value in overrides.items():
        if value is ...:
            del fields[key]
        else:
            fields[key] = value
    return json.dumps(fields).encode("utf-8")


def test_a_message_survives_a_round_trip() -> None:
    message = make_message()

    assert decode(encode(message)) == message


def test_a_message_without_a_prompt_survives_a_round_trip() -> None:
    """None must stay None, not become the string "None"."""
    message = make_message(prompt_id=None)

    assert decode(encode(message)).prompt_id is None


def test_encoding_is_byte_stable() -> None:
    """The same message must always produce the same bytes.

    Without sorted keys this passes by luck of dict ordering, and a stored
    payload stops being comparable to the one that produced it.
    """
    assert encode(make_message()) == encode(make_message())


def test_bytes_that_are_not_utf8_are_refused() -> None:
    with pytest.raises(MalformedMessage, match="not UTF-8"):
        decode(b"\xff\xfe not text")


def test_bytes_that_are_not_json_are_refused() -> None:
    with pytest.raises(MalformedMessage, match="not JSON"):
        decode(b"{unterminated")


def test_a_json_array_is_refused() -> None:
    """`json.loads` is happy with a list; every field read after it would not be."""
    with pytest.raises(MalformedMessage, match="must be a JSON object"):
        decode(b'["csv-parser-001"]')


def test_a_missing_field_is_refused() -> None:
    with pytest.raises(MalformedMessage, match="missing model_id"):
        decode(payload(model_id=...))


def test_an_unknown_field_is_refused() -> None:
    """The two sides disagree about the contract; that is worth a loud failure."""
    with pytest.raises(MalformedMessage, match="unknown key"):
        decode(payload(budget_micros=500))


def test_a_field_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(MalformedMessage, match="dispatched_at_ms must be int"):
        decode(payload(dispatched_at_ms="1726000000000"))


def test_an_unknown_suite_is_refused() -> None:
    """A suite this build does not know would be reported under a name it invents."""
    with pytest.raises(MalformedMessage, match="suite must be one of"):
        decode(payload(suite="swebench"))


def test_a_traversing_identifier_is_refused_as_a_malformed_message() -> None:
    """The dangerous values must not escape as a different exception type.

    A caller catching `MalformedMessage` would otherwise miss exactly the
    values that matter: `run_id` becomes a workspace directory name.
    """
    with pytest.raises(MalformedMessage, match=r"\.\."):
        decode(payload(run_id="../../etc/passwd"))


def test_a_future_schema_version_is_refused_by_version_not_by_key() -> None:
    """The diagnosis must name the version, not a key the future build added.

    Checking keys first would report "unknown key budget_micros", which sends
    the reader looking for a typo instead of for a newer producer.

    Incompatible rather than malformed: a newer worker can honour it, so a
    worker leaves it uncommitted instead of skipping it (D28).
    """
    with pytest.raises(IncompatibleMessage, match="understands at most"):
        decode(payload(schema_version=SCHEMA_VERSION + 1, budget_micros=500))


def test_an_older_schema_version_is_refused() -> None:
    with pytest.raises(MalformedMessage, match="no downgrade path"):
        decode(payload(schema_version=SCHEMA_VERSION - 1))


def test_a_boolean_schema_version_is_refused() -> None:
    """`True` is an `int` in Python, so an isinstance check alone passes it."""
    with pytest.raises(MalformedMessage, match="schema_version must be an integer"):
        decode(payload(schema_version=True))


def test_the_encoded_run_id_is_the_identifier_the_dispatcher_minted() -> None:
    """The idempotency key must survive the wire unchanged."""
    message = make_message(run_id=RunId("run-00042"))

    assert json.loads(encode(message))["run_id"] == "run-00042"
