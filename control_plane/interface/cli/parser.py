"""The argument parser: every subcommand and the flags it takes."""

from __future__ import annotations

import argparse
from pathlib import Path

from control_plane import __version__
from control_plane.interface.cli.common import (
    DEFAULT_IMAGE,
    DEFAULT_REPORT,
    DEFAULT_TASKS,
    PROGRAM,
)
from control_plane.interface.cli.flags import (
    add_agent_flags,
    add_bus_flag,
    add_db_flag,
    add_limit_flags,
    add_repeats_flag,
    add_run_flags,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Run and evaluate AI coding agents against repository tasks.",
    )
    parser.add_argument(
        "--version", action="version", version=f"{PROGRAM} {__version__}"
    )
    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    fixture_hash = subcommands.add_parser(
        "fixture-hash",
        help="print the content digest of a fixture directory",
        description=(
            "Print the digest a task file should pin. Producing it with the "
            "same code that verifies it is the point: a digest taken any other "
            "way would surface later as an unexplained FIXTURE_HASH_MISMATCH."
        ),
    )
    fixture_hash.add_argument("directory", type=Path)
    fixture_hash.add_argument("--manifest", action="store_true")

    run = subcommands.add_parser("run", help="evaluate one task")
    run.add_argument("task_file", type=Path)
    add_run_flags(run)

    bench = subcommands.add_parser(
        "bench",
        help="evaluate every task in a directory",
        description=(
            "Evaluate every *.toml directly inside DIRECTORY, in filename "
            "order, then print one summary per suite. A single database lock "
            "is held for the whole suite, so nothing else may run against this "
            "database while it works."
        ),
    )
    bench.add_argument("directory", type=Path, nargs="?", default=DEFAULT_TASKS)
    add_run_flags(bench)
    # Deliberately not in `add_run_flags`, which exists so the subcommands
    # cannot drift apart. A suite ceiling on `af run` would be a flag that
    # silently does nothing, which is the failure the TOML loader's
    # unknown-key rule exists to prevent one level up.
    bench.add_argument(
        "--max-bench-micros",
        type=int,
        default=None,
        help=(
            "stop the suite before the first task it could not afford to "
            "finish. Counted over this invocation, not over the database: a "
            "budget belongs to an experiment, not to a file"
        ),
    )
    add_repeats_flag(bench)

    dispatch_parser = subcommands.add_parser(
        "dispatch",
        help="publish every task in a directory as work for a worker pool",
        description=(
            "Publish one message per task to the work topic and exit. This "
            "command measures nothing and writes no database: it decides what "
            "is to be measured, and `af worker` does the measuring. The run "
            "identifiers are minted here, so a message redelivered to a worker "
            "is recognisable as the same run rather than counted twice."
        ),
    )
    dispatch_parser.add_argument(
        "directory", type=Path, nargs="?", default=DEFAULT_TASKS
    )
    add_agent_flags(dispatch_parser)
    add_bus_flag(dispatch_parser)
    add_repeats_flag(dispatch_parser)

    worker = subcommands.add_parser(
        "worker",
        help="measure work from the queue and publish each result",
        description=(
            "Join the worker group, measure each message under this worker's "
            "own configuration, and publish the record to the results topic. "
            "A message asking for another configuration, or for a task this "
            "checkout does not hold, stops the worker without acknowledging "
            "it, so a worker that can honour it takes it instead. The --db "
            "here is this worker's own; `af collect` keeps the shared one."
        ),
    )
    worker.add_argument("directory", type=Path, nargs="?", default=DEFAULT_TASKS)
    add_run_flags(worker)
    add_bus_flag(worker)
    worker.add_argument(
        "--worker-id",
        default=None,
        help="the name recorded as provenance (default: host name and pid)",
    )
    worker.add_argument(
        "--artifacts",
        default=None,
        metavar="s3://BUCKET[/PREFIX]",
        help=(
            "also store each run's files under PREFIX/runs/RUN_ID/ in this "
            "bucket, before its result is published (needs the aws extra)"
        ),
    )
    add_limit_flags(worker)

    collect = subcommands.add_parser(
        "collect",
        help="store the results the workers publish",
        description=(
            "Read the results topic and store each finished run in --db, the "
            "one database that owns the results. A run that arrives twice is "
            "stored once; one that arrives twice with different content keeps "
            "the first and says so. Holds the run lock, like `bench`, so this "
            "is the only writer."
        ),
    )
    add_db_flag(collect)
    add_bus_flag(collect)
    add_limit_flags(collect)

    show = subcommands.add_parser("show", help="print a stored run")
    show.add_argument("run_id")
    add_db_flag(show)

    report = subcommands.add_parser(
        "report",
        help="write a Markdown report over the stored runs",
        description=(
            "Summarise every run in the database, one section per suite and "
            "configuration, and write it as Markdown. Reads only: it takes no "
            "run lock, so a report can be taken while a suite is running, and "
            "any run still in flight is named and excluded rather than "
            "counted."
        ),
    )
    add_db_flag(report)
    report.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT,
        help="where to write it; - writes to stdout",
    )

    listing = subcommands.add_parser("list", help="list stored runs")
    add_db_flag(listing)
    listing.add_argument("--limit", type=int, default=20)

    doctor = subcommands.add_parser(
        "doctor",
        help="check that the container runtime enforces the validation limits",
    )
    doctor.add_argument("--image", default=DEFAULT_IMAGE)

    return parser
