"""The runner inside its real container.

The native suite runs `afrunner` as an ordinary process, where it is not PID 1
and its process tree belongs to the host. Only here does the kernel treat it as
the container's init: orphans reparent to it, and a signal it has not blocked
is discarded. These tests drive Docker from outside, the way the control plane
will.

Skipped unless `docker` works and the image exists (`make runner-image`).
`AF_DOCKER_INIT=1` runs the same suite with `docker run --init`, where tini is
PID 1 and `afrunner` is its child.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from control_plane.adapters.result_json import decode_report
from control_plane.domain.validation import (
    CommandOutcome,
    ExitReason,
    ValidationReport,
)
from tests.docker_support import IMAGE, requires_image

WITH_INIT = os.environ.get("AF_DOCKER_INIT") == "1"

pytestmark = [pytest.mark.docker, requires_image]


class Container:
    """One validation container's mounts, and the argv that starts it."""

    def __init__(self, root: Path, commands: str) -> None:
        self.inbox = root / "in"
        self.out = root / "out"
        self.work = root / "work"
        for directory in (self.inbox, self.out, self.work):
            directory.mkdir()
        (self.inbox / "commands.txt").write_text(commands, encoding="utf-8")
        self.result = self.out / "result.json"

    def argv(self, *docker_options: str, timeout_ms: int = 60_000) -> list[str]:
        return [
            "docker",
            "run",
            *(["--init"] if WITH_INIT else []),
            "--network",
            "none",
            "--cap-drop=ALL",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{self.inbox}:/in:ro",
            "-v",
            f"{self.out}:/out",
            "-v",
            f"{self.work}:/work",
            *docker_options,
            IMAGE,
            "--commands",
            "/in/commands.txt",
            "--workdir",
            "/work",
            "--result",
            "/out/result.json",
            "--log-dir",
            "/out/logs",
            "--timeout-ms",
            str(timeout_ms),
            "--total-timeout-ms",
            str(timeout_ms),
            "--grace-ms",
            "500",
            "--max-log-bytes",
            "1048576",
            "--tail-bytes",
            "4096",
            "--drain-after-exit-ms",
            "500",
        ]

    def run(self, *docker_options: str) -> int:
        completed = subprocess.run(
            self.argv("--rm", *docker_options),
            capture_output=True,
            check=False,
            timeout=120,
        )
        return completed.returncode

    def report(self) -> ValidationReport:
        return decode_report(self.result.read_bytes())


def test_the_runner_is_pid_1_unless_an_init_is_injected(tmp_path: Path) -> None:
    container = Container(tmp_path, "true\n")

    assert container.run() == 0

    report = container.report()
    assert report.pid1 is not WITH_INIT
    # Never emulated: the container runs on the host's architecture.
    assert report.arch == platform.machine()
    assert report.shell == "/usr/bin/dash"


def test_an_orphan_that_left_the_group_is_reaped(tmp_path: Path) -> None:
    # setsid takes the sleeper out of the command's process group, so the
    # group kill at the end of command 0 cannot reach it. Its shell parent then
    # exits, and the orphan reparents to the runner, which must reap it while
    # command 1 is running and still report command 1's own exit status.
    container = Container(
        tmp_path,
        "setsid sh -c 'sleep 0.3' >/dev/null 2>&1 &\nsleep 1\n",
    )

    assert container.run() == 0

    report = container.report()
    assert report.orphans_reaped_total >= 1
    assert report.commands[1].outcome is CommandOutcome.EXITED
    assert report.commands[1].exit_code == 0


def test_a_fork_refused_by_the_pids_limit_is_spawn_failed(tmp_path: Path) -> None:
    # The runner is the container's only process, so a limit of one leaves no
    # room for the command. With --init, tini takes a slot too.
    container = Container(tmp_path, "true\n")
    limit = "2" if WITH_INIT else "1"

    # Exit 0: the runner finished and the file is trustworthy. The failed spawn
    # is a fact about the host limit, recorded in the result, not a crash.
    assert container.run("--pids-limit", limit) == 0

    command = container.report().commands[0]
    assert command.outcome is CommandOutcome.SPAWN_FAILED
    assert command.spawn_error is not None


def test_docker_stop_leaves_a_partial_result(tmp_path: Path) -> None:
    # PID 1 discards a signal whose disposition is SIG_DFL. A runner that did
    # not block SIGTERM would ignore docker stop, be killed after the timeout,
    # exit 137, and leave no result at all.
    container = Container(tmp_path, "sleep 300\necho never\n")
    name = f"af-test-{uuid.uuid4().hex[:12]}"
    subprocess.run(
        container.argv("-d", "--name", name, timeout_ms=600_000),
        capture_output=True,
        check=True,
    )
    try:
        _wait_for(container.out / "logs" / "cmd-000.stdout.log")

        started = time.monotonic()
        subprocess.run(
            ["docker", "stop", "-t", "10", name], capture_output=True, check=True
        )
        stop_seconds = time.monotonic() - started

        exit_code = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.ExitCode}}", name],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)

    assert exit_code == "7"
    # Well inside docker stop's 10 second timeout, so the runner handled SIGTERM
    # itself rather than being killed when the timeout ran out.
    assert stop_seconds < 8

    report = container.report()
    assert report.exit_reason is ExitReason.SIGNAL
    assert report.aborted
    assert not report.commands[0].deadline_signal_sent
    assert report.commands[1].outcome is CommandOutcome.NOT_RUN


_ALLOCATE = "python3 -c \"b = b'x' * (512 * 1024 * 1024)\""


def test_a_command_killed_by_the_memory_limit_is_signaled(tmp_path: Path) -> None:
    # exec removes the shell layer, so the runner's own child is the process the
    # kernel's OOM killer chooses.
    container = Container(tmp_path, f"exec {_ALLOCATE}\n")

    assert container.run("--memory", "64m", "--memory-swap", "64m") == 0

    command = container.report().commands[0]
    assert command.outcome is CommandOutcome.SIGNALED
    assert command.signal == 9


def test_a_shell_layer_hides_the_oom_kill_as_exit_137(tmp_path: Path) -> None:
    # Debian bookworm's dash forks even a simple `sh -c` command, where Ubuntu's
    # dash execs it. The OOM killer therefore kills python, and the shell
    # reports that as an ordinary exit status of 128 + 9. The runner cannot
    # tell this apart from a command that chose to exit 137.
    container = Container(tmp_path, f"{_ALLOCATE}\n")

    assert container.run("--memory", "64m", "--memory-swap", "64m") == 0

    command = container.report().commands[0]
    assert command.outcome is CommandOutcome.EXITED
    assert command.exit_code == 137


def _wait_for(path: Path, timeout_s: float = 20.0) -> None:
    """Wait until the runner has started the command that writes `path`."""
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path.name} did not appear within {timeout_s}s")
        time.sleep(0.05)
