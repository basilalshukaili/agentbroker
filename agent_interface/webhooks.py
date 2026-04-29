"""
Webhook management endpoint — agents register callback URLs to receive
async operation completions.

Backed by reliability/webhook_delivery.py for delivery with exponential backoff.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from reliability.webhook_delivery import register_webhook
from core.models import WebhookRegistrationRequest, OperationStatus


@dataclass
class _WebhookRecord:
    webhook_id: str
    agent_id: str
    url: str
    events: list[str]
    secret: str
    registered_at: float
    active: bool


@dataclass
class WebhookRegistrationResult:
    status: OperationStatus
    webhook_id: Optional[str] = None
    message: str = ""
    secret: Optional[str] = None    # HMAC-SHA256 signing secret for verification


@dataclass
class WebhookListEntry:
    webhook_id: str
    agent_id: str
    url: str
    events: list[str]
    registered_at: float
    active: bool


# In-memory registry (production: PostgreSQL)
_registrations: dict[str, _WebhookRecord] = {}


def register(
    req: WebhookRegistrationRequest,
    agent_id: str,
) -> WebhookRegistrationResult:
    """
    Register a webhook URL for async operation callbacks.

    The returned secret must be stored by the agent and used to verify
    incoming webhook signatures (X-Signature header).
    """
    import secrets as _secrets
    signing_secret = _secrets.token_hex(32)
    wh_id = f"wh_{uuid.uuid4().hex[:12]}"
    events = req.events or ["operation.completed", "operation.failed"]

    reg = _WebhookRecord(
        webhook_id=wh_id,
        agent_id=agent_id,
        url=str(req.callback_url),
        events=events,
        secret=signing_secret,
        registered_at=time.time(),
        active=True,
    )
    _registrations[wh_id] = reg

    # Also register with the delivery module
    register_webhook(str(req.callback_url), signing_secret, events)

    return WebhookRegistrationResult(
        status=OperationStatus.SUCCESS,
        webhook_id=wh_id,
        message=f"Webhook registered for events: {events}",
        secret=signing_secret,
    )


def list_webhooks(agent_id: str) -> list[WebhookListEntry]:
    return [
        WebhookListEntry(
            webhook_id=r.webhook_id,
            agent_id=r.agent_id,
            url=r.url,
            events=r.events,
            registered_at=r.registered_at,
            active=r.active,
        )
        for r in _registrations.values()
        if r.agent_id == agent_id
    ]


def deactivate_webhook(webhook_id: str, agent_id: str) -> bool:
    reg = _registrations.get(webhook_id)
    if reg and reg.agent_id == agent_id:
        reg.active = False
        return True
    return False
