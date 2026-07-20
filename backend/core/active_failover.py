"""Provider-Agnostic Active Failover (Phase 12).

Root-cause fix (mandate 1): the LLM failover is NOT a try/except buried in
the Claude caller. It is a provider-agnostic orchestrator that sits ABOVE
the provider layer — any provider is just a named async callable, and the
router reroutes the SAME context payload to the next provider on a
retriable fault. A new provider drops in with one entry; no caller change.

Retriable classification (mandate 2a): a fault is retriable on HTTP 402
(payment required), 429 (rate limit), or 5xx — AND on the real-world
shapes those errors take in SDKs, most importantly Anthropic's
insufficient-credit case, which arrives as a **400** whose body says
"credit balance is too low". Classifying only on the numeric code would
miss it (the exact bug that made JARVIS answer "still starting up").

DRY (mandate 3): providers are callables, so the existing DoubleWord
provider / any client is reused as-is via a thin adapter — no duplicated
generation logic. Reuses the same context payload end to end.

Every public entry point NEVER raises out of the orchestrator (it returns
a typed result); classification NEVER raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Set

logger = logging.getLogger("Jarvis.ActiveFailover")

#: HTTP status codes that warrant failover to the next provider.
DEFAULT_RETRIABLE_CODES: Set[int] = {402, 429, 500, 502, 503, 504, 529}

#: Substrings (case-folded) in an error that indicate a retriable
#: quota/billing/availability fault even when the numeric code is a 400
#: (Anthropic's "credit balance is too low" ships as a 400).
_RETRIABLE_MESSAGE_TOKENS = (
    "credit balance", "insufficient", "quota", "billing",
    "rate limit", "overloaded", "capacity", "unavailable",
    "payment required", "too low",
)


def extract_status_code(exc: BaseException) -> Optional[int]:
    """Pull an HTTP status code out of the common SDK exception shapes
    (anthropic/openai/httpx expose ``.status_code`` or ``.response.status_
    code``; some use ``.code``). Returns None if none is present. NEVER
    raises."""
    for attr in ("status_code", "http_status", "code"):
        try:
            v = getattr(exc, attr, None)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        except Exception:  # noqa: BLE001
            pass
    try:
        resp = getattr(exc, "response", None)
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    except Exception:  # noqa: BLE001
        pass
    return None


def is_retriable_provider_error(
    exc: BaseException, retriable_codes: Optional[Set[int]] = None,
) -> bool:
    """Should this fault trigger failover to the next provider? True on a
    retriable HTTP code OR a quota/billing/availability message (the
    credit-balance-400 class). NEVER raises."""
    codes = retriable_codes if retriable_codes is not None else DEFAULT_RETRIABLE_CODES
    try:
        status = extract_status_code(exc)
        if status is not None:
            if status in codes:
                return True
            # 400 is normally NOT retriable — except the credit/quota body.
            if status == 400:
                msg = str(exc).casefold()
                return any(t in msg for t in _RETRIABLE_MESSAGE_TOKENS)
        # No/unknown code — fall back to message inspection.
        msg = str(exc).casefold()
        return any(t in msg for t in _RETRIABLE_MESSAGE_TOKENS)
    except Exception:  # noqa: BLE001
        return False


ProviderCall = Callable[[Any], Awaitable[Optional[str]]]


@dataclass
class Provider:
    """A named LLM provider — ``call(context) -> text``. The context is the
    exact payload; the orchestrator hands the SAME one to each provider."""
    name: str
    call: ProviderCall


@dataclass
class FailoverResult:
    ok: bool = False
    text: str = ""
    provider: str = ""
    attempts: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def failed_over(self) -> bool:
        """True if a provider other than the first succeeded."""
        return self.ok and bool(self.attempts) and self.provider != self.attempts[0]


async def generate_with_failover(
    context: Any,
    providers: List[Provider],
    *,
    retriable_codes: Optional[Set[int]] = None,
    on_failover: Optional[Callable[[str, BaseException], None]] = None,
) -> FailoverResult:
    """Try each provider in order with the SAME context. On a retriable
    fault, reroute to the next provider WITHOUT dropping the request. A
    non-retriable fault also advances (a dead provider shouldn't sink the
    request) but is logged distinctly. Returns the first non-empty
    response, or a failed result if every provider is exhausted. NEVER
    raises."""
    result = FailoverResult()
    last_error = ""
    for provider in providers:
        result.attempts.append(provider.name)
        try:
            text = await provider.call(context)
            if text and str(text).strip():
                result.ok = True
                result.text = str(text)
                result.provider = provider.name
                return result
            last_error = f"{provider.name}: empty response"
        except Exception as exc:  # noqa: BLE001
            retriable = is_retriable_provider_error(exc, retriable_codes)
            last_error = f"{provider.name}: {exc}"
            if retriable:
                logger.warning(
                    "[ActiveFailover] %s retriable fault (%s) — rerouting to "
                    "next provider", provider.name,
                    extract_status_code(exc) or "no-code")
                if on_failover:
                    try:
                        on_failover(provider.name, exc)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                logger.warning(
                    "[ActiveFailover] %s non-retriable fault — advancing: %s",
                    provider.name, exc)
            continue
    result.ok = False
    result.error = last_error or "no_providers"
    return result


__all__ = [
    "DEFAULT_RETRIABLE_CODES", "extract_status_code",
    "is_retriable_provider_error", "Provider", "FailoverResult",
    "generate_with_failover",
]
