"""
Channel adapter interface.
All channel adapters implement ChannelAdapter and call compliance.pre_check before dispatch.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ChannelRequest:
    recipient_id: str
    channel: str
    message_type: str
    content: str
    subject: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    country_code: Optional[str] = None
    state_code: Optional[str] = None
    agent_id: Optional[str] = None
    trace_id: Optional[str] = None


@dataclass
class ChannelResponse:
    success: bool
    provider_message_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw_response: Optional[dict[str, Any]] = None


class ChannelAdapter(ABC):
    """
    Base for all channel adapters.
    Subclasses MUST call compliance.pre_check.pre_check() before dispatching.
    """

    channel_name: str = "unknown"
    is_available: bool = True

    @abstractmethod
    async def send(self, request: ChannelRequest) -> ChannelResponse:
        """Dispatch an outbound communication. Must call pre_check first."""
        ...

    async def health_check(self) -> bool:
        """Returns True if this channel adapter is operational."""
        return self.is_available

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(channel={self.channel_name})"
