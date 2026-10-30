"""Unit tests for the cost tracker. Money arithmetic is exact (Decimal), see cost.py."""

import pytest

from cfdagent.llm.client import Usage
from cfdagent.llm.cost import PRICING, CostLimitExceeded, CostTracker, Pricing

PRICE = Pricing(input_per_m=0.15, output_per_m=0.60)


def usage(prompt: int, completion: int) -> Usage:
    return Usage(prompt_tokens=prompt, completion_tokens=completion, reasoning_tokens=None)


def test_pricing_table():
    assert PRICING["gpt-oss-120b"] == Pricing(0.15, 0.60)
    assert PRICING["Llama-3.3-70B-Instruct"] == Pricing(0.71, 0.71)


def test_cost_tracker_adds_and_caps():
    tracker = CostTracker(limit_usd=1.0, pricing=PRICE)
    assert tracker.total_usd == 0.0
    tracker.add(usage(1_000_000, 500_000), 0, 0)  # 0.15 + 0.30
    assert tracker.total_usd == pytest.approx(0.45)
    assert (tracker.tokens_in, tracker.tokens_out) == (1_000_000, 500_000)
    assert tracker.estimated is False
    tracker.add(usage(0, 900_000), 0, 0)  # +0.54 -> 0.99, still under
    assert tracker.total_usd == pytest.approx(0.99)
    with pytest.raises(CostLimitExceeded) as info:
        tracker.add(usage(0, 20_000), 0, 0)  # +0.012 -> 1.002
    assert info.value.total_usd == pytest.approx(1.002)
    assert info.value.limit_usd == 1.0
    assert tracker.total_usd == pytest.approx(1.002)  # the overrun is recorded, not hidden


def test_exactly_at_the_limit_does_not_raise():
    # Ten additions of 0.1 USD against a 1.0 USD limit: binary floats sum 0.1 ten times to
    # 0.9999999999999999, so a float tracker would be off at the boundary; Decimal is exact.
    tracker = CostTracker(limit_usd=1.0, pricing=Pricing(1.0, 1.0))
    for _ in range(10):
        tracker.add(usage(100_000, 0), 0, 0)
    assert tracker.total_usd == 1.0
    with pytest.raises(CostLimitExceeded):
        tracker.add(usage(1, 0), 0, 0)


def test_cost_cap_with_unknown_usage():
    tracker = CostTracker(limit_usd=0.001, pricing=Pricing(1.0, 1.0))
    tracker.add(None, prompt_chars=300, completion_chars=150)  # 100 + 50 estimated tokens
    assert tracker.estimated is True
    assert (tracker.tokens_in, tracker.tokens_out) == (100, 50)
    assert tracker.total_usd == pytest.approx(0.00015)
    with pytest.raises(CostLimitExceeded):
        tracker.add(None, prompt_chars=3_000_000, completion_chars=0)


def test_estimate_rounds_tokens_up():
    tracker = CostTracker(limit_usd=1.0, pricing=PRICE)
    tracker.add(None, prompt_chars=1, completion_chars=4)
    assert (tracker.tokens_in, tracker.tokens_out) == (1, 2)


def test_estimated_flag_is_sticky_and_known_usage_ignores_chars():
    tracker = CostTracker(limit_usd=1.0, pricing=PRICE)
    tracker.add(usage(10, 20), prompt_chars=9999, completion_chars=9999)
    assert (tracker.tokens_in, tracker.tokens_out) == (10, 20)
    assert tracker.estimated is False
    tracker.add(None, 3, 3)
    tracker.add(usage(1, 1), 0, 0)
    assert tracker.estimated is True


def test_negative_limit_or_prices_are_rejected():
    with pytest.raises(ValueError, match="limit"):
        CostTracker(limit_usd=-1.0, pricing=PRICE)
    with pytest.raises(ValueError, match="price"):
        Pricing(-0.1, 0.5)


@pytest.mark.parametrize(
    "bad",
    [
        Usage(-1, 5, None),
        Usage(5, -1, None),
        Usage("5", 5, None),
        Usage(5, 5.0, None),
        Usage(None, 5, None),
        Usage(5, None, None),
        Usage(True, 5, None),
    ],
)
def test_implausible_usage_is_estimated_from_chars(bad):
    tracker = CostTracker(limit_usd=1.0, pricing=PRICE)
    tracker.add(bad, prompt_chars=30, completion_chars=60)
    assert tracker.estimated is True
    assert (tracker.tokens_in, tracker.tokens_out) == (10, 20)


def test_zero_usage_is_real_usage():
    tracker = CostTracker(limit_usd=1.0, pricing=PRICE)
    tracker.add(usage(0, 0), prompt_chars=300, completion_chars=300)
    assert tracker.estimated is False
    assert (tracker.tokens_in, tracker.tokens_out, tracker.total_usd) == (0, 0, 0.0)


def test_add_is_atomic_when_it_fails_before_recording():
    tracker = CostTracker(limit_usd=1.0, pricing=PRICE)
    tracker.add(usage(100, 100), 0, 0)
    before = (tracker.total_usd, tracker.tokens_in, tracker.tokens_out, tracker.estimated)
    with pytest.raises(TypeError):
        tracker.add(None, prompt_chars="many", completion_chars=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="chars"):
        tracker.add(None, prompt_chars=-1, completion_chars=3)
    after = (tracker.total_usd, tracker.tokens_in, tracker.tokens_out, tracker.estimated)
    assert after == before


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), -0.5])
def test_pricing_rejects_non_finite_and_negative(bad):
    with pytest.raises(ValueError, match="price"):
        Pricing(bad, 1.0)
    with pytest.raises(ValueError, match="price"):
        Pricing(1.0, bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), -1.0])
def test_cost_limit_rejects_non_finite_and_negative(bad):
    with pytest.raises(ValueError, match="limit"):
        CostTracker(limit_usd=bad, pricing=PRICE)
