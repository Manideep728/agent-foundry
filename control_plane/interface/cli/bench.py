"""`af bench`: evaluate every task in a directory and summarise each suite."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from control_plane.adapters.run_lock import DatabaseBusy
from control_plane.adapters.storage import open_repository
from control_plane.adapters.system import SystemClock
from control_plane.adapters.task_toml import load_tasks
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.budget import can_start_another_run
from control_plane.domain.errors import DomainError
from control_plane.domain.model import total_micros
from control_plane.domain.record import RunRecord
from control_plane.domain.summary import summarise
from control_plane.interface.cli.common import (
    EXIT_ABORTED,
    EXIT_HARNESS_FAILED,
    EXIT_MEASURED,
    EXIT_USAGE,
    PROGRAM,
    exit_code_for,
)
from control_plane.interface.cli.flags import (
    agent_flag_problem,
    ceiling,
    oss_problem,
    repeats_problem,
)
from control_plane.interface.cli.printing import print_line, print_summary
from control_plane.interface.cli.wiring import (
    build_agent,
    build_runners,
    build_service,
    recover,
)


def bench_exit_code(
    records: Sequence[RunRecord], *, planned: int, interrupted: bool
) -> int:
    """0 only when every task in the directory produced a measurement.

    Not "0 if any did". Over eight tasks that would read 0 unless all eight
    failed, so the harness could break on seven and a CI job would stay green
    -- and the reason these codes exist at all is that a script never has to
    parse the text.

    The rule it preserves is the other one: this grades the harness, never the
    agent. Eight runs that all produced no patch are eight measurements and
    exit 0.
    """
    if interrupted:
        return EXIT_ABORTED
    if len(records) != planned:
        return EXIT_HARNESS_FAILED
    if any(exit_code_for(record) != EXIT_MEASURED for record in records):
        return EXIT_HARNESS_FAILED
    return EXIT_MEASURED


def bench_tasks(args: argparse.Namespace) -> int:
    problem = agent_flag_problem(args) or repeats_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    try:
        tasks = load_tasks(args.directory)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE
    if not tasks:
        # Never EXIT_MEASURED. A benchmark of nothing that exits 0 is a green
        # CI job asserting that zero tasks passed.
        print(f"{PROGRAM}: no task files in {args.directory}", file=sys.stderr)
        return EXIT_USAGE
    problem = oss_problem(args, tasks)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    clock = SystemClock()
    runner, docker = build_runners(args, clock)

    # The whole suite once, then again: repeats of one task are spread across
    # the session rather than run back to back, so a drift over time shows up
    # as disagreement instead of hiding inside one burst.
    schedule = [task for _ in range(args.repeats) for task in tasks]
    records: list[RunRecord] = []
    interrupted = False
    try:
        # One lock for the whole suite (D17: "a scheduler that owns the lock").
        # Recovery assumes every unfinished run is abandoned, and that has to
        # hold for the eighth task as much as the first. It must also be this
        # inner path rather than run_task: flock belongs to the open file
        # description, so a second acquisition in this same process contends
        # exactly as another process would.
        with open_repository(args.db, exclusive=True) as repository:
            workspaces = WorkspaceFactory(args.runs)
            recover(repository, workspaces, clock, docker.remove_container)
            service = build_service(
                args, repository, workspaces, runner, clock, build_agent(args, clock)
            )

            for position, task in enumerate(schedule, start=1):
                spent = total_micros(r.total_cost_micros for r in records)
                if not can_start_another_run(
                    spent,
                    ceiling(args.max_bench_micros),
                    ceiling(args.max_run_micros),
                ):
                    # Stopped, not failed. The exit code already says a suite
                    # that measured fewer tasks than it planned is not green,
                    # so no fourth code is needed to carry the reason.
                    print(
                        f"{PROGRAM}: stopping before {task.task_id}: "
                        f"{spent} micro-USD spent of "
                        f"{args.max_bench_micros} allowed",
                        file=sys.stderr,
                    )
                    break

                # Progress on stderr, data on stdout, so redirecting the
                # results still shows which task is running.
                print(
                    f"{PROGRAM}: [{position}/{len(schedule)}] {task.task_id}",
                    file=sys.stderr,
                )
                record = service.execute(task)
                records.append(record)
                print_line(record)
    except DatabaseBusy as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    except KeyboardInterrupt:
        # The service has already written the in-flight run ABORTED and kept
        # its workspace. Every task before it is a real measurement sitting in
        # `records`, and discarding those because the operator stopped the
        # ninth is the behaviour this catch exists to prevent.
        interrupted = True
        print(
            f"{PROGRAM}: aborted after {len(records)} of {len(schedule)}",
            file=sys.stderr,
        )

    for summary in summarise(records):
        print_summary(summary, planned=len(tasks))
    return bench_exit_code(records, planned=len(schedule), interrupted=interrupted)
