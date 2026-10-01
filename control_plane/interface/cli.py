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
import os
import socket
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from control_plane import __version__
from control_plane.adapters.dispatch_json import decode as decode_message
from control_plane.adapters.dispatch_json import encode as encode_message
from control_plane.adapters.docker_runner import DockerConfig, DockerRunner
from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.kafka_bus import (
    DEFAULT_BOOTSTRAP,
    DEFAULT_MAX_POLL_INTERVAL_MS,
    KafkaConsumer,
    KafkaPublisher,
    KafkaUnavailable,
)
from control_plane.adapters.local_runner import LocalRunner
from control_plane.adapters.ollama_client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_S,
    OllamaClient,
)
from control_plane.adapters.prompts import (
    DEFAULT_SINGLE_SHOT_PROMPT,
    DEFAULT_TOOL_PROMPT,
    VARIANTS,
    PromptVariant,
    variant,
)
from control_plane.adapters.record_json import decode as decode_result
from control_plane.adapters.record_json import encode as encode_result
from control_plane.adapters.run_lock import DatabaseBusy, exclusive_run_lock
from control_plane.adapters.single_shot_agent import SingleShotAgent
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.system import (
    SystemClock,
    UuidIdGenerator,
    harness_version,
)
from control_plane.adapters.task_toml import load_task, load_tasks
from control_plane.adapters.tool_use_agent import DEFAULT_MAX_TURNS, ToolUseAgent
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.budget import can_start_another_run, validate_ceiling
from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import manifest_bytes
from control_plane.domain.ids import RunId
from control_plane.domain.model import MicroUsd, total_micros
from control_plane.domain.outcomes import FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.states import RunState
from control_plane.domain.summary import Durations, SuiteSummary, summarise
from control_plane.domain.task import TaskDefinition
from control_plane.interface.doctor import run_doctor
from control_plane.interface.report import describe, render, unfinished
from control_plane.services.collect import Collected, Collection, Collector
from control_plane.services.dispatch import (
    COLLECTOR_GROUP,
    RUN_RESULTS_TOPIC,
    TASK_RUNS_TOPIC,
    WORKER_GROUP,
    DispatchIncomplete,
    IncompatibleMessage,
    RawDelivery,
    RunConfiguration,
    dispatch,
)
from control_plane.services.evaluate import EvaluationService
from control_plane.services.polling import poll_until
from control_plane.services.ports import AgentPort, ValidationRunner
from control_plane.services.recovery import recover_unfinished
from control_plane.services.worker import (
    Handled,
    Handling,
    ResultNotDelivered,
    Worker,
)

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
DEFAULT_REPORT = Path("report.md")
DEFAULT_IMAGE = "afrunner:local"

RUNNER_DOCKER = "docker"
RUNNER_LOCAL = "local"

AGENT_FAKE = "fake"
AGENT_OLLAMA = "ollama"
AGENT_TOOLS = "tools"

DEFAULT_MODEL = "qwen2.5-coder:3b"


def _add_agent_flags(parser: argparse.ArgumentParser) -> None:
    """The flags that say *what* to measure, rather than where to put it.

    Separate from `_add_run_flags` because `dispatch` needs exactly these and
    none of the others: it writes no database, makes no workspace and runs no
    validation, so `--db` and `--runs` would be flags it accepted and ignored.
    Split rather than duplicated -- two lists of the same flags would drift,
    and the drift would be a dispatcher that sends a configuration no worker
    can be given.
    """
    parser.add_argument("--solutions", type=Path, default=DEFAULT_SOLUTIONS)
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
        "--prompt",
        default=None,
        help=(
            "the named prompt variant to send (default: "
            f"{DEFAULT_TOOL_PROMPT} for --agent tools, "
            f"{DEFAULT_SINGLE_SHOT_PROMPT} for --agent ollama). "
            f"Known: {', '.join(sorted(VARIANTS))}"
        ),
    )
    parser.add_argument(
        "--max-run-micros",
        type=int,
        default=None,
        help=(
            "stop one run once it has spent this many micro-USD. The loop "
            "reports what it managed; the run stays a measurement. Overshoot "
            "is bounded by one model call, because the cost of a call is not "
            "known until it returns"
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
    parser.add_argument(
        "--ollama-timeout",
        type=int,
        default=int(DEFAULT_TIMEOUT_S),
        metavar="SECONDS",
        help=(
            f"how long one model call may take before the run fails as an agent "
            f"error (default {int(DEFAULT_TIMEOUT_S)}). A setting of this "
            f"machine, like --ollama-url, so it is not part of what a run is "
            f"grouped on"
        ),
    )


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
    parser.add_argument(
        "--runner",
        choices=(RUNNER_DOCKER, RUNNER_LOCAL),
        default=RUNNER_DOCKER,
        help=(
            "docker validates inside the runner image; local runs the commands "
            "on this machine with no isolation, for development only"
        ),
    )
    _add_agent_flags(parser)


def _add_bus_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bootstrap",
        default=DEFAULT_BOOTSTRAP,
        help=f"the Kafka broker (default {DEFAULT_BOOTSTRAP})",
    )


def _add_limit_flags(parser: argparse.ArgumentParser) -> None:
    """When a consumer stops by itself. Without either, it runs until stopped."""
    parser.add_argument(
        "--max-messages",
        type=int,
        default=None,
        help="stop after handling this many messages (default: run until stopped)",
    )
    parser.add_argument(
        "--idle-exit-s",
        type=int,
        default=None,
        help="stop once the topic has been quiet this long (default: never)",
    )


def _add_repeats_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help=(
            "how many runs per task (default 1). With more than one, the "
            "summary says which tasks gave the same result, patch and per-call "
            "token counts every time"
        ),
    )


def _repeats_problem(args: argparse.Namespace) -> str | None:
    if args.repeats < 1:
        return f"--repeats must be at least 1, got {args.repeats}"
    return None


def _limit_problem(args: argparse.Namespace) -> str | None:
    """Zero would be a consumer that joins its group and does nothing."""
    for flag, value in (
        ("--max-messages", args.max_messages),
        ("--idle-exit-s", args.idle_exit_s),
    ):
        if value is not None and value < 1:
            return f"{flag} must be at least 1, got {value}"
    return None


def _idle_exit_ms(args: argparse.Namespace) -> int | None:
    return None if args.idle_exit_s is None else args.idle_exit_s * 1000


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
    # Deliberately not in `_add_run_flags`, which exists so the two subcommands
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
    _add_repeats_flag(bench)

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
    _add_agent_flags(dispatch_parser)
    _add_bus_flag(dispatch_parser)
    _add_repeats_flag(dispatch_parser)

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
    _add_run_flags(worker)
    _add_bus_flag(worker)
    worker.add_argument(
        "--worker-id",
        default=None,
        help="the name recorded as provenance (default: host name and pid)",
    )
    _add_limit_flags(worker)

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
    collect.add_argument("--db", type=Path, default=DEFAULT_DB)
    _add_bus_flag(collect)
    _add_limit_flags(collect)

    show = subcommands.add_parser("show", help="print a stored run")
    show.add_argument("run_id")
    show.add_argument("--db", type=Path, default=DEFAULT_DB)

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
    report.add_argument("--db", type=Path, default=DEFAULT_DB)
    report.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT,
        help="where to write it; - writes to stdout",
    )

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
    agent: AgentPort,
) -> EvaluationService:
    """One service for the whole invocation.

    Every dependency is invocation-scoped and `execute` writes no state back
    onto the service, so one instance drives a whole suite. The agent is passed
    in rather than built here so that a worker checks messages against the
    same agent that measures them.
    """
    return EvaluationService(
        repository=repository,
        runner=runner,
        agent=agent,
        workspaces=workspaces,
        clock=clock,
        ids=UuidIdGenerator(),
        fixtures_root=args.fixtures,
        harness_version=harness_version(),
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
        service = _build_service(
            args, repository, workspaces, runner, clock, _build_agent(args, clock)
        )
        return service.execute(task)


def _build_agent(args: argparse.Namespace, clock: SystemClock) -> AgentPort:
    """Pick the agent the flags asked for.

    `fake` is the default deliberately. It needs no server and no GPU, so the
    test suite, CI and any offline clone keep working exactly as before, and
    reaching a model is something a run has to ask for.
    """
    if args.agent == AGENT_OLLAMA:
        return SingleShotAgent(
            _ollama_client(args),
            clock,
            prompt=_prompt(args, DEFAULT_SINGLE_SHOT_PROMPT),
        )
    if args.agent == AGENT_TOOLS:
        return ToolUseAgent(
            _ollama_client(args),
            clock,
            max_turns=args.max_turns,
            prompt=_prompt(args, DEFAULT_TOOL_PROMPT),
            run_ceiling=_ceiling(args.max_run_micros),
        )
    return FakeAgent(args.solutions, clock)


def _ollama_client(args: argparse.Namespace) -> OllamaClient:
    return OllamaClient(
        args.model, base_url=args.ollama_url, timeout_s=args.ollama_timeout
    )


def _poll_interval_ms(args: argparse.Namespace) -> int:
    """How long a worker may hold one message before its group evicts it (D32).

    D29 set fifteen minutes as three times the longest handling measured, and
    found that the longest is set by the model client's timeout, not by the
    task. So the interval follows the timeout at the same ratio, and never
    drops below the measured fifteen minutes.
    """
    timeout_ms: int = args.ollama_timeout * 1000
    return max(DEFAULT_MAX_POLL_INTERVAL_MS, 3 * timeout_ms)


def _prompt(args: argparse.Namespace, fallback: str) -> PromptVariant:
    return variant(args.prompt or fallback)


def _ceiling(value: int | None) -> MicroUsd | None:
    return None if value is None else MicroUsd(value)


def _agent_flag_problem(args: argparse.Namespace) -> str | None:
    """Why the agent flags cannot be honoured, if they cannot.

    Checked before a run is created, so a typo costs nothing. Resolved inside
    the agent instead, it would create a row and spend tokens before failing,
    and leave an unfinished run behind for the recovery scan.
    """
    if args.ollama_timeout < 1:
        return f"--ollama-timeout must be at least 1, got {args.ollama_timeout}"
    try:
        validate_ceiling(_ceiling(args.max_run_micros), "--max-run-micros")
        validate_ceiling(
            _ceiling(getattr(args, "max_bench_micros", None)), "--max-bench-micros"
        )
    except DomainError as error:
        return str(error)

    if args.prompt is None:
        return None
    if args.agent == AGENT_FAKE:
        # Refused rather than ignored. A flag that silently does nothing would
        # let a whole suite be recorded under a prompt nothing ever sent.
        return f"--agent {AGENT_FAKE} sends no prompt, so --prompt cannot apply"
    try:
        variant(args.prompt)
    except DomainError as error:
        return str(error)
    return None


def _print_line(record: RunRecord) -> None:
    """One line per task, shaped like `af list` so the two read the same.

    The run id is on screen because `af show <run-id>` is how a reader gets
    from a summary row to the detail behind it.
    """
    outcome = record.outcome.value if record.outcome else record.run.state.value
    print(f"{record.run.run_id}  {record.run.task_id!s:<24} {outcome}")


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
    for version in summary.harness_versions:
        print(f"  harness       {version}")

    print(f"  resolved      {summary.resolved}/{summary.measured}")
    print(f"  regressed     {summary.regressed}/{summary.measured}")
    if summary.repeated:
        print(f"  tasks agree   {summary.tasks_agreeing}/{len(summary.tasks)}")
        for task in summary.tasks:
            if not task.agrees:
                print(
                    f"  {'':<13} differs  {task.task_id}  "
                    f"{', '.join(task.results)}  {task.patches} patch(es)  "
                    f"{task.token_traces} token trace(s)"
                )
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
    problem = _agent_flag_problem(args) or _repeats_problem(args)
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

    clock = SystemClock()
    runner, docker = _build_runners(args, clock)

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
        with exclusive_run_lock(args.db), SqliteRunRepository(args.db) as repository:
            workspaces = WorkspaceFactory(args.runs)
            _recover(repository, workspaces, clock, docker.remove_container)
            service = _build_service(
                args, repository, workspaces, runner, clock, _build_agent(args, clock)
            )

            for position, task in enumerate(schedule, start=1):
                spent = total_micros(r.total_cost_micros for r in records)
                if not can_start_another_run(
                    spent,
                    _ceiling(args.max_bench_micros),
                    _ceiling(args.max_run_micros),
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
            f"{PROGRAM}: aborted after {len(records)} of {len(schedule)}",
            file=sys.stderr,
        )

    for summary in summarise(records):
        _print_summary(summary, planned=len(tasks))
    return _bench_exit_code(records, planned=len(schedule), interrupted=interrupted)


def run_task(args: argparse.Namespace) -> int:
    problem = _agent_flag_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

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


def dispatch_tasks(args: argparse.Namespace) -> int:
    """Publish a suite as work, and print what was published.

    Nothing is measured here and no database is opened, so there is no lock to
    take: the contention this project guards against is two processes writing
    runs, and a dispatcher writes none.
    """
    problem = _agent_flag_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    problem = _repeats_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    try:
        tasks = load_tasks(args.directory)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE
    if not tasks:
        # The same rule as `bench`: dispatching nothing and exiting 0 is a
        # green command that sent no work.
        print(f"{PROGRAM}: no task files in {args.directory}", file=sys.stderr)
        return EXIT_USAGE

    clock = SystemClock()
    # Built and then read, not constructed to be used. The agent owns the
    # mapping from the flags to the identity a benchmark groups on, and
    # reading it here keeps that mapping in one place. Building costs nothing:
    # no agent opens a connection in its constructor.
    configuration = _configuration(_build_agent(args, clock), args.image)

    try:
        publisher = KafkaPublisher(TASK_RUNS_TOPIC, args.bootstrap)
        messages = dispatch(
            tasks,
            configuration,
            publisher,
            UuidIdGenerator(),
            clock,
            encode_message,
            repeats=args.repeats,
        )
    except (KafkaUnavailable, DispatchIncomplete) as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED

    # Data on stdout, progress on stderr, so redirecting the identifiers still
    # shows what happened. The identifiers are the useful output: they are what
    # `af show` takes once a worker has stored the run.
    for message in messages:
        print(f"{message.run_id}\t{message.task_id}")
    print(
        f"{PROGRAM}: dispatched {len(messages)} run(s) for {len(tasks)} task(s) "
        f"to {TASK_RUNS_TOPIC} at {args.bootstrap}",
        file=sys.stderr,
    )
    # Zero means every message reached the broker. It does not mean anything
    # was measured; no measurement exists until a worker stores one.
    return EXIT_MEASURED


def _configuration(agent: AgentPort, image: str) -> RunConfiguration:
    """The grouping key, read off a built agent.

    Shared by the dispatcher, which sends it, and the worker, which compares
    against it. Two readings of the same flags would be two places that know
    the mapping, and the first bug would be the two disagreeing.
    """
    return RunConfiguration(
        agent_id=agent.agent_id,
        provider_id=agent.provider_id,
        model_id=agent.model_id,
        prompt_id=agent.prompt_id,
        image=image,
    )


def _print_eviction(delivery: RawDelivery, committed: bool) -> None:
    if committed:
        return
    print(
        f"{PROGRAM}: the group evicted this consumer before it committed "
        f"partition {delivery.partition} offset {delivery.offset}; the message "
        f"was reassigned, and the collector keeps the first result it receives",
        file=sys.stderr,
    )


def _print_handled(handled: Handled) -> None:
    delivery = handled.delivery
    _print_eviction(delivery, handled.committed)
    if handled.record is None:
        # On stderr: there is no run to name, and the position in the log is
        # the only honest way to identify bytes nobody could read.
        print(
            f"{PROGRAM}: skipped an undecodable message at partition "
            f"{delivery.partition} offset {delivery.offset}: {handled.detail}",
            file=sys.stderr,
        )
        return
    _print_line(handled.record)
    if handled.handling is Handling.REPUBLISHED:
        print(
            f"{PROGRAM}: {handled.record.run.run_id} was already stored; "
            f"republished rather than measured again",
            file=sys.stderr,
        )


def run_worker(args: argparse.Namespace) -> int:
    """Measure messages from the work topic until a limit or the operator stops it.

    Exits 0 when it stops at a limit, 2 when a message asks for something this
    worker cannot honour -- that is how it was started, not a harness fault --
    and 1 when the broker or the database fails it.
    """
    problem = _agent_flag_problem(args) or _limit_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    try:
        tasks = load_tasks(args.directory)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE
    if not tasks:
        print(f"{PROGRAM}: no task files in {args.directory}", file=sys.stderr)
        return EXIT_USAGE

    clock = SystemClock()
    runner, docker = _build_runners(args, clock)
    agent = _build_agent(args, clock)
    worker_id = args.worker_id or f"{socket.gethostname()}-{os.getpid()}"

    try:
        # The lock is held for the worker's whole life, as `bench` holds it for
        # a suite: recovery assumes every unfinished run in this database is
        # abandoned, and the redelivery check assumes no one else writes it.
        with exclusive_run_lock(args.db), SqliteRunRepository(args.db) as repository:
            workspaces = WorkspaceFactory(args.runs)
            _recover(repository, workspaces, clock, docker.remove_container)
            service = _build_service(args, repository, workspaces, runner, clock, agent)
            consumer = KafkaConsumer(
                TASK_RUNS_TOPIC,
                WORKER_GROUP,
                args.bootstrap,
                max_poll_interval_ms=_poll_interval_ms(args),
            )
            try:
                worker = Worker(
                    consumer=consumer,
                    publisher=KafkaPublisher(RUN_RESULTS_TOPIC, args.bootstrap),
                    tasks={task.task_id: task for task in tasks},
                    configuration=_configuration(agent, args.image),
                    executor=service,
                    records=repository,
                    worker_id=worker_id,
                    decode=decode_message,
                    encode=encode_result,
                )
                print(
                    f"{PROGRAM}: {worker_id} waiting for work on {TASK_RUNS_TOPIC} "
                    f"at {args.bootstrap}",
                    file=sys.stderr,
                )
                handled = poll_until(
                    worker,
                    clock,
                    _print_handled,
                    max_messages=args.max_messages,
                    idle_exit_ms=_idle_exit_ms(args),
                )
            finally:
                # Leaving the group at once lets it rebalance now, instead of
                # holding this worker's partitions until the broker gives up
                # on it -- which matters most when a message was refused.
                consumer.close()
    except DatabaseBusy as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    except (KafkaUnavailable, ResultNotDelivered) as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    except IncompatibleMessage as error:
        print(
            f"{PROGRAM}: refusing a message this worker cannot honour, and "
            f"leaving it for one that can: {error}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    except KeyboardInterrupt:
        print(f"{PROGRAM}: aborted", file=sys.stderr)
        return EXIT_ABORTED

    print(f"{PROGRAM}: {worker_id} handled {handled} message(s)", file=sys.stderr)
    return EXIT_MEASURED


def _print_collected(collected: Collected) -> None:
    """A stored run on stdout, shaped like `af list`; anything else on stderr."""
    _print_eviction(collected.delivery, collected.committed)
    if collected.collection is Collection.STORED and collected.message is not None:
        _print_line(collected.message.record)
        return
    delivery = collected.delivery
    where = f"partition {delivery.partition} offset {delivery.offset}"
    if collected.collection is Collection.DUPLICATE and collected.message is not None:
        detail = f"{collected.message.record.run.run_id} already stored"
    else:
        detail = collected.detail
    print(
        f"{PROGRAM}: {collected.collection.value} at {where}: {detail}", file=sys.stderr
    )


def run_collector(args: argparse.Namespace) -> int:
    """Store results until a limit or the operator stops it.

    Exits 0 when it stops at a limit, whatever it collected: a duplicate or a
    refused result is reported, not a failure of the collector. Exits 2 when a
    result comes from a newer build, which is left on the topic for a collector
    that can store it, and 1 when the broker or the database fails it.
    """
    problem = _limit_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    counts: dict[Collection, int] = dict.fromkeys(Collection, 0)

    def report(collected: Collected) -> None:
        counts[collected.collection] += 1
        _print_collected(collected)

    try:
        # The same lock `bench` takes. The collector is this database's one
        # writer, and a `bench` against the same file at the same time would
        # make it two.
        with exclusive_run_lock(args.db), SqliteRunRepository(args.db) as repository:
            consumer = KafkaConsumer(RUN_RESULTS_TOPIC, COLLECTOR_GROUP, args.bootstrap)
            try:
                collector = Collector(
                    consumer=consumer, store=repository, decode=decode_result
                )
                print(
                    f"{PROGRAM}: collecting from {RUN_RESULTS_TOPIC} at "
                    f"{args.bootstrap} into {args.db}",
                    file=sys.stderr,
                )
                poll_until(
                    collector,
                    SystemClock(),
                    report,
                    max_messages=args.max_messages,
                    idle_exit_ms=_idle_exit_ms(args),
                )
            finally:
                consumer.close()
    except DatabaseBusy as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    except KafkaUnavailable as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    except IncompatibleMessage as error:
        print(
            f"{PROGRAM}: stopping, and leaving the result on the topic for a "
            f"newer collector: {error}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    except KeyboardInterrupt:
        print(f"{PROGRAM}: aborted", file=sys.stderr)
        return EXIT_ABORTED

    summary = ", ".join(f"{count} {kind.value}" for kind, count in counts.items())
    print(f"{PROGRAM}: collected {summary}", file=sys.stderr)
    return EXIT_MEASURED


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


def write_report(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"{PROGRAM}: no database at {args.db}", file=sys.stderr)
        return EXIT_USAGE

    with SqliteRunRepository(args.db) as repository:
        # Every run, not the listing default. A report whose denominator was
        # silently capped at 50 would look complete and be wrong.
        records = repository.list_runs(limit=None)

    if not records:
        # Never EXIT_MEASURED, for the reason `bench` gives: a report of
        # nothing that exits 0 is a green CI job asserting that zero tasks
        # passed.
        print(f"{PROGRAM}: no runs in {args.db}", file=sys.stderr)
        return EXIT_USAGE

    text = render(
        records,
        generated_at_unix_ms=SystemClock().now_unix_ms(),
        source=str(args.db),
    )

    pending = unfinished(records)
    if pending:
        print(
            f"{PROGRAM}: {len(pending)} run(s) had not finished and are "
            f"excluded; the next af run recovers them",
            file=sys.stderr,
        )

    if str(args.out) == "-":
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    return EXIT_MEASURED


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

    print(f"{PROGRAM}: unknown command {args.command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
