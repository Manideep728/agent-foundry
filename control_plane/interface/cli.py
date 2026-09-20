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
from collections.abc import Callable, Sequence
from pathlib import Path

from control_plane import __version__
from control_plane.adapters.docker_runner import DockerConfig, DockerRunner
from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.local_runner import LocalRunner
from control_plane.adapters.ollama_client import DEFAULT_BASE_URL, OllamaClient
from control_plane.adapters.run_lock import DatabaseBusy, exclusive_run_lock
from control_plane.adapters.single_shot_agent import SingleShotAgent
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.system import SystemClock, UuidIdGenerator
from control_plane.adapters.task_toml import load_task, load_tasks
from control_plane.adapters.tool_use_agent import DEFAULT_MAX_TURNS, ToolUseAgent
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import manifest_bytes
from control_plane.domain.ids import RunId
from control_plane.domain.outcomes import FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.states import RunState
from control_plane.domain.summary import (
    Configuration,
    Durations,
    SuiteSummary,
    summarise,
)
from control_plane.domain.task import TaskDefinition
from control_plane.interface.doctor import run_doctor
from control_plane.services.evaluate import EvaluationService
from control_plane.services.ports import AgentPort, ValidationRunner
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
DEFAULT_TASKS = Path("tasks")
DEFAULT_IMAGE = "afrunner:local"

RUNNER_DOCKER = "docker"
RUNNER_LOCAL = "local"

AGENT_FAKE = "fake"
AGENT_OLLAMA = "ollama"
AGENT_TOOLS = "tools"

DEFAULT_MODEL = "qwen2.5-coder:3b"


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    """The flags any evaluation needs, shared by `run` and `bench`.

    A function rather than argparse's `parents=`, because a shared parent
    parser puts the same Action objects into two parsers -- a subtlety nobody
    reading this should have to know about. What matters is that the two
    subcommands cannot drift apart: a flag added to one is added to both.
    """
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--solutions", type=Path, default=DEFAULT_SOLUTIONS)
    parser.add_argument(
        "--runner",
        choices=(RUNNER_DOCKER, RUNNER_LOCAL),
        default=RUNNER_DOCKER,
        help=(
            "docker validates inside the runner image; local runs the commands "
            "on this machine with no isolation, for development only"
        ),
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="the validation image for --runner docker (build it: make runner-image)",
    )
    parser.add_argument(
        "--agent",
        choices=(AGENT_FAKE, AGENT_OLLAMA, AGENT_TOOLS),
        default=AGENT_FAKE,
        help=(
            "fake applies a known solution and calls no model, which is what "
            "CI and offline development use; ollama sends the whole fixture in "
            "one prompt and applies whatever comes back; tools gives the model "
            "read and write tools and several turns to use them"
        ),
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help=(
            f"turn budget for --agent tools (default {DEFAULT_MAX_TURNS}); a "
            f"model that never stops reading is ended and measured rather than "
            f"left to hit the task timeout"
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"the model for --agent ollama (default {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_BASE_URL,
        help=f"where the ollama server is (default {DEFAULT_BASE_URL})",
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
    _add_run_flags(run)

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
    _add_run_flags(bench)

    show = subcommands.add_parser("show", help="print a stored run")
    show.add_argument("run_id")
    show.add_argument("--db", type=Path, default=DEFAULT_DB)

    listing = subcommands.add_parser("list", help="list stored runs")
    listing.add_argument("--db", type=Path, default=DEFAULT_DB)
    listing.add_argument("--limit", type=int, default=20)

    doctor = subcommands.add_parser(
        "doctor",
        help="check that the container runtime enforces the validation limits",
    )
    doctor.add_argument("--image", default=DEFAULT_IMAGE)

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
        # Rendered a phase at a time. A run whose agent finished and whose
        # validation never started has a real agent duration and no validation
        # one, and printing "validation Nonems" for it would be worse than
        # either number. That combination became reachable when the runner
        # failure paths started keeping the agent result.
        agent = "-" if record.agent_ms is None else f"{record.agent_ms}ms"
        validation = (
            "-" if record.validation_ms is None else f"{record.validation_ms}ms"
        )
        print(f"  duration    agent {agent}   validation {validation}")
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
    # Conditional, so the local runner's output is unchanged. A line reading
    # "image None" on every development run is how a provenance field becomes
    # noise that everyone learns to skip. Printed in full, not abbreviated:
    # this is the value someone reproduces a run from.
    if record.image_digest:
        print(f"  image       {record.image_digest}")
    # Says why an outcome reads the way it does. Without it, malformed_output
    # is a verdict with no evidence attached.
    if record.agent_note:
        print(f"  agent note  {record.agent_note}")
    if record.workspace_path:
        print(f"  workspace   {record.workspace_path}")


def _build_runners(
    args: argparse.Namespace, clock: SystemClock
) -> tuple[ValidationRunner, DockerRunner]:
    """The runner the flags asked for, and the Docker runner recovery needs.

    Two values, because they answer different questions. The second is built
    whichever runner validates, since recovery removes containers left behind
    by *earlier* Docker runs even when this invocation is local.

    The isolation warning is printed here, which is once per invocation rather
    than once per task. Eight copies of it in a benchmark is how a warning
    becomes wallpaper.
    """
    docker = DockerRunner(DockerConfig(image=args.image))
    runner: ValidationRunner = (
        docker if args.runner == RUNNER_DOCKER else LocalRunner(clock)
    )
    if not runner.isolated:
        # On stderr, so an unisolated run is never mistaken for a measured one.
        print(
            f"{PROGRAM}: warning: running with {runner.runner_id}, which provides "
            f"no isolation. Commands execute as you, with your network and "
            f"filesystem. Development use only.",
            file=sys.stderr,
        )
    return runner, docker


def _recover(
    repository: SqliteRunRepository,
    workspaces: WorkspaceFactory,
    clock: SystemClock,
    remove_container: Callable[[RunId], None],
) -> None:
    """Mark abandoned runs interrupted, and say on stderr what was cleaned up.

    Recovery removes containers left by earlier Docker runs whichever runner
    this invocation uses, and it is idempotent -- a second pass finds nothing,
    because the runs it touched are now terminal. So a suite calls it once
    before its first task rather than before each one.
    """
    for entry in recover_unfinished(repository, workspaces, clock, remove_container):
        print(
            f"{PROGRAM}: marked {entry.run_id} interrupted ({entry.action.value})",
            file=sys.stderr,
        )
        if entry.cleanup_error is not None:
            print(
                f"{PROGRAM}: cleanup for {entry.run_id} failed: {entry.cleanup_error}",
                file=sys.stderr,
            )


def _build_service(
    args: argparse.Namespace,
    repository: SqliteRunRepository,
    workspaces: WorkspaceFactory,
    runner: ValidationRunner,
    clock: SystemClock,
) -> EvaluationService:
    """One service for the whole invocation.

    Every dependency is invocation-scoped and `execute` writes no state back
    onto the service, so one instance drives a whole suite.
    """
    return EvaluationService(
        repository=repository,
        runner=runner,
        agent=_build_agent(args, clock),
        workspaces=workspaces,
        clock=clock,
        ids=UuidIdGenerator(),
        fixtures_root=args.fixtures,
    )


def _recover_and_evaluate(
    args: argparse.Namespace,
    task: TaskDefinition,
    runner: ValidationRunner,
    docker: DockerRunner,
    clock: SystemClock,
) -> RunRecord:
    with SqliteRunRepository(args.db) as repository:
        workspaces = WorkspaceFactory(args.runs)
        _recover(repository, workspaces, clock, docker.remove_container)
        service = _build_service(args, repository, workspaces, runner, clock)
        return service.execute(task)


def _build_agent(args: argparse.Namespace, clock: SystemClock) -> AgentPort:
    """Pick the agent the flags asked for.

    `fake` is the default deliberately. It needs no server and no GPU, so the
    test suite, CI and any offline clone keep working exactly as before, and
    reaching a model is something a run has to ask for.
    """
    if args.agent == AGENT_OLLAMA:
        return SingleShotAgent(
            OllamaClient(args.model, base_url=args.ollama_url), clock
        )
    if args.agent == AGENT_TOOLS:
        return ToolUseAgent(
            OllamaClient(args.model, base_url=args.ollama_url),
            clock,
            max_turns=args.max_turns,
        )
    return FakeAgent(args.solutions, clock)


def _print_line(record: RunRecord) -> None:
    """One line per task, shaped like `af list` so the two read the same.

    The run id is on screen because `af show <run-id>` is how a reader gets
    from a summary row to the detail behind it.
    """
    outcome = record.outcome.value if record.outcome else record.run.state.value
    print(f"{record.run.run_id}  {record.run.task_id!s:<24} {outcome}")


def describe(configuration: Configuration) -> str:
    """One line naming a configuration, for any surface that renders one.

    The strategy first, because it is what a reader most often wants to
    compare and what two rows of the same model differ by.
    """
    if not configuration.is_named:
        if configuration.provider_id is not None:
            # Stored before the harness recorded the strategy. The provider and
            # model are still known, and printing them beats "unrecorded".
            return f"?/{configuration.provider_id}/{configuration.model_id}"
        return "unrecorded"
    return (
        f"{configuration.agent_id}/{configuration.provider_id}/{configuration.model_id}"
    )


def _print_durations(label: str, durations: object) -> None:
    if durations is None:
        return
    assert isinstance(durations, Durations)
    print(
        f"  {label:<13} min {durations.fastest_ms}ms  "
        f"median {durations.median_ms}ms  max {durations.slowest_ms}ms"
    )


def _print_summary(summary: SuiteSummary, *, planned: int) -> None:
    """Render one suite. Computation lives in the domain; this only formats."""
    attempted = summary.runs
    print()
    print(
        f"suite {summary.suite.value}  {planned} task(s)  "
        f"{summary.measured} measured of {attempted} attempted"
    )

    # D6: a report states which implementation produced each run. One line,
    # because the configuration is the grouping key -- a block covering two of
    # them cannot be built.
    #
    # "config", not "agent". The duration rows below are named for the agent
    # and validation *phases*, and one label meaning two things in one block
    # is how a reader stops trusting either.
    print(f"  config        {describe(summary.configuration)}")
    for digest in summary.image_digests:
        print(f"  image         {digest}")

    print(f"  resolved      {summary.resolved}/{summary.measured}")
    print(f"  regressed     {summary.regressed}/{summary.measured}")
    # Every outcome, including the zeros. A rate nobody can see is zero is a
    # rate nobody acts on.
    for outcome, count in summary.outcomes.items():
        print(f"  {'':<13} {outcome.value:<24} {count}")

    if summary.aborted:
        print(f"  aborted       {summary.aborted}")
    if summary.unmeasured:
        print(f"  no measure    {len(summary.unmeasured)}")
        for entry in summary.unmeasured:
            print(
                f"  {'':<13} {entry.kind.value} (from {entry.failed_from.value})  "
                f"{entry.task_id}  {entry.run_id}"
            )
            if entry.detail:
                print(f"  {'':<13}   {entry.detail}")
    if summary.runs_with_timeouts:
        print(
            f"  timed out     {summary.timed_out_commands} command(s) in "
            f"{summary.runs_with_timeouts} run(s)"
        )
    if summary.without_regression_check:
        print(
            f"  uncovered     {summary.without_regression_check} measured run(s) "
            f"had no pass->pass command to regress"
        )

    print(
        f"  cost          {int(summary.measured_cost_micros)} micro-USD over "
        f"{summary.measured} measured run(s)"
    )
    if summary.unmeasured_runs_that_spent:
        # Reported, never added in. FailureCategory forbids a failed run
        # contributing to a cost total; it does not forbid saying what the
        # harness cost when it produced nothing.
        print(
            f"  {'':<13} {int(summary.unmeasured_cost_micros)} micro-USD spent by "
            f"{summary.unmeasured_runs_that_spent} run(s) that measured nothing"
        )
    _print_durations("agent", summary.agent_ms)
    _print_durations("validation", summary.validation_ms)


def _bench_exit_code(
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
    if any(_exit_code_for(record) != EXIT_MEASURED for record in records):
        return EXIT_HARNESS_FAILED
    return EXIT_MEASURED


def bench_tasks(args: argparse.Namespace) -> int:
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

    clock = SystemClock()
    runner, docker = _build_runners(args, clock)

    records: list[RunRecord] = []
    interrupted = False
    try:
        # One lock for the whole suite (D17: "a scheduler that owns the lock").
        # Recovery assumes every unfinished run is abandoned, and that has to
        # hold for the eighth task as much as the first. It must also be this
        # inner path rather than run_task: flock belongs to the open file
        # description, so a second acquisition in this same process contends
        # exactly as another process would.
        with exclusive_run_lock(args.db), SqliteRunRepository(args.db) as repository:
            workspaces = WorkspaceFactory(args.runs)
            _recover(repository, workspaces, clock, docker.remove_container)
            service = _build_service(args, repository, workspaces, runner, clock)

            for position, task in enumerate(tasks, start=1):
                # Progress on stderr, data on stdout, so redirecting the
                # results still shows which task is running.
                print(
                    f"{PROGRAM}: [{position}/{len(tasks)}] {task.task_id}",
                    file=sys.stderr,
                )
                record = service.execute(task)
                records.append(record)
                _print_line(record)
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
            f"{PROGRAM}: aborted after {len(records)} of {len(tasks)}",
            file=sys.stderr,
        )

    for summary in summarise(records):
        _print_summary(summary, planned=len(tasks))
    return _bench_exit_code(records, planned=len(tasks), interrupted=interrupted)


def run_task(args: argparse.Namespace) -> int:
    try:
        task = load_task(args.task_file)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE

    clock = SystemClock()
    runner, docker = _build_runners(args, clock)

    # Held from before recovery until the run is stored. Recovery assumes every
    # unfinished run is abandoned, which is only true while no other af run
    # uses this database.
    try:
        with exclusive_run_lock(args.db):
            record = _recover_and_evaluate(args, task, runner, docker, clock)
    except DatabaseBusy as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    except KeyboardInterrupt:
        print(f"{PROGRAM}: aborted", file=sys.stderr)
        return EXIT_ABORTED

    _print_record(record)
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
    if args.command == "bench":
        return bench_tasks(args)
    if args.command == "show":
        return show_run(args)
    if args.command == "list":
        return list_runs(args)
    if args.command == "doctor":
        report, code = run_doctor(args.image)
        print(report)
        return code

    print(f"{PROGRAM}: unknown command {args.command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
