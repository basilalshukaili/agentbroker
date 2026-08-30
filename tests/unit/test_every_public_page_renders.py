"""Every public page must return 200 through its ROUTE, not just render.

I added /status, tested it by calling render_status() directly, saw 9,238
characters of HTML, and shipped a 500 to production. The function was fine.
It was never imported into main.py - the import is a multi-line block and my
edit matched a single-line form that does not exist there.

Calling the function is not testing the page, in exactly the way that
stubbing _gleif_by_name was not testing the registry lookup earlier the same
day. The route is the product; the function is an implementation detail.

This walks every human-facing route the footer and nav link to, through the
app, so a page that exists but is unreachable cannot ship again.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app, raise_server_exceptions=False)

# Everything a person can click to from the nav or the footer.
PUBLIC_PAGES = ["/status", "/terms", "/privacy", "/refund", "/health"]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_page_returns_200(path):
    r = client.get(path)
    assert r.status_code == 200, (
        f"{path} returned {r.status_code} - a page linked from the footer that "
        f"does not load: {r.text[:200]}")


@pytest.mark.parametrize("path", [p for p in PUBLIC_PAGES if p != "/health"])
def test_public_page_is_html_not_a_stack_trace(path):
    r = client.get(path)
    body = r.text
    assert "<html" in body.lower() or "<h1" in body.lower(), (
        f"{path} did not return a rendered page")
    assert "Traceback" not in body and "internal_error" not in body, (
        f"{path} rendered an error into the page")


def test_the_footer_status_link_points_at_a_page_not_json():
    """It used to link straight at /health, so a human clicking "Status" got
    a machine payload."""
    r = client.get("/terms")
    assert '/status"' in r.text, (
        "the footer no longer links to /status - if that is deliberate, "
        "update this test; if not, a person clicking Status gets raw JSON")
