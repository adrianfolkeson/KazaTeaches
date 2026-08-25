"""FSRS on items, and the interleaved due queue.

§3: the ITEM is the scheduling unit. Concept mastery is derived from item
reviews, never scheduled directly — so nothing here knows what a concept is
except as something to interleave across.

FSRS itself is deterministic and fully tested upstream; nothing in this file
invents an interval. What it decides is the *rating* a review earns and the
order the day's items are asked in.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler

from app.config import settings
from app.schemas import Verdict


def build_scheduler(*, fuzz: bool | None = None, maximum_interval: int | None = None) -> Scheduler:
    """The app's FSRS scheduler.

    Two settings differ from py-fsrs defaults, and neither invents an interval:

    - `maximum_interval` is a year rather than 36500 days. On defaults, four
      correct answers in a row push an item five years out, which for one course
      in one semester is indistinguishable from deleting it.
    - `enable_fuzzing` stays on, because a batch imported on one day would
      otherwise come due on one day forever. It is exposed so tests can turn it
      off: fuzzing is the one thing that makes scheduling non-reproducible.
    """
    return Scheduler(
        enable_fuzzing=settings.fsrs_fuzz if fuzz is None else fuzz,
        maximum_interval=maximum_interval or settings.fsrs_max_interval_days,
    )


_scheduler = build_scheduler()

# Verdict -> FSRS rating. Deliberately from the verdict, not the score: the
# verdict already folds in the two things that matter to scheduling — whether
# every required criterion landed, and whether the student was sure while
# missing them. A score threshold would re-derive that worse, and would drift
# every time the scoring weights are tuned.
RATING_FOR_VERDICT: dict[str, Rating] = {
    # Sure and wrong is the failure mode spaced repetition exists to catch. It
    # gets the same shortest interval as plainly wrong — being confident about
    # it is a reason to see the item sooner, never later.
    "confidently_wrong": Rating.Again,
    "wrong": Rating.Again,
    "partial": Rating.Hard,
    "correct_incomplete": Rating.Good,
    "correct": Rating.Easy,
}

# How long one free-text review takes, for the session's time cap. Writing an
# answer, reading the grading and the reference is a couple of minutes; this is
# a planning figure, not a measurement.
SECONDS_PER_ITEM = 100


def rating_for(verdict: Verdict) -> Rating:
    """The FSRS rating a verdict earns."""
    try:
        return RATING_FOR_VERDICT[verdict]
    except KeyError:  # pragma: no cover - guarded by the Verdict literal
        raise ValueError(f"no FSRS rating defined for verdict {verdict!r}") from None


def new_card() -> Card:
    return Card()


def card_from_state(state: dict | None) -> Card:
    """A review with no prior state is an item's first time out — a fresh card,
    which is why a newly generated item is due immediately."""
    return Card.from_dict(state) if state else new_card()


def review(
    state: dict | None,
    verdict: Verdict,
    *,
    now: datetime | None = None,
    scheduler: Scheduler | None = None,
) -> tuple[dict, datetime]:
    """Apply one review. Returns (fsrs_state, due_at).

    `now` and `scheduler` are injectable so a whole week can be simulated
    without waiting for one, and without fuzzing making the result unrepeatable.
    """
    card, _log = (scheduler or _scheduler).review_card(
        card_from_state(state),
        rating_for(verdict),
        review_datetime=now or datetime.now(timezone.utc),
    )
    return card.to_dict(), card.due


def interleave(due: list[dict], key: str = "concept_id") -> list[dict]:
    """Order the queue so consecutive questions come from different concepts
    (§0.4 interleaving), and never block one topic.

    Greedy: at each step take the concept with the most items still waiting,
    except the one just asked. Falling back to the previous concept only when
    nothing else is left means adjacent repeats happen exactly when they are
    unavoidable — when one concept holds more than half of what is due.

    Within a concept the most overdue item goes first; an item that has never
    been reviewed (`due_at` None) is the most overdue thing there is.
    """
    buckets: dict[str, list[dict]] = {}
    for row in due:
        buckets.setdefault(row[key], []).append(row)

    for rows in buckets.values():
        # None sorts before any datetime: a never-seen item leads its concept.
        rows.sort(key=lambda r: (r["due_at"] is not None, r["due_at"] or datetime.min))

    out: list[dict] = []
    previous: str | None = None
    while any(buckets.values()):
        available = [k for k, rows in buckets.items() if rows and k != previous]
        if not available:
            # Only the concept we just asked has anything left.
            available = [k for k, rows in buckets.items() if rows]
        # Most-loaded first so the queue drains evenly; name breaks ties so the
        # order is reproducible across runs.
        pick = min(available, key=lambda k: (-len(buckets[k]), k))
        out.append(buckets[pick].pop(0))
        previous = pick
    return out


def session_queue(
    due: list[dict],
    *,
    max_items: int,
    max_minutes: int,
    already_asked: frozenset[str] | set[str] = frozenset(),
    attempts: dict[str, int] | None = None,
    max_per_item: int | None = None,
    key: str = "concept_id",
) -> list[dict]:
    """The interleaved queue, cut to one sitting.

    Two orderings, in this order:

    1. Items not yet asked in this sitting, interleaved across concepts.
    2. Items already asked, interleaved among themselves.

    The split exists because FSRS puts a failed item back in about a minute and
    a half-known one in six, so an item the student never masters is *always*
    due. Ordering purely by due date hands that one item the whole session: in a
    simulated week it was asked 322 times while another item was never asked at
    all. Relearning inside a sitting is worth keeping — it is what those short
    steps are for — but only once everything else has had its turn.

    `attempts` + `max_per_item` retire an item for the rest of the sitting once
    it has been asked enough times. Without it, one item the student cannot get
    right consumes the whole day: in the same simulated week it took 123 of 140
    reviews, because it was the only thing still due.

    The cap is applied after interleaving, never before: cutting first would
    take the top N most-overdue items, which is the single-concept block that
    interleaving exists to break up.
    """
    if attempts and max_per_item:
        # An item still unclear after a few tries today is not going to clear on
        # the next try today. Spacing it to tomorrow is the whole thesis.
        due = [r for r in due if attempts.get(r["item_id"], 0) < max_per_item]

    fresh = [r for r in due if r["item_id"] not in already_asked]
    again = [r for r in due if r["item_id"] in already_asked]
    queue = interleave(fresh, key=key) + interleave(again, key=key)
    by_time = max(1, (max_minutes * 60) // SECONDS_PER_ITEM)
    return queue[: min(max_items, by_time)]
