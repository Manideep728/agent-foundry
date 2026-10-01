"""Aggregating runs into a suite verdict.

Every test here is about an exclusion rule. The arithmetic is trivial; what is
not trivial is which runs are allowed to contribute to which number, and every
way of getting that wrong produces a plausible figure rather than a crash.
"""

from __future__ import annotations

import dataclasses

from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.model import MicroUsd, ModelInvocation, ModelUsage
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.scoring import Score
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.summary import Configuration, spread, summarise
from control_plane.domain.task import Suite
from control_plane.domain.validation import (
    SCHEMA_VERSION,
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)
from tests.fakes import EPOCH_MS

STREAM = StreamCapture(
    bytes_total=3,
    bytes_written=3,
    truncated=False,
    path="cmd-000.stdout.log",
    tail="ok",
)


def a_report() -> ValidationReport:
    return ValidationReport(
        schema_version=SCHEMA_VERSION,
        runner_version="stub 0.1.0",
        arch="x86_64",
        pid1=True,
        shell="/bin/sh",
        workdir="/work",
        started_at_unix_ms=EPOCH_MS,
        wall_ms=10,
        exit_reason=ExitReason.COMPLETED,
        aborted=False,
        orphans_reaped_total=0,
        commands=(
            CommandResult(
                index=0,
                command="f2p",
                started_at_unix_ms=EPOCH_MS,
                duration_ms=5,
                outcome=CommandOutcome.EXITED,
                stdout=STREAM,
                stderr=STREAM,
                exit_code=0,
            ),
        ),
    )


def a_score(*, resolved: bool = True, p2p_total: int = 1) -> Score:
    return Score(
        resolved=resolved,
        regressed=False,
        fail_to_pass_passed=1 if resolved else 0,
        fail_to_pass_total=1,
        pass_to_pass_passed=p2p_total,
        pass_to_pass_total=p2p_total,
        timed_out_commands=0,
    )


AGENT = "single-shot"
PROVIDER = "ollama"
MODEL = "qwen2.5-coder:7b"


def an_invocation(cost: int = 0, model: str = MODEL) -> ModelInvocation:
    return ModelInvocation(
        provider_id=PROVIDER,
        model_id=model,
        usage=ModelUsage(input_tokens=10, output_tokens=5),
        cost=MicroUsd(cost),
        latency_ms=1,
    )


def _run(run_id: str, state: RunState) -> EvaluationRun:
    run = EvaluationRun.create(RunId(run_id), TaskId("t"), EPOCH_MS)
    if state is RunState.SCORED:
        for nxt in (RunState.PREPARING, RunState.AGENT_RUNNING, RunState.VALIDATING):
            run = run.transition_to(nxt, EPOCH_MS + 1)
        return run.transition_to(RunState.SCORED, EPOCH_MS + 2)
    if state is RunState.ABORTED:
        return run.abort(EPOCH_MS + 1)
    return run.fail(FailureCategory.INFRASTRUCTURE_FAILED, EPOCH_MS + 1, "docker died")


def scored(
    run_id: str = "run-0001",
    *,
    suite: Suite = Suite.SYNTHETIC,
    outcome: AgentOutcome = AgentOutcome.CLEAN_PATCH,
    resolved: bool = True,
    agent_ms: int = 100,
    validation_ms: int = 50,
    cost: int = 0,
    p2p_total: int = 1,
    agent: str | None = AGENT,
    model: str = MODEL,
    prompt_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        run=_run(run_id, RunState.SCORED),
        suite=suite,
        fixture_sha256="a" * 64,
        runner_id="docker",
        runner_version="stub 0",
        arch="x86_64",
        pid1=True,
        report=a_report(),
        score=a_score(resolved=resolved, p2p_total=p2p_total),
        outcome=outcome,
        patch_is_empty=not resolved and outcome is AgentOutcome.NO_PATCH_PRODUCED,
        agent_id=agent,
        provider_id=PROVIDER if agent else None,
        model_id=model if agent else None,
        prompt_id=prompt_id,
        invocations=(an_invocation(cost, model),),
        total_cost_micros=MicroUsd(cost),
        agent_ms=agent_ms,
        validation_ms=validation_ms,
    )


def failed(
    run_id: str = "run-0002", *, cost: int = 0, agent: str | None = AGENT
) -> RunRecord:
    return RunRecord(
        run=_run(run_id, RunState.FAILED),
        suite=Suite.SYNTHETIC,
        fixture_sha256="a" * 64,
        runner_id="docker",
        runner_version="stub 0",
        arch="unknown",
        pid1=False,
        agent_id=agent,
        provider_id=PROVIDER if agent else None,
        model_id=MODEL if agent else None,
        invocations=(an_invocation(cost),),
        total_cost_micros=MicroUsd(cost),
        agent_ms=9_999,
    )


def aborted(run_id: str = "run-0003", *, agent: str | None = AGENT) -> RunRecord:
    return RunRecord(
        run=_run(run_id, RunState.ABORTED),
        suite=Suite.SYNTHETIC,
        fixture_sha256="a" * 64,
        runner_id="docker",
        runner_version="stub 0",
        arch="unknown",
        pid1=False,
        agent_id=agent,
        provider_id=PROVIDER if agent else None,
        model_id=MODEL if agent else None,
    )


# --- the exclusion rules -----------------------------------------------------


def test_a_failed_run_contributes_to_no_rate_no_cost_and_no_latency() -> None:
    """FailureCategory says so in as many words, and the obvious sum breaks it.

    Totalling every record would fold a Docker outage into the agent's score,
    its cost and its latency at once -- the failure that "depresses every
    number in the benchmark while the report still looks plausible".
    """
    (summary,) = summarise([scored(agent_ms=100), failed(cost=500)])

    assert summary.runs == 2
    assert summary.measured == 1
    assert summary.resolved == 1
    assert summary.measured_cost_micros == 0
    assert summary.agent_ms is not None
    assert summary.agent_ms.counted == 1
    assert summary.agent_ms.slowest_ms == 100  # not the failed run's 9999


def test_money_spent_by_a_run_that_measured_nothing_is_reported_separately() -> None:
    # Both halves: not folded into the headline total, and not dropped either.
    # The price of harness unreliability is worth knowing.
    (summary,) = summarise([scored(cost=20), failed(cost=500)])

    assert summary.measured_cost_micros == 20
    assert summary.unmeasured_cost_micros == 500
    assert summary.unmeasured_runs_that_spent == 1


def test_an_aborted_run_is_neither_measured_nor_a_harness_failure() -> None:
    # ABORTED exists so an operator pressing ^C does not show up as the harness
    # being unreliable.
    (summary,) = summarise([scored(), aborted()])

    assert summary.measured == 1
    assert summary.aborted == 1
    assert summary.unmeasured == ()


def test_every_agent_outcome_is_a_key_even_when_it_never_occurred() -> None:
    """A rate nobody can see is zero is a rate nobody acts on.

    The malformed-output rate reading zero for a bad reason is the failure the
    agent pipeline already carries a long comment about. A row that is absent
    when the count is zero has exactly that shape.
    """
    (summary,) = summarise([scored()])

    assert set(summary.outcomes) == set(AgentOutcome)
    assert summary.outcomes[AgentOutcome.MALFORMED_OUTPUT] == 0
    assert summary.outcomes[AgentOutcome.CLEAN_PATCH] == 1


def test_the_outcome_counts_add_up_to_the_measured_runs() -> None:
    summary = summarise([scored("run-0001"), scored("run-0002")])[0]

    assert sum(summary.outcomes.values()) == summary.measured


def test_two_suites_are_summarised_separately_and_never_added_together() -> None:
    # D7. A single blended figure for a hand-written fixture and a real
    # repository is not a measurement of anything.
    summaries = summarise(
        [scored("run-0001", suite=Suite.SYNTHETIC), scored("run-0002", suite=Suite.OSS)]
    )

    assert [s.suite for s in summaries] == [Suite.SYNTHETIC, Suite.OSS]
    assert all(s.runs == 1 for s in summaries)


def test_a_suite_with_no_runs_at_all_is_not_reported() -> None:
    assert summarise([]) == ()


# --- the numbers themselves --------------------------------------------------


def test_the_median_is_a_duration_some_run_actually_had() -> None:
    # Catches someone "improving" this to (a + b) // 2, which reports a
    # duration no run produced.
    values = [10, 20, 30, 40]

    result = spread(values)

    assert result is not None
    assert result.median_ms in values
    assert (result.fastest_ms, result.slowest_ms) == (10, 40)


def test_a_suite_with_no_measured_runs_reports_no_latency_rather_than_zero() -> None:
    # min(()) raises, and 0ms would read as a duration that was observed.
    (summary,) = summarise([failed()])

    assert summary.measured == 0
    assert summary.agent_ms is None
    assert summary.validation_ms is None


def test_a_run_with_no_pass_to_pass_command_is_counted_as_unprotected() -> None:
    # Without this, "regressed 0/2" invites the reader to conclude both tasks
    # were checked for regressions when one could not detect one.
    (summary,) = summarise(
        [scored("run-0001", p2p_total=1), scored("run-0002", p2p_total=0)]
    )

    assert summary.without_regression_check == 1


def test_a_failed_run_is_listed_with_its_category_and_detail() -> None:
    (summary,) = summarise([failed()])

    assert len(summary.unmeasured) == 1
    entry = summary.unmeasured[0]
    assert entry.kind is FailureCategory.INFRASTRUCTURE_FAILED
    assert entry.detail == "docker died"
    assert entry.run_id == "run-0002"


def test_a_summary_names_the_configuration_it_covers() -> None:
    # D6: a report states which implementation produced each run.
    (summary,) = summarise([scored()])

    assert summary.configuration == Configuration(AGENT, PROVIDER, MODEL)


def test_two_agents_driving_one_model_are_summarised_apart() -> None:
    """The comparison the suite exists to make, and the one a blend destroys.

    The single-shot agent and the tool loop share a provider and a model, so a
    key of `(provider, model)` merges them. Measured on this project: the same
    7B model resolves 5 of 8 tasks single-shot and 0 of 8 through the loop.
    Blended that reads `resolved 5/16`, which is true of neither and hides the
    finding completely.
    """
    runs = [
        scored("run-0001", agent="single-shot", resolved=True),
        scored(
            "run-0002",
            agent="tools",
            resolved=False,
            outcome=AgentOutcome.NO_PATCH_PRODUCED,
        ),
    ]

    single, tools = summarise(runs)

    assert single.configuration.agent_id == "single-shot"
    assert single.resolved == 1
    assert tools.configuration.agent_id == "tools"
    assert tools.resolved == 0


def test_one_agent_against_two_models_is_summarised_apart() -> None:
    # The other half of the key. 3b and 7b are different measurements.
    runs = [
        scored("run-0001", model="qwen2.5-coder:3b"),
        scored("run-0002", model="qwen2.5-coder:7b"),
    ]

    small, large = summarise(runs)

    assert small.configuration.model_id == "qwen2.5-coder:3b"
    assert large.configuration.model_id == "qwen2.5-coder:7b"
    assert (small.measured, large.measured) == (1, 1)


def test_summaries_come_back_in_a_fixed_order() -> None:
    # A report whose sections move between two runs cannot be diffed.
    runs = [scored("run-0001", agent="tools"), scored("run-0002", agent="fake")]

    forwards = [s.configuration.sort_key for s in summarise(runs)]
    backwards = [s.configuration.sort_key for s in summarise(list(reversed(runs)))]

    assert forwards == backwards == sorted(forwards)


def test_a_run_stored_before_the_columns_existed_keeps_what_answered() -> None:
    """The migration leaves the columns NULL, and the invocations survive.

    Reporting those runs as "unrecorded" would throw away a provider and a
    model the database still holds. Only the strategy was never written down.
    """
    (summary,) = summarise([scored(agent=None)])

    assert summary.configuration == Configuration(None, PROVIDER, MODEL)
    assert not summary.configuration.is_named


def test_an_unnamed_configuration_does_not_join_a_named_one() -> None:
    # "We do not know what produced these" is not evidence for any particular
    # answer, so folding it into the one named row would corrupt that row.
    summaries = summarise([aborted("run-0001", agent=None), scored("run-0002")])

    assert len(summaries) == 2
    assert summaries[0].configuration == Configuration(None, None, None)


def test_two_prompts_of_one_model_summarise_apart() -> None:
    """The reason the prompt is in the key at all.

    Measured on 2026-09-20: `llama3.2:3b` writes nothing under `tools-v1` and
    produces patches under `tools-v2`. Merged into one row those are one rate
    describing neither, which is the blended-summary defect with the agent
    swapped for the prompt.
    """
    summaries = summarise(
        [
            scored(
                "run-0001",
                prompt_id="tools-v1",
                resolved=False,
                outcome=AgentOutcome.NO_PATCH_PRODUCED,
            ),
            scored("run-0002", prompt_id="tools-v2", resolved=True),
        ]
    )

    assert len(summaries) == 2
    assert [s.configuration.prompt_id for s in summaries] == ["tools-v1", "tools-v2"]
    assert [s.resolved for s in summaries] == [0, 1]


def test_a_run_with_no_prompt_groups_apart_from_one_with_a_prompt() -> None:
    # "This agent reads no instructions" is a different statement from any
    # particular prompt, so it may not join one.
    summaries = summarise([scored(prompt_id=None), scored(prompt_id="tools-v2")])

    assert len(summaries) == 2
    assert summaries[0].configuration.prompt_id is None


# --- agreement across repeats ------------------------------------------------


def on_task(
    record: RunRecord,
    task: str,
    *,
    patch: str | None = "p" * 64,
    tokens: tuple[tuple[int, int], ...] = ((10, 5),),
) -> RunRecord:
    """The same run, moved to a task, with a chosen patch and per-call tokens."""
    calls = tuple(
        dataclasses.replace(
            an_invocation(),
            usage=ModelUsage(input_tokens=sent, output_tokens=received),
        )
        for sent, received in tokens
    )
    return dataclasses.replace(
        record,
        run=dataclasses.replace(record.run, task_id=TaskId(task)),
        patch_sha256=patch if record.run.state is RunState.SCORED else None,
        invocations=calls,
    )


def spread_of(*records: RunRecord) -> tuple[bool, int, int]:
    (summary,) = summarise(records)
    return summary.repeated, summary.tasks_agreeing, len(summary.tasks)


def test_identical_repeats_of_a_task_agree() -> None:
    runs = [on_task(scored(f"run-{n}"), "csv") for n in range(3)]

    (summary,) = summarise(runs)

    (task,) = summary.tasks
    assert (task.task_id, task.runs, task.resolved) == ("csv", 3, 3)
    assert task.agrees
    assert spread_of(*runs) == (True, 1, 1)


def test_two_patches_for_one_task_disagree_even_with_one_outcome() -> None:
    """Both resolved, by different edits: the outcome agreed and the run did not."""
    first = on_task(scored("run-1"), "csv", patch="a" * 64)
    second = on_task(scored("run-2"), "csv", patch="b" * 64)

    (summary,) = summarise([first, second])

    assert summary.tasks[0].patches == 2
    assert not summary.tasks[0].agrees


def test_a_different_token_count_on_one_call_is_a_disagreement() -> None:
    """The tell D31 was found by: same outcome and patch, and one turn read a
    different number of tokens because an earlier reply differed."""
    first = on_task(scored("run-1"), "csv", tokens=((10, 5), (40, 7)))
    second = on_task(scored("run-2"), "csv", tokens=((10, 5), (41, 7)))

    (summary,) = summarise([first, second])

    assert summary.tasks[0].token_traces == 2
    assert not summary.tasks[0].agrees


def test_a_scored_run_and_a_failed_run_of_one_task_disagree() -> None:
    first = on_task(scored("run-1"), "csv")
    second = on_task(failed("run-2"), "csv")

    (summary,) = summarise([first, second])

    (task,) = summary.tasks
    assert task.results == ("clean_patch", "infrastructure_failed")
    assert task.resolved == 1
    assert not task.agrees


def test_runs_that_failed_the_same_way_agree() -> None:
    """Consistent failure is still consistent; the summary reports it as such
    and the failure list says what it was."""
    runs = [on_task(failed(f"run-{n}"), "csv") for n in range(2)]

    (summary,) = summarise(runs)

    assert summary.tasks[0].agrees
    assert summary.tasks[0].patches == 0


def test_one_run_per_task_is_not_a_repeat() -> None:
    runs = [on_task(scored("run-1"), "csv"), on_task(scored("run-2"), "semver")]

    assert spread_of(*runs) == (False, 2, 2)


def test_tasks_are_listed_in_order_and_account_for_every_run() -> None:
    runs = [
        on_task(scored("run-1"), "semver"),
        on_task(scored("run-2"), "csv"),
        on_task(scored("run-3"), "semver"),
    ]

    (summary,) = summarise(runs)

    assert [(t.task_id, t.runs) for t in summary.tasks] == [("csv", 1), ("semver", 2)]


def test_a_failed_run_adds_no_agent_time_to_its_task() -> None:
    """The suite-level rule, held per task: failed runs contribute no latency."""
    runs = [on_task(scored("run-1", agent_ms=100), "csv"), on_task(failed(), "csv")]

    (summary,) = summarise(runs)

    agent = summary.tasks[0].agent_ms
    assert agent is not None
    assert (agent.fastest_ms, agent.slowest_ms, agent.counted) == (100, 100, 1)
