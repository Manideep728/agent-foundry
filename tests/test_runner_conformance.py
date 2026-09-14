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
import signal
import subprocess
import time
from dataclasses import fields
from pathlib import Path

import pytest

from control_plane.adapters.result_json import decode_report
from control_plane.domain.validation import (
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_runner() -> Path | None:
    """Locate afrunner: an explicit build directory first, then the default.

    The default is `runner/build`, the Makefile's `RUNNER_BUILD_DIR`.
    """
    candidates: list[Path] = []
    override = os.environ.get("RUNNER_BUILD_DIR")
    if override:
        candidates.append(Path(override) / "afrunner")
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
    """Read the runner's JSON through the control plane's own decoder.

    The decoder rejects an unknown or missing key and a value of the wrong
    type, which is exactly the drift this test exists to catch.
    """
    return decode_report(result.read_bytes())


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


def _start(
    runner: Path, tmp_path: Path, command_text: str, *, grace_ms: int = 500
) -> tuple[subprocess.Popen[bytes], Path]:
    work = tmp_path / "work"
    logs = tmp_path / "out" / "logs"
    work.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    command_file = tmp_path / "commands.txt"
    command_file.write_text(command_text, encoding="utf-8", newline="")

    result = tmp_path / "out" / "result.json"
    process = subprocess.Popen(
        [
            str(runner),
            "--commands",
            str(command_file),
            "--workdir",
            str(work),
            "--result",
            str(result),
            "--log-dir",
            str(logs),
            "--timeout-ms",
            "60000",
            "--total-timeout-ms",
            "60000",
            "--grace-ms",
            str(grace_ms),
            "--max-log-bytes",
            "8388608",
            "--tail-bytes",
            "4096",
            "--drain-after-exit-ms",
            "500",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process, result


# Without a blocked SIGTERM and a signalfd, `docker stop` kills the runner at
# SIG_DFL and the container dies with no result at all. The control plane then
# records a runner failure and cannot say why.
def test_a_signal_writes_a_complete_partial_result(
    runner: Path, tmp_path: Path
) -> None:
    process, result = _start(runner, tmp_path, "sleep 30 & wait\necho never\n")
    time.sleep(1.0)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=30)

    assert process.returncode == 7
    assert result.is_file(), "the runner died without writing anything"

    report = _parse(result)
    assert report.exit_reason is ExitReason.SIGNAL
    assert report.aborted

    # The reader requires indices 0..n-1, so a partial result must still carry
    # an entry for every command in the file.
    assert len(report.commands) == 2
    assert report.commands[1].outcome is CommandOutcome.NOT_RUN

    # An abort is not a deadline. CommandResult refuses killed_after_grace
    # without deadline_signal_sent, so getting this wrong makes the report
    # unparseable, which _parse above would already have raised on.
    interrupted = report.commands[0]
    assert not interrupted.deadline_signal_sent
    assert not interrupted.killed_after_grace
    assert not interrupted.timed_out


def test_a_second_signal_does_not_wait_out_the_grace(
    runner: Path, tmp_path: Path
) -> None:
    process, result = _start(
        runner, tmp_path, "trap '' TERM; sleep 30 & wait\n", grace_ms=30000
    )
    time.sleep(1.0)
    process.send_signal(signal.SIGTERM)
    time.sleep(0.3)
    process.send_signal(signal.SIGTERM)

    started = time.monotonic()
    process.wait(timeout=30)
    elapsed = time.monotonic() - started

    assert process.returncode == 7
    assert elapsed < 20, "the second signal must skip the 30 second grace period"
    assert result.is_file()


def test_a_usage_error_is_exit_two(runner: Path) -> None:
    completed = subprocess.run(
        [str(runner), "--bogus"], capture_output=True, check=False
    )

    assert completed.returncode == 2
