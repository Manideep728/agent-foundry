"""`af run`: evaluate one task."""

from __future__ import annotations

import argparse
import sys

from control_plane.adapters.run_lock import DatabaseBusy
from control_plane.adapters.system import SystemClock
from control_plane.adapters.task_toml import load_task
from control_plane.domain.errors import DomainError
from control_plane.domain.outcomes import FailureCategory
from control_plane.interface.cli.common import (
    EXIT_ABORTED,
    EXIT_HARNESS_FAILED,
    EXIT_USAGE,
    PROGRAM,
    RUNNER_DOCKER,
    exit_code_for,
)
from control_plane.interface.cli.flags import agent_flag_problem, oss_problem
from control_plane.interface.cli.printing import print_record
from control_plane.interface.cli.wiring import build_runners, recover_and_evaluate


def run_task(args: argparse.Namespace) -> int:
    problem = agent_flag_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    try:
        task = load_task(args.task_file)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE
    problem = oss_problem(args, (task,))
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    clock = SystemClock()
    runner, docker = build_runners(args, clock)

    # Held from before recovery until the run is stored. Recovery assumes every
    # unfinished run is abandoned, which is only true while no other af run
    # uses this database.
    try:
        record = recover_and_evaluate(args, task, runner, docker, clock)
    except DatabaseBusy as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    except KeyboardInterrupt:
        print(f"{PROGRAM}: aborted", file=sys.stderr)
        return EXIT_ABORTED

    print_record(record)
    failure = record.run.failure
    if (
        args.runner == RUNNER_DOCKER
        and failure is not None
        and failure.kind is FailureCategory.INFRASTRUCTURE_FAILED
    ):
        # The cause is in the failure detail, and it is not always setup: a
        # memory-limit kill is also an infrastructure failure. So the hint
        # says what to do if the detail points at Docker, without claiming it.
        print(
            f"{PROGRAM}: validation produced no measurement; the failure detail "
            f"says why. If Docker is not running or the image is missing, start "
            f"Docker and run make runner-image, or pass --runner local for an "
            f"unisolated development run.",
            file=sys.stderr,
        )
    return exit_code_for(record)
