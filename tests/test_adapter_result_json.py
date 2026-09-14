"""Decoding `result.json`.

The patch under test can write to `/out`, so every malformed input must come
back as `MalformedResult` -- a caller that sees any other exception would treat
a hostile file as a crash in the harness.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

import pytest

from control_plane.adapters import result_json
from control_plane.adapters.result_json import MalformedResult, decode_report
from control_plane.domain.validation import (
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)


def _stream() -> dict[str, Any]:
    return {
        "bytes_total": 6,
        "bytes_written": 6,
        "truncated": False,
        "path": "cmd-000.stdout.log",
        "tail": "hello\n",
    }


def _command() -> dict[str, Any]:
    return {
        "index": 0,
        "command": "echo hello",
        "started_at_unix_ms": 1_757_900_000_000,
        "duration_ms": 3,
        "outcome": "exited",
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "deadline_signal_sent": False,
        "killed_after_grace": False,
        "group_escaped": False,
        "drain_deadline_hit": False,
        "orphans_reaped": 0,
        "spawn_error": None,
        "stdout": _stream(),
        "stderr": _stream() | {"bytes_total": 0, "bytes_written": 0, "tail": ""},
    }


def _report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runner_version": "afrunner 0.1.0",
        "arch": "x86_64",
        "pid1": True,
        "shell": "/usr/bin/dash",
        "workdir": "/work",
        "started_at_unix_ms": 1_757_900_000_000,
        "wall_ms": 5,
        "exit_reason": "completed",
        "aborted": False,
        "orphans_reaped_total": 0,
        "commands": [_command()],
    }


def _encode(raw: Any) -> bytes:
    return json.dumps(raw).encode("utf-8")


def test_a_valid_report_decodes_into_domain_types() -> None:
    report = decode_report(_encode(_report()))

    assert report.pid1 is True
    assert report.exit_reason is ExitReason.COMPLETED
    command = report.commands[0]
    assert command.outcome is CommandOutcome.EXITED
    assert command.exit_code == 0
    assert command.stdout.tail == "hello\n"


def test_the_type_tables_cover_exactly_the_dataclass_fields() -> None:
    # A field added to a dataclass without a table entry would make every real
    # result fail as an unknown key.
    assert set(result_json._REPORT_TYPES) == {f.name for f in fields(ValidationReport)}
    assert set(result_json._COMMAND_TYPES) == {f.name for f in fields(CommandResult)}
    assert set(result_json._STREAM_TYPES) == {f.name for f in fields(StreamCapture)}


def _with_command(**changes: Any) -> dict[str, Any]:
    raw = _report()
    raw["commands"][0].update(changes)
    return raw


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\xff\xfe", id="not-utf8"),
        pytest.param(b'{"schema_version": 1', id="truncated-json"),
        pytest.param(b"", id="empty-file"),
        pytest.param(_encode([_report()]), id="top-level-array"),
        pytest.param(_encode(_report() | {"commands": {}}), id="commands-not-a-list"),
        pytest.param(
            _encode(_report() | {"commands": [7]}), id="command-not-an-object"
        ),
        pytest.param(_encode(_report() | {"extra": 1}), id="unknown-report-key"),
        pytest.param(
            _encode({k: v for k, v in _report().items() if k != "arch"}),
            id="missing-report-key",
        ),
        pytest.param(_encode(_report() | {"pid1": 1}), id="int-for-bool"),
        pytest.param(_encode(_with_command(exit_code=True)), id="bool-for-int"),
        pytest.param(_encode(_with_command(signal="9")), id="string-for-int"),
        pytest.param(_encode(_report() | {"wall_ms": 5.0}), id="float-measurement"),
        pytest.param(_encode(_with_command(outcome="crashed")), id="unknown-outcome"),
        pytest.param(_encode(_report() | {"exit_reason": "?"}), id="unknown-reason"),
        pytest.param(_encode(_with_command(stdout=[])), id="stream-not-an-object"),
        pytest.param(_encode(_report() | {"schema_version": 2}), id="future-schema"),
        pytest.param(_encode(_report() | {"commands": []}), id="no-commands"),
        pytest.param(_encode(_with_command(exit_code=None)), id="broken-invariant"),
    ],
)
def test_every_defect_is_a_malformed_result(data: bytes) -> None:
    with pytest.raises(MalformedResult):
        decode_report(data)
