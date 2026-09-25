"""Cost estimation, with its uncertainty stated rather than hidden.

Every number here is a *published list price at a point in time*, and vendors
change them. So the contract of this module is:

  * a model that is in the table gets an estimate marked `priced: true`;
  * a model that is not gets `0.0` and `priced: false`, never a guessed number;
  * the table carries the date it was last checked, and the API returns it.

An invented figure in a spend dashboard is worse than a blank, because someone
will budget against it. A blank prompts them to enter their own rate, which
`PRICE_OVERRIDES` supports through configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: When the list prices below were last checked against vendor documentation.
#: Surfaced in the API so nobody treats a stale number as current.
PRICES_AS_OF = "2026-05-01"

TOKENS_PER_UNIT = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input_per_m: float
    output_per_m: float
    cached_input_per_m: float | None = None


#: Prefix-matched, longest prefix wins, so a dated model id
#: ("claude-sonnet-4-5-20260101") inherits its family's price.
LIST_PRICES: dict[str, ModelPrice] = {
    # Anthropic
    "claude-opus-4": ModelPrice(15.0, 75.0, 1.5),
    "claude-sonnet-4": ModelPrice(3.0, 15.0, 0.3),
    "claude-haiku-4": ModelPrice(1.0, 5.0, 0.1),
    "claude-3-5-haiku": ModelPrice(0.8, 4.0, 0.08),
    "claude-3-5-sonnet": ModelPrice(3.0, 15.0, 0.3),
    "claude-3-opus": ModelPrice(15.0, 75.0, 1.5),
    # OpenAI
    "gpt-4o-mini": ModelPrice(0.15, 0.6, 0.075),
    "gpt-4o": ModelPrice(2.5, 10.0, 1.25),
    "gpt-4-turbo": ModelPrice(10.0, 30.0),
    "gpt-4.1-mini": ModelPrice(0.4, 1.6, 0.1),
    "gpt-4.1": ModelPrice(2.0, 8.0, 0.5),
    "o3-mini": ModelPrice(1.1, 4.4),
    "o3": ModelPrice(2.0, 8.0),
    # Google
    "gemini-2.5-pro": ModelPrice(1.25, 10.0),
    "gemini-2.5-flash": ModelPrice(0.3, 2.5),
    "gemini-2.0-flash": ModelPrice(0.1, 0.4),
    # Mistral
    "mistral-large": ModelPrice(2.0, 6.0),
    "mistral-medium": ModelPrice(0.4, 2.0),
    "mistral-small": ModelPrice(0.1, 0.3),
    # DeepSeek
    "deepseek-chat": ModelPrice(0.27, 1.1, 0.07),
    "deepseek-reasoner": ModelPrice(0.55, 2.19, 0.14),
}

#: Self-hosted inference has no per-token vendor charge. It is priced at zero
#: and marked as priced, because zero is the true marginal API cost — the
#: infrastructure cost is real but is not a per-token number this can know.
FREE_PROVIDERS = frozenset({"OLLAMA"})


def _match(model: str) -> ModelPrice | None:
    name = (model or "").lower()
    best: tuple[int, ModelPrice] | None = None
    for prefix, price in LIST_PRICES.items():
        if name.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), price)
    return best[1] if best else None


def estimate(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> dict[str, Any]:
    """Estimate the cost of one call.

    Returns `priced: False` and a zero cost when the model is unknown. That is
    deliberate: a spend figure someone will act on must not be invented.
    """
    if provider.upper() in FREE_PROVIDERS:
        return {
            "cost_usd": 0.0,
            "priced": True,
            "basis": "self-hosted; no per-token vendor charge",
            "as_of": PRICES_AS_OF,
        }

    price = _match(model)
    if price is None:
        return {
            "cost_usd": 0.0,
            "priced": False,
            "basis": (
                f"No list price on file for '{model}'. Token counts are exact; "
                "the cost is not estimated rather than guessed."
            ),
            "as_of": PRICES_AS_OF,
        }

    billable_input = max(0, input_tokens - cached_tokens)
    cost = (billable_input / TOKENS_PER_UNIT) * price.input_per_m
    cost += (output_tokens / TOKENS_PER_UNIT) * price.output_per_m
    if cached_tokens and price.cached_input_per_m is not None:
        cost += (cached_tokens / TOKENS_PER_UNIT) * price.cached_input_per_m
    elif cached_tokens:
        cost += (cached_tokens / TOKENS_PER_UNIT) * price.input_per_m

    return {
        "cost_usd": round(cost, 6),
        "priced": True,
        "basis": f"published list price as of {PRICES_AS_OF}",
        "as_of": PRICES_AS_OF,
    }


def describe() -> dict[str, Any]:
    """What the billing screen shows about where these numbers come from."""
    return {
        "as_of": PRICES_AS_OF,
        "unit": "USD per million tokens",
        "model_count": len(LIST_PRICES),
        "note": (
            "Estimates use published list prices captured on the date above. "
            "They are not invoices: negotiated rates, committed-use discounts "
            "and price changes are not reflected. A model with no price on file "
            "reports exact token counts and no cost."
        ),
    }
