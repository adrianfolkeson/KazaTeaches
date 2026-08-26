"""The contracts. §8 is the important one — grading is a pure function with a
fixed input shape and a fixed JSON output shape."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["correct", "correct_incomplete", "partial", "confidently_wrong", "wrong"]

# Three-state rubric judgment. "partial" exists because a vague-but-pointed-the-
# right-way answer is neither a hit nor a miss, and collapsing it into either one
# throws away the signal the feedback is built on. Its credit is set in
# app/scoring.py, not here.
HitStatus = Literal["hit", "partial", "miss"]

# Concept weight. An enum rather than 1-5: the generator was picking numbers off
# a scale whose middle had no agreed meaning, and the only thing the number is
# ever used for is how many items to write and what to study first.
Importance = Literal["core", "supporting", "nice_to_know"]

# How many items each weight earns. Here rather than in generation.py because it
# is part of what `importance` *means*.
ITEMS_PER_IMPORTANCE: dict[str, int] = {"core": 4, "supporting": 3, "nice_to_know": 2}

# Study order, highest first.
IMPORTANCE_RANK: dict[str, int] = {"core": 3, "supporting": 2, "nice_to_know": 1}

ItemType = Literal[
    "definition",
    "explanation",
    "comparison",
    "scenario",
    "teach_me",
    "multiple_choice",
    "true_false",
    "code_output",
    "debugging",
    "design",
]


class RubricCriterion(BaseModel):
    id: str
    required: bool
    desc: str


class RubricHit(BaseModel):
    id: str
    status: HitStatus
    note: str


class GradingInput(BaseModel):
    """§8 input."""

    question: str
    reference_answer: str
    rubric: list[RubricCriterion]
    student_answer: str
    confidence: float = Field(ge=0.0, le=1.0)


class GradingOutput(BaseModel):
    """§8 output. `score`, `verdict` and `confidence_gap` are computed in code
    (app/scoring.py); the model only supplies judgment — see README."""

    score: float
    rubric_hits: list[RubricHit]
    verdict: Verdict
    feedback: str
    followup_question: str
    confidence_gap: float


class GraderJudgment(BaseModel):
    """What the model is actually asked for. Deliberately excludes score and
    verdict: those are deterministic functions of the hits."""

    rubric_hits: list[RubricHit]
    feedback: str
    followup_question: str


# --- generation ------------------------------------------------------------


class DraftConcept(BaseModel):
    name: str
    importance: Importance
    short_explanation: str


class DraftConceptList(BaseModel):
    concepts: list[DraftConcept]


class DraftItem(BaseModel):
    type: ItemType
    prompt: str
    reference_answer: str
    rubric: list[RubricCriterion]


class DraftItemList(BaseModel):
    items: list[DraftItem]


# --- API ------------------------------------------------------------------


class GenerateRequest(BaseModel):
    """Raw pasted material in. `course_id` targets an existing course; without
    one the draft lands in the default course on confirmation."""

    text: str
    course_id: str | None = None
    course_name: str | None = None


class DraftConceptWithItems(BaseModel):
    """One concept and the items written for it, before anything is stored."""

    name: str
    importance: Importance
    short_explanation: str
    items: list[DraftItem]


class GenerationDraft(BaseModel):
    """The whole generated batch, held for review. Nothing here is in the
    database yet — §"never save silently": a bad item poisons the schedule for
    weeks, and by then it is indistinguishable from a bad memory."""

    draft_id: str
    course_id: str
    course_name: str
    concepts: list[DraftConceptWithItems]
    n_items: int
    cost_usd: float


class ConfirmRequest(BaseModel):
    draft_id: str
    # Concept names the reviewer struck out. Everything not listed is saved.
    reject_concepts: list[str] = []
    # "<concept name>::<item prompt>" for individual items struck out.
    reject_items: list[str] = []


class IngestResponse(BaseModel):
    course_id: str
    concepts: int
    items: int


class DueItem(BaseModel):
    item_id: str
    concept_id: str
    concept_name: str
    type: ItemType
    prompt: str
    due_at: str | None
    seen_before: bool
    # Attempts spent on this item in the current sitting, and the ceiling.
    # Server-side state (app/main.py `_sitting`) the client cannot derive.
    attempt: int = 0
    attempts_allowed: int = 0


class SessionQueue(BaseModel):
    """Today's sitting. `due_total` is everything overdue; `items` is what fits
    in one session, interleaved across concepts."""

    course_id: str
    due_total: int
    items: list[DueItem]
    concepts_covered: int
    capped: bool
    reviews_done: int
    reviews_left: int


class ReviewRequest(BaseModel):
    item_id: str
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)


class ReviewResponse(BaseModel):
    grading: GradingOutput
    reference_answer: str
    next_due_at: str
    interval_days: float
    # The item's rubric, so a client can join rubric_hits[].id -> desc. A hit
    # carries the grader's note (why), never the criterion text (what), and a
    # result screen that lists what you missed needs the what.
    rubric: list[RubricCriterion] = []


class ConceptMastery(BaseModel):
    concept_id: str
    name: str
    importance: Importance
    items: int
    reviewed_items: int
    mastery: float | None
    mean_confidence_gap: float | None


class ProgressResponse(BaseModel):
    course_id: str
    due_now: int
    concepts: list[ConceptMastery]


class HistoryRow(BaseModel):
    reviewed_at: str
    prompt: str
    concept_name: str
    verdict: Verdict
    score: float
    confidence: float
    confidence_gap: float


class HistoryDay(BaseModel):
    day: str
    rows: list[HistoryRow]


class HistoryResponse(BaseModel):
    course_id: str
    total: int
    days: list[HistoryDay]


class ModelSpend(BaseModel):
    model: str
    calls: int
    cost_usd: float


class BudgetStatus(BaseModel):
    """What /api/budget reports. `cap` is enforced; `target` only informs."""

    month: str
    spent_usd: float
    cap_usd: float
    target_usd: float
    remaining_usd: float
    fraction_of_cap: float
    over_target: bool
    exhausted: bool
    by_model: list[ModelSpend]
