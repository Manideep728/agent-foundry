"""Turning parsed flags into the runners, agent and service a command uses."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from control_plane.adapters.docker_runner import DockerConfig, DockerRunner
from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.local_runner import LocalRunner
from control_plane.adapters.ollama_client import OllamaClient
from control_plane.adapters.openai_compat_client import OpenAICompatClient
from control_plane.adapters.prompts import (
    DEFAULT_SINGLE_SHOT_PROMPT,
    DEFAULT_TOOL_PROMPT,
    PromptVariant,
    variant,
)
from control_plane.adapters.single_shot_agent import SingleShotAgent
from control_plane.adapters.storage import open_repository, owner_key
from control_plane.adapters.system import (
    SystemClock,
    UuidIdGenerator,
    harness_version,
)
from control_plane.adapters.tool_use_agent import ToolUseAgent
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.ids import RunId
from control_plane.domain.model import ModelClient
from control_plane.domain.record import RunRecord
from control_plane.domain.task import TaskDefinition
from control_plane.interface.cli.common import (
    AGENT_OLLAMA,
    AGENT_SINGLE_SHOT,
    AGENT_TOOLS,
    PROGRAM,
    PROVIDER_OPENAI_COMPAT,
    RUNNER_DOCKER,
)
from control_plane.interface.cli.flags import ceiling, provider_name
from control_plane.services.dispatch import RunConfiguration
from control_plane.services.evaluate import EvaluationService
from control_plane.services.ports import AgentPort, RunRepository, ValidationRunner
from control_plane.services.recovery import recover_unfinished


def build_runners(
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
    docker = DockerRunner(DockerConfig(image=args.image, owner=owner_key(args.db)))
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


def recover(
    repository: RunRepository,
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


def build_service(
    args: argparse.Namespace,
    repository: RunRepository,
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


def recover_and_evaluate(
    args: argparse.Namespace,
    task: TaskDefinition,
    runner: ValidationRunner,
    docker: DockerRunner,
    clock: SystemClock,
) -> RunRecord:
    with open_repository(args.db, exclusive=True) as repository:
        workspaces = WorkspaceFactory(args.runs)
        recover(repository, workspaces, clock, docker.remove_container)
        service = build_service(
            args, repository, workspaces, runner, clock, build_agent(args, clock)
        )
        return service.execute(task)


def build_agent(args: argparse.Namespace, clock: SystemClock) -> AgentPort:
    """Pick the agent the flags asked for.

    `fake` is the default deliberately. It needs no server and no GPU, so the
    test suite, CI and any offline clone keep working exactly as before, and
    reaching a model is something a run has to ask for.
    """
    if args.agent in (AGENT_SINGLE_SHOT, AGENT_OLLAMA):
        return SingleShotAgent(
            _model_client(args),
            clock,
            prompt=_prompt(args, DEFAULT_SINGLE_SHOT_PROMPT),
        )
    if args.agent == AGENT_TOOLS:
        return ToolUseAgent(
            _model_client(args),
            clock,
            max_turns=args.max_turns,
            prompt=_prompt(args, DEFAULT_TOOL_PROMPT),
            run_ceiling=ceiling(args.max_run_micros),
        )
    return FakeAgent(args.solutions, clock)


def _model_client(args: argparse.Namespace) -> ModelClient:
    if args.provider == PROVIDER_OPENAI_COMPAT:
        # Read here, at the edge, and handed to the client only. It is never
        # printed, stored on a record, or put into an artifact.
        api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
        return OpenAICompatClient(
            args.model,
            base_url=args.base_url,
            api_key=api_key,
            provider_id=provider_name(args),
            timeout_s=args.ollama_timeout,
        )
    return OllamaClient(
        args.model, base_url=args.ollama_url, timeout_s=args.ollama_timeout
    )


def _prompt(args: argparse.Namespace, fallback: str) -> PromptVariant:
    return variant(args.prompt or fallback)


def configuration(agent: AgentPort, image: str) -> RunConfiguration:
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
