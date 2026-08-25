"""FSRS on items, and the interleaved due queue.

§3: the ITEM is the scheduling unit. Concept mastery is derived from item
reviews, never scheduled directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fsrs import Card, Scheduler

from app.scoring import rating_from
from app.schemas import Verdict

_scheduler = Scheduler()


def new_card() -> Card:
    return Card()


def card_from_state(state: dict | None) -> Card:
    return Card.from_dict(state) if state else new_card()


def review(state: dict | None, score: float, verdict: Verdict) -> tuple[dict, datetime]:
    """Apply one review. Returns (fsrs_state, due_at)."""
    card, _log = _scheduler.review_card(
        card_from_state(state),
        rating_from(score, verdict),
        review_datetime=datetime.now(timezone.utc),
    )
    return card.to_dict(), card.due


def interleave(due: list[dict], key: str = "concept_id") -> list[dict]:
    """Round-robin across concepts so consecutive questions come from different
    ones (§0.4 interleaving). Within a concept, the most overdue item goes first.
    """
    buckets: dict[str, list[dict]] = {}
    for row in due:
        buckets.setdefault(row[key], []).append(row)

    # Concepts with the most due items first, so the queue drains evenly.
    order = sorted(buckets, key=lambda k: (-len(buckets[k]), k))
    out: list[dict] = []
    while any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                out.append(buckets[k].pop(0))
    return out
