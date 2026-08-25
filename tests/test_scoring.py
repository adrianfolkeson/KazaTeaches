"""Tests for the deterministic half of grading. These assert the intent —
what a verdict is allowed to mean — not the arithmetic for its own sake."""

from __future__ import annotations

import pytest
from fsrs import Rating

from app.schemas import RubricCriterion as C, RubricHit as H
from app.scoring import confidence_gap, rating_from, score_from_hits, verdict_from

RUBRIC = [
    C(id="req_a", required=True, desc="a"),
    C(id="req_b", required=True, desc="b"),
    C(id="opt_c", required=False, desc="c"),
]


def hits(*hit_ids: str) -> list[H]:
    return [H(id=c.id, hit=c.id in hit_ids, note="") for c in RUBRIC]


def grade(*hit_ids: str, confidence: float = 0.5):
    h = hits(*hit_ids)
    score = score_from_hits(RUBRIC, h)
    return score, verdict_from(RUBRIC, h, score, confidence)


def test_a_missed_required_criterion_is_never_correct_incomplete():
    """`required` has to mean required, or the rubric is decoration."""
    _, verdict = grade("req_a", "opt_c", confidence=0.3)
    assert verdict != "correct_incomplete"


def test_only_an_optional_gap_is_correct_incomplete():
    score, verdict = grade("req_a", "req_b", confidence=0.6)
    assert verdict == "correct_incomplete"
    assert score == pytest.approx(0.8)


def test_everything_hit_is_correct():
    score, verdict = grade("req_a", "req_b", "opt_c")
    assert (score, verdict) == (1.0, "correct")


def test_required_criteria_cost_more_than_optional_ones():
    missed_required, _ = grade("req_b", "opt_c")
    missed_optional, _ = grade("req_a", "req_b")
    assert missed_required < missed_optional


def test_confidently_wrong_needs_the_confidence():
    """Same answer, different self-assessment: only the sure one is flagged."""
    _, unsure = grade("opt_c", confidence=0.2)
    _, sure = grade("opt_c", confidence=0.9)
    assert unsure == "partial"
    assert sure == "confidently_wrong"


def test_nothing_hit_and_no_overconfidence_is_wrong():
    assert grade(confidence=0.1)[1] == "wrong"


def test_a_skipped_criterion_fails_loudly():
    """A grader that answers two of three criteria must not silently produce a
    score built on the two it felt like judging."""
    partial = [H(id="req_a", hit=True, note=""), H(id="opt_c", hit=False, note="")]
    with pytest.raises(ValueError, match="req_b"):
        score_from_hits(RUBRIC, partial)


def test_empty_rubric_is_an_error_not_a_free_pass():
    with pytest.raises(ValueError):
        score_from_hits([], [])


def test_confidence_gap_is_signed():
    assert confidence_gap(0.8, 0.4) == pytest.approx(0.4)
    assert confidence_gap(0.2, 0.9) == pytest.approx(-0.7)


def test_confidently_wrong_always_resets_the_interval():
    """Even a half-decent score gets Again when the student was sure — being
    sure and wrong is the failure mode spaced repetition exists to catch."""
    assert rating_from(0.6, "confidently_wrong") == Rating.Again
    assert rating_from(0.6, "partial") == Rating.Hard


@pytest.mark.parametrize(
    "score,expected",
    [(0.0, Rating.Again), (0.39, Rating.Again), (0.4, Rating.Hard),
     (0.69, Rating.Hard), (0.7, Rating.Good), (0.89, Rating.Good), (1.0, Rating.Easy)],
)
def test_score_maps_monotonically_onto_fsrs_ratings(score, expected):
    assert rating_from(score, "partial") == expected
