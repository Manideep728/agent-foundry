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

from control_plane.adapters.prompts import (
    DEFAULT_TOOL_PROMPT,
    LIST_FILES,
    READ_FILE,
    TOOLS,
    VARIANTS,
    WRITE_FILE,
)
from control_plane.adapters.task_toml import load_task
from control_plane.adapters.tool_use_agent import ToolUseAgent
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.ids import RunId
from control_plane.domain.model import (
    Message,
    MessageRole,
    MicroUsd,
    MicroUsdPerMTok,
    ModelResponse,
    ModelUsage,
    TokenPrice,
    ToolCall,
    ToolSpec,
    total_micros,
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
        '{"name": "list_files", "arguments": {}}\n'
        '{"name": "read_file", "arguments": {"path": "csvkit/fields.py"}}',
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


def test_newline_separated_objects_are_prose_not_a_batch(tmp_path: Path) -> None:
    """Measured against qwen2.5-coder:3b on 2026-09-20.

    That model answers a minimal request with four JSON objects, one per line,
    in `content`, with `tool_calls` null. Four genuine requests therefore score
    as a run that asked for nothing, and every stored row for that model
    undercounts what it intended.

    Read anyway, the harness would be reading a shape the provider does not
    document and the model does not claim: a message is one JSON value or it is
    prose. Widening the grammar to fit one template is how a parser for broken
    JSON starts, which is the failure D3 exists to prevent, and the rule that
    prose can never become an action would then rest on a grammar rather than
    on one comparison. The cost is stated in D22 and in the notes: a figure
    from that model is a floor, not the model's intent.
    """
    batched = (
        '{"name": "list_files", "arguments": {}}\n'
        '{"name": "read_file", "arguments": {"path": "csvkit/fields.py"}}\n'
        '{"name": "write_file", "arguments": {"path": "csvkit/fields.py", '
        '"content": "x"}}'
    )

    result, repo, _ = run(tmp_path, [says(batched)])

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert result.outcome_detail == "the model never called a tool"
    # The write in the third object must not reach the workspace: a batch is
    # all prose or all calls, never the part the harness liked.
    assert (repo / "csvkit" / "fields.py").read_text() != "x"


# --- the prompt a loop was built with ----------------------------------------


def test_the_agent_reports_the_prompt_it_was_built_with(tmp_path: Path) -> None:
    """The id must come from the variant, not from a default read twice.

    `prompt_id` is what the run records. Were it a constant beside the
    injection point, an agent built with `tools-v2` could record `tools-v1`,
    and every measurement under the new prompt would be filed under the old
    one -- the blended summary again, with no symptom.
    """
    agent = ToolUseAgent(
        ScriptedClient([says("done")]),
        FakeClock(),
        prompt=VARIANTS["tools-v2"],
    )

    assert agent.prompt_id == "tools-v2"


def test_the_system_prompt_sent_is_the_variant_that_was_asked_for(
    tmp_path: Path,
) -> None:
    client = ScriptedClient([says("done")])
    agent = ToolUseAgent(
        client,
        FakeClock(),
        prompt=VARIANTS["tools-v2"],
    )
    task, repo = prepared(tmp_path)

    agent.edit(repo, task)

    system = client.conversations[0][0]
    assert system.role is MessageRole.SYSTEM
    assert system.content == VARIANTS["tools-v2"].system
    assert client.tools_offered == VARIANTS["tools-v2"].tools


def test_a_loop_with_no_prompt_named_uses_the_shipped_one(tmp_path: Path) -> None:
    # The default stays `tools-v1` until a measurement moves it. A default that
    # changed with the newest variant would silently change what every stored
    # comparison means.
    agent = ToolUseAgent(
        ScriptedClient([says("done")]),
        FakeClock(),
    )

    assert agent.prompt_id == DEFAULT_TOOL_PROMPT == "tools-v1"


# --- the spend ceiling -------------------------------------------------------


PRICED = TokenPrice(
    model_id="stub-1",
    input_per_mtok=MicroUsdPerMTok(1_000_000),
    output_per_mtok=MicroUsdPerMTok(5_000_000),
)
"""Enough that a scripted turn costs a round 35 micro-USD.

Every local model is free, so a test that used the real price would assert
nothing: the ceiling would never be reached however many turns ran.
"""


def test_a_loop_stops_when_the_run_ceiling_is_reached(tmp_path: Path) -> None:
    """Checked before a call, not after it.

    Each scripted turn costs 35. With a ceiling of 50 the loop may make the
    first call (nothing spent) and the second (35 spent), and must not make the
    third. Checking after the call instead would always spend one call past
    the number the operator gave.
    """
    task, repo = prepared(tmp_path)
    client = ScriptedClient([calls(ToolCall(LIST_FILES, {}))] * 5)
    agent = ToolUseAgent(
        client,
        FakeClock(),
        price=PRICED,
        max_turns=12,
        run_ceiling=MicroUsd(50),
    )

    result = agent.edit(repo, task)

    assert len(result.invocations) == 2
    assert total_micros(call.cost for call in result.invocations) == 70


def test_the_note_says_the_run_stopped_on_budget(tmp_path: Path) -> None:
    # The run stays a measurement, so the only place the reason can live is
    # the note. Without it a ceiling stop is indistinguishable from a model
    # that simply stopped asking for tools.
    task, repo = prepared(tmp_path)
    agent = ToolUseAgent(
        ScriptedClient([calls(ToolCall(LIST_FILES, {}))] * 5),
        FakeClock(),
        price=PRICED,
        run_ceiling=MicroUsd(50),
    )

    result = agent.edit(repo, task)

    assert result.outcome is None, "a ceiling stop is not a harness failure"
    assert result.outcome_detail is not None
    assert "run ceiling" in result.outcome_detail


def test_a_free_model_is_never_stopped_by_a_ceiling(tmp_path: Path) -> None:
    # Today every local model is free, so the ceiling must cost those runs
    # nothing at all.
    result, _, _ = run(
        tmp_path,
        [calls(ToolCall(LIST_FILES, {})), says("done")],
        run_ceiling=MicroUsd(1),
    )

    assert len(result.invocations) == 2


def test_a_ceiling_of_zero_is_refused_by_the_loop() -> None:
    with pytest.raises(PreconditionViolated, match="positive"):
        ToolUseAgent(ScriptedClient([]), FakeClock(), run_ceiling=MicroUsd(0))
