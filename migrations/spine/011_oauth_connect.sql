-- AgentBroker on the spine, 2026-10-03: the OAuth "Connect" sign-in (MCP authorization).
--
-- Applies to the PostgreSQL database behind AgentBroker's PostgREST endpoint (the "spine"), run as the
-- role that owns the objects there (it bypasses row-level security). The operator's own apply tool runs it
-- BEFORE the code that uses it is deployed (the same order migration 010 was applied in).
--
-- ADDITIVE AND IDEMPOTENT. Nothing existing is dropped, renamed or altered: five new tables, their
-- indexes, sixteen new functions. Every CREATE is `if not exists` / `or replace`, every REVOKE/GRANT is
-- safe to repeat. The previous container image never calls any of it.
--
-- WHAT THIS IS FOR. A consumer assistant (Claude, ChatGPT, Grok, Muse) cannot paste a header key; it can
-- only run OAuth. The service becomes its own authorization server: the person taps Connect, gives an
-- email address, opens a one-time link we mail them, and the assistant is handed a short-lived access
-- token (the same signed Agent-Identity token a pasted key is) plus a rotating refresh token, both bound to
-- the account that email stands for. This file holds the durable half of that: registered clients, the
-- in-flight sign-ins, single-use authorization codes and the refresh tokens.
--
-- WHAT IS DELIBERATELY NOT STORED
--   * No raw secret anywhere. Authorization codes, magic-link tokens, poll secrets and refresh tokens are
--     stored as SHA-256 hex digests only; the server holds the only copy of the value, in memory, for the
--     length of one response. A read of these tables yields nothing that can be presented to the service.
--   * No email address. The person's identity is `email_hash` = sha256(lower(trim(email))) - the same input
--     the free-key flow already derives its account id from (free_<first 16 hex>) - plus a masked hint
--     ("j***@gmail.com") for the confirmation page. The address itself exists in memory long enough to send
--     one message.
--
-- LEAST PRIVILEGE. Every function is SECURITY DEFINER with a pinned search_path and starts from
-- `revoke all ... from public, anon, authenticated`; only `anon` (the credential the container holds) and
-- `service_role` may execute. The five tables are closed to anon/authenticated/public outright (RLS on, no
-- policy). No function returns a secret: the only values that leave are the digests' consequences (is this
-- code valid; whose account; which client). Each state change is ONE statement or one locked
-- read-then-write, so two concurrent clicks, redemptions or refreshes cannot both win.
--
-- OPERATING INVARIANT (same as migration 009). This is safe only while the database anon credential is a
-- secret held by the service and its operator. These functions let a holder mint a pending sign-in, spend a
-- code it already knows, or rotate a refresh token it already holds - none lets it read someone else's
-- token or enumerate accounts - but `oauth_account_link` binds an email digest to a credit account, which is
-- as sensitive as `credit_grant`. If the anon credential ever appears on a public page, revoke both first.
--
-- NEW FUNCTIONS IN `public` ARE GRANTED TO anon/authenticated BY DEFAULT on this cluster (default privileges
-- inherited from the hosted platform it was restored from). Hence the revoke at the head of each.

-- ----------------------------------------------------------------------------
-- 1. Tables
-- ----------------------------------------------------------------------------

-- Dynamically registered clients (RFC 7591). Clients that identify themselves with a Client ID Metadata
-- Document are NOT stored: their document is fetched on demand and cached in memory.
create table if not exists public.oauth_clients (
    client_id            text primary key,
    client_name          text,
    redirect_uris        jsonb       not null,
    registration_ip_hash text,
    created_at           timestamptz not null default now(),
    last_used_at         timestamptz
);

-- One in-flight sign-in. `status`: new -> email_sent -> verified -> completed, or denied/expired.
create table if not exists public.oauth_requests (
    request_id       text primary key,
    client_id        text        not null,
    redirect_uri     text        not null,
    code_challenge   text        not null,
    scope            text        not null,
    state            text,
    resource         text        not null,
    poll_hash        text,
    magic_hash       text,
    email_hash       text,
    email_hint       text,
    status           text        not null default 'new'
                     check (status in ('new', 'email_sent', 'verified', 'denied', 'completed')),
    email_sent_count integer     not null default 0,
    last_email_at    timestamptz,
    created_at       timestamptz not null default now(),
    expires_at       timestamptz not null,
    verified_at      timestamptz,
    completed_at     timestamptz
);
create unique index if not exists oauth_requests_magic_hash_ux
    on public.oauth_requests (magic_hash) where magic_hash is not null;
create index if not exists oauth_requests_expires_ix on public.oauth_requests (expires_at);

-- Authorization codes: single use, short-lived, bound to the client, redirect URI and PKCE challenge.
create table if not exists public.oauth_codes (
    code_hash      text primary key,
    request_id     text        not null,
    client_id      text        not null,
    redirect_uri   text        not null,
    code_challenge text        not null,
    scope          text        not null,
    resource       text        not null,
    email_hash     text        not null,
    created_at     timestamptz not null default now(),
    expires_at     timestamptz not null,
    used_at        timestamptz
);
create index if not exists oauth_codes_expires_ix on public.oauth_codes (expires_at);

-- Refresh tokens: rotated on every use. `family_id` = the sign-in that started the chain, so presenting an
-- already-rotated token (theft or replay) can revoke the whole chain at once.
create table if not exists public.oauth_refresh_tokens (
    token_hash        text primary key,
    family_id         text        not null,
    client_id         text        not null,
    email_hash        text        not null,
    scope             text        not null,
    resource          text        not null,
    created_at        timestamptz not null default now(),
    expires_at        timestamptz not null,
    family_expires_at timestamptz not null,
    rotated_at        timestamptz,
    revoked_at        timestamptz
);
create index if not exists oauth_refresh_family_ix on public.oauth_refresh_tokens (family_id);
create index if not exists oauth_refresh_expires_ix on public.oauth_refresh_tokens (family_expires_at);

-- Which credit account an email stands for once it has bought credits. Written by the Polar order webhook
-- (the only place that sees the buyer's email and the customer id together), read when a token is minted, so
-- credits bought on the website follow the person into the assistant at the next refresh. First writer wins:
-- an existing link is never overwritten by a function reachable with the anon credential.
create table if not exists public.oauth_account_links (
    email_hash  text primary key,
    account_id  text        not null,
    customer_id text,
    plan        text,
    linked_at   timestamptz not null default now()
);

revoke all on public.oauth_clients        from anon, authenticated, public;
revoke all on public.oauth_requests       from anon, authenticated, public;
revoke all on public.oauth_codes          from anon, authenticated, public;
revoke all on public.oauth_refresh_tokens from anon, authenticated, public;
revoke all on public.oauth_account_links  from anon, authenticated, public;
alter table public.oauth_clients        enable row level security;
alter table public.oauth_requests       enable row level security;
alter table public.oauth_codes          enable row level security;
alter table public.oauth_refresh_tokens enable row level security;
alter table public.oauth_account_links  enable row level security;

-- ----------------------------------------------------------------------------
-- 2. Readiness probe - lets the service tell "migration not applied yet" from "database down".
-- ----------------------------------------------------------------------------
create or replace function public.oauth_ready() returns jsonb
language sql stable security definer set search_path = public
as $$ select jsonb_build_object('ready', true, 'schema', 1) $$;

revoke all on function public.oauth_ready() from public, anon, authenticated;
grant execute on function public.oauth_ready() to anon, service_role;

-- ----------------------------------------------------------------------------
-- 3. Clients
-- ----------------------------------------------------------------------------
create or replace function public.oauth_client_register(
    p_client_id     text,
    p_client_name   text,
    p_redirect_uris jsonb,
    p_ip_hash       text default null
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    v_count bigint;
begin
    if p_client_id is null or p_client_id !~ '^[A-Za-z0-9_-]{16,64}$' then
        raise exception 'oauth_client_register: bad client_id' using errcode = '22023';
    end if;
    if p_redirect_uris is null or jsonb_typeof(p_redirect_uris) <> 'array'
       or jsonb_array_length(p_redirect_uris) < 1 or jsonb_array_length(p_redirect_uris) > 10 then
        raise exception 'oauth_client_register: redirect_uris must be 1 to 10 entries' using errcode = '22023';
    end if;

    -- A registry anyone can write to must have a ceiling. Registrations nobody ever used are the first to go.
    select count(*) into v_count from oauth_clients;
    if v_count >= 50000 then
        delete from oauth_clients
         where client_id in (select client_id from oauth_clients
                              where last_used_at is null and created_at < now() - interval '30 days'
                              limit 5000);
        select count(*) into v_count from oauth_clients;
        if v_count >= 50000 then
            return jsonb_build_object('stored', false, 'reason', 'registry_full');
        end if;
    end if;

    insert into oauth_clients (client_id, client_name, redirect_uris, registration_ip_hash)
    values (p_client_id, left(p_client_name, 120), p_redirect_uris, left(p_ip_hash, 64))
    on conflict (client_id) do nothing;
    return jsonb_build_object('stored', true);
end;
$$;

create or replace function public.oauth_client_get(p_client_id text) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    r oauth_clients%rowtype;
begin
    select * into r from oauth_clients where client_id = left(coalesce(p_client_id, ''), 64);
    if not found then
        return jsonb_build_object('found', false);
    end if;
    if r.last_used_at is null or r.last_used_at < now() - interval '1 day' then
        update oauth_clients set last_used_at = now() where client_id = r.client_id;
    end if;
    return jsonb_build_object('found', true, 'client_name', r.client_name,
                              'redirect_uris', r.redirect_uris);
end;
$$;

-- ----------------------------------------------------------------------------
-- 4. A sign-in, start to finish
-- ----------------------------------------------------------------------------

-- Start one. Also sweeps a few long-dead rows from each table so nothing grows without bound and no
-- scheduler is needed (each sweep is capped, so no single sign-in pays for a backlog).
create or replace function public.oauth_request_create(
    p_request_id     text,
    p_client_id      text,
    p_redirect_uri   text,
    p_code_challenge text,
    p_scope          text,
    p_state          text,
    p_resource       text,
    p_ttl_seconds    integer default 900
) returns jsonb
language plpgsql security definer set search_path = public
as $$
begin
    if p_request_id is null or p_request_id !~ '^[A-Za-z0-9_-]{16,64}$' then
        raise exception 'oauth_request_create: bad request_id' using errcode = '22023';
    end if;
    if coalesce(p_client_id, '') = '' or length(p_client_id) > 2048
       or coalesce(p_redirect_uri, '') = '' or length(p_redirect_uri) > 2048
       or coalesce(p_resource, '') = '' or length(p_resource) > 512
       or coalesce(p_scope, '') = '' or length(p_scope) > 256
       or length(coalesce(p_state, '')) > 2048 then
        raise exception 'oauth_request_create: field missing or too long' using errcode = '22023';
    end if;
    if p_code_challenge is null or p_code_challenge !~ '^[A-Za-z0-9_-]{43}$' then
        raise exception 'oauth_request_create: code_challenge must be a 43-character S256 digest'
            using errcode = '22023';
    end if;

    delete from oauth_requests where request_id in
        (select request_id from oauth_requests where expires_at < now() - interval '1 day' limit 100);
    delete from oauth_codes where code_hash in
        (select code_hash from oauth_codes where expires_at < now() - interval '1 day' limit 100);
    delete from oauth_refresh_tokens where token_hash in
        (select token_hash from oauth_refresh_tokens where family_expires_at < now() - interval '1 day' limit 100);

    insert into oauth_requests (request_id, client_id, redirect_uri, code_challenge, scope, state, resource,
                                expires_at)
    values (p_request_id, p_client_id, p_redirect_uri, p_code_challenge, left(p_scope, 256),
            nullif(p_state, ''), p_resource,
            now() + make_interval(secs => greatest(60, least(coalesce(p_ttl_seconds, 900), 1800))));
    return jsonb_build_object('created', true);
end;
$$;

-- Read the public face of a sign-in (never a hash).
create or replace function public.oauth_request_get(p_request_id text) returns jsonb
language plpgsql stable security definer set search_path = public
as $$
declare
    r oauth_requests%rowtype;
begin
    select * into r from oauth_requests where request_id = left(coalesce(p_request_id, ''), 64);
    if not found then
        return jsonb_build_object('found', false);
    end if;
    return jsonb_build_object(
        'found', true, 'client_id', r.client_id, 'redirect_uri', r.redirect_uri, 'scope', r.scope,
        'state', r.state, 'resource', r.resource, 'email_hint', r.email_hint,
        'status', case when r.expires_at < now() and r.status <> 'completed' then 'expired' else r.status end,
        'email_sent_count', r.email_sent_count);
end;
$$;

-- Record that a link was mailed for this sign-in. Replaces any earlier link (so only the newest works),
-- binds the poll secret on first use, and enforces the resend gap and ceiling where it cannot be raced.
create or replace function public.oauth_request_set_email(
    p_request_id      text,
    p_poll_hash       text,
    p_magic_hash      text,
    p_email_hash      text,
    p_email_hint      text,
    p_min_gap_seconds integer default 20,
    p_max_sends       integer default 5
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    r oauth_requests%rowtype;
begin
    if coalesce(p_poll_hash, '') !~ '^[0-9a-f]{64}$' or coalesce(p_magic_hash, '') !~ '^[0-9a-f]{64}$'
       or coalesce(p_email_hash, '') !~ '^[0-9a-f]{64}$' then
        raise exception 'oauth_request_set_email: hashes must be 64 hex characters' using errcode = '22023';
    end if;
    select * into r from oauth_requests where request_id = left(coalesce(p_request_id, ''), 64) for update;
    if not found then
        return jsonb_build_object('ok', false, 'reason', 'not_found');
    end if;
    if r.expires_at < now() then
        return jsonb_build_object('ok', false, 'reason', 'expired');
    end if;
    if r.status not in ('new', 'email_sent') then
        return jsonb_build_object('ok', false, 'reason', 'bad_state');
    end if;
    if r.poll_hash is not null and r.poll_hash <> p_poll_hash then
        return jsonb_build_object('ok', false, 'reason', 'poll_mismatch');
    end if;
    if r.email_sent_count >= greatest(1, least(coalesce(p_max_sends, 5), 10)) then
        return jsonb_build_object('ok', false, 'reason', 'too_many');
    end if;
    if r.last_email_at is not null
       and now() - r.last_email_at < make_interval(secs => greatest(0, least(coalesce(p_min_gap_seconds, 20), 300))) then
        return jsonb_build_object('ok', false, 'reason', 'too_soon');
    end if;
    update oauth_requests
       set poll_hash = p_poll_hash, magic_hash = p_magic_hash, email_hash = p_email_hash,
           email_hint = left(p_email_hint, 120), status = 'email_sent',
           email_sent_count = email_sent_count + 1, last_email_at = now()
     where request_id = r.request_id;
    return jsonb_build_object('ok', true, 'sends', r.email_sent_count + 1);
end;
$$;

-- Look a sign-in up by its mailed link WITHOUT using the link up (a mail scanner opens links; only a
-- deliberate button press may spend one).
create or replace function public.oauth_request_lookup_magic(p_magic_hash text) returns jsonb
language plpgsql stable security definer set search_path = public
as $$
declare
    r oauth_requests%rowtype;
begin
    if coalesce(p_magic_hash, '') !~ '^[0-9a-f]{64}$' then
        return jsonb_build_object('found', false);
    end if;
    select * into r from oauth_requests where magic_hash = p_magic_hash;
    if not found then
        return jsonb_build_object('found', false);
    end if;
    return jsonb_build_object(
        'found', true, 'request_id', r.request_id, 'client_id', r.client_id,
        'redirect_uri', r.redirect_uri, 'scope', r.scope, 'resource', r.resource,
        'email_hint', r.email_hint,
        'status', case when r.expires_at < now() and r.status <> 'completed' then 'expired' else r.status end);
end;
$$;

-- The deliberate press: approve or deny. One UPDATE, so two presses cannot both win.
create or replace function public.oauth_request_decide(
    p_magic_hash text,
    p_approve    boolean
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    v_id text;
    r    oauth_requests%rowtype;
begin
    if coalesce(p_magic_hash, '') !~ '^[0-9a-f]{64}$' then
        return jsonb_build_object('ok', false, 'reason', 'not_found');
    end if;
    update oauth_requests
       set status = case when p_approve then 'verified' else 'denied' end, verified_at = now()
     where magic_hash = p_magic_hash and status = 'email_sent' and expires_at > now()
    returning request_id into v_id;
    if v_id is not null then
        return jsonb_build_object('ok', true, 'request_id', v_id,
                                  'status', case when p_approve then 'verified' else 'denied' end);
    end if;
    select * into r from oauth_requests where magic_hash = p_magic_hash;
    if not found then
        return jsonb_build_object('ok', false, 'reason', 'not_found');
    end if;
    if r.expires_at <= now() and r.status not in ('completed') then
        return jsonb_build_object('ok', false, 'reason', 'expired');
    end if;
    return jsonb_build_object('ok', false, 'reason', 'already_used', 'request_id', r.request_id,
                              'status', r.status);
end;
$$;

-- Only the party holding the poll secret learns the state of a sign-in. Anyone else gets 'unknown'.
create or replace function public.oauth_request_poll(p_request_id text, p_poll_hash text) returns jsonb
language plpgsql stable security definer set search_path = public
as $$
declare
    r oauth_requests%rowtype;
begin
    select * into r from oauth_requests where request_id = left(coalesce(p_request_id, ''), 64);
    if not found or r.poll_hash is null or r.poll_hash <> coalesce(p_poll_hash, '') then
        return jsonb_build_object('status', 'unknown');
    end if;
    return jsonb_build_object(
        'status', case when r.expires_at < now() and r.status <> 'completed' then 'expired' else r.status end);
end;
$$;

-- Hand the sign-in's result to the party that started it, exactly once. Approved: the authorization code is
-- created here (its digest stored, the value known only to the caller). Denied: the denial is delivered once.
create or replace function public.oauth_request_complete(
    p_request_id      text,
    p_poll_hash       text,
    p_code_hash       text,
    p_code_ttl_seconds integer default 120
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    r oauth_requests%rowtype;
begin
    if coalesce(p_code_hash, '') !~ '^[0-9a-f]{64}$' then
        raise exception 'oauth_request_complete: code_hash must be 64 hex characters' using errcode = '22023';
    end if;
    select * into r from oauth_requests where request_id = left(coalesce(p_request_id, ''), 64) for update;
    if not found or r.poll_hash is null or r.poll_hash <> coalesce(p_poll_hash, '') then
        return jsonb_build_object('ok', false, 'reason', 'unknown');
    end if;
    if r.expires_at < now() and r.status not in ('completed') then
        return jsonb_build_object('ok', false, 'reason', 'expired');
    end if;
    if r.status = 'completed' then
        return jsonb_build_object('ok', false, 'reason', 'completed');
    end if;
    if r.status not in ('verified', 'denied') then
        return jsonb_build_object('ok', false, 'reason', 'not_ready');
    end if;

    if r.status = 'denied' then
        update oauth_requests set status = 'completed', completed_at = now() where request_id = r.request_id;
        return jsonb_build_object('ok', true, 'outcome', 'denied', 'client_id', r.client_id,
                                  'redirect_uri', r.redirect_uri, 'state', r.state);
    end if;

    if r.email_hash is null then
        return jsonb_build_object('ok', false, 'reason', 'not_ready');
    end if;
    insert into oauth_codes (code_hash, request_id, client_id, redirect_uri, code_challenge, scope, resource,
                             email_hash, expires_at)
    values (p_code_hash, r.request_id, r.client_id, r.redirect_uri, r.code_challenge, r.scope, r.resource,
            r.email_hash, now() + make_interval(secs => greatest(30, least(coalesce(p_code_ttl_seconds, 120), 600))));
    update oauth_requests set status = 'completed', completed_at = now() where request_id = r.request_id;
    return jsonb_build_object('ok', true, 'outcome', 'approved', 'client_id', r.client_id,
                              'redirect_uri', r.redirect_uri, 'state', r.state);
end;
$$;

-- ----------------------------------------------------------------------------
-- 5. Codes and refresh tokens
-- ----------------------------------------------------------------------------

-- Spend an authorization code. ONE UPDATE ... RETURNING: of two concurrent redemptions exactly one gets the
-- row. Presenting a code that was already spent revokes every refresh token issued from that sign-in (RFC 6749
-- section 4.1.2: a replayed code means the first redemption may have been the thief's).
create or replace function public.oauth_code_consume(p_code_hash text) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    r oauth_codes%rowtype;
begin
    if coalesce(p_code_hash, '') !~ '^[0-9a-f]{64}$' then
        return jsonb_build_object('ok', false, 'reason', 'invalid');
    end if;
    update oauth_codes set used_at = now()
     where code_hash = p_code_hash and used_at is null and expires_at > now()
    returning * into r;
    if found then
        return jsonb_build_object('ok', true, 'request_id', r.request_id, 'client_id', r.client_id,
                                  'redirect_uri', r.redirect_uri, 'code_challenge', r.code_challenge,
                                  'scope', r.scope, 'resource', r.resource, 'email_hash', r.email_hash);
    end if;
    select * into r from oauth_codes where code_hash = p_code_hash;
    if found and r.used_at is not null then
        update oauth_refresh_tokens set revoked_at = now() where family_id = r.request_id and revoked_at is null;
        return jsonb_build_object('ok', false, 'reason', 'reused');
    end if;
    return jsonb_build_object('ok', false, 'reason', 'invalid');
end;
$$;

create or replace function public.oauth_refresh_store(
    p_token_hash         text,
    p_family_id          text,
    p_client_id          text,
    p_email_hash         text,
    p_scope              text,
    p_resource           text,
    p_ttl_seconds        integer,
    p_family_ttl_seconds integer
) returns jsonb
language plpgsql security definer set search_path = public
as $$
begin
    if coalesce(p_token_hash, '') !~ '^[0-9a-f]{64}$' or coalesce(p_email_hash, '') !~ '^[0-9a-f]{64}$' then
        raise exception 'oauth_refresh_store: hashes must be 64 hex characters' using errcode = '22023';
    end if;
    insert into oauth_refresh_tokens (token_hash, family_id, client_id, email_hash, scope, resource,
                                      expires_at, family_expires_at)
    values (p_token_hash, left(p_family_id, 64), left(p_client_id, 2048), p_email_hash, left(p_scope, 256),
            left(p_resource, 512),
            now() + make_interval(secs => greatest(60, least(coalesce(p_ttl_seconds, 2592000), 7776000))),
            now() + make_interval(secs => greatest(60, least(coalesce(p_family_ttl_seconds, 7776000), 15552000))))
    on conflict (token_hash) do nothing;
    return jsonb_build_object('stored', true);
end;
$$;

-- Exchange a refresh token for its successor. The old one is spent in the same transaction the new one is
-- written in. Presenting one that was already spent is reuse: the whole chain is revoked, because either the
-- legitimate client or a thief now holds a token that must never work again.
create or replace function public.oauth_refresh_rotate(
    p_old_hash    text,
    p_new_hash    text,
    p_client_id   text,
    p_ttl_seconds integer
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    t       oauth_refresh_tokens%rowtype;
    v_exp   timestamptz;
begin
    if coalesce(p_old_hash, '') !~ '^[0-9a-f]{64}$' or coalesce(p_new_hash, '') !~ '^[0-9a-f]{64}$' then
        return jsonb_build_object('ok', false, 'reason', 'invalid');
    end if;
    select * into t from oauth_refresh_tokens where token_hash = p_old_hash for update;
    if not found then
        return jsonb_build_object('ok', false, 'reason', 'invalid');
    end if;
    if t.client_id <> coalesce(p_client_id, '') then
        return jsonb_build_object('ok', false, 'reason', 'client_mismatch');
    end if;
    if t.revoked_at is not null then
        return jsonb_build_object('ok', false, 'reason', 'invalid');
    end if;
    if t.rotated_at is not null then
        update oauth_refresh_tokens set revoked_at = now() where family_id = t.family_id and revoked_at is null;
        return jsonb_build_object('ok', false, 'reason', 'reuse');
    end if;
    if t.expires_at < now() or t.family_expires_at < now() then
        return jsonb_build_object('ok', false, 'reason', 'expired');
    end if;
    v_exp := least(now() + make_interval(secs => greatest(60, least(coalesce(p_ttl_seconds, 2592000), 7776000))),
                   t.family_expires_at);
    update oauth_refresh_tokens set rotated_at = now() where token_hash = t.token_hash;
    insert into oauth_refresh_tokens (token_hash, family_id, client_id, email_hash, scope, resource,
                                      expires_at, family_expires_at)
    values (p_new_hash, t.family_id, t.client_id, t.email_hash, t.scope, t.resource, v_exp, t.family_expires_at);
    return jsonb_build_object('ok', true, 'family_id', t.family_id, 'email_hash', t.email_hash,
                              'scope', t.scope, 'resource', t.resource);
end;
$$;

-- RFC 7009 revocation: the whole chain, for the client that holds it. Always answers the same way.
create or replace function public.oauth_refresh_revoke(p_token_hash text, p_client_id text) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    v_family text;
begin
    if coalesce(p_token_hash, '') !~ '^[0-9a-f]{64}$' then
        return jsonb_build_object('revoked', false);
    end if;
    select family_id into v_family from oauth_refresh_tokens
     where token_hash = p_token_hash and client_id = coalesce(p_client_id, '');
    if v_family is null then
        return jsonb_build_object('revoked', false);
    end if;
    update oauth_refresh_tokens set revoked_at = now() where family_id = v_family and revoked_at is null;
    return jsonb_build_object('revoked', true);
end;
$$;

-- ----------------------------------------------------------------------------
-- 6. Which credit account an email stands for
-- ----------------------------------------------------------------------------

-- Record the link between a buyer's email and the credit account their order funded. INSERT ... DO NOTHING:
-- an existing link is never changed by this function.
create or replace function public.oauth_account_link(
    p_email_hash  text,
    p_account_id  text,
    p_customer_id text default null,
    p_plan        text default null
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
    v_n integer;
begin
    if coalesce(p_email_hash, '') !~ '^[0-9a-f]{64}$' or coalesce(p_account_id, '') !~ '^sub_[A-Za-z0-9_.:-]{1,120}$' then
        raise exception 'oauth_account_link: bad email_hash or account_id' using errcode = '22023';
    end if;
    insert into oauth_account_links (email_hash, account_id, customer_id, plan)
    values (p_email_hash, p_account_id, left(p_customer_id, 120), left(p_plan, 32))
    on conflict (email_hash) do nothing;
    get diagnostics v_n = row_count;
    return jsonb_build_object('linked', v_n = 1);
end;
$$;

-- Which paid account, if any, does this email digest stand for? First the explicit link; failing that, a
-- credit account whose own email matches (accounts created before the link existed). Returns identifiers
-- only - no balance, no email, no token.
create or replace function public.oauth_account_for_email(p_email_hash text) returns jsonb
language plpgsql stable security definer set search_path = public
as $$
declare
    l oauth_account_links%rowtype;
    c record;
begin
    if coalesce(p_email_hash, '') !~ '^[0-9a-f]{64}$' then
        return jsonb_build_object('found', false);
    end if;
    select * into l from oauth_account_links where email_hash = p_email_hash;
    if found then
        return jsonb_build_object('found', true, 'account_id', l.account_id, 'customer_id', l.customer_id,
                                  'plan', l.plan);
    end if;
    select account_id, customer_id, plan into c
      from credit_accounts
     where account_id like 'sub\_%' escape '\'
       and email is not null
       and encode(sha256(convert_to(lower(btrim(email)), 'UTF8')), 'hex') = p_email_hash
     order by updated_at desc
     limit 1;
    if found then
        return jsonb_build_object('found', true, 'account_id', c.account_id, 'customer_id', c.customer_id,
                                  'plan', c.plan);
    end if;
    return jsonb_build_object('found', false);
end;
$$;

-- ----------------------------------------------------------------------------
-- 7. Grants - last, and explicit, per function
-- ----------------------------------------------------------------------------
revoke all on function public.oauth_client_register(text, text, jsonb, text) from public, anon, authenticated;
grant execute on function public.oauth_client_register(text, text, jsonb, text) to anon, service_role;
revoke all on function public.oauth_client_get(text) from public, anon, authenticated;
grant execute on function public.oauth_client_get(text) to anon, service_role;
revoke all on function public.oauth_request_create(text, text, text, text, text, text, text, integer)
    from public, anon, authenticated;
grant execute on function public.oauth_request_create(text, text, text, text, text, text, text, integer)
    to anon, service_role;
revoke all on function public.oauth_request_get(text) from public, anon, authenticated;
grant execute on function public.oauth_request_get(text) to anon, service_role;
revoke all on function public.oauth_request_set_email(text, text, text, text, text, integer, integer)
    from public, anon, authenticated;
grant execute on function public.oauth_request_set_email(text, text, text, text, text, integer, integer)
    to anon, service_role;
revoke all on function public.oauth_request_lookup_magic(text) from public, anon, authenticated;
grant execute on function public.oauth_request_lookup_magic(text) to anon, service_role;
revoke all on function public.oauth_request_decide(text, boolean) from public, anon, authenticated;
grant execute on function public.oauth_request_decide(text, boolean) to anon, service_role;
revoke all on function public.oauth_request_poll(text, text) from public, anon, authenticated;
grant execute on function public.oauth_request_poll(text, text) to anon, service_role;
revoke all on function public.oauth_request_complete(text, text, text, integer) from public, anon, authenticated;
grant execute on function public.oauth_request_complete(text, text, text, integer) to anon, service_role;
revoke all on function public.oauth_code_consume(text) from public, anon, authenticated;
grant execute on function public.oauth_code_consume(text) to anon, service_role;
revoke all on function public.oauth_refresh_store(text, text, text, text, text, text, integer, integer)
    from public, anon, authenticated;
grant execute on function public.oauth_refresh_store(text, text, text, text, text, text, integer, integer)
    to anon, service_role;
revoke all on function public.oauth_refresh_rotate(text, text, text, integer) from public, anon, authenticated;
grant execute on function public.oauth_refresh_rotate(text, text, text, integer) to anon, service_role;
revoke all on function public.oauth_refresh_revoke(text, text) from public, anon, authenticated;
grant execute on function public.oauth_refresh_revoke(text, text) to anon, service_role;
revoke all on function public.oauth_account_link(text, text, text, text) from public, anon, authenticated;
grant execute on function public.oauth_account_link(text, text, text, text) to anon, service_role;
revoke all on function public.oauth_account_for_email(text) from public, anon, authenticated;
grant execute on function public.oauth_account_for_email(text) to anon, service_role;
