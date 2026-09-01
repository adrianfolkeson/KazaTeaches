"""Single Anthropic client + the one call shape the whole app uses:
structured output parsed into a pydantic model.

Every paid call in the app goes through parse(), which is also where the
monthly spend cap is enforced and where usage is metered. Adding a second call
path would put spending outside the cap — don't.
"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

import anthropic
from pydantic import BaseModel

from app.budget import BudgetExceeded, cost_usd, current_month
from app.config import settings

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None

# Set by app.main at startup. A callable rather than an import so this module
# does not depend on the store, and so tests can meter into a fake.
#   spend_reader() -> USD already spent this month
#   spend_writer(model, cost, usage) -> None
_spend_reader: Callable[[], float] | None = None
_spend_writer: Callable[[str, float, object], None] | None = None


def set_meter(reader, writer) -> None:
    """Wire the cap to a ledger. Until this is called, calls are unmetered and
    uncapped — which is what the eval runner and the unit tests want."""
    global _spend_reader, _spend_writer
    _spend_reader, _spend_writer = reader, writer


class AIError(RuntimeError):
    pass


def _why(exc: BaseException, depth: int = 4) -> str:
    """Unwrap the __cause__ chain into one line.

    An SDK error that says only "Connection error." is unactionable, and on a
    platform you cannot attach a debugger to, the exception chain is the only
    evidence there is.
    """
    parts, seen = [], set()
    cur: BaseException | None = exc
    while cur is not None and len(parts) < depth and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).strip()
        label = type(cur).__name__
        parts.append(f"{label}: {text}" if text else label)
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth login`
        # profile. Do not pass a key explicitly.
        _client = anthropic.Anthropic()
    return _client


def parse(
    *,
    model: str,
    system: list[dict],
    user: str,
    output_format: type[T],
    max_tokens: int = 16000,
    thinking: bool = True,
    effort: str | None = "high",
) -> T:
    """One structured call. `system` is a list of content blocks so callers can
    place `cache_control` on the stable prefix themselves (§5)."""
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_format": output_format,
    }
    # Small/cheap models reject thinking + effort; the caller decides.
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    if effort:
        kwargs["output_config"] = {"effort": effort}

    # Check before spending, not after. The window between this check and the
    # call means two concurrent requests can both pass at the boundary and
    # overshoot by one call — a few cents for a single-user app, and the
    # alternative (a lock around every API call) costs more than it saves.
    if _spend_reader is not None:
        spent = _spend_reader()
        if spent >= settings.monthly_budget_usd:
            raise BudgetExceeded(spent, settings.monthly_budget_usd, current_month())

    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return _attempt(kwargs, model, max_tokens, output_format)
        except AIError:
            raise
        except anthropic.APIConnectionError as e:
            if attempt == MAX_ATTEMPTS - 1:
                # httpx reports "Connection error." and hides the reason in
                # __cause__; the difference between DNS, TLS and a dropped
                # socket is the whole diagnosis.
                raise AIError(f"Could not reach the Anthropic API: {_why(e)}") from e
            last = e
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 1.5))
            continue
        except anthropic.APIStatusError as e:
            if not _is_transient(e) or attempt == MAX_ATTEMPTS - 1:
                raise AIError(f"Anthropic API error {e.status_code}: {e.message}") from e
            last = e
            # Exponential, with jitter so six concurrent generations do not all
            # come back at the same instant and overload it again.
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 1.5))
    raise AIError(f"Anthropic API stayed unavailable after {MAX_ATTEMPTS} attempts: {last}")


def _attempt(kwargs: dict, model: str, max_tokens: int, output_format: type[T]) -> T:
    try:
        # Streaming, always. The SDK refuses a non-streaming request whose
        # max_tokens could outlast a 10-minute HTTP timeout, and these ceilings
        # are high because thinking is billed against them. Nothing here reads
        # the incremental events — get_final_message() waits for the whole
        # answer — so the only thing streaming changes is that the connection
        # stays alive while the model works.
        with client().messages.stream(**kwargs) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise AIError("No usable Anthropic credentials — set ANTHROPIC_API_KEY.") from e
    # RateLimitError and APIConnectionError are deliberately NOT caught here:
    # both are worth another attempt, and the retry loop in parse() owns them.
    # Catching them here would convert them to AIError, which that loop re-raises
    # immediately — turning the most retryable errors there are into fatal ones.
    except ValueError as e:
        # The SDK raises plain ValueError for its own usage errors. Left alone
        # it lands in generation.py's rubric validation and is reported as
        # "Generation produced an invalid item", sending the reader to inspect
        # a rubric for a fault that is in the request.
        raise AIError(f"The Anthropic SDK rejected the request: {_why(e)}") from e

    # Meter first: the tokens are billed whether or not the response parses, so
    # a refusal or a malformed output must still count against the cap.
    if _spend_writer is not None and response.usage is not None:
        _spend_writer(model, cost_usd(model, response.usage), response.usage)

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None)
        raise AIError(f"Model declined the request (category={detail}).")
    if response.parsed_output is None:
        if response.stop_reason == "max_tokens":
            # Thinking tokens are billed and counted as output, so a generous
            # `effort` eats the ceiling before the structured answer begins.
            # The response is truncated mid-JSON: nothing to parse, and the
            # tokens are charged anyway.
            raise AIError(
                f"The model ran out of room before finishing its answer "
                f"(max_tokens={max_tokens} covers thinking as well as output). "
                f"Raise max_tokens for this call or lower its effort."
            )
        raise AIError(f"Model returned no parseable output (stop_reason={response.stop_reason}).")
    return response.parsed_output


# Anthropic-side capacity, not our fault and not our rate limit. Over a stream
# it arrives as an error event inside an HTTP 200, so the SDK's own retry — which
# keys on the status code — never sees it.
RETRYABLE = ("overloaded", "overloaded_error", "api_error", "internal server error")
MAX_ATTEMPTS = 4


def _is_transient(e: anthropic.APIStatusError) -> bool:
    body = f"{getattr(e, 'message', '')} {e}".lower()
    return e.status_code in (429, 500, 502, 503, 529) or any(m in body for m in RETRYABLE)


def cached(text: str) -> dict:
    """A system block marked for prompt caching.

    Only prefixes above the ~1024-token minimum actually cache; below that this
    is a no-op rather than an error, which is why it is safe to mark the short
    grader instructions too — they cache once the rubric guidance grows.
    """
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def block(text: str) -> dict:
    return {"type": "text", "text": text}
