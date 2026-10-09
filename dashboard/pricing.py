"""Offline estimates using a dated snapshot of public Standard API rates."""

from __future__ import annotations

CHECKED_AT = "2026-10-08"
SOURCE = "https://developers.openai.com/api/docs/pricing"
CACHE_SOURCE = "https://developers.openai.com/api/docs/guides/prompt-caching"
LONG_CONTEXT_THRESHOLD = 272_000

# USD per million tokens: uncached input, cache reads, cache writes, output.
# GPT-5.5 has no cache-write premium; writes use the ordinary input rate.
_RATES = {
    "gpt-6.1-sol": (2, 0.10, 2.50, 10),
    "gpt-6-sol": (2, 0.20, 2.50, 10),
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.50),
    "gpt-6-astra": (10, 1, 12.50, 50),
    "gpt-5.6-sol": (4, 0.40, 5, 20),
    "gpt-5.6-terra": (2, 0.20, 2.50, 12),
    "gpt-5.6-luna": (0.20, 0.02, 0.25, 1.20),
    "gpt-5.5": (5, 0.50, 5, 30),
}
RATES = {
    model: dict(zip(("input", "cached_input", "cache_write", "output"), prices))
    for model, prices in _RATES.items()
}
METADATA = {
    "currency": "USD",
    "basis": "Current Standard API rates; estimated token cost, not an invoice",
    "checked_at": CHECKED_AT,
    "source": SOURCE,
    "cache_source": CACHE_SOURCE,
    "rates_per_million": RATES,
    "assumptions": (
        "Current rates applied to all recorded history. Standard processing assumed: "
        "service tier and region are not recorded. Excludes tool fees, taxes, "
        "regional premiums, Fast/Ultrafast premiums, and contract discounts. "
        "Long-context rates apply above 272,000 input tokens per request "
        "(for GPT-5.5, across that model's session). Reasoning is already in output. "
        "Unknown models/providers or invalid counters remain unpriced."
    ),
}


def estimate_usd(
    model: str, input_tokens: int, cached_input_tokens: int,
    cache_write_input_tokens: int, output_tokens: int, *, long_session: bool = False,
) -> float | None:
    """Price disjoint input categories and inclusive output, never total_tokens."""
    rates = _RATES.get(model)
    counts = (input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens)
    if rates is None or any(not isinstance(n, int) or n < 0 for n in counts):
        return None
    ordinary = input_tokens - cached_input_tokens - cache_write_input_tokens
    if ordinary < 0:
        return None
    long_context = long_session if model == "gpt-5.5" else input_tokens > LONG_CONTEXT_THRESHOLD
    input_multiplier = 2 if long_context else 1
    output_multiplier = 1.5 if long_context else 1
    return (
        (ordinary * rates[0] + cached_input_tokens * rates[1]
         + cache_write_input_tokens * rates[2]) * input_multiplier
        + output_tokens * rates[3] * output_multiplier
    ) / 1_000_000
