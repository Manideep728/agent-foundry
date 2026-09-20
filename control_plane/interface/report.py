"""The benchmark report, as Markdown.

Acceptance criterion 9: a report includes successes, failures, regressions,
cost and latency. This renders one; it computes nothing. Every figure comes
from `domain.summary`, which is where the exclusion rules live and where the
float guard reaches.

Markdown rather than HTML or JSON, for one reason each. HTML would need a
viewer and would not diff. JSON is what the database already is -- a second
serialisation of the same rows, with the aggregation still left to whoever
reads it. Markdown renders in the place this report is actually read, and two
reports over the same database differ only where the measurements differ, so a
regression between two benchmark runs is a diff rather than a reading exercise.

The report is grouped exactly as the summary is, by suite and configuration,
because a rendering that could regroup is a rendering that could blend -- and
it would blend in the surface furthest from the rules saying it must not.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from control_plane.domain.record import RunRecord
from control_plane.domain.states import RunState
from control_plane.domain.summary import (
    Configuration,
    Durations,
    SuiteSummary,
    configuration_of,
    summarise,
)

NO_RUNS = "no runs recorded"


def describe(configuration: Configuration) -> str:
    """One line naming a configuration, strategy first.

    The strategy leads because it is what two rows of the same model differ by,
    and the difference a reader most often came to compare.
    """
    if configuration.agent_id is not None:
        return (
            f"{configuration.agent_id}/"
            f"{configuration.provider_id}/{configuration.model_id}"
        )
    if configuration.provider_id is not None:
        # Stored before the harness recorded the strategy. The provider and the
        # model survive on the invocations, and printing them beats discarding
        # them for the one part nobody wrote down.
        return f"?/{configuration.provider_id}/{configuration.model_id}"
    return "unrecorded"


def _iso(unix_ms: int) -> str:
    """UTC, to the second. A report is not timed to the millisecond.

    Integer division, so this file holds no float even though it is outside
    the directory the guard scans. The rule is about measurements, and a
    timestamp that acquired a rounding error on its way to being printed
    would be the least excusable place to find one.
    """
    moment = dt.datetime.fromtimestamp(unix_ms // 1000, tz=dt.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _range(durations: Durations | None) -> str:
    """min/median/max, never a mean.

    A mean is a duration no run had, and over eight tasks it is one slow run
    away from meaningless. Every figure here is traceable to a run.
    """
    if durations is None:
        return "--"
    return f"{durations.fastest_ms} / {durations.median_ms} / {durations.slowest_ms}"


def _runs_for(records: Sequence[RunRecord], summary: SuiteSummary) -> list[RunRecord]:
    """The records one summary covers, keyed exactly as the summary grouped.

    `configuration_of` rather than a second rule of its own. A renderer free to
    group differently is a renderer that will eventually put a run under a
    heading that did not count it.
    """
    selected = [
        record
        for record in records
        if record.suite is summary.suite
        and configuration_of(record) == summary.configuration
    ]
    # Fixed order, so two reports over the same database diff cleanly.
    return sorted(selected, key=lambda r: (str(r.run.task_id), str(r.run.run_id)))


def _environment(records: Sequence[RunRecord]) -> list[str]:
    """What validated these runs.

    A benchmark figure nobody can attribute to a machine is not reproducible,
    which is the reason these columns exist at all.
    """
    architectures = sorted({r.arch for r in records})
    digests = sorted({r.image_digest for r in records if r.image_digest})
    isolated = sorted({str(r.pid1) for r in records})
    lines = [
        f"- Architecture: {', '.join(architectures)}",
        f"- Supervisor was PID 1: {', '.join(isolated)}",
    ]
    if digests:
        lines.extend(f"- Image: `{digest}`" for digest in digests)
    else:
        # Never silently absent. A run with no image digest is a run nothing
        # can pin to the bytes that produced it, and saying so is the point.
        lines.append("- Image: none recorded")
    return lines


def _outcome_of(record: RunRecord) -> str:
    if record.outcome is not None:
        return record.outcome.value
    if record.run.failure is not None:
        return f"{record.run.state.value} ({record.run.failure.kind.value})"
    return record.run.state.value


def _run_row(record: RunRecord) -> str:
    score = record.score
    resolved = "--" if score is None else ("yes" if score.resolved else "no")
    regressed = "--" if score is None else ("yes" if score.regressed else "no")
    f2p = (
        "--"
        if score is None
        else f"{score.fail_to_pass_passed}/{score.fail_to_pass_total}"
    )
    p2p = (
        "--"
        if score is None
        else f"{score.pass_to_pass_passed}/{score.pass_to_pass_total}"
    )
    agent = "--" if record.agent_ms is None else str(record.agent_ms)
    validation = "--" if record.validation_ms is None else str(record.validation_ms)
    return (
        f"| {record.run.task_id} | {_outcome_of(record)} | {resolved} | {regressed} "
        f"| {f2p} | {p2p} | {agent} | {validation} "
        f"| {int(record.total_cost_micros)} |"
    )


def _section(summary: SuiteSummary, records: Sequence[RunRecord]) -> list[str]:
    covered = _runs_for(records, summary)
    lines = [
        f"## {summary.suite.value} -- {describe(summary.configuration)}",
        "",
        f"{summary.measured} measured of {summary.runs} attempted.",
        "",
        "| measure | value |",
        "|---|---|",
        f"| resolved | {summary.resolved}/{summary.measured} |",
        f"| regressed | {summary.regressed}/{summary.measured} |",
    ]
    for outcome, count in summary.outcomes.items():
        lines.append(f"| {outcome.value} | {count} |")
    lines.extend(
        [
            f"| cost, micro-USD | {int(summary.measured_cost_micros)} |",
            f"| agent ms, min/median/max | {_range(summary.agent_ms)} |",
            f"| validation ms, min/median/max | {_range(summary.validation_ms)} |",
        ]
    )
    if summary.without_regression_check:
        # Otherwise "regressed 1/7" invites the reader to conclude that seven
        # tasks were checked, when some had no pass-to-pass command to check.
        lines.append(
            f"| measured runs with nothing to regress "
            f"| {summary.without_regression_check} |"
        )
    if summary.runs_with_timeouts:
        lines.append(
            f"| timed-out commands "
            f"| {summary.timed_out_commands} in {summary.runs_with_timeouts} run(s) |"
        )
    lines.append("")
    lines.extend(_environment(covered))
    lines.append("")

    lines.extend(
        [
            "| task | outcome | resolved | regressed | fail->pass | pass->pass "
            "| agent ms | validation ms | cost |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    lines.extend(_run_row(record) for record in covered)
    lines.append("")

    if summary.aborted:
        lines.extend([f"{summary.aborted} run(s) aborted by the operator.", ""])

    if summary.unmeasured:
        # A failure list, not a failure count. A harness-failure rate nobody
        # can trace to a task and a reason cannot be acted on.
        lines.extend(
            [
                f"### Produced no measurement ({len(summary.unmeasured)})",
                "",
                "| task | failure | from state | detail | run |",
                "|---|---|---|---|---|",
            ]
        )
        lines.extend(
            f"| {entry.task_id} | {entry.kind.value} | {entry.failed_from.value} "
            f"| {entry.detail or '--'} | `{entry.run_id}` |"
            for entry in summary.unmeasured
        )
        lines.append("")
        if summary.unmeasured_runs_that_spent:
            # Reported, never added in. A failed run's spend is real money and
            # is invisible any other way, but it is not part of what the
            # benchmark cost to measure.
            lines.extend(
                [
                    f"{int(summary.unmeasured_cost_micros)} micro-USD was spent by "
                    f"{summary.unmeasured_runs_that_spent} run(s) that measured "
                    "nothing. It is excluded from the cost above.",
                    "",
                ]
            )
    return lines


def render(
    records: Sequence[RunRecord], *, generated_at_unix_ms: int, source: str
) -> str:
    """The whole report, as one Markdown string.

    Pure: the same records and the same timestamp render the same bytes, which
    is what lets a test assert on the text and a reader diff two reports.
    """
    # Terminal runs only. A run still in flight is in none of the summary's
    # three buckets, and `SuiteSummary` refuses a set of runs its counts do
    # not account for -- correctly, because a rate whose denominator includes
    # a run with no result yet is a rate that changes while nobody looks.
    pending = unfinished(records)
    settled = [record for record in records if record not in pending]

    summaries = summarise(settled)
    measured = sum(s.measured for s in summaries)
    lines = [
        "# AgentFoundry benchmark report",
        "",
        f"Generated {_iso(generated_at_unix_ms)} from `{source}`.",
        "",
        f"{len(settled)} run(s) recorded, {measured} of them measured, "
        f"across {len(summaries)} configuration(s).",
        "",
        "Suites and configurations are reported separately and never averaged "
        "together. Counts are `passed/total` and durations are ranges, because "
        "every figure here is one some run actually had.",
        "",
    ]
    if pending:
        # Named, not dropped in silence. These are runs whose process died
        # without recording an end; the next `af run` recovers them. A report
        # that omitted them would show a smaller denominator each time one
        # happened, and look healthier for it.
        lines.extend(
            [
                f"{len(pending)} run(s) had not finished when this was "
                "generated and are excluded from every figure below: "
                + ", ".join(f"`{r.run.run_id}` ({r.run.state.value})" for r in pending)
                + ".",
                "",
            ]
        )
    for summary in summaries:
        lines.extend(_section(summary, settled))
    # Exactly one trailing newline, so the file ends the way every other text
    # file in the repository does and a diff shows no phantom last line.
    return "\n".join(lines).rstrip("\n") + "\n"


def unfinished(records: Sequence[RunRecord]) -> tuple[RunRecord, ...]:
    """Runs that are neither measured nor terminally failed.

    A report covering a database that something is still writing to would
    describe a moment that never existed. These are named rather than counted
    so the reader can go and look.
    """
    terminal = (RunState.SCORED, RunState.FAILED, RunState.ABORTED)
    return tuple(r for r in records if r.run.state not in terminal)
