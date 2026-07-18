"""KimiProvider — Kimi K3 (Moonshot) third-tier adapter.

**STATUS: GROUNDWORK — INERT.** This adapter is credential-gated
(``is_available()`` is False without ``MOONSHOT_API_KEY``) and is NOT wired into
the live provider mesh, dispatch, or construction. It requires a live dry-run
capability scout (verifying K3 model entitlement + telemetry parsing) before
activation. Landing it inert keeps unvalidated network code out of the critical
routing path (the bulletproof mandate) while making the sanitization + routing
policy real + unit-tested today.

Kimi K3 is OpenAI-compatible (``https://api.moonshot.ai/v1``, Bearer
``MOONSHOT_API_KEY``). Two hard constraints shape this adapter:

  1. **Locked sampling params** (``temperature``/``top_p``/``n``/penalties) are
     rejected server-side (400). They are stripped DECLARATIVELY at egress via
     :func:`dw_egress_interceptor.sanitize_egress_body` (the ``"kimi"``/``"moonshot"``
     rule) — the root-cause fix, never a try/except on the 400.
  2. **reasoning_effort is locked to ``"max"``**, billed as OUTPUT tokens
     ($15/1M) — a severe latency + spend edge case. Semantic Budget Routing
     (:func:`kimi_route_eligible`) gates the lane to COMPLEX planning or
     DW-RT-denial failover ONLY; NEVER IMMEDIATE (TTFT SLA) or
     BACKGROUND/SPECULATIVE (bulk reasoning spend).

Pricing (operator-verified 2026-07-18): $3.00/1M input, $15.00/1M output, 90%
prompt-cache discount ($0.30/1M) on cached input.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

KIMI_PROVIDER_SCHEMA_VERSION = "kimi_provider.v1"

# The exact sampling params Kimi K3 locks server-side. Kept here as the source of
# truth for the egress sanitize rule (mirrored in dw_egress_interceptor).
KIMI_LOCKED_PARAMS = (
    "temperature", "top_p", "n",
    "presence_penalty", "frequency_penalty", "logprobs", "top_logprobs",
)

_FALSY = ("0", "false", "no", "off")
_MOONSHOT_KEY_ENV = "MOONSHOT_API_KEY"


# ---------------------------------------------------------------------------
# Config — all env-driven, no hardcoded model ids.
# ---------------------------------------------------------------------------


def kimi_provider_enabled() -> bool:
    """``JARVIS_KIMI_PROVIDER_ENABLED`` — master switch, default **OFF**. The
    adapter stays inert (never selected, never dispatched) until an operator
    explicitly enables it AND a credential is present. NEVER raises."""
    return os.environ.get(
        "JARVIS_KIMI_PROVIDER_ENABLED", "false",
    ).strip().lower() not in _FALSY


def kimi_base_url() -> str:
    """``KIMI_BASE_URL`` (default the Moonshot OpenAI-compatible endpoint)."""
    return os.environ.get(
        "KIMI_BASE_URL", "https://api.moonshot.ai/v1",
    ).strip() or "https://api.moonshot.ai/v1"


def kimi_model() -> str:
    """``JARVIS_KIMI_MODEL`` — the K3 model id (no hardcoded default beyond the
    documented family tag; the capability scout confirms the exact entitled id)."""
    return os.environ.get("JARVIS_KIMI_MODEL", "kimi-k3").strip() or "kimi-k3"


def moonshot_api_key() -> str:
    """The Moonshot bearer credential from the environment ("" if unset). NEVER
    raises."""
    try:
        return os.environ.get(_MOONSHOT_KEY_ENV, "").strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Semantic Budget Routing — the lane gate (pure, testable in isolation).
# ---------------------------------------------------------------------------


def kimi_route_eligible(route_value: str, *, dw_rt_denied: bool = False) -> bool:
    """Whether the Kimi lane may serve *route_value*, given locked ``max``
    reasoning (latency + output-token spend).

      * IMMEDIATE               → False (mandatory reasoning trace blows the TTFT SLA)
      * BACKGROUND / SPECULATIVE → False (bulk — the reasoning spend defeats the
                                   cost-optimized lane; that is the Liquidity Pool's job)
      * COMPLEX                 → True  (the "plans + reasons" tier — Kimi's home)
      * STANDARD                → True ONLY as DW-RT-denial FAILOVER (an
                                   independently-entitled alternative when DW-RT is
                                   auth-denied); otherwise False
      * anything else           → False

    Pure + NEVER raises."""
    try:
        rv = str(route_value or "").strip().lower()
        if rv == "complex":
            return True
        if rv == "standard":
            return bool(dw_rt_denied)      # failover-only for the premium lane
        return False                        # immediate / background / speculative / other
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------


class KimiProvider:
    """Minimal OpenAI-compatible Kimi K3 adapter. Payload assembly + native
    sanitization + availability gating are real + tested; network dispatch is
    credential-gated and intentionally NOT wired into the live mesh yet."""

    def __init__(
        self, *, api_key: Optional[str] = None,
        base_url: Optional[str] = None, model: Optional[str] = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else moonshot_api_key())
        self._base_url = (base_url or kimi_base_url()).rstrip("/")
        self._model = model or kimi_model()

    @property
    def model(self) -> str:
        return self._model

    def is_available(self) -> bool:
        """True only when the master switch is ON **and** a credential is present.
        Without both, the adapter is inert (routing must not select it). NEVER
        raises."""
        try:
            return kimi_provider_enabled() and bool(self._api_key)
        except Exception:  # noqa: BLE001
            return False

    def auth_headers(self) -> Dict[str, str]:
        """Bearer auth headers (OpenAI-compatible). NEVER raises."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def build_request_body(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        max_tokens: int = 16384,
        stream: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Assemble the OpenAI-shaped request body and NATIVELY sanitize it: the
        locked sampling params are stripped declaratively via the shared egress
        interceptor (DRY — the same ``sanitize_egress_body`` DW uses), so a locked
        param can never reach the wire even if a caller injects one via *extra*.
        ``reasoning_effort`` is stamped ``"max"`` (the only value K3 accepts).
        NEVER raises — a sanitizer fault returns the pre-sanitized body."""
        mdl = (model or self._model)
        body: Dict[str, Any] = {
            "model": mdl,
            "messages": list(messages or []),
            "max_tokens": int(max_tokens),
            "stream": bool(stream),
            "reasoning_effort": "max",     # locked server-side; explicit for clarity
        }
        if extra:
            body.update(extra)
        # Root-cause sanitization (declarative rule registry, NOT try/except-on-400).
        try:
            from backend.core.ouroboros.governance.dw_egress_interceptor import (  # noqa: PLC0415
                sanitize_egress_body,
            )
            body = sanitize_egress_body(body, mdl)
        except Exception:  # noqa: BLE001 — defensive; sanitize is itself fail-soft
            logger.debug("[KimiProvider] egress sanitize degraded", exc_info=True)
        return body


def resolve_kimi_availability() -> "tuple[bool, str]":
    """The parity predicate mirroring ``provider_availability._resolve_dw`` /
    ``_resolve_claude``: (available, reason). Available iff the master switch is
    ON and a credential is present; the reason is a forensic §7 label. Sync +
    NEVER raises. (Kept here so a future ``_resolve_kimi`` in provider_availability
    can delegate without re-deriving the credential logic.)"""
    try:
        if not kimi_provider_enabled():
            return False, "disabled"
        if not moonshot_api_key():
            return False, "no_credential"
        return True, "available"
    except Exception:  # noqa: BLE001
        return False, "fail_soft"


def stats() -> Dict[str, Any]:
    """Read-only observability snapshot. NEVER raises."""
    try:
        avail, reason = resolve_kimi_availability()
        return {
            "enabled": kimi_provider_enabled(),
            "available": avail,
            "reason": reason,
            "base_url": kimi_base_url(),
            "model": kimi_model(),
            "has_credential": bool(moonshot_api_key()),
            "locked_params": list(KIMI_LOCKED_PARAMS),
            "schema_version": KIMI_PROVIDER_SCHEMA_VERSION,
        }
    except Exception:  # noqa: BLE001
        return {"schema_version": KIMI_PROVIDER_SCHEMA_VERSION}
