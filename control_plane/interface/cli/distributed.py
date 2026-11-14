"""The Kafka commands: `dispatch`, `worker` and `collect` (D27)."""

from __future__ import annotations

import argparse
import os
import socket
import sys

from control_plane.adapters.dispatch_json import decode as decode_message
from control_plane.adapters.dispatch_json import encode as encode_message
from control_plane.adapters.kafka_bus import (
    KafkaConsumer,
    KafkaPublisher,
    KafkaUnavailable,
)
from control_plane.adapters.record_json import decode as decode_result
from control_plane.adapters.record_json import encode as encode_result
from control_plane.adapters.run_lock import DatabaseBusy
from control_plane.adapters.s3_artifacts import S3ArtifactStore
from control_plane.adapters.storage import display_name, open_repository
from control_plane.adapters.system import SystemClock, UuidIdGenerator
from control_plane.adapters.task_toml import load_tasks
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.errors import DomainError
from control_plane.interface.cli.common import (
    EXIT_ABORTED,
    EXIT_HARNESS_FAILED,
    EXIT_MEASURED,
    EXIT_USAGE,
    PROGRAM,
)
from control_plane.interface.cli.flags import (
    agent_flag_problem,
    artifacts_problem,
    idle_exit_ms,
    limit_problem,
    oss_problem,
    poll_interval_ms,
    repeats_problem,
)
from control_plane.interface.cli.printing import print_collected, print_handled
from control_plane.interface.cli.wiring import (
    build_agent,
    build_runners,
    build_service,
    configuration,
    recover,
)
from control_plane.services.collect import Collected, Collection, Collector
from control_plane.services.dispatch import (
    COLLECTOR_GROUP,
    RUN_RESULTS_TOPIC,
    TASK_RUNS_TOPIC,
    WORKER_GROUP,
    DispatchIncomplete,
    IncompatibleMessage,
    dispatch,
)
from control_plane.services.polling import poll_until
from control_plane.services.ports import ArtifactsNotStored, ArtifactStore
from control_plane.services.worker import ResultNotDelivered, Worker


def dispatch_tasks(args: argparse.Namespace) -> int:
    """Publish a suite as work, and print what was published.

    Nothing is measured here and no database is opened, so there is no lock to
    take: the contention this project guards against is two processes writing
    runs, and a dispatcher writes none.
    """
    problem = agent_flag_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    problem = repeats_problem(args)
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
    problem = oss_problem(args, tasks)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    clock = SystemClock()
    # Built and then read, not constructed to be used. The agent owns the
    # mapping from the flags to the identity a benchmark groups on, and
    # reading it here keeps that mapping in one place. Building costs nothing:
    # no agent opens a connection in its constructor.
    run_configuration = configuration(build_agent(args, clock), args.image)

    try:
        publisher = KafkaPublisher(TASK_RUNS_TOPIC, args.bootstrap)
        messages = dispatch(
            tasks,
            run_configuration,
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


def run_worker(args: argparse.Namespace) -> int:
    """Measure messages from the work topic until a limit or the operator stops it.

    Exits 0 when it stops at a limit, 2 when a message asks for something this
    worker cannot honour -- that is how it was started, not a harness fault --
    and 1 when the broker or the database fails it.
    """
    problem = (
        agent_flag_problem(args)
        or limit_problem(args)
        or artifacts_problem(args.artifacts)
    )
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
    problem = oss_problem(args, tasks)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    clock = SystemClock()
    runner, docker = build_runners(args, clock)
    agent = build_agent(args, clock)
    worker_id = args.worker_id or f"{socket.gethostname()}-{os.getpid()}"
    artifacts: ArtifactStore | None = (
        None if args.artifacts is None else S3ArtifactStore(args.artifacts)
    )

    try:
        # The lock is held for the worker's whole life, as `bench` holds it for
        # a suite: recovery assumes every unfinished run in this database is
        # abandoned, and the redelivery check assumes no one else writes it.
        with open_repository(args.db, exclusive=True) as repository:
            workspaces = WorkspaceFactory(args.runs)
            recover(repository, workspaces, clock, docker.remove_container)
            service = build_service(args, repository, workspaces, runner, clock, agent)
            consumer = KafkaConsumer(
                TASK_RUNS_TOPIC,
                WORKER_GROUP,
                args.bootstrap,
                max_poll_interval_ms=poll_interval_ms(args),
            )
            try:
                worker = Worker(
                    consumer=consumer,
                    publisher=KafkaPublisher(RUN_RESULTS_TOPIC, args.bootstrap),
                    tasks={task.task_id: task for task in tasks},
                    configuration=configuration(agent, args.image),
                    executor=service,
                    records=repository,
                    worker_id=worker_id,
                    decode=decode_message,
                    encode=encode_result,
                    artifacts=artifacts,
                )
                print(
                    f"{PROGRAM}: {worker_id} waiting for work on {TASK_RUNS_TOPIC} "
                    f"at {args.bootstrap}",
                    file=sys.stderr,
                )
                handled = poll_until(
                    worker,
                    clock,
                    print_handled,
                    max_messages=args.max_messages,
                    idle_exit_ms=idle_exit_ms(args),
                )
            finally:
                # Leaving the group at once lets it rebalance now, instead of
                # holding this worker's partitions until the broker gives up
                # on it -- which matters most when a message was refused.
                consumer.close()
    except DatabaseBusy as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    except (KafkaUnavailable, ResultNotDelivered, ArtifactsNotStored) as error:
        # The message is not committed, so it comes back; the run is found
        # stored and is uploaded and published then, not measured again.
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


def run_collector(args: argparse.Namespace) -> int:
    """Store results until a limit or the operator stops it.

    Exits 0 when it stops at a limit, whatever it collected: a duplicate or a
    refused result is reported, not a failure of the collector. Exits 2 when a
    result comes from a newer build, which is left on the topic for a collector
    that can store it, and 1 when the broker or the database fails it.
    """
    problem = limit_problem(args)
    if problem is not None:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return EXIT_USAGE

    counts: dict[Collection, int] = dict.fromkeys(Collection, 0)

    def report(collected: Collected) -> None:
        counts[collected.collection] += 1
        print_collected(collected)

    try:
        # The same lock `bench` takes. The collector is this database's one
        # writer, and a `bench` against the same file at the same time would
        # make it two.
        with open_repository(args.db, exclusive=True) as repository:
            consumer = KafkaConsumer(RUN_RESULTS_TOPIC, COLLECTOR_GROUP, args.bootstrap)
            try:
                collector = Collector(
                    consumer=consumer, store=repository, decode=decode_result
                )
                print(
                    f"{PROGRAM}: collecting from {RUN_RESULTS_TOPIC} at "
                    f"{args.bootstrap} into {display_name(args.db)}",
                    file=sys.stderr,
                )
                poll_until(
                    collector,
                    SystemClock(),
                    report,
                    max_messages=args.max_messages,
                    idle_exit_ms=idle_exit_ms(args),
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
