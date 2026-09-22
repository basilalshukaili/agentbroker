"""Offline fulfillment regressions exercising the real webhook and package resolver.

Service fakes model the credit RPC's idempotency contract, not its SQL. The
fixture denies network and dotenv reads and restores every patched dependency.
"""
from __future__ import annotations

import asyncio
import builtins
import importlib
import io
import os
from pathlib import Path
import socket
import sys
from types import ModuleType, SimpleNamespace

import pytest

from billing import polar_webhook as webhook


class FulfillmentServices:
    """Stateful fake storage, grants, token issuer and delivery services."""

    def __init__(self):
        self.tables = {}
        self.grants = {}
        self.balance = 0
        self.grant_calls = []
        self.mint_calls = []
        self.welcome_emails = []
        self.key_emails = []
        self.alerts = []
        self.trace = []
        self.grant_failure = None
        self.grant_response = None
        self.marker_failure = None
        self.mint_failure = False
        self.event = {
            "type": "order.paid",
            "data": {
                "id": "offline-order", "status": "paid",
                "customer": {"id": "offline-customer", "email": "buyer@example.invalid"},
                "product": {"id": "offline-product", "name": "Growth"}, "amount": 2900,
            },
        }

    async def select_rows(self, table, filters=None, limit=1000):
        return [row.copy() for row in self.tables.get(table, [])
                if all(row.get(k) == v for k, v in (filters or {}).items())][:limit]

    async def insert_row(self, table, row):
        if table == "polar_order_events":
            self.trace.append("marker")
            if self.marker_failure == "raise":
                raise RuntimeError("synthetic marker outage")
            if self.marker_failure == "none":
                return None
            if self.marker_failure == "empty":
                return {}
            if self.marker_failure == "wrong_order":
                return {**row, "order_id": "another-order"}
        self.tables.setdefault(table, []).append(row.copy())
        return row.copy()

    def mint(self, **kwargs):
        self.trace.append("mint")
        self.mint_calls.append(kwargs)
        if self.mint_failure:
            raise RuntimeError("synthetic mint outage PRIVATE_PROVIDER_DETAIL")
        return SimpleNamespace(token="FAKE-NOT-A-REAL-TOKEN", expires_at=1900000000,
                               agent_id="sub_" + kwargs["customer_id"])

    async def grant(self, **kwargs):
        self.trace.append("grant")
        self.grant_calls.append(kwargs)
        if self.grant_failure == "raise":
            raise RuntimeError("synthetic grant outage PRIVATE_PROVIDER_DETAIL")
        if self.grant_failure == "response":
            return self.grant_response
        order_key = kwargs["idempotency_key"]
        if order_key not in self.grants:
            self.grants[order_key] = kwargs.copy()
            self.balance += kwargs["amount"]
        if self.grant_failure == "committed_then_raise":
            raise RuntimeError("synthetic response lost after committed grant")
        return {"ok": True, "balance_after": self.balance}

    async def welcome(self, **kwargs):
        self.welcome_emails.append(kwargs)

    async def key_email(self, *args):
        self.key_emails.append(args)

    async def alert(self, message):
        self.alerts.append(message)

    def deliver(self):
        async def run():
            try:
                await webhook.handle_polar_event(self.event)
            finally:
                # Drain the queued welcome-email task while fakes are installed.
                await self.real_sleep(0)
        self.loop.run_until_complete(run())

    def assert_retryable_failure(self):
        with pytest.raises(webhook.PaidOrderFulfillmentError) as caught:
            self.deliver()
        assert str(caught.value) == "Paid order fulfillment incomplete; retry later."
        assert self.tables.get("polar_order_events", []) == []
        assert self.welcome_emails == self.key_emails == []
        assert not any("Their agent can now call paid tools" in x for x in self.alerts)

    def assert_recovered_once(self):
        self.grant_failure = self.marker_failure = None
        self.mint_failure = False
        self.deliver()
        assert self.balance == 3500
        assert len(self.grants) == 1
        assert len(self.tables["polar_order_events"]) == 1
        assert len(self.welcome_emails) == len(self.key_emails) == 1
        call_count = len(self.grant_calls)
        self.deliver()
        assert len(self.grant_calls) == call_count
        assert self.balance == 3500
        assert len(self.welcome_emails) == len(self.key_emails) == 1


@pytest.fixture
def services(monkeypatch):
    services = FulfillmentServices()
    # Construct asyncio's local wakeup sockets before denying connections.
    services.loop = asyncio.new_event_loop()
    services.real_sleep = asyncio.sleep
    monkeypatch.setattr(os, "environ", {
        k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR"}
    })
    monkeypatch.setenv("CREDITS_ENABLED", "true")

    def deny_network(*args, **kwargs):
        raise AssertionError("Network access is forbidden in fulfillment tests")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, deny_network)
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, deny_network)

    def guarded_open(original):
        def open_without_env(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                name = Path(os.fsdecode(file)).name
                assert name != ".env" and not name.startswith(".env."), "dotenv read forbidden"
            return original(file, *args, **kwargs)
        return open_without_env

    monkeypatch.setattr(builtins, "open", guarded_open(builtins.open))
    monkeypatch.setattr(io, "open", guarded_open(io.open))

    def fake_module(name, **members):
        module = ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
        parent, child = name.rsplit(".", 1)
        monkeypatch.setattr(importlib.import_module(parent), child, module, raising=False)

    fake_module("storage.supabase_client", select_rows=services.select_rows,
                insert_row=services.insert_row, insert_row_strict=services.insert_row)
    fake_module("agent_interface.identity", issue_subscription_token=services.mint)
    fake_module("billing.credits", grant=services.grant)
    fake_module("billing.emails", send_welcome_email=services.welcome)
    fake_module("billing.telegram_revenue_alerts", send_api_key_email=services.key_email,
                send_telegram_alert=services.alert)
    fake_module("compliance.log_redactor", mask_email=lambda email: "masked@example.invalid")

    async def no_sleep(*args, **kwargs):
        pass

    # Package resolution stays real; only remove backoff time from these tests.
    monkeypatch.setattr(webhook.asyncio, "sleep", no_sleep)
    try:
        yield services
    finally:
        services.loop.close()


def test_grant_outage_then_redelivery_recovers_once(services):
    services.grant_failure = "raise"
    services.assert_retryable_failure()
    assert len(services.grant_calls) == 3
    assert len(services.tables["ungranted_orders"]) == 1
    assert any("credit grant was not confirmed" in text for text in services.alerts)
    services.assert_recovered_once()


@pytest.mark.parametrize("response", [None, {}, [], {"ok": False}, {"ok": 1}, {"ok": "true"}])
def test_malformed_rpc_responses_fail_closed(services, response):
    services.grant_failure = "response"
    services.grant_response = response
    services.assert_retryable_failure()
    assert len(services.grant_calls) == 3
    services.assert_recovered_once()


@pytest.mark.parametrize("failure", ["raise", "none", "empty", "wrong_order"])
def test_marker_failure_after_grant_is_safe_to_replay(services, failure):
    services.marker_failure = failure
    services.assert_retryable_failure()
    assert services.balance == 3500
    services.assert_recovered_once()


def test_mint_failure_after_grant_is_safe_to_replay(services):
    services.mint_failure = True
    services.assert_retryable_failure()
    assert services.balance == 3500
    services.assert_recovered_once()


def test_lost_grant_response_never_double_credits(services):
    services.grant_failure = "committed_then_raise"
    services.assert_retryable_failure()
    assert services.balance == 3500
    assert any("credit grant was not confirmed" in text for text in services.alerts)
    assert not any("NO credits" in text for text in services.alerts)
    services.assert_recovered_once()


def test_unknown_package_fails_before_grant(services):
    services.event["data"]["product"] = {"id": "unmapped", "name": "Unknown"}
    services.assert_retryable_failure()
    assert services.grant_calls == []


def test_missing_order_fails_before_grant(services):
    del services.event["data"]["id"]
    services.assert_retryable_failure()
    assert services.grant_calls == []


@pytest.mark.parametrize("product", [None, [], "Growth", {"metadata": "invalid"}])
def test_missing_or_malformed_product_fails_before_grant(services, product):
    services.event["data"]["product"] = product
    services.assert_retryable_failure()
    assert services.grant_calls == []


def test_success_ordering_and_account_binding(services):
    services.assert_recovered_once()
    assert services.trace == ["grant", "mint", "marker"]
    grant = services.grants["offline-order"]
    assert grant["account_id"] == "sub_offline-customer"
    assert grant["order_id"] == "offline-order"
    assert grant["amount"] == 3500
    assert services.mint_calls[0]["customer_id"] == "offline-customer"


def test_disabled_credits_preserves_legacy_delivery(services, monkeypatch):
    monkeypatch.setenv("CREDITS_ENABLED", "false")
    services.event["data"].pop("product")
    services.deliver()
    services.deliver()
    assert services.grant_calls == []
    assert len(services.key_emails) == len(services.mint_calls) == 1
    assert services.welcome_emails == []


@pytest.mark.parametrize("event_type,status", [("ignored.event", "paid"), ("order.created", "pending")])
def test_ignored_and_unpaid_events_do_not_grant(services, event_type, status):
    services.event["type"] = event_type
    services.event["data"]["status"] = status
    services.deliver()
    assert services.grant_calls == services.mint_calls == []
