"""
Cal.com direct API adapter for appointment scheduling.

Migrated to **API v2** (April 2026) — v1 was decommissioned upstream.
Differences from v1:
  - Base URL: https://api.cal.com/v2
  - Auth: Authorization: Bearer <CALCOM_API_KEY> (not query param)
  - Booking & availability paths/payloads changed

Requires: CALCOM_API_KEY env var.
Used as the primary channel for schedule_appointment when SMB uses Cal.com.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional


class CalComAdapter:
    channel_name = "direct_api:calcom"

    def __init__(self) -> None:
        self._api_key = os.getenv("CALCOM_API_KEY", "")
        self._base_url = "https://api.cal.com/v2"
        self._username = os.getenv("CALCOM_USERNAME", "")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Cal.com v2 requires version header; pin to 2024-08-13 (stable as of Apr 2026)
            "cal-api-version": "2024-08-13",
        }

    async def get_me(self) -> dict[str, Any]:
        """Return profile info for the authenticated user. Used as a connectivity check."""
        if not self._api_key:
            return {"username": "stub", "stub": True}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._base_url}/me", headers=self._headers())
                resp.raise_for_status()
                return resp.json().get("data", resp.json())
        except Exception as exc:
            raise RuntimeError(f"Cal.com /me failed: {exc}") from exc

    async def get_availability(
        self,
        event_type_id: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        """Fetch available slots. v2 returns {data: {slots: {date: [...]}, ...}}."""
        if not self._api_key:
            from channels.stub_policy import stubs_allowed
            if not stubs_allowed():
                raise RuntimeError(
                    "availability check channel not configured (CALCOM_API_KEY missing) -- "
                    "no availability was fetched and nothing was charged")
            return self._stub_availability(date_from, date_to)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._base_url}/slots",
                    headers=self._headers(),
                    params={
                        "eventTypeId": event_type_id,
                        "startTime": date_from,
                        "endTime": date_to,
                    },
                )
                if resp.status_code != 200:
                    return []
                payload = resp.json().get("data", {})
                slots_by_day = payload.get("slots", {})
                # Flatten {date: [{time: ...}, ...]} → [{time: ...}, ...]
                flat: list[dict[str, Any]] = []
                if isinstance(slots_by_day, dict):
                    for day_slots in slots_by_day.values():
                        if isinstance(day_slots, list):
                            flat.extend(day_slots)
                return flat
        except Exception:
            return []

    async def book_slot(
        self,
        event_type_id: str,
        start: str,
        name: str,
        email: str,
        notes: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a booking. v2 path: POST /v2/bookings with new schema."""
        if not self._api_key:
            from channels.stub_policy import stubs_allowed
            if not stubs_allowed():
                # Never claim ACCEPTED for a booking that was never made.
                raise RuntimeError(
                    "booking channel not configured (CALCOM_API_KEY missing) — "
                    "no booking was created and nothing was charged")
            return {
                "uid": f"STUB_BOOKING_{event_type_id}",
                "status": "ACCEPTED",
                "startTime": start,
                "attendees": [{"name": name, "email": email}],
            }
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self._base_url}/bookings",
                    headers=self._headers(),
                    json={
                        "eventTypeId": int(event_type_id),
                        "start": start,
                        "attendee": {
                            "name": name,
                            "email": email,
                            "timeZone": "UTC",
                            "language": "en",
                        },
                        "bookingFieldsResponses": {"notes": notes or ""},
                        "metadata": {},
                    },
                )
                resp.raise_for_status()
                return resp.json().get("data", resp.json())
        except Exception as exc:
            raise RuntimeError(f"Cal.com booking failed: {exc}") from exc

    async def cancel_booking(self, booking_uid: str, reason: str = "") -> dict[str, Any]:
        """Cancel a booking. v2: POST /v2/bookings/{uid}/cancel"""
        if not self._api_key:
            from channels.stub_policy import stubs_allowed
            if not stubs_allowed():
                raise RuntimeError(
                    "booking channel not configured (CALCOM_API_KEY missing) -- "
                    "no cancellation was performed and nothing was charged")
            return {"status": "CANCELLED", "uid": booking_uid}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/bookings/{booking_uid}/cancel",
                    headers=self._headers(),
                    json={"cancellationReason": reason or "Customer requested cancellation"},
                )
                resp.raise_for_status()
                return resp.json().get("data", resp.json())
        except Exception as exc:
            raise RuntimeError(f"Cal.com cancellation failed: {exc}") from exc

    async def health_check(self) -> bool:
        """Lightweight liveness check. Returns True for stub mode (no key)."""
        if not self._api_key:
            return True
        try:
            await self.get_me()
            return True
        except Exception:
            return False

    @staticmethod
    def _stub_availability(date_from: str, date_to: str) -> list[dict[str, Any]]:
        date_part = date_from.split("T")[0]
        return [
            {"start": f"{date_part}T10:00:00.000Z"},
            {"start": f"{date_part}T11:00:00.000Z"},
            {"start": f"{date_part}T14:00:00.000Z"},
        ]
