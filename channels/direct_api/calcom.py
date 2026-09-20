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
from urllib.parse import urlparse


class DestinationNotBound(RuntimeError):
    """The requested business could not be tied to an event type we may book.

    Distinct from the generic RuntimeErrors this adapter raises (network,
    credentials, upstream 5xx) because it is NOT retriable and NOT an outage:
    the connected Cal.com account simply does not host the business that was
    asked for. The caller must refuse the booking rather than fall back, so it
    needs to tell this apart from "Cal.com is having a bad minute".
    """


class BookingOutcomeUnknown(RuntimeError):
    """book_slot / cancel_booking failed in a way that does NOT prove the
    upstream mutation did not happen.

    THE DANGEROUS CASE THIS EXISTS FOR: a timeout waiting for Cal.com's
    response to a booking POST. The request may already have been received
    and accepted -- httpx.TimeoutException here means we stopped waiting for
    an answer, not that Cal.com never acted on what we sent. The same is true
    of a 5xx (Cal.com's own server may have partially processed the mutation
    before erroring) and of a 2xx response body we could not parse (Cal.com
    told us it worked and we simply could not read what it said).

    A plain RuntimeError from this adapter still means the OLD, safe thing:
    the request was never delivered (DNS/connect failure, before any bytes
    reached Cal.com) or Cal.com affirmatively rejected it (4xx) -- both
    provably "nothing happened here".

    The caller (core/schedule_appointment.py) MUST catch this separately from
    a plain RuntimeError and must never report "nothing was booked" /
    "nothing was cancelled" for it -- that is exactly the claim that invites
    a caller to retry and double-book a real business or double-charge a real
    customer. See tests/unit/test_booking_retry_safety.py.
    """


# Cal.com pages that carry a bookable handle. An organisation subdomain
# (<org>.cal.com) is deliberately absent: it is a different namespace from the
# connected account's own handle, so matching it would bind strangers' pages.
_CALCOM_BOOKING_HOSTS = frozenset({"cal.com", "www.cal.com", "app.cal.com"})

# First path segments that are Cal.com's own routes, not a user handle.
# "cal.com/team/acme/intro" belongs to team `acme`; reading "team" as a handle
# would bind every team page on the platform to whatever account we hold.
_CALCOM_RESERVED_SEGMENTS = frozenset({
    "team", "teams", "org", "orgs", "d", "forms", "router", "video",
    "booking", "bookings", "event-types", "settings", "auth", "api", "apps",
})


def _calcom_url_parts(booking_url: str | None) -> tuple[Optional[str], Optional[str]]:
    """Split a Cal.com booking URL into (handle, event_slug).

    Returns (None, None) for anything that is not a personal-handle Cal.com
    page — another host, a reserved route, or no path at all. The caller reads
    that as "cannot bind", never as "bind to whatever is available".
    """
    if not booking_url or not str(booking_url).strip():
        return None, None
    try:
        parsed = urlparse(str(booking_url).strip())
    except ValueError:
        return None, None
    if (parsed.hostname or "").lower() not in _CALCOM_BOOKING_HOSTS:
        return None, None
    segments = [s for s in (parsed.path or "").split("/") if s]
    if not segments:
        return None, None
    handle = segments[0].lower()
    if handle in _CALCOM_RESERVED_SEGMENTS:
        return None, None
    event_slug = segments[1].lower() if len(segments) > 1 else None
    return handle, event_slug


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
        # AN UPSTREAM FAILURE IS NOT "ZERO SLOTS". Both branches below used to
        # return [] -- a non-200 response AND a raised transport exception
        # (timeout, connection refused, malformed JSON) were both silently
        # turned into an empty slot list. The caller (schedule_appointment.py)
        # cannot tell "Cal.com truly has nothing open" from "Cal.com is down"
        # if both arrive as the same empty list, so a 503 was reported to the
        # customer as a confident, SUCCESSFUL "0 slots available" answer and
        # charged the per_availability_check fee. Reproduced against the real
        # adapter 2026-09-21. Raising here lets the caller tell the two apart
        # and charge nothing for a question we never got to ask.
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._base_url}/slots",
                    # The /slots route only exists under cal-api-version
                    # 2024-09-04 (older versions 404) and takes start/end, not
                    # startTime/endTime (verified against live Cal.com v2).
                    headers={**self._headers(), "cal-api-version": "2024-09-04"},
                    params={
                        "eventTypeId": event_type_id,
                        "start": date_from,
                        "end": date_to,
                    },
                )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Cal.com availability lookup failed: HTTP "
                        f"{resp.status_code}")
                payload = resp.json().get("data", {})
                # 2024-09-04 returns data AS the {date: [slots]} map directly;
                # older shapes nested it under data.slots. Handle both.
                if isinstance(payload, dict) and isinstance(payload.get("slots"), dict):
                    slots_by_day = payload["slots"]
                elif isinstance(payload, dict):
                    slots_by_day = payload
                else:
                    slots_by_day = {}
                # Flatten {date: [{start: ...}, ...]} → [{start: ...}, ...]
                flat: list[dict[str, Any]] = []
                for day_slots in slots_by_day.values():
                    if isinstance(day_slots, list):
                        flat.extend(day_slots)
                return flat
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Cal.com availability lookup failed: {exc}") from exc

    async def get_event_types(self) -> list[dict[str, Any]]:
        """List event types on the wired Cal.com account (v2 GET /v2/event-types).

        Used to resolve an event type id for SMBs imported via import_booking_url,
        which store no calcom_event_type_id. SINGLE-TENANT: only one Cal.com key
        is wired (the founder's cal_live key), so this always returns that one
        account's event types. Raises RuntimeError (honest, no-charge) if the
        lookup cannot be performed.
        """
        if not self._api_key:
            from channels.stub_policy import stubs_allowed
            if not stubs_allowed():
                raise RuntimeError(
                    "event-type lookup not configured (CALCOM_API_KEY missing) -- "
                    "no event type could be resolved and nothing was charged")
            return [{"id": 1001, "slug": "stub-consult", "lengthInMinutes": 30}]
        try:
            import httpx
            params = {"username": self._username} if self._username else None
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._base_url}/event-types",
                    # The /event-types route only responds under cal-api-version
                    # 2024-06-14 (2024-08-13 => 404); token scopes it to this
                    # account, so username is optional (verified live).
                    headers={**self._headers(), "cal-api-version": "2024-06-14"},
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
        except Exception as exc:
            raise RuntimeError(f"Cal.com event-types lookup failed: {exc}") from exc
        # Cal.com v2 has shipped two shapes for this endpoint; accept both a
        # flat list and the grouped {eventTypeGroups:[{eventTypes:[...]}]} form.
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            flat: list[dict[str, Any]] = []
            for grp in (data.get("eventTypeGroups") or []):
                if isinstance(grp, dict):
                    flat.extend(grp.get("eventTypes") or [])
            if not flat and isinstance(data.get("eventTypes"), list):
                flat = data["eventTypes"]
            return flat
        return []

    async def get_connected_username(self) -> str:
        """The handle of the Cal.com account this key belongs to.

        CALCOM_USERNAME first (one env read, no round trip); /me otherwise.
        Returns "" when the account cannot be identified — and an empty handle
        matches no URL, so an unidentifiable account binds nothing.
        """
        if self._username:
            return self._username.strip().lower()
        try:
            me = await self.get_me()
        except RuntimeError:
            return ""
        username = ""
        if isinstance(me, dict):
            username = str(me.get("username") or me.get("handle") or "")
        return username.strip().lower()

    async def resolve_event_type_id_for_business(self, booking_url: str | None) -> str:
        """Event type id that PROVABLY belongs to the business at `booking_url`.

        THIS IS THE BINDING THAT WAS MISSING, AND IT IS WHY THIS METHOD EXISTS
        RATHER THAN A CALL TO get_default_event_type_id().

        An imported business stores no calcom_event_type_id, so the booking
        path used to ask the connected account for *any* usable event type and
        book against it, while the receipt named the imported business. The
        two have nothing to do with each other: the connected account is our
        own single-tenant cal_live key, so "book me at Brooklyn Plumbing"
        produced a real booking on our own calendar, reported as confirmed
        under Brooklyn Plumbing's name.

        A destination is bound only when:
          1. the URL is a personal-handle Cal.com page, AND
          2. that handle IS the connected account (so the calendar is the
             business's by construction), AND
          3. if the URL names an event slug, the account really has it.

        Anything else raises DestinationNotBound. There is no fallback on
        purpose: the caller cannot undo a booking that has already emailed a
        stranger, and a customer who is told they have an appointment does not
        go looking for one.
        """
        handle, event_slug = _calcom_url_parts(booking_url)
        if not handle:
            raise DestinationNotBound(
                "no Cal.com booking page is recorded for this business, so no "
                "event type can be tied to it")

        connected = await self.get_connected_username()
        if not connected or handle != connected:
            raise DestinationNotBound(
                f"Cal.com page '{handle}' is not hosted by the connected "
                f"account, so we hold no calendar for this business")

        types = await self.get_event_types()
        usable = [t for t in types if isinstance(t, dict) and t.get("id") is not None]
        if not usable:
            raise DestinationNotBound(
                "the connected Cal.com account exposes no bookable event type")

        if event_slug:
            for t in usable:
                if str(t.get("slug") or "").strip().lower() == event_slug:
                    return str(t["id"])
            # Right account, wrong service. Substituting a different event type
            # here is the same defect one calendar further in - a 15-minute
            # intro booked for an emergency call-out.
            raise DestinationNotBound(
                f"the connected account has no event type '{event_slug}'")

        # Handle-only URL: the account IS the business, so every event type on
        # it is that business's. Shortest duration is the least-committing
        # default when the URL did not name a service.
        return await self.get_default_event_type_id()

    async def get_default_event_type_id(self) -> str:
        """Resolve a usable event type id on the wired Cal.com account.

        NOT A DESTINATION. This answers "what can this ACCOUNT book", never
        "what belongs to THIS business" — call it only after
        resolve_event_type_id_for_business has tied the business to this
        account, or a booking lands on whichever calendar the key happens to
        hold.

        Prefers the shortest-duration event type (a short consult is the safest
        default for an imported business whose real service length is unknown),
        falling back to the first available. Raises RuntimeError (honest,
        no-charge) when none can be resolved so the caller reports the true
        reason instead of booking against a guessed id.
        """
        types = await self.get_event_types()
        usable = [t for t in types
                  if isinstance(t, dict) and t.get("id") is not None]
        if not usable:
            raise RuntimeError(
                "no Cal.com event type is available on the connected account to "
                "book against -- nothing was booked and nothing was charged")

        def _dur(t: dict) -> int:
            v = t.get("lengthInMinutes")
            if v is None:
                v = t.get("length")
            try:
                return int(v)
            except (TypeError, ValueError):
                return 10 ** 9

        usable.sort(key=_dur)
        return str(usable[0]["id"])

    async def book_slot(
        self,
        event_type_id: str,
        start: str,
        name: str,
        email: str,
        notes: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a booking. v2 path: POST /v2/bookings with new schema.

        Raises BookingOutcomeUnknown -- not a plain RuntimeError -- whenever
        the failure does not prove nothing was booked (timeout after the
        request was sent, a 5xx, or an unparseable 2xx body). See the
        exception's own docstring; the caller relies on this distinction.
        """
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
        import httpx
        payload = {
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
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self._base_url}/bookings",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            # We stopped waiting for an answer -- we did not learn Cal.com
            # rejected the request. It may already hold this booking.
            raise BookingOutcomeUnknown(
                f"Cal.com did not respond in time to the booking request "
                f"for event type {event_type_id} at {start} -- the request "
                f"may already have reached and been accepted by Cal.com; "
                f"we simply never received (or waited long enough for) the "
                f"confirmation: {exc}"
            ) from exc
        except Exception as exc:
            # Never reached Cal.com at all (DNS/connect/other transport
            # failure before any response) -- no request was delivered, so
            # this is a certain, safe-to-retry failure.
            raise RuntimeError(f"Cal.com booking failed: {exc}") from exc

        if resp.status_code >= 500:
            # Cal.com's OWN server errored after receiving the request. It
            # may have partially committed the booking before failing -- this
            # is not the same as Cal.com rejecting it.
            raise BookingOutcomeUnknown(
                f"Cal.com returned server error {resp.status_code} for the "
                f"booking request -- the request was received but its "
                f"outcome cannot be confirmed: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            # An affirmative rejection of the request AS SENT (validation,
            # auth, conflict) -- Cal.com processed and refused it. Certain.
            raise RuntimeError(
                f"Cal.com booking failed: HTTP {resp.status_code} "
                f"{resp.text[:200]}")
        try:
            return resp.json().get("data", resp.json())
        except Exception as exc:
            # Cal.com told us the booking succeeded (2xx) and we could not
            # read what it said. The booking most likely exists; we just
            # cannot report its id/status.
            raise BookingOutcomeUnknown(
                f"Cal.com accepted the booking request (HTTP "
                f"{resp.status_code}) but the response body could not be "
                f"parsed, so the booking's id and status cannot be read: "
                f"{exc}"
            ) from exc

    async def cancel_booking(self, booking_uid: str, reason: str = "") -> dict[str, Any]:
        """Cancel a booking. v2: POST /v2/bookings/{uid}/cancel

        Same BookingOutcomeUnknown treatment as book_slot, and for the same
        reason: a mutation with a real side effect and no undo must not be
        reported as "did not happen" when the failure does not prove that.
        """
        if not self._api_key:
            from channels.stub_policy import stubs_allowed
            if not stubs_allowed():
                raise RuntimeError(
                    "booking channel not configured (CALCOM_API_KEY missing) -- "
                    "no cancellation was performed and nothing was charged")
            return {"status": "CANCELLED", "uid": booking_uid}
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/bookings/{booking_uid}/cancel",
                    headers=self._headers(),
                    json={"cancellationReason": reason or "Customer requested cancellation"},
                )
        except httpx.TimeoutException as exc:
            raise BookingOutcomeUnknown(
                f"Cal.com did not respond in time to the cancellation "
                f"request for booking {booking_uid} -- it may already have "
                f"been cancelled upstream even though this call cannot "
                f"confirm it: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Cal.com cancellation failed: {exc}") from exc

        if resp.status_code >= 500:
            raise BookingOutcomeUnknown(
                f"Cal.com returned server error {resp.status_code} "
                f"cancelling booking {booking_uid} -- the cancellation's "
                f"outcome cannot be confirmed: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Cal.com cancellation failed: HTTP {resp.status_code} "
                f"{resp.text[:200]}")
        try:
            return resp.json().get("data", resp.json())
        except Exception as exc:
            raise BookingOutcomeUnknown(
                f"Cal.com accepted the cancellation of {booking_uid} (HTTP "
                f"{resp.status_code}) but the response body could not be "
                f"parsed: {exc}"
            ) from exc

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
