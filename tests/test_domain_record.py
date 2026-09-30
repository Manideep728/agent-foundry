"""The record's agreement invariants.

A stored outcome that drifts from the stored score is a silent reporting error:
every table built from the database repeats it, and nothing looks wrong. These
turn that into a load-time exception.
"""

from __future__ import annotations

import pytest

from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.record import RunRecord
from control_plane.domain.scoring import Score
from control_plane.domain.states import EvaluationRun, RunState
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


def a_report() -> ValidationReport:
    """The minimum a scored record needs. Its contents are not under test."""
    stream = StreamCapture(
        bytes_total=3,
        bytes_written=3,
        truncated=False,
        path="cmd-000.stdout.log",
        tail="ok\n",
    )
    return ValidationReport(
        schema_version=SCHEMA_VERSION,
        runner_version="local-subprocess 0.1.0 (no isolation)",
        arch="x86_64",
        pid1=False,
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
                command="python3 -m unittest tests.test_quoting",
                started_at_unix_ms=EPOCH_MS,
                duration_ms=5,
                outcome=CommandOutcome.EXITED,
                stdout=stream,
                stderr=stream,
                exit_code=1,
            ),
        ),
    )


UNRESOLVED = Score(
    resolved=False,
    regressed=False,
    fail_to_pass_passed=0,
    fail_to_pass_total=1,
    pass_to_pass_passed=1,
    pass_to_pass_total=1,
    timed_out_commands=0,
)


def scored_run() -> EvaluationRun:
    run = EvaluationRun.create(RunId("run-0001"), TaskId("csv-quoted-fields"), EPOCH_MS)
    for state in (
        RunState.PREPARING,
        RunState.AGENT_RUNNING,
        RunState.VALIDATING,
        RunState.SCORED,
    ):
        run = run.transition_to(state, EPOCH_MS)
    return run


def record(**overrides: object) -> RunRecord:
    fields: dict[str, object] = {
        "run": scored_run(),
        "suite": Suite.SYNTHETIC,
        "fixture_sha256": "a" * 64,
        "runner_id": "local-subprocess",
        "runner_version": "local-subprocess 0.1.0 (no isolation)",
        "arch": "x86_64",
        "pid1": False,
        "report": a_report(),
        "score": UNRESOLVED,
        "patch_is_empty": True,
    }
    fields.update(overrides)
    return RunRecord(**fields)  # type: ignore[arg-type]


def test_a_malformed_reply_may_disagree_with_the_classification() -> None:
    """The one outcome the validation cannot derive.

    An empty patch classifies as NO_PATCH_PRODUCED. The agent is allowed to
    overrule that, because only it saw the reply that could not be read.
    """
    stored = record(outcome=AgentOutcome.MALFORMED_OUTPUT)

    assert stored.outcome is AgentOutcome.MALFORMED_OUTPUT


def test_a_malformed_reply_that_changed_a_file_is_refused() -> None:
    """The constraint that keeps the exemption from being a blank cheque.

    A reply that could not be read as an edit cannot have written one. If both
    were true the adapter parsed something well enough to act on it, and
    calling that malformed would hide a real patch from the score.
    """
    with pytest.raises(PreconditionViolated, match="requires an empty patch"):
        record(
            outcome=AgentOutcome.MALFORMED_OUTPUT,
            patch_is_empty=False,
            patch_sha256="b" * 64,
            patch_bytes=421,
        )


def test_any_other_outcome_must_still_match_the_score() -> None:
    """The original invariant, which the malformed exemption must not weaken.

    This score resolved nothing, so CLEAN_PATCH is a lie. Nothing else may take
    the escape hatch that MALFORMED_OUTPUT has.
    """
    with pytest.raises(PreconditionViolated, match="disagrees with the score"):
        record(
            outcome=AgentOutcome.CLEAN_PATCH,
            patch_is_empty=False,
            patch_sha256="b" * 64,
            patch_bytes=421,
        )


def test_an_outcome_that_matches_the_score_is_accepted() -> None:
    stored = record(outcome=AgentOutcome.NO_PATCH_PRODUCED)

    assert stored.outcome is AgentOutcome.NO_PATCH_PRODUCED


def test_a_run_names_its_whole_configuration_or_none_of_it() -> None:
    """A half-named configuration is a wiring bug that nothing else catches.

    It does not violate any type, and it does not look wrong in a row. It
    surfaces as an extra line in a benchmark table -- one agent, no model --
    that a reader has to work out is the same configuration as the line above.
    """
    with pytest.raises(PreconditionViolated, match="whole configuration"):
        record(
            outcome=AgentOutcome.NO_PATCH_PRODUCED,
            agent_id="tools",
            provider_id="ollama",
        )


def test_an_unrecorded_configuration_is_all_three_missing() -> None:
    # A run stored before the harness wrote this down. None, never "".
    stored = record(outcome=AgentOutcome.NO_PATCH_PRODUCED)

    assert (stored.agent_id, stored.provider_id, stored.model_id) == (None,) * 3


def test_a_fully_named_configuration_is_accepted() -> None:
    stored = record(
        outcome=AgentOutcome.NO_PATCH_PRODUCED,
        agent_id="tools",
        provider_id="ollama",
        model_id="qwen2.5-coder:7b",
    )

    assert stored.agent_id == "tools"


def test_a_record_may_name_a_configuration_without_a_prompt() -> None:
    """The fake agent has a configuration and asks no model anything.

    Folding the prompt into the all-three-or-none rule would make that state
    unrepresentable, and the harness would have to invent a prompt name for an
    agent that reads no instructions.
    """
    stored = record(
        outcome=AgentOutcome.NO_PATCH_PRODUCED,
        agent_id="fake",
        provider_id="fake",
        model_id="scripted",
    )

    assert stored.prompt_id is None


def test_a_prompt_id_is_a_name_or_none_never_the_empty_string() -> None:
    # The `image_digest` rule, one field over: an empty name would satisfy a
    # filter looking for a real one, so a run with no prompt would be counted
    # in a comparison between prompts.
    with pytest.raises(PreconditionViolated, match="prompt_id"):
        record(outcome=AgentOutcome.NO_PATCH_PRODUCED, prompt_id="")


def test_a_harness_version_is_a_version_or_none_never_the_empty_string() -> None:
    # Same rule again. An empty version would read as "a build" in a report
    # that lists the builds a group spans, when it is really "no build named".
    with pytest.raises(PreconditionViolated, match="harness_version"):
        record(outcome=AgentOutcome.NO_PATCH_PRODUCED, harness_version="")
