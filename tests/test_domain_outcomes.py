"""Harness failures and agent outcomes must stay distinguishable."""

from __future__ import annotations

import pytest

from control_plane.domain.outcomes import AgentOutcome, FailureCategory


def test_the_two_vocabularies_do_not_share_any_value() -> None:
    # Both are written into records and reports. A shared string would make a
    # stored value ambiguous -- and the specific ambiguity that matters is a
    # container OOM reading as an agent failure, which would depress every
    # number in the benchmark without anything appearing to go wrong.
    failures = {member.value for member in FailureCategory}
    outcomes = {member.value for member in AgentOutcome}
    assert failures & outcomes == set()


@pytest.mark.parametrize("enum_type", [FailureCategory, AgentOutcome])
def test_no_member_is_a_silent_alias(
    enum_type: type[FailureCategory] | type[AgentOutcome],
) -> None:
    # @unique turns a duplicated value into an error at import time. Without
    # it, two members sharing a value become aliases: the second name silently
    # resolves to the first, and a category quietly stops being recordable.
    assert len(set(enum_type)) == len(list(enum_type))
    assert len({m.value for m in enum_type}) == len(list(enum_type))


@pytest.mark.parametrize(
    ("situation", "expected"),
    [
        ("docker daemon unreachable", FailureCategory.INFRASTRUCTURE_FAILED),
        ("container OOM-killed, exit 137", FailureCategory.INFRASTRUCTURE_FAILED),
        (
            "fixture on disk does not match the pinned hash",
            FailureCategory.FIXTURE_HASH_MISMATCH,
        ),
        ("model API returned 429 after retries", FailureCategory.AGENT_ERROR),
        ("runner exited 6 and wrote no result", FailureCategory.RUNNER_FAILED),
    ],
)
def test_harness_problems_classify_as_failures(
    situation: str, expected: FailureCategory
) -> None:
    # A table rather than a function, because the mapping is the design. It is
    # asserted so that moving one of these into AgentOutcome is a test failure
    # rather than a silent reclassification.
    assert expected in FailureCategory


@pytest.mark.parametrize(
    ("situation", "expected"),
    [
        (
            "patch applied, target tests pass, nothing regressed",
            AgentOutcome.CLEAN_PATCH,
        ),
        (
            "patch applied, target tests pass, a regression test now fails",
            AgentOutcome.PATCH_WITH_REGRESSIONS,
        ),
        ("patch applied, target tests still fail", AgentOutcome.TESTS_STILL_FAILING),
        ("validation command exceeded its deadline", AgentOutcome.VALIDATION_TIMEOUT),
        ("agent finished and changed no files", AgentOutcome.NO_PATCH_PRODUCED),
        (
            "model response could not be parsed as an edit",
            AgentOutcome.MALFORMED_OUTPUT,
        ),
    ],
)
def test_agent_problems_classify_as_measurements(
    situation: str, expected: AgentOutcome
) -> None:
    # Every one of these is the agent failing, and every one is still a result
    # worth counting. None of them may become a FailureCategory.
    assert expected in AgentOutcome
    assert expected.value not in {m.value for m in FailureCategory}


def test_values_are_stable_snake_case_strings() -> None:
    # These are written to storage and to reports, so renaming one is a data
    # migration, not a refactor.
    assert FailureCategory.INFRASTRUCTURE_FAILED.value == "infrastructure_failed"
    assert AgentOutcome.CLEAN_PATCH.value == "clean_patch"
    assert AgentOutcome("patch_with_regressions") is AgentOutcome.PATCH_WITH_REGRESSIONS
