-- AgentBroker on the spine, 2026-10-01: usage_events accepts outcome = 'notification'.
--
-- WHY: a JSON-RPC message without an id (notifications/initialized, sent by every MCP client right
-- after `initialize`) is accepted with HTTP 202 and is not an error. The server now records it as
-- outcome 'notification'. usage_events_insert_v2 validates the outcome against a fixed list, so
-- without this change every such row would be rejected by the function and lost.
--
-- ORDER: apply this BEFORE deploying the code that sends it. It only widens what is accepted, so
-- the image already live (and any older one) is unaffected.
--
-- Idempotent. Same signature, owner and grants as migration 009 (CREATE OR REPLACE keeps them; the
-- grants are repeated so a fresh database ends in the same state).

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
       ('ok', 'tool_failure', 'tool_error', 'rpc_error', 'exception', 'http_error', 'notification') then
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

comment on column public.usage_events.outcome is
    'ok | tool_failure | tool_error | rpc_error | exception | http_error | notification. '
    'notification = a JSON-RPC message with no id (or a client response), accepted with HTTP 202 - not an error. '
    'NULL on rows written before 2026-10-01.';

-- The "failures" index is for rows somebody goes looking for. A handshake row per connecting client
-- is not one of them, so it stays out.
drop index if exists public.idx_usage_events_failures;
create index if not exists idx_usage_events_failures
    on public.usage_events (ts desc)
    where outcome is not null and outcome not in ('ok', 'notification');
