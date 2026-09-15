"""The Docker runner.

Most of these tests replace the process call with a fake, so the command line
and the exit-code mapping are tested exactly and without Docker. The tests at
the end start real containers and skip when the image is absent.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from control_plane.adapters.docker_runner import (
    DockerConfig,
    DockerRunner,
    build_argv,
    container_name,
)
from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.system import SystemClock
from control_plane.adapters.task_toml import load_task
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.ids import RunId
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.states import RunState
from control_plane.domain.validation import CommandOutcome, ExitReason
from control_plane.services.errors import RunnerFailed, RunnerUnavailable
from control_plane.services.evaluate import EvaluationService
from control_plane.services.ports import ValidationRequest
from tests.docker_support import IMAGE, requires_image
from tests.fakes import FakeIdGenerator
from tests.result_samples import encode, sample_report

REPO = Path(__file__).resolve().parent.parent
RUN_ID = RunId("run-0001")


def _request(root: Path, *, stop_on_failure: bool = False) -> ValidationRequest:
    for directory in (root / "repo", root / "out" / "logs"):
        directory.mkdir(parents=True, exist_ok=True)
    return ValidationRequest(
        run_id=RUN_ID,
        workdir=root / "repo",
        output_dir=root / "out",
        log_dir=root / "out" / "logs",
        commands=("python3 -m unittest tests.test_a", "echo done"),
        per_command_timeout_ms=30_000,
        total_timeout_ms=120_000,
        stop_on_failure=stop_on_failure,
    )


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# --- the command line ------------------------------------------------------


def test_the_container_has_no_network_and_no_capabilities(tmp_path: Path) -> None:
    # A project invariant: validation runs with --network none, asserted on the
    # argv rather than trusted to an integration test.
    argv = build_argv(_request(tmp_path), DockerConfig())

    assert _value_after(argv, "--network") == "none"
    assert _value_after(argv, "--pull") == "never"
    assert "--cap-drop=ALL" in argv
    assert _value_after(argv, "--security-opt") == "no-new-privileges"


def test_the_limits_are_integers_and_swap_cannot_exceed_memory(tmp_path: Path) -> None:
    config = DockerConfig(memory_bytes=268_435_456, pids_limit=64, cpu_quota_us=50_000)
    argv = build_argv(_request(tmp_path), config)

    assert _value_after(argv, "--memory") == "268435456"
    assert _value_after(argv, "--memory-swap") == "268435456"
    assert _value_after(argv, "--pids-limit") == "64"
    assert _value_after(argv, "--cpu-period") == "100000"
    assert _value_after(argv, "--cpu-quota") == "50000"


def test_the_command_file_is_mounted_read_only_outside_out(tmp_path: Path) -> None:
    request = _request(tmp_path)
    argv = build_argv(request, DockerConfig())
    mounts = [arg for arg in argv if arg.startswith("--mount=")]

    assert mounts == [
        f"--mount=type=bind,source={tmp_path / 'in'},target=/in,readonly",
        f"--mount=type=bind,source={tmp_path / 'out'},target=/out",
        f"--mount=type=bind,source={tmp_path / 'repo'},target=/work",
    ]


def test_docker_options_precede_the_image_and_runner_flags_follow(
    tmp_path: Path,
) -> None:
    config = DockerConfig(image="afrunner:test", grace_ms=1_500, user="1000:1000")
    argv = build_argv(_request(tmp_path), config)
    image_at = argv.index("afrunner:test")

    assert argv[:2] == ["docker", "run"]
    # Kept after exit so `docker inspect` can read State.OOMKilled.
    assert "--rm" not in argv
    assert _value_after(argv, "--name") == "af-run-0001"
    assert "--user" in argv[:image_at]
    runner_flags = argv[image_at + 1 :]
    assert runner_flags == [
        "--commands",
        "/in/commands.txt",
        "--workdir",
        "/work",
        "--result",
        "/out/result.json",
        "--log-dir",
        "/out/logs",
        "--timeout-ms",
        "30000",
        "--total-timeout-ms",
        "120000",
        "--grace-ms",
        "1500",
        "--max-log-bytes",
        str(config.max_log_bytes),
        "--tail-bytes",
        str(config.tail_bytes),
        "--drain-after-exit-ms",
        str(config.drain_after_exit_ms),
    ]


def test_stop_on_failure_is_passed_only_when_requested(tmp_path: Path) -> None:
    assert "--stop-on-failure" not in build_argv(_request(tmp_path), DockerConfig())
    assert build_argv(_request(tmp_path, stop_on_failure=True), DockerConfig())[-1] == (
        "--stop-on-failure"
    )


def test_a_log_dir_outside_out_is_refused(tmp_path: Path) -> None:
    outside = replace(_request(tmp_path), log_dir=tmp_path / "elsewhere")

    with pytest.raises(RunnerFailed, match="not inside output_dir"):
        build_argv(outside, DockerConfig())


def test_a_path_with_a_comma_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunnerFailed, match="comma"):
        build_argv(_request(tmp_path / "a,b"), DockerConfig())


# --- running and interpreting ----------------------------------------------


class FakeDocker:
    """Records each call and plays the part of `docker` and the runner."""

    def __init__(
        self,
        returncode: int = 0,
        result: bytes | None = None,
        raises: Exception | None = None,
        stderr: bytes = b"",
        oom_killed: bool = False,
        inspect_fails: bool = False,
        rm_fails: bool = False,
        plant: Callable[[Path], None] | None = None,
    ) -> None:
        self.returncode = returncode
        self.result = result
        # Called with the result path during `docker run`, standing in for a
        # patch that writes something hostile to /out.
        self.plant = plant
        self.raises = raises
        self.stderr = stderr
        self.oom_killed = oom_killed
        self.inspect_fails = inspect_fails
        self.rm_fails = rm_fails
        self.calls: list[list[str]] = []
        # The host side of the /out mount, set once the request exists.
        self.out_dir: Path | None = None

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]

    def __call__(
        self, argv: list[str], timeout_s: float
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        if argv[:2] == ["docker", "rm"]:
            if self.rm_fails:
                return subprocess.CompletedProcess(argv, 1, b"", b"daemon went away")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[:2] == ["docker", "inspect"]:
            if self.inspect_fails:
                return subprocess.CompletedProcess(argv, 1, b"", b"No such object")
            answer = b"true\n" if self.oom_killed else b"false\n"
            return subprocess.CompletedProcess(argv, 0, answer, b"")
        if self.raises is not None:
            raise self.raises
        assert self.out_dir is not None
        if self.result is not None:
            (self.out_dir / "result.json").write_bytes(self.result)
        if self.plant is not None:
            self.plant(self.out_dir / "result.json")
        return subprocess.CompletedProcess(argv, self.returncode, b"", self.stderr)


def _runner(
    tmp_path: Path, docker: FakeDocker
) -> tuple[DockerRunner, ValidationRequest]:
    request = _request(tmp_path)
    docker.out_dir = request.output_dir
    return DockerRunner(DockerConfig(user="1000:1000"), run_process=docker), request


def _signal_report() -> bytes:
    raw = sample_report() | {"exit_reason": "signal", "aborted": True}
    return encode(raw)


def test_a_clean_exit_returns_the_decoded_report(tmp_path: Path) -> None:
    runner, request = _runner(tmp_path, FakeDocker(result=encode(sample_report())))

    report = runner.run(request)

    assert report.exit_reason is ExitReason.COMPLETED
    assert (tmp_path / "in" / "commands.txt").read_text(encoding="utf-8") == (
        "python3 -m unittest tests.test_a\necho done\n"
    )


def test_an_abort_returns_the_partial_report(tmp_path: Path) -> None:
    runner, request = _runner(tmp_path, FakeDocker(7, result=_signal_report()))

    assert runner.run(request).exit_reason is ExitReason.SIGNAL


@pytest.mark.parametrize(
    ("code", "result"),
    [
        pytest.param(0, _signal_report(), id="exit-0-but-signal"),
        pytest.param(7, encode(sample_report()), id="exit-7-but-completed"),
    ],
)
def test_an_exit_code_that_contradicts_the_file_is_runner_failed(
    tmp_path: Path, code: int, result: bytes
) -> None:
    runner, request = _runner(tmp_path, FakeDocker(code, result=result))

    with pytest.raises(RunnerFailed, match="exit_reason"):
        runner.run(request)


@pytest.mark.parametrize("code", [2, 3, 4, 5, 6])
def test_a_runner_error_code_is_runner_failed(tmp_path: Path, code: int) -> None:
    docker = FakeDocker(code, stderr=b"afrunner: missing required flag")
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerFailed, match="missing required flag"):
        runner.run(request)


def test_137_is_unavailable_and_never_a_measurement(tmp_path: Path) -> None:
    # Even with a valid result on disk: a SIGKILLed container is a host event.
    runner, request = _runner(tmp_path, FakeDocker(137, result=encode(sample_report())))

    with pytest.raises(RunnerUnavailable, match="memory limit"):
        runner.run(request)


@pytest.mark.parametrize("code", [1, 125, 126, 127])
def test_a_code_the_runner_never_uses_is_unavailable(tmp_path: Path, code: int) -> None:
    runner, request = _runner(tmp_path, FakeDocker(code, stderr=b"no such image"))

    with pytest.raises(RunnerUnavailable, match="no such image"):
        runner.run(request)


def test_no_result_file_is_runner_failed(tmp_path: Path) -> None:
    runner, request = _runner(tmp_path, FakeDocker(0))

    with pytest.raises(RunnerFailed, match="no result file"):
        runner.run(request)


def test_a_stale_result_file_is_not_read_as_this_runs_result(tmp_path: Path) -> None:
    runner, request = _runner(tmp_path, FakeDocker(0))
    (request.output_dir / "result.json").write_bytes(encode(sample_report()))

    with pytest.raises(RunnerFailed, match="no result file"):
        runner.run(request)


def test_a_malformed_result_is_runner_failed(tmp_path: Path) -> None:
    runner, request = _runner(tmp_path, FakeDocker(0, result=b'{"schema_version"'))

    with pytest.raises(RunnerFailed, match="malformed"):
        runner.run(request)


def test_a_result_symlinked_to_a_host_file_is_not_followed(tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.json"
    secret.write_bytes(encode(sample_report()))
    docker = FakeDocker(0, plant=lambda result: result.symlink_to(secret))
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerFailed, match="cannot be opened"):
        runner.run(request)


def test_a_fifo_in_place_of_the_result_does_not_block(tmp_path: Path) -> None:
    docker = FakeDocker(0, plant=os.mkfifo)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerFailed, match="not a regular file"):
        runner.run(request)


def test_missing_docker_is_unavailable(tmp_path: Path) -> None:
    runner, request = _runner(tmp_path, FakeDocker(raises=FileNotFoundError("docker")))

    with pytest.raises(RunnerUnavailable, match="not installed"):
        runner.run(request)


def test_a_host_timeout_removes_the_container(tmp_path: Path) -> None:
    # Killing the docker CLI leaves the container running, so it must be
    # removed by name.
    docker = FakeDocker(raises=subprocess.TimeoutExpired(["docker"], 1.0))
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerUnavailable, match="host budget"):
        runner.run(request)

    assert docker.calls[-1] == ["docker", "rm", "--force", container_name(RUN_ID)]


# --- memory limit and container lifetime -----------------------------------


def test_a_clean_run_is_inspected_then_removed(tmp_path: Path) -> None:
    docker = FakeDocker(result=encode(sample_report()))
    runner, request = _runner(tmp_path, docker)

    runner.run(request)

    assert docker.verbs() == ["run", "inspect", "rm"]
    assert docker.calls[1][-1] == container_name(RUN_ID)


@pytest.mark.parametrize(
    ("code", "result"),
    [
        pytest.param(0, encode(sample_report()), id="exit-0"),
        pytest.param(7, _signal_report(), id="exit-7"),
    ],
)
def test_an_oom_kill_overrides_a_valid_report(
    tmp_path: Path, code: int, result: bytes
) -> None:
    # The report may say `exited 137` for a test: without the verdict it would
    # score as a failed test rather than a resource limit.
    docker = FakeDocker(code, result=result, oom_killed=True)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerUnavailable, match="memory limit of 1073741824 bytes"):
        runner.run(request)

    assert docker.verbs()[-1] == "rm"


def test_a_failed_inspect_is_unavailable_not_a_pass(tmp_path: Path) -> None:
    docker = FakeDocker(result=encode(sample_report()), inspect_fails=True)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerUnavailable, match="could not inspect"):
        runner.run(request)

    assert docker.verbs()[-1] == "rm"


def test_a_docker_failure_is_reported_without_an_inspect(tmp_path: Path) -> None:
    # Exit 125 can mean the container was never created, so an inspect would
    # fail and replace Docker's own message with a less useful one.
    docker = FakeDocker(125, stderr=b"Unable to find image", inspect_fails=True)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerUnavailable, match="Unable to find image"):
        runner.run(request)

    assert "inspect" not in docker.verbs()


def test_a_container_that_cannot_be_removed_fails_a_clean_run(tmp_path: Path) -> None:
    docker = FakeDocker(result=encode(sample_report()), rm_fails=True)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerUnavailable, match="could not remove"):
        runner.run(request)


def test_a_failed_removal_does_not_hide_the_original_error(tmp_path: Path) -> None:
    docker = FakeDocker(3, stderr=b"afrunner: unreadable command file", rm_fails=True)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerFailed, match="unreadable command file"):
        runner.run(request)

    assert docker.verbs()[-1] == "rm"


# --- real containers -------------------------------------------------------


@pytest.mark.docker
@requires_image
def test_validation_cannot_reach_the_network_or_rewrite_its_commands(
    tmp_path: Path,
) -> None:
    request = replace(
        _request(tmp_path),
        run_id=RunId("net-0001"),
        commands=(
            'python3 -c "import socket; '
            "socket.create_connection(('1.1.1.1', 53), 3)\"",
            "echo tampered >> /in/commands.txt",
            "touch /work/written",
        ),
        total_timeout_ms=60_000,
    )

    report = DockerRunner(DockerConfig(image=IMAGE)).run(request)

    network, tamper, work = report.commands
    assert network.outcome is CommandOutcome.EXITED and network.exit_code != 0
    assert tamper.outcome is CommandOutcome.EXITED and tamper.exit_code != 0
    assert work.exit_code == 0
    assert (request.workdir / "written").is_file()
    assert report.pid1 is True


def _leftover_containers(run_id: str) -> str:
    return subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=agentfoundry.run_id={run_id}"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


@pytest.mark.docker
@requires_image
def test_a_command_killed_by_the_memory_limit_is_not_a_test_result(
    tmp_path: Path,
) -> None:
    # The plain form, with the shell layer: the runner alone would report it as
    # `exited 137`, indistinguishable from a failing test.
    request = replace(
        _request(tmp_path),
        run_id=RunId("oom-0001"),
        commands=("python3 -c \"b = b'x' * (512 * 1024 * 1024)\"",),
        total_timeout_ms=60_000,
    )
    config = DockerConfig(image=IMAGE, memory_bytes=64 * 1024 * 1024)

    with pytest.raises(RunnerUnavailable, match="memory limit of 67108864 bytes"):
        DockerRunner(config).run(request)

    assert _leftover_containers("oom-0001") == ""


@pytest.mark.docker
@requires_image
def test_one_task_runs_end_to_end_in_docker(tmp_path: Path) -> None:
    clock = SystemClock()
    runner = DockerRunner(DockerConfig(image=IMAGE))
    with SqliteRunRepository(tmp_path / "af.sqlite3") as repository:
        service = EvaluationService(
            repository=repository,
            runner=runner,
            agent=FakeAgent(REPO / "tasks" / "solutions", clock),
            workspaces=WorkspaceFactory(tmp_path / "runs"),
            clock=clock,
            ids=FakeIdGenerator("e2e"),
            fixtures_root=REPO / "tasks" / "fixtures",
        )
        record = service.execute(load_task(REPO / "tasks" / "csv-quoted-fields.toml"))

    assert record.run.state is RunState.SCORED
    assert record.outcome is AgentOutcome.CLEAN_PATCH
    assert record.score is not None and record.score.resolved
    assert record.pid1 is True
    assert record.runner_version == f"afrunner in docker image {IMAGE}"

    assert _leftover_containers("e2e-0001") == ""
