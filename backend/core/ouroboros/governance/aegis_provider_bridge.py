"""Aegis Provider Bridge — single canonical factory for credentialed
upstream clients (Slice 2B-ii).

# Architectural role

Aegis-1 shipped the credential-confiscation substrate (daemon, lease
primitives, bootstrap PSK handoff). Aegis-2B-i shipped the forwarding
surface (``/v1/messages`` + ``/v1/chat/completions`` + DW passthroughs).
This module is the single seam through which O+V's provider modules
construct upstream clients — when ``aegis.client.is_enabled()`` is
true, the constructed client routes through the Aegis daemon and
carries NO real upstream credentials. The real credentials live only
in the Aegis daemon's confiscated env (Slice 1).

AST pins in ``test_slice2bii_aegis_proxy_bridge.py`` enforce that no
module outside this file constructs ``AsyncAnthropic(...)`` or composes
``"Bearer <DW key>"`` headers. Every upstream call site routes through
the public API below.

# Public surface

  * :func:`make_async_anthropic_client` — Anthropic client factory.
    When Aegis enabled: ``base_url=JARVIS_AEGIS_URL`` (host root —
    the SDK appends ``/v1/messages``), ``api_key=<placeholder>``.
    When disabled: byte-identical to ``AsyncAnthropic(api_key=...)``.

  * :func:`dw_aegis_base_url` — DW base_url for f-string composition.
    When Aegis enabled: ``{JARVIS_AEGIS_URL}/v1`` (with ``/v1`` suffix
    because DW provider composes ``f"{base}/chat/completions"``).
    When disabled: env ``DOUBLEWORD_BASE_URL`` or
    ``https://api.doubleword.ai/v1``.

  * :func:`dw_authorization_header` — DW Authorization header dict.
    When Aegis enabled: ``{}`` (Aegis injects upstream bearer
    server-side). When disabled: ``{"Authorization": f"Bearer {DW_KEY}"}``.

  * :func:`acquire_call_lease` — PER-CALL lease acquisition. RAISES
    on failure — no silent fallback to direct upstream credentials.

  * :func:`merge_lease_header` — helper to compose ``extra_headers``
    dicts with an optional X-JARVIS-Lease token.

# Operator corrections honored (v2 revised design — 2026-05-24)

  1. Anthropic ``base_url`` is the host root, NOT host+/v1.
  2. ``messages.stream(...)`` and ``messages.create(...)`` both
     covered by the same client factory (transport swap is shared).
  3. ALL DW credentialed endpoints (chat/completions + files +
     batches + models) route through Aegis — Aegis registry already
     allowlists all 7.
  4. Per-call lease only — no ``default_headers`` X-JARVIS-Lease.
  5. ``acquire_call_lease`` RAISES on failure; provider sites do
     NOT swallow the exception.
  6. Tests prove wire behavior via ``httpx.MockTransport`` capture.

# Non-goals (deferred to separate slices)

  * Graduating ``JARVIS_AEGIS_ENABLED`` to default-TRUE — that's
    Slice 2B-iii (post-soak proof required).
  * SSE parsing / streaming / chunking — 0 changes (operator
    binding "transport-layer swap only").
  * Aegis daemon / forwarding / lease.py — 0 changes (substrate is
    shipped; this module is the consumer).
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict, Optional

from backend.core.ouroboros.aegis import client as aegis_client_mod

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Placeholder API keys
# ──────────────────────────────────────────────────────────────────────
#
# When Aegis is enabled, the constructed client carries a non-empty
# placeholder string — both the Anthropic SDK and aiohttp validators
# reject empty/None keys. The Aegis daemon's forwarding handler
# REPLACES this with the real confiscated credential server-side
# before the upstream request is dispatched.

_AEGIS_PLACEHOLDER_ANTHROPIC_KEY: str = (
    "aegis-managed-no-real-key-do-not-use"
)


# ──────────────────────────────────────────────────────────────────────
# Anthropic client factory
# ──────────────────────────────────────────────────────────────────────

def make_async_anthropic_client(
    *,
    api_key: Optional[str] = None,
    http_client: Optional[Any] = None,
    **extra_kwargs: Any,
) -> Any:
    """Single canonical factory for ``anthropic.AsyncAnthropic``.

    When :func:`aegis.client.is_enabled` returns True:
      * ``base_url`` is set to ``JARVIS_AEGIS_URL`` (host root). The
        Anthropic SDK appends ``/v1/messages`` to that internally —
        the final outbound URL is ``{JARVIS_AEGIS_URL}/v1/messages``.
        DO NOT prepend ``/v1`` to base_url; that produces the
        ``/v1/v1/messages`` bug the operator flagged.
      * ``api_key`` is set to a non-empty placeholder. The Aegis
        daemon's forwarding handler replaces it with the real
        confiscated ``ANTHROPIC_API_KEY`` server-side.

    When Aegis is disabled, this is byte-equivalent to constructing
    ``AsyncAnthropic(api_key=api_key, ...)`` directly — caller's
    ``api_key`` (or env default) flows through unmodified, no
    ``base_url`` override applied.

    Args:
        api_key: Override key (legacy callers may pass explicit
            keys; ignored when Aegis enabled).
        http_client: Optional ``httpx.AsyncClient`` for tests
            (used with ``MockTransport`` to capture wire requests).
        **extra_kwargs: Forwarded to ``AsyncAnthropic.__init__``.

    Returns:
        An ``anthropic.AsyncAnthropic`` instance, configured for
        the appropriate transport.
    """
    # Lazy import — keeps cold-import cost off the hot path for
    # callers that don't construct clients.
    from anthropic import AsyncAnthropic

    if aegis_client_mod.is_enabled():
        aegis_url = os.environ.get(
            aegis_client_mod.ENV_AEGIS_URL, "",
        ).strip().rstrip("/")
        if not aegis_url:
            # Should not happen — is_enabled() checks both env vars.
            # Defensive: surface clearly rather than silently leak.
            raise aegis_client_mod.AegisClientError(
                "is_enabled() returned True but JARVIS_AEGIS_URL "
                "is empty — Aegis preflight state corrupted"
            )
        kwargs: Dict[str, Any] = {
            # Host-root base_url. SDK appends /v1/messages.
            "base_url": aegis_url,
            # Non-empty placeholder — Aegis injects real key upstream.
            "api_key": _AEGIS_PLACEHOLDER_ANTHROPIC_KEY,
        }
        if http_client is not None:
            kwargs["http_client"] = http_client
        kwargs.update(extra_kwargs)
        logger.debug(
            "[ProviderBridge] AsyncAnthropic via Aegis: base_url=%s "
            "(real key confiscated by daemon)",
            aegis_url,
        )
        return AsyncAnthropic(**kwargs)

    # Legacy path — byte-identical to direct construction.
    legacy_kwargs: Dict[str, Any] = {}
    if api_key is not None:
        legacy_kwargs["api_key"] = api_key
    if http_client is not None:
        legacy_kwargs["http_client"] = http_client
    legacy_kwargs.update(extra_kwargs)
    return AsyncAnthropic(**legacy_kwargs)


# ──────────────────────────────────────────────────────────────────────
# DoubleWord transport configuration
# ──────────────────────────────────────────────────────────────────────

# Environment-driven legacy default. Mirrors doubleword_provider.py's
# pre-bridge constant — kept in sync intentionally so legacy callers
# see byte-identical behavior when Aegis is disabled.
_LEGACY_DW_BASE_URL_DEFAULT: str = "https://api.doubleword.ai/v1"


def dw_aegis_base_url() -> str:
    """Return the base URL that DW provider should use for f-string
    composition of credentialed paths.

    When Aegis enabled: ``{JARVIS_AEGIS_URL}/v1``. The ``/v1`` suffix
    is REQUIRED because the DW provider composes URLs like
    ``f"{base}/chat/completions"`` — the result must match the
    Aegis allowlisted route ``/v1/chat/completions``.

    When disabled: ``DOUBLEWORD_BASE_URL`` env var or the legacy
    ``https://api.doubleword.ai/v1`` default.
    """
    if aegis_client_mod.is_enabled():
        aegis_url = os.environ.get(
            aegis_client_mod.ENV_AEGIS_URL, "",
        ).strip().rstrip("/")
        if not aegis_url:
            raise aegis_client_mod.AegisClientError(
                "is_enabled() returned True but JARVIS_AEGIS_URL "
                "is empty — Aegis preflight state corrupted"
            )
        return f"{aegis_url}/v1"
    return os.environ.get(
        "DOUBLEWORD_BASE_URL", _LEGACY_DW_BASE_URL_DEFAULT,
    )


async def dw_session_auth_header() -> Dict[str, str]:
    """Slice 31 — Aegis-aware Authorization header builder for DW HTTP calls.

    Returns the ``Authorization`` header dict that must accompany
    every outbound HTTP call to the DW endpoint (when routed through
    Aegis). This is the **session bearer** the Aegis passthrough
    endpoint requires (``passthrough.py:_bearer_session`` extracts
    the token from ``Authorization: Bearer <token>``); it is NOT the
    DW API key and NOT a per-call lease token.

    Behavior matrix:

    * **Aegis enabled** → ``{"Authorization": "Bearer <session_token>"}``
      where ``session_token`` is fetched from ``AegisClient`` via the
      cached session state (single ``/session/establish`` per process;
      subsequent calls return the cached value with no daemon
      round-trip). On Aegis client error: returns ``{}`` rather than
      raising — defensive, lets the caller surface the real 401 from
      the daemon if cred path is broken.
    * **Aegis disabled** → ``{"Authorization": "Bearer <DOUBLEWORD_API_KEY>"}``
      (byte-identical to legacy ``dw_authorization_header()`` non-Aegis
      branch).

    Why this is separate from ``dw_authorization_header()``: the
    legacy sync helper returns ``{}`` for Aegis-enabled because
    pre-Slice-31 the assumption was Aegis injects the bearer
    SERVER-SIDE. v24 forensic (``bt-2026-05-27-183704``) showed the
    daemon actually requires ``Authorization: Bearer <session_token>``
    from the CLIENT at the passthrough layer (``missing_session_bearer``
    401). Slice 31 closes the gap by fetching the session token via
    this new async helper and including it in every outbound header.

    Composition pattern at call sites::

        auth_headers = await dw_session_auth_header()
        lease = await acquire_call_lease(op_id=..., route=..., ...)
        headers = merge_lease_into_session_headers(auth_headers, lease)
        async with session.post(..., headers=headers) as resp:
            ...
    """
    if aegis_client_mod.is_enabled():
        try:
            client = await aegis_client_mod.AegisClient.get()
            token = await client._ensure_session_token()
            return {"Authorization": _compose_bearer(token)}
        except Exception:  # noqa: BLE001 — defensive
            # Fallback to no Auth header — daemon will return its own
            # structured 401 which the caller's existing error path
            # surfaces. This is preferable to raising into the upload
            # call site which would short-circuit observability.
            return {}
    # Aegis disabled — legacy path: real DW API key as Bearer
    dw_key = os.environ.get("DOUBLEWORD_API_KEY", "").strip()
    if not dw_key:
        return {}
    return {"Authorization": _compose_bearer(dw_key)}


def dw_authorization_header() -> Dict[str, str]:
    """Return the Authorization header dict for the DW session.

    When Aegis enabled: empty dict. The Aegis daemon's forwarding
    handler injects the real ``DOUBLEWORD_API_KEY`` server-side. The
    O+V session must NOT hold the real bearer token — that defeats
    the Zero-Trust posture.

    When disabled: ``{"Authorization": "Bearer <DOUBLEWORD_API_KEY>"}``.

    .. note::
       Slice 31 — for Aegis-routed calls, this legacy sync helper
       returns ``{}`` which is correct for the *DW Authorization*
       header (Aegis daemon injects DW key server-side) but is
       INSUFFICIENT for the *Aegis session bearer* the daemon
       requires from the client. New code must use
       :func:`dw_session_auth_header` (async) which returns the
       session bearer when Aegis is enabled.
    """
    if aegis_client_mod.is_enabled():
        return {}
    dw_key = os.environ.get("DOUBLEWORD_API_KEY", "").strip()
    if not dw_key:
        return {}
    return {"Authorization": _compose_bearer(dw_key)}


def _compose_bearer(key: str) -> str:
    """Single private composer for ``Bearer <key>`` — keeps the
    literal string concentrated in one ~10-char function so AST pins
    elsewhere can stay surgical."""
    return f"Bearer {key}"


def compose_dw_bearer_header(api_key: str) -> Dict[str, str]:
    """Public legacy-path bearer composer for callers that already
    have an explicit ``api_key`` in scope (e.g. ``dw_heavy_probe``
    which receives it as a method parameter rather than reading env).

    Single seam — keeps the literal ``"Bearer "`` string concentrated
    in this module so AST pins in other files can forbid the f-string
    pattern locally (Slice 2B-ii.2's
    ``test_ast_pin_heavy_probe_session_post_carries_lease_header``).

    Returns ``{"Authorization": "Bearer {api_key}"}`` when ``api_key``
    is truthy; empty dict otherwise. Callers should typically use
    :func:`dw_authorization_header` instead (which reads from env);
    this variant exists for paths that thread the key explicitly.
    """
    if not api_key:
        return {}
    return {"Authorization": _compose_bearer(api_key)}


# ──────────────────────────────────────────────────────────────────────
# Per-call lease acquisition
# ──────────────────────────────────────────────────────────────────────

async def acquire_call_lease(
    *,
    op_id: str,
    route: str,
    estimated_cost_usd: float,
    causal_lineage_hash: str = "",
) -> Optional[str]:
    """Acquire a fresh per-call lease token (PER-CALL — never reuse
    across multiple upstream requests).

    Returns:
        The lease token string to attach as ``X-JARVIS-Lease`` header
        on the upstream call. ``None`` when Aegis is disabled — the
        caller skips header injection cleanly.

    Raises:
        :class:`aegis.client.AegisClientError` when Aegis is enabled
        and lease acquisition fails (daemon unreachable, cap
        exceeded, session expired). NO silent fallback to direct
        upstream credentials — operator correction #5.
    """
    if not aegis_client_mod.is_enabled():
        return None
    client = await aegis_client_mod.AegisClient.get()
    # Raises AegisClientError on any failure — propagated to caller.
    return await client.acquire_lease(
        op_id=op_id,
        route=route,
        estimated_cost_usd=estimated_cost_usd,
        causal_lineage_hash=causal_lineage_hash,
    )


# ──────────────────────────────────────────────────────────────────────
# Header composition helpers
# ──────────────────────────────────────────────────────────────────────

# Canonical X-JARVIS-Lease header name. Single source of truth so
# AST pins + Aegis daemon stay in sync on the exact spelling.
LEASE_HEADER_NAME: str = "X-JARVIS-Lease"


def merge_lease_header(
    extra_headers: Optional[Dict[str, str]],
    lease_token: Optional[str],
) -> Dict[str, str]:
    """Compose an ``extra_headers`` dict with an optional lease token.

    Pattern at call sites::

        lease = await acquire_call_lease(op_id=..., route=..., ...)
        await client.messages.create(
            ...,
            extra_headers=merge_lease_header(existing_headers, lease),
        )

    When ``lease_token`` is None (Aegis disabled), returns
    ``extra_headers`` unchanged (empty dict if input was None) — the
    upstream call proceeds with no Aegis header.
    """
    out: Dict[str, str] = dict(extra_headers or {})
    if lease_token is not None:
        out[LEASE_HEADER_NAME] = lease_token
    return out


def merge_lease_into_session_headers(
    base_headers: Optional[Dict[str, str]],
    lease_token: Optional[str],
) -> Dict[str, str]:
    """DW-side equivalent of :func:`merge_lease_header`.

    Composes ``dw_authorization_header()`` (or caller-supplied
    base_headers) with the per-call lease token. Used at DW
    ``session.post`` / ``session.get`` call sites.
    """
    out: Dict[str, str] = dict(base_headers or {})
    if lease_token is not None:
        out[LEASE_HEADER_NAME] = lease_token
    return out


__all__ = [
    "make_async_anthropic_client",
    "dw_aegis_base_url",
    "dw_authorization_header",
    "compose_dw_bearer_header",
    "acquire_call_lease",
    "merge_lease_header",
    "merge_lease_into_session_headers",
    "LEASE_HEADER_NAME",
]
