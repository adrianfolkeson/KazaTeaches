"""The spend cap. §5 is a cost *architecture*; this is the enforcement.

Two numbers matter and they are different things:

  target  — what normal daily use should land near (KT_MONTHLY_TARGET_USD).
            Advisory. Crossing it changes nothing except what /api/budget says.
  cap     — the hard ceiling (KT_MONTHLY_BUDGET_USD). Reaching it stops every
            paid call for the rest of the calendar month.

The cap is enforced in app/ai/client.py, the single function every Anthropic
call goes through, so there is no route that can spend around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


class BudgetExceeded(RuntimeError):
    """Raised instead of making a paid call. Carries the numbers so the API
    layer can tell the user how long they are locked out and by how much."""

    def __init__(self, spent: float, cap: float, month: str) -> None:
        self.spent, self.cap, self.month = spent, cap, month
        super().__init__(
            f"Monthly budget reached: ${spent:.2f} of ${cap:.2f} spent in {month}. "
            f"Raise KT_MONTHLY_BUDGET_USD or wait for the next month."
        )


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float

    # Writing to the prompt cache costs ~1.25x the input rate; reading from it
    # ~0.1x. Both are derived rather than listed, because they track the input
    # rate by definition.
    @property
    def cache_write(self) -> float:
        return self.input * 1.25

    @property
    def cache_read(self) -> float:
        return self.input * 0.10


# Anthropic first-party rates. Only models this app can actually be pointed at.
# An unlisted model is an error, not a free call — see cost_usd().
PRICES: dict[str, Price] = {
    "claude-fable-5": Price(input=10.0, output=50.0),
    "claude-opus-5": Price(input=5.0, output=25.0),
    "claude-opus-4-8": Price(input=5.0, output=25.0),
    "claude-opus-4-7": Price(input=5.0, output=25.0),
    "claude-opus-4-6": Price(input=5.0, output=25.0),
    "claude-sonnet-5": Price(input=2.0, output=10.0),
    "claude-sonnet-4-6": Price(input=3.0, output=15.0),
    "claude-haiku-4-5": Price(input=1.0, output=5.0),
}


# Measured on claude-opus-5 / claude-haiku-4-5, August 2026, with the prompts in
# app/ai/prompts.py. Re-measure after a prompt or model change — these drive the
# pre-flight estimate below, and a stale number turns a guard into a nuisance.
GRADING_USD_PER_REVIEW = 0.011      # range seen: 0.008 - 0.017
ITEM_GEN_USD_PER_CONCEPT = 0.067    # 3-4 items with rubrics, effort=high


def estimate_import_usd(n_concepts: int) -> float:
    """What generating items for this many concepts will cost.

    Used to refuse an import up front rather than let it run out of budget
    halfway and leave a course with concepts but no questions.
    """
    return round(n_concepts * ITEM_GEN_USD_PER_CONCEPT, 4)


def current_month(now: datetime | None = None) -> str:
    """Calendar month in UTC. The billing period is the month, not a rolling
    30 days, so the cap resets on the same boundary the invoice does."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def cost_usd(model: str, usage: object) -> float:
    """Cost of one call, from the SDK's usage object.

    Thinking tokens are billed as output tokens at the output rate and are
    already counted in `output_tokens` — there is nothing extra to add.
    """
    price = PRICES.get(model)
    if price is None:
        raise ValueError(
            f"No price listed for model {model!r}. Add it to app/budget.PRICES — "
            f"an untracked model would spend against the cap invisibly."
        )

    def tokens(name: str) -> int:
        return getattr(usage, name, None) or 0

    return (
        tokens("input_tokens") * price.input
        + tokens("output_tokens") * price.output
        + tokens("cache_creation_input_tokens") * price.cache_write
        + tokens("cache_read_input_tokens") * price.cache_read
    ) / 1_000_000
