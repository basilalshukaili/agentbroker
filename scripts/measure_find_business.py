#!/usr/bin/env python3
"""Measure find_business on a FIXED sample: success rate and latency, before and after a change.

    python scripts/measure_find_business.py --label before --root C:/path/to/base/checkout --out before.json
    python scripts/measure_find_business.py --label after  --out after.json
    python scripts/measure_find_business.py --compare before.json after.json

WHAT THIS TALKS TO. The PUBLIC OpenStreetMap servers (Nominatim and Overpass), through the checkout's own
client with its own politeness rules (identifying User-Agent, 1 request per second, concurrency cap). It is
a one-off measurement tool, NOT a test: nothing in the suite imports it and CI must never run it. Run it a
few times a day at most. It writes nothing to the spine, sends nothing, and reads no credentials: the
process is started without the service's environment and `_finish_request` has nowhere to log to.

WHAT IS MEASURED. The call goes through `agent_interface.mcp_server.handle_mcp_request` - the function the
HTTP layer calls - so argument handling, the budget and the response shape are the real ones, not a
re-implementation. `--root` points at any checkout (the "before" run uses an export of the commit that is
live), so the same script and the same sample score both versions.

TWO UPSTREAMS. By default the live public servers. They are volunteer-run and their speed on a given
afternoon dominates any measurement of OUR code: on 2026-10-03 the same checkout was scored while
Overpass answered in 2-25 s with about a third 5xx, and then while it did not answer at all. So
`--simulate` replaces the two servers with a deterministic stand-in whose Overpass latencies and failure
rate are taken from the live sample (see SIM_OVERPASS), and the SAME stand-in serves the "before" and the
"after" checkout. That isolates what the change does from what the upstream is doing. The live run is the
field check; the simulated run is the like-for-like comparison. Neither is the other.

THE SAMPLE, AND WHAT IT IS NOT. Two parts, fixed in this file so a rerun is comparable:

  OUTCOME (26 calls) reproduces the SHAPE of the 26 external find_business calls in the 47-hour
  window of docs/reviews/2026-10-03-mcp-demand-evidence.md: 11 with no arguments, 15 that sent
  `location` and `vertical` (5 of those worked, 10 failed validation). That window recorded argument
  NAMES only, never values, so the 10 failing shapes below are a RECONSTRUCTION of the mistakes an
  agent can make against the published schema (a bare string for `location`, a plural or unlisted
  `vertical`, a top-level `city` that the schema advertises). They are labelled `reconstructed`. The 5
  working calls are real lookups. Treat the before/after delta on the reconstructed shapes as "what the
  new input handling does with these mistakes", not as a replay of what callers actually sent.

  LATENCY (20 lookups) is 20 distinct, valid place x category pairs, dense and sparse, so p50 and p95
  rest on enough real upstream round trips to mean something. Duplicates would measure the cache.

OUTCOME CLASSES, so "success" cannot be quietly redefined:
  complete      a result: businesses found, or an honest empty answer from a search that ran to the end
  wrong_place   a result for a DIFFERENT place than the one asked about (never counted as usable)
  partial       the call returned inside its budget with what was ready and said the search continues
  guided_error  a refusal that names what was wrong AND carries a worked example (isError result)
  bare_error    a refusal with no example (a JSON-RPC -32602 with a field name and nothing else)
  unavailable   the upstream could not answer in time and nothing usable came back
`usable` = complete + partial. The no-argument probes can never be `usable` - there is no place to
search - so the report also gives the rate over the calls that carried enough information to search.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time

# --------------------------------------------------------------------------- the fixed sample

# (label, arguments, reconstructed?)  -- the 5 calls that carried a place and a kind in the right shape.
WORKING_5 = [
    ("canonical:muscat-dentist", {"vertical": "professional_services",
                                  "location": {"zip_or_city": "Muscat, Oman"}, "capability": "dentist"}, False),
    ("canonical:nizwa-plumber", {"vertical": "home_services",
                                 "location": {"zip_or_city": "Nizwa, Oman"}, "capability": "plumber"}, False),
    ("canonical:atlanta-restaurant", {"vertical": "personal_services",
                                      "location": {"zip_or_city": "Atlanta, Georgia"},
                                      "capability": "restaurant", "max_results": 5}, False),
    ("canonical:london-pharmacy", {"vertical": "professional_services",
                                   "location": {"zip_or_city": "London, United Kingdom"},
                                   "capability": "pharmacy", "max_results": 5}, False),
    ("canonical:dubai-hairdresser", {"vertical": "personal_services",
                                     "location": {"zip_or_city": "Dubai, United Arab Emirates"},
                                     "capability": "hairdresser", "max_results": 3}, False),
]

# Ten ways to get the schema wrong, each of which the live server answered -32602 to on 2026-10-03.
RECONSTRUCTED_10 = [
    ("string-location:muscat-restaurant", {"vertical": "restaurant", "location": "Muscat, Oman"}, True),
    ("string-location:salalah-dentist", {"vertical": "professional_services", "location": "Salalah, Oman",
                                         "capability": "dentist"}, True),
    ("string-location:sohar-plumber", {"vertical": "home_services", "location": "Sohar, Oman",
                                       "capability": "plumber", "max_results": 8}, True),
    ("plural-vertical:abudhabi-restaurants", {"vertical": "restaurants",
                                              "location": {"zip_or_city": "Abu Dhabi, United Arab Emirates"}}, True),
    ("plural-vertical:boston-lawyers", {"vertical": "lawyers",
                                        "location": {"zip_or_city": "Boston, Massachusetts"}}, True),
    ("unlisted-vertical:austin-cafe", {"vertical": "cafe",
                                       "location": {"zip_or_city": "Austin, Texas"}, "max_results": 8}, True),
    ("unlisted-vertical:berlin-clinic", {"vertical": "clinic",
                                         "location": {"zip_or_city": "Berlin, Germany"}, "max_results": 8}, True),
    ("top-level-city:muscat-pharmacy", {"vertical": "professional_services", "city": "Muscat",
                                        "capability": "pharmacy"}, True),
    ("top-level-city:nizwa-hairdresser", {"vertical": "personal_services", "city": "Nizwa", "region": "Ad Dakhiliyah",
                                          "capability": "hairdresser", "max_results": 8}, True),
    ("kind-only-in-vertical:dubai-bakery", {"vertical": "bakery", "location": "Dubai", "max_results": 8}, True),
]

NO_ARGS_11 = [(f"no-args:{i + 1}", {}, False) for i in range(11)]

OUTCOME_SAMPLE = WORKING_5 + RECONSTRUCTED_10 + NO_ARGS_11

# 20 distinct valid lookups: 10 places x 2 kinds. Dense kinds in dense cities are deliberately included.
_PLACES = ["Muscat, Oman", "Nizwa, Oman", "Salalah, Oman", "Sohar, Oman", "Dubai, United Arab Emirates",
           "Abu Dhabi, United Arab Emirates", "Atlanta, Georgia", "Boston, Massachusetts",
           "London, United Kingdom", "Berlin, Germany"]
_KINDS = [("personal_services", "hairdresser"), ("home_services", "plumber"),
          ("professional_services", "dentist"), ("personal_services", "restaurant"),
          ("professional_services", "lawyer")]
LATENCY_SAMPLE = [
    (f"lookup:{p.split(',')[0].lower().replace(' ', '-')}-{_KINDS[(i * 2 + j) % len(_KINDS)][1]}",
     {"vertical": _KINDS[(i * 2 + j) % len(_KINDS)][0], "location": {"zip_or_city": p},
      "capability": _KINDS[(i * 2 + j) % len(_KINDS)][1], "max_results": 5}, False)
    for i, p in enumerate(_PLACES) for j in range(2)
]

# --------------------------------------------------------------------------- simulated upstream

# Overpass answers, in the order requests arrive, as (seconds, http_status). The seconds are the
# Overpass-side durations observed in the live "before" run of 2026-10-03 (each lookup's wall time minus
# about a second of geocoding), rounded; 5 of the 20 were 5xx / timeouts (25%), which is lower than that
# afternoon's worst and in line with the evidence report's 5-of-26 completion. Same list, same order,
# for every checkout measured.
SIM_OVERPASS = [(6.8, 200), (24.0, 504), (15.6, 200), (16.0, 200), (4.6, 200), (12.3, 200), (2.3, 200),
                (9.4, 504), (1.8, 200), (9.5, 200), (1.0, 200), (23.0, 504), (7.0, 200), (12.8, 200),
                (6.2, 200), (3.3, 200), (12.1, 504), (10.5, 200), (10.2, 200), (19.0, 504)]
SIM_NOMINATIM_S = 0.35
_SIM_DENSE = ("atlanta", "london", "dubai", "boston", "berlin")


def install_simulated_upstream(osm_client_module, scale: float = 1.0) -> None:
    """Replace the public servers with the deterministic stand-in described above.

    Nominatim resolves ANY text to a point whose name echoes the text (so a call for one town that is
    answered for another is still caught by the place check). Overpass returns 150 businesses (a capped,
    dense answer that forces narrowing) for the dense towns at a wide radius, and 40 otherwise. It reads
    the real query text, so a narrowed search is a different request with its own latency."""
    import asyncio as _a
    import re
    import zlib

    import httpx

    # Which of SIM_OVERPASS a query gets is decided by the QUERY TEXT, not by arrival order, so the same
    # request meets the same upstream in every checkout even though a different version issues a
    # different number of requests (narrowing, background lookups). Retries of one query rotate.
    attempts: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "nominatim" in request.url.host:
            await _a.sleep(SIM_NOMINATIM_S * scale)
            q = request.url.params.get("q", "")
            h = zlib.crc32(q.casefold().encode("utf-8")) % 1000
            return httpx.Response(200, json=[{"lat": str(10 + h / 100.0), "lon": str(20 + h / 100.0),
                                               "display_name": f"{q}, Simland", "osm_type": "node", "osm_id": 1 + h}])
        form = dict(httpx.QueryParams(request.content.decode("utf-8")))
        ql = form.get("data", "")
        m = re.search(r"\(around:(\d+),(-?[\d.]+),(-?[\d.]+)\)", ql)
        radius, lat, lon = (int(m.group(1)), float(m.group(2)), float(m.group(3))) if m else (5000, 0.0, 0.0)
        k = attempts.get(ql, 0)
        attempts[ql] = k + 1
        secs, status = SIM_OVERPASS[(zlib.crc32(ql.encode("utf-8")) + 7 * k) % len(SIM_OVERPASS)]
        await _a.sleep(secs * scale)
        if status != 200:
            return httpx.Response(status)
        dense = radius >= 3000 and int(abs(lat * 100)) % 5 in (0, 1)       # about 40% of towns are dense
        n = 150 if dense else 40
        els = [{"type": "node", "id": 10_000 + i, "lat": lat + (i + 1) * 0.0001, "lon": lon,
                "tags": {"name": f"Sim Business {i}", "amenity": "restaurant"}} for i in range(n)]
        return httpx.Response(200, json={"elements": els})

    client = osm_client_module.OSMClient(transport=httpx.MockTransport(handler))
    osm_client_module.set_client(client)


# --------------------------------------------------------------------------- running a call


async def _call(handle, arguments: dict, rid: int) -> tuple[float, dict]:
    payload = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
               "params": {"name": "find_business", "arguments": arguments}}
    t0 = time.monotonic()
    resp = await handle(payload, {"user-agent": "measure_find_business/1.0 (internal)"})
    return time.monotonic() - t0, resp if isinstance(resp, dict) else {"_raw": resp}


def classify(resp: dict) -> dict:
    """Turn one JSON-RPC response into a class plus the few fields worth keeping. Works on the response
    shape of both the old and the new code; anything it cannot read is `unclassified`, never `complete`."""
    out: dict = {"class": "unclassified"}
    err = resp.get("error")
    if isinstance(err, dict):
        msg = str(err.get("message", ""))
        out.update({"class": "bare_error", "rpc_code": err.get("code"), "message": msg[:160]})
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        if "example" in json.dumps(data).lower():
            out["class"] = "guided_error"
        return out
    result = resp.get("result") or {}
    try:
        body = json.loads((result.get("content") or [{}])[0].get("text", "{}"))
    except (ValueError, TypeError, IndexError):
        return out
    if result.get("isError"):
        has_example = "example" in json.dumps(body.get("how_to_resolve") or {}).lower()
        code = body.get("error_code") or body.get("reason_code")
        if code in ("osm_temporarily_unavailable", "rate_limited"):
            out.update({"class": "unavailable", "reason": code})
        else:
            out.update({"class": "guided_error" if has_example else "bare_error", "reason": code,
                        "message": str(body.get("human_message", ""))[:160]})
        return out
    res = body.get("result") or {}
    search = res.get("search") or {}
    status = body.get("status")
    n = len(res.get("businesses") or [])
    out.update({"status": status, "reason": body.get("reason_code"), "search_status": search.get("status"),
                "search_reason": search.get("reason"),
                "n": n, "cached": search.get("cached"), "searched_near": (search.get("geocoded_place") or {}).get("display_name")})
    if status == "partial" or search.get("status") == "pending":
        out["class"] = "partial"
    elif status == "success":
        out["class"] = "complete"
    elif body.get("reason_code") == "osm_temporarily_unavailable":
        out["class"] = "unavailable"
    return out


def expected_place(arguments: dict) -> str | None:
    """The town the caller asked about, lower-cased: the first comma-separated part of whatever place
    argument was sent. Used to catch the most expensive wrong answer - a confident success for a
    DIFFERENT place (the live server answered `{"city": "Muscat"}` with Atlanta on 2026-10-03)."""
    loc = arguments.get("location")
    text = loc.get("zip_or_city") if isinstance(loc, dict) else loc if isinstance(loc, str) else arguments.get("city")
    return str(text).split(",")[0].strip().lower() if text else None


def apply_place_check(row: dict, arguments: dict) -> dict:
    """A `complete` answer whose geocoded place does not contain the asked-for town is `wrong_place`.
    It is never counted as usable: returning businesses for the wrong city is worse than an error."""
    want = expected_place(arguments)
    near = (row.get("searched_near") or "").lower()
    if row.get("class") == "complete" and want and near and want not in near:
        row["class"] = "wrong_place"
        row["wanted_place"] = want
    return row


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return round(s[k], 3)


async def settle(max_wait_s: float) -> float:
    """Wait for lookups the code under test left running in the background, and return how long that took.

    Callers in the real world arrive one at a time most of the time. Without this the harness fires
    distinct lookups back to back while the previous ones are still running upstream, and measures
    the public server's two-concurrent-request limit (supply/osm_client.MAX_CONCURRENT_UPSTREAM) instead
    of the change: every later call is refused `busy`. That burst case is real, and nothing in this
    change widens it - it is bounded by the upstream - so it is reported, not hidden: see the `--no-settle`
    option and the report's notes. A checkout that never leaves anything running (the "before" code)
    has nothing to wait for."""
    try:
        from core import find_business as FB
        pending = getattr(FB, "_BACKGROUND", None)
    except Exception:  # noqa: BLE001
        pending = None
    if not pending:
        return 0.0
    t0 = time.monotonic()
    while pending and time.monotonic() - t0 < max_wait_s:
        await asyncio.sleep(0.2)
    return time.monotonic() - t0


async def run_sample(handle, sample, label: str, settle_max_s: float = 40.0) -> list[dict]:
    rows = []
    for i, (name, args, reconstructed) in enumerate(sample):
        secs, resp = await _call(handle, args, i + 1)
        row = {"sample": label, "name": name, "reconstructed": reconstructed, "seconds": round(secs, 3)}
        row.update(classify(resp))
        apply_place_check(row, args)
        waited = await settle(settle_max_s) if settle_max_s > 0 else 0.0
        if waited > 0.05:
            row["settled_after_s"] = round(waited, 2)
        rows.append(row)
        print(f"  {label:8s} {name:42s} {row['class']:13s} {secs:6.2f}s  n={row.get('n', '-')}"
              + (f"  (then {waited:.1f}s for the background lookup)" if waited > 0.05 else ""), flush=True)
    return rows


def summarise(rows: list[dict]) -> dict:
    def share(sel):
        return round(sum(1 for r in sel if r["class"] in ("complete", "partial")) / len(sel), 3) if sel else None

    outcome = [r for r in rows if r["sample"] == "outcome"]
    carried = [r for r in outcome if not r["name"].startswith("no-args")]
    lookups = [r for r in rows if r["class"] in ("complete", "partial", "unavailable") and r["seconds"] > 0.05]
    lat = [r for r in rows if r["sample"] == "latency"]
    counts: dict = {}
    for r in outcome:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    return {
        "outcome_n": len(outcome), "outcome_class_counts": counts,
        "usable_rate_all_26": share(outcome),
        "usable_rate_calls_with_place_and_kind": share(carried), "n_with_place_and_kind": len(carried),
        "guided_or_usable_rate_all_26": round(sum(1 for r in outcome if r["class"] in ("complete", "partial", "guided_error")) / len(outcome), 3) if outcome else None,
        "complete_rate_latency_sample": round(sum(1 for r in lat if r["class"] == "complete") / len(lat), 3) if lat else None,
        "usable_rate_latency_sample": share(lat), "latency_n": len(lat),
        "latency_s": {"p50": percentile([r["seconds"] for r in lat], 50), "p95": percentile([r["seconds"] for r in lat], 95),
                      "max": max([r["seconds"] for r in lat], default=None)},
        "lookup_latency_s_all_samples": {"p50": percentile([r["seconds"] for r in lookups], 50),
                                         "p95": percentile([r["seconds"] for r in lookups], 95), "n": len(lookups)},
    }


async def main_async(args) -> int:
    root = os.path.abspath(args.root)
    sys.path.insert(0, root)
    os.chdir(root)
    # A bare environment: nothing that could make a call write anywhere.
    for k in list(os.environ):
        if k.startswith(("SUPABASE", "SPINE", "RESEND", "TWILIO", "VAPI", "JWT", "BILLING", "KEY_VERIFY")):
            os.environ.pop(k, None)
    import logging
    # With no Supabase configured every call logs a "usage_log_failed" traceback. It is expected here
    # (nothing may be written) and would bury the table this script exists to print.
    logging.disable(logging.CRITICAL)
    from agent_interface.mcp_server import handle_mcp_request  # noqa: E402
    from supply import osm_client  # noqa: E402

    if args.simulate:
        install_simulated_upstream(osm_client, args.sim_scale)

    report: dict = {"label": args.label, "root": root, "simulated_upstream": bool(args.simulate), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "passes": []}
    try:
        import subprocess
        report["commit"] = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None
    except Exception:  # noqa: BLE001
        report["commit"] = None
    for p in range(args.passes):
        print(f"--- {args.label} pass {p + 1}/{args.passes} ({'cold' if p == 0 else 'repeat, same process'})", flush=True)
        rows: list[dict] = []
        if args.sample in ("outcome", "both"):
            rows += await run_sample(handle_mcp_request, OUTCOME_SAMPLE, "outcome", args.settle)
        if args.sample in ("latency", "both"):
            rows += await run_sample(handle_mcp_request, LATENCY_SAMPLE, "latency", args.settle)
        report["passes"].append({"pass": p + 1, "rows": rows, "summary": summarise(rows)})
        if p + 1 < args.passes:
            await asyncio.sleep(args.pause)
    # Anything still running in the background belongs to a later pass; give it a moment, then report.
    client = osm_client.get_client()
    report["upstream_calls"] = dict(getattr(client, "upstream_calls", {}))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.out}")
    for p in report["passes"]:
        print(json.dumps({"pass": p["pass"], **p["summary"]}, indent=2))
    return 0


def compare(a_path: str, b_path: str) -> int:
    a, b = (json.load(open(x, encoding="utf-8")) for x in (a_path, b_path))
    keys = ["usable_rate_all_26", "usable_rate_calls_with_place_and_kind", "guided_or_usable_rate_all_26",
            "complete_rate_latency_sample", "usable_rate_latency_sample"]
    print(f"{'':44s}{a['label']:>12s}{b['label']:>12s}")
    sa, sb = a["passes"][0]["summary"], b["passes"][0]["summary"]
    for k in keys:
        print(f"{k:44s}{str(sa.get(k)):>12s}{str(sb.get(k)):>12s}")
    for k in ("p50", "p95", "max"):
        print(f"{'latency_s.' + k:44s}{str(sa['latency_s'][k]):>12s}{str(sb['latency_s'][k]):>12s}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--label", default="run")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="checkout whose code is measured (default: this one)")
    ap.add_argument("--out", default="")
    ap.add_argument("--passes", type=int, default=1, help="repeat the sample in the same process (2 = cold, then cached)")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between passes")
    ap.add_argument("--sample", choices=("outcome", "latency", "both"), default="both")
    ap.add_argument("--settle", type=float, default=40.0,
                    help="max seconds to wait after each call for lookups left running in the background "
                         "(0 = fire the next call at once, i.e. measure a burst; see settle())")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--reclassify", nargs=2, metavar=("IN", "OUT"))
    ap.add_argument("--simulate", action="store_true",
                    help="use the deterministic stand-in upstream instead of the public servers (see the docstring)")
    ap.add_argument("--sim-scale", type=float, default=1.0, help="multiply the stand-in's delays (1.0 = real time)")
    args = ap.parse_args(argv)
    if args.compare:
        return compare(*args.compare)
    if args.reclassify:
        return reclassify(*args.reclassify)
    return asyncio.run(main_async(args))


def reclassify(src: str, dst: str) -> int:
    """Re-derive classes in a saved report with the CURRENT place check (for a run made before it
    existed), so two reports are always scored by the same rules."""
    report = json.load(open(src, encoding="utf-8"))
    by_name = {n: a for n, a, _r in OUTCOME_SAMPLE + LATENCY_SAMPLE}
    for p in report["passes"]:
        for row in p["rows"]:
            if row.get("class") == "wrong_place":
                row["class"] = "complete"
            apply_place_check(row, by_name.get(row["name"], {}))
        p["summary"] = summarise(p["rows"])
    json.dump(report, open(dst, "w", encoding="utf-8"), indent=2)
    print(f"rescored {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
