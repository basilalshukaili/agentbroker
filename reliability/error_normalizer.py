"""
Error normalizer — maps raw upstream exceptions to structured APIError instances.
Every error returned to an agent must be machine-readable and actionable (§9.4).
"""
from __future__ import annotations

from core.models import APIError, ErrorCode, ErrorCategory


def normalize_exception(exc: Exception, operation: str = "") -> APIError:
    """Convert any exception into a structured APIError."""
    msg = str(exc)

    # Compliance violations are already structured
    from core.models import ComplianceViolationError
    if isinstance(exc, ComplianceViolationError):
        return exc.to_api_error()

    # HTTP errors from upstream providers
    if "429" in msg or "rate" in msg.lower():
        return APIError(
            code=ErrorCode.RATE_LIMITED,
            category=ErrorCategory.CLIENT_ERROR,
            retriable=True,
            message=f"Rate limit hit during {operation}. Retry after the indicated delay.",
            next_action="Respect retry_after_ms and reduce call frequency.",
            retry_after_ms=30000,
        )

    if "404" in msg or "not found" in msg.lower():
        return APIError(
            code=ErrorCode.SUPPLY_UNREACHABLE,
            category=ErrorCategory.SERVER_ERROR,
            retriable=True,
            message=f"Target resource not found: {msg}",
            next_action="Verify smb_id with verify_business before retrying.",
            retry_after_ms=5000,
        )

    if "timeout" in msg.lower() or "timed out" in msg.lower():
        return APIError(
            code=ErrorCode.TRANSIENT,
            category=ErrorCategory.SERVER_ERROR,
            retriable=True,
            message="Request timed out. The upstream service may be temporarily slow.",
            next_action="Retry after retry_after_ms.",
            retry_after_ms=10000,
        )

    if "500" in msg or "502" in msg or "503" in msg or "504" in msg:
        return APIError(
            code=ErrorCode.UPSTREAM_FAILURE,
            category=ErrorCategory.SERVER_ERROR,
            retriable=True,
            message=f"Upstream service returned an error: {msg[:200]}",
            next_action="Retry after retry_after_ms. If persistent, call self_test.",
            retry_after_ms=15000,
        )

    # Default: internal error
    return APIError(
        code=ErrorCode.INTERNAL,
        category=ErrorCategory.SERVER_ERROR,
        retriable=False,
        message=f"Unexpected error during {operation}: {msg[:200]}",
        next_action="Report to support with trace_id.",
    )
