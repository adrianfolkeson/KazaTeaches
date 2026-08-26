"""The access gate. Not a security system — a single shared secret for a single
user — but it is the only thing between a public URL and a spendable API key,
so its failure modes are worth pinning down."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth import COOKIE
from app.store import MemoryStore

KEY = "s3cret-access-key"


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv("KT_ACCESS_KEY", KEY)
    monkeypatch.setattr(main, "store", MemoryStore())
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def open_app(monkeypatch):
    monkeypatch.delenv("KT_ACCESS_KEY", raising=False)
    monkeypatch.setattr(main, "store", MemoryStore())
    with TestClient(main.app) as c:
        yield c


def test_a_spending_endpoint_is_refused_without_the_key(guarded):
    """The one that matters: /api/review calls the grader, which costs money."""
    r = guarded.post("/api/review", json={"item_id": "x", "answer": "a", "confidence": 0.5})
    assert r.status_code == 401


def test_the_shell_itself_is_refused(guarded):
    assert guarded.get("/").status_code == 401
    assert guarded.get("/app.js").status_code == 401


def test_a_wrong_key_is_refused(guarded):
    assert guarded.get("/?k=not-the-key").status_code == 401


def test_the_key_in_the_query_sets_a_cookie_and_redirects_it_out_of_the_url(guarded):
    """The secret must not survive in history, in a screenshot, or in the PWA's
    saved start_url."""
    r = guarded.get(f"/?k={KEY}")
    assert r.status_code == 303
    assert "k=" not in r.headers["location"]

    cookie = r.cookies.get(COOKIE)
    assert cookie == KEY
    set_cookie = r.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")


def test_the_cookie_alone_is_enough_afterwards(guarded):
    guarded.cookies.set(COOKIE, KEY)
    assert guarded.get("/api/session").status_code == 200


def test_health_stays_open_so_the_platform_can_probe_it(guarded):
    """A gated health check is a service the platform restarts forever."""
    r = guarded.get("/api/health")
    assert r.status_code == 200
    assert r.json()["store"] == "memory"


def test_an_unset_key_disables_the_gate_for_local_use(open_app):
    assert open_app.get("/api/session").status_code == 200


# --- deployment plumbing ---------------------------------------------------


def test_the_schema_bootstrap_runs_every_statement_in_schema_sql(monkeypatch):
    """A fresh database has to be usable on first boot. Verified without a
    Postgres by watching what the store hands the connection."""
    from app import store as store_mod

    executed: list[str] = []

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *args): executed.append(sql)

    pg = object.__new__(store_mod.PostgresStore)
    monkeypatch.setattr(store_mod.PostgresStore, "_conn", lambda self: FakeConn())
    pg.ensure_schema()

    assert len(executed) == 1, "the whole file goes in one statement batch"
    sql = executed[0]
    for table in ("courses", "concepts", "items", "reviews", "api_spend"):
        assert f"create table if not exists {table}" in sql, table


def test_every_statement_in_the_schema_is_idempotent():
    """ensure_schema runs on every boot, so a statement without a guard would
    fail the second deploy — after the first one had already worked."""
    import re

    from app.store import SCHEMA

    sql = re.sub(r"--.*", "", SCHEMA.read_text(encoding="utf-8"))
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    assert statements, "schema.sql is empty"
    for s in statements:
        assert re.match(r"create (table|index) if not exists", s, re.I), s[:70]


def test_the_memory_store_has_the_same_bootstrap_call():
    """main.py calls ensure_schema() unconditionally; the memory store must
    answer it rather than crash local development."""
    from app.store import MemoryStore

    MemoryStore().ensure_schema()


@pytest.mark.parametrize(
    "bad,expect",
    [
        ("1. Connection string\npostgresql://u:p@h:5432/db", "postgresql://"),
        ("host: aws-1-eu-west-1.pooler.supabase.com", "postgresql://"),
        ("postgresql://u:[YOUR-PASSWORD]@h:5432/db", "placeholder"),
        ("", "postgresql://"),
    ],
)
def test_a_malformed_database_url_says_what_is_wrong(bad, expect):
    """psycopg's own message for this names a token, not the mistake. A deploy
    that dies at startup should say why in one line."""
    from app.store import _check_dsn

    with pytest.raises(ValueError, match=expect):
        _check_dsn(bad)


def test_a_good_database_url_passes_through_stripped():
    from app.store import _check_dsn

    dsn = "  postgresql://user:pw@aws-1-eu-west-1.pooler.supabase.com:5432/postgres  "
    assert _check_dsn(dsn) == dsn.strip()


def test_the_manifest_link_asks_for_credentials():
    """A manifest is fetched without cookies unless the link says otherwise.
    Behind the gate that is a 401, and a 401 manifest silently downgrades the
    PWA to a bookmark — installable-looking, not installable."""
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(encoding="utf-8")
    link = next(l for l in html.splitlines() if 'rel="manifest"' in l)
    assert 'crossorigin="use-credentials"' in link, link


def test_a_connection_failure_reports_its_cause_not_just_its_class():
    """The SDK's own message for a TLS or DNS failure is 'Connection error.' —
    three words that fit every possible reason. On a platform you cannot attach
    a debugger to, the exception chain is the only evidence available."""
    from app.ai.client import _why

    root = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer")
    middle = ConnectionError("Connection error.")
    middle.__cause__ = root

    line = _why(middle)
    assert "CERTIFICATE_VERIFY_FAILED" in line
    assert "<-" in line, "the chain must be visible, not just the outermost error"


def test_the_cause_chain_survives_a_cycle():
    a = ValueError("outer")
    b = ValueError("inner")
    a.__cause__ = b
    b.__cause__ = a          # a chain that would otherwise loop forever
    assert "outer" in _why_import()(a)


def _why_import():
    from app.ai.client import _why
    return _why
