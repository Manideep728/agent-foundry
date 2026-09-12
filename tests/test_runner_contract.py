"""The contract document and the types that mirror it must not drift.

`docs/RUNNER_CONTRACT.md` is the interface between a C++ program and this one.
Two programs in two languages that agree on a schema only because someone read
both sources will drift, and the drift is silent -- it surfaces as a wrong
measurement rather than an error. This asserts they still agree.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from control_plane.domain.validation import (
    SCHEMA_VERSION,
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)

CONTRACT = Path(__file__).resolve().parent.parent / "docs" / "RUNNER_CONTRACT.md"

MIRRORED_TYPES = [ValidationReport, CommandResult, StreamCapture]

# Field rows look like `| \`name\` | type | notes |`, and a row may document two
# fields at once (`| \`stdout\`, \`stderr\` | object | ... |`). The character
# class must include digits -- `pid1` is a real field, and leaving them out
# silently under-reports what the document covers.
_FIELD_ROW = re.compile(r"^\| `([a-z0-9_]+)`(?:, `([a-z0-9_]+)`)? \|", re.MULTILINE)


def documented_fields() -> set[str]:
    text = CONTRACT.read_text(encoding="utf-8")
    names: set[str] = set()
    for first, second in _FIELD_ROW.findall(text):
        names.add(first)
        if second:
            names.add(second)
    return names


def mirrored_fields() -> set[str]:
    return {field.name for cls in MIRRORED_TYPES for field in dataclasses.fields(cls)}


def test_the_contract_document_exists() -> None:
    assert CONTRACT.is_file(), f"{CONTRACT} is the schema; it must be in the repo"


@pytest.mark.parametrize("cls", MIRRORED_TYPES)
def test_every_field_of_every_mirrored_type_is_documented(cls: Any) -> None:
    # Catches a field added to the Python side that the C++ implementer would
    # never learn about.
    undocumented = {f.name for f in dataclasses.fields(cls)} - documented_fields()
    assert not undocumented, (
        f"{cls.__name__} has fields absent from RUNNER_CONTRACT.md: "
        f"{sorted(undocumented)}"
    )


def test_the_document_describes_no_field_that_does_not_exist() -> None:
    # The other direction. Catches a field renamed in code while the document
    # keeps promising the old name to whoever writes the runner.
    phantom = documented_fields() - mirrored_fields()
    assert not phantom, (
        f"RUNNER_CONTRACT.md documents fields no type has: {sorted(phantom)}"
    )


def test_the_documented_schema_version_matches_the_code() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    assert f"Schema version: **{SCHEMA_VERSION}**" in text


@pytest.mark.parametrize("outcome", list(CommandOutcome))
def test_every_command_outcome_appears_in_the_document(
    outcome: CommandOutcome,
) -> None:
    # An outcome the runner can emit but the document never mentions is a value
    # the other implementer has no reason to produce or handle.
    assert f"`{outcome.value}`" in CONTRACT.read_text(encoding="utf-8")


@pytest.mark.parametrize("reason", list(ExitReason))
def test_every_exit_reason_appears_in_the_document(reason: ExitReason) -> None:
    assert f"`{reason.value}`" in CONTRACT.read_text(encoding="utf-8")


def test_the_document_states_the_rules_that_decide_a_measurement() -> None:
    # These three are the ones whose absence would produce a plausible but
    # wrong benchmark, so the document is not allowed to lose them quietly.
    text = CONTRACT.read_text(encoding="utf-8")
    assert "authority for reporting" in text  # wait status vs deadline
    assert "137" in text  # SIGKILL is not a failed test
    assert "waitpid(-1" in text  # PID 1 misattribution
