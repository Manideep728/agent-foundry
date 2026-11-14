"""How runs, suites and consumer events are printed. Formatting only."""

from __future__ import annotations

import sys

from control_plane.domain.record import RunRecord
from control_plane.domain.summary import Durations, SuiteSummary
from control_plane.interface.cli.common import PROGRAM
from control_plane.interface.report import describe
from control_plane.services.collect import Collected, Collection
from control_plane.services.dispatch import RawDelivery
from control_plane.services.worker import Handled, Handling


def print_record(record: RunRecord) -> None:
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


def print_line(record: RunRecord) -> None:
    """One line per task, shaped like `af list` so the two read the same.

    The run id is on screen because `af show <run-id>` is how a reader gets
    from a summary row to the detail behind it.
    """
    outcome = record.outcome.value if record.outcome else record.run.state.value
    print(f"{record.run.run_id}  {record.run.task_id!s:<24} {outcome}")


def _print_durations(label: str, durations: Durations | None) -> None:
    if durations is None:
        return
    print(
        f"  {label:<13} min {durations.fastest_ms}ms  "
        f"median {durations.median_ms}ms  max {durations.slowest_ms}ms"
    )


def print_summary(summary: SuiteSummary, *, planned: int) -> None:
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


def print_eviction(delivery: RawDelivery, committed: bool) -> None:
    if committed:
        return
    print(
        f"{PROGRAM}: the group evicted this consumer before it committed "
        f"partition {delivery.partition} offset {delivery.offset}; the message "
        f"was reassigned, and the collector keeps the first result it receives",
        file=sys.stderr,
    )


def print_handled(handled: Handled) -> None:
    delivery = handled.delivery
    print_eviction(delivery, handled.committed)
    if handled.record is None:
        # On stderr: there is no run to name, and the position in the log is
        # the only honest way to identify bytes nobody could read.
        print(
            f"{PROGRAM}: skipped an undecodable message at partition "
            f"{delivery.partition} offset {delivery.offset}: {handled.detail}",
            file=sys.stderr,
        )
        return
    print_line(handled.record)
    if handled.handling is Handling.REPUBLISHED:
        print(
            f"{PROGRAM}: {handled.record.run.run_id} was already stored; "
            f"republished rather than measured again",
            file=sys.stderr,
        )


def print_collected(collected: Collected) -> None:
    """A stored run on stdout, shaped like `af list`; anything else on stderr."""
    print_eviction(collected.delivery, collected.committed)
    if collected.collection is Collection.STORED and collected.message is not None:
        print_line(collected.message.record)
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
