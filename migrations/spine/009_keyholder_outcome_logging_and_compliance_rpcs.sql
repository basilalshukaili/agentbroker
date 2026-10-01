-- AgentBroker on the spine, 2026-10-01: outcome logging + the compliance write/read doors.
--
-- Applies to the VPS "spine" (PostgREST at https://techmate.om/spine, Postgres database `spine`),
-- as `spine_owner` (BYPASSRLS, owns the objects). Run with:
--
--     python scripts/apply_sql.py <this file>          (HatchLoop repo, SUPABASE_DB_URL = spine tunnel)
--
-- ADDITIVE AND IDEMPOTENT. Nothing here is dropped or renamed: the live container (build cd1b9a9)
-- keeps calling `usage_events_insert` and keeps working while this is applied, and the previous
-- container image still works after it. Every CREATE is `or replace`, every ALTER is `if not exists`,
-- every REVOKE/GRANT is safe to repeat.
--
-- WHAT THIS FIXES (docs/reviews/2026-09-30-agentbroker-keyholder-audit.md, fixes 1 and 8):
--
--   1. usage_events recorded only SUCCESSFUL requests and nothing about how they went. The new
--      columns and `usage_events_insert_v2` let the server log EVERY outcome (success, tool failure,
--      protocol error, crash, HTTP 429/5xx) with the status code, latency, client name, the state of
--      the caller's key (valid / invalid / expired / placeholder / none) and the NAMES of the
--      arguments - never their values.
--
--   2. compliance_audit and pending_keys writes were rejected by RLS (the container holds only the
--      `anon` JWT, which does not bypass RLS), 74 failures in 21 hours; and consent_optouts could
--      neither be read at start-up (OPTOUT_HYDRATION_FAILED, HTTP 403 on the spine) nor written
--      (a STOP link or a WhatsApp STOP could not be recorded durably). Each gets one narrow
--      SECURITY DEFINER function; the direct table door is closed for anon/authenticated/public.
--
-- THREAT MODEL, STATED PRECISELY BECAUSE 006 REFUSED A BULK READ AND THIS ADDS ONE.
-- 006 declined to expose the opt-out list because on Supabase the anon key sat in public pages:
-- anyone could have downloaded every phone number and email that ever texted STOP. On the spine
-- that premise is gone: PostgREST has no anonymous role, the edge requires a valid JWT, and the
-- anon key is a secret held by the service and its operator, not a published one.
-- `consent_optouts_hydrate` is therefore callable only by the holder of a secret, and
-- three in-process readers (core/demand_queue.py, core/schedule_appointment.py,
-- agent_interface/whatsapp_webhook.py) consult the in-memory set WITHOUT the per-send durable
-- check, so the boot-time load is load-bearing for them. The per-contact boolean
-- `consent_optouts_is_opted_out` (006) stays the authoritative send-path check and is unchanged.
-- If the anon key ever returns to a public page, revoke `consent_optouts_hydrate` first.
--
-- WHAT A FORGED CALL COULD DO (the holder of the anon key; same honest accounting as 003/005):
--   * usage_events_insert_v2 - add analytics rows. Bounded: lengths are clamped, the enumerations
--     are validated, nothing is read back.
--   * compliance_audit_insert - add an audit row. It cannot overwrite or delete one: the insert is
--     `on conflict (audit_id) do nothing`, and there is no update or delete function.
--   * pending_keys_upsert / pending_keys_consume - overwrite or spend a PENDING email verification.
--     Verification links are still HMAC-signed (verify_token runs first), so this cannot mint or
--     claim a key; at worst it makes one pending link read as "already used".
--   * consent_optouts_record - add an opt-out, which only ever SUPPRESSES messages.
--   None of them can read a stored token, a stored audit row, or anyone's key.
--
-- NEW FUNCTIONS IN `public` ARE GRANTED TO anon/authenticated BY DEFAULT on this cluster (default
-- privileges inherited from the hosted platform it was restored from). Every function below therefore
-- starts with `revoke all ... from public, anon, authenticated` and grants only what is needed.

-- ----------------------------------------------------------------------------
-- 1. usage_events: outcome columns (all nullable; old rows and the old RPC are untouched)
-- ----------------------------------------------------------------------------
alter table public.usage_events
    add column if not exists outcome        text,
    add column if not exists error_code     text,
    add column if not exists http_status    integer,
    add column if not exists latency_ms     integer,
    add column if not exists client_name    text,
    add column if not exists client_version text,
    add column if not exists key_state      text,
    add column if not exists arg_names      text[],
    add column if not exists requested_name text,
    add column if not exists detail         text;

comment on column public.usage_events.outcome is
    'ok | tool_failure | tool_error | rpc_error | exception | http_error. NULL on rows written before 2026-10-01.';
comment on column public.usage_events.key_state is
    'none | valid | invalid | expired | placeholder - what the caller presented in X-Agent-Identity / Authorization / X-Api-Key.';
comment on column public.usage_events.arg_names is
    'Argument NAMES only, never values. Sanitised by the server before it is sent.';
comment on column public.usage_events.requested_name is
    'The tool name a caller asked for when it is not one of ours (sanitised); NULL otherwise.';

-- Failures are the rows somebody will look for; successes are the bulk. A partial index keeps that
-- query cheap without taxing the hot insert path.
create index if not exists idx_usage_events_failures
    on public.usage_events (ts desc)
    where outcome is not null and outcome <> 'ok';

-- ----------------------------------------------------------------------------
-- 2. usage_events_insert_v2 - a NEW name, so the live 7-argument function is never ambiguous
--    with it (PostgREST resolves overloads by argument names and a defaulted superset would
--    collide with the old signature: PGRST203).
-- ----------------------------------------------------------------------------
create or replace function public.usage_events_insert_v2(
    p_tool           text,
    p_args_hash      text,
    p_ip_hash        text,
    p_user_agent     text,
    p_key_id         text,
    p_session_kind   text,
    p_method         text,
    p_outcome        text    default null,
    p_error_code     text    default null,
    p_http_status    integer default null,
    p_latency_ms     integer default null,
    p_client_name    text    default null,
    p_client_version text    default null,
    p_key_state      text    default null,
    p_arg_names      text[]  default null,
    p_requested_name text    default null,
    p_detail         text    default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id bigint;
    v_names text[];
begin
    if p_session_kind not in ('crawler', 'anon_agent', 'verified_agent_key', 'verified_human_key') then
        raise exception 'usage_events_insert_v2: invalid session_kind %', p_session_kind
            using errcode = '22023';
    end if;
    if p_outcome is not null and p_outcome not in
       ('ok', 'tool_failure', 'tool_error', 'rpc_error', 'exception', 'http_error') then
        raise exception 'usage_events_insert_v2: invalid outcome %', p_outcome
            using errcode = '22023';
    end if;
    if p_key_state is not null and p_key_state not in
       ('none', 'valid', 'invalid', 'expired', 'placeholder') then
        raise exception 'usage_events_insert_v2: invalid key_state %', p_key_state
            using errcode = '22023';
    end if;

    if p_arg_names is not null then
        select coalesce(array_agg(left(x, 64) order by o), '{}')
          into v_names
          from unnest(p_arg_names) with ordinality as t(x, o)
         where o <= 40;
    end if;

    insert into usage_events (
        ts, tool, args_hash, ip_hash, user_agent, key_id, session_kind, method,
        outcome, error_code, http_status, latency_ms, client_name, client_version,
        key_state, arg_names, requested_name, detail
    ) values (
        now(), left(p_tool, 128), p_args_hash, p_ip_hash, left(p_user_agent, 512),
        left(p_key_id, 64), p_session_kind, left(p_method, 64),
        p_outcome, left(p_error_code, 64), p_http_status, p_latency_ms,
        left(p_client_name, 128), left(p_client_version, 64),
        p_key_state, v_names, left(p_requested_name, 64), left(p_detail, 300)
    )
    returning id into v_id;

    return jsonb_build_object('id', v_id);
end;
$$;

revoke all on function public.usage_events_insert_v2(
    text, text, text, text, text, text, text, text, text, integer, integer, text, text, text, text[], text, text
) from public, anon, authenticated;
grant execute on function public.usage_events_insert_v2(
    text, text, text, text, text, text, text, text, text, integer, integer, text, text, text, text[], text, text
) to anon, service_role;

-- ----------------------------------------------------------------------------
-- 3. compliance_audit_insert - the append-only audit mirror (compliance/audit_log.py).
-- ----------------------------------------------------------------------------
revoke all on public.compliance_audit from anon, authenticated, public;
alter table public.compliance_audit enable row level security;   -- already on; idempotent

create or replace function public.compliance_audit_insert(
    p_audit_id          text,
    p_event_type        text,
    p_ts                timestamptz,
    p_agent_id          text,
    p_principal_kind    text,
    p_principal_id      text,
    p_operation         text,
    p_smb_id            text,
    p_recipient_id_hash text,
    p_channel           text,
    p_use_case          text,
    p_jurisdiction      text,
    p_decision          text,
    p_reason            text,
    p_token_hash        text,
    p_trace_id          text,
    p_metadata          jsonb default '{}'::jsonb
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_n integer;
begin
    if coalesce(btrim(p_audit_id), '') = '' then
        raise exception 'compliance_audit_insert: audit_id is required' using errcode = '22023';
    end if;
    insert into compliance_audit (
        audit_id, event_type, ts, agent_id, principal_kind, principal_id, operation, smb_id,
        recipient_id_hash, channel, use_case, jurisdiction, decision, reason, token_hash,
        trace_id, metadata
    ) values (
        left(p_audit_id, 128), left(p_event_type, 64), coalesce(p_ts, now()), left(p_agent_id, 128),
        left(p_principal_kind, 64), left(p_principal_id, 128), left(p_operation, 128),
        left(p_smb_id, 128), left(p_recipient_id_hash, 128), left(p_channel, 32),
        left(p_use_case, 64), left(p_jurisdiction, 32), left(p_decision, 64), left(p_reason, 2000),
        left(p_token_hash, 128), left(p_trace_id, 128), coalesce(p_metadata, '{}'::jsonb)
    )
    on conflict (audit_id) do nothing;
    get diagnostics v_n = row_count;
    return jsonb_build_object('inserted', v_n = 1);
end;
$$;

revoke all on function public.compliance_audit_insert(
    text, text, timestamptz, text, text, text, text, text, text, text, text, text, text, text, text, text, jsonb
) from public, anon, authenticated;
grant execute on function public.compliance_audit_insert(
    text, text, timestamptz, text, text, text, text, text, text, text, text, text, text, text, text, text, jsonb
) to anon, service_role;

-- ----------------------------------------------------------------------------
-- 4. pending_keys - the email-verification handshake (agent_interface/key_request_logic.py).
--    The table holds verification tokens and machine-minted keys, so no function here ever
--    RETURNS a token: consume deletes and reports only whether a row was there and its email.
-- ----------------------------------------------------------------------------
revoke all on public.pending_keys from anon, authenticated, public;
alter table public.pending_keys enable row level security;       -- already on; idempotent

create or replace function public.pending_keys_upsert(
    p_email      text,
    p_token      text,
    p_expires_at timestamptz,
    p_created_at timestamptz default null,
    p_source     text        default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
begin
    if coalesce(btrim(p_email), '') = '' or coalesce(p_token, '') = '' then
        raise exception 'pending_keys_upsert: email and token are required' using errcode = '22023';
    end if;
    insert into pending_keys (email, token, expires_at, created_at, source)
    values (left(p_email, 320), left(p_token, 2048), p_expires_at, coalesce(p_created_at, now()), left(p_source, 64))
    on conflict (email) do update
        set token      = excluded.token,
            expires_at = excluded.expires_at,
            created_at = excluded.created_at,
            source     = coalesce(excluded.source, pending_keys.source);
    return jsonb_build_object('stored', true);
end;
$$;

revoke all on function public.pending_keys_upsert(text, text, timestamptz, timestamptz, text)
    from public, anon, authenticated;
grant execute on function public.pending_keys_upsert(text, text, timestamptz, timestamptz, text)
    to anon, service_role;

-- Atomic "spend this verification": DELETE ... RETURNING, so two concurrent clicks on one link
-- cannot both succeed (the old select-then-delete could, and its failed delete was only logged).
-- `found` is authoritative: this runs as the table owner, so an empty answer means no row, never
-- "a row you may not see".
create or replace function public.pending_keys_consume(
    p_email text default null,
    p_token text default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_email text;
begin
    if coalesce(btrim(p_email), '') <> '' then
        delete from pending_keys where email = left(p_email, 320) returning email into v_email;
    elsif coalesce(p_token, '') <> '' then
        delete from pending_keys where token = left(p_token, 2048) returning email into v_email;
    else
        raise exception 'pending_keys_consume: email or token is required' using errcode = '22023';
    end if;
    return jsonb_build_object('found', v_email is not null, 'email', v_email);
end;
$$;

revoke all on function public.pending_keys_consume(text, text) from public, anon, authenticated;
grant execute on function public.pending_keys_consume(text, text) to anon, service_role;

-- ----------------------------------------------------------------------------
-- 5. consent_optouts - hydrate at start (read) and record a STOP (write).
-- ----------------------------------------------------------------------------
revoke all on public.consent_optouts from anon, authenticated, public;   -- already none; idempotent
alter table public.consent_optouts enable row level security;

-- Paged and ordered so a caller can read the whole list and never an arbitrary slice.
create or replace function public.consent_optouts_hydrate(
    p_limit  integer default 1000,
    p_offset integer default 0
) returns table (recipient_id text, channel text)
language sql
stable
security definer
set search_path = public
as $$
    select o.recipient_id, o.channel
      from consent_optouts o
     where o.recipient_id is not null and o.channel is not null
     order by o.created_at desc, o.id
     limit greatest(1, least(coalesce(p_limit, 1000), 1000))
    offset greatest(0, coalesce(p_offset, 0));
$$;

revoke all on function public.consent_optouts_hydrate(integer, integer) from public, anon, authenticated;
grant execute on function public.consent_optouts_hydrate(integer, integer) to anon, service_role;

-- Idempotent on (recipient_id, channel): a repeated STOP does not pile up duplicate rows.
create or replace function public.consent_optouts_record(
    p_recipient_id      text,
    p_channel           text,
    p_use_case          text        default 'marketing',
    p_revocation_method text        default null,
    p_source            text        default null,
    p_created_at        timestamptz default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_recipient text := left(btrim(coalesce(p_recipient_id, '')), 320);
    v_channel   text := left(btrim(coalesce(p_channel, '')), 32);
begin
    if v_recipient = '' or v_channel = '' then
        raise exception 'consent_optouts_record: recipient_id and channel are required'
            using errcode = '22023';
    end if;
    if exists (select 1 from consent_optouts where recipient_id = v_recipient and channel = v_channel) then
        return jsonb_build_object('recorded', true, 'already_present', true);
    end if;
    insert into consent_optouts (recipient_id, channel, use_case, revocation_method, source, created_at)
    values (v_recipient, v_channel, left(p_use_case, 64), left(p_revocation_method, 64),
            left(p_source, 64), coalesce(p_created_at, now()));
    return jsonb_build_object('recorded', true, 'already_present', false);
end;
$$;

revoke all on function public.consent_optouts_record(text, text, text, text, text, timestamptz)
    from public, anon, authenticated;
grant execute on function public.consent_optouts_record(text, text, text, text, text, timestamptz)
    to anon, service_role;

-- ----------------------------------------------------------------------------
-- VERIFY AFTER APPLYING (scripts/verify_spine_009.py runs all of this, and the live boundary
-- through the public door with Prefer: tx=rollback):
--
--   select p.proname, p.prosecdef, r.rolname, r.rolbypassrls
--     from pg_proc p join pg_roles r on r.oid = p.proowner
--    where p.proname in ('usage_events_insert_v2','compliance_audit_insert','pending_keys_upsert',
--                        'pending_keys_consume','consent_optouts_hydrate','consent_optouts_record');
--   -- expect prosecdef = true, owner spine_owner, rolbypassrls = true for all six.
--
--   select table_name, grantee, privilege_type from information_schema.role_table_grants
--    where table_schema='public' and table_name in ('compliance_audit','pending_keys','consent_optouts')
--      and grantee in ('anon','authenticated','public');
--   -- expect ZERO rows.
-- ----------------------------------------------------------------------------
