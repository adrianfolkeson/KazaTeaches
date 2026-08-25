"""Fas 0 loop, over HTTP: paste text -> concepts + items -> answer free text ->
grading -> mastery. One course, no auth, no PDF."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.ai.client import AIError
from app.ai.generation import extract_concepts, generate_items
from app.ai.grading import grade
from app.config import settings
from app.scheduling import interleave, review as fsrs_review
from app.schemas import (
    DueItem,
    GradingInput,
    GradingOutput,
    IngestRequest,
    IngestResponse,
    ProgressResponse,
    ReviewRequest,
    ReviewResponse,
    RubricCriterion,
)
from app.store import build_store

WEB = Path(__file__).resolve().parent.parent / "web"

store = build_store()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if store.backend == "memory":
        print(
            "WARNING: DATABASE_URL is not set. Running on the in-memory store —\n"
            "         every concept, item and review is lost when this process exits.",
            file=sys.stderr,
        )
    yield


app = FastAPI(title="Studiesystem — Fas 0", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {
        "store": store.backend,
        "persistent": store.backend == "postgres",
        "grading_model": settings.grading_model,
        "generation_model": settings.generation_model,
        "cheap_model": settings.cheap_model,
    }


@app.post("/api/grade", response_model=GradingOutput)
def grade_endpoint(inp: GradingInput) -> GradingOutput:
    """The §8 contract, standing alone. No database, no session — this is the
    endpoint the eval-set and every prompt experiment run against."""
    try:
        return grade(inp)
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.post("/api/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest) -> IngestResponse:
    text = req.text.strip()
    if len(text) < 200:
        raise HTTPException(422, "Paste at least a few paragraphs of material.")

    course_id = store.ensure_course(req.course_name or settings.course_name)
    try:
        concepts = extract_concepts(text)
        n_items = 0
        for concept in concepts:
            concept_id = store.add_concept(course_id, concept)
            for item in generate_items(concept, text):
                store.add_item(concept_id, item)
                n_items += 1
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Generation produced an invalid item: {e}") from e

    return IngestResponse(course_id=course_id, concepts=len(concepts), items=n_items)


@app.get("/api/next", response_model=DueItem | None)
def next_item() -> DueItem | None:
    course_id = store.ensure_course(settings.course_name)
    queue = interleave(store.due_items(course_id))
    if not queue:
        return None
    row = queue[0]
    return DueItem(
        item_id=row["item_id"],
        concept_id=row["concept_id"],
        concept_name=row["concept_name"],
        type=row["type"],
        prompt=row["prompt"],
        due_at=row["due_at"].isoformat() if row["due_at"] else None,
        seen_before=bool(row["seen_before"]),
    )


@app.post("/api/review", response_model=ReviewResponse)
def submit_review(req: ReviewRequest) -> ReviewResponse:
    item = store.get_item(req.item_id)
    if not item:
        raise HTTPException(404, "No such item.")
    if not req.answer.strip():
        raise HTTPException(422, "Write an answer first — that is the whole point.")

    try:
        result = grade(
            GradingInput(
                question=item["prompt"],
                reference_answer=item["reference_answer"],
                rubric=[RubricCriterion(**c) for c in item["rubric"]],
                student_answer=req.answer,
                confidence=req.confidence,
            )
        )
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    last = store.latest_review(req.item_id)
    state, due_at = fsrs_review(last["fsrs_state"] if last else None, result.score, result.verdict)

    store.record_review(
        item_id=req.item_id,
        answer=req.answer,
        score=result.score,
        rubric_hits=[h.model_dump() for h in result.rubric_hits],
        verdict=result.verdict,
        confidence=req.confidence,
        fsrs_state=state,
        due_at=due_at,
    )

    interval = (due_at - datetime.now(timezone.utc)).total_seconds() / 86400
    return ReviewResponse(
        grading=result,
        reference_answer=item["reference_answer"],
        next_due_at=due_at.isoformat(),
        interval_days=round(interval, 3),
    )


@app.get("/api/progress", response_model=ProgressResponse)
def progress() -> ProgressResponse:
    course_id = store.ensure_course(settings.course_name)
    return ProgressResponse(
        course_id=course_id,
        due_now=len(store.due_items(course_id)),
        concepts=store.progress(course_id),
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")
