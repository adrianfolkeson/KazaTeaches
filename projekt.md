# Studiesystem — projekt.md

> Ett inlärningssystem som optimerar för faktisk kunskap, inte för hur mycket
> AI-genererat material du konsumerar. Byggt först för mig och min
> Systemarkitekturutbildning. Om det bevisligen gör *mig* bättre → utveckla till SaaS.

---

## 0. Norrstjärna & filter

**Norrstjärna:** optimera för *retrieval och produktion*, aldrig för *konsumtion*.

**Filtret** — varje feature måste passera testet:
> Tvingar det mig att plocka fram svaret själv, eller låter det mig passivt läsa AI-text?

Om det andra: bygg det inte, eller gate:a det bakom retrieval.

**Vetenskaplig prioritetsordning** (allt byggs i denna ordning, feature creep filtreras mot den):

1. **Active recall** — testa dig själv, skriv svaret själv
2. **Spaced repetition** — repetera vid rätt tidpunkt (FSRS)
3. **Self-explanation** — förklara högt / "teach me"
4. **Interleaving** — blanda ämnen, blocka inte
5. **Desirable difficulties** — Socratic, exam mode, ingen autocomplete

Om en feature inte tjänar någon av dessa → den väntar eller stryks.

---

## 1. Den enda svåra delen: rättningen

Allt annat i appen är CRUD + schemaläggning. Det enda som är tekniskt svårt
och som avgör om appen är värd något är **hur bra AI:n bedömer fritextsvar.**
Är rättningen dålig är appen värdelös hur snygg resten än är.

→ **80 % av den tekniska mödan läggs här.**

### Tre principer som gör rättningen optimal

**1. Rubric genereras vid frågeskapandet, inte vid rättningen.**
När ett item skapas lagrar du referenssvar + rubric (jsonb: delpoäng som krävs).
Rättning blir då billig, konsistent och cachebar — du matchar mot en fast rubric
istället för att be modellen tycka till från scratch varje gång.

**2. Rättningen är en strukturerad funktion, inte en känsla.**
Fast input → fast JSON-output. Inget svävande. (Kontrakt i §8.)

**3. Confidence före facit.**
Fråga "hur säker är du?" *innan* svaret visas. Gapet mellan självskattad säkerhet
och faktisk korrekthet är den enskilt bästa signalen för "find my gaps" — bättre
än bara felaktiga svar.

### Det som gör att du kan iterera säkert: eval-set

Bygg 15–20 handrättade svar på olika kvalitetsnivåer:
- rätt och komplett
- rätt men ofullständigt
- självsäkert fel
- delvis rätt / missförstånd

Kör din grader mot eval-settet **varje gång** du ändrar prompt eller modell.
Utan detta flyger du blint på det enda som spelar roll. (Struktur i §9.)

---

## 2. Byggordning — varje fas bevisar en sak innan nästa

Bygg inte allt. Varje fas har en gate: den bevisar en hypotes, annars går du
inte vidare.

### Fas 0 — Loopen, tunnast möjligt
En kurs hårdkodad, ingen auth, ingen PDF. Klistra in text →
concepts + frågor → jag svarar fritext → rättning → mastery uppdateras.

- **Bevisar:** Kan AI:n rätta fritext meningsfullt?
- **Gate:** Om nej spelar inget annat roll. Bör märkas på en helg.

### Fas 1 — Schemaläggning + daglig session
FSRS på item-nivå. Interleaved kö (blanda due items över topics).
PDF-import. Enkel progress-sida.

- **Bevisar:** Håller loopen mig återkommande? Lär jag mig mätbart?
- **Gate:** Använder jag den frivilligt en vecka i sträck?

### Fas 2 — Djup för CS-utbildningen
Programming mode (kör kod + understanding-check). Exam mode (AI får inte hjälpa).
Explain-aloud / self-explanation.

- **Bevisar:** Klarar jag tentan bättre?

### Fas 3 — Struktur & insikt
Knowledge map. "Find my gaps" driven av confidence-vs-correctness. Analytics.

- **Bevisar:** Ser jag vad jag faktiskt inte kan?

### Fas 4 — SaaS-härdning
Auth, RLS, multi-tenant, billing, onboarding, privacy-härdning.

- **Bevisar:** Kan andra ha nytta av det jag redan vet fungerar för mig.
- **Gate:** Först efter månader av bevisad nytta för mig själv.

---

## 3. Datamodell v1 (minimal — inte 20 tabeller)

```sql
courses      (id, name)
concepts     (id, course_id, name, importance, short_explanation)
items        (id, concept_id, type, prompt, reference_answer, rubric jsonb)
reviews      (id, item_id, answer, score, rubric_hits jsonb,
              confidence, fsrs_state jsonb, due_at, reviewed_at)
```

**Nyckelbeslut:** *item:et (frågan), inte konceptet, är schemaläggningens enhet.*
FSRS schemalägger items. Mastery per koncept är **härlett** från dess items reviews,
inte lagrat som primärkälla. Fel på detta nu = smärtsam refaktor senare.

**Läggs till senare:**
- Fas 1: `materials`, `material_chunks` (PDF-import + embeddings)
- Fas 3: `concept_relationships` (graf), `study_sessions`, `study_plans`
- Fas 4: `users`, RLS-policies, `subscriptions`

---

## 4. Stack

| Lager | Val | Motiv |
|---|---|---|
| Klient | **Webb-PWA** | Funkar på mobil via browser, "add to home screen" känns som app, matchar SaaS utan omskrivning |
| Backend | **Python (FastAPI)** | AI-pipeline, rättning, eval, scheduling — där svårigheten sitter och där jag är snabbast |
| DB | **Postgres via Supabase** | Auth, Storage, RLS gratis när SaaS kommer |
| Schemaläggning | **py-fsrs** | Modern, evidensbaserad. Uppfinn inte hjulet |
| AI | **Anthropic API** | Rättning, generering, Socratic |
| Frontend | Tunn (server-renderat eller enkel React) | Inte den delen tid läggs på |

**Byt bort SwiftUI native.** Native iOS nu är en omväg: inte min styrka, låser till
en plattform innan loopen bevisats, saktar iterationen. Native app senare, bara om
efterfrågan dyker upp.

**Tradeoff medvetet vald:** Next.js/TS ger ett språk hela vägen men är inte min
styrka. Python-split ger fart i det som är svårt. Under dogfooding vinner fart.

---

## 5. Kostnadsarkitektur (princip, inte eftertanke)

- Rubric genereras **en gång** vid item-skapande → rättning blir billig
- **Prompt caching** på kursmaterial/kontext
- **Batcha** all generering vid import, aldrig on-demand mitt i en session

| Billig modell | Dyr modell |
|---|---|
| concept-extraktion (draft) | fritext-rättning |
| klassificering | kodanalys |
| embeddings | Socratic tutoring |
| MC / true-false-rättning | examensanalys |
| metadata | komplex feedback |

---

## 6. Frågetyper (genereras per koncept)

Definition · Förklaring (varför) · Jämförelse · Scenario · Multiple choice ·
True/false · Kod (vad skriver detta ut?) · Debugging · Design · "Teach me"

Prioritera **fritext** (definition, förklaring, teach-me) i Fas 0 — det är där
active recall och rättningen bevisas. MC/true-false är billigt men svagt; sekundärt.

---

## 7. Bygg INTE ännu (aktivt bortprioriterat)

- **Fri chatbot-tutor** — exakt "konsumera AI-content"-fällan. Gate bakom retrieval.
  Socratic mode ok i Fas 2, men styrt.
- **Voice** — kul, Fas 3+
- **Streaks / gamification** — belöna inlärning, inte app-öppning. Sekundärt.
- **Full Course→Module→Topic-hierarki** — börja platt (course → concept), lägg
  lager när smärtan känns
- **Flashcards** — active recall > flashcards. AI kan autogenerera senare, sekundärt
- **Native app, betalning, "importera hela kursen automatiskt"** — långt senare

---

## 8. Grading-kontrakt (Fas 0, det viktigaste)

Rättningen är en ren funktion med fast input och fast JSON-output.

### Input
```json
{
  "question": "Vad är en transaction i en databas?",
  "reference_answer": "En sekvens av operationer som körs som en atomär enhet ...",
  "rubric": [
    { "id": "atomicity",   "required": true,  "desc": "Nämner allt-eller-inget / atomär enhet" },
    { "id": "acid",        "required": false, "desc": "Kopplar till ACID" },
    { "id": "commit_rollback", "required": true, "desc": "Nämner commit/rollback" }
  ],
  "student_answer": "En grupp queries som körs ihop och antingen alla lyckas eller ingen.",
  "confidence": 0.8
}
```

### Output
```json
{
  "score": 0.67,
  "rubric_hits": [
    { "id": "atomicity",   "hit": true,  "note": "Uttryckt som alla-eller-ingen" },
    { "id": "acid",        "hit": false, "note": "Inte nämnt" },
    { "id": "commit_rollback", "hit": false, "note": "Saknas helt" }
  ],
  "verdict": "correct_incomplete",
  "feedback": "Bra på atomicitet, men du nämner inte commit/rollback.",
  "followup_question": "Vad händer om något går fel mitt i transaktionen?",
  "confidence_gap": 0.13
}
```

- `verdict` ∈ `correct` | `correct_incomplete` | `partial` | `confidently_wrong` | `wrong`
- `confidence_gap` = self-confidence − faktisk score. Positivt & stort = varningsflagga → matas till "find my gaps".
- `score` driver FSRS-uppdateringen (mappa till FSRS-rating).

---

## 9. Eval-set (bygg detta parallellt med gradern)

Fil `evals/grading_cases.jsonl`, en rad per case:

```json
{"question": "...", "reference_answer": "...", "rubric": [...],
 "student_answer": "...", "expected_verdict": "correct_incomplete",
 "expected_score_range": [0.5, 0.75]}
```

- Minst 15–20 cases, spridda över alla `verdict`-nivåer
- Testrunner: kör gradern mot varje case, jämför `verdict` och att `score` faller inom range
- Rapportera: % korrekt verdict, medelavvikelse i score
- **Kör vid varje prompt- eller modelländring.** Detta är din regressionssvit för
  det enda som spelar roll.

---

## 10. Success-mått (per fas, inte vanity metrics)

- **Fas 0:** eval-set verdict-accuracy > ~85 %, och jag litar på rättningen på mina egna svar
- **Fas 1:** frivillig användning ≥ 5 dagar/vecka; mätbar mastery-ökning på repeterade concepts
- **Fas 2:** subjektivt + objektivt bättre tentaresultat
- **Fas 3:** "find my gaps" pekar på luckor jag känner igen som verkliga
- **Fas 4:** minst en annan användare får samma nytta

---

## 11. Sekvens att börja med imorgon

1. FastAPI-skelett + Supabase-projekt (tomma tabeller enligt §3)
2. Grading-endpoint enligt §8 — hårdkoda ett item, testa mot mina egna svar
3. `evals/grading_cases.jsonl` med 15 handrättade cases + testrunner
4. Concept/item-generering från inklistrad text
5. Minimal frontend: klistra in text → svara på fråga → se rättning
6. FSRS på reviews → daglig due-kö

Först när loopen känns som *"fan, jag lär mig faktiskt bättre med det här"* —
bygg nästa lager.
