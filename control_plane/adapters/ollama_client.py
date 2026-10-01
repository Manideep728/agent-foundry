"""A model client for a locally hosted Ollama server.

Plain HTTP over JSON through `urllib`, deliberately. `pyproject.toml` keeps the
control plane on the standard library, and Ollama's API needs no SDK to reach,
so that stays true for the whole local-development path. The first real
dependency arrives with the hosted-provider adapter, which is where it is
actually needed.

Costs nothing and still reports real token counts (D6). That is the point of
running a local model during development: the same record shape as a paid
provider, so a report built against local runs does not have to change when a
paid run appears next to it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from control_plane.domain.model import (
    Message,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolSpec,
)
from control_plane.services.errors import AgentError

PROVIDER_ID = "ollama"

DEFAULT_BASE_URL = "http://localhost:11434"

DEFAULT_TIMEOUT_S = 300.0
"""Generous, because the first call to a model loads it.

A cold load of a 3B model was measured at 24 s on this machine, and a larger
model on a colder cache is slower. A timeout tuned to a warm server turns an
ordinary first run into a harness failure.
"""


class OllamaClient:
    """Calls `/api/chat` on an Ollama server and reports what it consumed."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        seed: int = 0,
        temperature: float = 0.0,
        num_predict: int | None = None,
    ) -> None:
        # Zero would not mean "no timeout": the socket would be non-blocking,
        # and every call would fail at once with an error about the socket.
        if timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {timeout_s}")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        # Temperature zero and a fixed seed, so a run is reproducible. A
        # benchmark whose numbers move between identical runs measures the
        # sampler as much as the model.
        self._seed = seed
        self._temperature = temperature
        self._num_predict = num_predict

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    @property
    def model_id(self) -> str:
        return self._model

    def complete(
        self,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...] = (),
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_encode_message(m) for m in messages],
            "stream": False,
            "options": {"temperature": self._temperature, "seed": self._seed},
            # Unloaded again once this reply is written, leaving nothing cached
            # for the next request. See `_unload` for why.
            "keep_alive": 0,
        }
        if tools:
            # Omitted entirely when empty rather than sent as []. Some builds
            # switch to a tool-calling prompt template on the key's presence,
            # which would change single-shot replies for no reason.
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        if self._num_predict is not None:
            payload["options"]["num_predict"] = self._num_predict

        self._unload()
        response = _decode(self._post("/api/chat", payload))
        if response.usage.cache_read_tokens:
            # The model was still loaded when this request arrived, most likely
            # because another client was using the same server at the time. The
            # reply is valid, but it is no longer a function of the request
            # alone, so a rerun could not reproduce it. Failing the run says so;
            # storing it would put an unrepeatable number in a table that
            # claims to be repeatable.
            raise AgentError(
                f"ollama reused {response.usage.cache_read_tokens} cached prompt "
                "tokens, so this reply depends on an earlier request; is another "
                "client using this server?"
            )
        return response

    def _unload(self) -> None:
        """Make the next request start from an empty prompt cache.

        Temperature 0 and a fixed seed are not enough on their own. A reply
        also depends on how much of its prompt the server could reuse from
        whatever it handled before, because the reused prefix changes how the
        rest is batched and so the floating-point result. Measured 2026-09-29:
        the same request after two different predecessors gave two different
        replies, and a tool loop's third turn changed after a fresh model
        load. That is how one task produced a patch in one suite and none in
        the next.

        An unload takes about 6 ms, loaded or not, and never loads the model
        to unload it. The reload that follows is the real cost: about 2.5 s
        per call for a 7B model from the page cache.
        """
        self._post("/api/generate", {"model": self._model, "keep_alive": 0})

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            # The body usually names the real problem -- an unpulled model, a
            # bad option -- and losing it leaves only a status code to debug.
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise AgentError(
                f"ollama returned {error.code} for {path}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise AgentError(
                f"could not reach ollama at {self._base_url}: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise AgentError(
                f"ollama did not answer within {self._timeout_s:.0f}s"
            ) from error

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AgentError(
                f"ollama returned a body that is not JSON: {raw[:200]!r}"
            ) from error
        if not isinstance(decoded, dict):
            raise AgentError(f"ollama returned {type(decoded).__name__}, not an object")
        return decoded


def _encode_message(message: Message) -> dict[str, Any]:
    """One message in Ollama's wire shape.

    An assistant turn that asked for tools is replayed with its tool calls
    intact. Dropping them makes the model see its own request disappear from
    the history, and it asks again -- a loop that burns its whole turn budget
    repeating one call.
    """
    encoded: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        encoded["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    if message.tool_name is not None:
        encoded["tool_name"] = message.tool_name
    return encoded


def _decode_tool_calls(message: dict[str, Any]) -> tuple[ToolCall, ...]:
    """The tools the model asked for, if any.

    Shape is checked, contents are not. A name that is not a tool, or arguments
    with the wrong keys, is the model using the tools badly -- which is a
    measurement the agent reports, not a transport fault. What is refused here
    is a body that is not the documented shape at all, because guessing at that
    would turn a provider change into wrong data rather than an error.
    """
    raw = message.get("tool_calls")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AgentError(
            f"ollama returned tool_calls as {type(raw).__name__}, not a list"
        )

    calls: list[ToolCall] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise AgentError("ollama returned a tool call that is not an object")
        function = entry.get("function")
        if not isinstance(function, dict):
            raise AgentError("ollama returned a tool call with no function object")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise AgentError("ollama returned a tool call with no name")
        arguments = function.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise AgentError(
                f"ollama returned arguments for {name} as "
                f"{type(arguments).__name__}, not an object"
            )
        calls.append(ToolCall(name=name, arguments=dict(arguments)))
    return tuple(calls)


def _count(body: dict[str, Any], key: str) -> int:
    """A token count from the response, absent meaning zero.

    Absent is not an error: Ollama omits counters it has nothing to report for.
    A non-integer is an error, because a string where a count belongs means the
    response is not the shape this adapter was written against, and guessing
    would put a wrong number into a measurement.
    """
    value = body.get(key, 0)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise AgentError(f"ollama reported a non-integer {key}: {value!r}")
    if value < 0:
        raise AgentError(f"ollama reported a negative {key}: {value}")
    return value


def _decode(body: dict[str, Any]) -> ModelResponse:
    message = body.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise AgentError("ollama returned no message content")

    # prompt_eval_cached_count is a SUBSET of prompt_eval_count, not an extra.
    # Measured: an identical second call reported prompt_eval_count 271 with
    # prompt_eval_cached_count 270. Adding them would report 541 tokens for a
    # 271-token prompt, so every cached run would claim roughly twice the
    # input it used. The domain wants cache reads as their own line item, so
    # the freshly evaluated count is the difference.
    prompt_total = _count(body, "prompt_eval_count")
    cached = _count(body, "prompt_eval_cached_count")
    if cached > prompt_total:
        raise AgentError(
            f"ollama reported {cached} cached prompt tokens out of "
            f"{prompt_total}, which cannot be a subset"
        )

    return ModelResponse(
        text=message["content"],
        tool_calls=_decode_tool_calls(message),
        usage=ModelUsage(
            input_tokens=prompt_total - cached,
            output_tokens=_count(body, "eval_count"),
            cache_read_tokens=cached,
        ),
        # "length" means the output cap cut the reply off. Anything else --
        # "stop", or a field this adapter has not seen -- is a complete reply.
        truncated=body.get("done_reason") == "length",
    )
