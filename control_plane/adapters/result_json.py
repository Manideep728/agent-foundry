"""Reading the runner's `result.json` into domain types.

The file is not trusted input. It is written into `/out`, and the patch under
test runs in the same container as the same user, so it can overwrite the
file. Every way the bytes can be wrong -- not UTF-8, not JSON, the wrong shape,
a missing or unknown key, a value of the wrong type, a broken invariant -- is
therefore reported as one `MalformedResult`, never as whatever exception the
standard library happens to raise.

Keys are matched exactly against the dataclass fields. An unknown key is an
error rather than ignored, because a key this build does not recognise means
the runner and the reader disagree about the contract.
"""

from __future__ import annotations

import json
from typing import Any

from control_plane.domain.errors import InvalidValidationReport
from control_plane.domain.validation import (
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)


class MalformedResult(Exception):
    """The result file cannot be turned into a `ValidationReport`."""


# The JSON type each scalar field must have. `bool` is checked before `int`
# because in Python `True` is an `int`, and `"exit_code": true` must not pass
# as exit code 1.
_STREAM_TYPES: dict[str, tuple[type, ...]] = {
    "bytes_total": (int,),
    "bytes_written": (int,),
    "truncated": (bool,),
    "path": (str,),
    "tail": (str,),
}
_COMMAND_TYPES: dict[str, tuple[type, ...]] = {
    "index": (int,),
    "command": (str,),
    "started_at_unix_ms": (int,),
    "duration_ms": (int,),
    "outcome": (str,),
    "stdout": (dict,),
    "stderr": (dict,),
    "exit_code": (int, type(None)),
    "signal": (int, type(None)),
    "timed_out": (bool,),
    "deadline_signal_sent": (bool,),
    "killed_after_grace": (bool,),
    "group_escaped": (bool,),
    "drain_deadline_hit": (bool,),
    "orphans_reaped": (int,),
    "spawn_error": (str, type(None)),
}
_REPORT_TYPES: dict[str, tuple[type, ...]] = {
    "schema_version": (int,),
    "runner_version": (str,),
    "arch": (str,),
    "pid1": (bool,),
    "shell": (str,),
    "workdir": (str,),
    "started_at_unix_ms": (int,),
    "wall_ms": (int,),
    "exit_reason": (str,),
    "aborted": (bool,),
    "orphans_reaped_total": (int,),
    "commands": (list,),
}


def decode_report(data: bytes) -> ValidationReport:
    """Decode and validate a result file's bytes.

    Raises `MalformedResult` for every defect, with the cause chained.
    """
    try:
        raw = json.loads(data.decode("utf-8"))
        return _report(raw)
    except MalformedResult:
        raise
    except (ValueError, InvalidValidationReport) as error:
        # ValueError covers bad UTF-8, bad JSON and an unknown enum value.
        raise MalformedResult(str(error)) from error


def _report(raw: Any) -> ValidationReport:
    values = _checked(raw, _REPORT_TYPES, "report")
    return ValidationReport(
        **{
            **values,
            "exit_reason": ExitReason(values["exit_reason"]),
            "commands": tuple(
                _command(entry, position)
                for position, entry in enumerate(values["commands"])
            ),
        }
    )


def _command(raw: Any, position: int) -> CommandResult:
    where = f"commands[{position}]"
    values = _checked(raw, _COMMAND_TYPES, where)
    return CommandResult(
        **{
            **values,
            "outcome": CommandOutcome(values["outcome"]),
            "stdout": _stream(values["stdout"], f"{where}.stdout"),
            "stderr": _stream(values["stderr"], f"{where}.stderr"),
        }
    )


def _stream(raw: Any, where: str) -> StreamCapture:
    return StreamCapture(**_checked(raw, _STREAM_TYPES, where))


def _checked(
    raw: Any, types: dict[str, tuple[type, ...]], where: str
) -> dict[str, Any]:
    """Require an object with exactly the expected keys and value types."""
    if not isinstance(raw, dict):
        raise MalformedResult(f"{where} must be an object, got {type(raw).__name__}")

    missing = types.keys() - raw.keys()
    unknown = raw.keys() - types.keys()
    if missing or unknown:
        raise MalformedResult(
            f"{where} keys differ from the contract: "
            f"missing {sorted(missing)}, unknown {sorted(unknown)}"
        )

    for key, allowed in types.items():
        value = raw[key]
        if isinstance(value, bool) and bool not in allowed:
            raise MalformedResult(f"{where}.{key} must not be a boolean")
        if not isinstance(value, allowed):
            raise MalformedResult(
                f"{where}.{key} has type {type(value).__name__}, "
                f"expected {' or '.join(t.__name__ for t in allowed)}"
            )
    return dict(raw)
