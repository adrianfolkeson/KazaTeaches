"""The Fas 0 loop end to end, with the model call stubbed out. Everything here
is the part that has to keep working while the grading prompt is still moving."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
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
        course_id, DraftConcept(name="Transaktioner", importance="core", short_explanation="…")
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
            rubric_hits=[
                RubricHit(id=c.id, status="hit" if c.id in hit_ids else "miss", note="")
                for c in RUBRIC
            ],
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
            rubric_hits=[RubricHit(id="atomicity", status="hit", note="")],
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


def test_the_sitting_ends_on_its_cap_even_when_items_are_still_due(client, monkeypatch):
    """The cap has to count reviews, not just truncate a list. An item the
    student never masters is always due — FSRS puts a half-known one back in six
    minutes — so without a counted cap /api/next keeps handing back the same
    item until the month's budget is gone.

    The item is forced back into the queue after each review, so what ends the
    sitting here is the cap and nothing else.
    """
    monkeypatch.setattr("app.ai.grading.parse", stub_judgment("atomicity"))
    monkeypatch.setattr(main.settings, "session_max_items", 3)

    served = 0
    while client.get("/api/next").json() is not None:
        r = client.post(
            "/api/review",
            json={"item_id": client.item_id, "answer": "halvt svar", "confidence": 0.5},
        )
        assert r.status_code == 200
        served += 1
        assert served <= 5, "the sitting never ended"
        # Forget the schedule, so the item is due again on the next call.
        client.store.reviews.clear()

    assert served == 3
    body = client.get("/api/session").json()
    assert body["reviews_done"] == 3
    assert body["reviews_left"] == 0
    assert body["due_total"] == 1, "the item is still due — the sitting is what ended"


def test_the_sitting_also_ends_when_nothing_is_due(client, monkeypatch):
    """The other reason to stop: everything is scheduled forward."""
    monkeypatch.setattr("app.ai.grading.parse", stub_judgment("atomicity", "commit_rollback", "acid"))
    client.post("/api/review",
                json={"item_id": client.item_id, "answer": "fullt svar", "confidence": 0.8})

    assert client.get("/api/next").json() is None
    body = client.get("/api/session").json()
    assert body["due_total"] == 0
    assert body["reviews_left"] > 0, "the cap was not what stopped it"


def test_a_new_sitting_starts_on_a_new_day(client, monkeypatch):
    """Yesterday's cap must not lock today out."""
    monkeypatch.setattr("app.ai.grading.parse", stub_judgment("atomicity"))
    monkeypatch.setattr(main.settings, "session_max_items", 1)

    client.post("/api/review",
                json={"item_id": client.item_id, "answer": "svar", "confidence": 0.5})
    assert client.get("/api/next").json() is None

    main._sitting["day"] = None  # the day rolled over
    assert client.get("/api/session").json()["reviews_done"] == 0
