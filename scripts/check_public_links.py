#!/usr/bin/env python3
"""Crawl every public page on BOTH hosts and report dead links.

WHY BOTH HOSTS. The same HTML is served from two places: the API origin
(api.hatchloop.dev) and the marketing edge (hatchloop.dev), which proxies some
origin routes and not others. A relative link is therefore correct on one host
and a 404 on the other, and the pages where that matters most - Terms, Privacy,
Refund - are exactly the ones served under both. Checking one host proves
nothing about the other, which is how "Status" stayed broken on the legal pages
for the entire life of the footer.

WHY THIS IS NOT A TEST. It needs the network and it fails when a host is slow,
so gating a build on it would get it marked flaky and then ignored. The
build-gating rule lives in `tests/unit/test_public_links_resolve.py`, which is
offline and deterministic: origin-only paths must be written absolute. This
script is the ground-truth check you run before or after a deploy, and it can
find things the offline test cannot - a route that was deleted, a proxy rule
that changed, an edge that stopped forwarding something it used to forward.

Usage:
    python scripts/check_public_links.py            # crawl, print, exit 1 on dead
    python scripts/check_public_links.py --quiet    # only print problems
"""
from __future__ import annotations

import concurrent.futures
import re
import sys
import urllib.error
import urllib.request

ORIGIN = "https://api.hatchloop.dev"
EDGE = "https://hatchloop.dev"

# Pages a human or a payment processor actually opens. Not every route - the
# point is the pages that carry navigation and legal copy.
PAGES = ["/", "/pricing", "/terms", "/privacy", "/refund", "/docs"]

TIMEOUT = 25


def fetch(url: str) -> tuple[int, str, str]:
    """Return (status, html, FINAL url after redirects).

    THE FINAL URL IS THE WHOLE POINT and the first version of this script got
    it wrong. `/pricing` on the origin 308-redirects to the edge; urllib
    follows it silently, so resolving the returned page's relative links
    against the REQUESTED host invented seven dead links that do not exist -
    edge-only paths probed against the origin. A link checker that reports
    false positives is worse than none, because the real two get lost in them.
    Relative hrefs resolve against the document's own address; so does this.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "hatchloop-linkcheck/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), r.url
    except urllib.error.HTTPError as e:
        return e.code, "", url
    except Exception:  # noqa: BLE001 - a network error is not a dead link
        return 0, "", url


def status(url: str) -> int:
    code, _, _ = fetch(url)
    return code


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    dead: list[tuple[str, str, str, int]] = []
    unreachable: list[str] = []

    for host in (ORIGIN, EDGE):
        for path in PAGES:
            page_url = host + path
            code, html, final_url = fetch(page_url)
            # Resolve this page's links against where the page ACTUALLY came
            # from, which is not where we asked for it when a redirect fired.
            base = final_url.split("/", 3)[:3]
            base = "/".join(base)
            redirected = base.rstrip("/") != host
            if code == 0:
                unreachable.append(page_url)
                continue
            if code >= 400:
                # A 404 page itself is a finding, but only on the host that is
                # supposed to serve it; /docs may legitimately be origin-only.
                if not quiet:
                    print(f"  {code}  {page_url}   (page itself)")
                continue

            links = sorted(set(re.findall(r'href="([^"#?]+)"', html)))
            targets = []
            for h in links:
                if h.startswith("mailto:") or h.startswith("tel:"):
                    continue
                targets.append(h if h.startswith("http") else base + h)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                codes = list(ex.map(status, targets))

            bad = [(t, c) for t, c in zip(targets, codes) if c >= 400]
            for t, c in bad:
                dead.append((base, path, t, c))
            if not quiet:
                mark = f"{len(bad)} DEAD" if bad else "ok"
                via = f"  -> {base}" if redirected else ""
                print(f"  {code}  {page_url:44} {len(targets):3} links  {mark}{via}")

    if unreachable:
        print(f"\n{len(unreachable)} page(s) unreachable (network, not a dead link):")
        for u in unreachable:
            print("   ", u)

    if dead:
        print(f"\n{len(dead)} DEAD LINK(S):")
        for host, path, target, code in dead:
            print(f"   {code}  {target}")
            print(f"        linked from {host}{path}")
        print("\nIf the target is an origin-only route, write it as __ORIGIN__<path>")
        print("in the page body - page() substitutes the real origin at render time.")
        return 1

    print("\nno dead links on either host")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
