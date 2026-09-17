"""An agent that looks around before it edits.

The single-shot agent sends every visible file and asks for the whole repository
back in one reply. That works for a fixture of a few hundred bytes and stops
working as soon as a task is larger than one prompt, because the prompt is the
entire context the model ever gets.

This one is given three tools -- list the files, read one, write one -- and
several turns to use them. It costs more calls for the same edit, which is the
point of keeping both: they answer the same tasks through different amounts of
context, and the benchmark can say which is worth the tokens.

Two properties of the single-shot agent are deliberately *not* reproduced.

Writes are no longer all-or-nothing. `_apply` there plans every edit before
making any, so a reply whose second block is unusable leaves nothing behind.
A loop writes when the model says to, so a run that gives up halfway leaves
half an edit -- which is a real patch, scored as one, and not called malformed.

`MALFORMED_OUTPUT` is claimable only while nothing has been written. The record
refuses that outcome alongside a non-empty patch, and rightly: a reply that
could not be read cannot have changed a file. A loop that wrote something and
then lost its way reports no outcome at all and lets the score speak.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import is_excluded, validate_fixture_path
from control_plane.domain.model import (
    Message,
    MessageRole,
    ModelClient,
    ModelInvocation,
    ModelResponse,
    TokenPrice,
    ToolCall,
    ToolSpec,
    cost_micros,
)
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.ports import Clock
from control_plane.domain.task import TaskDefinition
from control_plane.services.errors import AgentError
from control_plane.services.ports import AgentResult

DEFAULT_MAX_TURNS = 12
"""Turns before the loop stops and reports what the model managed.

A budget, not a target. A model that keeps reading files without ever writing
one would otherwise run until the task's total timeout, and a benchmark row
that says "timed out" tells you nothing about the model.
"""

MAX_READ_BYTES = 64_000
"""Per-file cap on what a read returns.

Enough for any fixture file, and small enough that a model asking for something
enormous cannot fill its own context with one call.
"""

LIST_FILES = "list_files"
READ_FILE = "read_file"
WRITE_FILE = "write_file"

TOOLS = (
    ToolSpec(
        name=LIST_FILES,
        description=(
            "List every file you may read or change, as paths relative to the "
            "repository root. Takes no arguments."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name=READ_FILE,
        description="Read one file. Give its path exactly as list_files gave it.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root.",
                }
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name=WRITE_FILE,
        description=(
            "Replace one file with new contents. Give the whole file, not a "
            "diff and not a fragment."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root.",
                },
                "content": {
                    "type": "string",
                    "description": "The complete new contents of the file.",
                },
            },
            "required": ["path", "content"],
        },
    ),
)

_SYSTEM_PROMPT = """You repair a small software repository.

Work with the tools you are given. A typical repair is: list the files, read
the ones that matter, then write the file you need to change.

Rules:
- Use list_files before assuming a path exists.
- Give whole files to write_file, never a diff or a fragment.
- Change as few files as possible.
- Some files are not shown to you. That is deliberate; work from the ones you
  can see.
- When the repair is done, say so in a short sentence and stop calling tools."""


class ToolError(Exception):
    """A tool the model could not use as asked.

    Returned to the model as the tool's answer rather than raised, so it can
    correct itself on the next turn. A model calling a tool wrongly is part of
    what this measures, not a harness fault -- unlike `AgentError`, which means
    the adapter itself broke.
    """


@dataclass(frozen=True, slots=True)
class _Workspace:
    """The repository copy, and the rules for touching it."""

    root: Path
    hidden: frozenset[str]

    def visible_files(self) -> list[str]:
        found = []
        for entry in sorted(self.root.rglob("*")):
            if not entry.is_file():
                continue
            relative = entry.relative_to(self.root)
            if any(is_excluded(part) for part in relative.parts):
                continue
            path = relative.as_posix()
            if path in self.hidden:
                continue
            found.append(path)
        return found

    def resolve(self, path: object) -> Path:
        """Turn a path the model sent into one inside the workspace.

        Two layers, both of which earn their place. `validate_fixture_path`
        rejects absolute paths, traversal segments and backslashes; the
        resolved-parent check catches what a symlink inside the workspace could
        still reach. This is the same pair the single-shot agent uses, called
        rather than restated -- a second spelling of a boundary is how the two
        drift apart.
        """
        if not isinstance(path, str):
            raise ToolError(f"path must be a string, got {type(path).__name__}")
        try:
            validate_fixture_path(path)
        except DomainError as error:
            raise ToolError(f"unusable path {path!r}: {error}") from error

        target = (self.root.resolve() / path).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ToolError(f"path {path!r} leaves the repository")
        return target

    def read(self, path: object) -> str:
        if isinstance(path, str) and path in self.hidden:
            # Named rather than silently absent, so the model stops asking. It
            # is still never shown: D18 -- a score taken with the fail-to-pass
            # test in the prompt cannot be compared with one taken without it.
            raise ToolError(f"{path} is not available for this task")
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"{path} is not a file in this repository")
        data = target.read_bytes()[:MAX_READ_BYTES]
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolError(f"{path} is not UTF-8 text") from error

    def write(self, path: object, content: object) -> str:
        if not isinstance(content, str):
            raise ToolError(f"content must be a string, got {type(content).__name__}")
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Normalised to end in exactly one newline, matching the single-shot
        # agent, so the two produce identical patches for identical edits and
        # their diffs stay comparable.
        target.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"


class ToolUseAgent:
    """Edits a workspace over several turns, using tools."""

    def __init__(
        self,
        client: ModelClient,
        clock: Clock,
        *,
        price: TokenPrice | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        self._client = client
        self._clock = clock
        self._price = price or TokenPrice.free(client.model_id)
        self._max_turns = max_turns

    @property
    def provider_id(self) -> str:
        return self._client.provider_id

    def edit(self, workspace: Path, task: TaskDefinition) -> AgentResult:
        space = _Workspace(root=workspace, hidden=frozenset(task.hidden_paths))
        messages: list[Message] = [
            Message(MessageRole.SYSTEM, _SYSTEM_PROMPT),
            Message(MessageRole.USER, self._opening(space, task)),
        ]
        invocations: list[ModelInvocation] = []
        wrote_anything = False
        text_calls = 0

        for _ in range(self._max_turns):
            response, invocation = self._call(messages)
            invocations.append(invocation)

            tool_calls = response.tool_calls
            if not tool_calls:
                tool_calls = _tool_calls_in_text(response.text)
                if tool_calls:
                    text_calls += 1

            if not tool_calls:
                # The model answered in prose. If it has already written
                # something that is the end of the job; if it has not, it
                # talked instead of working, which is a malformed run.
                if wrote_anything:
                    return AgentResult(
                        tuple(invocations), outcome_detail=_note(text_calls)
                    )
                return AgentResult(
                    tuple(invocations),
                    outcome=AgentOutcome.MALFORMED_OUTPUT,
                    raw_reply=response.text,
                    outcome_detail="the model never called a tool",
                )

            messages.append(
                Message(MessageRole.ASSISTANT, response.text, tool_calls=tool_calls)
            )
            for call in tool_calls:
                answer, wrote = self._run_tool(space, call)
                wrote_anything = wrote_anything or wrote
                messages.append(Message(MessageRole.TOOL, answer, tool_name=call.name))

        # Out of turns. Whatever was written is a real patch and is scored as
        # one; only a loop that never wrote anything is malformed.
        if wrote_anything:
            return AgentResult(tuple(invocations), outcome_detail=_note(text_calls))
        return AgentResult(
            tuple(invocations),
            outcome=AgentOutcome.MALFORMED_OUTPUT,
            raw_reply=None,
            outcome_detail=f"used all {self._max_turns} turns without writing a file",
        )

    def _opening(self, space: _Workspace, task: TaskDefinition) -> str:
        listing = "\n".join(space.visible_files())
        return f"{task.description.strip()}\n\nFiles you may read:\n{listing}"

    def _call(self, messages: list[Message]) -> tuple[ModelResponse, ModelInvocation]:
        started = self._clock.monotonic_ms()
        response = self._client.complete(tuple(messages), TOOLS)
        return response, ModelInvocation(
            provider_id=self._client.provider_id,
            model_id=self._client.model_id,
            usage=response.usage,
            cost=cost_micros(response.usage, self._price),
            latency_ms=self._clock.monotonic_ms() - started,
        )

    def _run_tool(self, space: _Workspace, call: ToolCall) -> tuple[str, bool]:
        """Run one tool. Returns what to tell the model, and whether it wrote.

        Every failure the model can cause comes back as text it can read and
        retry. Raising here would end the run as a harness failure and remove
        it from the benchmark, which would quietly delete exactly the runs
        where a model handles tools badly.
        """
        try:
            if call.name == LIST_FILES:
                return "\n".join(space.visible_files()), False
            if call.name == READ_FILE:
                return space.read(call.arguments.get("path")), False
            if call.name == WRITE_FILE:
                answer = space.write(
                    call.arguments.get("path"), call.arguments.get("content")
                )
                return answer, True
        except ToolError as error:
            return f"error: {error}", False
        except OSError as error:
            raise AgentError(f"{call.name} failed on the workspace: {error}") from error

        known = ", ".join(tool.name for tool in TOOLS)
        return f"error: no tool named {call.name}. Available: {known}", False


_TOOL_NAMES = frozenset(tool.name for tool in TOOLS)


def _note(text_calls: int) -> str | None:
    """What to record about *how* the model asked for its tools.

    Reported rather than smoothed over. If a model never uses the structured
    channel, its runs are not comparable with one that does, and a fallback
    that left no trace would hide exactly that.
    """
    if text_calls == 0:
        return None
    return f"{text_calls} turn(s) asked for tools as text rather than tool calls"


def _tool_calls_in_text(text: str) -> tuple[ToolCall, ...]:
    """A tool call the model wrote as content instead of sending as one.

    Measured on 2026-09-16: `qwen2.5-coder:3b` advertises the `tools`
    capability, and Ollama returns `tool_calls: null` with
    `{"name": "list_files", "arguments": {}}` sitting in `content` -- for a
    minimal, correct request. Refusing to read that would measure the
    provider's template rather than the model, and would leave the loop
    untestable against the only model this project develops against.

    Deliberately narrow, so ordinary prose can never be mistaken for a call:
    the whole message must be one JSON object or a list of them, every entry
    must name a tool that exists, and arguments must be an object. Anything
    else is prose. Each use is counted and reported, so a model that only ever
    asks this way is visible in the data rather than quietly accommodated.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # A fenced block, which some templates wrap the call in.
        body = stripped.split("\n", 1)[-1]
        stripped = body.rsplit("```", 1)[0].strip()
    if not stripped.startswith(("{", "[")):
        return ()

    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return ()

    entries = decoded if isinstance(decoded, list) else [decoded]
    calls: list[ToolCall] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return ()
        name = entry.get("name")
        if not isinstance(name, str) or name not in _TOOL_NAMES:
            return ()
        arguments = entry.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return ()
        calls.append(ToolCall(name=name, arguments=dict(arguments)))
    return tuple(calls)


def describe_tools() -> str:
    """The tool schemas as JSON. Used by a test and by `af doctor`."""
    return json.dumps(
        [{"name": tool.name, "parameters": tool.parameters} for tool in TOOLS],
        indent=2,
    )
