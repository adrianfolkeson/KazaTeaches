"""The grader. §1: this is where 80% of the technical effort goes, because it is
the only part that decides whether the app is worth anything."""

from __future__ import annotations

from app.ai.client import cached, parse
from app.config import settings
from app.schemas import GraderJudgment, GradingInput, GradingOutput
from app.scoring import confidence_gap, score_from_hits, verdict_from
from app.ai.prompts import GRADER_SYSTEM


def _user_message(inp: GradingInput) -> str:
    lines = [
        "<question>",
        inp.question,
        "</question>",
        "",
        "<reference_answer>",
        inp.reference_answer,
        "</reference_answer>",
        "",
        "<rubric>",
    ]
    for c in inp.rubric:
        req = "required" if c.required else "optional"
        lines.append(f'- id="{c.id}" ({req}): {c.desc}')
    lines += [
        "</rubric>",
        "",
        "<student_answer>",
        inp.student_answer,
        "</student_answer>",
    ]
    return "\n".join(lines)


def grade(inp: GradingInput, *, model: str | None = None) -> GradingOutput:
    """Pure function: same input, same shape out. No DB, no session state.

    The student's self-rated confidence is deliberately NOT shown to the model.
    If the grader knew the student felt sure, its hit judgments would drift
    toward that feeling — and confidence_gap, the whole point of asking, would
    be measuring itself.
    """
    judgment = parse(
        model=model or settings.grading_model,
        # Stable prefix first so it can cache across every review in a session.
        system=[cached(GRADER_SYSTEM)],
        user=_user_message(inp),
        output_format=GraderJudgment,
        max_tokens=4000,
        effort="high",
    )

    # score_from_hits raises if the model skipped or invented a criterion —
    # a partial grading is worse than a loud failure.
    score = score_from_hits(inp.rubric, judgment.rubric_hits)
    verdict = verdict_from(inp.rubric, judgment.rubric_hits, score, inp.confidence)

    return GradingOutput(
        score=score,
        rubric_hits=judgment.rubric_hits,
        verdict=verdict,
        feedback=judgment.feedback,
        followup_question=judgment.followup_question,
        confidence_gap=confidence_gap(inp.confidence, score),
    )
