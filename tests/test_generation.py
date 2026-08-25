"""Tests for the generator and the review gate.

The generator's *quality* is checked by evals/gen_selfcheck.py, which grades
each reference answer against its own rubric. These tests cover the parts that
must hold without calling a model: the contract shape, the structural rejects,
and the rule that nothing reaches the database unconfirmed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.ai import generation
from app.schemas import (
    ITEMS_PER_IMPORTANCE,
    DraftConcept,
    DraftItem,
    RubricCriterion,
)
from app.store import MemoryStore


def item(prompt: str = "Vad är en transaktion?") -> DraftItem:
    return DraftItem(
        type="definition",
        prompt=prompt,
        reference_answer="En atomär enhet som avslutas med commit eller rollback.",
        rubric=[
            RubricCriterion(id="atomicity", required=True, desc="allt eller inget"),
            RubricCriterion(id="commit_rollback", required=False, desc="commit/rollback"),
        ],
    )


@pytest.fixture
def client(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(
        generation, "extract_concepts",
        lambda text, model=None: [
            DraftConcept(name="Transaktioner", importance="core", short_explanation="…"),
            DraftConcept(name="Deadlock", importance="nice_to_know", short_explanation="…"),
        ],
    )
    monkeypatch.setattr(
        generation, "generate_items",
        lambda concept, text, model=None: [item(f"{concept.name} q1"), item(f"{concept.name} q2")],
    )
    with TestClient(main.app) as c:
        c.store = store
        yield c


# --- the contract ----------------------------------------------------------


def test_the_rubric_the_generator_emits_is_the_one_the_grader_eats():
    """Not a copy of the shape — the same class. If these ever diverge, the two
    halves stop composing and nothing else in this file would notice."""
    from app.schemas import GradingInput

    rubric_field = DraftItem.model_fields["rubric"].annotation
    grading_field = GradingInput.model_fields["rubric"].annotation
    assert rubric_field == grading_field == list[RubricCriterion]
    assert set(RubricCriterion.model_fields) == {"id", "required", "desc"}


def test_importance_decides_how_many_items_get_written():
    assert ITEMS_PER_IMPORTANCE["core"] > ITEMS_PER_IMPORTANCE["supporting"]
    assert ITEMS_PER_IMPORTANCE["supporting"] > ITEMS_PER_IMPORTANCE["nice_to_know"]


def test_an_unknown_importance_is_rejected_at_the_schema():
    with pytest.raises(ValueError):
        DraftConcept(name="x", importance="critical", short_explanation="…")


# --- structural rejects ----------------------------------------------------


def test_a_rubric_with_no_required_criterion_is_rejected():
    """Every criterion optional means no answer can ever be wrong."""
    bad = item()
    bad.rubric = [RubricCriterion(id="a", required=False, desc="a"),
                  RubricCriterion(id="b", required=False, desc="b")]
    with pytest.raises(ValueError, match="no required criterion"):
        generation._validate([bad], DraftConcept(name="c", importance="core", short_explanation="…"))


def test_duplicate_criterion_ids_are_rejected():
    """score_from_hits keys on id — a duplicate silently drops one criterion."""
    bad = item()
    bad.rubric = [RubricCriterion(id="same", required=True, desc="a"),
                  RubricCriterion(id="same", required=False, desc="b")]
    with pytest.raises(ValueError, match="duplicate rubric ids"):
        generation._validate([bad], DraftConcept(name="c", importance="core", short_explanation="…"))


def test_a_non_ascii_criterion_id_is_rejected():
    """Ids are permanent and every stored grading carries them."""
    bad = item()
    bad.rubric = [RubricCriterion(id="återställer", required=True, desc="a"),
                  RubricCriterion(id="ok_id", required=False, desc="b")]
    with pytest.raises(ValueError, match="not snake_case ascii"):
        generation._validate([bad], DraftConcept(name="c", importance="core", short_explanation="…"))


# --- the review gate -------------------------------------------------------


def test_generating_writes_nothing_to_the_database(client):
    """The whole point: a bad item gets scheduled and graded against for weeks."""
    r = client.post("/api/generate", json={"text": "x" * 400})
    assert r.status_code == 200
    draft = r.json()
    assert draft["n_items"] == 4
    assert [c["importance"] for c in draft["concepts"]] == ["core", "nice_to_know"]
    assert client.store.concepts == {}
    assert client.store.items == {}


def test_confirming_saves_exactly_what_was_not_rejected(client):
    draft = client.post("/api/generate", json={"text": "x" * 400}).json()

    r = client.post("/api/ingest", json={
        "draft_id": draft["draft_id"],
        "reject_concepts": ["Deadlock"],
        "reject_items": ["Transaktioner::Transaktioner q2"],
    })
    assert r.status_code == 200
    assert r.json()["concepts"] == 1
    assert r.json()["items"] == 1

    kept = [i["prompt"] for i in client.store.items.values()]
    assert kept == ["Transaktioner q1"]
    assert [c["name"] for c in client.store.concepts.values()] == ["Transaktioner"]


def test_a_concept_whose_items_were_all_rejected_is_not_saved(client):
    """A concept with no items is a row that can never come up for review."""
    draft = client.post("/api/generate", json={"text": "x" * 400}).json()
    client.post("/api/ingest", json={
        "draft_id": draft["draft_id"],
        "reject_items": [f"Deadlock::Deadlock q{i}" for i in (1, 2)],
    })
    assert [c["name"] for c in client.store.concepts.values()] == ["Transaktioner"]


def test_a_draft_cannot_be_confirmed_twice(client):
    """Confirming twice would duplicate every item into the schedule."""
    draft = client.post("/api/generate", json={"text": "x" * 400}).json()
    assert client.post("/api/ingest", json={"draft_id": draft["draft_id"]}).status_code == 200
    again = client.post("/api/ingest", json={"draft_id": draft["draft_id"]})
    assert again.status_code == 404
    assert len(client.store.items) == 4


def test_an_unknown_draft_id_is_not_a_silent_no_op(client):
    r = client.post("/api/ingest", json={"draft_id": "does-not-exist"})
    assert r.status_code == 404


def test_thin_material_is_refused_before_a_model_is_called(client):
    r = client.post("/api/generate", json={"text": "för kort"})
    assert r.status_code == 422
