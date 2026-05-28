"""Slice 39 Task 5 — DoubleWord Transport-vs-Upstream Failure Disambiguator.

Bifurcation rationale (v34 evidence, PRD §49.6.2)
---------------------------------------------------
The v34 battle-test soak surfaced two structurally distinct DW failure
shapes that require DIFFERENT recovery strategies:

  TRANSPORT — a socket/protocol-level fault.  The peer dropped the TCP
    connection, the TTFT timer expired before any byte arrived, or
    aiohttp signalled ServerDisconnectedError / ClientConnectorError.
    The model never had a chance to speak.  Recovery: flush the
    connection pool and immediately re-probe a *different* socket
    (Task 6 hard-flush path).

  UPSTREAM — the transport layer was *healthy*; the server answered with
    HTTP 200 and a well-formed SSE envelope, but emitted zero content
    tokens before [DONE].  Classic DW ``done_before_content``.  Also
    covers HTTP 5xx responses: the server answered, meaning the socket
    was fine — the model or endpoint faulted server-side.  Recovery:
    demote the model in the PromotionLedger; flushing the connection
    pool would be counterproductive (the socket is healthy) and risks a
    brute-force re-probe loop against a capacity-constrained upstream.

Design goals
------------
1. Pure function — ``classify_surface_failure`` has no I/O, no env
   reads, no side-effects.  Fully deterministic from the ProbeOutcome
   fields alone.
2. Precedence: UPSTREAM markers are checked before TRANSPORT markers.
   ``done_before_content`` therefore always wins, even if a transport
   keyword appears coincidentally in the same message (shouldn't happen,
   but defensive ordering prevents misclassification).
3. Conservative default: ambiguous outcomes (no recognised marker, no
   5xx) resolve to UPSTREAM.  We never flush a connection pool on
   ambiguous signal; Task 6's raw bypass-probe is the mechanism that
   promotes ambiguous → transport when warranted.

References
----------
* v34 soak postmortem — ``memory/project_v34_soak_postmortem.md``
* PRD §49.6.2 — Transport Disambiguation
* Slice 39 plan — ``memory/project_slice_39_transport_health.md``
"""

from __future__ import annotations

import logging
from enum import Enum

# Module-level import (no cycle: preflight_probe does not import this module).
# Hoisted so the raw_http_bypass_probe error path can construct a failed
# ProbeOutcome without a re-import that could itself raise — guarantees the
# documented NEVER-raises contract.
from backend.core.ouroboros.governance.preflight_probe import ProbeOutcome

logger = logging.getLogger("Ouroboros.TransportDisambiguator")

# ---------------------------------------------------------------------------
# Marker tables — all matched case-insensitively as substrings of the
# concatenated (error_message + " " + error_body) blob.
# ---------------------------------------------------------------------------

# Outcomes that indicate the upstream *server* is at fault even when the
# transport layer was healthy (HTTP 200 + clean SSE envelope that produced
# zero content tokens, or a structured server-side fault).
_UPSTREAM_MARKERS: tuple[str, ...] = ("done_before_content",)

# Outcomes that indicate a socket/protocol-level fault.  The model never
# had an opportunity to produce output.
_TRANSPORT_MARKERS: tuple[str, ...] = (
    "stream_closed_early",
    "ttft_timeout",
    "asyncio.wait_for",
    "serverdisconnected",
    "clientconnector",
    "connectionreset",
    "connection reset",
    "session_acquire_failed",
    "prober_raised",      # inner adapter catch (preflight_probe.py:753)
    "probe_raised",       # outer run_preflight catch (preflight_probe.py:428)
    "connecttimeout",
    "connect timeout",
    # ``transport:{ExcType}:...`` prefix is emitted ONLY on the streaming
    # socket-fault path (dw_heavy_probe.py:790) — covers every aiohttp
    # transport exception (ServerTimeoutError / ClientPayloadError /
    # ClientOSError / ...) by prefix instead of enumerating exception names.
    # Safe: this prefix never appears on done_before_content / status_* /
    # entitlement paths, and UPSTREAM markers are checked first regardless.
    "transport:",
)


class FailureClass(str, Enum):
    """Closed taxonomy of surface-failure classifications.

    Values are lowercase strings so they can be embedded directly in
    structured log lines and SSE payloads without further conversion.
    """

    NONE = "none"
    """ProbeOutcome.success is True — no failure to classify."""

    TRANSPORT = "transport"
    """Socket/protocol-level fault — connection pool flush warranted (Task 6)."""

    UPSTREAM = "upstream"
    """Server-side fault or healthy-transport empty-completion — no pool flush."""


def classify_surface_failure(outcome) -> FailureClass:
    """Classify a ``ProbeOutcome`` into a ``FailureClass``.

    Pure function — no I/O, no env reads, no side-effects.

    Precedence (highest to lowest):
      1. success=True                        → NONE
      2. UPSTREAM marker in blob             → UPSTREAM  (checked before transport)
      3. TRANSPORT marker in blob            → TRANSPORT
      4. HTTP 5xx status_code                → UPSTREAM  (server answered = socket ok)
      5. anything else                       → UPSTREAM  (conservative default)

    The conservative default (step 5) is intentional: we never flush a
    connection pool on an ambiguous signal.  Task 6's raw bypass-probe
    is the escalation mechanism for ambiguous → transport promotion.

    Args:
        outcome: Any object with ``success``, ``error_message``,
                 ``error_body``, and ``status_code`` attributes
                 (structurally duck-typed so tests and adapters need not
                 import this module to satisfy the type).

    Returns:
        ``FailureClass`` enum member.
    """
    # 1. Healthy probe — nothing to classify.
    if getattr(outcome, "success", False):
        return FailureClass.NONE

    # 2+3. Build a single lowercase blob from both message fields (handle None
    #      defensively — callers may pass partially-populated outcomes).
    msg = getattr(outcome, "error_message", None) or ""
    body = getattr(outcome, "error_body", None) or ""
    blob = (msg + " " + body).lower()

    # 2. UPSTREAM markers take unconditional precedence.
    if any(marker in blob for marker in _UPSTREAM_MARKERS):
        logger.debug("classify_surface_failure: UPSTREAM (upstream_marker) blob=%r", blob[:120])
        return FailureClass.UPSTREAM

    # 3. Transport markers — socket/protocol fault.
    if any(marker in blob for marker in _TRANSPORT_MARKERS):
        logger.debug("classify_surface_failure: TRANSPORT (transport_marker) blob=%r", blob[:120])
        return FailureClass.TRANSPORT

    # 4. HTTP 5xx — server answered, socket was healthy.
    status_code = getattr(outcome, "status_code", 0) or 0
    if 500 <= status_code < 600:
        logger.debug(
            "classify_surface_failure: UPSTREAM (http_5xx) status=%s", status_code
        )
        return FailureClass.UPSTREAM

    # 5. Conservative default — ambiguous signal; never flush on ambiguity.
    logger.debug(
        "classify_surface_failure: UPSTREAM (default_conservative) blob=%r status=%s",
        blob[:120],
        status_code,
    )
    return FailureClass.UPSTREAM


# ---------------------------------------------------------------------------
# Task 6 — raw bypass probe + disambiguate_and_recover
# ---------------------------------------------------------------------------

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


def _envb(name: str, default: bool) -> bool:
    """Read a boolean env var (true/1/yes → True; false/0/no → False)."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return default


@dataclass(frozen=True)
class DisambiguationResult:
    """Outcome of :func:`disambiguate_and_recover`.

    Fields
    ------
    failure_class : FailureClass
        Classification of the original outcome.
    raw_probe_succeeded : Optional[bool]
        Result of the bypass probe.  ``None`` when no bypass probe was run
        (UPSTREAM or NONE branch, or probe disabled).
    flushed : bool
        Whether ``lifecycle.flush_transport_pool`` was called and returned
        truthy.  Always ``False`` on the UPSTREAM branch (flush-bypass
        invariant).
    surface_verdict_value : str
        Human-readable verdict token for logs and SSE payloads.
    diagnostic : str
        Short diagnostic string extracted from the original outcome.
    """

    failure_class: FailureClass
    raw_probe_succeeded: Optional[bool]
    flushed: bool
    surface_verdict_value: str
    diagnostic: str


async def raw_http_bypass_probe(provider: object, model_id: str) -> object:
    """Probe the streaming surface via a FRESH aiohttp session/connector.

    Bypasses the provider's pooled session entirely — uses a one-shot
    ``TCPConnector(limit=1, ttl_dns_cache=0, force_close=True)`` so stale
    pooled sockets cannot contaminate the result.

    Returns a ``ProbeOutcome`` (success or not).  NEVER raises.
    """
    # Lazy imports keep startup cost and circular-import risk minimal.
    session = None
    connector = None
    try:
        import aiohttp  # type: ignore[import]

        from backend.core.ouroboros.governance.dw_heavy_probe import HeavyProber
        from backend.core.ouroboros.governance.preflight_probe import (
            _heavyresult_to_outcome,
        )

        connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=0, force_close=True)
        session = aiohttp.ClientSession(connector=connector, trust_env=True)

        prober = HeavyProber()
        result = await prober.probe(
            session=session,
            model_id=model_id,
            base_url=getattr(provider, "_base_url", ""),
            api_key=getattr(provider, "_api_key", ""),
        )
        return _heavyresult_to_outcome(result)

    except Exception as exc:  # pylint: disable=broad-except
        # Module-level ProbeOutcome (imported at top) guarantees this path
        # can construct a result without a re-import that could itself raise.
        logger.debug("raw_http_bypass_probe raised: %s: %s", type(exc).__name__, str(exc)[:120])
        return ProbeOutcome(
            model_id=model_id,
            success=False,
            status_code=0,
            error_message=f"raw_bypass_raised:{type(exc).__name__}:{str(exc)[:120]}",
        )

    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass


def _flip_topology_breaker(model_id: str, diagnostic: str) -> None:
    """Best-effort topology breaker flip for UPSTREAM failures.

    Lazy-imports ``get_default_sentinel`` and calls ``report_failure`` with
    ``FailureSource.LIVE_TRANSPORT`` (non-terminal — live transport degraded,
    not deterministically dead).  NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (
            FailureSource,
            get_default_sentinel,
        )

        sentinel = get_default_sentinel()
        sentinel.report_failure(
            model_id,
            FailureSource.LIVE_TRANSPORT,
            diagnostic[:200],
            status_code=0,
            response_body=diagnostic[:200],
            is_terminal=False,
        )
        logger.debug(
            "_flip_topology_breaker: reported LIVE_TRANSPORT failure model=%s diag=%r",
            model_id,
            diagnostic[:80],
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("_flip_topology_breaker: suppressed %s: %s", type(exc).__name__, str(exc)[:120])


async def disambiguate_and_recover(
    *,
    provider: object,
    outcome: object,
    lifecycle: Any,
    raw_probe_fn: Optional[Callable[[object, str], Awaitable[object]]] = None,
) -> DisambiguationResult:
    """Classify a Surface-B failure and route recovery BY class.

    Flush-bypass invariant (the heart of Slice 39)
    -----------------------------------------------
    UPSTREAM (``done_before_content``, HTTP 5xx, conservative default)
      → ``lifecycle.flush_transport_pool`` is **NEVER called**.  The socket
        is healthy; flushing and re-probing the same empty stream is a
        brute-force loop against a capacity-constrained upstream.  Instead
        the topology breaker is updated to reflect degraded upstream capacity.

    TRANSPORT (socket/protocol-level fault)
      → A raw bypass probe is fired through a FRESH aiohttp session.
        ONLY if the fresh socket succeeds (true pool stagnation) is
        ``lifecycle.flush_transport_pool`` called.

    NONE (success=True)
      → No-op.

    Args:
        provider:      The DW provider instance (used for base_url/api_key
                       extraction by the raw bypass probe).
        outcome:       A ``ProbeOutcome``-shaped object describing the failure.
        lifecycle:     A ``ClientLifecycleManager``-shaped object with an
                       async ``flush_transport_pool(provider, *, reason)``
                       method.
        raw_probe_fn:  Optional override for the bypass probe (injected by
                       tests to avoid real network calls).  Defaults to
                       :func:`raw_http_bypass_probe`.

    Returns:
        :class:`DisambiguationResult` — fully described, never raises.
    """
    cls = classify_surface_failure(outcome)
    model_id: str = getattr(outcome, "model_id", "") or ""
    diag: str = getattr(outcome, "error_message", "") or ""

    # ------------------------------------------------------------------
    # NONE — healthy probe, nothing to do.
    # ------------------------------------------------------------------
    if cls is FailureClass.NONE:
        return DisambiguationResult(
            failure_class=cls,
            raw_probe_succeeded=None,
            flushed=False,
            surface_verdict_value="healthy",
            diagnostic="",
        )

    # ------------------------------------------------------------------
    # UPSTREAM — flush is UNCONDITIONALLY BYPASSED.
    # The socket is healthy; the model/endpoint faulted server-side.
    # ------------------------------------------------------------------
    if cls is FailureClass.UPSTREAM:
        logger.warning(
            "disambiguate_and_recover: UPSTREAM failure — flush BYPASSED "
            "(socket healthy); marking upstream_degraded model=%s diag=%r",
            model_id,
            diag[:80],
        )
        _flip_topology_breaker(model_id, diag or "upstream")
        return DisambiguationResult(
            failure_class=cls,
            raw_probe_succeeded=None,
            flushed=False,
            surface_verdict_value="upstream_degraded",
            diagnostic=diag or "upstream",
        )

    # ------------------------------------------------------------------
    # TRANSPORT — raw bypass probe decides whether to flush.
    # ------------------------------------------------------------------
    if not _envb("JARVIS_DW_RAW_BYPASS_PROBE_ENABLED", True):
        logger.debug(
            "disambiguate_and_recover: raw bypass probe disabled (env off) model=%s",
            model_id,
        )
        return DisambiguationResult(
            failure_class=cls,
            raw_probe_succeeded=None,
            flushed=False,
            surface_verdict_value="transport_degraded",
            diagnostic="raw_probe_disabled",
        )

    fn = raw_probe_fn if raw_probe_fn is not None else raw_http_bypass_probe
    raw = await fn(provider, model_id)
    raw_ok: bool = bool(getattr(raw, "success", False))

    if raw_ok:
        # Fresh socket succeeded → the existing pool has stale connections.
        logger.info(
            "disambiguate_and_recover: raw bypass probe SUCCEEDED while pooled "
            "probe failed — pool stagnation confirmed; flushing pool model=%s",
            model_id,
        )
        flushed = bool(await lifecycle.flush_transport_pool(provider, reason=f"pool_stagnation:{model_id}"))
    else:
        # Fresh socket ALSO failed → systemic transport issue; flushing won't help.
        logger.info(
            "disambiguate_and_recover: raw bypass ALSO failed — systemic transport "
            "issue, no flush model=%s",
            model_id,
        )
        flushed = False

    return DisambiguationResult(
        failure_class=cls,
        raw_probe_succeeded=raw_ok,
        flushed=flushed,
        surface_verdict_value="transport_degraded",
        diagnostic=diag or "transport",
    )
