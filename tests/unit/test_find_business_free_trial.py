"""
find_business free trial: a caller with no key gets N successful calls, then a
friendly "here is how to get a key" -- and nothing else about access changes.

Founder scope (chat 4 row 3070, "Yes proceed", answering row 3068): "make
find_business a zero-friction free trial (no signup, 10 free calls) so any
Smithery visitor can test it immediately."

WHAT IS TESTED AGAINST WHAT. The counter's state is Supabase, reached through
two SECURITY DEFINER functions (sql/agentbroker/008_anon_trial_reserve_release_
rpc.sql). No Postgres runs here, so tests/anon_trial_fake.py re-states those
functions in Python and every behavioural test below runs the REAL gate
(billing/anon_trial.py, the real tools/call handler, the real REST route)
against that model. The SQL file's structure is checked at the bottom against
the same rules the model encodes; the SQL itself has to be applied and
tests/integration/test_anon_trial_rpc_live.py run before this ships.

The conftest autouse fixture makes the gate admit everything for the rest of
the suite; every test here installs the faithful model over it.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re

import pytest

import billing.anon_trial as anon_trial
from core import tool_auth
from tests.anon_trial_fake import FakeTrialStore

# Captured at import time, i.e. BEFORE the conftest autouse fixture replaces it.
_REAL_RPC = anon_trial._rpc

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# What the container sees as its TCP peer when Caddy (on the host) connects
# through Docker's port publish: the bridge gateway. A private address, so the
# gate treats X-Forwarded-For as written by our own proxy.
PROXY = "172.17.0.1"
N = tool_auth.TRIAL_CALLS_PER_CALLER

GOOD_ARGS = {"vertical": "personal_services",
             "location": {"zip_or_city": "Atlanta"}, "max_results": 1}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

class Clock:
    day = "2026-09-30"


@pytest.fixture
def store(monkeypatch):
    s = FakeTrialStore()
    monkeypatch.setattr(anon_trial, "_rpc", s)
    return s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(anon_trial, "_today_utc", lambda: c.day)
    return c


@pytest.fixture(autouse=True)
def _quiet_usage_telemetry(monkeypatch):
    """Telemetry is fire-and-forget and unrelated; keep its noise out."""
    import billing.usage_logger as ul
    monkeypatch.setattr(ul, "fire_log_usage", lambda *a, **k: None)


@pytest.fixture
def runs(monkeypatch):
    """Counts how many times the real find_business handler actually ran."""
    import core.find_business as fb
    real = fb.handle_find_business
    box = {"n": 0}

    async def counting(*a, **k):
        box["n"] += 1
        return await real(*a, **k)

    monkeypatch.setattr(fb, "handle_find_business", counting)
    return box


def hdrs(client_ip=None, peer=PROXY, **extra):
    """Headers as main.py hands them down: the socket peer, then whatever the
    proxy put in X-Forwarded-For."""
    h = {anon_trial.PEER_HEADER: peer}
    if client_ip:
        h["x-forwarded-for"] = client_ip
    h.update(extra)
    return h


def rpc_call(headers, args=None, tool="find_business", profile=None, method="tools/call"):
    import agent_interface.mcp_server as ms
    params = {"name": tool, "arguments": GOOD_ARGS if args is None else args}
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "tools/call":
        payload["params"] = params
    return asyncio.run(ms.handle_mcp_request(payload, headers, profile=profile))


def body(resp):
    """(isError, parsed tool-result JSON) from a tools/call response."""
    assert "error" not in resp, f"got a JSON-RPC error object, not a tool result: {resp}"
    r = resp["result"]
    return r["isError"], json.loads(r["content"][0]["text"])


def make_key(agent_id="trial_test_agent"):
    from agent_interface.identity import TokenRequest, issue_token
    return issue_token(TokenRequest(agent_id=agent_id, principal_id="p_" + agent_id)).token


def caller_rows(store):
    return {k: v["count"] for k, v in store.rows.items() if k.startswith("trial:c:")}


# ---------------------------------------------------------------------------
# The allowance
# ---------------------------------------------------------------------------

class TestAllowance:
    def test_the_first_N_keyless_calls_succeed_and_report_what_is_left(self, store, clock, runs):
        h = hdrs("203.0.113.10")
        for i in range(1, N + 1):
            is_err, b = body(rpc_call(h))
            assert not is_err, f"call {i} was refused: {b}"
            assert b["status"] == "success"
            trial = b["free_trial"]
            assert trial["calls_included"] == N
            assert trial["calls_used"] == i
            assert trial["calls_remaining"] == N - i
        assert runs["n"] == N

    def test_the_last_free_call_says_so_and_says_where_to_go_next(self, store, clock):
        h = hdrs("203.0.113.11")
        for _ in range(N - 1):
            rpc_call(h)
        _, b = body(rpc_call(h))
        assert b["free_trial"]["calls_remaining"] == 0
        assert "last keyless" in b["free_trial"]["note"]
        assert "/keys/request" in b["free_trial"]["note"]

    def test_call_N_plus_1_is_a_friendly_tool_result_not_a_protocol_error(self, store, clock):
        h = hdrs("203.0.113.12")
        for _ in range(N):
            rpc_call(h)
        resp = rpc_call(h)
        # A tool RESULT (many MCP clients hide JSON-RPC errors from the model).
        assert "error" not in resp
        is_err, b = body(resp)
        assert is_err is True
        assert b["status"] == "failure"
        assert b["reason_code"] == "free_trial_exhausted"
        assert b["error_code"] == "auth_required"        # the vocabulary agents branch on
        assert b["retriable"] is False
        assert b["cost"]["amount"] == 0.0
        assert b["free_trial"]["calls_remaining"] == 0

    def test_the_refusal_tells_the_agent_exactly_how_to_get_a_key(self, store, clock):
        h = hdrs("203.0.113.13")
        for _ in range(N):
            rpc_call(h)
        _, b = body(rpc_call(h))
        msg = b["human_message"]
        assert str(N) in msg and "find_business" in msg
        assert "still free" in msg                        # a key is not a payment
        assert "/keys/request" in msg
        assert "X-Agent-Identity" in msg
        fk = b["how_to_resolve"]["free_key"]
        assert fk["method"] == "POST" and fk["url"].endswith("/keys/request")
        assert fk["body"] == {"email": "you@example.com"}
        assert b["how_to_resolve"]["header"] == "X-Agent-Identity"

    def test_the_refusal_uses_the_key_path_the_server_already_advertises(self, store, clock):
        """The same URL the auth_required error and /keys/request itself name."""
        from agent_interface.key_requests import _public_base
        h = hdrs("203.0.113.14")
        for _ in range(N):
            rpc_call(h)
        _, b = body(rpc_call(h))
        assert b["how_to_resolve"]["free_key"]["url"] == _public_base() + "/keys/request"

    def test_a_refused_call_does_not_run_the_tool(self, store, clock, runs):
        h = hdrs("203.0.113.15")
        for _ in range(N):
            rpc_call(h)
        assert runs["n"] == N
        for _ in range(5):
            rpc_call(h)
        assert runs["n"] == N, "a refused find_business call still executed the tool"

    def test_refused_calls_do_not_keep_counting_up(self, store, clock):
        h = hdrs("203.0.113.16")
        for _ in range(N + 7):
            rpc_call(h)
        assert list(caller_rows(store).values()) == [N]
        assert store.global_count("find_business") == N

    def test_callers_are_counted_separately(self, store, clock):
        a, b_ = hdrs("203.0.113.20"), hdrs("203.0.113.21")
        for _ in range(N):
            rpc_call(a)
        assert body(rpc_call(a))[0] is True                # a is used up
        assert body(rpc_call(b_))[0] is False              # b has a full allowance

    def test_an_ipv6_caller_is_one_caller_per_slash_64(self, store, clock):
        """A tenant is handed a whole /64; keying on the full address would let
        one machine mint 2^64 'different' callers."""
        for i in range(N):
            rpc_call(hdrs("2001:db8:abcd:12::%x" % (i + 1)))
        is_err, b = body(rpc_call(hdrs("2001:db8:abcd:12:ffff::9")))
        assert is_err and b["reason_code"] == "free_trial_exhausted"
        # A different /64 is a different caller.
        assert body(rpc_call(hdrs("2001:db8:abcd:13::1")))[0] is False

    def test_the_store_holds_a_keyed_hash_and_never_the_address(self, store, clock):
        ip = "198.51.100.77"
        rpc_call(hdrs(ip))
        blob = json.dumps([store.rows, store.calls], default=str)
        assert ip not in blob
        assert ip.replace(".", "") not in blob
        (key,) = [k for k in store.rows if k.startswith("trial:c:")]
        assert re.fullmatch(r"trial:c:find_business:[0-9a-f]{64}", key)

    def test_the_hash_depends_on_a_server_side_secret(self, monkeypatch):
        """A bare SHA-256 of an IPv4 address is undone by trying all 2^32."""
        monkeypatch.setenv("JWT_SIGNING_SECRET", "secret-one")
        k1 = anon_trial.caller_key("find_business", "v4:198.51.100.77")
        monkeypatch.setenv("JWT_SIGNING_SECRET", "secret-two")
        k2 = anon_trial.caller_key("find_business", "v4:198.51.100.77")
        import hashlib
        bare = hashlib.sha256(b"find_business|v4:198.51.100.77").hexdigest()
        assert k1 != k2 and bare not in (k1, k2)

    def test_the_hash_is_stable_for_one_caller_and_separate_per_tool(self):
        a = anon_trial.caller_key("find_business", "v4:198.51.100.1")
        assert a == anon_trial.caller_key("find_business", "v4:198.51.100.1")
        assert a != anon_trial.caller_key("find_business", "v4:198.51.100.2")
        assert a != anon_trial.caller_key("another_tool", "v4:198.51.100.1")


# ---------------------------------------------------------------------------
# Who counts as "the same caller" -- and which headers may be believed
# ---------------------------------------------------------------------------

class TestIdentityTrust:
    def test_x_forwarded_for_is_ignored_on_a_direct_connection(self):
        """A peer that is not our proxy wrote that header itself."""
        h = {anon_trial.PEER_HEADER: "203.0.113.50", "x-forwarded-for": "10.9.9.9, 1.2.3.4"}
        assert str(anon_trial.client_ip(h)) == "203.0.113.50"

    def test_a_forged_leftmost_hop_does_not_choose_the_caller(self):
        """Proxy appends: client-claimed values sit on the LEFT, ours on the right."""
        h = {anon_trial.PEER_HEADER: PROXY, "x-forwarded-for": "1.1.1.1, 203.0.113.60"}
        assert str(anon_trial.client_ip(h)) == "203.0.113.60"

    def test_a_chain_of_local_proxies_is_skipped(self):
        h = {anon_trial.PEER_HEADER: "127.0.0.1",
             "x-forwarded-for": "203.0.113.61, 10.0.0.5, 172.16.4.4"}
        assert str(anon_trial.client_ip(h)) == "203.0.113.61"

    def test_a_malformed_hop_is_not_guessed_around(self):
        h = {anon_trial.PEER_HEADER: PROXY, "x-forwarded-for": "203.0.113.62, not-an-ip"}
        assert anon_trial.client_ip(h) is None
        assert anon_trial.caller_identity(h) == "unidentified"

    def test_x_real_ip_is_never_believed(self):
        h = {anon_trial.PEER_HEADER: PROXY, "x-forwarded-for": "203.0.113.63",
             "x-real-ip": "8.8.8.8"}
        assert str(anon_trial.client_ip(h)) == "203.0.113.63"
        h2 = {anon_trial.PEER_HEADER: PROXY, "x-real-ip": "8.8.8.8"}
        assert str(anon_trial.client_ip(h2)) == PROXY      # not 8.8.8.8

    def test_without_a_verified_peer_no_forwarding_header_is_trusted(self):
        assert anon_trial.client_ip({"x-forwarded-for": "203.0.113.64"}) is None
        assert anon_trial.caller_identity({"x-forwarded-for": "203.0.113.64"}) == "unidentified"

    def test_a_second_proxy_hop_hides_the_visitor_until_the_operator_names_it(self, monkeypatch):
        """hatchloop.dev/mcp/agent-broker is a Next.js rewrite on the same box
        that calls api.hatchloop.dev from the box's own public address. With
        Caddy trusting that address the chain arrives as 'visitor, 127.0.0.1,
        <box>'; the box address is not private, so it is the visitor unless it
        is declared one of OUR proxies."""
        box = "198.51.100.200"
        chain = {anon_trial.PEER_HEADER: PROXY,
                 "x-forwarded-for": "203.0.113.90, 127.0.0.1, " + box}
        monkeypatch.delenv("ANON_TRIAL_TRUSTED_PROXIES", raising=False)
        assert str(anon_trial.client_ip(chain)) == box            # every visitor looks like the box
        monkeypatch.setenv("ANON_TRIAL_TRUSTED_PROXIES", "127.0.0.1, " + box)
        assert str(anon_trial.client_ip(chain)) == "203.0.113.90"  # the visitor is back

    def test_trusted_proxy_networks_are_accepted_and_junk_is_dropped_not_guessed(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("ANON_TRIAL_TRUSTED_PROXIES", "198.51.100.0/24; nonsense , 2001:db8::/32")
        chain = {anon_trial.PEER_HEADER: PROXY, "x-forwarded-for": "203.0.113.91, 198.51.100.7"}
        with caplog.at_level(logging.ERROR, logger="smb_broker.anon_trial"):
            assert str(anon_trial.client_ip(chain)) == "203.0.113.91"
        assert "nonsense" in " ".join(r.getMessage() for r in caplog.records)

    def test_listing_a_proxy_can_never_make_a_client_supplied_hop_win(self, monkeypatch):
        """Only hops to the RIGHT of the first untrusted address are ever skipped."""
        monkeypatch.setenv("ANON_TRIAL_TRUSTED_PROXIES", "198.51.100.200")
        h = {anon_trial.PEER_HEADER: PROXY,
             "x-forwarded-for": "198.51.100.200, 203.0.113.92"}
        assert str(anon_trial.client_ip(h)) == "203.0.113.92"

    def test_the_trusted_proxy_variable_has_a_non_empty_default(self):
        """An empty default would make scripts/check_deploy_env.py demand it."""
        assert anon_trial._EXTRA_PROXIES_DEFAULT

    def test_ports_and_brackets_and_mapped_ipv4_are_normalised(self):
        assert str(anon_trial._parse_ip("203.0.113.9:4433")) == "203.0.113.9"
        assert str(anon_trial._parse_ip("[2001:db8::1]:443")) == "2001:db8::1"
        assert str(anon_trial._parse_ip("::ffff:203.0.113.9")) == "203.0.113.9"
        assert anon_trial._parse_ip("") is None and anon_trial._parse_ip(None) is None

    def test_unidentified_callers_share_one_allowance_rather_than_each_getting_one(self, store, clock):
        for _ in range(N):
            rpc_call({})                                   # no peer header at all
        is_err, b = body(rpc_call({"x-forwarded-for": "203.0.113.70"}))   # still unverified
        assert is_err and b["reason_code"] == "free_trial_exhausted"

    def test_a_client_cannot_use_a_forged_forwarding_header_to_reset_its_allowance(self, store, clock):
        """Same real client (the rightmost hop), a new forged value each call."""
        for i in range(N):
            rpc_call(hdrs("9.9.9.%d, 203.0.113.80" % (i + 1)))
        is_err, b = body(rpc_call(hdrs("7.7.7.7, 203.0.113.80")))
        assert is_err and b["reason_code"] == "free_trial_exhausted"


# ---------------------------------------------------------------------------
# The global daily ceiling
# ---------------------------------------------------------------------------

class TestGlobalCeiling:
    def test_a_caller_within_their_allowance_is_still_refused_at_the_ceiling(self, store, clock, monkeypatch, runs):
        monkeypatch.setenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", "3")
        for i in range(3):
            assert body(rpc_call(hdrs("203.0.113.%d" % (100 + i))))[0] is False
        is_err, b = body(rpc_call(hdrs("203.0.113.199")))    # a brand new caller
        assert is_err is True
        assert b["reason_code"] == "free_trial_daily_capacity"
        assert b["error_code"] == "rate_limited" and b["retriable"] is True
        assert 0 < b["retry_after_ms"] <= 86_400_000
        assert "/keys/request" in b["human_message"]         # a key still gets them in
        assert runs["n"] == 3

    def test_a_refusal_at_the_ceiling_consumes_nothing(self, store, clock, monkeypatch):
        monkeypatch.setenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", "1")
        rpc_call(hdrs("203.0.113.110"))
        for _ in range(4):
            rpc_call(hdrs("203.0.113.111"))
        assert store.global_count("find_business") == 1
        # The refused caller's own counter is still zero: they lose nothing.
        refused_key = anon_trial.caller_key("find_business", "v4:203.0.113.111")
        assert store.count_for_caller("find_business", refused_key) == 0

    def test_a_caller_over_their_allowance_cannot_drain_the_global_ceiling(self, store, clock):
        h = hdrs("203.0.113.120")
        for _ in range(N):
            rpc_call(h)
        used = store.global_count("find_business")
        for _ in range(50):
            rpc_call(h)                                      # hammering while refused
        assert store.global_count("find_business") == used

    def test_the_ceiling_reopens_on_the_next_utc_day(self, store, clock, monkeypatch):
        monkeypatch.setenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", "2")
        rpc_call(hdrs("203.0.113.130")); rpc_call(hdrs("203.0.113.131"))
        assert body(rpc_call(hdrs("203.0.113.132")))[1]["reason_code"] == "free_trial_daily_capacity"
        clock.day = "2026-10-01"
        is_err, _ = body(rpc_call(hdrs("203.0.113.132")))
        assert is_err is False

    def test_a_stale_clock_cannot_rewind_the_ceiling(self, store, clock, monkeypatch):
        monkeypatch.setenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", "2")
        clock.day = "2026-10-05"
        rpc_call(hdrs("203.0.113.140")); rpc_call(hdrs("203.0.113.141"))
        clock.day = "2026-10-01"                              # an older day
        is_err, b = body(rpc_call(hdrs("203.0.113.142")))
        assert is_err and b["reason_code"] == "free_trial_daily_capacity"

    def test_the_ceiling_has_a_sane_default_and_env_wins_at_call_time(self, monkeypatch):
        monkeypatch.delenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", raising=False)
        import config
        assert anon_trial.global_daily_ceiling() == config.FIND_BUSINESS_TRIAL_GLOBAL_DAILY == 1000
        monkeypatch.setenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", "42")
        assert anon_trial.global_daily_ceiling() == 42
        monkeypatch.setenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", "junk")
        assert anon_trial.global_daily_ceiling() == 1000

    def test_the_ceiling_has_a_non_empty_default_so_the_deploy_env_check_ignores_it(self):
        """scripts/check_deploy_env.py treats a variable with no/empty default as
        REQUIRED in the container. This one is a safety valve with a working
        default, so it must not become a new deploy blocker."""
        src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
        assert re.search(r'_env_int\("FIND_BUSINESS_TRIAL_GLOBAL_DAILY",\s*1000\)', src)


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

class TestFailClosed:
    def _assert_get_a_key(self, resp, runs):
        is_err, b = body(resp)
        assert is_err is True
        assert b["reason_code"] == "free_trial_unavailable"
        assert b["error_code"] == "auth_required"
        assert "/keys/request" in b["human_message"]
        assert b["how_to_resolve"]["free_key"]["url"].endswith("/keys/request")
        assert runs["n"] == 0, "the tool ran even though the trial counter was unreachable"

    def test_an_unreachable_store_refuses_and_does_not_run_the_tool(self, store, clock, runs):
        store.down = RuntimeError("rpc('anon_trial_reserve') transport error: ConnectError")
        self._assert_get_a_key(rpc_call(hdrs("203.0.113.150")), runs)

    def test_a_timeout_refuses(self, store, clock, runs):
        store.down = asyncio.TimeoutError()
        self._assert_get_a_key(rpc_call(hdrs("203.0.113.151")), runs)

    def test_a_missing_migration_refuses(self, store, clock, runs):
        """PostgREST's answer for a function that has not been applied."""
        store.down = RuntimeError('rpc(\'anon_trial_reserve\') failed: HTTP 404 body={"code":"PGRST202"}')
        self._assert_get_a_key(rpc_call(hdrs("203.0.113.152")), runs)

    def test_with_no_supabase_configured_at_all_the_real_transport_refuses(self, monkeypatch, clock, runs):
        monkeypatch.setattr(anon_trial, "_rpc", _REAL_RPC)
        for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
            monkeypatch.delenv(name, raising=False)
        self._assert_get_a_key(rpc_call(hdrs("203.0.113.153")), runs)

    @pytest.mark.parametrize("reply", [
        None,
        [],
        "ok",
        {},
        {"allowed": True},                                                     # missing fields
        {"allowed": "yes", "reason": "ok", "caller_count": 1, "global_count": 1},
        {"allowed": True, "reason": "caller_limit", "caller_count": 1, "global_count": 1},
        {"allowed": False, "reason": "ok", "caller_count": 1, "global_count": 1},
        {"allowed": True, "reason": "ok", "caller_count": True, "global_count": 1},
        {"allowed": True, "reason": "ok", "caller_count": "1", "global_count": 1},
        {"allowed": True, "reason": "surprise", "caller_count": 1, "global_count": 1},
        # admitted at a count past the limit, or at zero: the function is broken
        {"allowed": True, "reason": "ok", "caller_count": N + 1, "global_count": 1},
        {"allowed": True, "reason": "ok", "caller_count": 0, "global_count": 1},
    ])
    def test_a_reply_of_the_wrong_shape_is_never_read_as_allowed(self, reply, store, clock, runs):
        store.override = lambda fn, payload: reply
        self._assert_get_a_key(rpc_call(hdrs("203.0.113.154")), runs)

    def test_keyed_callers_are_not_affected_by_a_counter_outage(self, store, clock, runs):
        store.down = RuntimeError("transport error")
        is_err, b = body(rpc_call(hdrs("203.0.113.155", **{"x-agent-identity": make_key()})))
        assert is_err is False and b["status"] == "success"
        assert store.calls == []

    def test_the_failure_is_logged_loudly_and_names_the_missing_migration(self, store, clock, caplog):
        import logging
        store.down = RuntimeError('rpc(\'anon_trial_reserve\') failed: HTTP 404 body={"code":"PGRST202"}')
        with caplog.at_level(logging.ERROR, logger="smb_broker.anon_trial"):
            rpc_call(hdrs("203.0.113.156"))
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "FAILING CLOSED" in text and "kind=misconfigured" in text
        assert "008_anon_trial_reserve_release_rpc.sql" in text


# ---------------------------------------------------------------------------
# Only a call that succeeded is charged
# ---------------------------------------------------------------------------

class TestOnlySuccessCounts:
    def test_calls_rejected_for_bad_arguments_cost_nothing(self, store, clock):
        h = hdrs("203.0.113.160")
        for _ in range(N + 5):                               # missing required 'vertical'
            resp = rpc_call(h, args={"location": {"zip_or_city": "Atlanta"}})
            assert "error" in resp                           # -32602 invalid params
        assert list(caller_rows(store).values()) == [0]
        assert store.global_count("find_business") == 0
        # ... and the full allowance is still there afterwards.
        for i in range(N):
            assert body(rpc_call(h))[0] is False, f"good call {i + 1} was refused"
        assert body(rpc_call(h))[0] is True

    def test_a_handler_that_raises_gives_the_slot_back(self, store, clock, monkeypatch):
        import core.find_business as fb

        async def boom(*a, **k):
            raise RuntimeError("upstream fell over")

        monkeypatch.setattr(fb, "handle_find_business", boom)
        resp = rpc_call(hdrs("203.0.113.161"))
        assert "error" in resp                               # surfaced as before
        assert list(caller_rows(store).values()) == [0]
        assert store.global_count("find_business") == 0

    def test_a_failure_receipt_gives_the_slot_back(self, store, clock, monkeypatch):
        import core.find_business as fb
        from core.models import OperationStatus
        real = fb.handle_find_business

        async def failing(*a, **k):
            r = await real(*a, **k)
            return r.model_copy(update={"status": OperationStatus.FAILURE})

        monkeypatch.setattr(fb, "handle_find_business", failing)
        is_err, b = body(rpc_call(hdrs("203.0.113.162")))
        assert is_err is True and b["status"] == "failure"
        assert "free_trial" not in b                          # not a trial-charged success
        assert list(caller_rows(store).values()) == [0]
        assert store.global_count("find_business") == 0

    def test_a_success_is_charged_exactly_once(self, store, clock):
        h = hdrs("203.0.113.163")
        for i in range(1, 6):
            rpc_call(h)
            assert list(caller_rows(store).values()) == [i]
            assert store.global_count("find_business") == i

    def test_if_the_release_cannot_be_reached_the_caller_keeps_the_loss_and_nothing_raises(
            self, store, clock, caplog, monkeypatch):
        import logging
        # reserve works; release is down
        real_call = store.__call__

        async def flaky(fn, payload):
            if fn == "anon_trial_release":
                raise RuntimeError("transport error")
            return await real_call(fn, payload)

        monkeypatch.setattr(anon_trial, "_rpc", flaky)
        with caplog.at_level(logging.ERROR, logger="smb_broker.anon_trial"):
            resp = rpc_call(hdrs("203.0.113.164"), args={"location": {"zip_or_city": "x"}})
        assert "error" in resp                               # the ORIGINAL error, not ours
        assert list(caller_rows(store).values()) == [1]      # conservative
        assert "anon_trial_release_failed" in " ".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Keyed behaviour is unchanged
# ---------------------------------------------------------------------------

class TestKeyedUnchanged:
    def test_a_valid_key_is_never_counted_and_never_limited(self, store, clock, runs):
        h = hdrs("203.0.113.170", **{"x-agent-identity": make_key()})
        for _ in range(N + 5):
            is_err, b = body(rpc_call(h))
            assert is_err is False and b["status"] == "success"
            assert "free_trial" not in b
        assert store.calls == [], "a keyed call touched the trial counter"
        assert runs["n"] == N + 5

    def test_a_key_sent_as_a_bearer_token_counts_as_a_key_too(self, store, clock):
        h = hdrs("203.0.113.171", authorization="Bearer " + make_key())
        for _ in range(N + 2):
            assert body(rpc_call(h))[0] is False
        assert store.calls == []

    def test_a_key_sent_as_x_api_key_counts_as_a_key_too(self, store, clock):
        h = hdrs("203.0.113.172", **{"x-api-key": make_key()})
        for _ in range(N + 2):
            assert body(rpc_call(h))[0] is False
        assert store.calls == []

    def test_a_forged_or_garbage_key_is_not_a_key(self, store, clock):
        h = hdrs("203.0.113.173", **{"x-agent-identity": "not.a.real.token"})
        for _ in range(N):
            assert body(rpc_call(h))[0] is False
        is_err, b = body(rpc_call(h))
        assert is_err and b["reason_code"] == "free_trial_exhausted"

    def test_the_word_anonymous_is_not_a_key(self, store, clock):
        h = hdrs("203.0.113.174", **{"x-agent-identity": "anonymous"})
        rpc_call(h)
        assert [c[0] for c in store.calls] == ["anon_trial_reserve"]

    def test_a_keyed_caller_with_the_same_ip_as_an_exhausted_stranger_is_unaffected(self, store, clock):
        stranger = hdrs("203.0.113.175")
        for _ in range(N):
            rpc_call(stranger)
        assert body(rpc_call(stranger))[0] is True
        keyed = hdrs("203.0.113.175", **{"x-agent-identity": make_key()})
        assert body(rpc_call(keyed))[0] is False

    def test_other_tools_are_untouched_by_the_gate(self, store, clock):
        for tool, args in (("verify_business", {"smb_id": "smb_001"}),
                           ("preview_cost", {"operation": "find_business"})):
            rpc_call(hdrs("203.0.113.176"), args=args, tool=tool)
        assert store.calls == [], "the trial gate touched a tool that is not part of the trial"


# ---------------------------------------------------------------------------
# Discovery traffic costs nothing
# ---------------------------------------------------------------------------

class TestDiscoveryIsFree:
    @pytest.mark.parametrize("method", ["initialize", "tools/list", "resources/list",
                                        "prompts/list", "ping"])
    def test_listing_methods_never_touch_the_counter(self, method, store, clock):
        h = hdrs("203.0.113.180", **{"user-agent": "Smithery/1.0 python-httpx"})
        for _ in range(N + 3):
            resp = rpc_call(h, method=method)
            assert "result" in resp
        assert store.calls == []

    def test_a_crawler_that_only_lists_tools_never_uses_up_a_single_slot(self, store, clock):
        crawler = hdrs("203.0.113.181", **{"user-agent": "glama-bot/2.0"})
        for method in ("initialize", "tools/list", "prompts/list", "resources/list"):
            rpc_call(crawler, method=method)
        # the allowance is intact for the first real call from that address
        is_err, b = body(rpc_call(crawler))
        assert is_err is False and b["free_trial"]["calls_used"] == 1

    def test_a_crawler_user_agent_is_NOT_a_way_around_the_limit(self, store, clock):
        """Deliberate. The crawler classification keys on the User-Agent, and
        `curl/` is on that list: exempting it would let anyone be unlimited by
        typing a header. Discovery is free because it never reaches the gate,
        not because of who is asking."""
        h = hdrs("203.0.113.182", **{"user-agent": "curl/8.5.0"})
        for _ in range(N):
            rpc_call(h)
        is_err, b = body(rpc_call(h))
        assert is_err and b["reason_code"] == "free_trial_exhausted"


# ---------------------------------------------------------------------------
# Every door that reaches find_business
# ---------------------------------------------------------------------------

class TestEveryDoor:
    def test_a_capability_door_shares_the_same_allowance_as_the_full_server(self, store, clock):
        h = hdrs("203.0.113.190")
        for _ in range(N // 2):
            rpc_call(h)
        for _ in range(N - N // 2):
            body(rpc_call(h, profile="appointment-booking"))
        is_err, b = body(rpc_call(h))
        assert is_err and b["reason_code"] == "free_trial_exhausted"
        is_err2, b2 = body(rpc_call(h, profile="appointment-booking"))
        assert is_err2 and b2["reason_code"] == "free_trial_exhausted"

    def _client(self, peer_host):
        from fastapi.testclient import TestClient
        import main as rest_main
        return TestClient(rest_main.app, client=(peer_host, 50000))

    REST_BODY = {"vertical": "personal_services", "location": {"zip_or_city": "Atlanta"}}

    def test_the_rest_door_is_gated_the_same_way(self, store, clock):
        c = self._client("203.0.113.191")
        for i in range(1, N + 1):
            r = c.post("/ops/find_business", json=self.REST_BODY)
            assert r.status_code == 200, r.text
            assert r.headers["X-Free-Trial-Calls-Remaining"] == str(N - i)
        r = c.post("/ops/find_business", json=self.REST_BODY)
        assert r.status_code == 401
        d = r.json()["detail"]
        assert d["reason_code"] == "free_trial_exhausted"
        assert d["how_to_resolve"]["free_key"]["url"].endswith("/keys/request")

    def test_the_rest_door_and_mcp_share_one_allowance(self, store, clock):
        """The caller refused on /mcp must not simply post to /ops."""
        c = self._client("203.0.113.192")
        for _ in range(N):
            rpc_call(hdrs("203.0.113.192", peer="203.0.113.192"))   # direct, same address
        assert c.post("/ops/find_business", json=self.REST_BODY).status_code == 401

    def test_the_rest_door_reports_the_daily_ceiling_as_429_with_retry_after(self, store, clock, monkeypatch):
        monkeypatch.setenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", "1")
        assert self._client("203.0.113.193").post("/ops/find_business", json=self.REST_BODY).status_code == 200
        r = self._client("203.0.113.194").post("/ops/find_business", json=self.REST_BODY)
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 0

    def test_the_rest_door_fails_closed(self, store, clock):
        store.down = RuntimeError("transport error")
        r = self._client("203.0.113.195").post("/ops/find_business", json=self.REST_BODY)
        assert r.status_code == 401
        assert r.json()["detail"]["reason_code"] == "free_trial_unavailable"

    def test_the_rest_door_lets_a_keyed_caller_straight_through(self, store, clock):
        c = self._client("203.0.113.196")
        for _ in range(N + 2):
            r = c.post("/ops/find_business", json=self.REST_BODY,
                       headers={"X-Agent-Identity": make_key()})
            assert r.status_code == 200
            assert "X-Free-Trial-Calls-Remaining" not in r.headers
        assert store.calls == []

    def test_a_client_cannot_pre_fill_the_peer_field_that_decides_who_is_believed(self, store, clock):
        """main.py stamps the socket peer over anything the client sent."""
        c = self._client("203.0.113.197")
        c.post("/ops/find_business", json=self.REST_BODY,
               headers={"x-hl-peer-addr": "8.8.8.8", "X-Forwarded-For": "8.8.4.4"})
        (call,) = store.calls
        assert call[1]["p_caller_key"] == anon_trial.caller_key("find_business", "v4:203.0.113.197")

    def test_headers_with_peer_strips_every_casing_of_the_field(self):
        import main as rest_main

        class _Req:
            headers = {"X-HL-Peer-Addr": "8.8.8.8", "x-hl-peer-addr": "8.8.4.4", "accept": "*/*"}

            class client:
                host = "203.0.113.198"

        h = rest_main._headers_with_peer(_Req())
        assert h[anon_trial.PEER_HEADER] == "203.0.113.198"
        assert [k for k in h if k.lower() == anon_trial.PEER_HEADER] == [anon_trial.PEER_HEADER]

    def test_every_trial_tool_is_gated_on_both_doors(self):
        """Wiring guard: adding a tool to TRIAL_TOOLS without wiring it into
        the MCP dispatcher AND its /ops route would advertise a limit nobody
        enforces. The MCP dispatcher gates by the set; the REST route is
        per-tool, so it is checked here."""
        main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        mcp_src = open(os.path.join(ROOT, "agent_interface", "mcp_server.py"),
                       encoding="utf-8").read()
        assert "_anon_trial.admit(name, headers or {})" in mcp_src
        for tool in tool_auth.TRIAL_TOOLS:
            m = re.search(r'@app\.post\("/ops/%s".*?\n(?=@app\.)' % re.escape(tool),
                          main_src, flags=re.S)
            assert m, f"no /ops/{tool} route found in main.py"
            assert f'_anon_trial.admit("{tool}"' in m.group(0), (
                f"/ops/{tool} is a trial tool but its REST route does not call "
                f"billing.anon_trial.admit - the limit is a suggestion there")


# ---------------------------------------------------------------------------
# Restart durability
# ---------------------------------------------------------------------------

class TestRestartDurability:
    def test_counts_live_in_the_store_not_in_the_process(self, store, clock, monkeypatch):
        h = hdrs("203.0.113.200")
        for _ in range(3):
            rpc_call(h)
        assert list(caller_rows(store).values()) == [3]

        # A restart: every in-process object is gone, the database is not.
        importlib.reload(anon_trial)
        monkeypatch.setattr(anon_trial, "_rpc", store)
        monkeypatch.setattr(anon_trial, "_today_utc", lambda: clock.day)

        is_err, b = body(rpc_call(h))
        assert is_err is False
        assert b["free_trial"]["calls_used"] == 4, "the allowance restarted with the process"
        for _ in range(N - 4):
            rpc_call(h)
        is_err, b = body(rpc_call(h))
        assert is_err and b["reason_code"] == "free_trial_exhausted"

    def test_an_exhausted_caller_stays_exhausted_across_a_restart(self, store, clock, monkeypatch):
        h = hdrs("203.0.113.201")
        for _ in range(N):
            rpc_call(h)
        importlib.reload(anon_trial)
        monkeypatch.setattr(anon_trial, "_rpc", store)
        monkeypatch.setattr(anon_trial, "_today_utc", lambda: clock.day)
        assert body(rpc_call(h))[0] is True

    def test_the_global_count_survives_a_restart_too(self, store, clock, monkeypatch):
        monkeypatch.setenv("FIND_BUSINESS_TRIAL_GLOBAL_DAILY", "2")
        rpc_call(hdrs("203.0.113.202")); rpc_call(hdrs("203.0.113.203"))
        importlib.reload(anon_trial)
        monkeypatch.setattr(anon_trial, "_rpc", store)
        monkeypatch.setattr(anon_trial, "_today_utc", lambda: clock.day)
        assert body(rpc_call(hdrs("203.0.113.204")))[1]["reason_code"] == "free_trial_daily_capacity"

    def test_the_module_keeps_no_mutable_state_that_could_hold_a_counter(self):
        mutable = [k for k, v in vars(anon_trial).items()
                   if isinstance(v, (dict, list, set)) and not k.startswith("__")]
        assert mutable == [], (
            f"billing/anon_trial.py holds mutable module state {mutable}; a counter "
            f"kept there would reset on every restart and deploy")

    def test_only_primitives_and_hashes_are_sent_to_the_store(self, store, clock):
        rpc_call(hdrs("203.0.113.205", **{"user-agent": "x"}))
        (fn, payload), = store.calls
        assert fn == "anon_trial_reserve"
        assert set(payload) == {"p_tool", "p_caller_key", "p_day", "p_caller_limit", "p_global_limit"}
        assert payload["p_caller_limit"] == N
        assert re.fullmatch(r"[0-9a-f]{64}", payload["p_caller_key"])


# ---------------------------------------------------------------------------
# What the machine-readable surfaces say
# ---------------------------------------------------------------------------

class TestSurfacesTellTheTruth:
    def test_the_classification_is_its_own_class(self):
        assert tool_auth.auth_class("find_business") == "trial"
        assert tool_auth.TRIAL_TOOLS == {"find_business"}
        assert tool_auth.partition_is_sound() is None
        assert tool_auth.trial_free() == 1
        assert tool_auth.keyless() + tool_auth.quota_free() + tool_auth.trial_free() \
            == tool_auth.usable_without_key()
        assert not tool_auth.requires_key("find_business")     # a stranger CAN call it

    def test_the_tool_list_tag_promises_only_what_the_gate_enforces(self):
        resp = rpc_call({}, method="tools/list")
        desc = {t["name"]: t["description"] for t in resp["result"]["tools"]}["find_business"]
        assert "[free, no key]" not in desc, "unlimited-keyless promise for a limited tool"
        assert f"no key for your first {N} calls" in desc
        assert "free key" in desc and "stay free" in desc
        # and the tools that ARE unlimited still say so
        others = [d for n, d in ((t["name"], t["description"]) for t in resp["result"]["tools"])
                  if n == "verify_business"]
        assert "[free, no key]" in others[0]

    def test_the_mcp_descriptor_lists_it_as_a_trial_tool_not_a_free_tool(self):
        from agent_interface.well_known import get_mcp_descriptor
        p = get_mcp_descriptor()["payments"]
        assert "find_business" not in p["free_tools"]
        assert p["trial_tools"] == ["find_business"]
        assert p["trial"]["keyless_calls_per_caller"] == N
        assert p["trial"]["get_a_key"]["url"].endswith("/keys/request")
        assert p["trial"]["header"] == "X-Agent-Identity"
        assert "find_business" not in p["free_with_key_tools"]
        # the note's arithmetic still adds up
        usable = len(p["free_tools"]) + len(p["quota_free_tools"]) + len(p["trial_tools"])
        assert f"{usable} usable without signing up" in p["note"]
        assert "FOUR NUMBERS" in p["note"] and "find_business" in p["note"]

    def test_the_descriptor_number_matches_the_derived_number(self):
        from agent_interface.well_known import get_mcp_descriptor
        p = get_mcp_descriptor()["payments"]
        usable = len(p["free_tools"]) + len(p["quota_free_tools"]) + len(p["trial_tools"])
        assert usable == tool_auth.usable_without_key()
        assert len(p["free_tools"]) == tool_auth.keyless()

    def test_the_handshake_instructions_name_the_trial(self):
        resp = rpc_call({}, method="initialize")
        text = resp["result"]["instructions"]
        assert f"first {N} calls" in text and "find_business" in text
        assert f"{tool_auth.keyless()} always free" in text
        assert f"{tool_auth.trial_free()} free for your first" in text

    def test_the_one_sentence_every_surface_uses_says_it(self):
        s = tool_auth.free_tier_sentence()
        assert f"{tool_auth.trial_free()} free for your first {N} calls" in s
        assert f"{tool_auth.usable_without_key()} of the {tool_auth.total_tools()} tools work with no key" in s
        assert f"{N}-call trial" in tool_auth.auth_note()

    def test_cost_sentences_do_not_call_it_unconditionally_keyless(self):
        from agent_interface.well_known import describe_cost
        free = {"basis": "free"}
        assert "no key required" in describe_cost(free, "verify_business")
        s = describe_cost(free, "find_business")
        assert f"first {N} calls" in s and "no key required" not in s
        llms = importlib.import_module("agent_interface.well_known").get_llms_txt()
        section = llms.split("### find_business", 1)[1].split("### ", 1)[0]
        assert f"first {N} calls" in section and "(no key required)" not in section

    def test_the_a2a_card_no_longer_lists_it_among_the_unlimited_free_tools(self):
        from agent_interface.well_known import get_agent_card
        desc = get_agent_card()["description"]
        assert "Read tools (find_business" not in desc
        assert f"find_business is free with no key for your first {N} calls" in desc

    def test_the_registry_catalogues_carry_the_trial_terms(self):
        for rel in ("smithery.yaml", "glama.json"):
            text = re.sub(r"\s+", " ", open(os.path.join(ROOT, rel), encoding="utf-8").read())
            assert "find_business" in text, rel
            assert f"first {N} calls" in text, f"{rel} does not state the trial terms"
        # generated from the registry source with tokens, not typed
        src = open(os.path.join(ROOT, "registry", "servers.yaml"), encoding="utf-8").read()
        assert "{n_trial}" in src and "{n_trial_calls}" in src

    def test_the_readme_states_the_same_number_the_gate_enforces(self):
        readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        assert f"free for your first {N} calls without a key" in readme
        assert f"find_business` gives every caller {N} free calls with no key" in readme

    def test_the_auth_required_error_no_longer_promises_find_business_unlimited(self, monkeypatch):
        """The most consequential error we serve used to say read-only tools
        including find_business 'stay free'; that is now only true with a key
        after the trial."""
        import config
        monkeypatch.setattr(config, "REQUIRE_AUTH", True)
        monkeypatch.setenv("POLAR_CHECKOUT_URL", "https://example.test/checkout")
        resp = rpc_call({}, tool="send_message",
                        args={"channel": "sms", "recipient": "+15550100", "message": "x"})
        text = json.dumps(resp)
        assert "find_business, verify_business, preview_cost, get_status) stay free" not in text
        assert f"find_business is free with no key for your first {N} calls" in text


# ---------------------------------------------------------------------------
# The migration file (only present inside the full hatchloop workspace)
# ---------------------------------------------------------------------------

def _sql_path():
    override = os.environ.get("HATCHLOOP_SQL_DIR")
    base = override or os.path.join(os.path.dirname(ROOT), "sql", "agentbroker")
    p = os.path.join(base, "008_anon_trial_reserve_release_rpc.sql")
    return p if os.path.isfile(p) else None


@pytest.fixture(scope="module")
def sql():
    # Normalised: the workspace copy may carry CRLF on Windows checkouts.
    return open(_sql_path(), encoding="utf-8").read().replace("\r\n", "\n")


@pytest.mark.skipif(_sql_path() is None,
                    reason="sql/ lives in the hatchloop workspace, outside the public "
                           "agentbroker repo; set HATCHLOOP_SQL_DIR to check it here")
class TestMigrationStructure:
    def _body(self, sql, name):
        m = re.search(r"create or replace function public\.%s\(.*?\n\$\$;" % name, sql, flags=re.S)
        assert m, f"{name} is not defined"
        return m.group(0)

    def test_both_functions_are_narrow_security_definer_and_anon_executable(self, sql):
        for name, sig in (("anon_trial_reserve", "text, text, text, integer, integer"),
                          ("anon_trial_release", "text, text, text")):
            b = self._body(sql, name)
            assert "security definer" in b
            assert "set search_path = public" in b, "a definer function without a pinned search_path"
            assert re.search(r"revoke all on function public\.%s\(%s\)\s+from public" % (name, sig), sql)
            assert re.search(r"grant execute on function public\.%s\(%s\)\s+to anon" % (name, sig), sql)

    def test_it_creates_no_table_and_changes_no_table_grant(self, sql):
        code = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
        assert not re.search(r"\b(create|alter|drop)\s+(unlogged\s+|temp\w*\s+)?table\b", code, re.I)
        assert not re.search(r"\b(grant|revoke)\b[^;]*\bon\s+(table\s+)?anon_data_quota\b", code, re.I)

    def test_it_rejects_malformed_input_with_the_patterns_the_model_uses(self, sql):
        for pat in (r"^[a-z][a-z0-9_]{0,63}$", r"^[0-9a-f]{64}$", r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"):
            assert pat in sql, f"{pat} missing from the migration"
        assert "errcode = '22023'" in sql

    def test_lock_order_is_global_then_caller_in_both_functions(self, sql):
        for name in ("anon_trial_reserve", "anon_trial_release"):
            b = self._body(sql, name)
            g = b.index("bucket_key = v_gkey\n    for update")
            c = b.index("bucket_key = v_ckey\n    for update")
            assert g < c, f"{name} locks the caller row before the global row - deadlock risk"

    def test_a_refusal_writes_no_counter(self, sql):
        b = self._body(sql, "anon_trial_reserve")
        first_bump = b.index("set count = v_ccount + 1")
        for reason in ("'caller_limit'", "'global_limit'"):
            assert b.index(reason) < first_bump, (
                f"the {reason} refusal comes after a counter is bumped")

    def test_the_day_only_rolls_forward(self, sql):
        assert "p_day > v_gdate" in self._body(sql, "anon_trial_reserve")

    def test_the_caller_allowance_is_lifetime_and_the_global_one_is_daily(self, sql):
        b = self._body(sql, "anon_trial_reserve")
        assert "'lifetime'" in b
        assert "trial:g:" in b and "trial:c:" in b

    def test_release_never_creates_a_row_and_never_goes_below_zero(self, sql):
        b = self._body(sql, "anon_trial_release")
        assert "insert into" not in b
        assert "greatest(count - 1, 0)" in b
        assert "v_ccount <= 0" in b

    def test_the_model_and_the_sql_agree_on_the_reply_vocabulary(self, sql):
        b = self._body(sql, "anon_trial_reserve")
        for word in ("'allowed'", "'reason'", "'caller_count'", "'global_count'",
                     "'ok'", "'caller_limit'", "'global_limit'"):
            assert word in b
        assert "'released'" in self._body(sql, "anon_trial_release")
