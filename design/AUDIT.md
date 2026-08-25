# Design ↔ schema audit

Design fields come from README (see `github.md`); `app/schemas.py` and
`db/schema.sql` are the truth. Every design variable, and what actually backs it.

## Exact matches — no work needed

| Design | Backend |
|---|---|
| `vCorrect` `vIncomplete` `vPartial` `vConfWrong` `vWrong` | `grading.verdict` — all five, exact |
| `hits` / `partials` / `misses` | `grading.rubric_hits[].status` = `hit`/`partial`/`miss` |
| `scoreText` | `grading.score` |
| `gapText` `gapInk` | `grading.confidence_gap` |
| `feedback` | `grading.feedback` |
| `followup` | `grading.followup_question` |
| `facit` | `ReviewResponse.reference_answer` |
| `question` | `/api/next` → `prompt` |
| `concept` | `/api/next` → `concept_name` |
| `dueCount` | `/api/session` → `due_total` |
| `budgetPct` `budgetLine` | `/api/budget` → `fraction_of_cap`, `spent_usd`, `cap_usd` |
| `passLine` `capLine` | `/api/session` → `reviews_done`, `reviews_left` |

The three-state rubric matched by luck of timing: the design read a README that
already described `hit|partial|miss`. Had it read `projekt.md` §8 it would have
had a boolean.

## Mismatches

**1. `{{ r.desc }}` has no source — the biggest one.**
The design puts the criterion's human description as the main line of every
rubric row under "Vad du hade" / "Vad du missade". `RubricHit` is
`{id, status, note}`. `desc` lives on `RubricCriterion`, which is stored on the
item and never sent to the client. `note` is the grader's ≤12-word reason, not
the criterion text — rendering it there would answer "why" where the design asks
"what".
→ Fix: `ReviewResponse` carries the item's `rubric` so the client joins
`rubric_hits[].id` → `rubric[].desc`. Additive; no grading logic touched.

**2. `conceptCount` is ambiguous.**
Design labels it "begrepp" on the Idag screen. `session.concepts_covered` counts
concepts *in today's queue*, not in the course. The course total is
`/api/progress` → `concepts.length`.
→ Using the course total; the queue count is already implied by `dueCount`.

**3. `attemptLine` has no source.**
Per-item attempts live in `_sitting` in `app/main.py`, server-side only.
→ Fix: `DueItem` carries `attempt` and `attempts_allowed`. Additive.

**4. `ticks[]` has no source.**
A 20-bar sparkline on both Idag and Session. No endpoint exposes per-day review
counts, and `reviews` rows are not aggregated anywhere.
→ Driving it from the live sitting instead: one tick per slot in today's cap,
filled for reviews done. Same shape, real data, no new endpoint.

**5. `hasCode` / `code` has no source.**
Items have `prompt`, not a separate code field. Code arrives inside the prompt.
→ Parsing fenced blocks out of `prompt` client-side. Item 5 of the brief (a
monospace treatment) is what renders them.

**6. `retry` ("Svara på den") has no endpoint.**
The follow-up question is generated text, not a stored item; nothing accepts an
answer to it and nothing would schedule it.
→ Dropping the button. Noted rather than faked.

**7. `w.pct` assumes mastery is always a number.**
`ConceptMastery.mastery` is `float | null` — null until a concept has a reviewed
item. The design has no empty state.
→ Rendering nulls as "aldrig testad", excluded from "svagast".

**8. `confOptions` is 5 discrete buttons; the API takes a float.**
Not a conflict. Mapping the five to 0.1 / 0.3 / 0.5 / 0.7 / 0.9.

## Backend changes this needs (all additive, no logic touched)

- `ReviewResponse.rubric: list[RubricCriterion]` — for mismatch 1
- `DueItem.attempt` + `DueItem.attempts_allowed` — for mismatch 3
