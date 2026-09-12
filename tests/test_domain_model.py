"""Cost arithmetic and the model port."""

from __future__ import annotations

import pytest

from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.model import (
    Message,
    MessageRole,
    MicroUsd,
    MicroUsdPerMTok,
    ModelClient,
    ModelInvocation,
    ModelResponse,
    ModelUsage,
    TokenPrice,
    ceil_div,
    cost_micros,
    total_micros,
)

# $3.00 / Mtok in, $15.00 / Mtok out -- the shape of a real published price,
# exactly representable because the unit is per million tokens.
PAID = TokenPrice(
    model_id="paid-model",
    input_per_mtok=MicroUsdPerMTok(3_000_000),
    output_per_mtok=MicroUsdPerMTok(15_000_000),
)


def test_a_quarter_dollar_per_mtok_is_an_exact_integer() -> None:
    # The reason prices are per million tokens. Per token, $0.25/Mtok is 0.25
    # micro-dollars -- not an integer, so the price itself could not be written
    # down without rounding before any usage was involved.
    cheap = TokenPrice(
        model_id="cheap",
        input_per_mtok=MicroUsdPerMTok(250_000),
        output_per_mtok=MicroUsdPerMTok(1_250_000),
    )
    assert cost_micros(ModelUsage(input_tokens=1_000_000), cheap) == 250_000


def test_cost_of_a_realistic_call() -> None:
    usage = ModelUsage(input_tokens=12_000, output_tokens=800)
    # 12_000 * 3_000_000 // 1e6 = 36_000 ; 800 * 15_000_000 // 1e6 = 12_000
    assert cost_micros(usage, PAID) == MicroUsd(48_000)


def test_cost_rounds_up_never_down() -> None:
    # One token at $3/Mtok is 3 micro-dollars; one token at a price that does
    # not divide evenly must still not round to zero, or a long tail of tiny
    # calls would be recorded as free.
    tiny = TokenPrice(
        model_id="tiny",
        input_per_mtok=MicroUsdPerMTok(1),
        output_per_mtok=MicroUsdPerMTok(0),
    )
    assert cost_micros(ModelUsage(input_tokens=1), tiny) == 1


def test_rounding_up_is_the_safe_direction_for_a_spend_ceiling() -> None:
    # The number feeds a budget guard. Under-estimating lets a benchmark run
    # past its ceiling; over-estimating stops it early.
    half = TokenPrice(
        model_id="half",
        input_per_mtok=MicroUsdPerMTok(500_000),
        output_per_mtok=MicroUsdPerMTok(0),
    )
    assert cost_micros(ModelUsage(input_tokens=1), half) == 1  # not 0


def test_a_run_total_equals_the_sum_of_its_calls() -> None:
    # Rounding per line item rather than once at the end is what makes this
    # hold. If it did not, a report's per-call column would not add up to its
    # own total, and a reader would rightly distrust every other figure.
    calls = [
        ModelUsage(input_tokens=1_234, output_tokens=57),
        ModelUsage(input_tokens=987, output_tokens=3),
        ModelUsage(input_tokens=1, output_tokens=1),
    ]
    per_call = [cost_micros(u, PAID) for u in calls]
    assert total_micros(per_call) == sum(per_call)


def test_cache_rates_are_separate_line_items_not_a_multiplier() -> None:
    # Providers quote cache writes and reads as fractions of the base rate.
    # Expressing that as a float multiplier would put floating point back in
    # exactly the place this module removes it.
    priced = TokenPrice(
        model_id="cached",
        input_per_mtok=MicroUsdPerMTok(3_000_000),
        output_per_mtok=MicroUsdPerMTok(15_000_000),
        cache_write_per_mtok=MicroUsdPerMTok(3_750_000),  # 1.25x base
        cache_read_per_mtok=MicroUsdPerMTok(300_000),  # 0.10x base
    )
    usage = ModelUsage(cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert cost_micros(usage, priced) == 3_750_000 + 300_000


# --- the local model and the paid API produce the same record ----------------


def test_a_local_model_costs_zero_but_still_records_real_tokens() -> None:
    # Zero is a measured fact: this run cost nothing. It is not the same
    # statement as "we do not know what this cost", which belongs only to a run
    # interrupted mid-call.
    usage = ModelUsage(input_tokens=9_000, output_tokens=1_200)
    free = TokenPrice.free("qwen2.5-coder:7b")

    assert cost_micros(usage, free) == 0
    assert usage.total_tokens == 10_200


def test_local_and_paid_invocations_are_structurally_identical() -> None:
    # D6: accounting is part of the interface from the start, precisely so this
    # holds on day one. Bolted on later, the local runs would carry no token
    # counts and nothing would be comparable across providers.
    usage = ModelUsage(input_tokens=5_000, output_tokens=500)

    local = ModelInvocation(
        provider_id="ollama",
        model_id="qwen2.5-coder:7b",
        usage=usage,
        cost=cost_micros(usage, TokenPrice.free("qwen2.5-coder:7b")),
        latency_ms=4_200,
    )
    paid = ModelInvocation(
        provider_id="anthropic",
        model_id="paid-model",
        usage=usage,
        cost=cost_micros(usage, PAID),
        latency_ms=1_100,
    )

    assert local.usage == paid.usage
    assert local.cost == 0
    assert paid.cost > 0
    assert type(local) is type(paid)


# --- arithmetic and validation ----------------------------------------------


def test_usage_adds_componentwise() -> None:
    a = ModelUsage(input_tokens=1, output_tokens=2, cache_read_tokens=3)
    b = ModelUsage(input_tokens=10, output_tokens=20, cache_write_tokens=30)
    total = a + b

    assert total.input_tokens == 11
    assert total.output_tokens == 22
    assert total.cache_write_tokens == 30
    assert total.cache_read_tokens == 3


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"],
)
def test_negative_token_counts_are_rejected(field: str) -> None:
    with pytest.raises(PreconditionViolated, match=field):
        ModelUsage(**{field: -1})


def test_negative_prices_are_rejected() -> None:
    with pytest.raises(PreconditionViolated, match="input_per_mtok"):
        TokenPrice(
            model_id="m",
            input_per_mtok=MicroUsdPerMTok(-1),
            output_per_mtok=MicroUsdPerMTok(0),
        )


@pytest.mark.parametrize(("n", "d"), [(-1, 10), (10, 0), (10, -1)])
def test_ceil_div_rejects_operands_it_cannot_round_correctly(n: int, d: int) -> None:
    # -(-n // d) silently gives the wrong answer for negative operands, so the
    # guard is the only thing standing between a sign error and a plausible
    # wrong cost.
    with pytest.raises(PreconditionViolated):
        ceil_div(n, d)


def test_an_invocation_cannot_have_negative_latency_or_cost() -> None:
    with pytest.raises(PreconditionViolated, match="latency"):
        ModelInvocation(
            provider_id="p",
            model_id="m",
            usage=ModelUsage(),
            cost=MicroUsd(0),
            latency_ms=-1,
        )


# --- the port ----------------------------------------------------------------


def test_a_fake_provider_satisfies_the_model_port() -> None:
    class FakeModel:
        provider_id = "fake"
        model_id = "scripted"

        def complete(self, messages: tuple[Message, ...]) -> ModelResponse:
            return ModelResponse(
                text=f"saw {len(messages)} messages",
                usage=ModelUsage(input_tokens=10, output_tokens=5),
            )

    client: ModelClient = FakeModel()
    assert isinstance(client, ModelClient)

    response = client.complete((Message(MessageRole.USER, "fix the parser"),))
    assert response.usage.total_tokens == 15
