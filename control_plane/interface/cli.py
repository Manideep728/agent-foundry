"""Command line entry point.

Exit codes carry the distinction the whole project is built on, so a CI script
never has to parse text:

    0   a measurement exists, whatever it says about the agent
    1   the harness failed; there is no measurement
    2   usage error
    130 aborted by the operator

A run whose agent produced a patch that fails every test exits 0. That is not
leniency -- the harness did its job.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from control_plane import __version__
from control_plane.adapters.docker_runner import DockerRunner
from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.local_runner import LocalRunner
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.system import SystemClock, UuidIdGenerator
from control_plane.adapters.task_toml import load_task
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import manifest_bytes
from control_plane.domain.ids import RunId
from control_plane.domain.record import RunRecord
from control_plane.domain.states import RunState
from control_plane.services.evaluate import EvaluationService
from control_plane.services.recovery import recover_unfinished

PROGRAM = "af"

EXIT_MEASURED = 0
EXIT_HARNESS_FAILED = 1
EXIT_USAGE = 2
EXIT_ABORTED = 130

DEFAULT_DB = Path("af.sqlite3")
DEFAULT_RUNS = Path("runs")
DEFAULT_FIXTURES = Path("tasks/fixtures")
DEFAULT_SOLUTIONS = Path("tasks/solutions")


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
    run.add_argument("--db", type=Path, default=DEFAULT_DB)
    run.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    run.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    run.add_argument("--solutions", type=Path, default=DEFAULT_SOLUTIONS)

    show = subcommands.add_parser("show", help="print a stored run")
    show.add_argument("run_id")
    show.add_argument("--db", type=Path, default=DEFAULT_DB)

    listing = subcommands.add_parser("list", help="list stored runs")
    listing.add_argument("--db", type=Path, default=DEFAULT_DB)
    listing.add_argument("--limit", type=int, default=20)

    return parser


def run_fixture_hash(directory: Path, show_manifest: bool) -> int:
    try:
        digest, entries = digest_tree(directory)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE

    if show_manifest:
        sys.stdout.write(manifest_bytes(entries).decode("ascii"))
    print(digest)
    return EXIT_MEASURED


def _exit_code_for(record: RunRecord) -> int:
    if record.run.state is RunState.SCORED:
        return EXIT_MEASURED
    if record.run.state is RunState.ABORTED:
        return EXIT_ABORTED
    return EXIT_HARNESS_FAILED


def _print_record(record: RunRecord) -> None:
    run = record.run
    print(f"run {run.run_id}  {run.task_id}  [{record.suite.value}]")
    print(f"  state       {run.state.value}")

    if run.failure is not None:
        failed_from = run.failure.failed_from.value
        print(f"  failure     {run.failure.kind.value} (from {failed_from})")
        if run.failure.detail:
            print(f"              {run.failure.detail}")

    if record.outcome is not None:
        print(f"  outcome     {record.outcome.value}")

    if record.score is not None:
        score = record.score
        print(
            f"  resolved    {'yes' if score.resolved else 'no':<4} "
            f"       regressed   {'yes' if score.regressed else 'no'}"
        )
        f2p = f"{score.fail_to_pass_passed}/{score.fail_to_pass_total}"
        p2p = f"{score.pass_to_pass_passed}/{score.pass_to_pass_total}"
        print(f"  fail->pass  {f2p:<12} pass->pass  {p2p}")
        if score.timed_out_commands:
            print(f"  timed out   {score.timed_out_commands} command(s)")

    if record.task_defect:
        print("  TASK DEFECT an empty patch resolved this task; it measures nothing")

    if record.agent_ms is not None or record.validation_ms is not None:
        print(
            f"  duration    agent {record.agent_ms}ms   "
            f"validation {record.validation_ms}ms"
        )
    if record.invocations:
        provider = record.invocations[0].provider_id
        model = record.invocations[0].model_id
        print(
            f"  cost        {record.total_cost_micros} micro-USD  ({provider}/{model})"
        )
    if record.patch_is_empty is not None:
        state = "empty" if record.patch_is_empty else f"{record.patch_bytes} bytes"
        print(f"  patch       {state}")
    print(
        f"  runner      {record.runner_version}  pid1={record.pid1} arch={record.arch}"
    )
    if record.workspace_path:
        print(f"  workspace   {record.workspace_path}")


def run_task(args: argparse.Namespace) -> int:
    try:
        task = load_task(args.task_file)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE

    clock = SystemClock()
    runner = LocalRunner(clock)

    if not runner.isolated:
        # Printed every time, on stderr. Once a runner with a real boundary
        # exists this becomes an explicit opt-in rather than the default.
        print(
            f"{PROGRAM}: warning: running with {runner.runner_id}, which provides "
            f"no isolation. Commands execute as you, with your network and "
            f"filesystem. Development use only.",
            file=sys.stderr,
        )

    with SqliteRunRepository(args.db) as repository:
        workspaces = WorkspaceFactory(args.runs)
        # Removing a container by name needs no image, so the default config
        # serves whichever runner this invocation uses.
        recovered = recover_unfinished(
            repository, workspaces, clock, DockerRunner().remove_container
        )
        for entry in recovered:
            print(
                f"{PROGRAM}: marked {entry.run_id} interrupted ({entry.action.value})",
                file=sys.stderr,
            )
            if entry.cleanup_error is not None:
                print(
                    f"{PROGRAM}: cleanup for {entry.run_id} failed: "
                    f"{entry.cleanup_error}",
                    file=sys.stderr,
                )

        service = EvaluationService(
            repository=repository,
            runner=runner,
            agent=FakeAgent(args.solutions, clock),
            workspaces=workspaces,
            clock=clock,
            ids=UuidIdGenerator(),
            fixtures_root=args.fixtures,
        )
        try:
            record = service.execute(task)
        except KeyboardInterrupt:
            print(f"{PROGRAM}: aborted", file=sys.stderr)
            return EXIT_ABORTED

    _print_record(record)
    return _exit_code_for(record)


def show_run(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"{PROGRAM}: no database at {args.db}", file=sys.stderr)
        return EXIT_USAGE
    with SqliteRunRepository(args.db) as repository:
        try:
            record = repository.get(RunId(args.run_id))
        except DomainError as error:
            print(f"{PROGRAM}: {error}", file=sys.stderr)
            return EXIT_USAGE
    if record is None:
        print(f"{PROGRAM}: no run {args.run_id}", file=sys.stderr)
        return EXIT_USAGE
    _print_record(record)
    return _exit_code_for(record)


def list_runs(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"{PROGRAM}: no database at {args.db}", file=sys.stderr)
        return EXIT_USAGE
    with SqliteRunRepository(args.db) as repository:
        records = repository.list_runs(args.limit)

    if not records:
        print("no runs recorded")
        return EXIT_MEASURED

    for record in records:
        outcome = record.outcome.value if record.outcome else record.run.state.value
        print(
            f"{record.run.run_id}  {record.run.task_id:<24} "
            f"{record.suite.value:<10} {outcome}"
        )
    return EXIT_MEASURED


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
    if args.command == "show":
        return show_run(args)
    if args.command == "list":
        return list_runs(args)

    print(f"{PROGRAM}: unknown command {args.command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
