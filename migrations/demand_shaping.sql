-- Demand shaping / conversation threading schema.
--
-- These tables were created ad-hoc as the correlation layer was built, so the
-- schema existed only in production. That is a real risk: nothing could rebuild
-- it, and no reviewer could read what the code assumes. This file is the record.
--
-- IF NOT EXISTS throughout: safe to run against the live project.

-- ---------------------------------------------------------------------------
-- Conversation threading (core/conversations.py)
-- ---------------------------------------------------------------------------
create table if not exists conversations (
    conversation_id      text primary key,
    ref_token            text,
    agent_id             text,
    end_user_ref         text,
    business_id          text,
    business_number      text,          -- normalized: digits only
    our_number           text,          -- normalized: digits only
    channel              text default 'whatsapp',
    state                text default 'open',
    intent               text,
    last_outbound_wamid  text,
    last_inbound_at      timestamptz,
    expires_at           timestamptz,
    created_at           timestamptz default now(),
    updated_at           timestamptz default now()
);

-- The pair lookup runs on every inbound message; the business_id + created_at
-- pair backs the per-business rate window in demand_shaping.check_budget.
create index if not exists conversations_pair_idx
    on conversations (our_number, business_number, state);
create index if not exists conversations_business_time_idx
    on conversations (business_id, created_at desc);
create index if not exists conversations_ref_idx
    on conversations (ref_token, business_number);

create table if not exists conversation_messages (
    id               bigserial primary key,
    conversation_id  text references conversations (conversation_id),
    direction        text check (direction in ('in', 'out')),
    wamid            text,
    body             text,
    created_at       timestamptz default now()
);

-- find_by_wamid resolves a reply's context.id through this ledger.
create unique index if not exists conversation_messages_wamid_idx
    on conversation_messages (wamid) where wamid is not null;
create index if not exists conversation_messages_conv_idx
    on conversation_messages (conversation_id, created_at);

-- ---------------------------------------------------------------------------
-- Demand queue (core/demand_queue.py)
--
-- An over-budget request is QUEUED, not dropped. Until this table existed the
-- receipt said "queued" and nothing stored it, which made the word untrue.
-- ---------------------------------------------------------------------------
create table if not exists pending_requests (
    request_id       text primary key,
    idem_key         text,              -- stops an honest retry double-queueing
    business_id      text,
    business_number  text,
    agent_id         text,
    end_user_ref     text,
    intent           text,
    ref_token        text,
    digest_id        text,
    state            text default 'queued',
        -- queued | dispatched | accepted | declined | expired
    created_at       timestamptz default now(),
    updated_at       timestamptz default now(),
    expires_at       timestamptz
);

create index if not exists pending_requests_business_idx
    on pending_requests (business_id, state, created_at);
create index if not exists pending_requests_number_idx
    on pending_requests (business_number, state);
create index if not exists pending_requests_digest_idx
    on pending_requests (digest_id);
-- One live queue entry per (business, idempotency key): the retry an agent
-- makes after our own retry_after_ms must not appear twice in one digest.
create unique index if not exists pending_requests_idem_idx
    on pending_requests (business_id, idem_key) where state = 'queued';

-- ---------------------------------------------------------------------------
-- Dispatched digests
--
-- request_ids is ORDERED and immutable: index i is what the business saw as
-- item i+1. Replies are scored against this snapshot, never against the live
-- queue, which may have grown since the digest was sent.
-- ---------------------------------------------------------------------------
create table if not exists demand_digests (
    digest_id        text primary key,
    business_id      text,
    business_number  text,
    our_number       text,
    wamid            text,
    request_ids      jsonb not null default '[]'::jsonb,
    state            text default 'awaiting_reply',   -- awaiting_reply | resolved
    created_at       timestamptz default now(),
    updated_at       timestamptz default now()
);

create index if not exists demand_digests_pair_idx
    on demand_digests (our_number, business_number, state, created_at desc);
