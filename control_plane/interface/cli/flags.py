"""The flags the commands share, and the checks run on them before any work."""

from __future__ import annotations

import argparse
from pathlib import Path

from control_plane.adapters.kafka_bus import (
    DEFAULT_BOOTSTRAP,
    DEFAULT_MAX_POLL_INTERVAL_MS,
)
from control_plane.adapters.ollama_client import DEFAULT_BASE_URL, DEFAULT_TIMEOUT_S
from control_plane.adapters.prompts import (
    DEFAULT_SINGLE_SHOT_PROMPT,
    DEFAULT_TOOL_PROMPT,
    VARIANTS,
    variant,
)
from control_plane.adapters.s3_artifacts import parse_location
from control_plane.adapters.tool_use_agent import DEFAULT_MAX_TURNS
from control_plane.domain.budget import validate_ceiling
from control_plane.domain.errors import DomainError
from control_plane.domain.model import MicroUsd
from control_plane.interface.cli.common import (
    AGENT_FAKE,
    AGENT_OLLAMA,
    AGENT_TOOLS,
    DEFAULT_DB,
    DEFAULT_FIXTURES,
    DEFAULT_IMAGE,
    DEFAULT_MODEL,
    DEFAULT_RUNS,
    DEFAULT_SOLUTIONS,
    RUNNER_DOCKER,
    RUNNER_LOCAL,
)


def add_agent_flags(parser: argparse.ArgumentParser) -> None:
    """The flags that say *what* to measure, rather than where to put it.

    Separate from `add_run_flags` because `dispatch` needs exactly these and
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


def add_run_flags(parser: argparse.ArgumentParser) -> None:
    """The flags any evaluation needs, shared by `run`, `bench` and `worker`.

    A function rather than argparse's `parents=`, because a shared parent
    parser puts the same Action objects into two parsers -- a subtlety nobody
    reading this should have to know about. What matters is that the
    subcommands cannot drift apart: a flag added to one is added to all.
    """
    add_db_flag(parser)
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
    add_agent_flags(parser)


def add_db_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"the database of runs (default {DEFAULT_DB})",
    )


def add_bus_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bootstrap",
        default=DEFAULT_BOOTSTRAP,
        help=f"the Kafka broker (default {DEFAULT_BOOTSTRAP})",
    )


def add_limit_flags(parser: argparse.ArgumentParser) -> None:
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


def add_repeats_flag(parser: argparse.ArgumentParser) -> None:
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


def repeats_problem(args: argparse.Namespace) -> str | None:
    if args.repeats < 1:
        return f"--repeats must be at least 1, got {args.repeats}"
    return None


def artifacts_problem(location: str | None) -> str | None:
    """A malformed location is refused before the worker joins its group."""
    if location is None:
        return None
    try:
        parse_location(location)
    except ValueError as error:
        return f"--artifacts: {error}"
    return None


def limit_problem(args: argparse.Namespace) -> str | None:
    """Zero would be a consumer that joins its group and does nothing."""
    for flag, value in (
        ("--max-messages", args.max_messages),
        ("--idle-exit-s", args.idle_exit_s),
    ):
        if value is not None and value < 1:
            return f"{flag} must be at least 1, got {value}"
    return None


def ceiling(value: int | None) -> MicroUsd | None:
    return None if value is None else MicroUsd(value)


def agent_flag_problem(args: argparse.Namespace) -> str | None:
    """Why the agent flags cannot be honoured, if they cannot.

    Checked before a run is created, so a typo costs nothing. Resolved inside
    the agent instead, it would create a row and spend tokens before failing,
    and leave an unfinished run behind for the recovery scan.
    """
    if args.ollama_timeout < 1:
        return f"--ollama-timeout must be at least 1, got {args.ollama_timeout}"
    try:
        validate_ceiling(ceiling(args.max_run_micros), "--max-run-micros")
        validate_ceiling(
            ceiling(getattr(args, "max_bench_micros", None)), "--max-bench-micros"
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


def idle_exit_ms(args: argparse.Namespace) -> int | None:
    return None if args.idle_exit_s is None else args.idle_exit_s * 1000


def poll_interval_ms(args: argparse.Namespace) -> int:
    """How long a worker may hold one message before its group evicts it (D32).

    D29 set fifteen minutes as three times the longest handling measured, and
    found that the longest is set by the model client's timeout, not by the
    task. So the interval follows the timeout at the same ratio, and never
    drops below the measured fifteen minutes.
    """
    timeout_ms: int = args.ollama_timeout * 1000
    return max(DEFAULT_MAX_POLL_INTERVAL_MS, 3 * timeout_ms)
