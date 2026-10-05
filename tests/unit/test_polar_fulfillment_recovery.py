"""Handler acceptance with durable service fakes; real SQL/ACL proofs are separate."""
import asyncio
import copy
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from billing import polar_webhook as webhook
from billing import polar_fulfillment as store

class Services:
    def __init__(self):
        self.rows, self.balance, self.trace, self.mints, self.deliveries = {}, 0, [], [], []
        self.failure = None
        self.event = {"type":"order.paid","data":{"id":"offline-order","status":"paid",
            "customer":{"id":"offline-customer","email":"buyer@example.invalid"},
            "product":{"id":"offline-product","name":"Growth"},"amount":2900}}

    @staticmethod
    def result(row):
        return {"ok":True, **{k:row[k] for k in ("status","issued_at","token_id","fence","issuance_version")}}

    async def claim(self, **payload):
        self.trace.append("claim")
        if self.failure == "lookup":
            raise RuntimeError("synthetic lookup outage")
        key, binding = payload["order_id"], {k:v for k,v in payload.items() if k != "owner"}
        row = self.rows.get(key)
        if row:
            if row["status"] == "refunded":
                return {"ok":True,"status":"refunded"}
            if row["binding"] != binding:
                return {"ok":True,"status":"conflict"}
            if row["status"] == "claimed":
                return {"ok":True,"status":"in_progress"}
            if row["status"] == "complete":
                return self.result(row)
        if any(r["status"] == "refunded" and r["binding"]["customer_id"] == payload["customer_id"]
               for r in self.rows.values()):
            return {"ok":True,"status":"conflict"}
        if row:
            row.update(fence=row["fence"]+1, owner=payload["owner"], status="claimed")
        else:
            row = dict(binding=binding,owner=payload["owner"],status="claimed",issued_at=1791000000.0,
                       token_id="a"*32,fence=1,issuance_version=1)
            self.rows[key] = row
        return self.result(row)

    async def complete(self, **payload):
        self.trace.append("commit")
        await asyncio.sleep(0)  # Allow an overlapping webhook to see the active claim.
        row = self.rows[payload["order_id"]]
        if row["status"] == "refunded":
            return {"ok":True,"status":"refunded"}
        assert row["owner"] == payload["owner"] and row["fence"] == payload["fence"]
        if self.failure in {"grant","link","marker"}:
            raise RuntimeError("synthetic transaction outage")
        self.balance += row["binding"]["credits"]
        row["status"] = "complete"
        if self.failure == "lost_response":
            raise RuntimeError("response lost after commit")
        return self.result(row)

    async def release(self, **payload):
        row = self.rows[payload["order_id"]]
        if row["status"] == "claimed" and row["owner"] == payload["owner"] and row["fence"] == payload["fence"]:
            row["status"] = "ready"

    async def refund(self, **payload):
        if self.failure == "refund":
            raise RuntimeError("synthetic refund outage")
        row = self.rows.setdefault(payload["order_id"],{"binding":{"customer_id":payload["customer_id"]}})
        assert row["binding"]["customer_id"] == payload["customer_id"]
        row["status"] = "refunded"

    def mint(self, customer_id, plan, email, **issuance):
        self.trace.append("sign")
        if self.failure == "mint":
            raise RuntimeError("synthetic signer unavailable")
        self.mints.append(issuance.copy())
        return SimpleNamespace(token="synthetic."+issuance["token_id"],expires_at=1900000000)

    async def welcome(self, **payload):
        self.trace.append("welcome")
        if self.failure == "email":
            raise RuntimeError("synthetic delivery outage")
        if self.failure == "email_false":
            return False
        self.deliveries.append(payload["api_key"])
        return True

    async def key_email(self,email,plan,token,expiry):
        self.trace.append("key_email")
        self.deliveries.append(token)
        return True

    def deliver(self,event=None):
        return self.loop.run_until_complete(webhook.handle_polar_event(event or self.event))

@pytest.fixture
def services(monkeypatch):
    s = Services()
    s.loop = asyncio.new_event_loop()  # Local Windows wakeup sockets before external-I/O denial.
    monkeypatch.setenv("CREDITS_ENABLED","true")
    monkeypatch.setattr("agent_interface.identity._revoked_customer_ids", set())
    def denied(*args,**kwargs):
        raise AssertionError("External I/O forbidden")
    monkeypatch.setattr(socket.socket,"connect",denied)
    monkeypatch.setattr(socket.socket,"connect_ex",denied)
    monkeypatch.setattr(socket,"getaddrinfo",denied)
    for name in ("claim","complete","release","refund"):
        monkeypatch.setattr(store,name,getattr(s,name))
    monkeypatch.setattr("agent_interface.identity.issue_subscription_token",s.mint)
    monkeypatch.setattr("billing.emails.send_welcome_email",s.welcome)
    monkeypatch.setattr("billing.telegram_revenue_alerts.send_api_key_email",s.key_email)
    monkeypatch.setattr("billing.telegram_revenue_alerts.send_telegram_alert",AsyncMock())
    try:
        yield s
    finally:
        s.loop.close()

@pytest.mark.parametrize("failure",["lookup","mint","grant","link","marker"])
def test_failure_never_marks_or_delivers_success_and_redelivery_recovers(services,failure):
    services.failure = failure
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver()
    assert services.balance == 0 and services.deliveries == []
    assert not any(row["status"] == "complete" for row in services.rows.values())
    services.failure = None
    services.deliver()
    assert services.balance == 3500 and services.rows["offline-order"]["status"] == "complete"

def test_completion_response_loss_recovers_the_same_identity_once(services):
    services.failure = "lost_response"
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver()
    assert services.balance == 3500 and services.deliveries == []
    services.failure = None
    services.deliver()
    services.deliver()
    assert services.balance == 3500 and len(set(services.deliveries)) == 1
    assert len({(m["issued_at"],m["token_id"]) for m in services.mints}) == 1

def test_concurrent_delivery_has_one_grant_and_one_identity(services):
    async def drive():
        return await asyncio.gather(webhook.handle_polar_event(services.event),
                                    webhook.handle_polar_event(services.event), return_exceptions=True)
    results = services.loop.run_until_complete(drive())
    assert sum(isinstance(result, webhook.PaidOrderFulfillmentError) for result in results) == 1
    services.deliver()  # Provider retry after the lease holder completes.
    assert services.balance == 3500 and len(set(services.deliveries)) == 1

def test_notifications_follow_atomic_credit_and_oauth_completion(services):
    services.deliver()
    assert services.trace == ["claim","sign","commit","welcome","key_email"]
    assert services.rows["offline-order"]["binding"]["email_hash"] != "buyer@example.invalid"

@pytest.mark.parametrize("failure", ["email", "email_false"])
def test_delivery_failure_retries_identity_without_granting_again(services, failure):
    services.failure = failure
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver()
    services.failure = None
    services.deliver()
    assert services.balance == 3500 and len(set(services.deliveries)) == 1

@pytest.mark.parametrize("field,value",[("customer",{}),("id",""),("product",{}),
    ("product",{"id":"unknown","name":"Unknown"}),("product",{"id":"x","metadata":"invalid"})])
def test_missing_or_unknown_entitlement_does_not_claim(services,field,value):
    services.event["data"][field] = value
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver()
    assert services.rows == {} and services.balance == 0

@pytest.mark.parametrize("field",["customer","product"])
def test_replay_with_changed_entitlement_fails_closed(services,field):
    services.deliver()
    services.event["data"][field]["id"] = "other"
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver()
    assert services.balance == 3500


@pytest.mark.parametrize("change", ["email", "credits", "plan"])
def test_completed_order_rejects_changed_email_or_package(services, change):
    services.deliver()
    if change == "email":
        services.event["data"]["customer"]["email"] = "other@example.invalid"
    elif change == "credits":
        services.event["data"]["product"]["metadata"] = {"credits":999}
    else:
        services.event["data"]["product"]["name"] = "Enterprise"
        services.event["data"]["product"]["metadata"] = {"credits":3500}
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver()
    assert services.balance == 3500 and len(services.deliveries) == 2

def test_refund_before_payment_is_terminal(services):
    services.deliver({"type":"refund.created","data":{"id":"offline-refund","order":{
        "id":"offline-order","customer":{"id":"offline-customer"}},
        "status":"succeeded","revoke_benefits":True}})
    services.deliver()
    assert services.balance == 0 and services.deliveries == []

def test_refund_store_outage_is_retryable(services):
    services.failure = "refund"
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver({"type":"order.refunded","data":{**services.event["data"],"status":"refunded"}})

def test_subscription_revocation_is_a_customerwide_tombstone(services):
    services.deliver({"type":"subscription.revoked","data":{"id":"offline-subscription",
                     "customer_id":"offline-customer"}})
    assert services.rows["offline-subscription"]["status"] == "refunded"
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver()
    assert services.balance == 0 and services.deliveries == []


def test_new_paid_order_for_revoked_customer_is_not_silently_acknowledged(services):
    services.deliver({"type":"order.refunded","data":{**services.event["data"],"status":"refunded"}})
    services.event["data"]["id"] = "new-paid-order"
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver()
    assert "new-paid-order" not in services.rows and services.balance == 0

def test_refund_winning_before_commit_suppresses_delivery(services,monkeypatch):
    async def winning_refund(**payload):
        services.rows[payload["order_id"]]["status"] = "refunded"
        return {"ok":True,"status":"refunded"}
    monkeypatch.setattr(store,"complete",winning_refund)
    services.deliver()
    assert services.balance == 0 and services.deliveries == []

def test_unpaid_created_and_subscription_events_do_not_fulfill_credit_orders(services):
    for kind,status in [("order.created","pending"),("order.created",""),("subscription.active","active"),("ignored","paid")]:
        event = copy.deepcopy(services.event)
        event["type"],event["data"]["status"] = kind,status
        services.deliver(event)
    assert services.rows == {} and services.balance == 0


def test_scoped_refund_remains_durable_with_credits_disabled(services, monkeypatch):
    monkeypatch.setenv("CREDITS_ENABLED", "false")
    monkeypatch.setenv("POLAR_FULFILLMENT_KEY", "synthetic-scoped-key")
    services.deliver({"type":"order.refunded","data":{**services.event["data"],"status":"refunded"}})
    assert services.rows["offline-order"]["status"] == "refunded"
    services.failure = "refund"
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver({"type":"order.refunded","data":{**services.event["data"],"status":"refunded"}})


def test_existing_paid_order_fulfills_with_offers_disabled_and_scoped_key(services, monkeypatch):
    monkeypatch.setenv("CREDITS_ENABLED", "false")
    monkeypatch.setenv("POLAR_FULFILLMENT_KEY", "synthetic-scoped-key")
    services.deliver()
    assert services.balance == 3500
    assert services.trace == ["claim", "sign", "commit", "welcome", "key_email"]


@pytest.mark.parametrize("kind", ["refund.created", "refund.updated"])
@pytest.mark.parametrize("status,benefits", [("pending",True),("failed",True),("canceled",True),("succeeded",False)])
def test_unsettled_or_benefits_retained_refund_never_revokes(services, kind, status, benefits):
    services.deliver({"type":kind,"data":{"id":"refund","order_id":"offline-order",
        "customer_id":"offline-customer","status":status,"revoke_benefits":benefits}})
    assert services.rows == {}


def test_successful_refund_update_revokes_after_pending_created(services):
    data = {"id":"refund","order_id":"offline-order","customer_id":"offline-customer",
            "status":"pending","revoke_benefits":True}
    services.deliver({"type":"refund.created","data":data})
    services.deliver({"type":"refund.updated","data":{**data,"status":"succeeded"}})
    services.deliver()
    assert services.balance == 0 and services.deliveries == []


@pytest.mark.parametrize("data", [
    {"status":"partially_refunded"},
    {"status":"paid","total_amount":2900,"refunded_amount":1000,"refunded_tax_amount":0},
])
def test_partial_order_refund_does_not_customerwide_revoke(services, data):
    services.deliver({"type":"order.refunded","data":{**services.event["data"],**data}})
    assert services.rows == {}


def test_full_order_amounts_revoke_without_terminal_status(services):
    services.deliver({"type":"order.refunded","data":{**services.event["data"],
        "total_amount":2900,"refunded_amount":2800,"refunded_tax_amount":100}})
    assert services.rows["offline-order"]["status"] == "refunded"


@pytest.mark.parametrize("kind", ["order.refunded", "refund.created", "refund.updated"])
def test_ambiguous_refund_is_retryable_without_irreversible_write(services, kind):
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        services.deliver({"type":kind,"data":{"id":"offline-order","customer_id":"offline-customer"}})
    assert services.rows == {}


@pytest.mark.parametrize("credits_enabled", ["true", "false"])
@pytest.mark.parametrize("kind,data", [("order.created",{}),("order.paid",{"paid":False}),
    ("order.paid",{"status":"pending"})])
def test_unpaid_orders_do_not_mint_in_either_mode(services, monkeypatch, credits_enabled, kind, data):
    monkeypatch.setenv("CREDITS_ENABLED", credits_enabled)
    services.deliver({"type":kind,"data":{**services.event["data"],"status":"",**data}})
    assert services.mints == [] and services.rows == {}
