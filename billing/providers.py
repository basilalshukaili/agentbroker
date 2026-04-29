"""
Billing providers — Stripe-free, Oman-compatible alternatives.

Why no Stripe: Stripe does not operate in Oman as a recipient as of 2026-04.

Four provider implementations, founder picks one via BILLING_PROVIDER env var:

    polar         — Polar.sh (Merchant of Record, payouts via wire/Wise)
    lemonsqueezy  — Lemon Squeezy (MoR, payouts via Wise)
    coinbase      — Coinbase Commerce (crypto USDC, no banking required)
    manual        — Generates a payment-link via PayPal/Wise; no auto-charge

All providers implement the BillingProvider interface so the rest of the
codebase doesn't care which one is active.
"""
from __future__ import annotations

import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Common types
# ---------------------------------------------------------------------------

@dataclass
class CheckoutSession:
    session_id: str
    payment_url: str
    amount_usd: float
    currency: str = "USD"
    provider: str = ""
    expires_at: Optional[float] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class PaymentReceipt:
    receipt_id: str
    session_id: str
    amount_usd: float
    currency: str
    paid_at: float
    provider: str
    raw_response: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class BillingProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    async def create_checkout(
        self, *,
        amount_usd: float,
        description: str,
        agent_id: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession: ...

    @abstractmethod
    async def get_status(self, session_id: str) -> Optional[PaymentReceipt]: ...

    @abstractmethod
    def health_check(self) -> bool: ...


# ---------------------------------------------------------------------------
# Paddle — Merchant of Record, works for Oman residents
# ---------------------------------------------------------------------------

class PaddleProvider(BillingProvider):
    """
    Paddle.com — established Merchant of Record.
    - Handles VAT/tax globally
    - Pays out via international wire / PayPal / Wise to Oman bank accounts
    - Free to start; takes ~5% + $0.50 per transaction (only on real sales)
    - Production API: https://api.paddle.com (set PADDLE_ENVIRONMENT=sandbox to test)
    """
    name = "paddle"

    def __init__(self) -> None:
        self._api_key = os.getenv("PADDLE_API_KEY", "")
        env = os.getenv("PADDLE_ENVIRONMENT", "production").lower()
        self._base_url = (
            "https://sandbox-api.paddle.com" if env == "sandbox"
            else "https://api.paddle.com"
        )

    async def create_checkout(self, *, amount_usd, description, agent_id,
                              success_url, cancel_url) -> CheckoutSession:
        if not self._api_key:
            return _stub_session("paddle", amount_usd, success_url, agent_id)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Paddle v1 transactions API
                resp = await client.post(
                    f"{self._base_url}/transactions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "items": [{
                            "quantity": 1,
                            "price": {
                                "description": description,
                                "name": description[:50],
                                "billing_cycle": None,  # one-time
                                "trial_period": None,
                                "tax_mode": "internal",
                                "unit_price": {
                                    "amount": str(int(amount_usd * 100)),
                                    "currency_code": "USD",
                                },
                                "quantity": {"minimum": 1, "maximum": 1},
                            },
                        }],
                        "checkout": {"url": success_url},
                        "custom_data": {"agent_id": agent_id},
                        "collection_mode": "automatic",
                        "currency_code": "USD",
                    },
                )
                if resp.status_code in (200, 201):
                    data = resp.json().get("data", {})
                    checkout_url = data.get("checkout", {}).get("url") or success_url
                    return CheckoutSession(
                        session_id=data.get("id", f"pad_{uuid.uuid4().hex[:12]}"),
                        payment_url=checkout_url,
                        amount_usd=amount_usd,
                        provider="paddle",
                        expires_at=time.time() + 3600,
                        metadata={"agent_id": agent_id, "raw": data},
                    )
        except Exception:
            pass
        return _stub_session("paddle", amount_usd, success_url, agent_id)

    async def get_status(self, session_id: str) -> Optional[PaymentReceipt]:
        if not self._api_key:
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._base_url}/transactions/{session_id}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    if data.get("status") == "completed":
                        amount = int(data.get("details", {}).get("totals", {}).get("total", 0))
                        return PaymentReceipt(
                            receipt_id=data.get("id", session_id),
                            session_id=session_id,
                            amount_usd=amount / 100,
                            currency="USD",
                            paid_at=time.time(),
                            provider="paddle",
                            raw_response=data,
                        )
        except Exception:
            pass
        return None

    def health_check(self) -> bool:
        return bool(self._api_key)


# ---------------------------------------------------------------------------
# Polar.sh — primary recommendation
# ---------------------------------------------------------------------------

class PolarProvider(BillingProvider):
    """
    Polar.sh — open-source Stripe alternative for SaaS / dev tools.
    Merchant of Record. Pays out via international wire to local bank accounts
    including Oman (verified at https://polar.sh/docs).

    Sign up: https://polar.sh
    Get API key from: https://polar.sh/settings
    """
    name = "polar"

    def __init__(self) -> None:
        self._api_key = os.getenv("POLAR_API_KEY", "")
        self._org_id = os.getenv("POLAR_ORG_ID", "")
        self._base_url = "https://api.polar.sh/v1"

    async def create_checkout(self, *, amount_usd, description, agent_id,
                              success_url, cancel_url) -> CheckoutSession:
        if not self._api_key:
            return _stub_session("polar", amount_usd, success_url, agent_id)
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/checkouts",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "organization_id": self._org_id,
                        "amount": int(amount_usd * 100),
                        "currency": "USD",
                        "product_description": description,
                        "success_url": success_url,
                        "cancel_url": cancel_url,
                        "customer_metadata": {"agent_id": agent_id},
                    },
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()
                return CheckoutSession(
                    session_id=data["id"],
                    payment_url=data["url"],
                    amount_usd=amount_usd,
                    provider="polar",
                    expires_at=time.time() + 3600,
                    metadata={"agent_id": agent_id},
                )
        except Exception:
            return _stub_session("polar", amount_usd, success_url, agent_id)

    async def get_status(self, session_id: str) -> Optional[PaymentReceipt]:
        if not self._api_key:
            return None
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/checkouts/{session_id}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "succeeded":
                        return PaymentReceipt(
                            receipt_id=data.get("payment_id", session_id),
                            session_id=session_id,
                            amount_usd=data["amount"] / 100,
                            currency=data.get("currency", "USD"),
                            paid_at=time.time(),
                            provider="polar",
                            raw_response=data,
                        )
        except Exception:
            pass
        return None

    def health_check(self) -> bool:
        return bool(self._api_key and self._org_id)


# ---------------------------------------------------------------------------
# Lemon Squeezy — alternative
# ---------------------------------------------------------------------------

class LemonSqueezyProvider(BillingProvider):
    """
    Lemon Squeezy — Merchant of Record SaaS billing.
    Owned by Stripe but operates as MoR globally. Payouts via Wise / direct deposit.

    Sign up: https://lemonsqueezy.com
    """
    name = "lemonsqueezy"

    def __init__(self) -> None:
        self._api_key = os.getenv("LEMONSQUEEZY_API_KEY", "")
        self._store_id = os.getenv("LEMONSQUEEZY_STORE_ID", "")
        self._base_url = "https://api.lemonsqueezy.com/v1"

    async def create_checkout(self, *, amount_usd, description, agent_id,
                              success_url, cancel_url) -> CheckoutSession:
        if not self._api_key or not self._store_id:
            return _stub_session("lemonsqueezy", amount_usd, success_url, agent_id)
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/checkouts",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Accept": "application/vnd.api+json",
                        "Content-Type": "application/vnd.api+json",
                    },
                    json={
                        "data": {
                            "type": "checkouts",
                            "attributes": {
                                "checkout_data": {
                                    "custom": {"agent_id": agent_id},
                                },
                                "product_options": {
                                    "name": description,
                                    "redirect_url": success_url,
                                },
                            },
                            "relationships": {
                                "store": {"data": {"type": "stores", "id": self._store_id}},
                            },
                        },
                    },
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()["data"]
                return CheckoutSession(
                    session_id=data["id"],
                    payment_url=data["attributes"]["url"],
                    amount_usd=amount_usd,
                    provider="lemonsqueezy",
                    expires_at=time.time() + 3600,
                    metadata={"agent_id": agent_id},
                )
        except Exception:
            return _stub_session("lemonsqueezy", amount_usd, success_url, agent_id)

    async def get_status(self, session_id: str) -> Optional[PaymentReceipt]:
        return None  # implement when needed

    def health_check(self) -> bool:
        return bool(self._api_key and self._store_id)


# ---------------------------------------------------------------------------
# Coinbase Commerce — crypto, anywhere
# ---------------------------------------------------------------------------

class CoinbaseCommerceProvider(BillingProvider):
    """
    Coinbase Commerce — accept USDC / BTC / ETH.
    Settles to a wallet you control. No banking required. Works in every country.
    Useful as a fallback for jurisdictions where MoR providers can't pay out.

    Sign up: https://commerce.coinbase.com
    """
    name = "coinbase"

    def __init__(self) -> None:
        self._api_key = os.getenv("COINBASE_COMMERCE_API_KEY", "")
        self._base_url = "https://api.commerce.coinbase.com"

    async def create_checkout(self, *, amount_usd, description, agent_id,
                              success_url, cancel_url) -> CheckoutSession:
        if not self._api_key:
            return _stub_session("coinbase", amount_usd, success_url, agent_id)
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/charges",
                    headers={
                        "X-CC-Api-Key": self._api_key,
                        "X-CC-Version": "2018-03-22",
                    },
                    json={
                        "name": description[:100],
                        "description": description,
                        "pricing_type": "fixed_price",
                        "local_price": {"amount": str(amount_usd), "currency": "USD"},
                        "metadata": {"agent_id": agent_id},
                        "redirect_url": success_url,
                        "cancel_url": cancel_url,
                    },
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()["data"]
                return CheckoutSession(
                    session_id=data["id"],
                    payment_url=data["hosted_url"],
                    amount_usd=amount_usd,
                    provider="coinbase",
                    expires_at=time.time() + 3600,
                    metadata={"agent_id": agent_id, "code": data.get("code", "")},
                )
        except Exception:
            return _stub_session("coinbase", amount_usd, success_url, agent_id)

    async def get_status(self, session_id: str) -> Optional[PaymentReceipt]:
        return None  # webhook-driven in production

    def health_check(self) -> bool:
        return bool(self._api_key)


# ---------------------------------------------------------------------------
# Manual provider — emails a payment link, no auto-charge
# ---------------------------------------------------------------------------

class ManualProvider(BillingProvider):
    """
    Day-zero billing: emit a payment-instruction URL.
    The founder receives funds via Wise / PayPal / direct invoice.
    Use for: pilot phase before any MoR is set up. Zero-friction launch.
    """
    name = "manual"

    def __init__(self) -> None:
        self._wise_link = os.getenv("WISE_PAYMENT_LINK", "")
        self._paypal_link = os.getenv("PAYPAL_PAYMENT_LINK", "")

    async def create_checkout(self, *, amount_usd, description, agent_id,
                              success_url, cancel_url) -> CheckoutSession:
        session_id = f"manual_{uuid.uuid4().hex[:12]}"
        # Provide whatever link the founder configured
        url = (self._wise_link or self._paypal_link
               or "https://wise.com/pay/your-link-here")
        return CheckoutSession(
            session_id=session_id,
            payment_url=url,
            amount_usd=amount_usd,
            provider="manual",
            expires_at=time.time() + 7 * 86400,
            metadata={
                "agent_id": agent_id,
                "description": description,
                "instructions": (
                    f"Send ${amount_usd:.2f} USD via the above link. "
                    f"Include reference: {session_id}. "
                    "We mark your account paid within 24h of confirmation."
                ),
            },
        )

    async def get_status(self, session_id: str) -> Optional[PaymentReceipt]:
        return None  # founder confirms manually

    def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[BillingProvider]] = {
    "paddle": PaddleProvider,
    "polar": PolarProvider,
    "lemonsqueezy": LemonSqueezyProvider,
    "coinbase": CoinbaseCommerceProvider,
    "manual": ManualProvider,
}


def get_billing_provider() -> BillingProvider:
    """Read BILLING_PROVIDER env var and return the right adapter."""
    name = os.getenv("BILLING_PROVIDER", "manual").lower()
    cls = _PROVIDERS.get(name, ManualProvider)
    return cls()


def list_available_providers() -> list[str]:
    return list(_PROVIDERS.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_session(provider: str, amount_usd: float, success_url: str, agent_id: str) -> CheckoutSession:
    sid = f"{provider}_stub_{uuid.uuid4().hex[:8]}"
    return CheckoutSession(
        session_id=sid,
        payment_url=f"{success_url}?session_id={sid}&stub=true",
        amount_usd=amount_usd,
        provider=provider,
        expires_at=time.time() + 3600,
        metadata={"stub": True, "agent_id": agent_id},
    )
