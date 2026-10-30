"""Token prices and the per-run cost cap.

Money is summed as `decimal.Decimal` built from the decimal text of each price (`Decimal(str(x))`),
so a total such as ten additions of 0.1 USD is exactly 1.0 and the "total greater than limit"
test has no binary-float surprises. `total_usd` converts to `float` only for display and logging.
"""

import math
from dataclasses import dataclass
from decimal import Decimal

from cfdagent.llm.client import Usage, usage_is_known

_PER_MILLION = Decimal(1_000_000)
# Without a usage report from the service a token count is estimated from the text length.
# Three characters per token over-counts typical English and code, which errs on the side of
# stopping a run early instead of overspending.
CHARS_PER_TOKEN = 3


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""

    input_per_m: float
    output_per_m: float

    def __post_init__(self) -> None:
        for value in (self.input_per_m, self.output_per_m):
            if not math.isfinite(value) or value < 0:
                raise ValueError("price per million tokens must be finite and not negative")


# Keyed by Foundry deployment name. Pay-per-token global deployments, read from the public Azure
# retail prices API (serviceName "Foundry Models", region italynorth, 2026-10-30): gpt-oss-120B
# 0.00015 / 0.0006 USD per 1K tokens, Llama 3.3 70B 0.00071 / 0.00071 USD per 1K tokens.
# A model that is not listed here needs explicit prices on the command line.
PRICING: dict[str, Pricing] = {
    "gpt-oss-120b": Pricing(input_per_m=0.15, output_per_m=0.60),
    "Llama-3.3-70B-Instruct": Pricing(input_per_m=0.71, output_per_m=0.71),
}


class CostLimitExceeded(Exception):
    """The accumulated LLM cost went above the run's limit."""

    def __init__(self, total_usd: float, limit_usd: float):
        super().__init__(f"LLM cost {total_usd:.6f} USD exceeds the limit of {limit_usd:.6f} USD")
        self.total_usd = total_usd
        self.limit_usd = limit_usd


class CostTracker:
    """Accumulates the cost of model calls and enforces a hard cap.

    `add` records the call first and then raises `CostLimitExceeded` if the total is above the
    limit (a total exactly at the limit is allowed), so the overrun is visible in the totals.
    """

    def __init__(self, limit_usd: float, pricing: Pricing):
        if not math.isfinite(limit_usd) or limit_usd < 0:
            raise ValueError("cost limit must be finite and not negative")
        self._limit = Decimal(str(limit_usd))
        self._limit_float = limit_usd
        self._price_in = Decimal(str(pricing.input_per_m))
        self._price_out = Decimal(str(pricing.output_per_m))
        self._total = Decimal(0)
        self._tokens_in = 0
        self._tokens_out = 0
        self._estimated = False

    def add(self, usage: Usage | None, prompt_chars: int, completion_chars: int) -> None:
        """Record one call. If `usage` is `None` or implausible (see `usage_is_known`) the tokens
        are estimated from the character counts (rounded up) and the tracker is flagged
        `estimated`; the flag never clears.

        Atomic: all new totals are computed first and assigned together, so a failure while
        computing leaves the tracker unchanged. The call is recorded before the cap is checked,
        so the overrun is visible in the totals when `CostLimitExceeded` is raised.
        """
        estimated = not usage_is_known(usage)
        if estimated:
            for chars in (prompt_chars, completion_chars):
                if not isinstance(chars, int) or isinstance(chars, bool):
                    raise TypeError("prompt_chars and completion_chars must be integers")
                if chars < 0:
                    raise ValueError("prompt_chars and completion_chars must not be negative")
            tokens_in = math.ceil(prompt_chars / CHARS_PER_TOKEN)
            tokens_out = math.ceil(completion_chars / CHARS_PER_TOKEN)
        else:
            assert usage is not None
            tokens_in = usage.prompt_tokens
            tokens_out = usage.completion_tokens
        total = (
            self._total + (tokens_in * self._price_in + tokens_out * self._price_out) / _PER_MILLION
        )
        self._tokens_in += tokens_in
        self._tokens_out += tokens_out
        self._estimated = self._estimated or estimated
        self._total = total
        if total > self._limit:
            raise CostLimitExceeded(self.total_usd, self._limit_float)

    @property
    def total_usd(self) -> float:
        return float(self._total)

    @property
    def estimated(self) -> bool:
        return self._estimated

    @property
    def tokens_in(self) -> int:
        return self._tokens_in

    @property
    def tokens_out(self) -> int:
        return self._tokens_out
