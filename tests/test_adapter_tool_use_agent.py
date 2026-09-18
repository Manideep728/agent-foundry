"""The tool-use agent.

The model is replaced by a script of turns, so the loop, the tool boundary and
the outcome rules are tested exactly and without a GPU. What is deliberately
re-tested here rather than assumed from the single-shot agent is the security
boundary: the two agents reach the filesystem through different code, and a
boundary that holds in one is not thereby holding in the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.task_toml import load_task
from control_plane.adapters.tool_use_agent import (
    LIST_FILES,
    READ_FILE,
    TOOLS,
    WRITE_FILE,
    ToolUseAgent,
)
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.ids import RunId
from control_plane.domain.model import (
    Message,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolSpec,
)
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.task import TaskDefinition
from tests.fakes import FakeClock

REPO = Path(__file__).resolve().parent.parent
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"
FIXTURES = REPO / "tasks" / "fixtures"


class ScriptedClient:
    """Plays a fixed list of turns, and records what it was offered."""

    provider_id = "stub"
    model_id = "stub-1"

    def __init__(self, turns: list[ModelResponse]) -> None:
        self._turns = list(turns)
        self.conversations: list[tuple[Message, ...]] = []
        self.tools_offered: tuple[ToolSpec, ...] = ()

    def complete(
        self,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...] = (),
    ) -> ModelResponse:
        self.conversations.append(messages)
        self.tools_offered = tools
        if not self._turns:
            raise AssertionError("the agent asked for more turns than were scripted")
        return self._turns.pop(0)


def says(text: str) -> ModelResponse:
    return ModelResponse(text=text, usage=ModelUsage(input_tokens=10, output_tokens=5))


def calls(*tool_calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        text="",
        usage=ModelUsage(input_tokens=10, output_tokens=5),
        tool_calls=tool_calls,
    )


def prepared(tmp_path: Path) -> tuple[TaskDefinition, Path]:
    task = load_task(TASK_FILE)
    factory = WorkspaceFactory(tmp_path / "runs")
    workspace = factory.create(RunId("run-0001"))
    factory.copy_fixture(workspace, FIXTURES / task.fixture_id)
    return task, workspace.repo


def run(tmp_path: Path, turns: list[ModelResponse], **kwargs: object):  # type: ignore[no-untyped-def]
    task, repo = prepared(tmp_path)
    client = ScriptedClient(turns)
    agent = ToolUseAgent(client, FakeClock(), **kwargs)  # type: ignore[arg-type]
    return agent.edit(repo, task), repo, client


# --- the loop ----------------------------------------------------------------


def test_a_write_reaches_the_workspace(tmp_path: Path) -> None:
    result, repo, _ = run(
        tmp_path,
        [
            calls(
                ToolCall(
                    WRITE_FILE,
                    {
                        "path": "csvkit/fields.py",
                        "content": "def split_fields(x):\n    return []",
                    },
                )
            ),
            says("done"),
        ],
    )

    assert result.outcome is None
    assert (repo / "csvkit" / "fields.py").read_text().endswith("return []\n")


def test_every_turn_is_recorded_as_its_own_invocation(tmp_path: Path) -> None:
    # A multi-turn agent's cost is the sum of its calls, not its last one.
    # Reporting one invocation would make the loop look as cheap as a single
    # prompt while it spent four times the tokens.
    result, _, _ = run(
        tmp_path,
        [
            calls(ToolCall(LIST_FILES, {})),
            calls(ToolCall(READ_FILE, {"path": "csvkit/fields.py"})),
            calls(ToolCall(WRITE_FILE, {"path": "csvkit/fields.py", "content": "x"})),
            says("done"),
        ],
    )

    assert len(result.invocations) == 4


def test_the_assistant_turn_is_replayed_with_its_tool_calls(tmp_path: Path) -> None:
    # Dropping them makes the model see its own request vanish from the
    # history, so it asks again and spends the whole budget on one call.
    _, _, client = run(
        tmp_path,
        [calls(ToolCall(LIST_FILES, {})), says("done")],
    )

    second = client.conversations[1]
    assistant = [m for m in second if m.role.value == "assistant"]
    assert assistant and assistant[0].tool_calls
    assert assistant[0].tool_calls[0].name == LIST_FILES
    assert [m for m in second if m.role.value == "tool"]


def test_the_tools_are_offered_on_every_turn(tmp_path: Path) -> None:
    _, _, client = run(tmp_path, [calls(ToolCall(LIST_FILES, {})), says("done")])

    assert {tool.name for tool in client.tools_offered} == {
        LIST_FILES,
        READ_FILE,
        WRITE_FILE,
    }


# --- the outcome rules -------------------------------------------------------


def test_prose_with_no_tool_call_is_malformed(tmp_path: Path) -> None:
    # The model talked instead of working. That is not restraint, and scoring
    # it as no_patch_produced would put it with agents that chose to change
    # nothing.
    result, _, _ = run(tmp_path, [says("You should fix the parser.")])

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert result.raw_reply == "You should fix the parser."
    assert result.outcome_detail == "the model never called a tool"


def test_a_loop_that_wrote_something_and_then_stalled_is_not_malformed(
    tmp_path: Path,
) -> None:
    """The record forbids malformed_output with a non-empty patch, correctly.

    A reply that could not be read cannot have changed a file. A loop that
    wrote a file and then ran out of turns has changed one, so it reports no
    outcome and lets the score describe what it managed.
    """
    result, _, _ = run(
        tmp_path,
        [
            calls(ToolCall(WRITE_FILE, {"path": "csvkit/fields.py", "content": "x"})),
            calls(ToolCall(READ_FILE, {"path": "csvkit/fields.py"})),
        ],
        max_turns=2,
    )

    assert result.outcome is None


def test_the_turn_budget_is_honoured(tmp_path: Path) -> None:
    # Without a budget a model that keeps reading runs until the task's total
    # timeout, and "timed out" says nothing about the model.
    result, _, _ = run(
        tmp_path,
        [calls(ToolCall(LIST_FILES, {}))] * 3,
        max_turns=3,
    )

    assert len(result.invocations) == 3


def test_a_turn_budget_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ToolUseAgent(ScriptedClient([]), FakeClock(), max_turns=0)


# --- the boundary ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../escaped.py",
        "../../etc/passwd",
        "/etc/passwd",
        "csvkit/../../escaped.py",
        "csvkit\\fields.py",
    ],
)
def test_a_write_outside_the_workspace_is_refused(tmp_path: Path, path: str) -> None:
    # Re-tested here rather than inherited from the single-shot agent: this
    # agent reaches the filesystem through different code, and the model
    # chooses these paths directly rather than through a parsed reply.
    result, repo, _ = run(
        tmp_path,
        [calls(ToolCall(WRITE_FILE, {"path": path, "content": "x"})), says("done")],
    )

    assert not (repo.parent.parent / "escaped.py").exists()
    # Not malformed: the call was read and refused on its path. The model was
    # understood and produced no patch, which is what no_patch_produced means.
    assert result.outcome is None


def test_a_symlink_out_of_the_workspace_is_refused(tmp_path: Path) -> None:
    task, repo = prepared(tmp_path)
    (repo / "escape").symlink_to(tmp_path, target_is_directory=True)
    client = ScriptedClient(
        [
            calls(ToolCall(WRITE_FILE, {"path": "escape/evil.py", "content": "boom"})),
            says("done"),
        ]
    )
    agent = ToolUseAgent(client, FakeClock())

    agent.edit(repo, task)

    assert not (tmp_path / "evil.py").exists()


def test_a_hidden_path_is_not_listed_and_not_readable(tmp_path: Path) -> None:
    """D18: the fail-to-pass test must not reach the model.

    A list_files or read_file that forgot this would hand over the answer, and
    the score would stop being comparable with one taken without it -- silently,
    because the run still works.
    """
    result, _, client = run(
        tmp_path,
        [
            calls(ToolCall(LIST_FILES, {})),
            calls(ToolCall(READ_FILE, {"path": "tests/test_quoting.py"})),
            calls(ToolCall(WRITE_FILE, {"path": "csvkit/fields.py", "content": "x"})),
            says("done"),
        ],
    )

    answers = [
        m.content
        for conversation in client.conversations
        for m in conversation
        if m.role.value == "tool"
    ]
    assert not any("test_quoting.py" in a and "not available" not in a for a in answers)
    assert any("not available for this task" in a for a in answers)
    assert result.outcome is None


def test_the_hidden_file_is_still_on_disk(tmp_path: Path) -> None:
    # Hiding governs the prompt only. The file is still copied and still run by
    # validation, or the task would measure nothing.
    _, repo, _ = run(tmp_path, [says("nothing to do")])

    assert (repo / "tests" / "test_quoting.py").is_file()


def test_an_excluded_directory_is_never_listed(tmp_path: Path) -> None:
    task, repo = prepared(tmp_path)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "fields.cpython-312.pyc").write_bytes(b"\x00")
    client = ScriptedClient([calls(ToolCall(LIST_FILES, {})), says("done")])
    agent = ToolUseAgent(client, FakeClock())

    agent.edit(repo, task)

    listing = next(m.content for m in client.conversations[1] if m.role.value == "tool")
    assert "__pycache__" not in listing


# --- tools the model used badly ---------------------------------------------


def test_an_unknown_tool_is_answered_rather_than_raised(tmp_path: Path) -> None:
    # A model calling a tool that does not exist is a measurement. Raising
    # would make it a harness failure and drop the run from the benchmark --
    # deleting exactly the runs where a model handles tools badly.
    result, _, client = run(
        tmp_path,
        [
            calls(ToolCall("delete_everything", {})),
            calls(ToolCall(WRITE_FILE, {"path": "csvkit/fields.py", "content": "x"})),
            says("done"),
        ],
    )

    answers = [m.content for m in client.conversations[1] if m.role.value == "tool"]
    assert "no tool named delete_everything" in answers[0]
    assert result.outcome is None


def test_a_missing_argument_is_answered_rather_than_raised(tmp_path: Path) -> None:
    result, _, client = run(
        tmp_path,
        [
            calls(ToolCall(READ_FILE, {})),
            calls(ToolCall(WRITE_FILE, {"path": "csvkit/fields.py", "content": "x"})),
            says("done"),
        ],
    )

    answers = [m.content for m in client.conversations[1] if m.role.value == "tool"]
    assert answers[0].startswith("error:")
    assert result.outcome is None


def test_reading_a_file_that_does_not_exist_is_answered(tmp_path: Path) -> None:
    _, _, client = run(
        tmp_path,
        [
            calls(ToolCall(READ_FILE, {"path": "nosuch.py"})),
            calls(ToolCall(WRITE_FILE, {"path": "csvkit/fields.py", "content": "x"})),
            says("done"),
        ],
    )

    answers = [m.content for m in client.conversations[1] if m.role.value == "tool"]
    assert "not a file in this repository" in answers[0]


def test_every_tool_declares_an_object_schema() -> None:
    # Ollama rejects a tool whose parameters are not a JSON Schema object, and
    # the failure arrives as an opaque 400 mid-run.
    for tool in TOOLS:
        assert tool.parameters["type"] == "object"
        assert "properties" in tool.parameters


# --- tool calls the model wrote as text -------------------------------------


def test_a_tool_call_sent_as_text_is_acted_on(tmp_path: Path) -> None:
    """Measured on 2026-09-16 against qwen2.5-coder:3b.

    The model advertises the `tools` capability, and Ollama still returns
    `tool_calls: null` with the call sitting in `content` as JSON. Refusing to
    read that measures the provider's chat template rather than the model.
    """
    result, repo, _ = run(
        tmp_path,
        [
            says(
                '{"name": "write_file", "arguments": '
                '{"path": "csvkit/fields.py", "content": "ok"}}'
            ),
            says("done"),
        ],
    )

    assert (repo / "csvkit" / "fields.py").read_text() == "ok\n"
    assert result.outcome is None


def test_asking_by_text_is_reported_rather_than_smoothed_over(
    tmp_path: Path,
) -> None:
    # A model that never uses the structured channel is not comparable with one
    # that does. A fallback leaving no trace would hide exactly that.
    result, _, _ = run(
        tmp_path,
        [
            says(
                '{"name": "write_file", "arguments": '
                '{"path": "csvkit/fields.py", "content": "ok"}}'
            ),
            says("done"),
        ],
    )

    assert result.outcome_detail is not None
    assert "as text rather than tool calls" in result.outcome_detail


def test_a_structured_call_reports_nothing_extra(tmp_path: Path) -> None:
    result, _, _ = run(
        tmp_path,
        [
            calls(ToolCall(WRITE_FILE, {"path": "csvkit/fields.py", "content": "x"})),
            says("done"),
        ],
    )

    assert result.outcome_detail is None


@pytest.mark.parametrize(
    "text",
    [
        "I will now call write_file to fix it.",
        '{"name": "not_a_tool", "arguments": {}}',
        '{"name": "read_file", "arguments": "csvkit/fields.py"}',
        '{"arguments": {}}',
        "Here is some JSON-ish text: {name: read_file}",
        '{"name": "write_file", "arguments": {"content": """x"""}}',
    ],
)
def test_prose_is_never_mistaken_for_a_tool_call(tmp_path: Path, text: str) -> None:
    """The fallback must not turn discussion into an action.

    The last case is real: the model wrote a write_file call whose content was
    a Python triple-quoted string, which is not JSON. Guessing at what it meant
    would mean writing a parser for broken JSON -- the failure D3 names -- so
    it stays a malformed run.
    """
    result, _, _ = run(tmp_path, [says(text)])

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert result.outcome_detail == "the model never called a tool"


def test_a_fenced_tool_call_is_read(tmp_path: Path) -> None:
    # Some chat templates wrap the call in a code fence.
    fenced = (
        '```json\n{"name": "write_file", "arguments": '
        '{"path": "csvkit/fields.py", "content": "ok"}}\n```'
    )
    _, repo, _ = run(tmp_path, [says(fenced), says("done")])

    assert (repo / "csvkit" / "fields.py").read_text() == "ok\n"


# --- what "malformed" actually means ----------------------------------------


def test_a_model_that_used_its_tools_and_then_stopped_is_not_malformed(
    tmp_path: Path,
) -> None:
    """Measured against qwen2.5-coder:7b on 2026-09-17, four turns.

    It listed the files, read two of them, then said the code looked correct
    and stopped. The loop reported malformed_output with the note "the model
    never called a tool" -- and it had called three.

    malformed_output is a statement about the harness being unable to read the
    model. Here the model was read perfectly and chose not to edit, which is
    an empty patch and classifies as no_patch_produced. Conflating the two
    blames the harness for the model's decision and inflates the one rate the
    agent pipeline exists to measure honestly.
    """
    result, _, _ = run(
        tmp_path,
        [
            calls(ToolCall(LIST_FILES, {})),
            calls(ToolCall(READ_FILE, {"path": "csvkit/fields.py"})),
            says("This already looks correct to me, so I am making no changes."),
        ],
    )

    assert result.outcome is None
    assert result.outcome_detail is not None
    assert "never called a tool" not in result.outcome_detail
    assert "2 tool call(s)" in result.outcome_detail


def test_a_model_that_never_called_a_tool_is_still_malformed(tmp_path: Path) -> None:
    # The genuine case the branch exists for: nothing was read, so nothing can
    # be said about what the model decided.
    result, _, _ = run(tmp_path, [says("You should fix the parser.")])

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert result.outcome_detail == "the model never called a tool"


def test_running_out_of_turns_after_using_tools_is_not_malformed(
    tmp_path: Path,
) -> None:
    # Same rule at the budget boundary. A loop that read files for its whole
    # budget was understood; it simply did not finish.
    result, _, _ = run(
        tmp_path,
        [calls(ToolCall(LIST_FILES, {})), calls(ToolCall(LIST_FILES, {}))],
        max_turns=2,
    )

    assert result.outcome is None
    assert result.outcome_detail is not None
    assert "turn budget" in result.outcome_detail
