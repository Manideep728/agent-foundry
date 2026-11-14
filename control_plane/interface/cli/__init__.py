"""Command line entry point.

Exit codes carry the distinction the whole project is built on, so a CI script
never has to parse text:

    0   a measurement exists, whatever it says about the agent
    1   the harness failed; there is no measurement
    2   usage error
    130 aborted by the operator

A run whose agent produced a patch that fails every test exits 0. That is not
leniency -- the harness did its job.

One module per command; `common`, `flags`, `wiring` and `printing` are what
the commands share.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from control_plane.interface.cli.bench import bench_tasks
from control_plane.interface.cli.common import EXIT_USAGE, PROGRAM
from control_plane.interface.cli.distributed import (
    dispatch_tasks,
    run_collector,
    run_worker,
)
from control_plane.interface.cli.oss import fetch_tasks, import_swebench
from control_plane.interface.cli.parser import build_parser
from control_plane.interface.cli.run import run_task
from control_plane.interface.cli.stored import (
    list_runs,
    run_fixture_hash,
    show_run,
    write_report,
)
from control_plane.interface.doctor import run_doctor

__all__ = ["build_parser", "main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Return a process exit code. Never calls sys.exit itself, so that tests
    can drive it directly without catching SystemExit."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        print(f"{PROGRAM}: no command given", file=sys.stderr)
        return EXIT_USAGE

    if args.command == "fixture-hash":
        return run_fixture_hash(args.directory, args.manifest)
    if args.command == "run":
        return run_task(args)
    if args.command == "bench":
        return bench_tasks(args)
    if args.command == "collect":
        return run_collector(args)
    if args.command == "worker":
        return run_worker(args)
    if args.command == "dispatch":
        return dispatch_tasks(args)
    if args.command == "show":
        return show_run(args)
    if args.command == "report":
        return write_report(args)
    if args.command == "list":
        return list_runs(args)
    if args.command == "doctor":
        report, code = run_doctor(args.image)
        print(report)
        return code
    if args.command == "fetch":
        return fetch_tasks(args)
    if args.command == "import-swebench":
        return import_swebench(args)

    print(f"{PROGRAM}: unknown command {args.command!r}", file=sys.stderr)
    return EXIT_USAGE
