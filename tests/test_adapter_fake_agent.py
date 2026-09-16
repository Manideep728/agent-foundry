"""The offline agent."""

from __future__ import annotations

from pathlib import Path

from control_plane.adapters.diffing import diff_workspace
from control_plane.adapters.fake_agent import FakeAgent, ScriptedModelClient
from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.task_toml import load_task
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.ids import RunId
from control_plane.domain.model import MicroUsdPerMTok, ModelInvocation, TokenPrice
from control_plane.services.ports import AgentResult
from tests.fakes import FakeClock

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tasks" / "fixtures"
SOLUTIONS = REPO / "tasks" / "solutions"
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"


def only_invocation(result: AgentResult) -> ModelInvocation:
    """The one call this agent makes, asserted to be the only one.

    The port carries a tuple so a multi-turn agent can report every call. This
    agent is single-shot, and a second invocation appearing here would mean the
    cost and latency below describe only part of the run.
    """
    assert len(result.invocations) == 1
    return result.invocations[0]


def prepared(tmp_path: Path):  # type: ignore[no-untyped-def]
    task = load_task(TASK_FILE)
    factory = WorkspaceFactory(tmp_path / "runs")
    workspace = factory.create(RunId("run-0001"))
    _, entries = factory.copy_fixture(workspace, FIXTURES / task.fixture_id)
    return task, workspace, entries


def test_the_agent_applies_the_known_solution(tmp_path: Path) -> None:
    task, workspace, entries = prepared(tmp_path)
    clock = FakeClock()

    FakeAgent(SOLUTIONS, clock).edit(workspace.repo, task)

    patch = diff_workspace(entries, FIXTURES / task.fixture_id, workspace.repo)
    assert patch.paths == ("csvkit/fields.py",)
    assert "in_quotes" in (workspace.repo / "csvkit" / "fields.py").read_text()


def test_the_agent_does_not_touch_the_fixture(tmp_path: Path) -> None:
    # The agent writes into the workspace only. If it reached the fixture, the
    # next run of every task using it would measure something different.
    task, workspace, _ = prepared(tmp_path)
    before = digest_tree(FIXTURES / task.fixture_id)[0]

    FakeAgent(SOLUTIONS, FakeClock()).edit(workspace.repo, task)

    assert digest_tree(FIXTURES / task.fixture_id)[0] == before


def test_a_missing_solution_leaves_the_workspace_unchanged(tmp_path: Path) -> None:
    # How a do-nothing agent is expressed. The run is then scored
    # NO_PATCH_PRODUCED, which is a measurement rather than a failure.
    task, workspace, entries = prepared(tmp_path)

    FakeAgent(tmp_path / "no-solutions-here", FakeClock()).edit(workspace.repo, task)

    assert diff_workspace(entries, FIXTURES / task.fixture_id, workspace.repo).is_empty


def test_usage_is_recorded_with_real_token_counts(tmp_path: Path) -> None:
    # Non-zero, so cost arithmetic is exercised end to end rather than being
    # structurally zero and therefore untested.
    task, workspace, _ = prepared(tmp_path)

    invocation = only_invocation(
        FakeAgent(SOLUTIONS, FakeClock()).edit(workspace.repo, task)
    )

    assert invocation.provider_id == "fake"
    assert invocation.usage.input_tokens > 0
    assert invocation.usage.output_tokens > 0
    assert invocation.cost == 0  # a model that was never called is free


def test_a_priced_client_produces_a_real_cost(tmp_path: Path) -> None:
    # Proves the same code path yields a paid figure when the price is not
    # zero -- the local-versus-API comparability D6 asks for.
    task, workspace, _ = prepared(tmp_path)
    price = TokenPrice(
        model_id="scripted",
        input_per_mtok=MicroUsdPerMTok(3_000_000),
        output_per_mtok=MicroUsdPerMTok(15_000_000),
    )

    invocation = only_invocation(
        FakeAgent(SOLUTIONS, FakeClock(), price=price).edit(workspace.repo, task)
    )

    # 4200 * 3 + 310 * 15 micro-dollars
    assert invocation.cost == 4_200 * 3 + 310 * 15


def test_latency_comes_from_the_injected_clock(tmp_path: Path) -> None:
    task, workspace, _ = prepared(tmp_path)

    class AdvancingClock(FakeClock):
        def monotonic_ms(self) -> int:
            value = super().monotonic_ms()
            self.advance(250)
            return value

    invocation = only_invocation(
        FakeAgent(SOLUTIONS, AdvancingClock()).edit(workspace.repo, task)
    )
    assert invocation.latency_ms == 250


def test_the_model_client_sees_the_task_description(tmp_path: Path) -> None:
    task, workspace, _ = prepared(tmp_path)
    client = ScriptedModelClient()

    FakeAgent(SOLUTIONS, FakeClock(), client=client).edit(workspace.repo, task)

    assert len(client.calls) == 1
    assert task.description.strip()[:20] in client.calls[0][1].content
