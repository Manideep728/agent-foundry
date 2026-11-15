"""A model client for any server that speaks the OpenAI chat-completions API.

One adapter reaches the free hosted tiers (D38) and local servers such as
llama.cpp and vLLM, because they share `POST /chat/completions`. Plain HTTP
through `urllib`, like the Ollama client, so the core still needs no
dependency.

Two things differ from Ollama and shape this module. Tool answers are matched
to calls by identifier, not by name. And a free tier refuses requests above its
rate with 429, so the client waits and retries a bounded number of times, and
reports how long it waited apart from how long the model took.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any

from control_plane import __version__
from control_plane.domain.model import (
    Message,
    MessageRole,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolSpec,
)
from control_plane.services.errors import AgentError

DEFAULT_TIMEOUT_S = 300.0
DEFAULT_RATE_LIMIT_ATTEMPTS = 6
DEFAULT_MAX_WAIT_S = 60.0
"""The longest one wait may be. A provider that asks for an hour is refused."""

USER_AGENT = f"agentfoundry/{__version__}"
"""Sent on every request. Cloudflare, in front of Groq, refuses urllib's default
`Python-urllib/3.x` with 403 "error code: 1010" before the API sees the key."""

_FALLBACK_WAIT_S = 5.0
"""The first wait when a 429 names no Retry-After; doubled on each attempt."""


class OpenAICompatClient:
    """Calls `/chat/completions` and reports what it consumed and waited."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None,
        provider_id: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        seed: int = 0,
        rate_limit_attempts: int = DEFAULT_RATE_LIMIT_ATTEMPTS,
        max_wait_s: float = DEFAULT_MAX_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
        monotonic_ms: Callable[[], int] = lambda: time.monotonic_ns() // 1_000_000,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {timeout_s}")
        if rate_limit_attempts < 1:
            raise ValueError("rate_limit_attempts must be at least 1")
        self._model = model
        self._base_url = base_url.rstrip("/")
        # Kept private and never formatted into a message: every error below
        # names the URL and the status, and none of them names a header.
        self._api_key = api_key
        self._provider_id = provider_id
        self._timeout_s = timeout_s
        self._seed = seed
        self._rate_limit_attempts = rate_limit_attempts
        self._max_wait_s = max_wait_s
        self._sleep = sleep
        self._monotonic_ms = monotonic_ms

    @property
    def provider_id(self) -> str:
        return self._provider_id

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
            # The same settings the Ollama client sends. A hosted model is not
            # guaranteed to be deterministic under them; repeats measure that.
            "temperature": 0,
            "seed": self._seed,
        }
        if tools:
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
        body, waited_ms = self._post_with_retries(payload)
        return _decode(body, self._provider_id, waited_ms)

    def _post_with_retries(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        waited_ms = 0
        fallback_s = _FALLBACK_WAIT_S
        for attempt in range(1, self._rate_limit_attempts + 1):
            try:
                return self._post(payload), waited_ms
            except _RateLimited as limited:
                if attempt == self._rate_limit_attempts:
                    raise AgentError(
                        f"{self._provider_id} refused {attempt} request(s) with "
                        f"429 after {waited_ms} ms of waiting; the tier's rate "
                        f"limit is lower than this run needs"
                    ) from None
                wait_s = limited.retry_after_s
                if wait_s is None:
                    wait_s = fallback_s
                    fallback_s *= 2
                if wait_s > self._max_wait_s:
                    raise AgentError(
                        f"{self._provider_id} asked for a {wait_s:.0f} s wait, "
                        f"longer than the {self._max_wait_s:.0f} s this client "
                        f"allows"
                    ) from None
                started = self._monotonic_ms()
                self._sleep(wait_s)
                waited_ms += self._monotonic_ms() - started
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            # An HTTPError holds the response open; without this every refused
            # request leaks a socket, and a rate-limited suite makes hundreds.
            with error:
                if error.code == 429:
                    retry_after = _retry_after_s(error.headers.get("Retry-After"))
                    raise _RateLimited(retry_after) from None
                detail = error.read().decode("utf-8", errors="replace")[:500]
            raise AgentError(
                f"{self._provider_id} returned {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise AgentError(
                f"could not reach {self._provider_id} at {self._base_url}: "
                f"{error.reason}"
            ) from error
        except TimeoutError as error:
            raise AgentError(
                f"{self._provider_id} did not answer within {self._timeout_s:.0f}s"
            ) from error

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AgentError(
                f"{self._provider_id} returned a body that is not JSON: {raw[:200]!r}"
            ) from error
        if not isinstance(decoded, dict):
            raise AgentError(
                f"{self._provider_id} returned {type(decoded).__name__}, not an object"
            )
        return decoded


class _RateLimited(Exception):
    def __init__(self, retry_after_s: float | None) -> None:
        super().__init__("429")
        self.retry_after_s = retry_after_s


def _retry_after_s(value: str | None) -> float | None:
    """Retry-After as seconds: a number, or an HTTP date. None when absent."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - time.time())


def _encode_message(message: Message) -> dict[str, Any]:
    """One message in the chat-completions wire shape.

    A call read out of the reply text has no identifier, so it cannot be
    replayed as a structured call: the API would refuse an answer to a call id
    it never issued. Such an answer goes back as a user message that names the
    tool, which is what the model asked for in the text.
    """
    if message.role is MessageRole.TOOL:
        if message.tool_call_id is not None:
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        return {
            "role": "user",
            "content": f"Result of {message.tool_name}:\n{message.content}",
        }

    encoded: dict[str, Any] = {"role": message.role.value, "content": message.content}
    identified = [call for call in message.tool_calls if call.call_id is not None]
    if identified:
        encoded["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in identified
        ]
    return encoded


def _decode_tool_calls(message: dict[str, Any], provider: str) -> tuple[ToolCall, ...]:
    """The tools the model asked for. Shape is checked; contents are not."""
    raw = message.get("tool_calls")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AgentError(f"{provider} returned tool_calls that is not a list")

    calls: list[ToolCall] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("function"), dict):
            raise AgentError(f"{provider} returned a tool call with no function object")
        function = entry["function"]
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise AgentError(f"{provider} returned a tool call with no name")
        call_id = entry.get("id")
        if call_id is not None and not isinstance(call_id, str):
            raise AgentError(f"{provider} returned a tool call id that is not text")
        calls.append(
            ToolCall(
                name=name,
                arguments=_arguments(function.get("arguments")),
                call_id=call_id,
            )
        )
    return tuple(calls)


def _arguments(raw: object) -> dict[str, object]:
    """The arguments as an object, or empty when the model sent something else.

    The API carries arguments as a JSON string the model wrote. A string that
    is not a JSON object is the model using the tool badly, which the agent
    answers with a tool error and records; it is not a transport fault.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _count(usage: dict[str, Any], key: str, provider: str) -> int:
    value = usage.get(key, 0)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AgentError(f"{provider} reported an unusable {key}: {value!r}")
    return value


def _decode(body: dict[str, Any], provider: str, waited_ms: int) -> ModelResponse:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentError(f"{provider} returned no choices")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise AgentError(f"{provider} returned a choice with no message")
    message = choice["message"]
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise AgentError(f"{provider} returned message content that is not text")

    usage = body.get("usage")
    if not isinstance(usage, dict):
        # Refused rather than recorded as zero. A run whose tokens are unknown
        # is not a run that used none, and the benchmark reports tokens.
        raise AgentError(f"{provider} returned no usage, so the call cannot be counted")
    prompt = _count(usage, "prompt_tokens", provider)
    details = usage.get("prompt_tokens_details")
    # Cached tokens are a subset of prompt_tokens, as with Ollama.
    cached = (
        _count(details, "cached_tokens", provider) if isinstance(details, dict) else 0
    )
    if cached > prompt:
        raise AgentError(f"{provider} reported {cached} cached of {prompt} tokens")

    return ModelResponse(
        text=content or "",
        tool_calls=_decode_tool_calls(message, provider),
        usage=ModelUsage(
            input_tokens=prompt - cached,
            output_tokens=_count(usage, "completion_tokens", provider),
            cache_read_tokens=cached,
        ),
        truncated=choice.get("finish_reason") == "length",
        waited_ms=waited_ms,
    )
