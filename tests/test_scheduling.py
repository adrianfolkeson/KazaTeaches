"""Scheduling, offline. FSRS is deterministic, so none of this needs a model —
and none of it may reach one. These assert the two things that make this a
spaced-repetition system rather than a grader with a database: that a review
moves the next due date in the right direction, and that a day's queue never
blocks one topic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fsrs import Rating

from app.scheduling import (
    RATING_FOR_VERDICT,
    SECONDS_PER_ITEM,
    build_scheduler,
    interleave,
    rating_for,
    session_queue,
)
from app.scheduling import review as _review

T0 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)

# Fuzzing is on in the app on purpose — without it a batch imported on one day
# comes due on one day forever. It is also the only thing that makes scheduling
# unrepeatable, so every test here runs against an unfuzzed scheduler and says
# so, rather than asserting loose bounds around a random jitter.
SCHEDULER = build_scheduler(fuzz=False)


def review(state, verdict, *, now=None):
    return _review(state, verdict, now=now, scheduler=SCHEDULER)


def days_until(due: datetime, now: datetime) -> float:
    return (due - now).total_seconds() / 86400


def run(verdicts: list[str], start: datetime = T0) -> list[tuple[float, datetime]]:
    """Review an item repeatedly, each time on its own due date. Returns the
    interval granted by each review, in days."""
    state, now, out = None, start, []
    for verdict in verdicts:
        state, due = review(state, verdict, now=now)
        out.append((days_until(due, now), due))
        now = due
    return out


# --- verdict -> rating -----------------------------------------------------


def test_every_verdict_maps_to_a_rating():
    """A verdict with no rating would crash mid-review, after the student has
    already paid for the grading."""
    from typing import get_args

    from app.schemas import Verdict

    assert set(get_args(Verdict)) == set(RATING_FOR_VERDICT)


def test_the_mapping_is_the_one_the_spec_asked_for():
    assert rating_for("wrong") == Rating.Again
    assert rating_for("confidently_wrong") == Rating.Again
    assert rating_for("partial") == Rating.Hard
    assert rating_for("correct_incomplete") == Rating.Good
    assert rating_for("correct") == Rating.Easy


def test_being_sure_and_wrong_is_rated_no_better_than_being_wrong():
    """The whole point of asking for confidence: it can only shorten the
    interval, never lengthen it."""
    assert rating_for("confidently_wrong") == rating_for("wrong") == Rating.Again


# --- intervals move in the right direction ---------------------------------


def test_intervals_grow_when_answers_are_correct():
    intervals = [days for days, _ in run(["correct"] * 5)]
    assert intervals == sorted(intervals), intervals
    assert intervals[-1] > intervals[0]
    assert intervals[-1] > 30, "five correct reviews should reach months, not days"


def test_intervals_stop_at_the_configured_ceiling():
    """FSRS defaults run to 36500 days. An item due in 100 years is deleted, not
    scheduled — build_scheduler caps it at a year."""
    intervals = [days for days, _ in run(["correct"] * 8)]
    assert max(intervals) <= 365
    assert intervals[-1] == 365


def test_correct_incomplete_also_grows_but_slower_than_correct():
    """Good and Easy both advance; a partial gap should not advance as fast as a
    complete answer, or `correct_incomplete` stops meaning anything."""
    good = [d for d, _ in run(["correct_incomplete"] * 5)]
    easy = [d for d, _ in run(["correct"] * 5)]
    assert good == sorted(good)
    assert good[-1] < easy[-1]


def grow_then(final_verdict: str, n_correct: int = 4) -> tuple[float, float]:
    """Answer correctly n times, then once with `final_verdict`. Returns the
    interval the last correct review granted and the one the lapse granted,
    both measured from the review that granted them."""
    state, now = None, T0
    grown = 0.0
    for _ in range(n_correct):
        state, due = review(state, "correct", now=now)
        grown = days_until(due, now)
        now = due
    _, due_after = review(state, final_verdict, now=now)
    return grown, days_until(due_after, now)


def test_again_collapses_an_interval_that_had_grown():
    """The failure this system exists for: an item you thought you knew."""
    grown, after_lapse = grow_then("wrong")
    assert grown > 30, f"four correct reviews should reach months, got {grown:.1f}d"
    assert after_lapse < 1, "a lapse must bring the item back at once"
    assert after_lapse < grown / 100


def test_confidently_wrong_collapses_the_interval_the_same_way():
    """Sure and wrong is treated exactly as wrong — never softened because the
    student sounded certain."""
    _, after_wrong = grow_then("wrong")
    _, after_sure = grow_then("confidently_wrong")
    assert after_sure == after_wrong < 1


def test_partial_keeps_the_item_close():
    """Hard should not push an item out of sight — a half-known item is the one
    worth seeing again soon."""
    intervals = [d for d, _ in run(["partial"] * 4)]
    assert all(d < 30 for d in intervals), intervals


def test_a_new_item_is_due_immediately():
    """No prior state means a fresh card, which is what puts a just-generated
    item into today's queue without a special case anywhere."""
    from app.scheduling import card_from_state

    assert card_from_state(None).due <= datetime.now(timezone.utc)


def test_review_is_reproducible_without_fuzzing():
    """Same state, same verdict, same clock -> same due date. With fuzzing on
    this is deliberately false, which is why the app's scheduler is injectable
    and every test here uses an unfuzzed one."""
    a_state, a_due = review(None, "correct", now=T0)
    b_state, b_due = review(None, "correct", now=T0)
    assert a_due == b_due
    assert a_state["stability"] == b_state["stability"]


# --- the queue -------------------------------------------------------------


def due_row(concept: str, item: str, due_at: datetime | None = T0):
    return {"concept_id": concept, "item_id": item, "due_at": due_at,
            "concept_name": concept, "type": "definition", "prompt": item,
            "seen_before": due_at is not None}


def adjacent_repeats(queue: list[dict]) -> int:
    return sum(1 for a, b in zip(queue, queue[1:]) if a["concept_id"] == b["concept_id"])


def test_two_concepts_alternate():
    queue = interleave([due_row("a", "a1"), due_row("a", "a2"),
                        due_row("b", "b1"), due_row("b", "b2")])
    assert [r["concept_id"] for r in queue] == ["a", "b", "a", "b"]
    assert adjacent_repeats(queue) == 0


def test_nothing_repeats_while_another_concept_still_has_items():
    """The rule as stated: consecutive items from one concept only when it is
    unavoidable."""
    due = ([due_row("a", f"a{i}") for i in range(4)]
           + [due_row("b", f"b{i}") for i in range(3)]
           + [due_row("c", f"c{i}") for i in range(2)])
    queue = interleave(due)
    assert len(queue) == 9
    assert adjacent_repeats(queue) == 0, [r["concept_id"] for r in queue]


def test_repeats_only_appear_once_one_concept_holds_the_majority():
    """Five of one and one of another cannot alternate. The repeats that remain
    are the arithmetic minimum, not a scheduling failure."""
    queue = interleave([due_row("a", f"a{i}") for i in range(5)] + [due_row("b", "b1")])
    ids = [r["concept_id"] for r in queue]
    assert ids.count("a") == 5
    # 5 a's split by the single b: 3 unavoidable adjacencies.
    assert adjacent_repeats(queue) == 3, ids


def test_a_single_concept_is_not_an_error():
    queue = interleave([due_row("a", f"a{i}") for i in range(3)])
    assert len(queue) == 3


def test_an_empty_queue_is_empty():
    assert interleave([]) == []


def test_the_most_overdue_item_leads_its_concept():
    old = T0 - timedelta(days=10)
    recent = T0 - timedelta(hours=1)
    queue = interleave([due_row("a", "recent", recent), due_row("a", "old", old),
                        due_row("b", "b1", T0)])
    assert [r["item_id"] for r in queue if r["concept_id"] == "a"] == ["old", "recent"]


def test_a_never_reviewed_item_leads_its_concept():
    """A just-generated item has no due date and is the most overdue thing
    there is — it has never been seen at all."""
    queue = interleave([due_row("a", "seen", T0 - timedelta(days=30)),
                        due_row("a", "new", None)])
    assert [r["item_id"] for r in queue] == ["new", "seen"]


def test_the_queue_order_is_reproducible():
    due = [due_row("a", f"a{i}") for i in range(3)] + [due_row("b", f"b{i}") for i in range(3)]
    assert [r["item_id"] for r in interleave(due)] == [r["item_id"] for r in interleave(due)]


# --- session cap -----------------------------------------------------------


def test_the_session_is_capped_by_count():
    due = [due_row(f"c{i % 4}", f"i{i}") for i in range(50)]
    assert len(session_queue(due, max_items=12, max_minutes=999)) == 12


def test_the_session_is_capped_by_time():
    due = [due_row(f"c{i % 4}", f"i{i}") for i in range(50)]
    queue = session_queue(due, max_items=999, max_minutes=10)
    assert len(queue) == (10 * 60) // SECONDS_PER_ITEM


def test_the_cap_is_applied_after_interleaving_not_before():
    """Cutting first takes the top N most overdue, which is the single-concept
    block interleaving exists to break up."""
    due = ([due_row("a", f"a{i}", T0 - timedelta(days=100)) for i in range(10)]
           + [due_row("b", f"b{i}", T0) for i in range(10)])
    queue = session_queue(due, max_items=6, max_minutes=999)
    assert len({r["concept_id"] for r in queue}) == 2, [r["concept_id"] for r in queue]
    assert adjacent_repeats(queue) == 0


def test_a_short_session_still_returns_one_item():
    due = [due_row("a", "a1")]
    assert len(session_queue(due, max_items=20, max_minutes=0)) == 1


def test_items_that_do_not_fit_stay_due():
    """The cap is a cut, not a discard: what does not fit is still overdue and
    leads tomorrow's queue."""
    due = [due_row(f"c{i % 3}", f"i{i}") for i in range(30)]
    queue = session_queue(due, max_items=10, max_minutes=999)
    assert len(queue) == 10
    served = {r["item_id"] for r in queue}
    assert len([r for r in due if r["item_id"] not in served]) == 20


def test_an_item_retires_from_the_sitting_after_enough_attempts():
    """FSRS learning steps assume a card you re-see in seconds. Here a review is
    a written answer that costs a grading call, so an item still unclear on the
    third try today belongs to tomorrow, not to the next six minutes."""
    due = [due_row("a", "stuck"), due_row("b", "b1")]
    queue = session_queue(due, max_items=20, max_minutes=99,
                          attempts={"stuck": 3}, max_per_item=3)
    assert [r["item_id"] for r in queue] == ["b1"]


def test_an_item_below_the_attempt_limit_still_comes_back():
    due = [due_row("a", "stuck"), due_row("b", "b1")]
    queue = session_queue(due, max_items=20, max_minutes=99,
                          attempts={"stuck": 2}, max_per_item=3)
    assert {r["item_id"] for r in queue} == {"stuck", "b1"}


def test_without_an_attempt_limit_nothing_is_retired():
    """The limit is opt-in, so session_queue stays usable for plain ordering."""
    due = [due_row("a", "stuck"), due_row("b", "b1")]
    queue = session_queue(due, max_items=20, max_minutes=99, attempts={"stuck": 99})
    assert len(queue) == 2


def test_one_unlearnable_item_cannot_consume_the_whole_sitting():
    """The failure this cap exists for, stated as arithmetic: with the item
    retired, a sitting of 20 cannot spend more than the limit on it."""
    stuck_asked = 0
    attempts: dict[str, int] = {}
    asked: set[str] = set()
    for _ in range(20):
        queue = session_queue([due_row("a", "stuck")], max_items=20, max_minutes=99,
                              already_asked=asked, attempts=attempts, max_per_item=3)
        if not queue:
            break
        k = queue[0]["item_id"]
        asked.add(k)
        attempts[k] = attempts.get(k, 0) + 1
        stuck_asked += 1
    assert stuck_asked == 3


# --- a simulated week ------------------------------------------------------


def test_a_week_of_study_produces_sane_intervals_and_an_interleaved_queue():
    """The gate: run a plausible week and assert the system behaves like spaced
    repetition — the shaky items keep coming back, the solid ones recede out of
    the week entirely, and no day's queue blocks a topic.
    """
    # Three concepts, two items each, covering the three trajectories that
    # matter: never wrong, wrong then mastered, and never quite mastered.
    items = {
        "tx1": ("tx", ["correct"]),                       # solid from the start
        "tx2": ("tx", ["correct_incomplete", "correct"]),  # nearly solid
        "acid1": ("acid", ["wrong", "partial", "correct_incomplete", "correct"]),  # recovers
        "acid2": ("acid", ["correct"]),                   # solid
        "iso1": ("iso", ["partial"]),                     # never masters it
        "iso2": ("iso", ["confidently_wrong", "partial", "correct_incomplete", "correct"]),
    }
    state: dict[str, dict | None] = {k: None for k in items}
    due_at: dict[str, datetime | None] = {k: None for k in items}
    last_interval: dict[str, float] = {k: 0.0 for k in items}
    first_interval: dict[str, float] = {}
    step: dict[str, int] = {k: 0 for k in items}

    day_reports = []
    # Half-hour ticks, so an item FSRS puts back in minutes is re-served the way
    # it would be inside a real sitting rather than skipped to the next day.
    asked_this_sitting: set[str] = set()
    for tick in range(7 * 48):
        now = T0 + timedelta(minutes=30 * tick)
        if tick % 48 == 0:
            # A new day is a new sitting: caps reset, nothing carries over.
            asked_this_sitting, attempts_today, reviews_today = set(), {}, 0
        if reviews_today >= 20:
            continue
        ready = [
            due_row(items[k][0], k, due_at[k])
            for k in items
            if due_at[k] is None or due_at[k] <= now
        ]
        if not ready:
            continue
        queue = session_queue(ready, max_items=20 - reviews_today, max_minutes=25,
                              already_asked=asked_this_sitting,
                              attempts=attempts_today, max_per_item=3)
        if not queue:
            continue
        if tick % 48 == 0:
            day_reports.append((tick // 48, [r["item_id"] for r in queue],
                                adjacent_repeats(queue)))

        # One answer per tick — a review takes minutes, not a whole day.
        k = queue[0]["item_id"]
        asked_this_sitting.add(k)
        attempts_today[k] = attempts_today.get(k, 0) + 1
        reviews_today += 1
        verdicts = items[k][1]
        verdict = verdicts[min(step[k], len(verdicts) - 1)]
        step[k] += 1
        state[k], due_at[k] = review(state[k], verdict, now=now)
        last_interval[k] = days_until(due_at[k], now)
        first_interval.setdefault(k, last_interval[k])

    # Every day's queue was interleaved as far as arithmetic allowed.
    for day, ids, repeats in day_reports:
        concepts = [items[i][0] for i in ids]
        biggest = max((concepts.count(c) for c in set(concepts)), default=0)
        unavoidable = max(0, 2 * biggest - len(concepts) - 1)
        assert repeats <= unavoidable, (day, concepts, repeats, unavoidable)

    # Day 0 offers everything: nothing has ever been seen.
    assert set(day_reports[0][1]) == set(items)

    # An item answered correctly every time leaves the week entirely.
    for solid in ("tx1", "acid2"):
        assert last_interval[solid] >= 7, (solid, last_interval[solid])

    # An item never quite mastered never leaves the near queue, however many
    # times it is asked. This is the item the student actually needs.
    assert last_interval["iso1"] < 1, last_interval["iso1"]
    assert step["iso1"] > 3 * step["tx1"], (step["iso1"], step["tx1"])

    # ...but it cannot eat the week either. Three attempts a day, seven days.
    assert step["iso1"] <= 7 * 3, step["iso1"]

    # A week of this course is a plausible amount of work, not a treadmill.
    assert sum(step.values()) < 60, step

    # An item that was wrong and then mastered recovers: its interval ends far
    # longer than the one its first, failed review granted.
    for recovered in ("acid1", "iso2"):
        assert last_interval[recovered] > first_interval[recovered] * 10, recovered
        assert last_interval[recovered] > 1, recovered

    # Effort followed difficulty, per item, which is the whole point of §3.
    assert step["acid1"] > step["acid2"]
    assert step["tx1"] == min(step.values())
    assert all(step[k] >= 1 for k in items), step


def test_fuzzing_is_what_makes_the_app_scheduler_unrepeatable():
    """Documents the trade the app makes, so nobody 'fixes' the jitter later:
    fuzzing spreads a same-day import across different days, at the cost of
    reproducibility."""
    fuzzed = build_scheduler(fuzz=True)
    dues = {_review(None, "correct", now=T0, scheduler=fuzzed)[1] for _ in range(40)}
    assert len(dues) > 1, "fuzzing should vary the due date"

    unfuzzed = build_scheduler(fuzz=False)
    dues = {_review(None, "correct", now=T0, scheduler=unfuzzed)[1] for _ in range(40)}
    assert len(dues) == 1


def test_the_week_simulation_makes_no_api_call(monkeypatch):
    """Guard the discipline: scheduling must stay offline. If FSRS ever grew a
    model call, or an import pulled the grader in, this fails."""
    import app.ai.client as ai_client

    def forbidden(**kwargs):
        raise AssertionError("scheduling must not call the API")

    monkeypatch.setattr(ai_client, "parse", forbidden)
    run(["correct", "wrong", "partial", "correct_incomplete", "correct"])
