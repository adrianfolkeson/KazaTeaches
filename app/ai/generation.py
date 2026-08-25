"""Import-time generation: pasted text -> concepts -> items with rubrics.

§5: all generation happens here, in a batch, at import. Never on-demand in the
middle of a study session — that is where latency and cost would land on the
one loop that has to feel frictionless.
"""

from __future__ import annotations

from app.ai.client import block, cached, parse
from app.ai.prompts import CONCEPT_SYSTEM, ITEM_SYSTEM
from app.config import settings
from app.schemas import DraftConcept, DraftConceptList, DraftItem, DraftItemList


def extract_concepts(source_text: str, *, model: str | None = None) -> list[DraftConcept]:
    """Cheap lane (§5): a draft concept list is classification, not judgment."""
    result = parse(
        model=model or settings.cheap_model,
        system=[cached(CONCEPT_SYSTEM)],
        user=f"<material>\n{source_text}\n</material>\n\nExtract the concepts.",
        output_format=DraftConceptList,
        max_tokens=8000,
        # Cheap models reject adaptive thinking and effort.
        thinking=False,
        effort=None,
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
    n_items = 2 if concept.importance <= 2 else 3 if concept.importance <= 4 else 4
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
    """Reject a malformed rubric at import instead of discovering it mid-review."""
    ok: list[DraftItem] = []
    for item in items:
        ids = [c.id for c in item.rubric]
        if len(item.rubric) < 2:
            raise ValueError(f"{concept.name}: item rubric has fewer than 2 criteria")
        if len(set(ids)) != len(ids):
            raise ValueError(f"{concept.name}: duplicate rubric ids {ids}")
        if not any(c.required for c in item.rubric):
            raise ValueError(f"{concept.name}: item rubric has no required criterion")
        ok.append(item)
    return ok
