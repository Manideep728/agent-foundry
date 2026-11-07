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
ATTEMPT = "a1b2c3d4"
NAME = container_name(RUN_ID, ATTEMPT)


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


def test_every_bind_source_is_an_absolute_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Docker refuses a relative bind source, and the default runs dir is one.

    `--runs` defaults to `Path("runs")`, so every command in the README --
    `af run tasks/...`, `make bench` -- built a relative mount and every run
    from a clean checkout was recorded `infrastructure_failed`. Nothing caught
    it because every test and every development command passed an absolute
    `--runs` under tmp_path.

    Asserted on the argv rather than left to an integration test, like the
    `--network none` invariant above, so it fails without Docker present.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs" / "out" / "logs").mkdir(parents=True)
    (tmp_path / "runs" / "repo").mkdir(parents=True)
    relative = replace(
        _request(tmp_path / "runs"),
        workdir=Path("runs/repo"),
        output_dir=Path("runs/out"),
        log_dir=Path("runs/out/logs"),
    )

    argv = build_argv(relative, DockerConfig(), name=NAME)

    sources = [
        part.split("source=", 1)[1].split(",", 1)[0]
        for part in argv
        if part.startswith("--mount=")
    ]
    assert sources, "the container mounts nothing"
    assert all(Path(source).is_absolute() for source in sources), sources


def test_the_container_has_no_network_and_no_capabilities(tmp_path: Path) -> None:
    # A project invariant: validation runs with --network none, asserted on the
    # argv rather than trusted to an integration test.
    argv = build_argv(_request(tmp_path), DockerConfig(), name=NAME)

    assert _value_after(argv, "--network") == "none"
    assert _value_after(argv, "--pull") == "never"
    assert "--cap-drop=ALL" in argv
    assert _value_after(argv, "--security-opt") == "no-new-privileges"


def test_the_limits_are_integers_and_swap_cannot_exceed_memory(tmp_path: Path) -> None:
    config = DockerConfig(memory_bytes=268_435_456, pids_limit=64, cpu_quota_us=50_000)
    argv = build_argv(_request(tmp_path), config, name=NAME)

    assert _value_after(argv, "--memory") == "268435456"
    assert _value_after(argv, "--memory-swap") == "268435456"
    assert _value_after(argv, "--pids-limit") == "64"
    assert _value_after(argv, "--cpu-period") == "100000"
    assert _value_after(argv, "--cpu-quota") == "50000"


def test_the_command_file_is_mounted_read_only_outside_out(tmp_path: Path) -> None:
    request = _request(tmp_path)
    argv = build_argv(request, DockerConfig(), name=NAME)
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
    argv = build_argv(_request(tmp_path), config, name=NAME)
    image_at = argv.index("afrunner:test")

    assert argv[:2] == ["docker", "run"]
    # Kept after exit so `docker inspect` can read State.OOMKilled.
    assert "--rm" not in argv
    assert _value_after(argv, "--name") == "af-run-0001-a1b2c3d4"
    assert "--user" in argv[:image_at]
    runner_flags = argv[image_at + 1 :]
    assert runner_flags == [
        "--commands",
        "/in/commands.txt",
        "--workdir",
        "/work",
        "--result",
        "/out/result.json",
        "--oom-result",
        "/out/oom.json",
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
    assert "--stop-on-failure" not in build_argv(
        _request(tmp_path), DockerConfig(), name=NAME
    )
    assert build_argv(
        _request(tmp_path, stop_on_failure=True), DockerConfig(), name=NAME
    )[-1] == ("--stop-on-failure")


def test_a_log_dir_outside_out_is_refused(tmp_path: Path) -> None:
    outside = replace(_request(tmp_path), log_dir=tmp_path / "elsewhere")

    with pytest.raises(RunnerFailed, match="not inside output_dir"):
        build_argv(outside, DockerConfig(), name=NAME)


def test_a_path_with_a_comma_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunnerFailed, match="comma"):
        build_argv(_request(tmp_path / "a,b"), DockerConfig(), name=NAME)


# --- running and interpreting ----------------------------------------------


_OOM_NONE = b'{"oom_kills":0}'
# What the runner writes when it cannot read its cgroup: cgroup v1, or no
# private cgroup namespace.
_OOM_UNKNOWN = b'{"oom_kills":null}'


class FakeDocker:
    """Records each call and plays the part of `docker` and the runner."""

    def __init__(
        self,
        returncode: int = 0,
        result: bytes | None = None,
        oom_file: bytes | None = _OOM_NONE,
        raises: Exception | None = None,
        stderr: bytes = b"",
        oom_killed: bool = False,
        inspect_fails: bool = False,
        rm_fails: bool = False,
        plant: Callable[[Path], None] | None = None,
        image_digest: bytes = b"sha256:" + b"a" * 64 + b"\n",
        image_inspect_fails: bool = False,
        listed: bytes = b"",
    ) -> None:
        # What `docker ps` answers: the containers recovery finds by label.
        self.listed = listed
        self.image_digest = image_digest
        self.image_inspect_fails = image_inspect_fails
        self.returncode = returncode
        self.result = result
        self.oom_file = oom_file
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
        # Before the `raises` check further down, so a test that makes
        # `docker run` explode still answers the digest probe -- which is the
        # whole point of reading it before the container starts.
        if argv[:3] == ["docker", "image", "inspect"]:
            if self.image_inspect_fails:
                return subprocess.CompletedProcess(argv, 1, b"", b"No such image")
            return subprocess.CompletedProcess(argv, 0, self.image_digest, b"")
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, self.listed, b"")
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
        if self.oom_file is not None:
            (self.out_dir / "oom.json").write_bytes(self.oom_file)
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
    runner = DockerRunner(
        DockerConfig(user="1000:1000"), run_process=docker, new_attempt=lambda: ATTEMPT
    )
    return runner, request


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

    assert docker.calls[-1] == ["docker", "rm", "--force", NAME]


# --- memory limit and container lifetime -----------------------------------


def test_a_counted_clean_run_is_removed_without_an_inspect(tmp_path: Path) -> None:
    docker = FakeDocker(result=encode(sample_report()))
    runner, request = _runner(tmp_path, docker)

    runner.run(request)

    # "image" is the digest probe, and it comes first on purpose: the image is
    # resolved before the container starts, so a run that dies still names the
    # image it died in. The runner's OOM count answers, so Docker is not asked.
    assert docker.verbs() == ["image", "run", "rm"]


def test_an_uncounted_clean_run_is_inspected_then_removed(tmp_path: Path) -> None:
    docker = FakeDocker(result=encode(sample_report()), oom_file=_OOM_UNKNOWN)
    runner, request = _runner(tmp_path, docker)

    runner.run(request)

    assert docker.verbs() == ["image", "run", "inspect", "rm"]
    assert docker.calls[2][-1] == NAME


@pytest.mark.parametrize(
    ("code", "result"),
    [
        pytest.param(0, encode(sample_report()), id="exit-0"),
        pytest.param(7, _signal_report(), id="exit-7"),
    ],
)
@pytest.mark.parametrize(
    ("oom_file", "docker_flag"),
    [
        # The regression: Docker lost the OOM event and says false, but the
        # kernel counted the kill. Seen on loaded CI runners and reproduced
        # locally under CPU load.
        pytest.param(b'{"oom_kills":1}', False, id="counted-docker-lost-it"),
        pytest.param(_OOM_UNKNOWN, True, id="uncounted-docker-fallback"),
    ],
)
def test_an_oom_kill_overrides_a_valid_report(
    tmp_path: Path, code: int, result: bytes, oom_file: bytes, docker_flag: bool
) -> None:
    # The report may say `exited 137` for a test: without the verdict it would
    # score as a failed test rather than a resource limit.
    docker = FakeDocker(code, result=result, oom_file=oom_file, oom_killed=docker_flag)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerUnavailable, match="memory limit of 1073741824 bytes"):
        runner.run(request)

    assert docker.verbs()[-1] == "rm"


def test_a_zero_count_overrides_a_docker_flag(tmp_path: Path) -> None:
    # The count is exact and Docker's flag is not, so a zero count is trusted
    # without asking Docker at all.
    docker = FakeDocker(result=encode(sample_report()), oom_killed=True)
    runner, request = _runner(tmp_path, docker)

    assert runner.run(request).exit_reason is ExitReason.COMPLETED
    assert "inspect" not in docker.verbs()


@pytest.mark.parametrize(
    ("oom_file", "message"),
    [
        pytest.param(None, "wrote no OOM file", id="absent"),
        pytest.param(b"oom_kill 1", "not JSON", id="not-json"),
        pytest.param(b"[1]", "exactly one key", id="not-an-object"),
        pytest.param(b'{"oom_kills":0,"x":1}', "exactly one key", id="extra-key"),
        pytest.param(b'{"oom_kills":-1}', "not a count", id="negative"),
        pytest.param(b'{"oom_kills":true}', "not a count", id="bool"),
        pytest.param(b'{"oom_kills":"1"}', "not a count", id="string"),
    ],
)
def test_a_bad_oom_file_is_runner_failed(
    tmp_path: Path, oom_file: bytes | None, message: str
) -> None:
    docker = FakeDocker(result=encode(sample_report()), oom_file=oom_file)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerFailed, match=message):
        runner.run(request)

    assert "inspect" not in docker.verbs()


def test_an_oom_file_symlinked_to_a_host_file_is_not_followed(tmp_path: Path) -> None:
    host_file = tmp_path / "host-secret"
    host_file.write_bytes(b'{"oom_kills":0}')

    def plant(result: Path) -> None:
        (result.parent / "oom.json").symlink_to(host_file)

    docker = FakeDocker(result=encode(sample_report()), oom_file=None, plant=plant)
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerFailed, match="OOM file cannot be opened"):
        runner.run(request)


def test_an_oom_file_left_from_an_earlier_run_is_not_read(tmp_path: Path) -> None:
    docker = FakeDocker(result=encode(sample_report()), oom_file=None)
    runner, request = _runner(tmp_path, docker)
    (request.output_dir / "oom.json").write_bytes(b'{"oom_kills":0}')

    with pytest.raises(RunnerFailed, match="wrote no OOM file"):
        runner.run(request)


def test_a_failed_inspect_is_unavailable_not_a_pass(tmp_path: Path) -> None:
    docker = FakeDocker(
        result=encode(sample_report()), oom_file=_OOM_UNKNOWN, inspect_fails=True
    )
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
            harness_version=None,
        )
        record = service.execute(load_task(REPO / "tasks" / "csv-quoted-fields.toml"))

    assert record.run.state is RunState.SCORED
    assert record.outcome is AgentOutcome.CLEAN_PATCH
    assert record.score is not None and record.score.resolved
    assert record.pid1 is True
    assert record.runner_version == f"afrunner in docker image {IMAGE}"

    assert _leftover_containers("e2e-0001") == ""


# --- the image digest -------------------------------------------------------


DIGEST = "sha256:" + "a" * 64


def test_the_image_is_resolved_before_the_container_starts(tmp_path: Path) -> None:
    # Order is the design. Reading the digest from the post-run OOM inspect
    # instead would leave it empty for every run that did not exit 0 or 7.
    docker = FakeDocker(result=encode(sample_report()))
    runner, request = _runner(tmp_path, docker)

    runner.run(request)

    assert docker.verbs()[0] == "image"
    assert runner.image_digest == DIGEST


def test_a_run_that_failed_still_reports_the_image_it_used(tmp_path: Path) -> None:
    # The availability argument for reading it up front, as a test rather than
    # a comment: this is the run whose provenance actually gets asked for.
    docker = FakeDocker(3, stderr=b"afrunner: could not open /out")
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerFailed):
        runner.run(request)

    assert runner.image_digest == DIGEST


def test_a_garbled_image_inspect_is_not_stored_as_a_digest(tmp_path: Path) -> None:
    # Docker exits 0 and prints a Go template error when the format is wrong.
    # "<no value>" written into the provenance column looks like data for ever.
    docker = FakeDocker(result=encode(sample_report()), image_digest=b"<no value>\n")
    runner, request = _runner(tmp_path, docker)

    with pytest.raises(RunnerUnavailable, match="not an image digest"):
        runner.run(request)


def test_a_missing_image_records_no_digest_and_lets_docker_run_explain(
    tmp_path: Path,
) -> None:
    # Best effort on the call: `docker run --pull never` gives a far better
    # message about a missing image than an inspect could, and replacing a good
    # message with a worse one is a mistake this project has made before.
    docker = FakeDocker(
        result=encode(sample_report()),
        image_inspect_fails=True,
    )
    runner, request = _runner(tmp_path, docker)

    runner.run(request)

    assert runner.image_digest is None


def test_a_missing_docker_keeps_its_own_error(tmp_path: Path) -> None:
    # The digest probe must not turn "docker is not installed" into a complaint
    # about an inspect.
    runner, request = _runner(tmp_path, FakeDocker(raises=FileNotFoundError("docker")))

    with pytest.raises(RunnerUnavailable, match="not installed"):
        runner.run(request)


def test_a_runner_reports_no_digest_before_it_has_run(tmp_path: Path) -> None:
    runner, _ = _runner(tmp_path, FakeDocker(result=encode(sample_report())))

    assert runner.image_digest is None


# --- one container per attempt ---------------------------------------------


def test_a_redelivered_run_does_not_reuse_the_dead_attempts_container_name(
    tmp_path: Path,
) -> None:
    """A worker killed mid-validation leaves `af-<run>-<attempt>` behind; the
    worker that receives the run again must not ask Docker for that name."""
    docker = FakeDocker(result=encode(sample_report()))
    request = _request(tmp_path)
    docker.out_dir = request.output_dir
    runner = DockerRunner(DockerConfig(user="1000:1000"), run_process=docker)

    runner.run(request)
    runner.run(request)

    runs = [call for call in docker.calls if call[1] == "run"]
    names = [_value_after(call, "--name") for call in runs]
    assert len(set(names)) == 2
    for call in runs:
        assert _value_after(call, "--label") == f"agentfoundry.run_id={RUN_ID.value}"


def test_recovery_removes_every_container_the_run_left(tmp_path: Path) -> None:
    docker = FakeDocker(listed=b"0f1e2d3c4b5a\n9a8b7c6d5e4f\n")
    runner, _ = _runner(tmp_path, docker)

    runner.remove_container(RUN_ID)

    assert docker.calls == [
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=agentfoundry.run_id={RUN_ID.value}",
        ],
        ["docker", "rm", "--force", "0f1e2d3c4b5a"],
        ["docker", "rm", "--force", "9a8b7c6d5e4f"],
    ]


def test_recovery_of_a_run_with_no_container_left_removes_nothing(
    tmp_path: Path,
) -> None:
    docker = FakeDocker(listed=b"")
    runner, _ = _runner(tmp_path, docker)

    runner.remove_container(RUN_ID)

    assert docker.verbs() == ["ps"]
