"""The contracts. §8 is the important one — grading is a pure function with a
fixed input shape and a fixed JSON output shape."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["correct", "correct_incomplete", "partial", "confidently_wrong", "wrong"]

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
    hit: bool
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
    importance: int = Field(ge=1, le=5)
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


class IngestRequest(BaseModel):
    text: str
    course_name: str | None = None


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


class ReviewRequest(BaseModel):
    item_id: str
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)


class ReviewResponse(BaseModel):
    grading: GradingOutput
    reference_answer: str
    next_due_at: str
    interval_days: float


class ConceptMastery(BaseModel):
    concept_id: str
    name: str
    importance: int
    items: int
    reviewed_items: int
    mastery: float | None
    mean_confidence_gap: float | None


class ProgressResponse(BaseModel):
    course_id: str
    due_now: int
    concepts: list[ConceptMastery]
