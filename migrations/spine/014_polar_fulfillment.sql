-- Durable, privately authorized Polar fulfillment. Apply as spine_owner after
-- the operator provisions the NOLOGIN polar_fulfillment role and scoped JWT.
-- No payment switch is changed. Rollback code may leave these additive objects.
-- Receipts contain identifiers/digests and frozen issuance claims, never a raw
-- email, credential, provider body or error.
-- notification_pending records possible undelivered notification intent only.
-- No delivery worker/acknowledgment is provided; provider retries are bounded.
-- Refund is terminal access revocation, not a monetary/credit-ledger reversal.
begin;
set local lock_timeout = '3s';
do $$
begin
  if current_user <> 'spine_owner' or not exists
      (select 1 from pg_catalog.pg_roles where rolname = current_user and rolbypassrls) then
    raise exception 'fulfillment migration requires spine_owner' using errcode = '42501';
  end if;
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'polar_fulfillment'
      and not rolcanlogin and not rolinherit and not rolsuper and not rolcreatedb
      and not rolcreaterole and not rolreplication and not rolbypassrls) then
    raise exception 'scoped fulfillment role not provisioned safely' using errcode = '42501';
  end if;
  if exists (select 1 from pg_catalog.pg_auth_members m join pg_catalog.pg_roles r
      on r.oid = m.member where r.rolname = 'polar_fulfillment') then
    raise exception 'scoped fulfillment role must not inherit another role' using errcode = '42501';
  end if;
end $$;

create table if not exists public.polar_fulfillment_orders (
  order_id text primary key,
  customer_id text not null,
  account_id text,
  product_id text,
  credits bigint,
  plan text,
  email_hash text,
  issued_at bigint,
  token_id uuid,
  issuance_version smallint,
  entitlements jsonb,
  status text not null check (status in ('ready','claimed','complete','refunded')),
  lease_owner uuid,
  lease_until timestamptz,
  fence bigint not null default 0,
  notification_pending boolean not null default false,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  check (status = 'refunded' or (account_id is not null and product_id is not null
    and credits is not null and credits > 0 and plan is not null and email_hash is not null
    and email_hash ~ '^[0-9a-f]{64}$' and issued_at is not null and issued_at > 0
    and token_id is not null and issuance_version is not null and issuance_version = 1 and entitlements is not null))
);
alter table public.polar_fulfillment_orders enable row level security;
revoke all on public.polar_fulfillment_orders from public, anon, authenticated, service_role, polar_fulfillment;
grant usage on schema public to polar_fulfillment;

create or replace function public.polar_fulfillment_claim(
  p_order_id text, p_customer_id text, p_account_id text, p_product_id text,
  p_credits bigint, p_plan text, p_email_hash text, p_owner text
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $$
declare r public.polar_fulfillment_orders%rowtype; v_owner uuid; v_ent jsonb;
begin
  if coalesce(p_order_id,'') !~ '^[A-Za-z0-9_.:-]{1,120}$'
    or coalesce(p_customer_id,'') !~ '^[A-Za-z0-9_.:-]{1,120}$'
    or p_account_id is distinct from 'sub_' || p_customer_id
    or coalesce(p_product_id,'') !~ '^[A-Za-z0-9_.:-]{1,120}$'
    or p_credits is null or p_credits < 1 or p_credits > 100000000
    or coalesce(p_plan,'') not in ('developer','business','enterprise')
    or coalesce(p_email_hash,'') !~ '^[0-9a-f]{64}$'
    or coalesce(p_owner,'') !~ '^[0-9a-fA-F-]{36}$' then
    raise exception 'invalid fulfillment binding' using errcode = '22023';
  end if;
  v_owner := p_owner::uuid;
  -- The same customer lock is taken by claim, commit and refund. Customer-wide
  -- revocation therefore cannot pass between the final check and a grant.
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('polar/customer/' || p_customer_id, 0));
  v_ent := jsonb_build_object('operations',jsonb_build_array('*'),'verticals',jsonb_build_array('*'),
    'budget_cap_usd',case p_plan when 'business' then 5000 when 'enterprise' then 25000 else 500 end,
    'ttl_seconds',case p_plan when 'enterprise' then 31536000 else 7776000 end);
  insert into public.polar_fulfillment_orders(order_id,customer_id,account_id,product_id,credits,plan,
      email_hash,issued_at,token_id,issuance_version,entitlements,status)
    values(p_order_id,p_customer_id,p_account_id,p_product_id,p_credits,p_plan,p_email_hash,
      floor(extract(epoch from clock_timestamp()))::bigint,gen_random_uuid(),1,v_ent,'ready')
    on conflict(order_id) do nothing;
  select * into r from public.polar_fulfillment_orders where order_id=p_order_id for update;
  if r.customer_id is distinct from p_customer_id then
    return jsonb_build_object('ok',true,'status','conflict');
  end if;
  if r.status='refunded' or exists(select 1 from public.polar_order_events
      where customer_id=p_customer_id and status='revoked') then
    update public.polar_fulfillment_orders set status='refunded',lease_owner=null,lease_until=null,
      notification_pending=false,updated_at=clock_timestamp() where order_id=p_order_id;
    return jsonb_build_object('ok',true,'status','refunded');
  end if;
  if r.account_id is distinct from p_account_id or r.product_id is distinct from p_product_id
    or r.credits is distinct from p_credits or r.plan is distinct from p_plan or r.email_hash is distinct from p_email_hash then
    return jsonb_build_object('ok',true,'status','conflict');
  end if;
  if r.status='complete' then
    return jsonb_build_object('ok',true,'status','complete','issued_at',r.issued_at,'token_id',r.token_id,
      'fence',r.fence,'issuance_version',r.issuance_version,'entitlements',r.entitlements);
  end if;
  if r.status='claimed' and r.lease_until > clock_timestamp() then
    return jsonb_build_object('ok',true,'status','in_progress');
  end if;
  update public.polar_fulfillment_orders set status='claimed',lease_owner=v_owner,
    lease_until=clock_timestamp()+interval '30 seconds',fence=fence+1,updated_at=clock_timestamp()
    where order_id=p_order_id returning * into r;
  return jsonb_build_object('ok',true,'status','claimed','issued_at',r.issued_at,'token_id',r.token_id,
    'fence',r.fence,'issuance_version',r.issuance_version,'entitlements',r.entitlements);
end $$;

create or replace function public.polar_fulfillment_complete(p_order_id text,p_owner text,p_fence bigint)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, pg_temp as $$
declare r public.polar_fulfillment_orders%rowtype; v_customer text; l public.credit_ledger%rowtype;
  a public.oauth_account_links%rowtype; v_grant jsonb;
begin
  select customer_id into v_customer from public.polar_fulfillment_orders where order_id=p_order_id;
  if not found then raise exception 'fulfillment lease unavailable' using errcode='55000'; end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('polar/customer/' || v_customer,0));
  select * into r from public.polar_fulfillment_orders where order_id=p_order_id for update;
  if r.status='refunded' or exists(select 1 from public.polar_order_events where customer_id=r.customer_id and status='revoked') then
    update public.polar_fulfillment_orders set status='refunded',lease_owner=null,lease_until=null,
      notification_pending=false,updated_at=clock_timestamp() where order_id=p_order_id;
    return jsonb_build_object('ok',true,'status','refunded');
  end if;
  -- Check the fence before even an already-complete response: a stale worker
  -- must not report fulfillment as if it still owned a reclaimed lease.
  if r.fence is distinct from p_fence or r.lease_owner is distinct from p_owner::uuid then
    raise exception 'fulfillment lease unavailable' using errcode='55000';
  end if;
  if r.status <> 'complete' and (r.status <> 'claimed' or r.lease_until <= clock_timestamp()) then
    raise exception 'fulfillment lease unavailable' using errcode='55000';
  end if;
  if r.status <> 'complete' then
    -- Existing ledger keys are global. Prove that an older grant using this
    -- order key funded this exact account/amount; ok/idempotent alone is not proof.
    select * into l from public.credit_ledger where idempotency_key=p_order_id for update;
    if found and (l.account_id is distinct from r.account_id or l.amount_credits is distinct from r.credits
       or l.source is distinct from 'polar' or l.order_id is distinct from p_order_id or l.entry_type is distinct from 'topup') then
      raise exception 'fulfillment ledger conflict' using errcode='23514';
    end if;
    v_grant := public.credit_grant(r.account_id,r.credits,'polar',p_order_id,p_order_id);
    select * into l from public.credit_ledger where idempotency_key=p_order_id for update;
    if not found or v_grant->>'ok' is distinct from 'true' or l.account_id is distinct from r.account_id
       or l.amount_credits is distinct from r.credits or l.source is distinct from 'polar'
       or l.order_id is distinct from p_order_id or l.entry_type is distinct from 'topup' then
      raise exception 'fulfillment ledger conflict' using errcode='23514';
    end if;
    insert into public.oauth_account_links(email_hash,account_id,customer_id,plan)
      values(r.email_hash,r.account_id,r.customer_id,r.plan) on conflict(email_hash) do nothing;
    select * into a from public.oauth_account_links where email_hash=r.email_hash for update;
    -- Preserve first-writer account linkage. A different plan requires an
    -- explicit upgrade policy rather than falsely reporting full delivery.
    if not found or a.account_id is distinct from r.account_id or a.customer_id is distinct from r.customer_id
        or a.plan is distinct from r.plan then
      raise exception 'fulfillment account link conflict' using errcode='23514';
    end if;
    update public.polar_fulfillment_orders set status='complete',notification_pending=true,
      updated_at=clock_timestamp() where order_id=p_order_id returning * into r;
    if not exists(select 1 from public.polar_order_events where order_id=p_order_id and status='processed') then
      insert into public.polar_order_events(order_id,event_type,customer_id,status,ts)
        values(p_order_id,'order.paid',r.customer_id,'processed',clock_timestamp());
    end if;
  end if;
  return jsonb_build_object('ok',true,'status','complete','issued_at',r.issued_at,'token_id',r.token_id,
    'fence',r.fence,'issuance_version',r.issuance_version,'entitlements',r.entitlements);
end $$;

create or replace function public.polar_fulfillment_release(p_order_id text,p_owner text,p_fence bigint)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, pg_temp as $$
declare r public.polar_fulfillment_orders%rowtype;
begin
  select * into r from public.polar_fulfillment_orders where order_id=p_order_id for update;
  if not found then return jsonb_build_object('ok',true,'status','in_progress'); end if;
  if r.status in ('complete','refunded') then return jsonb_build_object('ok',true,'status',r.status); end if;
  if r.status='claimed' and r.lease_owner=p_owner::uuid and r.fence=p_fence then
    update public.polar_fulfillment_orders set status='ready',lease_owner=null,lease_until=null,
      updated_at=clock_timestamp() where order_id=p_order_id;
    return jsonb_build_object('ok',true,'status','ready');
  end if;
  return jsonb_build_object('ok',true,'status','in_progress');
end $$;

create or replace function public.polar_fulfillment_refund(p_order_id text,p_customer_id text)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, pg_temp as $$
declare r public.polar_fulfillment_orders%rowtype;
begin
  if coalesce(p_order_id,'') !~ '^[A-Za-z0-9_.:-]{1,120}$'
      or coalesce(p_customer_id,'') !~ '^[A-Za-z0-9_.:-]{1,120}$' then
    raise exception 'invalid fulfillment binding' using errcode='22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('polar/customer/' || p_customer_id,0));
  insert into public.polar_fulfillment_orders(order_id,customer_id,status)
    values(p_order_id,p_customer_id,'refunded') on conflict(order_id) do nothing;
  select * into r from public.polar_fulfillment_orders where order_id=p_order_id for update;
  if r.customer_id is distinct from p_customer_id then
    raise exception 'fulfillment refund binding conflict' using errcode='23514';
  end if;
  update public.polar_fulfillment_orders set status='refunded',lease_owner=null,lease_until=null,
    notification_pending=false,updated_at=clock_timestamp() where customer_id=p_customer_id;
  if not exists(select 1 from public.polar_order_events where customer_id=p_customer_id and status='revoked') then
    insert into public.polar_order_events(order_id,event_type,customer_id,status,ts)
      values(p_order_id,'order.refunded',p_customer_id,'revoked',clock_timestamp());
  end if;
  return jsonb_build_object('ok',true,'status','refunded');
end $$;

revoke all on function public.polar_fulfillment_claim(text,text,text,text,bigint,text,text,text) from public,anon,authenticated,service_role;
revoke all on function public.polar_fulfillment_complete(text,text,bigint) from public,anon,authenticated,service_role;
revoke all on function public.polar_fulfillment_release(text,text,bigint) from public,anon,authenticated,service_role;
revoke all on function public.polar_fulfillment_refund(text,text) from public,anon,authenticated,service_role;
grant execute on function public.polar_fulfillment_claim(text,text,text,text,bigint,text,text,text) to polar_fulfillment;
grant execute on function public.polar_fulfillment_complete(text,text,bigint) to polar_fulfillment;
grant execute on function public.polar_fulfillment_release(text,text,bigint) to polar_fulfillment;
grant execute on function public.polar_fulfillment_refund(text,text) to polar_fulfillment;
commit;
