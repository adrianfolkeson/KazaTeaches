"""Single Anthropic client + the one call shape the whole app uses:
structured output parsed into a pydantic model."""

from __future__ import annotations

from typing import TypeVar

import anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


class AIError(RuntimeError):
    pass


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

    try:
        response = client().messages.parse(**kwargs)
    except anthropic.AuthenticationError as e:
        raise AIError("No usable Anthropic credentials — set ANTHROPIC_API_KEY.") from e
    except anthropic.RateLimitError as e:
        raise AIError("Rate limited by the Anthropic API — retry shortly.") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError(f"Could not reach the Anthropic API: {e}") from e

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None)
        raise AIError(f"Model declined the request (category={detail}).")
    if response.parsed_output is None:
        raise AIError(f"Model returned no parseable output (stop_reason={response.stop_reason}).")
    return response.parsed_output


def cached(text: str) -> dict:
    """A system block marked for prompt caching.

    Only prefixes above the ~1024-token minimum actually cache; below that this
    is a no-op rather than an error, which is why it is safe to mark the short
    grader instructions too — they cache once the rubric guidance grows.
    """
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def block(text: str) -> dict:
    return {"type": "text", "text": text}
