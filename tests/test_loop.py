"""The Fas 0 loop end to end, with the model call stubbed out. Everything here
is the part that has to keep working while the grading prompt is still moving."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.scheduling import interleave, review
from app.schemas import DraftConcept, DraftItem, GraderJudgment, RubricCriterion, RubricHit
from app.store import MemoryStore

RUBRIC = [
    RubricCriterion(id="atomicity", required=True, desc="allt eller inget"),
    RubricCriterion(id="commit_rollback", required=True, desc="commit/rollback"),
    RubricCriterion(id="acid", required=False, desc="ACID"),
]


@pytest.fixture
def client(monkeypatch):
    store = MemoryStore()
    course_id = store.ensure_course(main.settings.course_name)
    concept_id = store.add_concept(
        course_id, DraftConcept(name="Transaktioner", importance=5, short_explanation="…")
    )
    item_id = store.add_item(
        concept_id,
        DraftItem(
            type="definition",
            prompt="Vad är en transaction?",
            reference_answer="En atomär sekvens…",
            rubric=RUBRIC,
        ),
    )
    monkeypatch.setattr(main, "store", store)
    with TestClient(main.app) as c:
        c.item_id = item_id
        c.store = store
        yield c


def stub_judgment(*hit_ids: str):
    def _parse(**kwargs):
        return GraderJudgment(
            rubric_hits=[RubricHit(id=c.id, hit=c.id in hit_ids, note="") for c in RUBRIC],
            feedback="stub",
            followup_question="stub?",
        )

    return _parse


def test_a_fresh_item_is_due_immediately(client):
    body = client.get("/api/next").json()
    assert body["item_id"] == client.item_id
    assert body["seen_before"] is False
    assert body["concept_name"] == "Transaktioner"


def test_review_grades_schedules_and_removes_the_item_from_the_queue(client, monkeypatch):
    monkeypatch.setattr("app.ai.grading.parse", stub_judgment("atomicity", "commit_rollback"))

    r = client.post(
        "/api/review",
        json={"item_id": client.item_id, "answer": "allt eller inget, commit/rollback", "confidence": 0.6},
    )
    assert r.status_code == 200
    body = r.json()

    assert body["grading"]["verdict"] == "correct_incomplete"
    assert body["grading"]["score"] == pytest.approx(0.8)
    assert body["grading"]["confidence_gap"] == pytest.approx(-0.2)
    assert body["reference_answer"] == "En atomär sekvens…"
    assert datetime.fromisoformat(body["next_due_at"]) > datetime.now(timezone.utc)

    # Scheduled forward, so it is no longer in today's queue.
    assert client.get("/api/next").json() is None


def test_progress_derives_mastery_from_reviews(client, monkeypatch):
    monkeypatch.setattr("app.ai.grading.parse", stub_judgment("atomicity"))
    client.post(
        "/api/review",
        json={"item_id": client.item_id, "answer": "bara atomicitet", "confidence": 0.9},
    )

    concept = client.get("/api/progress").json()["concepts"][0]
    assert concept["items"] == 1
    assert concept["reviewed_items"] == 1
    assert concept["mastery"] == pytest.approx(0.4)
    assert concept["mean_confidence_gap"] == pytest.approx(0.5)


def test_a_grader_that_skips_a_criterion_does_not_produce_a_review(client, monkeypatch):
    """Fail loud: a malformed grading must not be written to the review log."""
    def bad(**kwargs):
        return GraderJudgment(
            rubric_hits=[RubricHit(id="atomicity", hit=True, note="")],
            feedback="…",
            followup_question="…",
        )

    monkeypatch.setattr("app.ai.grading.parse", bad)
    r = client.post(
        "/api/review", json={"item_id": client.item_id, "answer": "svar", "confidence": 0.5}
    )
    assert r.status_code == 502
    assert client.store.reviews == []


def test_an_empty_answer_is_rejected(client):
    r = client.post("/api/review", json={"item_id": client.item_id, "answer": "   ", "confidence": 0.5})
    assert r.status_code == 422


def test_queue_interleaves_concepts_instead_of_blocking_them():
    queue = interleave(
        [
            {"concept_id": "a", "item_id": "a1"},
            {"concept_id": "a", "item_id": "a2"},
            {"concept_id": "a", "item_id": "a3"},
            {"concept_id": "b", "item_id": "b1"},
            {"concept_id": "b", "item_id": "b2"},
        ]
    )
    concepts = [row["concept_id"] for row in queue]
    assert len(queue) == 5
    # Only the tail may repeat, once the smaller concept has run out.
    assert concepts[:4] == ["a", "b", "a", "b"]


def test_a_confidently_wrong_answer_is_scheduled_sooner_than_a_correct_one():
    _, soon = review(None, 0.3, "confidently_wrong")
    _, later = review(None, 1.0, "correct")
    assert soon < later
