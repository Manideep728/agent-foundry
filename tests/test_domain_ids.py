"""Identifier validation.

The character set is not cosmetic: adapters build directory names and database
keys out of these values, so a separator or a stray newline here becomes a path
traversal or a corrupted record there.
"""

from __future__ import annotations

import dataclasses

import pytest

from control_plane.domain.errors import DomainError, InvalidIdentifier
from control_plane.domain.ids import MAX_LENGTH, RunId, TaskId

ID_TYPES = [TaskId, RunId]


@pytest.mark.parametrize("id_type", ID_TYPES)
@pytest.mark.parametrize(
    "value",
    [
        "a",
        "csv-parser",
        "csv_parser",
        "task.001",
        "run-0001",
        "0",
        "A" * MAX_LENGTH,
        "django__django-11099",  # the shape SWE-bench instance ids take
    ],
)
def test_accepts_reasonable_identifiers(
    id_type: type[TaskId] | type[RunId], value: str
) -> None:
    assert id_type(value).value == value
    assert str(id_type(value)) == value


@pytest.mark.parametrize("id_type", ID_TYPES)
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty"),
        ("A" * (MAX_LENGTH + 1), "too long"),
        ("-leading-hyphen", "must start alphanumeric"),
        ("_leading-underscore", "must start alphanumeric"),
        (".leading-dot", "must start alphanumeric"),
        # Every one of these would escape or corrupt a path built from the id.
        ("../etc/passwd", "path traversal"),
        ("a/b", "separator"),
        ("a\\b", "windows separator"),
        ("a..b", "dot-dot anywhere"),
        ("has space", "whitespace"),
        ("tab\there", "whitespace"),
        # \Z rather than $ in the pattern is what catches this one: `$` also
        # matches before a trailing newline, so a `$`-anchored regex would
        # accept it.
        ("trailing-newline\n", "trailing newline"),
        ("nul\x00byte", "null byte"),
        ("semi;colon", "shell metacharacter"),
        ("dollar$sign", "shell metacharacter"),
    ],
)
def test_rejects_dangerous_identifiers(
    id_type: type[TaskId] | type[RunId], value: str, reason: str
) -> None:
    with pytest.raises(InvalidIdentifier):
        id_type(value)


def test_invalid_identifier_is_a_domain_error() -> None:
    # Callers catch DomainError to separate "the model rejected this" from an
    # arbitrary ValueError raised inside the standard library.
    with pytest.raises(DomainError):
        TaskId("")


@pytest.mark.parametrize("id_type", ID_TYPES)
def test_identifiers_are_frozen(id_type: type[TaskId] | type[RunId]) -> None:
    identifier = id_type("stable")
    with pytest.raises(dataclasses.FrozenInstanceError):
        identifier.value = "mutated"  # type: ignore[misc]


def test_task_and_run_ids_are_distinct_types() -> None:
    # Same string, different types: equality must not hold, or a TaskId could
    # be passed where a RunId belongs and compare equal to the wrong record.
    #
    # Widened to `object` deliberately. Compared directly, mypy rejects the
    # expression as a non-overlapping equality check -- which is the same
    # property, proven statically. This asserts it holds at runtime too, since
    # dataclass __eq__ returns NotImplemented across classes and the fallback
    # is identity.
    task: object = TaskId("shared")
    run: object = RunId("shared")
    assert task != run
