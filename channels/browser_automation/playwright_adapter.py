"""
Playwright browser automation adapter — last-resort channel.
Used when no direct API, voice AI, SMS, or email path is available.
Requires: playwright installed (`pip install playwright && playwright install chromium`).
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional


class PlaywrightAdapter:
    channel_name = "browser_automation:playwright"

    async def submit_web_form(
        self,
        url: str,
        fields: dict[str, str],
        submit_selector: str = "button[type=submit]",
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        """
        Fill and submit a web form.
        Returns success + any confirmation text visible after submission.
        """
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            return {
                "success": False,
                "error": "playwright not installed",
                "note": "Install with: pip install playwright && playwright install chromium",
            }

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=timeout_ms)
                for selector, value in fields.items():
                    await page.fill(selector, value)
                await page.click(submit_selector, timeout=5000)
                await page.wait_for_load_state("networkidle", timeout=15000)
                confirmation_text = await page.text_content("body") or ""
                return {
                    "success": True,
                    "url_after": page.url,
                    "confirmation_snippet": confirmation_text[:500],
                }
            except Exception as exc:
                return {"success": False, "error": str(exc)}
            finally:
                await browser.close()

    async def health_check(self) -> bool:
        try:
            from playwright.async_api import async_playwright  # type: ignore
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                await browser.close()
            return True
        except Exception:
            return False
