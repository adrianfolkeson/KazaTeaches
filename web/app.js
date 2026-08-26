/* KazaTeaches — thin fetch frontend over the FastAPI backend.
 *
 * No framework, no build step. The three screens are one document; only the
 * grading result is genuinely dynamic, and it replaces a region rather than a
 * page. Field names here are the ones in app/schemas.py — see design/AUDIT.md
 * for where the design's own names differ and why.
 */

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  return body;
}

function fail(e) {
  const el = $("#err");
  el.style.display = "block";
  el.textContent = e.message;
}

// Screens you cannot navigate away from without losing something: an answer in
// progress, a result you have not read, or a generated draft that cost money
// and only exists until it is saved.
const NO_EXIT = new Set(["s-session", "s-graded", "s-draft"]);
const NAV_FOR = { "s-today": "s-today", "s-import": "s-import", "s-draft": "s-import",
                  "s-history": "s-history" };

function show(id) {
  document.querySelectorAll(".kt-screen").forEach((s) => s.toggleAttribute("data-on", s.id === id));
  const nav = $("#nav");
  nav.hidden = NO_EXIT.has(id);
  nav.querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-current", String(b.dataset.go === NAV_FOR[id])));
  window.scrollTo(0, 0);
}

/* Code arrives inside `prompt` — items have no separate code field (audit §5).
   Split fenced blocks out so the monospace treatment can hold them, and escape
   everything: this is model-generated text going into innerHTML. */
function renderProse(text) {
  return esc(text)
    .split(/```(?:[a-z]*\n)?/g)
    .map((part, i) => (i % 2
      ? `<pre class="kt-code"><code>${part.replace(/\n$/, "")}</code></pre>`
      : part.replace(/`([^`\n]+)`/g, "<code>$1</code>")))
    .join("");
}

const CONF = [
  { v: 0.1, n: "10%", l: "Gissar" },
  { v: 0.3, n: "30%", l: "Osäker" },
  { v: 0.5, n: "50%", l: "Kanske" },
  { v: 0.7, n: "70%", l: "Ganska säker" },
  { v: 0.9, n: "90%", l: "Säker" },
];

const VERDICT = {
  correct:            ["Rätt", "Alla obligatoriska kriterier träffade."],
  correct_incomplete: ["Rätt, men ofullständigt", "Kärnan sitter. Kanterna saknas."],
  partial:            ["Delvis", "Rätt riktning, för lite innehåll."],
  confidently_wrong:  ["Säker och fel", "Du kände dig säker på något som inte höll — den dyraste sortens lucka."],
  wrong:              ["Fel", "Inget av kriterierna landade. Inget drama — den kommer tillbaka."],
};

let current = null;      // the DueItem being answered
let confidence = null;   // chosen before the answer is graded, never after
let session = null;      // last /api/session payload

/* ── ticks ─────────────────────────────────────────────────────────────── */
function ticks(el, done, total) {
  const n = Math.max(1, total);
  el.innerHTML = Array.from({ length: n }, (_, i) =>
    `<i${i < done ? " data-done" : ""} style="height:${i < done ? "100%" : "45%"}"></i>`).join("");
}

/* ── Idag ──────────────────────────────────────────────────────────────── */
async function loadToday() {
  try {
    const [s, progress, budget] = await Promise.all([
      api("/api/session"),
      api("/api/progress"),
      api("/api/budget").catch(() => null),
    ]);
    session = s;

    $("#due-count").textContent = s.due_total;
    $("#concept-count").textContent = progress.concepts.length;

    const cap = s.reviews_done + s.reviews_left;
    ticks($("#today-ticks"), s.reviews_done, cap);
    $("#pass-line").textContent = `${s.reviews_done} av ${cap} i dagens pass`;

    $("#today-line").textContent =
      s.due_total === 0 && s.reviews_done === 0
        ? "Inget är förfallet. Importera material för att komma igång."
        : s.reviews_left === 0
          ? "Dagens pass är klart. Det som är kvar leder morgondagens kö."
          : `${s.items.length === 1 ? "1 fråga väntar" : s.items.length + " frågor väntar"}, från ${s.concepts_covered} ${s.concepts_covered === 1 ? "begrepp" : "begrepp"}.`;

    $("#start").disabled = s.items.length === 0;
    $("#start").textContent = s.reviews_done ? "Fortsätt passet" : "Starta passet";
    $("#cap-line").textContent = s.capped
      ? `Passet är kapat — ${s.due_total} förfallna totalt.`
      : "";

    // mastery is float|null: null means never tested, not zero (audit §7).
    const weak = progress.concepts
      .filter((c) => c.mastery !== null)
      .sort((a, b) => a.mastery - b.mastery)
      .slice(0, 3);
    const untested = progress.concepts.filter((c) => c.mastery === null).length;
    $("#weak").innerHTML = weak.length
      ? weak.map((c) => {
          const pct = Math.round(c.mastery * 100);
          const ink = pct < 50 ? "var(--color-accent-2)" : "var(--color-accent)";
          const gap = c.mean_confidence_gap;
          const note = gap !== null && gap >= 0.2
            ? `Du överskattar dig här — gap +${Math.round(gap * 100)}`
            : `${c.reviewed_items} av ${c.items} frågor testade`;
          return `<div style="display:flex; flex-direction:column; gap:var(--space-1);">
            <div style="display:flex; align-items:baseline; gap:var(--space-2);">
              <span style="font-family:var(--font-heading); font-size:19px; line-height:1.15;">${esc(c.name)}</span>
              <span class="kt-num" style="margin-left:auto; font-size:12px;
                    color:color-mix(in srgb, var(--color-text) 55%, transparent);">${pct}%</span>
            </div>
            <div style="height:3px; background:color-mix(in srgb, var(--color-text) 10%, transparent);">
              <div style="height:3px; width:${pct}%; background:${ink};"></div>
            </div>
            <div style="font-size:12px; color:color-mix(in srgb, var(--color-text) 55%, transparent);">${esc(note)}</div>
          </div>`;
        }).join("")
      : `<div style="font-size:15px; font-style:italic; color:color-mix(in srgb, var(--color-text) 55%, transparent);">
           ${untested ? `${untested} begrepp, inget testat än.` : "Inget importerat än."}</div>`;

    if (budget) {
      $("#budget").hidden = false;
      $("#budget-bar").style.width = `${Math.min(100, Math.round(budget.fraction_of_cap * 100))}%`;
      $("#budget-bar").style.background = budget.exhausted
        ? "var(--color-accent-2)" : budget.over_target ? "var(--color-process-yellow)" : "var(--color-accent)";
      $("#budget-line").textContent =
        `$${budget.spent_usd.toFixed(2)} av $${budget.cap_usd.toFixed(2)} i ${budget.month}`;
    }
  } catch (e) { fail(e); }
}

/* ── Session ───────────────────────────────────────────────────────────── */
async function nextQuestion() {
  try {
    current = await api("/api/next");
    if (!current) {
      await loadToday();
materialMeta();
pendingDraft();
      show("s-today");
      return;
    }
    confidence = null;
    $("#q-concept").textContent = current.concept_name;
    $("#q-prompt").innerHTML = renderProse(current.prompt.split("```")[0]);
    // A fenced block in the prompt becomes set matter under the question.
    const fenced = current.prompt.includes("```")
      ? renderProse(current.prompt.slice(current.prompt.indexOf("```")))
      : "";
    $("#q-code").innerHTML = fenced;
    $("#attempt-line").textContent = current.attempt
      ? `Försök ${current.attempt + 1} av ${current.attempts_allowed}`
      : (current.seen_before ? "Repetition" : "Ny fråga");
    $("#answer").value = "";
    $("#word-line").textContent = "";
    renderConf();
    updateSubmit();

    const s = await api("/api/session");
    session = s;
    ticks($("#session-ticks"), s.reviews_done, s.reviews_done + s.reviews_left);

    show("s-session");
    $("#answer").focus();
  } catch (e) { fail(e); }
}

function renderConf() {
  $("#conf").innerHTML = CONF.map((c) =>
    `<button type="button" data-v="${c.v}" aria-pressed="${confidence === c.v}">
       <span class="n">${c.n}</span><span class="l">${c.l}</span></button>`).join("");
  $("#conf").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { confidence = Number(b.dataset.v); renderConf(); updateSubmit(); };
  });
  $("#conf-hint").textContent = confidence === null ? "innan du ser facit" : "";
}

function updateSubmit() {
  const hasAnswer = $("#answer").value.trim().length > 0;
  const ready = hasAnswer && confidence !== null;
  $("#submit").disabled = !ready;
  $("#submit").textContent = !hasAnswer
    ? "Skriv ett svar först"
    : confidence === null ? "Välj hur säker du är" : "Rätta";
}

async function submitAnswer() {
  const answer = $("#answer").value.trim();
  if (!answer || confidence === null) return;
  $("#submit").disabled = true;
  $("#submit").textContent = "Rättar…";
  try {
    const res = await api("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: current.item_id, answer, confidence }),
    });
    renderGrading(res, answer);
    show("s-graded");
  } catch (e) {
    fail(e);
    updateSubmit();
  }
}

/* ── Bedömning ─────────────────────────────────────────────────────────── */
function renderGrading(res, answer) {
  const g = res.grading;
  // rubric_hits carries {id, status, note}; the criterion text lives on the
  // rubric, which ReviewResponse now includes so the join is possible (audit §1).
  const desc = Object.fromEntries((res.rubric || []).map((c) => [c.id, c]));

  const [label, sub] = VERDICT[g.verdict] ?? [g.verdict, ""];
  const v = $("#verdict");
  v.dataset.v = g.verdict;
  v.querySelector(".v").textContent = label;
  v.querySelector(".sub").textContent = sub;

  const rows = (status) => g.rubric_hits.filter((h) => h.status === status).map((h) => {
    const c = desc[h.id];
    const required = c?.required ? "obligatoriskt" : "extra";
    return `<li data-s="${status}"><span class="mark"></span><div>
      <div class="desc">${esc(c?.desc ?? h.id)}</div>
      <div class="note">${esc(h.note || required)}</div></div></li>`;
  });

  const hits = rows("hit"), partials = rows("partial"), misses = rows("miss");
  $("#r-hits").innerHTML = hits.join("");
  $("#no-hits").hidden = hits.length > 0;
  $("#h-partials").hidden = partials.length === 0;
  $("#r-partials").innerHTML = partials.join("");
  $("#h-misses").hidden = misses.length === 0;
  $("#r-misses").innerHTML = misses.join("");

  $("#st-score").textContent = Math.round(g.score * 100) + "%";
  $("#st-conf").textContent = Math.round(confidence * 100) + "%";
  const gap = g.confidence_gap;
  $("#st-gap").textContent = (gap > 0 ? "+" : "") + Math.round(gap * 100);
  $("#st-gap").style.color = gap >= 0.4 ? "var(--color-accent-2)"
    : gap >= 0.2 ? "var(--color-accent-700)" : "inherit";

  $("#g-feedback").innerHTML = renderProse(g.feedback);
  $("#g-followup").innerHTML = renderProse(g.followup_question);
  $("#facit").innerHTML = renderProse(res.reference_answer);
  $("#facit").hidden = true;
  $("#your-answer").textContent = answer;

  const d = res.interval_days;
  $("#sched-line").textContent = d < 1
    ? `Kommer tillbaka om ${Math.max(1, Math.round(d * 24 * 60))} min`
    : `Kommer tillbaka om ${Math.round(d)} dagar`;
}

/* ── Importera ─────────────────────────────────────────────────────────── */
let draft = null;
const cutItems = new Set();

function pendingDraft() {
  // A generated draft lives only in this page and in the server's staging dict.
  // Leaving the review screen must therefore leave a way back, or the only way
  // to recover it is to generate again and pay again.
  const banner = $("#pending-draft");
  if (!draft) { banner.hidden = true; return; }
  const kept = draft.concepts.reduce((n, c) =>
    n + c.items.filter((i) => !cutItems.has(`${c.name}::${i.prompt}`)).length, 0);
  banner.hidden = false;
  banner.querySelector("span").textContent =
    `Ogranskat utkast: ${kept === 1 ? "1 fråga" : kept + " frågor"}, redan betalt.`;
}

function materialMeta() {
  const text = $("#material").value.trim();
  const chars = text.length;
  $("#material-meta").textContent = chars ? `${chars} tecken` : "";
  // The server refuses under 200; say so here rather than after a round trip.
  $("#generate").disabled = chars < 200;
  $("#generate").textContent = chars && chars < 200
    ? `Behöver minst 200 tecken (${chars})` : "Generera frågor";
}

async function generate() {
  const text = $("#material").value.trim();
  $("#generate").disabled = true;
  $("#generate").textContent = "Genererar…";
  $("#generate-msg").textContent =
    "Det här tar en stund. Frågor och rubriker skrivs en gång, här.";
  try {
    draft = await api("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    cutItems.clear();
    renderDraft();
    pendingDraft();
    show("s-draft");
    loadBudget();
  } catch (e) {
    $("#generate-msg").textContent = e.message;
    $("#generate-msg").style.color = "var(--color-accent-2)";
  } finally {
    materialMeta();
  }
}

/* ── Granska utkast ────────────────────────────────────────────────────── */
const IMPORTANCE = { core: "kärna", supporting: "stöd", nice_to_know: "detalj" };

function renderDraft() {
  if (!draft) return;
  const kept = draft.concepts.reduce((n, c) =>
    n + c.items.filter((i) => !cutItems.has(`${c.name}::${i.prompt}`)).length, 0);

  $("#draft-meta").textContent =
    `${draft.concepts.length} begrepp · ${draft.n_items} ${draft.n_items === 1 ? "fråga" : "frågor"} · $${draft.cost_usd.toFixed(3)}`;

  $("#draft-items").innerHTML = draft.concepts.map((c) =>
    c.items.map((item, idx) => {
      const key = `${c.name}::${item.prompt}`;
      const cut = cutItems.has(key);
      return `<div class="kt-draft"${cut ? " data-cut" : ""}>
        ${idx === 0 ? `<div class="kt-eyebrow" style="letter-spacing:.09em;">
            <span style="color:var(--color-accent-700);">${esc(c.name)}</span>
            <span style="margin-left:var(--space-2);">${esc(IMPORTANCE[c.importance] ?? c.importance)}</span>
          </div>` : ""}
        <div class="q">${renderProse(item.prompt)}</div>
        <div class="ref">${renderProse(item.reference_answer)}</div>
        <div>${item.rubric.map((r) =>
          `<div class="crit"${r.required ? " data-req" : ""}><i></i><div>${esc(r.desc)}
             <span style="font-size:10px; letter-spacing:.08em; text-transform:uppercase;
                   color:color-mix(in srgb, var(--color-text) 40%, transparent);">
               ${r.required ? "krävs" : "extra"}</span></div></div>`).join("")}</div>
        <button class="cut-btn" data-key="${esc(key)}">${cut ? "Ångra" : "Stryk"}</button>
      </div>`;
    }).join("")).join("");

  $("#draft-items").querySelectorAll("[data-key]").forEach((b) => {
    b.onclick = () => {
      const k = b.dataset.key;
      cutItems.has(k) ? cutItems.delete(k) : cutItems.add(k);
      renderDraft();
    };
  });

  $("#draft-save").textContent = kept
    ? (kept === 1 ? "Spara 1 fråga" : `Spara ${kept} frågor`)
    : "Inget att spara";
  $("#draft-save").disabled = kept === 0;
  pendingDraft();
}

async function saveDraft() {
  $("#draft-save").disabled = true;
  $("#draft-save").textContent = "Sparar…";
  try {
    const res = await api("/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_id: draft.draft_id, reject_items: [...cutItems] }),
    });
    draft = null;
    cutItems.clear();
    pendingDraft();
    $("#material").value = "";
    materialMeta();
    $("#generate-msg").style.color = "";
    $("#generate-msg").textContent =
      `Sparat: ${res.concepts} begrepp, ${res.items === 1 ? "1 fråga" : res.items + " frågor"}. Due nu.`;
    await loadToday();
    show("s-today");
  } catch (e) {
    fail(e);
    renderDraft();
  }
}

/* ── Historik ──────────────────────────────────────────────────────────── */
const DAYS = ["söndag", "måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag"];

function dayLabel(iso) {
  const d = new Date(iso + "T00:00:00");
  const today = new Date().toISOString().slice(0, 10);
  if (iso === today) return "Idag";
  const y = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  if (iso === y) return "Igår";
  return `${DAYS[d.getDay()]} ${d.getDate()}/${d.getMonth() + 1}`;
}

async function loadHistory() {
  const el = $("#history-days");
  el.innerHTML = "";
  try {
    const h = await api("/api/history");
    const n = (count, one, many) => `${count} ${count === 1 ? one : many}`;
    $("#history-meta").textContent = h.total
      ? `${n(h.total, "rättning", "rättningar")} över ${n(h.days.length, "dag", "dagar")}`
      : "";
    if (!h.total) {
      el.innerHTML = `<div style="font-size:15px; font-style:italic;
        color:color-mix(in srgb, var(--color-text) 55%, transparent);">
        Inget rättat än.</div>`;
      return;
    }
    el.innerHTML = h.days.map((d) => `
      <div class="kt-day">
        <h3>${esc(dayLabel(d.day))}</h3>
        ${d.rows.map((r) => {
          const gap = Math.round(r.confidence_gap * 100);
          const gapInk = r.confidence_gap >= 0.4 ? "var(--color-accent-2)"
            : r.confidence_gap >= 0.2 ? "var(--color-accent-700)" : "inherit";
          return `<div class="kt-hrow" data-v="${esc(r.verdict)}">
            <span class="mark"></span>
            <div>
              <div class="q">${esc(r.prompt)}</div>
              <div class="meta">
                <span class="v">${esc((VERDICT[r.verdict] ?? [r.verdict])[0])}</span>
                <span>${Math.round(r.score * 100)}%</span>
                <span style="color:${gapInk};">gap ${gap > 0 ? "+" : ""}${gap}</span>
                <span class="concept">${esc(r.concept_name)}</span>
              </div>
            </div>
          </div>`;
        }).join("")}
      </div>`).join("");
  } catch (e) { fail(e); }
}

/* ── wiring ────────────────────────────────────────────────────────────── */
$("#nav").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    const id = b.dataset.go;
    if (id === "s-today") loadToday();
    if (id === "s-history") loadHistory();
    show(id);
  };
});
$("#material").oninput = materialMeta;
$("#generate").onclick = generate;
$("#draft-save").onclick = saveDraft;
$("#draft-back").onclick = () => show("s-import");
$("#resume-draft").onclick = () => { renderDraft(); show("s-draft"); };
$("#start").onclick = nextQuestion;
$("#next").onclick = nextQuestion;
$("#pause").onclick = async () => { await loadToday(); show("s-today"); };
$("#to-today").onclick = async () => { await loadToday(); show("s-today"); };
$("#show-facit").onclick = () => { $("#facit").hidden = !$("#facit").hidden; };
$("#submit").onclick = submitAnswer;
$("#answer").oninput = (e) => {
  const words = e.target.value.trim().split(/\s+/).filter(Boolean).length;
  $("#word-line").textContent = words ? `${words} ord` : "";
  updateSubmit();
};

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

loadToday();
materialMeta();
pendingDraft();
