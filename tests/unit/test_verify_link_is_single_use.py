"""A verification link must stop working once it has been used.

Found by walking the signup as a stranger: clicking the emailed link a second
time issued a key again, and a third time would have too, for the whole hour
the token stays valid.

The key is deterministic per email, so a replay never created a second
identity or extra quota - the defect is that a link keeps working after the
person has finished with it, and anyone who later reads that email (a
forwarded thread, a shared inbox, a log) can use it.

Single use could not be enforced before because consume_pending returned None
both for "already used" and for "the database did not answer". Those mean
opposite things: the first should refuse, the second must NOT, or a Supabase
blip becomes a signup outage.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_interface import key_requests as KR
from agent_interface import key_request_logic as KRL


def _get(token: str):
    return asyncio.run(KR.verify_free_key(token=token))


@pytest.fixture
def _valid_token(monkeypatch):
    monkeypatch.setattr(KR, "verify_token", lambda t: "someone@example.com")
    # Do not send email or touch identity storage in a unit test.
    async def _no_email(*a, **kw):
        return True

    monkeypatch.setattr(KR, "send_key_email", _no_email, raising=False)
    return "tok_valid"


def test_the_first_click_issues_a_key(monkeypatch, _valid_token):
    async def _consume(token, email=None):
        return "someone@example.com"          # row was there

    monkeypatch.setattr(KR, "consume_pending", _consume)
    resp = _get(_valid_token)
    assert resp.status_code == 200
    assert b"free" in resp.body.lower()


def test_the_second_click_is_refused(monkeypatch, _valid_token):
    async def _consume(token, email=None):
        return None                           # row gone: already used

    monkeypatch.setattr(KR, "consume_pending", _consume)
    resp = _get(_valid_token)
    assert resp.status_code == 400, (
        "a used verification link still minted a key")
    body = resp.body.decode("utf-8", "replace").lower()
    assert "already been used" in body
    # It must also tell them what to do next, or this is just a dead end.
    assert "docs" in body or "request" in body


def test_a_database_outage_does_not_block_signup(monkeypatch, _valid_token):
    """The reason single-use was not enforced in the first place. The
    signature and expiry are a real gate on their own; refusing every signup
    during a blip is the worse failure."""
    async def _consume(token, email=None):
        raise KRL.PendingLookupUnavailable("supabase down")

    monkeypatch.setattr(KR, "consume_pending", _consume)
    resp = _get(_valid_token)
    assert resp.status_code == 200, (
        "a Supabase outage turned into a signup outage")


def test_an_invalid_signature_is_still_refused(monkeypatch):
    monkeypatch.setattr(KR, "verify_token", lambda t: None)
    resp = _get("tok_forged")
    assert resp.status_code == 400


def test_consume_pending_separates_absent_from_unreachable(monkeypatch):
    """The distinction the whole fix rests on. Both used to be None."""
    from storage import supabase_client as sb

    async def _empty(table, filters=None, **kw):
        # The row is absent, but the TABLE is readable - which is what makes
        # "already used" a safe conclusion. An entirely empty read is treated
        # as unavailable now (RLS can hide rows behind a 200 []), so the probe
        # has to see something.
        if not filters:
            return [{"email": "someone-else@example.com"}]
        return []

    async def _down(table, filters=None, **kw):
        raise sb.SupabaseUnavailable("no config")

    monkeypatch.setattr(sb, "select_rows_strict", _empty)
    assert asyncio.run(KRL.consume_pending("t")) is None

    monkeypatch.setattr(sb, "select_rows_strict", _down)
    with pytest.raises(KRL.PendingLookupUnavailable):
        asyncio.run(KRL.consume_pending("t"))
