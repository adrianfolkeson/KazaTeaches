"""Import-time generation: pasted text -> concepts -> items with rubrics.

§5: all generation happens here, in a batch, at import. Never on-demand in the
middle of a study session — that is where latency and cost would land on the
one loop that has to feel frictionless.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from uuid import uuid4

from app.ai.client import cached, parse
from app.ai.prompts import CONCEPT_SYSTEM, ITEM_SYSTEM
from app.budget import BudgetExceeded, current_month, estimate_import_usd
from app.config import settings

# Enough to cut a fifteen-minute import to a couple of minutes; low enough that
# a burst past the budget cap costs cents, not dollars.
CONCURRENT_GENERATIONS = 6
from app.schemas import (
    ITEMS_PER_IMPORTANCE,
    DraftConcept,
    DraftConceptList,
    DraftConceptWithItems,
    DraftItem,
    DraftItemList,
    GenerationDraft,
)


def extract_concepts(source_text: str, *, model: str | None = None) -> list[DraftConcept]:
    """Not the cheap lane, despite §5 — deciding what counts as one concept is
    judgment, and it is the decision every later cost and every later item
    inherits. Splitting one idea into four does not just produce four weaker
    concepts; it generates four items' worth of questions and charges for them.
    """
    result = parse(
        model=model or settings.concept_model,
        system=[cached(CONCEPT_SYSTEM)],
        user=f"<material>\n{source_text}\n</material>\n\nExtract the concepts.",
        output_format=DraftConceptList,
        max_tokens=8000,
        effort="high",
    )
    return result.concepts


def generate_items(
    concept: DraftConcept,
    source_text: str,
    *,
    model: str | None = None,
) -> list[DraftItem]:
    """Expensive lane: the rubric written here is what every future grading of
    this item is matched against.

    `source_text` sits in a cached system block, identical across every concept
    in the import, so the material is paid for once per import rather than once
    per concept.
    """
    n_items = ITEMS_PER_IMPORTANCE[concept.importance]
    result = parse(
        model=model or settings.generation_model,
        system=[cached(ITEM_SYSTEM), cached(f"<material>\n{source_text}\n</material>")],
        user=(
            f"Concept: {concept.name}\n"
            f"What it is: {concept.short_explanation}\n"
            f"Importance: {concept.importance}/5\n\n"
            f"Write {n_items} items for this concept, grounded in the material above."
        ),
        output_format=DraftItemList,
        max_tokens=8000,
        effort="high",
    )
    return _validate(result.items, concept)


def _validate(items: list[DraftItem], concept: DraftConcept) -> list[DraftItem]:
    """Reject a malformed rubric at generation instead of discovering it mid-review.

    Structural checks only — whether the rubric is *right* is what
    evals/gen_selfcheck.py answers, by grading each reference answer against its
    own rubric. These are the breaks that make a rubric ungradeable at all.
    """
    ok: list[DraftItem] = []
    for item in items:
        ids = [c.id for c in item.rubric]
        if len(item.rubric) < 2:
            raise ValueError(f"{concept.name}: item rubric has fewer than 2 criteria")
        if len(set(ids)) != len(ids):
            raise ValueError(f"{concept.name}: duplicate rubric ids {ids}")
        if not any(c.required for c in item.rubric):
            raise ValueError(f"{concept.name}: item rubric has no required criterion")
        bad = [c.id for c in item.rubric if not re.fullmatch(r"[a-z0-9_]+", c.id)]
        if bad:
            # Ids are permanent — every stored grading carries them.
            raise ValueError(f"{concept.name}: rubric ids are not snake_case ascii: {bad}")
        ok.append(item)
    return ok


def generate_draft(
    source_text: str,
    *,
    course_id: str,
    course_name: str,
    concept_model: str | None = None,
    item_model: str | None = None,
    spend_before: float = 0.0,
    spend_after: Callable[[], float] | None = None,
) -> GenerationDraft:
    """Text in, concepts + items out. Stores nothing.

    The split matters: this function is the whole generator, and the only thing
    standing between it and the database is a human saying yes. A bad item is
    not a bad answer — it is scheduled, repeated, and graded against for weeks,
    and by then it is indistinguishable from a bad memory.
    """
    concepts = extract_concepts(source_text, model=concept_model)

    # Item generation is one call per concept, each checked against the cap
    # separately. Without this a large batch runs until the money runs out and
    # returns a draft that is silently missing its last concepts' items.
    if spend_after is not None:
        spent = spend_after()
        needed = estimate_import_usd(len(concepts))
        remaining = settings.monthly_budget_usd - spent
        if needed > remaining:
            raise BudgetExceeded(spent, settings.monthly_budget_usd, current_month())

    # One call per concept, and they do not depend on each other. Run
    # sequentially, fifteen concepts took a quarter of an hour of a spinner —
    # long enough that the container idled out and took the draft with it.
    # Bounded concurrency rather than unbounded: the cap check happens once,
    # before the loop, so a burst of parallel calls can overshoot it, and the
    # bound is what keeps that overshoot to cents.
    with ThreadPoolExecutor(max_workers=CONCURRENT_GENERATIONS) as pool:
        results = list(pool.map(
            lambda c: (c, generate_items(c, source_text, model=item_model)), concepts))

    out: list[DraftConceptWithItems] = []
    n_items = 0
    for concept, items in results:
        n_items += len(items)
        out.append(
            DraftConceptWithItems(
                name=concept.name,
                importance=concept.importance,
                short_explanation=concept.short_explanation,
                items=items,
            )
        )

    return GenerationDraft(
        draft_id=str(uuid4()),
        course_id=course_id,
        course_name=course_name,
        concepts=out,
        n_items=n_items,
        cost_usd=round((spend_after() - spend_before) if spend_after else 0.0, 4),
    )
