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
        "messages": type("M", (), {"parse": staticmethod(explode)})()
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
        "messages": type("M", (), {"parse": staticmethod(fake_parse)})()
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
