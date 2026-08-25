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
    importance         int  not null default 3,   -- 1..5, drives generation depth
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
    rubric_hits  jsonb not null,                  -- [{id, hit, note}]
    verdict      text not null,
    confidence   double precision not null,       -- asked BEFORE the answer is revealed (§1.3)
    fsrs_state   jsonb not null,                  -- fsrs.Card.to_dict()
    due_at       timestamptz not null,
    reviewed_at  timestamptz not null default now()
);
create index if not exists reviews_item_idx on reviews (item_id, reviewed_at desc);
create index if not exists reviews_due_idx  on reviews (due_at);
