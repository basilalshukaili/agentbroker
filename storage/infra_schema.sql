-- Infra contributor programme: nodes, one-time tokens, and receipts.
--
-- RLS IS ON FROM THE FIRST LINE. On 2026-08-26 this project shipped 14 public
-- tables with RLS off, one of which let an anon key read email + verification
-- token together - a free-key takeover. The `rls` health check now catches any
-- new table, so a table created without it fails the monitor rather than
-- sitting quietly readable. service_role bypasses RLS, which is how the API
-- routes reach these.
--
-- WHAT IS SENSITIVE HERE, and it is not obvious:
--   * `seed` is the node's shared secret. Anyone holding it can answer that
--     node's challenges from anywhere. It is returned to the node exactly once
--     at enrollment and never again.
--   * `declared_specs` is a blob the NODE authored. It is stored so the
--     measured probe has something to contradict - never as fact.
--   * There is deliberately no email, org, or subscription column. The node
--     never sends one, because `claude auth status` returning a friend's email
--     does not make it ours to keep.

create table if not exists infra_tokens (
    token_hash    text primary key,          -- sha256; the plaintext never rests here
    owner         text not null,             -- who the CEO bot issued it to
    issued_at     timestamptz not null default now(),
    expires_at    timestamptz,
    used_at       timestamptz,               -- burned on enrollment; single use, full stop
    node_id       text
);

create table if not exists infra_nodes (
    node_id           text primary key,      -- SERVER-assigned; never derived from the machine
    owner             text not null,
    seed              text not null,
    blob_bytes        bigint not null,
    declared_specs    jsonb not null default '{}'::jsonb,
    enrolled_at       timestamptz not null default now(),
    last_seen_at      timestamptz,
    retired_at        timestamptz,
    epoch             integer not null default 0,
    last_answer       text not null default repeat('0', 64),
    pending_nonce     text,
    pending_offsets   jsonb,
    pending_issued_at timestamptz,
    persistence_mode  text,                  -- windows-task-onlogon | none
    platform          text
);

create table if not exists infra_receipts (
    id          bigserial primary key,
    node_id     text not null,
    owner       text not null,
    epoch       integer not null,
    at          timestamptz not null default now(),
    credited    boolean not null default false
);

-- The accounting query collapses receipts to distinct time slots per PERSON,
-- so this index is on (owner, at) rather than (node_id, at): ten containers
-- belonging to one person are read together, which is the whole point.
create index if not exists idx_infra_receipts_owner_at on infra_receipts (owner, at desc);
create index if not exists idx_infra_nodes_owner on infra_nodes (owner);

alter table infra_tokens   enable row level security;
alter table infra_nodes    enable row level security;
alter table infra_receipts enable row level security;

-- No policies are defined on purpose. With RLS enabled and no policy, the anon
-- and authenticated roles can read nothing at all, while service_role (used by
-- the API routes) bypasses RLS entirely. A contributor reads their own figures
-- through an authenticated endpoint that checks who they are - never by
-- querying these tables directly with a public key.
