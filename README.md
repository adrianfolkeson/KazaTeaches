# Studiesystem — Fas 0

Implementation of `projekt.md` Fas 0 plus the §11 starting sequence: paste text →
concepts + items → answer free text → grading → mastery, with FSRS scheduling and
an interleaved due queue on top.

The north star is retrieval, not consumption, so there is no chat tutor here and
nothing reads AI prose to you. You write the answer, then you see the facit.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # put your ANTHROPIC_API_KEY in it
.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Paste material under **Importera**, then **Plugga**.

`DATABASE_URL` is optional. Without it the app runs on an in-memory store and
says so loudly at startup and on `/api/health` — fine for proving the loop, useless
for proving the habit. With Supabase: create the project, run `db/schema.sql` in
the SQL editor, put the connection string in `.env`.

## Run the evals

The eval-set is the regression suite for the only part that decides whether this
app is worth anything. Run it on **every** prompt change and **every** model change:

```bash
.venv/bin/python evals/run_evals.py
.venv/bin/python evals/run_evals.py --model claude-sonnet-5 --jobs 8
```

20 hand-graded cases in `evals/grading_cases.jsonl`, spread across all five
verdicts, including the two failure modes that matter most: *confidently wrong*
and *restates the question without retrieving anything*. It reports verdict
accuracy, score-within-range, mean score deviation and — the deepest signal —
per-criterion rubric-hit accuracy, then exits non-zero below the §10 Fas 0 gate
of 85% verdict accuracy.

Each case stores `expected_hits`, and the runner re-derives `expected_verdict`
and `expected_score_range` from them at load time. A case that disagrees with
itself aborts the run instead of quietly corrupting the metric.

```bash
.venv/bin/python -m pytest tests -q    # deterministic scoring, scheduling, the loop
```

## How grading works

Three things from §1, unchanged:

1. **The rubric is written once, at item creation**, not at grading time. Grading
   is a match against a fixed rubric — cheap, consistent, cacheable.
2. **Grading is a structured function**, fixed input → fixed JSON output (§8).
3. **Confidence is asked before the facit appears.** The gap between self-rated
   confidence and actual score is the "find my gaps" signal.

Two deliberate design choices on top of §8, both worth arguing about:

**The model judges hits; code computes everything else.** The grader is asked
for one thing per rubric criterion — did the answer actually express this, yes or
no — plus feedback and a follow-up question. `score`, `verdict` and
`confidence_gap` are then derived in `app/scoring.py`. A model-authored score is
the least reproducible number in the system, and it is precisely the number the
eval-set has to hold stable across prompt edits. The §8 output contract is
unchanged; only who computes each field is.

Score is a weighted hit ratio: required criteria count double (`REQUIRED_WEIGHT`
in `app/scoring.py`), so missing a must-have costs more than missing a nuance.
Verdicts follow from the hits: everything hit → `correct`; every *required*
criterion hit → `correct_incomplete`; a low score held with high confidence →
`confidently_wrong`; some credit → `partial`; none → `wrong`.

> Note: §8's worked example is internally inconsistent — it shows `score: 0.67`
> next to `rubric_hits` where one of three criteria is hit. No hit-ratio rule
> produces 0.67 from that. This implementation follows the rubric_hits, so that
> example answer scores 0.4 and, at the stated confidence of 0.8, comes out as
> `confidently_wrong` rather than `correct_incomplete`. Being 80% sure of an
> answer that misses a *required* criterion is exactly the case the app exists to
> surface. If you'd rather that read as `correct_incomplete`, the thresholds are
> two named constants — but regenerate the affected eval cases when you change them.

**The grader never sees the student's confidence.** It is in the §8 input and it
reaches `app/scoring.py`, but it is deliberately kept out of the prompt. A grader
that knows the student felt sure drifts toward that feeling, and then
`confidence_gap` is measuring itself.

## Cost architecture (§5)

| Lane | Model (env var) | Used for |
|---|---|---|
| Expensive | `KT_GRADING_MODEL` = `claude-opus-5` | free-text grading |
| Expensive | `KT_GENERATION_MODEL` = `claude-opus-5` | items + rubrics — rubric quality caps grading quality |
| Cheap | `KT_CHEAP_MODEL` = `claude-haiku-4-5` | draft concept extraction |

Rubrics are generated once per item. Course material sits in a cached system
block that is byte-identical across every concept in an import, so it is paid for
once per import rather than once per concept. All generation is batched at import
time; nothing is generated mid-session. If your cheap model rejects structured
outputs, set `KT_CHEAP_MODEL=claude-sonnet-5`.

## Layout

```
app/scoring.py       deterministic score / verdict / confidence gap / FSRS rating
app/ai/grading.py    the grader — the one hard part
app/ai/prompts.py    all prompt text, so a prompt change is one reviewable diff
app/ai/generation.py text -> concepts -> items + rubrics, batched at import
app/scheduling.py    FSRS on items; interleaved due queue
app/store.py         Postgres (Supabase) or in-memory
app/main.py          the loop over HTTP
db/schema.sql        §3 data model
evals/               the regression suite that guards the grader
web/index.html       thin client, autocomplete off on purpose
```

The item, not the concept, is the scheduling unit. Concept mastery is **derived**
from its items' latest reviews and is never stored — getting this wrong now would
be a painful refactor later (§3).

## What is deliberately not here

Per §7: no free chat tutor, no voice, no streaks, no flashcards, no
course→module→topic hierarchy, no auth, no PDF import, no native app. Fas 1
adds `materials` / `material_chunks`; Fas 3 adds the knowledge map and analytics;
Fas 4 adds auth, RLS and multi-tenancy.

## The gate

Fas 0 is proven when eval verdict accuracy clears ~85% **and** you trust the
grading on your own answers. Until both hold, don't build the next layer.
