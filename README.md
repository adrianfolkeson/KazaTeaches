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

38 hand-graded cases in `evals/grading_cases.jsonl`, spread across all five
verdicts, including the failure modes that matter most: *confidently wrong*,
*restates the question without retrieving anything*, and *vague gesture in the
right direction*. It reports verdict accuracy, score-within-range, mean score
deviation and — the deepest signal — per-criterion status accuracy, split into
*too generous* and *too harsh* so a calibration drift is visible as a direction
rather than just a number. Exits non-zero below the §10 Fas 0 gate of 85%
verdict accuracy.

Each case stores an `expected` map of `criterion id -> hit | partial | miss`,
and the runner re-derives `expected_verdict` and `expected_score_range` from it
at load time. A case that disagrees with itself aborts the run instead of
quietly corrupting the metric.

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
for one thing per rubric criterion — to what degree did the answer express this:
`hit`, `partial` or `miss` — plus feedback and a follow-up question. `score`,
`verdict` and `confidence_gap` are then derived in `app/scoring.py`. A
model-authored score is the least reproducible number in the system, and it is
precisely the number the eval-set has to hold stable across prompt edits. The §8
output contract is unchanged; only who computes each field is.

`partial` exists for one specific answer: right direction, no content — *"man
delar upp tabeller så man inte upprepar data, undviker redundans typ"*. Scoring
that as a miss makes it indistinguishable from never approaching the idea;
scoring it as a hit rewards fluency over recall. It is worth half its criterion
(`CREDIT` in `app/scoring.py`). A confident statement that is simply wrong is
never a partial — the prompt is explicit about that, because "sure and wrong" is
the case the whole app exists to surface.

Score is a weighted credit ratio: required criteria count double
(`REQUIRED_WEIGHT`), a partial earns half of whatever its criterion is worth.
Verdicts follow from the statuses: every required criterion hit and
score ≥ `CORRECT_SCORE` → `correct`; every required criterion hit, or all of them
at least partially covered with score ≥ `COVERED_SCORE` → `correct_incomplete`; a
low score held with high confidence → `confidently_wrong`; some credit →
`partial`; none → `wrong`.

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

## Scheduling (§3)

**The item is the scheduling unit.** FSRS schedules items; concept mastery is
derived from its items' reviews at read time and never stored as a source of
truth. Nothing in `app/scheduling.py` knows what a concept is except as
something to interleave across.

### Verdict to rating

The FSRS rating comes from the verdict, not the score:

| Verdict | Rating |
|---|---|
| `wrong`, `confidently_wrong` | `Again` |
| `partial` | `Hard` |
| `correct_incomplete` | `Good` |
| `correct` | `Easy` |

The verdict already folds in the two things scheduling cares about — whether
every required criterion landed, and whether the student was sure while missing
them. A score threshold would re-derive that worse, and would drift every time
the scoring weights are tuned. `rating_for` used to live in `app/scoring.py`;
it moved here, because it is a scheduling policy that happens to read a grading
result, and keeping it there made `scoring.py` the file where two unrelated
policies were tuned.

Intervals come from py-fsrs unchanged. Two scheduler settings differ from its
defaults, and neither invents an interval:

- **`maximum_interval` is 365 days, not 36500.** On defaults, four correct
  answers push an item 5.5 years out — for one course in one semester that is
  indistinguishable from deleting it.
- **Fuzzing stays on.** It jitters each interval a few percent so a batch
  imported on one day does not come due on one day forever. It is also the only
  thing that makes scheduling unrepeatable, so `build_scheduler(fuzz=False)`
  exists and every test uses it. The spec for this work said "FSRS is
  deterministic"; with fuzzing on, it is not, and asserting loose bounds around
  a random jitter would have been the wrong fix.

### The daily queue

`GET /api/session` returns the sitting; `GET /api/next` returns its head.

Items are interleaved across concepts — greedy, taking the concept with the most
waiting except the one just asked, so adjacent repeats happen exactly when one
concept holds more than half of what is due (§0.4). Within a concept the most
overdue goes first, and an item never reviewed is the most overdue thing there
is, which is what puts a freshly generated item into today's queue with no
special case anywhere.

Three caps bound a sitting, and all three were found by simulating a week rather
than reasoned about in advance:

| Cap | Default | Why |
|---|---|---|
| `KT_SESSION_MAX_ITEMS` | 20 | A queue longer than a sitting becomes a backlog you stop opening. |
| `KT_SESSION_MAX_MINUTES` | 25 | Whichever binds first wins. |
| `KT_SESSION_MAX_PER_ITEM` | 3 | See below. |

The simulation is in `tests/test_scheduling.py` and it found two real bugs:

**One item starved the rest.** FSRS puts a failed item back in about a minute and
a half-known one in six, so an item the student never masters is *always* due.
Ordering purely by due date gave it 322 of 336 reviews in a simulated week while
another item was never asked at all. `session_queue` now orders items not yet
asked this sitting ahead of ones already asked — relearning inside a sitting is
worth keeping, but only once everything else has had its turn.

**The caps did nothing.** `/api/next` recomputed the queue on every call and
returned its head, so `session_max_items` truncated a list nobody read past index
0. The sitting is now real state — a review count, a set of items asked, and
per-item attempts — reset at the UTC day boundary.

**And one item could still eat the day.** With the sitting capped at 20, the
unlearnable item took 123 of 140 weekly reviews, because once everything else was
scheduled forward it was the only thing due. FSRS learning steps assume a
flashcard you re-see in seconds; here a review is a written answer that costs a
grading call. An item still unclear on the third attempt today is not going to
clear on the eighteenth, so `KT_SESSION_MAX_PER_ITEM` retires it until tomorrow.

The same simulated week, after all three:

```
item      reviews   final interval
tx1             1              8 d     answered correctly first time
tx2             3             36 d
acid1           6             21 d     wrong, then mastered
acid2           1              8 d
iso1           21          6 min     never mastered — 3 attempts a day, no more
iso2            6             21 d
total 38 reviews  ~$0.42/week
```

Effort followed difficulty per item, which is the whole point of §3.

## Cost architecture (§5)

| Lane | Model (env var) | Used for |
|---|---|---|
| Expensive | `KT_GRADING_MODEL` = `claude-opus-5` | free-text grading |
| Expensive | `KT_GENERATION_MODEL` = `claude-opus-5` | items + rubrics — rubric quality caps grading quality |
| Expensive | `KT_CONCEPT_MODEL` = `claude-opus-5` | concept extraction — see below |

Rubrics are generated once per item. Course material sits in a cached system
block that is byte-identical across every concept in an import, so it is paid for
once per import rather than once per concept. All generation is batched at import
time; nothing is generated mid-session.

### Why concept extraction is not on the cheap lane

§5 puts concept extraction in the cheap lane, on the reasoning that a draft
concept list is classification rather than judgment. Measured, that is wrong in
both directions — it costs more and it is worse.

The concept list decides how many items get generated, and generation is
`$0.067` per concept. Extraction is a few cents at most on any model. So a model
that splits one idea into four does not merely produce four weaker concepts; it
triples the import bill and hands the student three times the questions for the
same understanding. The cheap lane's saving is a rounding error against what its
mistakes cost downstream.

The same measurement showed the first fix was not the model at all. On 1.5K
characters of Swedish material, the original prompt produced 15 concepts on
Haiku, **17 on Sonnet**, and 13 on Opus — a more expensive model made it worse,
because the prompt's own budget ("5 to 25 concepts, fewer if the material is
thin") never said what thin meant. Rewriting `CONCEPT_SYSTEM` around what an
examiner would put on a paper, with explicit merge rules, took every model to
4-5 concepts. Only then was the model choice worth making: Opus was the one that
found *"avvägningen mellan isolering och samtidighet"*, the most exam-worthy idea
in the passage, which the others dropped.

| Model | Concepts | Extraction | Projected import |
|---|---|---|---|
| Haiku 4.5 | 4 | $0.003 | $0.27 |
| Sonnet 5 | 4 | $0.016 | $0.28 |
| **Opus 5** | **5** | $0.024 | **$0.36** |

Against the original prompt on Haiku, that import was $1.01. Nine cents buys the
better concept list.

### One criterion, one thing

`ITEM_SYSTEM` used to say only that criteria must be "independently checkable and
must not overlap". A real import produced this anyway:

```
atomicity_consistency_forklaras   req  Förklarar Atomicity som allt-eller-inget
                                       och Consistency som övergång mellan
                                       giltiga tillstånd.
```

Two ideas, one status. A student who has Atomicity and not Consistency forces the
grader to pick one verdict for both, and `partial` stops meaning "vague" and
starts also meaning "one of the two" — the exact ambiguity the three-state scale
exists to remove. The prompt now forbids joining criteria with "and", and says
what to do with a set too large for the rubric: ask for the set in one criterion,
spend the rest on what the question is actually about. Regenerating the same
concept gives five atomic criteria instead of three compound ones.

The same import produced `rollback_atersaller` — "återställer" transliterated
with a letter dropped. Criterion ids are permanent and every future grading
carries them, so the prompt now specifies the transliteration (å/ä -> a, ö -> o)
instead of leaving it to chance.

## The generator

`POST /api/generate` takes pasted material and returns concepts, items and
rubrics. It stores nothing. `POST /api/ingest` takes the returned `draft_id`
plus whatever you struck out and saves the rest.

The two steps are the point. A bad item is not a bad answer: once stored it is
scheduled, resurfaced and graded against for weeks, and by then it is
indistinguishable from a bad memory. The web UI shows every generated question
with its reference answer and full rubric, with a strike-out button per item and
per concept, and a save button that names the count.

**The rubric the generator emits is the class the grader eats** — `list[RubricCriterion]`,
`{id, required, desc}`, not a copy of the shape. `tests/test_generation.py`
asserts that identity directly, because if the two ever diverge the halves stop
composing and nothing else would notice.

`importance` is `core | supporting | nice_to_know` rather than 1-5. The number
had no agreed meaning in its middle, and the only thing it is ever used for is
how many items to write (4 / 3 / 2) and what to study first.

### The self-check

`evals/gen_selfcheck.py` is the test the generator cannot argue with: it grades
each item's own reference answer against that item's own rubric, using the
grader unmodified.

```bash
.venv/bin/python evals/gen_selfcheck.py
.venv/bin/python evals/gen_selfcheck.py --item-model claude-opus-5 --material notes.md
```

A reference answer is by definition a full-credit answer, so anything below 0.9
— or any required criterion not hit — means the rubric asks for something its own
reference does not say. That is a generator bug, not a grading bug, and every
student answer for that item would be graded against the same break.

Current run: **15/15 items, mean reference score 1.000**, $0.46 (generation
$0.26, grading $0.20).

What it does *not* tell you: the measure saturates. A coherent-but-unambitious
rubric scores 1.00 exactly like a good one, so this cannot rank two models
against each other — both will pass. It catches incoherence, which is the failure
mode that silently corrupts grading; judging ambition still needs eyes.

### Not yet covered by an eval

`evals/grading_cases.jsonl` protects grading; `evals/gen_selfcheck.py` now
protects rubric/reference coherence, and `tests/test_generation.py` covers the
structural rejects (no required criterion, duplicate ids, non-ASCII ids).

What still has no automated check: whether a rubric is *ambitious* — whether it
asks for the thing worth knowing rather than a coherent triviality — and whether
concept counts stay proportional to material length. Both currently rest on
reading a draft before saving it, which is what the review gate is for.

### What it actually costs

Measured August 2026 against the prompts in `app/ai/prompts.py`. Re-measure after
any prompt or model change — `app/budget.py` carries these as constants and uses
them to pre-flight an import.

| Operation | Model | Cost |
|---|---|---|
| One review (grading) | `claude-opus-5` | **$0.011** (0.008 - 0.017) |
| Concept extraction, per import | `claude-haiku-4-5` | $0.004 |
| Item generation, per concept | `claude-opus-5` | **$0.067** |

The grader's system prompt is ~1.4K tokens, above the ~1024-token cache minimum,
so every review after the first in a session reads it from cache at a tenth of
the input rate. Item generation writes a larger cache entry (the course material)
that is reused across the concepts in one import.

### The spend cap

Two numbers, and they behave differently:

| Setting | Default | Behaviour |
|---|---|---|
| `KT_MONTHLY_BUDGET_USD` | `20` | **Hard.** At this, every paid call is refused with HTTP 402 for the rest of the calendar month. |
| `KT_MONTHLY_TARGET_USD` | `10` | Advisory. Crossing it only changes what `/api/budget` reports and turns the budget bar amber. |

What the defaults buy, at the measured rates. An import is priced per concept,
and the rewritten `CONCEPT_SYSTEM` keeps concept counts proportional to the
material — 1.5K characters of dense prose came out at 5 concepts and $0.26, where
the original prompt produced 15 and $1.01:

- **$10 (target):** a few course imports plus ~28 reviews a day for a month. That
  is normal daily use.
- **$20 (cap):** a semester's worth of material plus ~58 reviews a day for a
  month. That is heavy use every day, and it lands just under the ceiling.

Reviews dominate. Imports are a rounding error unless you paste a textbook.

The cap is enforced in `app/ai/client.py::parse()` — the single function every
Anthropic call passes through — so no route can spend around it. Two mechanics
are worth knowing:

- **Metered after the call, checked before it.** Tokens are billed whether or not
  the response parses, so a refusal or a malformed output still counts. Two
  concurrent requests can both pass the check at the boundary and overshoot by
  one call; for a single-user app that is a couple of cents, and a lock around
  every API call would cost more than it saves.
- **An import is pre-flighted.** Item generation is one call per concept, each
  checked separately, so a large import could otherwise run out of budget halfway
  and leave a course holding concepts with no questions. `/api/ingest` estimates
  the whole import first and refuses before writing anything.

`GET /api/budget` reports spend, remaining, and a per-model breakdown; the web UI
shows it as a bar under the nav. On the in-memory store the ledger dies with the
process — `/api/health` reports `budget_persistent: false` rather than implying a
guarantee it cannot make. Set `DATABASE_URL` for a cap that survives a restart.

## Deploying

One user, one always-on container, free tier. `render.yaml` is a blueprint:
Render reads it from the repo, so there is no CLI and no Dockerfile.

**Not serverless, on purpose.** `app/main.py` keeps the day's sitting
(`_sitting`) and unconfirmed generation drafts (`_drafts`) in process memory.
Across several instances the per-day review cap would be per-instance, and a
draft generated on one would not exist on the one that gets the save.

### First deploy

1. **Postgres first** (§25). Supabase → new project → Settings → Database →
   Connection string → **Session pooler**. Without `DATABASE_URL` the app runs
   on the in-memory store and every review, every concept and the spend ledger
   die with the container — on every redeploy and on every free-tier spin-down.
   The app says so at startup and `/api/health` reports `persistent: false`.
2. **Render** → New → Blueprint → pick this repo. It reads `render.yaml`.
3. Set the two secrets it prompts for: `ANTHROPIC_API_KEY` and `DATABASE_URL`.
   `KT_ACCESS_KEY` is generated by Render — read it from
   Environment once.
4. Open `https://<service>.onrender.com/?k=<KT_ACCESS_KEY>` on the phone once.
   The key moves into an HttpOnly cookie and drops out of the URL. Then Share →
   Add to Home Screen.

`db/schema.sql` is applied on every boot. Every statement is guarded, so it is
the whole schema on a fresh database and a no-op on an existing one — which is
what lets the data outlive the container. It does **not** migrate: a column
whose type changed (`importance`, int → text) needs an explicit `alter table`.

### Access

Every route spends money through the Anthropic key, so a public URL without a
gate is a bill anyone can run up. `KT_ACCESS_KEY` gates everything except
`/api/health`, which the platform probes and which exposes nothing.

Leaving `KT_ACCESS_KEY` unset disables the gate. That is right locally and wrong
everywhere else, so startup says so in the log.

### Cost, in three layers

| Layer | Where | What it does |
|---|---|---|
| Anthropic Console → Limits | console.anthropic.com | Hard stop at the account. Set it — nothing in this repo can override it. |
| `KT_MONTHLY_BUDGET_USD` | `app/budget.py` | HTTP 402 at $20 for the rest of the month. Ledger is in Postgres, so it survives a redeploy. |
| `KT_SESSION_MAX_*` | `app/scheduling.py` | Bounds one day: 20 reviews, 3 per item. |

**The session caps weaken on the free tier.** Spin-down clears `_sitting`, so
the day's review count restarts at zero after 15 idle minutes. The monthly cap
is unaffected — that one is in the database — so the real guard is the $20, not
the daily 20. Fine for a week of dogfooding; move off free tier before trusting
the daily cap.

### Runbook

| | |
|---|---|
| Deploy a new version | `git push origin main` — `autoDeploy: true` |
| Logs | Render dashboard → the service → Logs (live tail) |
| App spend | `GET /api/budget`, or the bar at the bottom of Idag |
| Real spend | console.anthropic.com → Usage |
| Which store is live | `GET /api/health` → `persistent: true` means Postgres |
| Roll back | Render → Deploys → pick an older one → Redeploy |
| Rotate the access key | Render → Environment → edit `KT_ACCESS_KEY` → reopen with `?k=` |

First request after idle takes ~50s on the free tier; the container is asleep.

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
