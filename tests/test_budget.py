"""Tests for the spend cap. The cap only matters if it actually stops a call,
so these assert refusal and metering — not the arithmetic of a price table."""

from __future__ import annotations

import pytest

from app.ai import client as ai_client
from app.budget import BudgetExceeded, PRICES, cost_usd, current_month
from app.config import settings
from app.store import MemoryStore


class Usage:
    def __init__(self, inp=0, out=0, cache_read=0, cache_write=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


@pytest.fixture
def metered(monkeypatch):
    """A store wired to the cap, restored afterwards so the meter never leaks
    into another test."""
    store = MemoryStore()
    ai_client.set_meter(store.month_spend, store.record_spend)
    yield store
    ai_client.set_meter(None, None)


def test_an_unpriced_model_is_an_error_not_a_free_call():
    """Silently costing nothing is how a model escapes the cap entirely."""
    with pytest.raises(ValueError, match="No price listed"):
        cost_usd("claude-imaginary-9", Usage(inp=1000))


def test_cached_input_is_charged_at_a_tenth_of_fresh_input():
    fresh = cost_usd("claude-opus-5", Usage(inp=10_000))
    cached = cost_usd("claude-opus-5", Usage(cache_read=10_000))
    assert cached == pytest.approx(fresh * 0.10)


def test_output_costs_more_than_input_on_every_priced_model():
    """Thinking is billed as output. If a price table ever inverts these, the
    cost model in the README stops being true."""
    for model, price in PRICES.items():
        assert price.output > price.input, model


def test_the_ledger_sums_only_the_current_month(metered):
    metered.record_spend("claude-opus-5", 1.50, Usage(inp=100, out=100))
    metered.record_spend("claude-opus-5", 0.25, Usage(inp=100, out=100))
    metered.spend[0]["month"] = "1999-01"  # an older month must not count
    assert metered.month_spend() == pytest.approx(0.25)
    assert metered.month_spend("1999-01") == pytest.approx(1.50)


def test_a_call_over_the_cap_is_refused_before_it_is_made(metered, monkeypatch):
    """The point of the cap: the API is never reached, so no money is spent."""
    called = False

    def explode(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("the API must not be called once the cap is reached")

    monkeypatch.setattr(ai_client, "client", lambda: type("C", (), {
        "messages": type("M", (), {"stream": staticmethod(explode)})()
    })())
    metered.record_spend("claude-opus-5", settings.monthly_budget_usd, Usage())

    with pytest.raises(BudgetExceeded) as exc:
        ai_client.parse(
            model="claude-opus-5", system=[], user="x",
            output_format=type("X", (), {}), max_tokens=10,
        )
    assert not called
    assert exc.value.month == current_month()
    assert exc.value.cap == settings.monthly_budget_usd


def test_spending_just_under_the_cap_still_goes_through(metered, monkeypatch):
    """A cap that trips early is a cap you route around."""
    reached = False

    def fake_parse(**kwargs):
        nonlocal reached
        reached = True
        raise RuntimeError("reached the API")  # far enough for this test

    monkeypatch.setattr(ai_client, "client", lambda: type("C", (), {
        "messages": type("M", (), {"stream": staticmethod(fake_parse)})()
    })())
    metered.record_spend("claude-opus-5", settings.monthly_budget_usd - 0.01, Usage())

    with pytest.raises(RuntimeError, match="reached the API"):
        ai_client.parse(
            model="claude-opus-5", system=[], user="x",
            output_format=type("X", (), {}), max_tokens=10,
        )
    assert reached


def test_the_breakdown_separates_the_expensive_lane_from_the_cheap_one(metered):
    metered.record_spend("claude-opus-5", 2.00, Usage(out=1000))
    metered.record_spend("claude-opus-5", 1.00, Usage(out=500))
    metered.record_spend("claude-haiku-4-5", 0.05, Usage(out=100))

    rows = metered.spend_breakdown()
    assert [r["model"] for r in rows] == ["claude-opus-5", "claude-haiku-4-5"]
    assert rows[0] == {"model": "claude-opus-5", "calls": 2, "cost_usd": pytest.approx(3.0)}


def test_generation_is_refused_up_front_rather_than_stranded_halfway(monkeypatch):
    """Item generation is a call per concept. A batch that runs out of budget
    halfway returns a draft quietly missing its last concepts' items, so the
    check has to happen after extraction and before the first item."""
    from fastapi.testclient import TestClient

    from app import main
    from app.ai import generation
    from app.schemas import DraftConcept

    store = MemoryStore()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(
        generation, "extract_concepts",
        lambda text, model=None: [
            DraftConcept(name=f"c{i}", importance="supporting", short_explanation="…")
            for i in range(20)
        ],
    )
    monkeypatch.setattr(
        generation, "generate_items",
        lambda *a, **k: pytest.fail("no item may be generated once the batch is refused"),
    )

    with TestClient(main.app) as client:
        store.record_spend("claude-opus-5", settings.monthly_budget_usd - 0.10, Usage())
        r = client.post("/api/generate", json={"text": "x" * 400})

    assert r.status_code == 402
    assert store.concepts == {}, "a refused generation must not leave rows behind"


def test_a_truncated_response_says_it_ran_out_of_room(monkeypatch):
    """stop_reason=max_tokens means the ceiling covered thinking as well as
    output and the answer stopped mid-JSON. 'no parseable output' names the
    symptom; the ceiling is the cause and the only thing you can act on."""
    from app.ai import client as c

    class Truncated:
        stop_reason = "max_tokens"
        parsed_output = None
        usage = None

    class Stream:
        """messages.stream() is a context manager, not a plain call."""

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self): return Truncated()

    monkeypatch.setattr(c, "client", lambda: type("C", (), {
        "messages": type("M", (), {"stream": staticmethod(lambda **kw: Stream())})()
    })())

    with pytest.raises(c.AIError, match="ran out of room"):
        c.parse(model="claude-opus-5", system=[], user="x",
                output_format=type("X", (), {}), max_tokens=8000)


def test_the_generation_ceiling_leaves_room_for_thinking():
    """Regression: 8000 truncated concept extraction on a real import. These
    calls run at effort=high, where reasoning routinely outweighs the answer."""
    from app.ai.generation import GENERATION_MAX_TOKENS

    assert GENERATION_MAX_TOKENS >= 32000


def test_an_sdk_usage_error_is_not_reported_as_a_bad_rubric(monkeypatch):
    """The SDK raises plain ValueError for its own usage errors. Uncaught, it
    lands in generation.py's rubric validation and is reported as 'Generation
    produced an invalid item' — sending the reader to inspect a rubric for a
    fault that is in the request."""
    from app.ai import client as c

    def refuse(**kwargs):
        raise ValueError("Streaming is required for operations that may take longer than 10 minutes.")

    monkeypatch.setattr(c, "client", lambda: type("C", (), {
        "messages": type("M", (), {"stream": staticmethod(refuse)})()
    })())

    with pytest.raises(c.AIError, match="SDK rejected the request"):
        c.parse(model="claude-opus-5", system=[], user="x",
                output_format=type("X", (), {}), max_tokens=32000)


def test_an_overloaded_response_is_retried(monkeypatch):
    """overloaded_error arrives inside an HTTP 200 over a stream, so the SDK's
    own retry — which keys on the status code — never sees it."""
    import anthropic

    from app.ai import client as c

    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Parsed:
        stop_reason = "end_turn"
        parsed_output = "ok"
        usage = None

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise anthropic.APIStatusError(
                    "Overloaded", response=httpx_response(200), body={"type": "overloaded_error"})
            return Parsed()

    monkeypatch.setattr(c, "client", lambda: type("C", (), {
        "messages": type("M", (), {"stream": staticmethod(lambda **kw: Stream())})()
    })())

    out = c.parse(model="claude-opus-5", system=[], user="x",
                  output_format=type("X", (), {}), max_tokens=32000)
    assert out == "ok"
    assert calls["n"] == 3, "it should have taken two retries"


def test_a_bad_request_is_not_retried(monkeypatch):
    """Retrying a 400 wastes time on something that will never succeed."""
    import anthropic

    from app.ai import client as c

    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self):
            calls["n"] += 1
            raise anthropic.APIStatusError(
                "bad request", response=httpx_response(400), body={"type": "invalid_request_error"})

    monkeypatch.setattr(c, "client", lambda: type("C", (), {
        "messages": type("M", (), {"stream": staticmethod(lambda **kw: Stream())})()
    })())

    with pytest.raises(c.AIError):
        c.parse(model="claude-opus-5", system=[], user="x",
                output_format=type("X", (), {}), max_tokens=32000)
    assert calls["n"] == 1, "a 400 must not be retried"


def httpx_response(status: int):
    import httpx2 as httpx

    return httpx.Response(status_code=status, request=httpx.Request("POST", "https://x"))
