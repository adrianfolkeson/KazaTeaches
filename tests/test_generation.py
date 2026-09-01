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
    RubricHit,
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


# --- the draft has to outlive the process ---------------------------------


def test_a_draft_survives_the_process_that_made_it(client):
    """The failure this replaced: drafts lived in process memory, a free
    instance sleeps after fifteen idle minutes, and reviewing a draft takes
    longer than that. A review gate that deletes what you are reviewing is not
    a gate."""
    draft = client.post("/api/generate", json={"text": "x" * 400}).json()

    # Whatever the request handler held is gone; only the store remains.
    import importlib

    from app import main as main_mod

    importlib.reload(main_mod)
    main_mod.store = client.store

    assert main_mod.store.get_draft(draft["draft_id"]) is not None
    saved = main_mod.store.pop_draft(draft["draft_id"])
    assert saved["n_items"] == draft["n_items"]


def test_an_unsaved_draft_is_findable_after_the_page_is_gone(client):
    """It cost money and the page that made it is the only thing that knew its
    id. Without a list there is no way back to it at all."""
    client.post("/api/generate", json={"text": "x" * 400})
    pending = client.get("/api/drafts").json()
    assert len(pending) == 1
    assert pending[0]["n_items"] == 4


def test_a_second_generation_is_refused_while_one_is_running(client, monkeypatch):
    """A reloaded page used to start a second run silently, and the bill
    arrived twice."""
    import threading

    from app import main as main_mod

    started, release = threading.Event(), threading.Event()

    def slow(concept, text, model=None):
        started.set()
        release.wait(timeout=5)
        return [item(f"{concept.name} q1")]

    monkeypatch.setattr(generation, "generate_items", slow)

    result: dict = {}
    t = threading.Thread(target=lambda: result.update(
        code=client.post("/api/generate", json={"text": "x" * 400}).status_code))
    t.start()
    assert started.wait(timeout=5), "the first generation never began"

    second = client.post("/api/generate", json={"text": "x" * 400})
    assert second.status_code == 409
    assert "pågår redan" in second.json()["detail"]

    release.set()
    t.join(timeout=10)
    assert result.get("code") == 200
    assert main_mod._generating.locked() is False, "the lock must be released"


def test_the_lock_is_released_when_generation_fails(client, monkeypatch):
    """A lock held by a crashed request blocks every later one, and the only
    cure is a redeploy."""
    from app import main as main_mod
    from app.ai.client import AIError

    def boom(*a, **k):
        raise AIError("upstream is down")

    monkeypatch.setattr(generation, "generate_items", boom)
    assert client.post("/api/generate", json={"text": "x" * 400}).status_code == 502
    assert main_mod._generating.locked() is False


# --- removing an item after it was saved -----------------------------------


def test_an_item_can_be_deleted_after_it_has_been_scheduled(client):
    """Some questions only reveal themselves as bad once you have been asked
    them. Until this existed there was no way to remove one at all."""
    draft = client.post("/api/generate", json={"text": "x" * 400}).json()
    client.post("/api/ingest", json={"draft_id": draft["draft_id"]})

    item_id = next(iter(client.store.items))
    assert client.delete(f"/api/items/{item_id}").status_code == 200
    assert item_id not in client.store.items
    assert client.get("/api/session").json()["due_total"] == 3


def test_deleting_the_last_item_of_a_concept_removes_the_concept(client):
    """A concept with no items can never come up for review, so leaving it
    behind means progress reports on something unreachable."""
    draft = client.post("/api/generate", json={"text": "x" * 400}).json()
    client.post("/api/ingest", json={"draft_id": draft["draft_id"]})

    concept_id = next(c["id"] for c in client.store.concepts.values()
                      if c["name"] == "Deadlock")
    for item_id in [i for i, v in client.store.items.items() if v["concept_id"] == concept_id]:
        client.delete(f"/api/items/{item_id}")

    assert concept_id not in client.store.concepts
    names = [c["name"] for c in client.get("/api/progress").json()["concepts"]]
    assert names == ["Transaktioner"]


def test_deleting_an_item_takes_its_review_history_with_it(client, monkeypatch):
    from app import main as main_mod

    draft = client.post("/api/generate", json={"text": "x" * 400}).json()
    client.post("/api/ingest", json={"draft_id": draft["draft_id"]})
    item_id = next(iter(client.store.items))

    client.store.record_review(
        item_id=item_id, answer="a", score=0.5, rubric_hits=[], verdict="partial",
        confidence=0.5, fsrs_state={}, due_at=main_mod.datetime.now(main_mod.timezone.utc),
    )
    assert len(client.store.reviews) == 1

    client.delete(f"/api/items/{item_id}")
    assert client.store.reviews == []


def test_deleting_an_unknown_item_is_not_a_silent_success(client):
    assert client.delete("/api/items/does-not-exist").status_code == 404


# --- starting over ---------------------------------------------------------


def test_reset_clears_the_content_but_not_the_spend_ledger(client):
    """The money was spent whether or not the questions it bought still exist.
    Zeroing the ledger would make the monthly cap lie for the rest of the
    month — the one number that must survive a wipe."""
    from app.budget import current_month

    draft = client.post("/api/generate", json={"text": "x" * 400}).json()
    client.post("/api/ingest", json={"draft_id": draft["draft_id"]})
    client.store.record_spend("claude-opus-5", 3.50, type("U", (), {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})())

    r = client.post("/api/reset", json={"confirm": "radera allt"})
    assert r.status_code == 200
    assert r.json()["deleted"] == {"concepts": 2, "items": 4, "reviews": 0}

    assert client.store.concepts == {}
    assert client.store.items == {}
    assert client.get("/api/session").json()["due_total"] == 0
    assert client.store.month_spend(current_month()) == pytest.approx(3.50)


def test_reset_without_the_phrase_deletes_nothing(client):
    """The access gate stops other people; the phrase stops a mis-click and a
    stray fetch, neither of which types a word."""
    draft = client.post("/api/generate", json={"text": "x" * 400}).json()
    client.post("/api/ingest", json={"draft_id": draft["draft_id"]})

    for body in ({"confirm": ""}, {"confirm": "ja"}, {"confirm": "delete all"}):
        assert client.post("/api/reset", json=body).status_code == 422
    assert len(client.store.items) == 4


def test_reset_accepts_the_phrase_regardless_of_case_and_padding(client):
    assert client.post("/api/reset", json={"confirm": "  Radera Allt "}).status_code == 200


def test_reset_also_clears_an_unreviewed_draft(client):
    """A draft left over from the old course would otherwise reappear on the
    Import screen as a pending banner for content that is gone."""
    client.post("/api/generate", json={"text": "x" * 400})
    assert len(client.get("/api/drafts").json()) == 1

    client.post("/api/reset", json={"confirm": "radera allt"})
    assert client.get("/api/drafts").json() == []


def test_reset_clears_the_days_sitting(client, monkeypatch):
    """The sitting counts reviews against items that no longer exist; leaving
    it would cap a fresh course with yesterday's spent slots."""
    from app import main as main_mod

    monkeypatch.setattr("app.ai.grading.parse", lambda **kw: __import__(
        "app.schemas", fromlist=["GraderJudgment"]).GraderJudgment(
        rubric_hits=[RubricHit(id="atomicity", status="hit", note=""),
                     RubricHit(id="commit_rollback", status="miss", note="")],
        feedback="x", followup_question="y"))

    draft = client.post("/api/generate", json={"text": "x" * 400}).json()
    client.post("/api/ingest", json={"draft_id": draft["draft_id"]})
    item_id = next(iter(client.store.items))
    client.post("/api/review", json={"item_id": item_id, "answer": "a", "confidence": 0.5})
    assert main_mod._sitting["reviews"] == 1

    client.post("/api/reset", json={"confirm": "radera allt"})
    assert main_mod._sitting["reviews"] == 0
