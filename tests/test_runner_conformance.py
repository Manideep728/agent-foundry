"""The C++ runner's output must satisfy the Python reader.

Two programs in two languages agree on a schema only when something checks. The
contract document and `tests/test_runner_contract.py` keep the *names* aligned;
this keeps the *bytes* aligned, by running the real binary and parsing its real
output through `ValidationReport`, whose `__post_init__` enforces every
invariant the contract states.

No Docker is needed. The runner writes a result without spawning anything, so
this check exists well before the supervisor does.

Skipped when the binary is absent, so `make test` still passes on a machine with
no C++ toolchain.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from control_plane.domain.validation import (
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_runner() -> Path | None:
    """Locate afrunner, preferring an explicit build directory.

    The default build tree is outside the repository, because the sources sit
    on a Windows drive that WSL reaches over 9p.
    """
    candidates: list[Path] = []
    override = os.environ.get("RUNNER_BUILD_DIR")
    if override:
        candidates.append(Path(override) / "afrunner")
    candidates.append(Path.home() / ".cache" / "afrunner" / "build" / "afrunner")
    candidates.append(REPO_ROOT / "runner" / "build" / "afrunner")

    return next(
        (c for c in candidates if c.is_file() and os.access(c, os.X_OK)),
        None,
    )


RUNNER = _find_runner()

pytestmark = pytest.mark.skipif(
    RUNNER is None,
    reason="afrunner is not built; run `make runner-build`",
)


@pytest.fixture(name="runner")
def runner_fixture() -> Path:
    assert RUNNER is not None  # guaranteed by pytestmark
    return RUNNER


def _invoke(
    runner: Path,
    tmp_path: Path,
    command_text: str,
    *,
    workdir: Path | None = None,
) -> tuple[int, Path]:
    """Run the runner over `command_text` and return its code and result path."""
    work = tmp_path / "work"
    logs = tmp_path / "out" / "logs"
    work.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    # newline="" keeps a \r\n in the fixture literal, which is the case the
    # carriage-return rule exists for.
    command_file = tmp_path / "commands.txt"
    command_file.write_text(command_text, encoding="utf-8", newline="")

    result = tmp_path / "out" / "result.json"
    completed = subprocess.run(
        [
            str(runner),
            "--commands",
            str(command_file),
            "--workdir",
            str(work if workdir is None else workdir),
            "--result",
            str(result),
            "--log-dir",
            str(logs),
            "--timeout-ms",
            "120000",
            "--total-timeout-ms",
            "600000",
            "--grace-ms",
            "5000",
            "--max-log-bytes",
            "8388608",
            "--tail-bytes",
            "4096",
            "--drain-after-exit-ms",
            "2000",
        ],
        capture_output=True,
        check=False,
    )
    return completed.returncode, result


def _parse(result: Path) -> ValidationReport:
    """Build a ValidationReport from the runner's JSON.

    Deliberately splats the decoded object into the dataclasses: an unknown key
    or a missing one raises TypeError here, which is exactly the drift this
    test exists to catch.
    """
    raw: dict[str, Any] = json.loads(result.read_text(encoding="utf-8"))

    commands = []
    for entry in raw.pop("commands"):
        entry["outcome"] = CommandOutcome(entry["outcome"])
        entry["stdout"] = StreamCapture(**entry["stdout"])
        entry["stderr"] = StreamCapture(**entry["stderr"])
        commands.append(CommandResult(**entry))

    raw["exit_reason"] = ExitReason(raw["exit_reason"])
    return ValidationReport(commands=tuple(commands), **raw)


def test_the_runner_writes_a_report_the_reader_accepts(
    runner: Path, tmp_path: Path
) -> None:
    code, result = _invoke(runner, tmp_path, "true\nfalse\n")

    assert code == 0
    assert result.is_file()

    report = _parse(result)
    assert report.schema_version == 1
    assert len(report.commands) == 2


def test_the_json_keys_are_exactly_the_documented_fields(
    runner: Path, tmp_path: Path
) -> None:
    """The strongest drift guard available: the wire format, key for key."""
    _, result = _invoke(runner, tmp_path, "true\n")
    raw = json.loads(result.read_text(encoding="utf-8"))

    assert set(raw) == {f.name for f in fields(ValidationReport)}
    for entry in raw["commands"]:
        assert set(entry) == {f.name for f in fields(CommandResult)}
        for stream in ("stdout", "stderr"):
            assert set(entry[stream]) == {f.name for f in fields(StreamCapture)}


def test_indices_are_contiguous_and_ordered(runner: Path, tmp_path: Path) -> None:
    _, result = _invoke(runner, tmp_path, "one\ntwo\nthree\n")
    report = _parse(result)

    assert [c.index for c in report.commands] == [0, 1, 2]


def test_a_carriage_return_is_stripped_from_every_command(
    runner: Path, tmp_path: Path
) -> None:
    """A task file authored on Windows must not run `pytest -q tests/\\r`."""
    _, result = _invoke(runner, tmp_path, "pytest -q tests/\r\nruff check .\r\n")
    report = _parse(result)

    assert [c.command for c in report.commands] == ["pytest -q tests/", "ruff check ."]


def test_comments_and_blank_lines_do_not_take_an_index(
    runner: Path, tmp_path: Path
) -> None:
    _, result = _invoke(runner, tmp_path, "# header\n\nfirst\n\n# middle\nsecond\n")
    report = _parse(result)

    assert [c.command for c in report.commands] == ["first", "second"]


def test_the_report_names_the_environment(runner: Path, tmp_path: Path) -> None:
    _, result = _invoke(runner, tmp_path, "true\n")
    report = _parse(result)

    assert report.arch.strip()
    assert report.shell.startswith("/")
    assert report.runner_version.startswith("afrunner ")


def test_a_command_file_with_no_commands_is_exit_three(
    runner: Path, tmp_path: Path
) -> None:
    code, result = _invoke(runner, tmp_path, "# nothing to do\n")

    assert code == 3
    # Absence of the file is meaningful: it says the runner did not finish.
    assert not result.exists()


def test_a_missing_workdir_is_exit_four(runner: Path, tmp_path: Path) -> None:
    code, _ = _invoke(runner, tmp_path, "true\n", workdir=tmp_path / "does-not-exist")

    assert code == 4


def test_a_usage_error_is_exit_two(runner: Path) -> None:
    completed = subprocess.run(
        [str(runner), "--bogus"], capture_output=True, check=False
    )

    assert completed.returncode == 2
