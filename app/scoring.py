"""Deterministic half of grading.

The model judges one thing only: did the student's answer express this rubric
criterion, yes or no. Everything downstream of that — score, verdict,
confidence gap, FSRS rating — is arithmetic, so it lives in code where it is
testable and cannot drift between runs (CLAUDE.md: model for judgment, code for
determinism).

§8's example output shows the model producing `score` directly. We deviate on
purpose: a model-authored score is the single least reproducible number in the
system and it is exactly the number the eval-set has to hold stable.
"""

from __future__ import annotations

from fsrs import Rating

from app.schemas import RubricCriterion, RubricHit, Verdict

REQUIRED_WEIGHT = 2.0
OPTIONAL_WEIGHT = 1.0

# A confidence this far above the achieved score is what "confidently wrong" means.
CONFIDENT_GAP = 0.4


def _weight(c: RubricCriterion) -> float:
    return REQUIRED_WEIGHT if c.required else OPTIONAL_WEIGHT


def score_from_hits(rubric: list[RubricCriterion], hits: list[RubricHit]) -> float:
    """Weighted fraction of rubric criteria the answer hit. Required criteria
    count double, so missing one costs more than missing a nice-to-have."""
    if not rubric:
        raise ValueError("rubric is empty — an item without a rubric cannot be graded")
    by_id = {h.id: h for h in hits}
    missing = [c.id for c in rubric if c.id not in by_id]
    if missing:
        raise ValueError(f"grader returned no verdict for rubric criteria: {missing}")

    total = sum(_weight(c) for c in rubric)
    earned = sum(_weight(c) for c in rubric if by_id[c.id].hit)
    return round(earned / total, 4)


def verdict_from(
    rubric: list[RubricCriterion],
    hits: list[RubricHit],
    score: float,
    confidence: float,
) -> Verdict:
    by_id = {h.id: h for h in hits}
    required = [c for c in rubric if c.required]
    all_required_hit = all(by_id[c.id].hit for c in required)
    all_hit = all(by_id[c.id].hit for c in rubric)

    if all_hit:
        return "correct"
    if all_required_hit:
        # Every must-have is there, an optional nuance is not.
        return "correct_incomplete"
    if score < 0.5 and (confidence - score) >= CONFIDENT_GAP:
        # The dangerous case: sure of an answer that is not there. This is the
        # signal "find my gaps" is built on (§1.3).
        return "confidently_wrong"
    if score > 0.0:
        return "partial"
    return "wrong"


def confidence_gap(confidence: float, score: float) -> float:
    """Self-rated confidence minus actual score. Positive and large = warning flag."""
    return round(confidence - score, 4)


def rating_from(score: float, verdict: Verdict) -> Rating:
    """Map a score onto an FSRS rating (§8: score drives the FSRS update)."""
    if verdict == "confidently_wrong":
        # Being sure and wrong deserves the shortest possible interval,
        # regardless of how many partial credits were scraped together.
        return Rating.Again
    if score < 0.4:
        return Rating.Again
    if score < 0.7:
        return Rating.Hard
    if score < 0.9:
        return Rating.Good
    return Rating.Easy
