"""Tests for the deterministic half of grading. These assert the intent —
what a verdict is allowed to mean — not the arithmetic for its own sake."""

from __future__ import annotations

import pytest

from app.schemas import RubricCriterion as C, RubricHit as H
from app.scoring import confidence_gap, score_from_hits, verdict_from

RUBRIC = [
    C(id="req_a", required=True, desc="a"),
    C(id="req_b", required=True, desc="b"),
    C(id="opt_c", required=False, desc="c"),
]


def hits(*hit_ids: str, partial: tuple[str, ...] = ()) -> list[H]:
    return [
        H(id=c.id, status="hit" if c.id in hit_ids else "partial" if c.id in partial else "miss", note="")
        for c in RUBRIC
    ]


def grade(*hit_ids: str, partial: tuple[str, ...] = (), confidence: float = 0.5):
    h = hits(*hit_ids, partial=partial)
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
    incomplete = [H(id="req_a", status="hit", note=""), H(id="opt_c", status="miss", note="")]
    with pytest.raises(ValueError, match="req_b"):
        score_from_hits(RUBRIC, incomplete)


def test_empty_rubric_is_an_error_not_a_free_pass():
    with pytest.raises(ValueError):
        score_from_hits([], [])


def test_confidence_gap_is_signed():
    assert confidence_gap(0.8, 0.4) == pytest.approx(0.4)
    assert confidence_gap(0.2, 0.9) == pytest.approx(-0.7)


def test_a_partial_is_worth_half_of_its_criterion():
    """A vague-but-right-direction answer must land between the miss and the hit,
    or `partial` is just a second name for one of them."""
    missed, _ = grade("req_a", confidence=0.5)
    halfway, _ = grade("req_a", partial=("req_b",), confidence=0.5)
    full, _ = grade("req_a", "req_b", confidence=0.5)
    assert missed < halfway < full
    assert halfway == pytest.approx(0.6)


def test_partial_required_criteria_are_not_correct():
    """Half-expressing every must-have is not knowing the answer."""
    _, verdict = grade(partial=("req_a", "req_b"), confidence=0.5)
    assert verdict not in ("correct", "correct_incomplete")


def test_a_partial_required_criterion_still_reads_as_incomplete_not_partial():
    """One must-have fully there, the other clearly gestured at, plus the
    optional: the student has the idea and is missing precision, which is a
    different diagnosis than having half the answer."""
    score, verdict = grade("req_a", "opt_c", partial=("req_b",), confidence=0.5)
    assert score == pytest.approx(0.8)
    assert verdict == "correct_incomplete"


def test_correct_tolerates_a_half_expressed_optional_but_not_a_missing_one():
    """CORRECT_SCORE is slack, deliberately: every must-have fully there and the
    optional nuance gestured at is a correct answer. Losing the optional
    outright is not — that is the gap worth telling the student about."""
    gestured, gestured_verdict = grade("req_a", "req_b", partial=("opt_c",), confidence=0.5)
    dropped, dropped_verdict = grade("req_a", "req_b", confidence=0.5)
    assert (gestured, gestured_verdict) == (pytest.approx(0.9), "correct")
    assert (dropped, dropped_verdict) == (pytest.approx(0.8), "correct_incomplete")
