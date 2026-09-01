"""Fas 0 loop, over HTTP: paste text -> concepts + items -> answer free text ->
grading -> mastery. One course, no auth, no PDF."""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.ai.client import AIError, set_meter
from app.auth import access_key, gate
from app.diagnose import probe
from app.ai.generation import generate_draft
from app.ai.grading import grade
from app.budget import BudgetExceeded, current_month
from app.config import settings
from app.scheduling import review as fsrs_review, session_queue
from app.schemas import (
    ConfirmRequest,
    DraftConcept,
    DraftItem,
    DueItem,
    GenerateRequest,
    GenerationDraft,
    GradingInput,
    GradingOutput,
    HistoryDay,
    HistoryResponse,
    HistoryRow,
    IngestResponse,
    ResetRequest,
    SessionQueue,
    BudgetStatus,
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
    # Arm the spend cap before the first request can be served. On the memory
    # store the ledger dies with the process, so the cap only holds within one
    # run — /api/health says so rather than implying a guarantee it cannot make.
    set_meter(store.month_spend, store.record_spend)

    # Idempotent, and it runs before the first request so a fresh database is
    # usable on the first boot rather than on the first crash.
    store.ensure_schema()

    if store.backend == "memory":
        print(
            "WARNING: DATABASE_URL is not set. Running on the in-memory store —\n"
            "         every concept, item and review is lost when this process exits,\n"
            "         and the monthly spend ledger resets with it.",
            file=sys.stderr,
        )
    if access_key() is None:
        print(
            "WARNING: KT_ACCESS_KEY is not set. Every endpoint is open, including\n"
            "         the ones that spend money. Fine locally; never in production.",
            file=sys.stderr,
        )

    spent = store.month_spend()
    print(
        f"Budget {current_month()}: ${spent:.2f} spent of "
        f"${settings.monthly_budget_usd:.2f} cap (target ${settings.monthly_target_usd:.2f}).",
        file=sys.stderr,
    )
    yield


app = FastAPI(title="Studiesystem — Fas 0", lifespan=lifespan)

# Registered before the static mount so it covers the shell as well as the API.
app.middleware("http")(gate)


@app.get("/api/health")
def health(diag: bool = False) -> dict:
    """The platform's probe, and — with ?diag=1 — an outbound connectivity test.

    The diagnosis is off by default so the health check the platform runs every
    few seconds stays instant. It is on this unauthenticated route on purpose:
    when the app cannot reach the API, the person debugging it is outside the
    gate, and a diagnosis you need a cookie to read is a diagnosis you cannot
    get at exactly when you need it. It returns error classes and booleans; the
    key and the data never appear.
    """
    body = {
        "store": store.backend,
        "persistent": store.backend == "postgres",
        "grading_model": settings.grading_model,
        "generation_model": settings.generation_model,
        "concept_model": settings.concept_model,
        # False on the memory store: the cap cannot survive a restart there.
        "budget_persistent": store.backend == "postgres",
        # Which commit is actually serving. Render sets this; locally it is
        # None. Without it "did my push land?" cannot be answered from outside
        # the dashboard, and the gate keeps everything else out.
        "commit": (os.getenv("RENDER_GIT_COMMIT") or "local")[:7],
    }
    if diag:
        body["outbound"] = probe()
    return body


@app.get("/api/budget", response_model=BudgetStatus)
def budget() -> BudgetStatus:
    spent = store.month_spend()
    cap = settings.monthly_budget_usd
    return BudgetStatus(
        month=current_month(),
        spent_usd=round(spent, 4),
        cap_usd=cap,
        target_usd=settings.monthly_target_usd,
        remaining_usd=round(max(0.0, cap - spent), 4),
        fraction_of_cap=round(spent / cap, 4) if cap else 1.0,
        over_target=spent > settings.monthly_target_usd,
        exhausted=spent >= cap,
        by_model=store.spend_breakdown(),
    )


@app.post("/api/grade", response_model=GradingOutput)
def grade_endpoint(inp: GradingInput) -> GradingOutput:
    """The §8 contract, standing alone. No database, no session — this is the
    endpoint the eval-set and every prompt experiment run against."""
    try:
        return grade(inp)
    except BudgetExceeded as e:
        raise HTTPException(status_code=402, detail=str(e)) from e
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


# One generation at a time. Two runs of the same material is the same bill
# twice, and a reloaded page used to start a second one silently.
_generating = threading.Lock()


@app.post("/api/generate", response_model=GenerationDraft)
def generate(req: GenerateRequest) -> GenerationDraft:
    """Text in, concepts + items out — and nothing written to the database.

    The result is held for review and saved only by /api/ingest. A bad item is
    not a bad answer: it gets scheduled, repeated and graded against for weeks,
    and by then it is indistinguishable from a bad memory.
    """
    text = req.text.strip()
    if len(text) < 200:
        raise HTTPException(422, "Paste at least a few paragraphs of material.")

    course_name = req.course_name or settings.course_name
    course_id = req.course_id or store.ensure_course(course_name)

    if not _generating.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="En generering pågår redan. Vänta tills den är klar — "
                   "att starta en till kostar lika mycket en gång till.",
        )

    before = store.month_spend()
    try:
        draft = generate_draft(
            text,
            course_id=course_id,
            course_name=course_name,
            spend_before=before,
            spend_after=store.month_spend,
        )
    except BudgetExceeded as e:
        raise HTTPException(status_code=402, detail=str(e)) from e
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Generation produced an invalid item: {e}") from e
    finally:
        _generating.release()

    # To the database, not to process memory. Reviewing a draft takes longer
    # than a free instance stays awake, and a draft that dies while you read it
    # is a review gate that deletes what you are reviewing.
    store.save_draft(draft.draft_id, draft.model_dump())
    return draft


@app.post("/api/ingest", response_model=IngestResponse)
def ingest(req: ConfirmRequest) -> IngestResponse:
    """Save a reviewed draft. Everything not explicitly rejected is stored."""
    payload = store.pop_draft(req.draft_id)
    if payload is None:
        raise HTTPException(
            404,
            "Utkastet finns inte längre. Det sparas nu i databasen, så det här "
            "ska inte kunna hända igen — men det som redan hann försvinna är borta.",
        )
    draft = GenerationDraft(**payload)

    rejected_concepts = set(req.reject_concepts)
    rejected_items = set(req.reject_items)

    n_concepts = n_items = 0
    for concept in draft.concepts:
        if concept.name in rejected_concepts:
            continue
        keep = [i for i in concept.items
                if f"{concept.name}::{i.prompt}" not in rejected_items]
        if not keep:
            # A concept with no items is a row that can never come up for review.
            continue
        concept_id = store.add_concept(
            draft.course_id,
            DraftConcept(
                name=concept.name,
                importance=concept.importance,
                short_explanation=concept.short_explanation,
            ),
        )
        n_concepts += 1
        for item in keep:
            store.add_item(concept_id, item)
            n_items += 1

    return IngestResponse(course_id=draft.course_id, concepts=n_concepts, items=n_items)


# One sitting. Held in memory: a sitting is a stretch of attention, and a
# process that restarted was not in the middle of one.
#
# This is what makes the session caps real. Without it /api/next recomputes the
# queue on every call and always hands back its head — and an item the student
# never masters is *always* due, because FSRS puts a half-known one back in six
# minutes. In a simulated week that item was asked 319 times, at $0.011 of
# grading each, while other items waited.
_sitting: dict = {"day": None, "asked": set(), "attempts": {}, "reviews": 0, "started": None}


def _current_sitting() -> dict:
    """The sitting in progress, rolled over at the UTC day boundary."""
    now = datetime.now(timezone.utc)
    if _sitting["day"] != now.date():
        _sitting.update(day=now.date(), asked=set(), attempts={}, reviews=0, started=now)
    return _sitting


def _sitting_remaining(sitting: dict) -> int:
    """Reviews left in this sitting, by whichever cap binds first."""
    elapsed_min = (datetime.now(timezone.utc) - sitting["started"]).total_seconds() / 60
    by_time = settings.session_max_minutes - elapsed_min
    if by_time <= 0:
        return 0
    return max(0, settings.session_max_items - sitting["reviews"])


def _due_item(row: dict, sitting: dict | None = None) -> DueItem:
    return DueItem(
        attempt=(sitting["attempts"].get(row["item_id"], 0) if sitting else 0),
        attempts_allowed=settings.session_max_per_item,
        item_id=row["item_id"],
        concept_id=row["concept_id"],
        concept_name=row["concept_name"],
        type=row["type"],
        prompt=row["prompt"],
        due_at=row["due_at"].isoformat() if row["due_at"] else None,
        seen_before=bool(row["seen_before"]),
    )


def _session(course_id: str, sitting: dict) -> list[dict]:
    return session_queue(
        store.due_items(course_id),
        max_items=_sitting_remaining(sitting),
        max_minutes=settings.session_max_minutes,
        already_asked=sitting["asked"],
        attempts=sitting["attempts"],
        max_per_item=settings.session_max_per_item,
    )


@app.get("/api/next", response_model=DueItem | None)
def next_item() -> DueItem | None:
    """The next question in today's sitting, or null when the sitting is done.

    Re-derived on every call rather than held as a cursor: a review changes what
    is due, and a stale cursor would keep asking from a queue that no longer
    exists.
    """
    sitting = _current_sitting()
    if _sitting_remaining(sitting) <= 0:
        return None
    queue = _session(store.ensure_course(settings.course_name), sitting)
    return _due_item(queue[0], sitting) if queue else None


@app.get("/api/session", response_model=SessionQueue)
def session() -> SessionQueue:
    """The sitting, so the UI can show how much is left and the queue can be
    inspected without answering it."""
    sitting = _current_sitting()
    course_id = store.ensure_course(settings.course_name)
    due = store.due_items(course_id)
    queue = _session(course_id, sitting)
    return SessionQueue(
        course_id=course_id,
        due_total=len(due),
        items=[_due_item(r, sitting) for r in queue],
        concepts_covered=len({r["concept_id"] for r in queue}),
        capped=len(queue) < len(due),
        reviews_done=sitting["reviews"],
        reviews_left=_sitting_remaining(sitting),
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
    except BudgetExceeded as e:
        raise HTTPException(status_code=402, detail=str(e)) from e
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    last = store.latest_review(req.item_id)
    state, due_at = fsrs_review(last["fsrs_state"] if last else None, result.verdict)

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

    sitting = _current_sitting()
    sitting["asked"].add(req.item_id)
    sitting["attempts"][req.item_id] = sitting["attempts"].get(req.item_id, 0) + 1
    sitting["reviews"] += 1

    interval = (due_at - datetime.now(timezone.utc)).total_seconds() / 86400
    return ReviewResponse(
        grading=result,
        reference_answer=item["reference_answer"],
        next_due_at=due_at.isoformat(),
        interval_days=round(interval, 3),
        rubric=[RubricCriterion(**c) for c in item["rubric"]],
    )


@app.get("/api/progress", response_model=ProgressResponse)
def progress() -> ProgressResponse:
    course_id = store.ensure_course(settings.course_name)
    return ProgressResponse(
        course_id=course_id,
        due_now=len(store.due_items(course_id)),
        concepts=store.progress(course_id),
    )


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str) -> dict:
    """Remove an item for good.

    The review gate is meant to catch a bad question before it is scheduled,
    but it only works if you spot it there. Some only reveal themselves once
    you have been asked them — and until now there was no way to remove one,
    at all.
    """
    if not store.delete_item(item_id):
        raise HTTPException(404, "No such item.")
    return {"deleted": item_id}


# Typing this is the guard. The access gate already stops anyone else, so what
# is left to protect against is a mis-click and a stray fetch — and neither of
# those types a word.
RESET_PHRASE = "radera allt"


@app.post("/api/reset")
def reset(req: ResetRequest) -> dict:
    """Delete every concept, item and review. Starting a new course.

    Keeps the spend ledger: that money is spent whether or not the questions it
    bought still exist, and zeroing it would make the monthly cap lie.
    """
    if req.confirm.strip().lower() != RESET_PHRASE:
        raise HTTPException(422, f'Skriv "{RESET_PHRASE}" för att bekräfta.')

    counts = store.reset_content()
    # Yesterday's sitting counted items that no longer exist.
    _sitting.update(day=None, asked=set(), attempts={}, reviews=0, started=None)
    return {"deleted": counts}


@app.get("/api/drafts", response_model=list[GenerationDraft])
def drafts() -> list[GenerationDraft]:
    """Drafts generated but not yet saved. They cost money and they are only
    reachable from here once the page that made them is gone."""
    return [GenerationDraft(**p) for p in store.pending_drafts()]


@app.get("/api/history", response_model=HistoryResponse)
def history(limit: int = 200) -> HistoryResponse:
    """Past reviews, grouped by day. A log: the questions without their answers,
    so reading it is not a cheap substitute for answering them again."""
    course_id = store.ensure_course(settings.course_name)
    rows = store.history(course_id, limit=limit)

    days: list[HistoryDay] = []
    for row in rows:
        when = row["reviewed_at"]
        day = when.date().isoformat()
        entry = HistoryRow(
            reviewed_at=when.isoformat(),
            prompt=row["prompt"],
            concept_name=row["concept_name"],
            verdict=row["verdict"],
            score=round(float(row["score"]), 4),
            confidence=round(float(row["confidence"]), 4),
            confidence_gap=round(float(row["confidence"]) - float(row["score"]), 4),
        )
        if days and days[-1].day == day:
            days[-1].rows.append(entry)
        else:
            days.append(HistoryDay(day=day, rows=[entry]))

    return HistoryResponse(course_id=course_id, total=len(rows), days=days)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


# The frontend is static files, mounted last so every /api/ route wins the match.
# A service worker only registers for the scope it is served from, so sw.js has
# to sit at the root — which mounting web/ at "/" gives for free.
app.mount("/", StaticFiles(directory=WEB), name="web")
