"""`af doctor`: check that this machine's container runtime enforces the limits.

The limits are asserted by unit tests on the `docker run` command line, but a
command line is only a request. Whether the runtime honours it depends on the
machine -- cgroups v1 or v2, the Docker version, a VM in between -- so each
probe runs a real validation container through the same `DockerRunner` that
`af run` uses and reports what it observed.

Integers only in the output, like every other measurement in the project.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from control_plane.adapters.docker_runner import DockerConfig, DockerRunner
from control_plane.domain.ids import RunId
from control_plane.domain.validation import CommandOutcome, ValidationReport
from control_plane.services.errors import ServiceError
from control_plane.services.ports import ValidationRequest

_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024
_SMALL_FILES = 200


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    # None for a measurement with no pass or fail, such as a latency.
    passed: bool | None
    observed: str


Probe = Callable[[], ProbeResult]


def run_probes(probes: Iterable[tuple[str, Probe]]) -> list[ProbeResult]:
    """Run every probe. One that raises fails with its error, and the rest run."""
    results = []
    for name, probe in probes:
        try:
            results.append(probe())
        except Exception as error:
            # A probe that crashes has still observed something: that the
            # runtime could not be checked. Report it and keep going.
            results.append(ProbeResult(name, False, f"{type(error).__name__}: {error}"))
    return results


def render(results: Sequence[ProbeResult]) -> str:
    lines = []
    for result in results:
        status = {True: "PASS", False: "FAIL", None: "INFO"}[result.passed]
        lines.append(f"{status}  {result.name:<10}  {result.observed}")
    return "\n".join(lines)


def exit_code(results: Sequence[ProbeResult]) -> int:
    return 1 if any(result.passed is False for result in results) else 0


def default_probes(image: str, scratch: Path) -> list[tuple[str, Probe]]:
    """The probes for `image`, each validating in its own directory under `scratch`."""
    config = DockerConfig(image=image)

    def validate(
        name: str, commands: tuple[str, ...], probe_config: DockerConfig = config
    ) -> ValidationReport:
        root = scratch / name
        (root / "repo").mkdir(parents=True)
        (root / "out" / "logs").mkdir(parents=True)
        request = ValidationRequest(
            run_id=RunId(f"doctor-{name}-{uuid.uuid4().hex[:8]}"),
            workdir=root / "repo",
            output_dir=root / "out",
            log_dir=root / "out" / "logs",
            commands=commands,
            per_command_timeout_ms=30_000,
            total_timeout_ms=60_000,
        )
        return DockerRunner(probe_config).run(request)

    def runtime() -> ProbeResult:
        completed = subprocess.run(
            [
                "docker",
                "info",
                "--format",
                "server {{.ServerVersion}}, cgroups v{{.CgroupVersion}}, "
                "{{.Architecture}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return ProbeResult("runtime", False, completed.stderr.strip())
        return ProbeResult("runtime", True, completed.stdout.strip())

    def image_present() -> ProbeResult:
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                image,
                "--version",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        version = completed.stdout.strip()
        if completed.returncode != 0 or not version.startswith("afrunner "):
            return ProbeResult("image", False, completed.stderr.strip() or version)
        return ProbeResult("image", True, f"{image}: {version}")

    def pid1() -> ProbeResult:
        report = validate("pid1", ("true",))
        return ProbeResult(
            "pid1", report.pid1, f"pid1={report.pid1}, shell {report.shell}"
        )

    def network() -> ProbeResult:
        report = validate(
            "network",
            (
                'python3 -c "import socket; '
                "socket.create_connection(('1.1.1.1', 53), 3)\"",
            ),
        )
        command = report.commands[0]
        blocked = command.outcome is CommandOutcome.EXITED and command.exit_code != 0
        detail = "blocked" if blocked else f"reachable ({command.outcome.value})"
        return ProbeResult("network", blocked, f"1.1.1.1:53 {detail}")

    def memory() -> ProbeResult:
        limited = replace(config, memory_bytes=_MEMORY_LIMIT_BYTES)
        try:
            validate(
                "memory",
                ("python3 -c \"b = b'x' * (512 * 1024 * 1024)\"",),
                limited,
            )
        except ServiceError as error:
            detected = "memory limit" in str(error)
            return ProbeResult(
                "memory",
                detected,
                f"512 MiB under a {_MEMORY_LIMIT_BYTES}-byte limit: "
                + ("kill detected" if detected else str(error)),
            )
        return ProbeResult("memory", False, "allocation finished: limit not enforced")

    def pids() -> ProbeResult:
        report = validate("pids", ("true",), replace(config, pids_limit=1))
        command = report.commands[0]
        refused = command.outcome is CommandOutcome.SPAWN_FAILED
        return ProbeResult(
            "pids",
            refused,
            f"--pids-limit 1: {command.spawn_error or command.outcome.value}",
        )

    def bind_mount() -> ProbeResult:
        report = validate(
            "bind",
            (
                f"i=0; while [ $i -lt {_SMALL_FILES} ]; do "
                'echo x > "file$i"; i=$((i + 1)); done',
            ),
        )
        command = report.commands[0]
        return ProbeResult(
            "bind",
            None,
            f"{_SMALL_FILES} small files written to /work in {command.duration_ms} ms",
        )

    return [
        ("runtime", runtime),
        ("image", image_present),
        ("pid1", pid1),
        ("network", network),
        ("memory", memory),
        ("pids", pids),
        ("bind", bind_mount),
    ]


def run_doctor(image: str) -> tuple[str, int]:
    """Run the default probes against `image`; return the report and exit code."""
    with tempfile.TemporaryDirectory(prefix="af-doctor-") as scratch:
        results = run_probes(default_probes(image, Path(scratch)))
    return render(results), exit_code(results)
