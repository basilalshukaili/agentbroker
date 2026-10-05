"""Real PostgreSQL proof of private fulfillment, never a production DSN.

SPINE_PG_TESTS=1 starts the already-local postgres image using tests.spine_pg.
All credentials/ports are random, loopback only, and the container is removed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")
from tests import spine_pg

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "migrations/spine/014_polar_fulfillment.sql"
WHY = spine_pg.docker_unavailable_reason()
pytestmark = pytest.mark.skipif(bool(WHY), reason=WHY or "ok")


@pytest.fixture(scope="module")
def cluster():
    with spine_pg.start_spine() as s:
        spine_pg.run_sql_sync(s.admin(), "create role polar_fulfillment nologin noinherit;")
        spine_pg.run_sql_sync(s.admin(), "create database polar_test owner spine_owner;")
        spine_pg.run_sql_sync(s.admin("polar_test"), spine_pg.DATABASE_SQL)
        spine_pg.run_sql_sync(s.owner("polar_test"), (ROOT / "migrations/credits_billing.sql").read_text())
        spine_pg.run_sql_sync(s.owner("polar_test"), (ROOT / "migrations/spine/011_oauth_connect.sql").read_text())
        spine_pg.run_sql_sync(s.owner("polar_test"), """
          create table public.polar_order_events(id bigserial primary key,order_id text,
            event_type text,customer_id text,status text,ts timestamptz default now());
          alter table public.polar_order_events enable row level security;
          revoke all on function public.credit_grant(text,bigint,text,text,text) from public,anon,authenticated;
          revoke all on function public.credit_reserve(text,bigint,text,text,text) from public,anon,authenticated;
          revoke all on function public.credit_commit(text,bigint) from public,anon,authenticated;
          revoke all on function public.credit_release(text,text) from public,anon,authenticated;
        """)
        spine_pg.run_sql_sync(s.owner("polar_test"), SQL.read_text())
        spine_pg.run_sql_sync(s.owner("polar_test"), SQL.read_text())
        yield s


async def rpc(cluster, action, *, role="polar_fulfillment", connection=None, **payload):
    conn = connection or await asyncpg.connect(cluster.admin("polar_test"))
    try:
        await conn.execute("set role " + role)
        args = ",".join(f"{name} := ${i+1}" for i, name in enumerate(payload))
        result = await conn.fetchval(f"select public.polar_fulfillment_{action}({args})", *payload.values())
        return json.loads(result)
    finally:
        if connection is None:
            await conn.close()


def query(cluster, sql, *args):
    return asyncio.run(spine_pg.query(cluster.admin("polar_test"), sql, *args))


def binding():
    suffix = uuid.uuid4().hex
    return dict(p_order_id="order-"+suffix,p_customer_id="customer-"+suffix,
                p_account_id="sub_customer-"+suffix,p_product_id="product-growth",p_credits=3500,
                p_plan="developer",p_email_hash=hashlib.sha256(suffix.encode()).hexdigest(),
                p_owner=str(uuid.uuid4()))


def claim(cluster, args):
    return asyncio.run(rpc(cluster,"claim",**args))


def finish(cluster, args, receipt):
    return asyncio.run(rpc(cluster,"complete",p_order_id=args["p_order_id"],
                          p_owner=args["p_owner"],p_fence=receipt["fence"]))


def refund(cluster, args):
    return asyncio.run(rpc(cluster,"refund",p_order_id=args["p_order_id"],p_customer_id=args["p_customer_id"]))


def balance(cluster, args):
    rows=query(cluster,"select balance_credits from public.credit_accounts where account_id=$1",args["p_account_id"])
    return rows[0]["balance_credits"] if rows else 0


def test_catalog_exact_private_boundary_and_four_functions(cluster):
    rows=query(cluster,"""select p.proname,r.rolname,p.prosecdef,p.proconfig,
      has_function_privilege('anon',p.oid,'EXECUTE') as anon,
      has_function_privilege('authenticated',p.oid,'EXECUTE') as authenticated,
      has_function_privilege('polar_fulfillment',p.oid,'EXECUTE') as scoped
      from pg_proc p join pg_roles r on r.oid=p.proowner
      where p.proname like 'polar_fulfillment_%' order by p.proname""")
    assert len(rows)==4
    assert all(r["rolname"]=="spine_owner" and r["prosecdef"] and r["scoped"]
               and not r["anon"] and not r["authenticated"]
               and "search_path=pg_catalog, public, pg_temp" in r["proconfig"] for r in rows)
    rows=query(cluster,"""select has_table_privilege('polar_fulfillment','public.polar_fulfillment_orders','SELECT,INSERT,UPDATE,DELETE') as rights,
       has_function_privilege('polar_fulfillment','public.credit_grant(text,bigint,text,text,text)','EXECUTE') as grant_right,
       has_function_privilege('polar_fulfillment','public.credit_reserve(text,bigint,text,text,text)','EXECUTE') as reserve_right""")
    assert not any(rows[0].values())


@pytest.mark.parametrize("role",["anon","authenticated"])
def test_public_roles_cannot_invoke_fulfillment(cluster,role):
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        asyncio.run(rpc(cluster,"claim",role=role,**binding()))


def test_scoped_role_cannot_read_receipts_or_call_general_credit_api(cluster):
    async def drive():
        c=await asyncpg.connect(cluster.admin("polar_test"))
        try:
            await c.execute("set role polar_fulfillment")
            for sql in ["select count(*) from public.polar_fulfillment_orders",
                        "select public.credit_grant('forbidden',1,'polar','forbidden','forbidden')",
                        "select count(*) from public.credit_ledger"]:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await c.fetchval(sql)
        finally: await c.close()
    asyncio.run(drive())


def test_stable_lost_result_replay_grants_once_and_preserves_link(cluster):
    args=binding(); receipt=claim(cluster,args); completed=finish(cluster,args,receipt)
    # Model a committed response lost before the provider observed it.
    replay=claim(cluster,{**args,"p_owner":str(uuid.uuid4())})
    assert replay["status"]=="complete"
    for key in ["issued_at","token_id","issuance_version","entitlements"]:
        assert receipt[key]==completed[key]==replay[key]
    assert finish(cluster,args,receipt)["status"]=="complete"
    assert balance(cluster,args)==3500
    assert query(cluster,"select count(*) as n from public.credit_ledger where idempotency_key=$1",args["p_order_id"])[0]["n"]==1
    link=query(cluster,"select account_id,customer_id from public.oauth_account_links where email_hash=$1",args["p_email_hash"])[0]
    assert link["account_id"]==args["p_account_id"] and link["customer_id"]==args["p_customer_id"]
    receipt_row=query(cluster,"select notification_pending from public.polar_fulfillment_orders where order_id=$1",args["p_order_id"])[0]
    assert receipt_row["notification_pending"] is True


@pytest.mark.parametrize("field,value",[("p_customer_id","other"),("p_product_id","other"),
    ("p_credits",3501),("p_plan","business"),("p_email_hash","a"*64)])
def test_order_binding_cannot_change(cluster,field,value):
    args=binding(); claim(cluster,args)
    changed={**args,field:value,"p_owner":str(uuid.uuid4())}
    if field=="p_customer_id": changed["p_account_id"]="sub_other"
    assert claim(cluster,changed)["status"]=="conflict"
    assert balance(cluster,args)==0


def test_two_concurrent_claims_and_completions_only_one_grant(cluster):
    args=binding(); other={**args,"p_owner":str(uuid.uuid4())}
    async def drive():
        receipts=await asyncio.gather(rpc(cluster,"claim",**args),rpc(cluster,"claim",**other))
        assert sorted(r["status"] for r in receipts)==["claimed","in_progress"]
        i=next(i for i,r in enumerate(receipts) if r["status"]=="claimed")
        owner=(args,other)[i]
        kw=dict(p_order_id=args["p_order_id"],p_owner=owner["p_owner"],p_fence=receipts[i]["fence"])
        done=await asyncio.gather(rpc(cluster,"complete",**kw),rpc(cluster,"complete",**kw))
        assert all(r["status"]=="complete" for r in done)
    asyncio.run(drive()); assert balance(cluster,args)==3500


def test_lease_takeover_and_release_fence_reject_stale_worker(cluster):
    args=binding(); first=claim(cluster,args)
    query(cluster,"update public.polar_fulfillment_orders set lease_until=clock_timestamp()-interval '1 second' where order_id=$1 returning order_id",args["p_order_id"])
    other={**args,"p_owner":str(uuid.uuid4())}; second=claim(cluster,other)
    assert second["fence"]==first["fence"]+1
    assert second["token_id"]==first["token_id"]
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError): finish(cluster,args,first)
    released=asyncio.run(rpc(cluster,"release",p_order_id=args["p_order_id"],p_owner=args["p_owner"],p_fence=first["fence"]))
    assert released["status"]=="in_progress"
    assert finish(cluster,other,second)["status"]=="complete"
    assert balance(cluster,args)==3500


def test_expired_lease_cannot_complete_without_reclaim(cluster):
    args=binding(); receipt=claim(cluster,args)
    query(cluster,"update public.polar_fulfillment_orders set lease_until=clock_timestamp()-interval '1 second' where order_id=$1 returning order_id",args["p_order_id"])
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError): finish(cluster,args,receipt)
    assert balance(cluster,args)==0


def test_owner_release_allows_retry_with_same_issuance_and_new_fence(cluster):
    args=binding(); first=claim(cluster,args)
    remaining=query(cluster,"select extract(epoch from lease_until-clock_timestamp()) as seconds from public.polar_fulfillment_orders where order_id=$1",args["p_order_id"])[0]["seconds"]
    assert 25 < remaining <= 30
    released=asyncio.run(rpc(cluster,"release",p_order_id=args["p_order_id"],p_owner=args["p_owner"],p_fence=first["fence"]))
    assert released["status"]=="ready"
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError): finish(cluster,args,first)
    other={**args,"p_owner":str(uuid.uuid4())}; second=claim(cluster,other)
    assert second["token_id"]==first["token_id"] and second["issued_at"]==first["issued_at"]
    assert second["fence"]==first["fence"]+1
    assert finish(cluster,other,second)["status"]=="complete"


@pytest.mark.parametrize("field,value",[("p_order_id",""),("p_customer_id",""),
    ("p_account_id","sub_somebody_else"),("p_credits",0),("p_credits",-1),
    ("p_plan","unknown"),("p_email_hash","not-a-digest"),("p_owner","bad-owner")])
def test_invalid_binding_is_rejected_before_any_entitlement(cluster,field,value):
    args={**binding(),field:value}
    with pytest.raises(asyncpg.InvalidParameterValueError): claim(cluster,args)
    assert balance(cluster,args)==0


def test_matching_existing_ledger_grant_is_adopted_without_double_grant(cluster):
    args=binding()
    query(cluster,"select public.credit_grant($1,$2,'polar',$3,$3)",args["p_account_id"],args["p_credits"],args["p_order_id"])
    receipt=claim(cluster,args); finish(cluster,args,receipt)
    assert balance(cluster,args)==3500


def test_existing_ledger_conflict_never_credits_or_marks_complete(cluster):
    args=binding()
    query(cluster,"select public.credit_grant($1,1,'polar',$2,$2)","sub_wrong",args["p_order_id"])
    receipt=claim(cluster,args)
    with pytest.raises(asyncpg.CheckViolationError): finish(cluster,args,receipt)
    assert balance(cluster,args)==0


def test_account_link_conflict_rolls_back_grant_and_completion(cluster):
    args=binding(); receipt=claim(cluster,args)
    query(cluster,"insert into public.oauth_account_links(email_hash,account_id,customer_id,plan) values($1,'sub_other','other','developer') returning email_hash",args["p_email_hash"])
    with pytest.raises(asyncpg.CheckViolationError): finish(cluster,args,receipt)
    assert balance(cluster,args)==0
    assert query(cluster,"select count(*) as n from public.credit_ledger where idempotency_key=$1",args["p_order_id"])[0]["n"]==0
    assert query(cluster,"select status from public.polar_fulfillment_orders where order_id=$1",args["p_order_id"])[0]["status"]=="claimed"


def test_existing_link_other_plan_rolls_back_grant(cluster):
    args=binding(); receipt=claim(cluster,args)
    query(cluster,"insert into public.oauth_account_links(email_hash,account_id,customer_id,plan) values($1,$2,$3,'business') returning email_hash",
          args["p_email_hash"],args["p_account_id"],args["p_customer_id"])
    with pytest.raises(asyncpg.CheckViolationError): finish(cluster,args,receipt)
    assert balance(cluster,args)==0


def test_refund_before_claim_is_terminal_and_binding_protected(cluster):
    args=binding(); assert refund(cluster,args)["status"]=="refunded"
    assert claim(cluster,args)["status"]=="refunded"
    with pytest.raises(asyncpg.CheckViolationError): refund(cluster,{**args,"p_customer_id":"wrong"})
    assert balance(cluster,args)==0


def test_refund_after_claim_blocks_commit_and_future_customer_orders(cluster):
    args=binding(); receipt=claim(cluster,args); refund(cluster,args)
    assert finish(cluster,args,receipt)["status"]=="refunded"
    other={**args,"p_order_id":"second-"+uuid.uuid4().hex,"p_owner":str(uuid.uuid4())}
    assert claim(cluster,other)["status"]=="refunded"
    assert balance(cluster,args)==0


def test_refund_after_complete_never_reactivates_or_erases_revocation(cluster):
    args=binding(); receipt=claim(cluster,args); finish(cluster,args,receipt); refund(cluster,args)
    assert claim(cluster,args)["status"]=="refunded"
    assert finish(cluster,args,receipt)["status"]=="refunded"
    assert query(cluster,"select count(*) as n from public.polar_order_events where customer_id=$1 and status='revoked'",args["p_customer_id"])[0]["n"]==1
    assert balance(cluster,args)==3500  # revocation removes access, not a second monetary refund


@pytest.mark.parametrize("winner",["refund","complete"])
def test_forced_refund_commit_interleaving_always_finishes_revoked(cluster,winner):
    args=binding(); receipt=claim(cluster,args)
    async def drive():
        c=await asyncpg.connect(cluster.admin("polar_test")); tx=c.transaction()
        try:
            await tx.start()
            if winner=="refund":
                await rpc(cluster,"refund",connection=c,p_order_id=args["p_order_id"],p_customer_id=args["p_customer_id"])
                loser=asyncio.create_task(rpc(cluster,"complete",p_order_id=args["p_order_id"],p_owner=args["p_owner"],p_fence=receipt["fence"]))
            else:
                await rpc(cluster,"complete",connection=c,p_order_id=args["p_order_id"],p_owner=args["p_owner"],p_fence=receipt["fence"])
                loser=asyncio.create_task(rpc(cluster,"refund",p_order_id=args["p_order_id"],p_customer_id=args["p_customer_id"]))
            await asyncio.sleep(.15)
            assert not loser.done(),"Second transaction did not wait behind customer lock"
            await tx.commit(); result=await asyncio.wait_for(loser,5)
            assert result["status"]=="refunded"
        finally: await c.close()
    asyncio.run(drive())
    assert query(cluster,"select status from public.polar_fulfillment_orders where order_id=$1",args["p_order_id"])[0]["status"]=="refunded"
    assert balance(cluster,args)==(0 if winner=="refund" else 3500)


def test_receipt_schema_has_no_raw_identity_or_email_or_error_fields(cluster):
    cols=query(cluster,"select column_name from information_schema.columns where table_name='polar_fulfillment_orders'")
    assert not ({"email","token","raw_token","body","error","error_text"}&{r["column_name"] for r in cols})


def test_wrong_migration_owner_is_refused(cluster):
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        spine_pg.run_sql_sync(cluster.admin("polar_test"),SQL.read_text())


@contextmanager
def http_rpc_gateway(cluster):
    """Real loopback HTTP, exact named RPC payload, real role and PostgreSQL.

    This is a SQL transport shim, not a model of fulfillment. The actual four
    migrated functions decide every result. It does not claim PostgREST JWT proof.
    """
    credential=secrets.token_hex(24)
    observed=[]
    allowed={"claim","complete","release","refund"}

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self,*args): pass

        def do_POST(self):
            action=self.path.removeprefix("/rest/v1/rpc/polar_fulfillment_")
            try:
                assert action in allowed and self.headers.get("Authorization")=="Bearer "+credential
                assert self.headers.get("apikey")==credential
                payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                # The identifiers below are synthetic. Never persist the body.
                observed.append((action,set(payload)))
                result=asyncio.run(rpc(cluster,action,**payload))
                status=200
            except Exception:
                result={"error":"RPC rejected"};status=400
            body=json.dumps(result).encode()
            self.send_response(status);self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)

    server=ThreadingHTTPServer(("127.0.0.1",0),Gateway)
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}",credential,observed
    finally:
        server.shutdown();server.server_close();worker.join(timeout=3)


@pytest.mark.parametrize("plan,product,credits,budget,ttl",[
    ("developer","Growth",3500,500,7776000),
    ("business","Growth",3500,5000,7776000),
    ("enterprise","Growth",3500,25000,31536000),
])
def test_real_handler_http_payload_and_sql_fulfillment_then_refund(cluster,monkeypatch,plan,product,credits,budget,ttl):
    from agent_interface import identity
    from billing import polar_webhook,emails,telegram_revenue_alerts
    suffix=uuid.uuid4().hex
    args=binding()
    args["p_plan"]=plan
    email=suffix+"@example.invalid"
    args["p_email_hash"]=hashlib.sha256(email.encode()).hexdigest()
    event={"type":"order.paid","data":{"id":args["p_order_id"],"status":"paid",
        "customer":{"id":args["p_customer_id"],"email":email},
        "product":{"id":args["p_product_id"],"name":product},"metadata":{"plan":plan}}}
    delivered=[]

    async def welcome(**kw): delivered.append(kw["api_key"]);return True
    async def key_email(_email,_plan,key,_expiry): delivered.append(key);return True
    monkeypatch.setattr(emails,"send_welcome_email",welcome)
    monkeypatch.setattr(telegram_revenue_alerts,"send_api_key_email",key_email)
    monkeypatch.setenv("CREDITS_ENABLED","true")
    monkeypatch.setattr(identity,"_SIGNING_SECRET","isolated-fulfillment-signing-secret")
    # Only synthetic local revocation state participates; no production hydration.
    monkeypatch.setattr(identity,"_revoked_customer_ids",set())
    monkeypatch.setattr(identity,"_revocation_hydrated",True)
    monkeypatch.setattr(identity,"_revocation_next_try",time.time()+3600)
    monkeypatch.setattr(identity,"_revoked_jtis",set())
    monkeypatch.setattr(identity,"_jti_revocation_hydrated",True)
    with http_rpc_gateway(cluster) as (url,key,observed):
        monkeypatch.setenv("SUPABASE_URL",url)
        monkeypatch.setenv("POLAR_FULFILLMENT_KEY",key)
        asyncio.run(polar_webhook.handle_polar_event(event))
        asyncio.run(polar_webhook.handle_polar_event(event))
        assert len(delivered)==4 and len(set(delivered))==1
        assert balance(cluster,args)==credits
        claims=identity._verify(delivered[0])
        assert claims["scope"]["budget_cap_usd"]==budget
        assert claims["exp"]-claims["iat"]==ttl
        assert observed[0][1]=={"p_order_id","p_customer_id","p_account_id","p_product_id",
                                "p_credits","p_plan","p_email_hash","p_owner"}
        assert [action for action,_ in observed]==["claim","complete","claim"]
        # Nested Refund resource, no email or product: the persisted receipt and
        # explicit customer binding are sufficient, and the previously sent key fails.
        refunded={"type":"refund.created","data":{"id":"refund-"+suffix,
            "order":{"id":args["p_order_id"],"customer_id":args["p_customer_id"]}}}
        asyncio.run(polar_webhook.handle_polar_event(refunded))
        assert identity.validate_token(delivered[0]).valid is False
        asyncio.run(polar_webhook.handle_polar_event(event))
        assert len(delivered)==4 and balance(cluster,args)==credits
        assert [action for action,_ in observed][-2:]==["refund","claim"]
