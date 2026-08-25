"""Persistence. Postgres (Supabase) when DATABASE_URL is set, otherwise an
in-memory store so the Fas 0 loop can be exercised before a database exists.

The in-memory store is not a silent fallback — main.py prints a warning at
startup and /api/health reports which one is live.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.budget import current_month
from app.config import settings
from app.schemas import IMPORTANCE_RANK, DraftConcept, DraftItem


def _now() -> datetime:
    return datetime.now(timezone.utc)


SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "db" / "schema.sql"


class MemoryStore:
    backend = "memory"

    def ensure_schema(self) -> None:
        """Nothing to create — the dicts are the schema."""

    def __init__(self) -> None:
        self.courses: dict[str, dict] = {}
        self.concepts: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.reviews: list[dict] = []
        self.spend: list[dict] = []

    def ensure_course(self, name: str) -> str:
        for cid, c in self.courses.items():
            if c["name"] == name:
                return cid
        cid = str(uuid4())
        self.courses[cid] = {"id": cid, "name": name}
        return cid

    def add_concept(self, course_id: str, c: DraftConcept) -> str:
        cid = str(uuid4())
        self.concepts[cid] = {
            "id": cid,
            "course_id": course_id,
            "name": c.name,
            "importance": c.importance,
            "short_explanation": c.short_explanation,
        }
        return cid

    def add_item(self, concept_id: str, it: DraftItem) -> str:
        iid = str(uuid4())
        self.items[iid] = {
            "id": iid,
            "concept_id": concept_id,
            "type": it.type,
            "prompt": it.prompt,
            "reference_answer": it.reference_answer,
            "rubric": [c.model_dump() for c in it.rubric],
        }
        return iid

    def get_item(self, item_id: str) -> dict | None:
        item = self.items.get(item_id)
        if not item:
            return None
        concept = self.concepts[item["concept_id"]]
        return {**item, "concept_name": concept["name"], "course_id": concept["course_id"]}

    def _latest(self, item_id: str) -> dict | None:
        rows = [r for r in self.reviews if r["item_id"] == item_id]
        return max(rows, key=lambda r: r["reviewed_at"]) if rows else None

    def latest_review(self, item_id: str) -> dict | None:
        return self._latest(item_id)

    def record_review(self, **row: Any) -> None:
        self.reviews.append({"id": str(uuid4()), "reviewed_at": _now(), **row})

    def due_items(self, course_id: str) -> list[dict]:
        now = _now()
        out = []
        for item in self.items.values():
            concept = self.concepts[item["concept_id"]]
            if concept["course_id"] != course_id:
                continue
            last = self._latest(item["id"])
            if last and last["due_at"] > now:
                continue
            out.append(
                {
                    "item_id": item["id"],
                    "concept_id": concept["id"],
                    "concept_name": concept["name"],
                    "type": item["type"],
                    "prompt": item["prompt"],
                    "due_at": last["due_at"] if last else None,
                    "seen_before": last is not None,
                }
            )
        out.sort(key=lambda r: (r["due_at"] is not None, r["due_at"] or now))
        return out

    # --- spend ledger (app/budget.py) --------------------------------------

    def record_spend(self, model: str, cost_usd: float, usage: object) -> None:
        def tok(name: str) -> int:
            return getattr(usage, name, None) or 0

        self.spend.append(
            {
                "id": str(uuid4()),
                "month": current_month(),
                "model": model,
                "cost_usd": cost_usd,
                "input_tokens": tok("input_tokens"),
                "output_tokens": tok("output_tokens"),
                "cache_read_tokens": tok("cache_read_input_tokens"),
                "cache_write_tokens": tok("cache_creation_input_tokens"),
                "spent_at": _now(),
            }
        )

    def month_spend(self, month: str | None = None) -> float:
        month = month or current_month()
        return round(sum(r["cost_usd"] for r in self.spend if r["month"] == month), 6)

    def spend_breakdown(self, month: str | None = None) -> list[dict]:
        month = month or current_month()
        by_model: dict[str, dict] = {}
        for r in self.spend:
            if r["month"] != month:
                continue
            agg = by_model.setdefault(
                r["model"], {"model": r["model"], "calls": 0, "cost_usd": 0.0}
            )
            agg["calls"] += 1
            agg["cost_usd"] += r["cost_usd"]
        for agg in by_model.values():
            agg["cost_usd"] = round(agg["cost_usd"], 6)
        return sorted(by_model.values(), key=lambda r: -r["cost_usd"])

    def progress(self, course_id: str) -> list[dict]:
        rows = []
        for concept in self.concepts.values():
            if concept["course_id"] != course_id:
                continue
            items = [i for i in self.items.values() if i["concept_id"] == concept["id"]]
            latest = [self._latest(i["id"]) for i in items]
            seen = [r for r in latest if r]
            rows.append(
                {
                    "concept_id": concept["id"],
                    "name": concept["name"],
                    "importance": concept["importance"],
                    "items": len(items),
                    "reviewed_items": len(seen),
                    "mastery": sum(r["score"] for r in seen) / len(seen) if seen else None,
                    "mean_confidence_gap": (
                        sum(r["confidence"] - r["score"] for r in seen) / len(seen) if seen else None
                    ),
                }
            )
        rows.sort(key=lambda r: (-IMPORTANCE_RANK[r["importance"]], r["name"]))
        return rows


class PostgresStore:
    backend = "postgres"

    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self._psycopg = psycopg
        self._dsn = dsn
        self._row_factory = dict_row

    def _conn(self):
        return self._psycopg.connect(self._dsn, row_factory=self._row_factory, autocommit=True)

    def ensure_schema(self) -> None:
        """Apply db/schema.sql on every boot.

        Every statement in it is `create ... if not exists`, so this is a no-op
        on an existing database and the whole schema on a fresh one. That means
        a redeploy against a new database just works, and a redeploy against the
        old one changes nothing — which is the property that lets the data
        outlive the container.

        It does NOT migrate. A column whose type changed (importance: int ->
        text) needs an explicit `alter table`; this would silently leave the old
        one in place. See README, "Deploying".
        """
        with self._conn() as conn:
            conn.execute(SCHEMA.read_text(encoding="utf-8"))

    def ensure_course(self, name: str) -> str:
        with self._conn() as conn:
            row = conn.execute("select id from courses where name = %s", (name,)).fetchone()
            if row:
                return str(row["id"])
            cid = str(uuid4())
            conn.execute("insert into courses (id, name) values (%s, %s)", (cid, name))
            return cid

    def add_concept(self, course_id: str, c: DraftConcept) -> str:
        cid = str(uuid4())
        with self._conn() as conn:
            conn.execute(
                "insert into concepts (id, course_id, name, importance, short_explanation)"
                " values (%s, %s, %s, %s, %s)",
                (cid, course_id, c.name, c.importance, c.short_explanation),
            )
        return cid

    def add_item(self, concept_id: str, it: DraftItem) -> str:
        iid = str(uuid4())
        with self._conn() as conn:
            conn.execute(
                "insert into items (id, concept_id, type, prompt, reference_answer, rubric)"
                " values (%s, %s, %s, %s, %s, %s)",
                (
                    iid,
                    concept_id,
                    it.type,
                    it.prompt,
                    it.reference_answer,
                    json.dumps([c.model_dump() for c in it.rubric]),
                ),
            )
        return iid

    def get_item(self, item_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "select i.id, i.concept_id, i.type, i.prompt, i.reference_answer, i.rubric,"
                "       c.name as concept_name, c.course_id"
                "  from items i join concepts c on c.id = i.concept_id"
                " where i.id = %s",
                (item_id,),
            ).fetchone()
        if row:
            row["id"] = str(row["id"])
            row["concept_id"] = str(row["concept_id"])
            row["course_id"] = str(row["course_id"])
        return row

    def latest_review(self, item_id: str) -> dict | None:
        with self._conn() as conn:
            return conn.execute(
                "select score, confidence, fsrs_state, due_at from reviews"
                " where item_id = %s order by reviewed_at desc limit 1",
                (item_id,),
            ).fetchone()

    def record_review(self, **row: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "insert into reviews (id, item_id, answer, score, rubric_hits, verdict,"
                "                     confidence, fsrs_state, due_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid4()),
                    row["item_id"],
                    row["answer"],
                    row["score"],
                    json.dumps(row["rubric_hits"]),
                    row["verdict"],
                    row["confidence"],
                    json.dumps(row["fsrs_state"], default=str),
                    row["due_at"],
                ),
            )

    def due_items(self, course_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "select i.id as item_id, c.id as concept_id, c.name as concept_name,"
                "       i.type, i.prompt, r.due_at, (r.due_at is not null) as seen_before"
                "  from items i"
                "  join concepts c on c.id = i.concept_id"
                "  left join lateral ("
                "       select due_at from reviews where item_id = i.id"
                "        order by reviewed_at desc limit 1"
                "  ) r on true"
                " where c.course_id = %s and (r.due_at is null or r.due_at <= now())"
                " order by r.due_at nulls first",
                (course_id,),
            ).fetchall()
        for row in rows:
            row["item_id"] = str(row["item_id"])
            row["concept_id"] = str(row["concept_id"])
        return rows

    # --- spend ledger (app/budget.py) --------------------------------------

    def record_spend(self, model: str, cost_usd: float, usage: object) -> None:
        def tok(name: str) -> int:
            return getattr(usage, name, None) or 0

        with self._conn() as conn:
            conn.execute(
                "insert into api_spend (id, month, model, cost_usd, input_tokens,"
                "                       output_tokens, cache_read_tokens, cache_write_tokens)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid4()),
                    current_month(),
                    model,
                    cost_usd,
                    tok("input_tokens"),
                    tok("output_tokens"),
                    tok("cache_read_input_tokens"),
                    tok("cache_creation_input_tokens"),
                ),
            )

    def month_spend(self, month: str | None = None) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "select coalesce(sum(cost_usd), 0) as total from api_spend where month = %s",
                (month or current_month(),),
            ).fetchone()
        return round(float(row["total"]), 6)

    def spend_breakdown(self, month: str | None = None) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "select model, count(*) as calls, sum(cost_usd) as cost_usd"
                "  from api_spend where month = %s"
                " group by model order by sum(cost_usd) desc",
                (month or current_month(),),
            ).fetchall()
        for row in rows:
            row["cost_usd"] = round(float(row["cost_usd"]), 6)
            row["calls"] = int(row["calls"])
        return rows

    def progress(self, course_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "select c.id as concept_id, c.name, c.importance,"
                "       count(i.id) as items,"
                "       count(r.score) as reviewed_items,"
                "       avg(r.score) as mastery,"
                "       avg(r.confidence - r.score) as mean_confidence_gap"
                "  from concepts c"
                "  left join items i on i.concept_id = c.id"
                "  left join lateral ("
                "       select score, confidence from reviews where item_id = i.id"
                "        order by reviewed_at desc limit 1"
                "  ) r on true"
                " where c.course_id = %s"
                " group by c.id, c.name, c.importance"
                " order by case c.importance"
                "            when 'core' then 3 when 'supporting' then 2 else 1 end desc,"
                "          c.name",
                (course_id,),
            ).fetchall()
        for row in rows:
            row["concept_id"] = str(row["concept_id"])
        return rows


def build_store():
    if settings.database_url:
        return PostgresStore(settings.database_url)
    return MemoryStore()
