"""Deterministic half of grading.

Grading only. The FSRS rating a verdict earns lives in app/scheduling.py — it is
a scheduling decision that happens to read a grading result, and keeping it here
made scoring.py the place two unrelated policies were tuned.

The model judges one thing only: to what degree did the student's answer express
this rubric criterion — hit, partial or miss. Everything downstream of that —
score, verdict, confidence gap, FSRS rating — is arithmetic, so it lives in code
where it is testable and cannot drift between runs (CLAUDE.md: model for
judgment, code for determinism).

§8's example output shows the model producing `score` directly. We deviate on
purpose: a model-authored score is the single least reproducible number in the
system and it is exactly the number the eval-set has to hold stable.
"""

from __future__ import annotations

from app.schemas import RubricCriterion, RubricHit, Verdict

REQUIRED_WEIGHT = 2.0
OPTIONAL_WEIGHT = 1.0

# What each rubric status is worth. A partial is deliberately worth half rather
# than nothing: the grader is told to use it for "right direction but vague",
# and scoring that as zero makes the verdict indistinguishable from an answer
# that never approached the idea.
CREDIT: dict[str, float] = {"hit": 1.0, "partial": 0.5, "miss": 0.0}

# A confidence this far above the achieved score is what "confidently wrong" means.
CONFIDENT_GAP = 0.4

# All required criteria hit and this much of the total earned = nothing to add.
CORRECT_SCORE = 0.85

# Required criteria covered only partially still count as an incomplete answer
# rather than a partial one, but only once this much of the total is earned.
COVERED_SCORE = 0.6


def _weight(c: RubricCriterion) -> float:
    return REQUIRED_WEIGHT if c.required else OPTIONAL_WEIGHT


def score_from_hits(rubric: list[RubricCriterion], hits: list[RubricHit]) -> float:
    """Weighted fraction of the rubric the answer earned. Required criteria count
    double, so missing one costs more than missing a nice-to-have, and a partial
    earns half of whatever its criterion is worth."""
    if not rubric:
        raise ValueError("rubric is empty — an item without a rubric cannot be graded")
    by_id = {h.id: h for h in hits}
    missing = [c.id for c in rubric if c.id not in by_id]
    if missing:
        raise ValueError(f"grader returned no verdict for rubric criteria: {missing}")

    total = sum(_weight(c) for c in rubric)
    earned = sum(_weight(c) * CREDIT[by_id[c.id].status] for c in rubric)
    return round(earned / total, 4)


def verdict_from(
    rubric: list[RubricCriterion],
    hits: list[RubricHit],
    score: float,
    confidence: float,
) -> Verdict:
    by_id = {h.id: h for h in hits}
    required = [c for c in rubric if c.required]
    all_required_hit = all(by_id[c.id].status == "hit" for c in required)
    required_covered = all(by_id[c.id].status in ("hit", "partial") for c in required)

    if all_required_hit and score >= CORRECT_SCORE:
        return "correct"
    if all_required_hit or (required_covered and score >= COVERED_SCORE):
        # Every must-have is there — fully, or clearly enough to build on — and
        # what is left is an optional nuance or a loose edge.
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
