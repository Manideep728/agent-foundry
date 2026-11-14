"""Names every command shares: the program name, exit codes and defaults."""

from __future__ import annotations

from pathlib import Path

from control_plane.domain.record import RunRecord
from control_plane.domain.states import RunState

PROGRAM = "af"

EXIT_MEASURED = 0
EXIT_HARNESS_FAILED = 1
EXIT_USAGE = 2
EXIT_ABORTED = 130

DEFAULT_DB = "af.sqlite3"
DEFAULT_RUNS = Path("runs")
DEFAULT_FIXTURES = Path("tasks/fixtures")
DEFAULT_SOLUTIONS = Path("tasks/solutions")
DEFAULT_TASKS = Path("tasks")
DEFAULT_REPORT = Path("report.md")
DEFAULT_IMAGE = "afrunner:local"

RUNNER_DOCKER = "docker"
RUNNER_LOCAL = "local"

AGENT_FAKE = "fake"
AGENT_OLLAMA = "ollama"
AGENT_TOOLS = "tools"

DEFAULT_MODEL = "qwen2.5-coder:3b"


def exit_code_for(record: RunRecord) -> int:
    if record.run.state is RunState.SCORED:
        return EXIT_MEASURED
    if record.run.state is RunState.ABORTED:
        return EXIT_ABORTED
    return EXIT_HARNESS_FAILED
