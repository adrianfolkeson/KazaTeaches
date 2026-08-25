repo: adrianfolkeson/KazaTeaches
branch: main

## Last sync
date: 2026-08-25
- Läste README (grader-kontrakt, scoring-regler, sessionscaps, budget) via web fetch — GitHub-verktygen är inte anslutna i projektet.
- Byggde KazaTeaches.dc.html: idag-vy, session med confidence-input före facit, bedömningsvy med rubric_hits.

## Screen map
| Skärm | Källa i repot |
| --- | --- |
| Idag | app/scheduling.py (daglig kö, caps 20/25/3), app/budget.py |
| Session | app/main.py (/api/next), §8 grader-input (confidence före facit) |
| Bedömning | app/ai/grading.py + app/scoring.py (score, verdict, rubric_hits, confidence_gap, followup_question) |

## Att verifiera mot koden
Fältnamnen i designen är tagna ur README. app/schemas.py och db/schema.sql är ännu inte lästa
direkt (GitHub-verktygen saknas) — anslut repot om något fält ska stämma exakt.
