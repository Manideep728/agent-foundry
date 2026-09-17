"""Model usage, prices and cost, in integers throughout.

No float touches a measurement. binary64 cannot represent 0.1 exactly, so a
total assembled from per-call floats depends on summation order -- the same
result scored twice can print two different figures -- and stored as SQLite
REAL it stops summing exactly. For a project whose product is reproducibility
that is disqualifying, so money is integer micro-dollars and time is integer
milliseconds, end to end.

Prices are quoted per *million* tokens rather than per token, because per-token
micro-dollars are not integral for real prices: $3.00/Mtok is 3 micro-dollars
per token, but $0.25/Mtok is 0.25. Per million, every published price is an
exact integer -- $0.25/Mtok is 250_000 -- and no rounding happens at the point
the price is written down.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import NewType, Protocol, runtime_checkable

from control_plane.domain.errors import PreconditionViolated

MicroUsd = NewType("MicroUsd", int)
"""Millionths of a US dollar."""

MicroUsdPerMTok = NewType("MicroUsdPerMTok", int)
"""Millionths of a US dollar per million tokens."""

ZERO_MICRO_USD = MicroUsd(0)
"""A named zero, so dataclass defaults reference it rather than calling
MicroUsd() -- a call evaluated once at class creation is a trap even when,
as here, NewType makes it the identity function."""

_TOKENS_PER_PRICE_UNIT = 1_000_000


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer division rounding up. Both operands must be non-negative."""
    if numerator < 0 or denominator <= 0:
        raise PreconditionViolated(
            f"ceil_div expects a non-negative numerator and a positive "
            f"denominator, got {numerator}/{denominator}"
        )
    return -(-numerator // denominator)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Token counts for one model call.

    Cache reads and writes are separate line items rather than a multiplier on
    the base rate. Providers quote them as fractions of the base price, and
    expressing that as a float multiplier would reintroduce floating point at
    precisely the point this module exists to avoid it.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
        ):
            if getattr(self, name) < 0:
                raise PreconditionViolated(f"{name} must be >= 0")

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
        )

    def __add__(self, other: ModelUsage) -> ModelUsage:
        return ModelUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """What one model costs, per million tokens."""

    model_id: str
    input_per_mtok: MicroUsdPerMTok
    output_per_mtok: MicroUsdPerMTok
    cache_write_per_mtok: MicroUsdPerMTok = MicroUsdPerMTok(0)
    cache_read_per_mtok: MicroUsdPerMTok = MicroUsdPerMTok(0)

    def __post_init__(self) -> None:
        for name in (
            "input_per_mtok",
            "output_per_mtok",
            "cache_write_per_mtok",
            "cache_read_per_mtok",
        ):
            if getattr(self, name) < 0:
                raise PreconditionViolated(f"{name} must be >= 0")

    @classmethod
    def free(cls, model_id: str) -> TokenPrice:
        """A locally hosted model. Costs nothing, still reports real tokens."""
        return cls(
            model_id=model_id,
            input_per_mtok=MicroUsdPerMTok(0),
            output_per_mtok=MicroUsdPerMTok(0),
        )


def cost_micros(usage: ModelUsage, price: TokenPrice) -> MicroUsd:
    """Estimated cost of one model call, rounded up per line item.

    Rounding up, because this number is also the input to a spend ceiling: an
    under-estimate lets a benchmark run past its budget, an over-estimate stops
    it early, and the safe direction is obvious.

    Rounded per line item rather than once at the end, because costs are summed
    and never re-derived. Round only at the total and a run's cost stops
    equalling the sum of its calls' costs -- a report whose per-call column
    does not add up to its own total destroys trust in every other figure on
    the page. The cost of that choice is bounded: at most four micro-dollars of
    over-estimate per call, which is under a thousandth of a cent.
    """
    return MicroUsd(
        ceil_div(usage.input_tokens * price.input_per_mtok, _TOKENS_PER_PRICE_UNIT)
        + ceil_div(usage.output_tokens * price.output_per_mtok, _TOKENS_PER_PRICE_UNIT)
        + ceil_div(
            usage.cache_write_tokens * price.cache_write_per_mtok,
            _TOKENS_PER_PRICE_UNIT,
        )
        + ceil_div(
            usage.cache_read_tokens * price.cache_read_per_mtok,
            _TOKENS_PER_PRICE_UNIT,
        )
    )


def total_micros(costs: Iterable[MicroUsd]) -> MicroUsd:
    """Exact integer sum. Every aggregate above a single call is exact."""
    return MicroUsd(sum(costs))


@unique
class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    """A tool's answer, fed back so the model can act on it.

    A separate role rather than another user turn: a model trained on tool use
    treats the two differently, and flattening them loses the distinction
    between what the operator asked for and what the harness observed.
    """


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One request from the model to run a tool.

    `arguments` is whatever the provider decoded, unvalidated. The model may
    send a missing key or a number where a path belongs, and deciding what to
    do about that is the agent's job -- a bad argument is a measurement, not a
    transport error.
    """

    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool as it is offered to the model."""

    name: str
    description: str
    parameters: dict[str, object]
    """JSON Schema for the arguments, passed to the provider unchanged."""


@dataclass(frozen=True, slots=True)
class Message:
    role: MessageRole
    content: str

    tool_calls: tuple[ToolCall, ...] = ()
    """Set on an assistant turn that asked for tools, when it is replayed.

    The conversation sent on turn N+1 must contain the assistant turn from turn
    N exactly as it was, or the model sees its own tool call vanish and repeats
    it for ever.
    """

    tool_name: str | None = None
    """Which tool a TOOL message answers. Providers match replies by name."""


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What a provider returned, with what it consumed to return it."""

    text: str
    usage: ModelUsage

    tool_calls: tuple[ToolCall, ...] = ()
    """Tools the model asked to run before it can answer.

    Empty for a provider that was offered none, and for a turn where the model
    chose to answer directly. A reply may carry both text and tool calls.
    """

    truncated: bool = False
    """Whether the provider stopped at the output cap rather than finishing.

    A fact about the reply, not about the model's ability. A truncated reply is
    the most likely reason an agent cannot read an edit out of it, and the two
    have different fixes: raise the cap, or change the model. Without this the
    agent can only report that the output was malformed, and a benchmark would
    blame the model for a limit the harness chose.
    """


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    """One completed model call, as it is recorded.

    `cost` is never None here. A locally hosted model records zero, which is a
    measured fact; "we do not know what this cost" is a different statement and
    belongs to a run that was interrupted mid-call, not to a call that
    completed.
    """

    provider_id: str
    model_id: str
    usage: ModelUsage
    cost: MicroUsd
    latency_ms: int

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise PreconditionViolated("latency_ms must be >= 0")
        if self.cost < 0:
            raise PreconditionViolated("cost must be >= 0")


@runtime_checkable
class ModelClient(Protocol):
    """Provider-neutral model interface, owned by the domain (D6).

    Services never learn which implementation they hold, so swapping a local
    model for a hosted API is configuration rather than restructuring. Usage
    accounting is part of the interface from the start: bolted on later, the
    earlier local runs would carry no token counts and nothing would be
    comparable across providers.
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    def complete(
        self,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...] = (),
    ) -> ModelResponse:
        """Answer, optionally being allowed to ask for tools first.

        Defaulted rather than required, so a client that cannot offer tools and
        a caller that does not want them both keep working unchanged.
        """
        ...
