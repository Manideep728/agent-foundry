"""Running validation inside the runner image.

`afrunner` runs as PID 1 in a container started with no network, no
capabilities, and fixed memory, CPU and process limits. This module builds that
`docker run` command line, starts it, and turns what comes back into either a
`ValidationReport` or one of the two service errors.

The container reduces accidental host access. It is not a hardened boundary
against a deliberately malicious workload, and nothing here claims it is.

Three directories are mounted:

- `in/` read-only at `/in`, holding `commands.txt`. It sits beside `out/`, not
  inside it, because `/out` is writable and a patch that could rewrite the
  command file could rewrite the thing judging it.
- `out/` read-write at `/out`, where the runner writes `result.json` and logs.
- the repository copy read-write at `/work`.

Because the patch can write to `/out`, `result.json` is untrusted when it comes
back: it is opened without following symlinks, its size is capped, and it is
decoded strictly.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from control_plane.adapters.result_json import MalformedResult, decode_report
from control_plane.domain.ids import RunId
from control_plane.domain.validation import ExitReason, ValidationReport
from control_plane.services.errors import RunnerFailed, RunnerUnavailable
from control_plane.services.ports import ValidationRequest

RUNNER_ID = "docker-afrunner"

# From docs/RUNNER_CONTRACT.md.
_EXIT_OK = 0
_EXIT_ABORTED = 7
_EXIT_RUNNER_FAILURES = range(2, 7)
# 128 + SIGKILL. Usually the memory limit, sometimes `docker kill`. Never a
# failed command: the runner's own exit code describes the runner.
_EXIT_SIGKILL = 137

# Beyond the task's total budget the runner may still spend the grace period,
# the bounded kill confirmation and the post-exit drain, plus container start
# and teardown. The host waits this much longer before it gives up.
_HOST_MARGIN_MS = 30_000

# A result holds at most a few KiB of tail per stream, so anything this large
# was not written by the runner.
_MAX_RESULT_BYTES = 16 * 1024 * 1024

_REMOVE_TIMEOUT_S = 30.0


def _current_user() -> str:
    return f"{os.getuid()}:{os.getgid()}"


@dataclass(frozen=True, slots=True)
class DockerConfig:
    """The image and the limits every validation container gets.

    Integers throughout, including CPU, which is a quota in microseconds per
    period rather than `--cpus 1.5`.
    """

    image: str = "afrunner:local"
    memory_bytes: int = 1024 * 1024 * 1024
    pids_limit: int = 512
    cpu_period_us: int = 100_000
    cpu_quota_us: int = 100_000
    # Well below docker stop's 10 second default, so an abort finishes and
    # writes its partial result before Docker escalates to SIGKILL.
    grace_ms: int = 2_000
    max_log_bytes: int = 8 * 1024 * 1024
    tail_bytes: int = 4_096
    drain_after_exit_ms: int = 2_000
    # The host user, so files written to the bind mounts stay owned by it.
    user: str = field(default_factory=_current_user)


# Takes the argv and a timeout in seconds. May raise FileNotFoundError when
# `docker` is absent and subprocess.TimeoutExpired when the timeout passes.
ProcessRunner = Callable[[list[str], float], "subprocess.CompletedProcess[bytes]"]


def _run_process(
    argv: list[str], timeout_s: float
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, timeout=timeout_s, check=False)


def container_name(run_id: RunId) -> str:
    """The name recovery uses to find a container a crashed run left behind."""
    return f"af-{run_id.value}"


def inbox_for(request: ValidationRequest) -> Path:
    return request.output_dir.parent / "in"


def build_argv(request: ValidationRequest, config: DockerConfig) -> list[str]:
    """The complete `docker run` command line. Pure, so it is tested directly."""
    try:
        log_dir = request.log_dir.relative_to(request.output_dir)
    except ValueError as error:
        raise RunnerFailed(
            f"log_dir {request.log_dir} is not inside output_dir "
            f"{request.output_dir}, so the container cannot see it"
        ) from error

    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name(request.run_id),
        "--label",
        f"agentfoundry.run_id={request.run_id.value}",
        "--network",
        "none",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(config.pids_limit),
        "--memory",
        str(config.memory_bytes),
        # Equal to --memory, so the limit cannot be escaped into swap.
        "--memory-swap",
        str(config.memory_bytes),
        "--cpu-period",
        str(config.cpu_period_us),
        "--cpu-quota",
        str(config.cpu_quota_us),
        "--user",
        config.user,
        _bind(inbox_for(request), "/in", read_only=True),
        _bind(request.output_dir, "/out", read_only=False),
        _bind(request.workdir, "/work", read_only=False),
        config.image,
        "--commands",
        "/in/commands.txt",
        "--workdir",
        "/work",
        "--result",
        "/out/result.json",
        "--log-dir",
        f"/out/{log_dir.as_posix()}",
        "--timeout-ms",
        str(request.per_command_timeout_ms),
        "--total-timeout-ms",
        str(request.total_timeout_ms),
        "--grace-ms",
        str(config.grace_ms),
        "--max-log-bytes",
        str(config.max_log_bytes),
        "--tail-bytes",
        str(config.tail_bytes),
        "--drain-after-exit-ms",
        str(config.drain_after_exit_ms),
    ]
    if request.stop_on_failure:
        argv.append("--stop-on-failure")
    return argv


def _bind(source: Path, target: str, *, read_only: bool) -> str:
    # --mount, not -v: -v silently creates a missing source directory as root,
    # where --mount refuses to start. A comma would split the --mount value
    # into a different option, so a path holding one is refused outright.
    if "," in str(source):
        raise RunnerFailed(f"cannot bind-mount {source}: the path contains a comma")
    spec = f"--mount=type=bind,source={source},target={target}"
    return f"{spec},readonly" if read_only else spec


class DockerRunner:
    """A `ValidationRunner` that runs `afrunner` in a locked-down container."""

    def __init__(
        self,
        config: DockerConfig | None = None,
        run_process: ProcessRunner = _run_process,
    ) -> None:
        self._config = config or DockerConfig()
        self._run_process = run_process

    @property
    def runner_id(self) -> str:
        return RUNNER_ID

    @property
    def runner_version(self) -> str:
        return f"afrunner in docker image {self._config.image}"

    @property
    def isolated(self) -> bool:
        return True

    def run(self, request: ValidationRequest) -> ValidationReport:
        inbox = inbox_for(request)
        inbox.mkdir(exist_ok=True)
        (inbox / "commands.txt").write_text(
            "".join(f"{command}\n" for command in request.commands), encoding="utf-8"
        )
        result_path = request.output_dir / "result.json"
        # A file from anything but this container must not be read as its result.
        result_path.unlink(missing_ok=True)

        argv = build_argv(request, self._config)
        budget_ms = (
            request.total_timeout_ms
            + self._config.grace_ms
            + self._config.drain_after_exit_ms
            + _HOST_MARGIN_MS
        )
        try:
            completed = self._run_process(argv, budget_ms / 1000)
        except FileNotFoundError as error:
            raise RunnerUnavailable("docker is not installed or not on PATH") from error
        except subprocess.TimeoutExpired as error:
            # Killing the docker CLI does not stop the container.
            self.remove_container(request.run_id)
            raise RunnerUnavailable(
                f"the container ran past {budget_ms} ms and was removed"
            ) from error

        return _interpret(completed, result_path)

    def remove_container(self, run_id: RunId) -> None:
        """Force-remove the run's container. A container already gone is fine."""
        name = container_name(run_id)
        try:
            completed = self._run_process(
                ["docker", "rm", "--force", name], _REMOVE_TIMEOUT_S
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise RunnerUnavailable(f"could not remove container {name}") from error
        if completed.returncode != 0 and b"No such container" not in completed.stderr:
            raise RunnerUnavailable(
                f"could not remove container {name}: {_stderr_tail(completed)}"
            )


def _interpret(
    completed: subprocess.CompletedProcess[bytes], result_path: Path
) -> ValidationReport:
    code = completed.returncode
    if code == _EXIT_SIGKILL:
        raise RunnerUnavailable(
            "the container was killed by SIGKILL (exit 137); the usual cause is "
            "the memory limit"
        )
    if code in _EXIT_RUNNER_FAILURES:
        raise RunnerFailed(f"afrunner exited {code}: {_stderr_tail(completed)}")
    if code not in (_EXIT_OK, _EXIT_ABORTED):
        # 125-127 belong to Docker itself: no daemon, no image, no entrypoint.
        raise RunnerUnavailable(f"docker run exited {code}: {_stderr_tail(completed)}")

    try:
        report = decode_report(_read_result(result_path))
    except MalformedResult as error:
        raise RunnerFailed(
            f"afrunner exited {code} but {result_path} is malformed: {error}"
        ) from error

    # The exit code and the file must tell the same story, or neither is
    # trusted.
    if (report.exit_reason is ExitReason.SIGNAL) != (code == _EXIT_ABORTED):
        raise RunnerFailed(
            f"afrunner exited {code} but {result_path} says exit_reason "
            f"{report.exit_reason.value}"
        )
    return report


def _read_result(path: Path) -> bytes:
    # The patch can write to /out. O_NOFOLLOW stops a result.json symlinked to a
    # host file from being read through, and O_NONBLOCK stops a FIFO in its
    # place from blocking the open forever.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise MalformedResult("the runner wrote no result file") from error
    except OSError as error:
        raise MalformedResult(f"the result file cannot be opened: {error}") from error

    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise MalformedResult("the result file is not a regular file")

    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(_MAX_RESULT_BYTES + 1)
    if len(data) > _MAX_RESULT_BYTES:
        raise MalformedResult(f"the result file exceeds {_MAX_RESULT_BYTES} bytes")
    return data


def _stderr_tail(completed: subprocess.CompletedProcess[bytes]) -> str:
    return completed.stderr[-1000:].decode("utf-8", errors="replace").strip()
