#!/usr/bin/env python
"""Regression suite for the only thing that matters (§9).

Run this on every prompt change and every model change:

    python evals/run_evals.py
    python evals/run_evals.py --model claude-sonnet-5 --jobs 8

Exits non-zero when verdict accuracy falls below the Fas 0 gate (§10: ~85%).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.client import AIError  # noqa: E402
from app.ai.grading import grade  # noqa: E402
from app.schemas import GradingInput, RubricCriterion, RubricHit  # noqa: E402
from app.scoring import score_from_hits, verdict_from  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "grading_cases.jsonl"


def load_cases(path: Path) -> list[dict]:
    """Load and re-derive every expectation. A case whose stored verdict or score
    range does not follow from its own expected_hits is a broken case, and a
    broken case silently corrupts the metric it exists to protect."""
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for c in cases:
        rubric = [RubricCriterion(**r) for r in c["rubric"]]
        hits = [RubricHit(id=r.id, hit=r.id in c["expected_hits"], note="") for r in rubric]
        score = score_from_hits(rubric, hits)
        verdict = verdict_from(rubric, hits, score, c["confidence"])
        lo, hi = c["expected_score_range"]
        if verdict != c["expected_verdict"]:
            raise SystemExit(
                f"case {c['id']}: stored verdict {c['expected_verdict']!r} but its own "
                f"expected_hits imply {verdict!r}"
            )
        if not lo <= score <= hi:
            raise SystemExit(
                f"case {c['id']}: expected_score_range {[lo, hi]} excludes the score "
                f"{score} implied by its own expected_hits"
            )
    return cases


def run_case(case: dict, model: str | None) -> dict:
    inp = GradingInput(
        question=case["question"],
        reference_answer=case["reference_answer"],
        rubric=[RubricCriterion(**r) for r in case["rubric"]],
        student_answer=case["student_answer"],
        confidence=case["confidence"],
    )
    try:
        out = grade(inp, model=model)
    except (AIError, ValueError) as e:
        return {"id": case["id"], "error": str(e)}

    lo, hi = case["expected_score_range"]
    expected_hits = set(case["expected_hits"])
    got_hits = {h.id for h in out.rubric_hits if h.hit}
    return {
        "id": case["id"],
        "error": None,
        "verdict_ok": out.verdict == case["expected_verdict"],
        "score_ok": lo <= out.score <= hi,
        "score_dev": abs(out.score - (lo + hi) / 2),
        "expected_verdict": case["expected_verdict"],
        "got_verdict": out.verdict,
        "got_score": out.score,
        "false_positive": sorted(got_hits - expected_hits),
        "false_negative": sorted(expected_hits - got_hits),
        "n_criteria": len(case["rubric"]),
        "feedback": out.feedback,
        "note": case.get("note", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="override KT_GRADING_MODEL for this run")
    ap.add_argument("--jobs", type=int, default=5, help="parallel grading calls")
    ap.add_argument("--gate", type=float, default=0.85, help="minimum verdict accuracy")
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    ap.add_argument("--verbose", action="store_true", help="print feedback for every case")
    args = ap.parse_args()

    cases = load_cases(args.cases)
    print(f"{len(cases)} cases · model={args.model or 'KT_GRADING_MODEL default'}\n")

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda c: run_case(c, args.model), cases))

    errors = [r for r in results if r["error"]]
    graded = [r for r in results if not r["error"]]

    for r in results:
        if r["error"]:
            print(f"  ERROR  {r['id']:<28} {r['error']}")
            continue
        mark = "ok  " if r["verdict_ok"] and r["score_ok"] else "FAIL"
        print(f"  {mark}   {r['id']:<28} {r['expected_verdict']:<18} -> {r['got_verdict']:<18} score={r['got_score']:.2f}")
        if not r["verdict_ok"] or not r["score_ok"]:
            if r["false_positive"]:
                print(f"           credited but should not be: {', '.join(r['false_positive'])}")
            if r["false_negative"]:
                print(f"           missed but should be credited: {', '.join(r['false_negative'])}")
            print(f"           case note: {r['note']}")
            print(f"           feedback:  {r['feedback']}")
        elif args.verbose:
            print(f"           feedback:  {r['feedback']}")

    if not graded:
        print("\nNo case completed — the grader could not be reached.")
        return 1

    verdict_acc = sum(r["verdict_ok"] for r in graded) / len(graded)
    score_acc = sum(r["score_ok"] for r in graded) / len(graded)
    mean_dev = sum(r["score_dev"] for r in graded) / len(graded)
    total_criteria = sum(r["n_criteria"] for r in graded)
    wrong_criteria = sum(len(r["false_positive"]) + len(r["false_negative"]) for r in graded)

    print(f"\n  verdict accuracy      {verdict_acc:.0%}  ({sum(r['verdict_ok'] for r in graded)}/{len(graded)})")
    print(f"  score within range    {score_acc:.0%}")
    print(f"  mean score deviation  {mean_dev:.3f}")
    print(f"  rubric-hit accuracy   {(total_criteria - wrong_criteria) / total_criteria:.0%}"
          f"  ({wrong_criteria} wrong of {total_criteria} criteria)")
    if errors:
        print(f"  errors                {len(errors)}")

    confusion = Counter((r["expected_verdict"], r["got_verdict"]) for r in graded if not r["verdict_ok"])
    if confusion:
        print("\n  confusions:")
        for (exp, got), n in confusion.most_common():
            print(f"    {exp} -> {got}  x{n}")

    if verdict_acc < args.gate:
        print(f"\nBELOW GATE: verdict accuracy {verdict_acc:.0%} < {args.gate:.0%}")
        return 1
    print(f"\nPASS: verdict accuracy {verdict_acc:.0%} >= {args.gate:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
