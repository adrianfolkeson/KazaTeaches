#!/usr/bin/env python
"""Does the generator produce rubrics the grader can actually grade?

The test is the one the generator cannot argue with: take each item's own
reference answer — the thing the generator itself calls a full-credit answer —
and grade it against that item's own rubric. A reference answer that fails its
own rubric means the two halves were written past each other, and every student
answer for that item will be graded against the same break.

This composes the two halves rather than testing them separately: it is the
grader, unmodified, judging the generator's output.

    python evals/gen_selfcheck.py
    python evals/gen_selfcheck.py --item-model claude-opus-5 --material notes.txt

Exits non-zero when any reference answer scores below the floor or misses a
required criterion.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai import client as ai_client  # noqa: E402
from app.ai.client import AIError  # noqa: E402
from app.ai.generation import generate_draft  # noqa: E402
from app.ai.grading import grade  # noqa: E402
from app.schemas import GradingInput  # noqa: E402
from app.store import MemoryStore  # noqa: E402

DEFAULT_MATERIAL = Path(__file__).resolve().parent / "selfcheck_material.md"

# A reference answer is by definition a full-credit answer. Anything below this
# means the rubric asks for something the reference does not say.
SCORE_FLOOR = 0.9

# Confidence is part of the grading input but irrelevant here — a reference
# answer has no student behind it. Held at 0 so it can never tip a verdict into
# confidently_wrong and confuse the diagnosis.
NO_STUDENT = 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--material", type=Path, default=DEFAULT_MATERIAL)
    ap.add_argument("--concept-model", default=None)
    ap.add_argument("--item-model", default=None, help="override KT_GENERATION_MODEL")
    ap.add_argument("--grading-model", default=None)
    ap.add_argument("--floor", type=float, default=SCORE_FLOOR)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # Meter the run so the cost of a self-check is visible, and so the budget
    # cap applies here exactly as it does in the app.
    ledger = MemoryStore()
    ai_client.set_meter(ledger.month_spend, ledger.record_spend)

    text = args.material.read_text(encoding="utf-8")
    print(f"Generating from {args.material.name} ({len(text)} chars)")
    try:
        draft = generate_draft(
            text,
            course_id="selfcheck",
            course_name="selfcheck",
            concept_model=args.concept_model,
            item_model=args.item_model,
            spend_after=ledger.month_spend,
        )
    except (AIError, ValueError) as e:
        print(f"\nGeneration failed outright: {e}")
        return 1

    gen_cost = ledger.month_spend()
    print(
        f"  {len(draft.concepts)} concepts, {draft.n_items} items, ${gen_cost:.4f}"
        f"  (concepts={args.concept_model or 'default'}, items={args.item_model or 'default'})\n"
    )

    failures: list[str] = []
    checked = 0
    scores: list[float] = []

    for concept in draft.concepts:
        print(f"  {concept.importance:<13} {concept.name}")
        for item in concept.items:
            required = {c.id for c in item.rubric if c.required}
            try:
                out = grade(
                    GradingInput(
                        question=item.prompt,
                        reference_answer=item.reference_answer,
                        rubric=item.rubric,
                        student_answer=item.reference_answer,
                        confidence=NO_STUDENT,
                    ),
                    model=args.grading_model,
                )
            except (AIError, ValueError) as e:
                failures.append(f"{concept.name} / {item.type}: grading failed — {e}")
                print(f"    ERROR  [{item.type}] {item.prompt[:58]}… {e}")
                continue

            checked += 1
            scores.append(out.score)
            missed_required = sorted(
                h.id for h in out.rubric_hits if h.id in required and h.status != "hit"
            )
            not_hit = sorted(h.id for h in out.rubric_hits if h.status != "hit")

            ok = out.score >= args.floor and not missed_required
            print(f"    {'ok  ' if ok else 'FAIL'}   [{item.type:<15}] score={out.score:.2f}"
                  f"  {item.prompt[:52]}…")
            if not ok:
                if missed_required:
                    failures.append(
                        f"{concept.name} / {item.type}: reference answer misses required "
                        f"{missed_required}"
                    )
                    print(f"           required not hit: {', '.join(missed_required)}")
                else:
                    failures.append(
                        f"{concept.name} / {item.type}: reference answer scored "
                        f"{out.score:.2f} < {args.floor}"
                    )
                for h in out.rubric_hits:
                    if h.status != "hit":
                        print(f"             {h.status:<8} {h.id}: {h.note}")
                print(f"           grader feedback: {out.feedback}")
            elif args.verbose and not_hit:
                print(f"           optional not hit: {', '.join(not_hit)}")
        print()

    total = ledger.month_spend()
    print("=" * 68)
    if not checked:
        print("No item was graded — the grader could not be reached.")
        return 1

    print(f"  items checked         {checked}")
    print(f"  passing               {checked - len(failures)}/{checked}"
          f"  ({(checked - len(failures)) / checked:.0%})")
    print(f"  mean reference score  {sum(scores) / len(scores):.3f}")
    print(f"  cost                  ${total:.4f} "
          f"(generation ${gen_cost:.4f}, grading ${total - gen_cost:.4f})")

    if failures:
        print(f"\n  {len(failures)} broken item(s):")
        for f in failures:
            print(f"    - {f}")
        print("\nA reference answer that fails its own rubric is a generator bug, not a"
              "\ngrading bug. Fix ITEM_SYSTEM in app/ai/prompts.py and run this again.")
        return 1

    print("\nPASS: every reference answer satisfies its own rubric.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
