"""The wire encoding of a `TaskRunMessage`.

A message is not trusted input. It was produced by some other process, quite
possibly an older build of this one, and it has sat in a log that outlives
both. Every way the bytes can be wrong -- not UTF-8, not JSON, not an object,
a missing key, an unknown key, a value of the wrong type, an identifier that
would not be safe as a path segment -- is reported as one `MalformedMessage`.

Fields are mapped by hand rather than by `asdict`. The mapping is the wire
format, and a reflective encoder makes renaming a field a silent format
change: the rename passes every test, the new build writes the new name, and
the workers still running the old build reject every message.

Keys are matched exactly. An unknown key is an error, for the same reason it
is in `result_json`: a key this build does not recognise means the two sides
disagree about the contract, and the disagreement is worth a loud failure
rather than a measurement that quietly ignores half of its configuration.
"""

from __future__ import annotations

import json
from typing import Any

from control_plane.domain.errors import DomainError
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.task import Suite
from control_plane.services.dispatch import SCHEMA_VERSION, TaskRunMessage


class MalformedMessage(Exception):
    """The bytes cannot be turned into a `TaskRunMessage`."""


# The JSON type each field must have. `bool` would pass an `int` check in
# Python, so nothing here is declared `int` that could legitimately arrive as
# `true`; `schema_version` and `dispatched_at_ms` are both genuinely numeric.
_TYPES: dict[str, tuple[type, ...]] = {
    "schema_version": (int,),
    "run_id": (str,),
    "task_id": (str,),
    "suite": (str,),
    "fixture_sha256": (str,),
    "agent_id": (str,),
    "provider_id": (str,),
    "model_id": (str,),
    "prompt_id": (str, type(None)),
    "image": (str,),
    "dispatched_at_ms": (int,),
}


def encode(message: TaskRunMessage) -> bytes:
    """Serialise a message to the bytes that go on the topic.

    `sort_keys` and a compact separator, so the same message always produces
    the same bytes. That is what makes a redelivered message recognisable as
    the same message by comparison rather than only by `run_id`, and it keeps
    a stored payload diffable.
    """
    payload = {
        "schema_version": message.schema_version,
        "run_id": message.run_id.value,
        "task_id": message.task_id.value,
        "suite": str(message.suite),
        "fixture_sha256": message.fixture_sha256,
        "agent_id": message.agent_id,
        "provider_id": message.provider_id,
        "model_id": message.model_id,
        "prompt_id": message.prompt_id,
        "image": message.image,
        "dispatched_at_ms": message.dispatched_at_ms,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode(raw: bytes) -> TaskRunMessage:
    """Parse the bytes of one message, or raise `MalformedMessage`.

    The version is checked before anything else is read. A message from a
    future build may use a key this one would reject as unknown, and "unknown
    key `budget_micros`" is a worse diagnosis than "this build speaks version
    1 and the message is version 2".
    """
    fields = _object(raw)
    _check_version(fields)

    missing = sorted(set(_TYPES) - set(fields))
    if missing:
        raise MalformedMessage(f"message is missing {', '.join(missing)}")
    unknown = sorted(set(fields) - set(_TYPES))
    if unknown:
        raise MalformedMessage(f"message has unknown key(s) {', '.join(unknown)}")

    for key, allowed in _TYPES.items():
        if not isinstance(fields[key], allowed):
            names = " or ".join(t.__name__ for t in allowed)
            raise MalformedMessage(
                f"{key} must be {names}, got {type(fields[key]).__name__}"
            )

    try:
        suite = Suite(fields["suite"])
    except ValueError:
        raise MalformedMessage(
            f"suite must be one of {', '.join(s.value for s in Suite)}, "
            f"got {fields['suite']!r}"
        ) from None

    # Identifier validation and the message's own invariants both raise
    # `DomainError`, and both mean the same thing here: these bytes are not a
    # message. Letting `InvalidIdentifier` escape would make a caller that
    # catches `MalformedMessage` miss exactly the values that are dangerous --
    # a `run_id` of `../../etc` becomes a directory the worker creates.
    try:
        return TaskRunMessage(
            run_id=RunId(fields["run_id"]),
            task_id=TaskId(fields["task_id"]),
            suite=suite,
            fixture_sha256=fields["fixture_sha256"],
            agent_id=fields["agent_id"],
            provider_id=fields["provider_id"],
            model_id=fields["model_id"],
            prompt_id=fields["prompt_id"],
            image=fields["image"],
            dispatched_at_ms=fields["dispatched_at_ms"],
            schema_version=fields["schema_version"],
        )
    except DomainError as error:
        raise MalformedMessage(str(error)) from error


def _object(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise MalformedMessage(f"message is not UTF-8: {error}") from error
    except json.JSONDecodeError as error:
        raise MalformedMessage(f"message is not JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise MalformedMessage(
            f"message must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _check_version(fields: dict[str, Any]) -> None:
    version = fields.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise MalformedMessage(f"schema_version must be an integer, got {version!r}")
    if version > SCHEMA_VERSION:
        raise MalformedMessage(
            f"message is schema_version {version} and this build understands "
            f"at most {SCHEMA_VERSION}"
        )
    if version < SCHEMA_VERSION:
        # Refused rather than upgraded. There is one version, so there is no
        # migration to write yet, and inventing defaults for an older message
        # would mean a worker filling in a configuration the dispatcher never
        # chose -- which is the class of defect this contract exists to stop.
        raise MalformedMessage(
            f"message is schema_version {version} and this build writes "
            f"{SCHEMA_VERSION}; no downgrade path exists"
        )
