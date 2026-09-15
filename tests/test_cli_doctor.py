"""`af doctor`: the report and exit code, then one run against the real image."""

from __future__ import annotations

import pytest

from control_plane.interface.cli import main
from control_plane.interface.doctor import (
    ProbeResult,
    exit_code,
    render,
    run_probes,
)
from tests.docker_support import IMAGE, requires_image


def test_every_probe_passing_exits_zero_and_informational_ones_do_not_count() -> None:
    results = [
        ProbeResult("runtime", True, "server 29.1.3"),
        ProbeResult("bind", None, "200 small files in 180 ms"),
    ]

    assert exit_code(results) == 0


def test_one_failing_probe_exits_one() -> None:
    results = [
        ProbeResult("runtime", True, "server 29.1.3"),
        ProbeResult("network", False, "1.1.1.1:53 reachable"),
    ]

    assert exit_code(results) == 1


def test_the_report_marks_each_probe_and_keeps_its_order() -> None:
    report = render(
        [
            ProbeResult("runtime", True, "server 29.1.3"),
            ProbeResult("network", False, "reachable"),
            ProbeResult("bind", None, "180 ms"),
        ]
    )

    assert report.splitlines() == [
        "PASS  runtime     server 29.1.3",
        "FAIL  network     reachable",
        "INFO  bind        180 ms",
    ]


def test_a_probe_that_raises_fails_and_the_rest_still_run() -> None:
    def broken() -> ProbeResult:
        raise FileNotFoundError("docker")

    results = run_probes(
        [
            ("runtime", broken),
            ("bind", lambda: ProbeResult("bind", None, "180 ms")),
        ]
    )

    assert results[0] == ProbeResult("runtime", False, "FileNotFoundError: docker")
    assert results[1].name == "bind"
    assert exit_code(results) == 1


@pytest.mark.docker
@requires_image
def test_the_doctor_passes_on_a_machine_that_enforces_the_limits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["doctor", "--image", IMAGE]) == 0

    lines = capsys.readouterr().out.splitlines()
    names = [line.split()[1] for line in lines]
    assert names == ["runtime", "image", "pid1", "network", "memory", "pids", "bind"]
    assert not [line for line in lines if line.startswith("FAIL")]
