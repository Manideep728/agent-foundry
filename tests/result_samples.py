"""A valid `result.json` as plain data, for tests that need one to vary.

Each call returns fresh dictionaries, so a test can mutate its copy freely.
"""

from __future__ import annotations

import json
from typing import Any


def sample_stream() -> dict[str, Any]:
    return {
        "bytes_total": 6,
        "bytes_written": 6,
        "truncated": False,
        "path": "cmd-000.stdout.log",
        "tail": "hello\n",
    }


def sample_command() -> dict[str, Any]:
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
        "stdout": sample_stream(),
        "stderr": sample_stream() | {"bytes_total": 0, "bytes_written": 0, "tail": ""},
    }


def sample_report() -> dict[str, Any]:
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
        "commands": [sample_command()],
    }


def encode(raw: Any) -> bytes:
    return json.dumps(raw).encode("utf-8")
