"""An agent that applies a known solution, with no model in the loop.

It exists so the whole pipeline is reachable offline and in CI. The solution it
applies is a real directory of real files under `tasks/solutions/<fixture_id>/`
rather than a string embedded in the code, so the "known patch" is something a
reader can open and the expected diff is derivable rather than asserted.

The scripted model client reports non-zero token counts, so cost arithmetic is
exercised end to end instead of being structurally zero. Its price is zero,
which is the honest value for a model that was never called.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from control_plane.domain.model import (
    Message,
    MessageRole,
    ModelInvocation,
    ModelResponse,
    ModelUsage,
    TokenPrice,
    ToolSpec,
    cost_micros,
)
from control_plane.domain.ports import Clock
from control_plane.domain.task import TaskDefinition
from control_plane.services.ports import AgentResult

PROVIDER_ID = "fake"
MODEL_ID = "scripted"


class ScriptedModelClient:
    """Returns canned text and plausible usage without touching a network."""

    def __init__(self, reply: str = "applied the known solution") -> None:
        self._reply = reply
        self.calls: list[tuple[Message, ...]] = []

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    @property
    def model_id(self) -> str:
        return MODEL_ID

    def complete(
        self,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...] = (),
    ) -> ModelResponse:
        self.calls.append(messages)
        return ModelResponse(
            text=self._reply,
            usage=ModelUsage(input_tokens=4_200, output_tokens=310),
        )


class FakeAgent:
    """Copies a prepared solution into the workspace.

    Edits in place and returns only what it consumed. Capturing the patch is
    somebody else's job, so this cannot misreport what it changed -- if the
    solution directory is empty the diff is empty, and the run is honestly
    scored as having produced no patch.
    """

    def __init__(
        self,
        solutions_root: Path,
        clock: Clock,
        client: ScriptedModelClient | None = None,
        price: TokenPrice | None = None,
    ) -> None:
        self._solutions_root = solutions_root
        self._clock = clock
        self._client = client or ScriptedModelClient()
        self._price = price or TokenPrice.free(MODEL_ID)

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    def edit(self, workspace: Path, task: TaskDefinition) -> AgentResult:
        started = self._clock.monotonic_ms()

        response = self._client.complete(
            (
                Message(MessageRole.SYSTEM, "You are fixing a repository."),
                Message(MessageRole.USER, task.description),
            )
        )

        solution = self._solutions_root / task.fixture_id
        if solution.is_dir():
            # dirs_exist_ok because the workspace is already populated: this
            # overlays the solution rather than replacing the tree.
            shutil.copytree(solution, workspace, dirs_exist_ok=True)
        # A missing solution directory is not an error. It is how a
        # do-nothing agent is expressed, and the run is then scored as
        # NO_PATCH_PRODUCED -- which is a measurement, not a failure.

        # No outcome: this agent never reports on its own output. It either
        # applied a solution or found none, and scoring reads the patch to see
        # which -- the same evidence it would use for a real model.
        return AgentResult(
            (
                ModelInvocation(
                    provider_id=PROVIDER_ID,
                    model_id=MODEL_ID,
                    usage=response.usage,
                    cost=cost_micros(response.usage, self._price),
                    latency_ms=self._clock.monotonic_ms() - started,
                ),
            )
        )
