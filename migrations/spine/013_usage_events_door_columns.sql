-- AgentBroker on the spine, 2026-10-04: every usage row says WHICH DOOR it came through, under WHICH PROTOCOL
-- VERSION, and how many RESULTS it handed back.   (Verdict item A7, docs/reviews/2026-10-03-mcp-focus-verdict.md:
-- "instrument every door - without it we cannot see the first buyer".)
--
-- Applies to the PostgreSQL database behind AgentBroker's PostgREST endpoint (the "spine"), run as `spine_owner`,
-- the role that owns every object there (it bypasses row-level security). The operator's own apply tool runs it
-- (scripts/apply_sql.py: one transaction, SUPABASE_DB_URL through the spine tunnel) BEFORE the code that uses it
-- is deployed - the same order migrations 009, 010 and 011 were applied in.
--
-- ADDITIVE AND IDEMPOTENT. Nothing existing is dropped, renamed or replaced: three nullable columns on
-- usage_events and ONE NEW FUNCTION, `usage_events_insert_v3`. `usage_events_insert` and
-- `usage_events_insert_v2` are not touched, so the container image that is live today (and any older one) keeps
-- writing exactly what it writes now, and rolling the code back needs no database change. Every CREATE is
-- `or replace` / `if not exists`, every REVOKE/GRANT is safe to repeat, the one UPDATE only fills NULLs.
--
-- WHY A NEW NAME AND NOT A BIGGER v2. PostgREST resolves an overloaded function by the names of the arguments it
-- is sent. A v2 with three more defaulted parameters, next to the 17-parameter v2 that is live, would answer
-- every call with PGRST203 ("could not choose the best candidate"); dropping the live v2 to avoid that would
-- break the running image for as long as it took the code to move. Migration 009 made the same choice for the
-- same reason when it added v2 beside v1.
--
-- WHAT THE THREE COLUMNS ARE
--   door              Which entrance the request used. `agent-broker` = the full server (/mcp and
--                     /mcp/agent-broker, which are one door); one of the capability doors by name
--                     (`sanctions-screening`, `compliance-check`, ...); `retired:<slug>` = one of the six
--                     retired servers, answered with a tombstone; `unknown` = a path under /mcp that is not a
--                     door. NULL = the request never reached an MCP door (a page, a REST route), or the row was
--                     written before this migration and its detail text names no door. Set by the SERVER from
--                     the route, never from anything the caller sent.
--   protocol_version  The MCP revision the exchange ran under, and only ever one of the versions we speak:
--                     the `_meta` declaration or the MCP-Protocol-Version header on a request, the negotiated
--                     version on `initialize`. NULL = the request did not state one (a stateless server cannot
--                     know what an earlier handshake agreed), or it stated one we do not speak.
--   result_count      How many items the call's principal list held: businesses (find_business), matches
--                     (screen_sanctions), restrictions (map_trade_restriction), awards (lookup_us_contracts),
--                     or the tools/resources/prompts a list method returned. NULL = the call failed, or has no
--                     list worth counting. Never a value from the result, only how many.
--
-- WHAT IS DELIBERATELY NOT STORED. No caller text of any kind reaches these columns: the door is one of our own
-- labels, the version is checked against our own list before it is sent, the count is a number. The function
-- refuses anything else (22023), and the container sends NULL rather than a value it cannot vouch for, because a
-- refused call loses the whole row.
--
-- HISTORY. Rows written before this migration keep NULL, except that the old free-text `detail` ('door=<name>',
-- 'pv=<version>', written by the images before this one) is copied into the new columns when it is exactly of that
-- shape. Nothing is inferred: a row whose detail names no door stays NULL. Rows the previous image writes between
-- this migration and the deploy of the new code get the same treatment by running this file once more after the
-- deploy (it is idempotent).
--
-- LEAST PRIVILEGE (same as 009/010/011). The function is SECURITY DEFINER with a pinned search_path and starts
-- from `revoke all ... from public, anon, authenticated`; only `anon` (the credential the container holds) and
-- `service_role` may execute it. The table stays closed to anon/authenticated/public.
--
-- OPERATING INVARIANT (as 009): safe only while the database anon credential is a secret held by the service and
-- its operator. A forged call can add a nuisance analytics row with a well-formed door label; it cannot read a
-- row, change one, or move money.
--
-- OWNERSHIP. A function created by a different role than `spine_owner` would be a SECURITY DEFINER that runs as
-- that role - on a superuser it would be a superuser-owned function callable by `anon`. docs/supabase-to-vps-
-- cutover.md says so in one line ("do not run migrations as the techmate superuser without SET ROLE spine_owner
-- first"); the check below makes the file refuse instead of relying on someone having read it.

set local lock_timeout = '10s';     -- a long analytics query must not make the ALTER queue every insert behind it

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'spine_owner') and current_user <> 'spine_owner' then
        raise exception '013_usage_events_door_columns: run this as spine_owner (SET ROLE spine_owner first); current_user is %',
            current_user using errcode = '42501';
    end if;
end
$$;

-- ----------------------------------------------------------------------------
-- 1. The columns (all nullable, no default: a metadata-only change, old rows and old writers untouched)
-- ----------------------------------------------------------------------------
alter table public.usage_events
    add column if not exists door             text,
    add column if not exists protocol_version text,
    add column if not exists result_count     integer;

comment on column public.usage_events.door is
    'Which entrance: agent-broker (full server) | a capability door name | retired:<slug> | unknown. NULL = not an MCP door, or written before 013 with no door in detail. Set by the server from the route.';
comment on column public.usage_events.protocol_version is
    'MCP revision the exchange ran under (one of the versions we speak): _meta declaration, MCP-Protocol-Version header, or the version negotiated by initialize. NULL = not stated.';
comment on column public.usage_events.result_count is
    'How many items the principal list of the result held (businesses, matches, restrictions, awards; tools/resources/prompts for list methods). NULL = failed, or no list.';

-- ----------------------------------------------------------------------------
-- 2. usage_events_insert_v3: v2 plus the three. Same checks, same clamps, same owner and grants.
-- ----------------------------------------------------------------------------
create or replace function public.usage_events_insert_v3(
    p_tool             text,
    p_args_hash        text,
    p_ip_hash          text,
    p_user_agent       text,
    p_key_id           text,
    p_session_kind     text,
    p_method           text,
    p_outcome          text    default null,
    p_error_code       text    default null,
    p_http_status      integer default null,
    p_latency_ms       integer default null,
    p_client_name      text    default null,
    p_client_version   text    default null,
    p_key_state        text    default null,
    p_arg_names        text[]  default null,
    p_requested_name   text    default null,
    p_detail           text    default null,
    p_door             text    default null,
    p_protocol_version text    default null,
    p_result_count     integer default null
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
        raise exception 'usage_events_insert_v3: invalid session_kind %', p_session_kind
            using errcode = '22023';
    end if;
    if p_outcome is not null and p_outcome not in
       ('ok', 'tool_failure', 'tool_error', 'rpc_error', 'exception', 'http_error', 'notification') then
        raise exception 'usage_events_insert_v3: invalid outcome %', p_outcome
            using errcode = '22023';
    end if;
    if p_key_state is not null and p_key_state not in
       ('none', 'valid', 'invalid', 'expired', 'placeholder') then
        raise exception 'usage_events_insert_v3: invalid key_state %', p_key_state
            using errcode = '22023';
    end if;
    -- The three new fields are refused, not clamped: a door that is not one of our labels is a bug to be seen,
    -- and a clamped value would be a plausible-looking wrong one. The value itself is not echoed.
    if p_door is not null and p_door !~ '^(retired:)?[a-z0-9][a-z0-9._-]{0,62}$' then
        raise exception 'usage_events_insert_v3: invalid door' using errcode = '22023';
    end if;
    if p_protocol_version is not null and p_protocol_version !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then
        raise exception 'usage_events_insert_v3: invalid protocol_version' using errcode = '22023';
    end if;
    if p_result_count is not null and (p_result_count < 0 or p_result_count > 1000000) then
        raise exception 'usage_events_insert_v3: invalid result_count' using errcode = '22023';
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
        key_state, arg_names, requested_name, detail,
        door, protocol_version, result_count
    ) values (
        now(), left(p_tool, 128), p_args_hash, p_ip_hash, left(p_user_agent, 512),
        left(p_key_id, 64), p_session_kind, left(p_method, 64),
        p_outcome, left(p_error_code, 64), p_http_status, p_latency_ms,
        left(p_client_name, 128), left(p_client_version, 64),
        p_key_state, v_names, left(p_requested_name, 64), left(p_detail, 300),
        p_door, p_protocol_version, p_result_count
    )
    returning id into v_id;

    return jsonb_build_object('id', v_id);
end;
$$;

-- New functions in `public` are granted to anon/authenticated by default on this cluster (default privileges
-- inherited from the platform the spine was restored from), and to PUBLIC by Postgres itself. Start from nothing.
revoke all on function public.usage_events_insert_v3(
    text, text, text, text, text, text, text, text, text, integer, integer, text, text, text, text[], text, text,
    text, text, integer
) from public, anon, authenticated;
grant execute on function public.usage_events_insert_v3(
    text, text, text, text, text, text, text, text, text, integer, integer, text, text, text, text[], text, text,
    text, text, integer
) to anon, service_role;

comment on function public.usage_events_insert_v3(
    text, text, text, text, text, text, text, text, text, integer, integer, text, text, text, text[], text, text,
    text, text, integer
) is 'AgentBroker usage row writer: v2 plus door, protocol_version, result_count (migration 013). v1 and v2 are unchanged.';

-- ----------------------------------------------------------------------------
-- 3. History: copy the old free-text detail into the columns, only where it is exactly of the shape the server
--    wrote and only where the column is still empty. Nothing is inferred from anything else.
-- ----------------------------------------------------------------------------
update public.usage_events
   set door = substring(detail from '(?:^| )door=([a-z0-9][a-z0-9._-]{0,62})(?: |$)')
 where door is null
   and detail like '%door=%';

update public.usage_events
   set protocol_version = substring(detail from '(?:^| )pv=([0-9]{4}-[0-9]{2}-[0-9]{2})(?: |$)')
 where protocol_version is null
   and detail like '%pv=%';
