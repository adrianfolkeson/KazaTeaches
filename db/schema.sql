-- Studiesystem — datamodell v1 (§3).
-- Scheduling unit is the ITEM, not the concept. Mastery per concept is DERIVED
-- from its items' reviews and is deliberately not stored here.

create table if not exists courses (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    created_at  timestamptz not null default now()
);

create table if not exists concepts (
    id                 uuid primary key default gen_random_uuid(),
    course_id          uuid not null references courses(id) on delete cascade,
    name               text not null,
    importance        text not null
        check (importance in ('core', 'supporting', 'nice_to_know')),
    short_explanation  text not null default '',
    created_at         timestamptz not null default now()
);
create index if not exists concepts_course_idx on concepts (course_id);

create table if not exists items (
    id                uuid primary key default gen_random_uuid(),
    concept_id        uuid not null references concepts(id) on delete cascade,
    type              text not null,              -- definition | explanation | comparison | scenario | teach_me | ...
    prompt            text not null,
    reference_answer  text not null,
    -- Rubric is generated ONCE, at item creation (§1.1). Grading matches against
    -- it instead of re-deriving criteria per review: cheap, consistent, cacheable.
    rubric            jsonb not null,             -- [{id, required, desc}]
    created_at        timestamptz not null default now()
);
create index if not exists items_concept_idx on items (concept_id);

create table if not exists reviews (
    id           uuid primary key default gen_random_uuid(),
    item_id      uuid not null references items(id) on delete cascade,
    answer       text not null,
    score        double precision not null,
    rubric_hits  jsonb not null,                  -- [{id, status: hit|partial|miss, note}]
    verdict      text not null,
    confidence   double precision not null,       -- asked BEFORE the answer is revealed (§1.3)
    fsrs_state   jsonb not null,                  -- fsrs.Card.to_dict()
    due_at       timestamptz not null,
    reviewed_at  timestamptz not null default now()
);
create index if not exists reviews_item_idx on reviews (item_id, reviewed_at desc);
create index if not exists reviews_due_idx  on reviews (due_at);

-- Spend ledger for the monthly cap (app/budget.py). One row per paid API call.
-- `month` is the UTC calendar month 'YYYY-MM' — the same boundary the invoice
-- uses, so the cap resets when the bill does.
create table if not exists api_spend (
    id                 uuid primary key default gen_random_uuid(),
    month              text not null,
    model              text not null,
    cost_usd           double precision not null,
    input_tokens       integer not null,
    output_tokens      integer not null,      -- thinking tokens are billed here
    cache_read_tokens  integer not null,
    cache_write_tokens integer not null,
    spent_at           timestamptz not null default now()
);
create index if not exists api_spend_month_idx on api_spend (month);

-- Generated but unreviewed drafts. In process memory this survived exactly as
-- long as the container did, and on a free tier that is fifteen idle minutes —
-- shorter than reviewing a draft takes. A review gate that deletes what you are
-- reviewing is not a gate.
create table if not exists drafts (
    id           uuid primary key,
    course_id    uuid not null references courses(id) on delete cascade,
    course_name  text not null,
    payload      jsonb not null,          -- the whole GenerationDraft
    n_items      integer not null,
    cost_usd     double precision not null,
    created_at   timestamptz not null default now()
);
create index if not exists drafts_created_idx on drafts (created_at desc);
