"""
DoublewordProvider — Tier 0 batch inference via Doubleword's 397B MoE model.

Implements the CandidateProvider protocol for the Ouroboros governance pipeline.
Uses Doubleword's 4-stage async batch API (upload → create → poll → retrieve).

Boundary Principle:
  Deterministic: Batch protocol, JSONL formatting, polling cadence, cost tracking.
  Agentic: The routing decision to USE Doubleword (complexity > 0.85, ULTRA_TASKS)
           is made by the governance pipeline's routing layer, not this provider.

Doubleword API docs: https://docs.doubleword.ai
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)

# Slice 2B-ii — Aegis Provider Bridge (Zero-Trust transport).
# When JARVIS_AEGIS_ENABLED is true, all credentialed DW upstream
# calls route through the Aegis daemon's /v1/* forwarding surface
# instead of api.doubleword.ai directly. The DW session holds NO
# real Authorization Bearer token in that path; Aegis injects the
# confiscated DOUBLEWORD_API_KEY server-side. Per-call leases via
# X-JARVIS-Lease header (operator correction #4 — never client-wide).
from backend.core.ouroboros.governance.aegis_provider_bridge import (
    acquire_call_lease as _aegis_acquire_call_lease,
    dw_aegis_base_url as _aegis_dw_base_url,
    dw_authorization_header as _aegis_dw_auth_header,
    dw_session_auth_header as _aegis_dw_session_auth_header,
    merge_lease_into_session_headers as _aegis_merge_lease_headers,
)
from backend.core.ouroboros.governance.stream_rupture import (
    CognitiveStallError,
    StreamRuptureError,
    cognitive_stall_timeout_s as _cognitive_stall_timeout_s,
    stream_inter_chunk_timeout_s as _stream_inter_chunk_timeout_s,
    stream_rupture_timeout_s as _stream_rupture_timeout_s,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all env-driven, no hardcoding (Manifesto §5)
# ---------------------------------------------------------------------------

_DW_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
_DW_BASE_URL = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
_DW_MODEL = os.environ.get(
    "DOUBLEWORD_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8"
)
_DW_COMPLETION_WINDOW = os.environ.get("DOUBLEWORD_WINDOW", "1h")
_DW_MAX_TOKENS = int(os.environ.get("DOUBLEWORD_MAX_TOKENS", "16384"))

# Complexity-aware max_tokens: lower ceilings for simpler tasks make DW
# respond faster (fewer tokens to generate) without sacrificing quality.
# Trivial one-liner fixes don't need 16K output tokens.
#
# Why 8192 for trivial (not 4096):
# bt-2026-04-10-091829/debug.log:677 showed DW's STANDARD-demotion path
# emitting a 7401-char JSON response that truncated mid-string for a
# "trivial" requirements.txt op (Python 3.9→3.11 upgrade). The response
# exceeded the 4096-token cap because the generated content was larger
# than the 3518-byte source file (adding packages). A 4096 ceiling gives
# roughly ~7400 chars of JSON-escaped output — exactly where it broke.
# 8192 gives ~14.5KB headroom at ~+$0.002/op — strictly better than
# truncated responses that cost the full op + retry.
_DW_COMPLEXITY_MAX_TOKENS: Dict[str, int] = {
    "trivial": 8192,
    "moderate": 8192,
    "standard": 12288,
    "complex": 16384,
    "heavy_code": 16384,
}

# Dynamic max_tokens constants — parallel to _CLAUDE_OUTPUT_* in providers.py.
# Task #187 added dynamic output budgets to Claude but not DW, so a
# "trivial" task targeting a 7KB requirements.txt was truncated at the
# 4096 ceiling — bt-2026-04-10-091829 debug.log:677 showed DW streaming
# ~4203 chars of valid ASCII content before JSON parse failed with
# "Unterminated string". The formula: needed = (bytes/CHARS_PER_TOKEN) *
# SAFETY + OVERHEAD, floored at the complexity ceiling, capped at
# _DW_MAX_TOKENS. Small files keep the cheap complexity ceiling; large
# files get a proportionally bigger budget so full-file rewrites never
# truncate.
_DW_CHARS_PER_TOKEN = 3.5
_DW_OUTPUT_SAFETY = 1.4
_DW_OUTPUT_OVERHEAD_TOKENS = 2048  # JSON schema wrapper + rationale + slack
_DW_POLL_INTERVAL_S = float(os.environ.get("DOUBLEWORD_POLL_INTERVAL_S", "5"))
_DW_MAX_WAIT_S = float(os.environ.get("DOUBLEWORD_MAX_WAIT_S", "3600"))
_DW_TEMPERATURE = float(os.environ.get("DOUBLEWORD_TEMPERATURE", "0.2"))
_DW_REASONING_EFFORT = os.environ.get("JARVIS_DW_REASONING_EFFORT", "none")


# Slice 55 — complexity → reasoning_effort map. Leaf/utility work goes fast &
# cheap (no chain-of-thought); high-impact core changes get a CoT buffer for
# quality. Derived from the op's task_complexity; the env var remains an
# explicit override / kill-switch (NOT eliminated).
_COMPLEXITY_REASONING_EFFORT = {
    "trivial": "none",
    "simple": "none",
    "moderate": "low",
    "complex": "medium",
    "heavy_code": "high",
    "architectural": "high",
}


# Slice 84 — ordered effort scale + serveability clamp. Direct boundary probes
# (2026-06-03) proved DW streams effort=none/low/medium cleanly (ttfb 1.4-6.3s,
# real content) but RUPTURES the chunked stream at effort=high
# (ClientPayloadError: TransferEncodingError) — which the dispatch then mislabels
# live_transport. So a complexity that derives "high" (heavy_code/architectural)
# must be clamped to a serveable ceiling before it reaches the wire. Default cap
# "medium" (verified serveable, keeps a real CoT buffer); env-tunable up/down.
# The explicit JARVIS_DW_REASONING_EFFORT override is NOT clamped — operator
# intent wins (e.g. a future DW build that serves "high" cleanly).
_EFFORT_ORDER: Tuple[str, ...] = ("none", "low", "medium", "high")
_DW_MAX_EFFORT_DEFAULT: str = "medium"


def _dw_max_reasoning_effort() -> str:
    """Env-tunable serveability ceiling for DW reasoning_effort. Invalid →
    default ``medium``. NEVER raises."""
    raw = os.environ.get("JARVIS_DW_MAX_REASONING_EFFORT", "").strip().lower()
    return raw if raw in _EFFORT_ORDER else _DW_MAX_EFFORT_DEFAULT


def _clamp_reasoning_effort(eff: str) -> str:
    """Clamp ``eff`` down to the serveable ceiling (never exceed it). An
    unknown effort string is returned unchanged (fail-open). Pure."""
    cap = _dw_max_reasoning_effort()
    try:
        if _EFFORT_ORDER.index(eff) > _EFFORT_ORDER.index(cap):
            return cap
    except ValueError:
        pass
    return eff


# Slice 168 — per-MODEL reasoning_effort FLOOR. Root cause (Seb @ Doubleword,
# 2026-06-08, "Cancelled DeepSeek-v4-pro batch"): some DW models REJECT
# reasoning_effort="none" and the batch is cancelled. We derive "none" for
# trivial/simple ops, so a trivial op routed to such a model gets cancelled. Floor the
# effort UP to the model's minimum supported value. Env-driven map (generic
# substring→effort matching — no hardcoded model in the algorithm); the default carries
# the one model DW has told us rejects "none". Override/extend via
# JARVIS_DW_MODEL_MIN_EFFORT="substr:effort,substr:effort". Ideally resolved from DW's
# /v1/models capability metadata once exposed (open question to DW).
_DEFAULT_DW_MODEL_MIN_EFFORT: str = "deepseek-v4-pro:low"


def _dw_model_min_effort_map() -> Dict[str, str]:
    """Parse the env-driven model→min-effort map. NEVER raises."""
    raw = os.environ.get("JARVIS_DW_MODEL_MIN_EFFORT", _DEFAULT_DW_MODEL_MIN_EFFORT)
    out: Dict[str, str] = {}
    try:
        for pair in str(raw).split(","):
            key, sep, val = pair.partition(":")
            key = key.strip().lower()
            val = val.strip().lower()
            if sep and key and val in _EFFORT_ORDER:
                out[key] = val
    except Exception:  # noqa: BLE001
        pass
    return out


# Slice 169 — test hook; when set, replaces the dynamic catalog resolver.
_catalog_min_reasoning_effort_override = None


def _dw_model_min_effort(model_id: str) -> str:
    """Minimum reasoning_effort the target model accepts. Slice 169 — DYNAMIC first:
    resolve from DW's live /v1/models capability metadata (via the catalog) so this
    self-updates as DW adds models / exposes the field. Slice 168 — static env-map
    fallback when the dynamic source has no answer. "none" (no floor) when neither
    matches. NEVER raises."""
    if not model_id:
        return "none"
    mid = str(model_id).strip().lower()
    # Slice 169 — dynamic capability resolution (no hardcode when DW exposes it).
    try:
        _resolver = _catalog_min_reasoning_effort_override
        if _resolver is None:
            from backend.core.ouroboros.governance.dw_catalog_client import (
                catalog_min_reasoning_effort as _resolver,
            )
        _dyn = _resolver(mid)
        if _dyn:
            return _dyn
    except Exception:  # noqa: BLE001 — fall through to the static floor
        pass
    # Slice 168 — static env-map fallback.
    for substr, floor in _dw_model_min_effort_map().items():
        if substr in mid:
            return floor
    return "none"


def _clamp_up_to_min(eff: str, floor: str) -> str:
    """Clamp ``eff`` UP to ``floor`` (never lowers). Symmetric to
    _clamp_reasoning_effort. Unknown strings pass through. Pure."""
    try:
        if _EFFORT_ORDER.index(eff) < _EFFORT_ORDER.index(floor):
            return floor
    except ValueError:
        pass
    return eff


def _reasoning_effort_for(complexity: str = "", model: str = "") -> str:
    """Slice 55/84/168 — resolve reasoning_effort. Explicit
    ``JARVIS_DW_REASONING_EFFORT`` wins (operator override); otherwise derive from the
    op's ``task_complexity`` and clamp to the DW-serveable ceiling (Slice 84 — ``high``
    ruptures DW's chunked stream). Slice 168 — finally floor UP to the target model's
    minimum supported effort so we never send a value the model rejects (e.g.
    deepseek-v4-pro rejects ``none``)."""
    env = os.environ.get("JARVIS_DW_REASONING_EFFORT", "").strip().lower()
    if env:
        base = env
    else:
        derived = _COMPLEXITY_REASONING_EFFORT.get(
            (complexity or "").strip().lower(), "none",
        )
        base = _clamp_reasoning_effort(derived)
    return _clamp_up_to_min(base, _dw_model_min_effort(model))


def _reasoning_request_params(effort: str = "", *, complexity: str = "", model: str = "") -> dict:
    """Slice 54/55 — DoubleWord reasoning-control request params.

    Qwen3.5 are reasoning models that burn the token budget on chain-of-thought
    before emitting ``content``. Verified 2026-06-01: the OpenAI-standard
    ``reasoning_effort`` knob IS honored by DW (``none`` → finish=stop,
    content present, 0 reasoning tokens), while the previously-used
    ``chat_template_kwargs={"enable_thinking": False}`` is silently IGNORED
    (still 62 reasoning tokens, empty content) — which is why suppression never
    worked and every DW candidate came back empty / done_before_content.

    Slice 55 — effort is now derived from ``complexity`` (via
    :func:`_reasoning_effort_for`) so leaf/utility ops stay fast & cheap
    (``none``) while high-impact core changes get a CoT buffer. An explicit
    ``effort`` arg or the ``JARVIS_DW_REASONING_EFFORT`` env override either
    still wins. When effort resolves to ``none`` the (DW-ignored but harmless)
    enable_thinking flag is also sent for intent clarity.
    """
    eff = (effort or _reasoning_effort_for(complexity, model=model) or "none").strip().lower()
    # Slice 168 — apply the per-model floor even to an explicit effort (a model that
    # rejects "none" rejects it however it was chosen).
    eff = _clamp_up_to_min(eff, _dw_model_min_effort(model))
    params: dict = {"reasoning_effort": eff}
    if eff == "none":
        # Belt-and-braces: harmless (DW ignores it) but keeps intent explicit
        # for any future endpoint that honors the chat-template flag.
        params["chat_template_kwargs"] = {"enable_thinking": False}
    return params


def _extract_completion_text(message: dict) -> str:
    """Slice 54 — answer text from a completion message, reasoning-model-aware.

    Reads ``content`` first (populated when ``reasoning_effort=none`` or once
    the model exits the think phase). Falls back to ``reasoning`` then
    ``reasoning_details[].text`` — the CORRECT DW field names. The prior code
    fell back to ``reasoning_content`` which does not exist on these models, so
    it always read empty. NEVER raises.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if content:
        return content
    reasoning = message.get("reasoning")
    if reasoning:
        return reasoning
    details = message.get("reasoning_details")
    if isinstance(details, list):
        texts = [d.get("text", "") for d in details
                 if isinstance(d, dict) and d.get("text")]
        if texts:
            return "\n".join(texts)
    return ""


_DW_CONNECT_TIMEOUT_S = float(os.environ.get("DOUBLEWORD_CONNECT_TIMEOUT_S", "10"))
_DW_REQUEST_TIMEOUT_S = float(os.environ.get("DOUBLEWORD_REQUEST_TIMEOUT_S", "120"))


# ============================================================================
# Slice 36 — Adaptive Transport Selector
# ============================================================================
#
# v31 empirical evidence (bt-2026-05-28-065235):
#
#   * STAGE_RT_HTTP_POST p50 TTFT     = 66,775 ms (DW /v1/chat/completions)
#   * STAGE_RT_VENOM_TOOL_LOOP p50    = 66,849 ms (nested with above)
#   * Phase 0 probe via prompt_only   =  4,000-8,000 ms end-to-end (BATCH)
#
# Same DW account, same Qwen3.5-397B model, same prompt sizes (1-50KB).
# DW's RT streaming endpoint has fundamentally different latency
# characteristics than its batch API. Production picked RT; v25-v31
# produced 0 APPLY events across 6 capability soaks.
#
# Slice 36 makes the transport selection ADAPTIVE: when Claude is
# disabled (pure-DW config), STANDARD/COMPLEX routes auto-switch to
# the empirically-faster BATCH API path. RT path preserved for
# IMMEDIATE / BG / SPECULATIVE (latency-sensitive low-context paths
# where Venom adds value).
#
# Trade-off acknowledged: BATCH path does NOT support the Venom tool
# loop (single-shot generation). v31 evidence: 0 successful Venom
# rounds across all production ops anyway (every RT call timed out).
# Net delta = positive (some candidates >> zero candidates).
#
# Operator escape hatch: ``JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX=0``
# reverts to legacy RT-only routing for STANDARD/COMPLEX.

_SLICE36_FORCE_BATCH_ENV: str = "JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX"

# Slice 159 — DW transport sovereignty. Slice 36 force-batch originally engaged ONLY
# when Claude was explicitly DISABLED. But a *configured* yet *credit-dead* Claude
# leaves _claude_disabled=False → RT streaming → DW SSE rupture (live_transport) →
# cascade to a dead Claude → terminal_quota. The premise "Claude catches RT failures"
# is false whenever Claude's circuit breaker is OPEN. Composes the Slice 146 breaker.
_FORCE_BATCH_ON_BREAKER_ENV: str = "JARVIS_DW_FORCE_BATCH_ON_CLAUDE_BREAKER"


def _force_batch_on_breaker_enabled() -> bool:
    """Master for the Slice 159 breaker-aware force-batch trigger. Default **TRUE**
    (failure-path-only: only fires while the Claude breaker is OPEN, so it can't
    affect the Claude-healthy happy path; =0 reverts to legacy disabled-only). The
    economic breaker (JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED) still governs whether
    the breaker opens at all. NEVER raises."""
    return os.environ.get(_FORCE_BATCH_ON_BREAKER_ENV, "true").strip().lower() \
        not in ("0", "false", "no", "off")


_INTRA_DW_FAILOVER_ENV: str = "JARVIS_DW_INTRA_TRANSPORT_FAILOVER_ENABLED"


def _slice170_intra_dw_failover_enabled() -> bool:
    """Master for the Slice 170 intra-DW transport failover. Default **TRUE**
    (failure-path-only: only fires when the surface-health ledger reports the DW
    streaming wire degraded AND the batch lane healthy — it cannot affect the
    stream-healthy happy path). When ON, a DW transport rupture fails over to DW-batch
    instead of cascading to the expensive Claude fallback, keeping the FUNDED primary
    primary. =0 reverts to the legacy cascade-to-Claude-when-available behaviour.
    NEVER raises."""
    return os.environ.get(_INTRA_DW_FAILOVER_ENV, "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _claude_unavailable_now() -> bool:
    """Slice 171 — is Claude unavailable as a fallback right now (disabled OR breaker
    OPEN)? Mirrors the _slice36 gate; used to ATTRIBUTE a force_batch decision: a
    force_batch while Claude is AVAILABLE can only be the Slice 170 reroute (the legacy
    batch path requires Claude unavailable). NEVER raises."""
    try:
        _disabled = os.environ.get(
            "JARVIS_PROVIDER_CLAUDE_DISABLED", "",
        ).strip().lower() in ("1", "true", "yes", "on")
        return _disabled or (_force_batch_on_breaker_enabled() and _claude_breaker_open())
    except Exception:  # noqa: BLE001
        return False


def _record_intra_failover_telemetry(context: Any, force_batch: bool) -> bool:
    """Slice 171 — record a Slice 170 intra-DW failover when one fired. A force_batch
    decision while Claude is AVAILABLE ⟹ the intra-DW failover fired (a DW rupture
    rerouted to batch), so we AVOIDED a Claude cascade = capital saved. Fire-and-forget:
    a single lock-guarded counter increment, no I/O, no GIL contention. NEVER raises.
    Returns True iff a failover was recorded (for telemetry/tests)."""
    try:
        if not force_batch:
            return False
        if _claude_unavailable_now():
            return False  # Claude dead → batch is the legacy path, not a saved-vs-Claude event
        from backend.core.ouroboros.governance.economic_telemetry import (
            economic_telemetry_enabled,
            get_economic_telemetry,
        )
        if not economic_telemetry_enabled():
            return False
        get_economic_telemetry().record_intra_failover()
        return True
    except Exception:  # noqa: BLE001 — telemetry must never break the adapter
        return False


def _claude_breaker_open(getter: Any = None) -> bool:
    """Slice 161 — True iff the Claude breaker is NOT CLOSED (OPEN or HALF_OPEN), i.e.
    Claude is unreliable as a fallback, so STANDARD/COMPLEX ops must force the DW batch
    transport.

    Reads the breaker STATE directly (read-only). It deliberately does NOT call
    ``should_allow_request()``, which (Slice 159 bug) has a SIDE EFFECT — it transitions
    OPEN->HALF_OPEN and consumes the single probe slot — AND returns True during the
    probe window, flickering force-batch OFF exactly when DW must carry the op (the
    complex-op live_transport rupture observed in the soak). HALF_OPEN still counts as
    unreliable: a probe is in flight, Claude is not yet proven healthy. ``getter``
    injectable. NEVER raises — fail-closed to False (legacy RT)."""
    try:
        if getter is None:
            from backend.core.ouroboros.governance.claude_circuit_breaker import (
                get_claude_circuit_breaker as getter,  # type: ignore[assignment]
            )
        from backend.core.ouroboros.governance.claude_circuit_breaker import CircuitState
        return getter().state in (CircuitState.OPEN, CircuitState.HALF_OPEN)
    except Exception:  # noqa: BLE001 — defensive; legacy RT on any failure
        return False


def _slice172_predictive_routing_enabled() -> bool:
    """Slice 172 — master for predictive preemptive routing (default FALSE, §33.1: acts
    on a forecast, not a confirmed failure). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.dw_failure_predictor import (
            predictive_routing_enabled,
        )
        return predictive_routing_enabled()
    except Exception:  # noqa: BLE001
        return False


def _dw_rupture_risk_high(model_id: str = "") -> bool:
    """Slice 172/174/175 — is the FORECAST rupture risk for THIS model at/above its (possibly
    self-calibrated) threshold? Delegates to the predictor's per-model risk_exceeds_threshold,
    which also drives that model's Slice 174 calibration loop when enabled. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.dw_failure_predictor import (
            get_dw_failure_predictor,
        )
        if get_dw_failure_predictor().risk_exceeds_threshold(model_id=model_id):
            return True
        # Slice 179 — warm-boot: a CONFIRMED-degraded stream (persisted ledger) arms the
        # forecast at T=0, before the per-model ring has warmed.
        return _dw_streaming_warm_degraded()
    except Exception:  # noqa: BLE001
        return False


def _dw_batch_lane_healthy() -> bool:
    """Slice 173 — True iff DW's BATCH_STORAGE surface is HEALTHY, reusing the EXISTING
    surface-health ledger check (``preflight_probe._batch_surface_healthy`` — no duplicate
    health logic). Fails CLOSED (False on any error / flag-off). The Slice 172 predictive
    detour MUST consult this: a forecast must never route an op INTO a degraded batch lane.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.preflight_probe import (
            _batch_surface_healthy,
        )
        return _batch_surface_healthy()
    except Exception:  # noqa: BLE001
        return False


_WARM_BOOT_ENV: str = "JARVIS_DW_WARM_BOOT_ENABLED"


def _slice179_warm_boot_enabled() -> bool:
    """Slice 179 — master for warm-boot armor. Default **TRUE** (failure-path-only: only
    fires when the PERSISTED ledger shows the stream already degraded, so it cannot affect a
    healthy-stream boot). NEVER raises."""
    return os.environ.get(_WARM_BOOT_ENV, "true").strip().lower() not in ("0", "false", "no", "off")


def _dw_streaming_warm_degraded() -> bool:
    """Slice 179 — does the PERSISTED surface-health ledger show DIRECT_STREAMING degraded
    RIGHT NOW (RAW verdict — NOT freshness-gated like the Slice-170 path)? On a container
    restart the ledger inherits the prior session's verdict, so this arms the cortex at T=0:
    the very first ops (incl. the sentinel model-selection probe) batch instead of exhausting
    on a rupturing RT stream. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.preflight_probe import (
            is_surface_health_enabled,
        )
        if not is_surface_health_enabled():
            return False
        from backend.core.ouroboros.governance.dw_surface_health import (
            SurfaceHealthLedger,
            SurfaceKind,
            SurfaceVerdict,
        )
        rec = SurfaceHealthLedger().verdict_for(SurfaceKind.DIRECT_STREAMING)
        return rec is not None and rec.verdict in (
            SurfaceVerdict.TRANSPORT_DEGRADED,
            SurfaceVerdict.UPSTREAM_DEGRADED,
        )
    except Exception:  # noqa: BLE001
        return False


_dw_sentinel_force_batch = __import__("contextvars").ContextVar(
    "_dw_sentinel_force_batch", default=False,
)


def set_sentinel_force_batch(value: bool):
    """Slice 182 — the sentinel COMMANDS DW-batch for the current async task's probes (async-
    safe ContextVar; survives the frozen-context contract). Returns a token to reset with."""
    return _dw_sentinel_force_batch.set(bool(value))


def reset_sentinel_force_batch(token) -> None:
    """Slice 182 — clear the sentinel force-batch command (call in a finally). NEVER raises."""
    try:
        _dw_sentinel_force_batch.reset(token)
    except Exception:  # noqa: BLE001
        pass


def _slice36_should_force_batch(context: Any, *, model_id: str = "") -> bool:
    """Slice 36 — adaptive transport selector decision.

    ``model_id`` (Slice 175): the resolved target DW model, so the predictive branch queries
    that model's OWN rupture forecast (a volatile model batches while a stable one streams).
    Empty → the shared "unknown" bucket (graceful for call sites that haven't resolved it).

    Returns True when the BATCH API path should be used in place of
    the RT streaming path for THIS specific op.

    Decision matrix:

      * ``JARVIS_PROVIDER_CLAUDE_DISABLED != true`` (Claude available
        as fallback) → False (RT path; Claude catches any RT failure)
      * Master flag explicitly off → False (operator opt-out)
      * Route NOT in {standard, complex} → False (preserve RT for
        IMMEDIATE / BG / SPECULATIVE where Venom adds value AND
        prompt sizes are small)
      * All conditions hold → True (force batch)

    NEVER raises — defensive on any attribute lookup or env parse
    failure (returns False = preserve legacy RT behavior).
    """
    try:
        # Slice 182 — SENTINEL force-batch override (HIGHEST precedence). The sentinel, having
        # natively queried the predictor/warm-boot, COMMANDS batch for this probe via an
        # async-safe ContextVar. Load-bearing: in the sentinel path the per-model frozen
        # context carries an EMPTY provider_route, so the route gate below cannot engage and
        # every probe ruptured on RT (the v181 soak bleed). This override bypasses the gate.
        if _dw_sentinel_force_batch.get():
            return True
        # Route gate — only STANDARD + COMPLEX get the batch path (IMMEDIATE / BG /
        # SPECULATIVE keep RT: small prompts + Venom value).
        _route = (
            getattr(context, "provider_route", "") or ""
        ).strip().lower()
        _route_ok = _route in ("standard", "complex")

        # Slice 170 — intra-DW transport failover PRECEDES the cross-provider cascade.
        # The economic leak: a DW streaming rupture (mislabeled live_transport) cascades
        # to the expensive Claude fallback whenever Claude has credit — so DW, the FUNDED
        # primary, only stays primary when Claude is broke. But a rupture is transport-
        # specific: DW's batch lane serves the identical request stream-free. When the
        # surface-health ledger shows the streaming wire degraded AND batch healthy (the
        # Slice 41 signal), force DW-batch REGARDLESS of Claude availability — the rupture
        # fails over WITHIN DW, not to Claude. Claude remains the fallback for genuine
        # DW-WIDE outages (both surfaces degraded), not transport blips. Failure-path-only
        # (fires solely on a degraded stream) → default-ON, §33.1.
        if _route_ok and _slice170_intra_dw_failover_enabled() and _slice41_ledger_force_batch():
            return True

        # Slice 172 — PREDICTIVE preemptive routing (the cortex). The forecast says a
        # rupture is likely within the horizon → route to batch BEFORE the stream breaks,
        # so it never throws, never panics, never wakes Claude. Distinct from Slice 170
        # (reactive, on a CONFIRMED degraded stream): this fires on a FORECAST while the
        # stream may still look healthy. Opt-in (§33.1 — acts on a prediction); default
        # FALSE. Route-gated (standard/complex) like every other batch path.
        #
        # Slice 173 — MULTI-SURFACE SAFETY GUARD (closes Blindspot A). The detour fires
        # ONLY if the batch lane is itself HEALTHY: a predictive cortex must never route an
        # op INTO a degraded batch lane. If the forecast is high BUT batch is also degraded,
        # abort the detour and stay on RT — a subsequent rupture then correctly cascades to
        # Claude (both DW surfaces are compromised). Reuses _dw_batch_lane_healthy (the
        # existing ledger check), not a duplicate.
        if (
            _route_ok
            and _slice172_predictive_routing_enabled()
            and _dw_rupture_risk_high(model_id)
            and _dw_batch_lane_healthy()
        ):
            logger.info(
                "[Cortex] forecast preempt: model=%s rupture-risk≥threshold → DW-batch "
                "(dodging the rupture before it throws)", model_id or "?",
            )
            return True

        # Slice 179 — WARM-BOOT armor (cold-start eradication). Independent of the predictor's
        # forecast warmth AND the predictive-routing flag: if the PERSISTED surface-health
        # ledger shows DIRECT_STREAMING degraded, DW's stream is confirmed broken from
        # millisecond zero (a restart inherits the prior verdict; the Slice-170 path can lapse
        # it as stale). Force batch immediately — so the very first ops, INCLUDING the sentinel
        # model-selection probe, never exhaust on a rupturing RT stream. Batch-health-gated
        # (never detour into a broken batch lane). Failure-path-only → default-ON.
        if (
            _route_ok
            and _slice179_warm_boot_enabled()
            and _dw_streaming_warm_degraded()
            and _dw_batch_lane_healthy()
        ):
            logger.info(
                "[Cortex] WARM-BOOT armor: persisted ledger shows DIRECT_STREAMING degraded "
                "→ DW-batch from T=0 (model=%s; cold-start exhaustion eradicated)",
                model_id or "?",
            )
            return True

        # Legacy pure-DW batch optimization: requires Claude unavailable. With Claude
        # available + a HEALTHY stream, RT failures cascade to Claude fallback and the
        # empirical cost-benefit shifts (the Slice 170 block above is the exception — a
        # DEGRADED stream fails over to DW-batch first).
        _claude_disabled = os.environ.get(
            "JARVIS_PROVIDER_CLAUDE_DISABLED", "",
        ).strip().lower() in ("1", "true", "yes", "on")
        # Slice 159 — Claude is ALSO unavailable-as-fallback when its circuit breaker
        # is OPEN (economic credit-death / transport). Then an RT failure cascades to a
        # blocked Claude → terminal_quota exhaustion, so DW must carry via batch. This
        # is what makes the organism sovereign on DW when Claude is credit-dead (not
        # merely when explicitly disabled). Gated (default ON, failure-path-only).
        _claude_unavailable = _claude_disabled or (
            _force_batch_on_breaker_enabled() and _claude_breaker_open()
        )
        if not _claude_unavailable:
            return False
        if not _route_ok:
            return False
        # Slice 36 static opt-out (default ON per operator authorization).
        _raw = os.environ.get(_SLICE36_FORCE_BATCH_ENV, "1").strip().lower()
        _static_on = _raw not in ("0", "false", "no", "off")
        # Slice 41 — ledger-driven failover. Even if the static opt-in is OFF,
        # force batch when the surface-health ledger shows the streaming wire
        # degraded but the batch lane healthy (don't fail closed / halt loops).
        return _static_on or _slice41_ledger_force_batch()
    except Exception:  # noqa: BLE001 — defensive
        return False


def _slice41_ledger_force_batch() -> bool:
    """Slice 41 — True iff the Slice 39/40 surface-health ledger shows the
    DIRECT_STREAMING surface degraded AND BATCH_STORAGE healthy (gated on the
    surface-health master flag). This is the autonomous, ledger-driven failover
    trigger: when the real-time wire is dropping content upstream but batch
    generation works, route standard/complex ops through the batch lane.
    NEVER raises — returns False (legacy RT) on any error / flag-off."""
    try:
        from backend.core.ouroboros.governance.preflight_probe import (
            is_surface_health_enabled,
        )
        if not is_surface_health_enabled():
            return False
        from backend.core.ouroboros.governance.dw_surface_health import (
            SurfaceHealthLedger,
            SurfaceKind,
            SurfaceVerdict,
        )
        led = SurfaceHealthLedger()
        stream = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
        batch = led.verdict_for(SurfaceKind.BATCH_STORAGE)
        streaming_degraded = stream is not None and stream.verdict in (
            SurfaceVerdict.TRANSPORT_DEGRADED,
            SurfaceVerdict.UPSTREAM_DEGRADED,
        )
        batch_healthy = batch is not None and batch.verdict == SurfaceVerdict.HEALTHY
        return streaming_degraded and batch_healthy
    except Exception:  # noqa: BLE001 — defensive, fail to legacy RT
        return False

# Pricing (March 2026)
_DW_INPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_INPUT_COST_PER_M", "0.10"))
_DW_OUTPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_OUTPUT_COST_PER_M", "0.40"))
_DW_MAX_COST_PER_OP = float(os.environ.get("DOUBLEWORD_MAX_COST_PER_OP", "0.10"))
_DW_DAILY_BUDGET = float(os.environ.get("DOUBLEWORD_DAILY_BUDGET", "5.00"))

# ---------------------------------------------------------------------------
# DW Heavy Non-Streaming Lane — "Functions, Not Agents" for codegen
# ---------------------------------------------------------------------------
# Composes the existing ``DoublewordProvider.complete_sync()`` primitive
# (the canonical Functions-not-Agents path, already used by
# ``CompactionCaller``, ``BlastRadius``, ``FailureClustering``,
# ``DreamSeed``) and adds a codegen-shaped wrapper that:
#   * keeps the ``prompt_override`` / S1-cache prompt seam intact,
#   * parses the response through the existing
#     ``_parse_generation_response`` (no parallel parser),
#   * uses ``_resolve_effective_model(context)`` (no hardcoded models),
#   * returns ``GenerationResult`` (not ``CompleteSyncResult``).
#
# Architectural correction (per operator design lock, 2026-05-21):
# DW heavy routing is constrained by **streaming transport reliability**,
# not by model reasoning quality. This lane unlocks DW reasoning via a
# stream-free path; ``enable_thinking=True`` is the default for heavy
# ops because the lane exists precisely to make DW reasoning reachable
# without the SSE stall surface.
#
# Master flag and all knobs default-FALSE / dormant on merge. Existing
# behavior unchanged until operator explicitly enables.

_DW_HEAVY_FN_LANE_ENABLED = (
    os.environ.get("JARVIS_DW_HEAVY_FN_LANE_ENABLED", "false").strip().lower()
    in ("1", "true", "yes", "on")
)
_DW_HEAVY_FN_LANE_ELIGIBLE_COMPLEXITIES: tuple = tuple(
    s.strip().lower()
    for s in os.environ.get(
        "JARVIS_DW_HEAVY_FN_LANE_ELIGIBLE_COMPLEXITIES",
        "complex,heavy_code",
    ).split(",")
    if s.strip()
)
_DW_HEAVY_FN_LANE_PREFER_OVER_SSE = (
    os.environ.get(
        "JARVIS_DW_HEAVY_FN_LANE_PREFER_OVER_SSE", "false",
    ).strip().lower()
    in ("1", "true", "yes", "on")
)
_DW_HEAVY_FN_LANE_PREFER_ON_SSE_STALL = (
    os.environ.get(
        "JARVIS_DW_HEAVY_FN_LANE_PREFER_ON_SSE_STALL", "true",
    ).strip().lower()
    in ("1", "true", "yes", "on")
)
_DW_HEAVY_FN_LANE_TIMEOUT_S = float(
    os.environ.get("JARVIS_DW_HEAVY_FN_LANE_TIMEOUT_S", "120.0")
)
_DW_HEAVY_FN_LANE_MAX_TOKENS = int(
    os.environ.get("JARVIS_DW_HEAVY_FN_LANE_MAX_TOKENS", "16384")
)
# Default TRUE: the lane exists to unlock DW reasoning over a
# stream-free transport. Operators can flip to FALSE per-ops if needed.
_DW_HEAVY_FN_LANE_ENABLE_THINKING = (
    os.environ.get(
        "JARVIS_DW_HEAVY_FN_LANE_ENABLE_THINKING", "true",
    ).strip().lower()
    in ("1", "true", "yes", "on")
)


def _dw_heavy_fn_lane_master_enabled() -> bool:
    """Re-read env per call so a flip hot-reverts. NEVER raises."""
    try:
        return os.environ.get(
            "JARVIS_DW_HEAVY_FN_LANE_ENABLED", "false",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 — defensive
        return False


def _dw_heavy_fn_lane_eligible_complexities() -> tuple:
    """Re-read env per call. Default complex,heavy_code."""
    try:
        return tuple(
            s.strip().lower()
            for s in os.environ.get(
                "JARVIS_DW_HEAVY_FN_LANE_ELIGIBLE_COMPLEXITIES",
                "complex,heavy_code",
            ).split(",")
            if s.strip()
        )
    except Exception:  # noqa: BLE001
        return ("complex", "heavy_code")


def _dw_heavy_fn_lane_prefer_over_sse() -> bool:
    try:
        return os.environ.get(
            "JARVIS_DW_HEAVY_FN_LANE_PREFER_OVER_SSE", "false",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def _dw_heavy_fn_lane_prefer_on_sse_stall() -> bool:
    try:
        return os.environ.get(
            "JARVIS_DW_HEAVY_FN_LANE_PREFER_ON_SSE_STALL", "true",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return True


def _dw_heavy_fn_lane_timeout_s() -> float:
    try:
        v = float(os.environ.get(
            "JARVIS_DW_HEAVY_FN_LANE_TIMEOUT_S", "120.0",
        ))
        return max(5.0, min(600.0, v))
    except Exception:  # noqa: BLE001
        return 120.0


def _dw_heavy_fn_lane_max_tokens() -> int:
    try:
        v = int(os.environ.get(
            "JARVIS_DW_HEAVY_FN_LANE_MAX_TOKENS", "16384",
        ))
        return max(256, min(32768, v))
    except Exception:  # noqa: BLE001
        return 16384


def _dw_heavy_fn_lane_enable_thinking() -> bool:
    """Default TRUE — the lane exists to unlock DW reasoning over a
    stream-free transport. NEVER raises."""
    try:
        return os.environ.get(
            "JARVIS_DW_HEAVY_FN_LANE_ENABLE_THINKING", "true",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return True


class DoublewordInfraError(Exception):
    """Infrastructure failure from DoublewordProvider.

    Propagated to the CandidateGenerator's FailbackStateMachine so it can
    classify the failure mode (rate limit vs timeout vs connection error)
    and predict recovery timing.  The ``status_code`` field carries the
    HTTP status (429, 500, etc.) or 0 for non-HTTP failures.

    Phase 12 Slice F — Substrate Error Unmasking (operator-mandated
    2026-04-27): added ``response_body`` and ``model_id`` so the
    sentinel + classifier can distinguish 4xx modality errors (NON_CHAT
    models silently slotted into generative routes) from 5xx transport
    errors (genuine endpoint instability) without regex-matching on the
    string repr.

    Failure-class taxonomy carried structurally:
      * ``status_code in (400, 404, 422)`` AND modality body markers
        → terminal/modality error, model permanently excluded by
        Slice H breaker until next catalog refresh
      * ``status_code in (429, 503)`` → rate limit / overload, retry
      * ``status_code in (500, 502, 504)`` → transient transport
      * ``status_code == 401`` / ``403`` → auth failure, terminal
      * ``status_code == 0`` → non-HTTP (DNS/TLS/timeout)
    """

    def __init__(
        self,
        reason: str,
        status_code: int = 0,
        *,
        response_body: str = "",
        model_id: str = "",
    ) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.response_body = (response_body or "")[:1024]  # bounded
        self.model_id = (model_id or "")[:128]

    def is_modality_error(self) -> bool:
        """True iff the response indicates the model can't accept
        ``/chat/completions`` payloads. Used by Slice H breaker to
        decide TERMINAL_OPEN vs transient.

        Heuristic on KNOWN-AT-RUNTIME signals (NOT regex on model id):
          * status_code in {400, 404, 422} — bad request / not found /
            unprocessable entity (classic OpenAI-compat modality 4xx)
          * AND response_body contains a modality marker the DW server
            actually emits
        Both required: a 400 about a bad max_tokens is NOT modality.
        """
        if self.status_code not in (400, 404, 422):
            return False
        body_lower = (self.response_body or "").lower()
        # These markers are observed in DW + OpenAI-compat error
        # responses for modality-mismatched calls. Matched on the
        # SERVER's response body (which is ground truth from DW),
        # NOT on our local model_id string. If DW returns a body
        # without these markers, we conservatively treat it as
        # transient — we don't infer modality from absence.
        markers = (
            "does not support chat",
            "not a chat model",
            "endpoint not supported",
            "embedding only",
            "model_not_chat",
            "task mismatch",
            "wrong endpoint",
            "unsupported endpoint",
            "model is not available for chat",
        )
        return any(m in body_lower for m in markers)

    def is_terminal_auth_error(self) -> bool:
        """401/403 → permanent auth failure for this model_id."""
        return self.status_code in (401, 403)

    def is_transient(self) -> bool:
        """5xx + 429 → transient; should retry per backoff schedule."""
        if self.status_code in (429, 503, 500, 502, 504):
            return True
        # Non-HTTP failures (status_code == 0) — DNS/TLS/timeout —
        # treated as transient unless the reason text indicates
        # something terminal. Conservative: assume transient.
        return self.status_code == 0


@dataclass
class DoublewordStats:
    """Cumulative stats for observability (Pillar 7)."""
    total_batches: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_s: float = 0.0
    failed_batches: int = 0
    empty_content_retries: int = 0


@dataclass
class PendingBatch:
    """Tracks an in-flight Doubleword batch for async retrieval."""
    op_id: str
    batch_id: str
    file_id: str
    prompt: str
    submitted_at: float  # time.monotonic()
    wall_submitted_at: float = field(default_factory=time.time)


@dataclass
class CompletedBatch:
    """Stores a completed Doubleword batch result for deferred application."""
    op_id: str
    batch_id: str
    result: "GenerationResult"
    completed_at: float  # time.monotonic()
    wall_completed_at: float = field(default_factory=time.time)


@dataclass
class CompleteSyncResult:
    """Result of a non-streaming complete_sync() call.

    Functions-not-Agents path: structured return for short, bounded,
    schema-validated function callers (CompactionCaller, BlastRadius,
    FailureClustering, DreamSeed). Never used by the agent cascade —
    agent-shaped workloads go through generate()/Venom/SSE.
    """
    content: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    model: str


class DoublewordProvider:
    """Tier 0 CandidateProvider using Doubleword batch API with 397B MoE model.

    Follows the same protocol as PrimeProvider and ClaudeProvider:
      - generate(ctx) → GenerationResult
      - health_probe() → bool

    The batch API is 4-stage async:
      1. Upload JSONL file
      2. Create batch job
      3. Poll until completion
      4. Retrieve and parse results
    """

    def __init__(
        self,
        api_key: str = _DW_API_KEY,
        base_url: Optional[str] = None,
        model: str = _DW_MODEL,
        max_tokens: int = _DW_MAX_TOKENS,
        repo_root: Optional[Path] = None,
        repo_roots: Optional[Dict[str, Path]] = None,
        rate_limiter: Optional[Any] = None,
        max_cost_per_op: float = _DW_MAX_COST_PER_OP,
        daily_budget: float = _DW_DAILY_BUDGET,
        tool_loop: Optional[Any] = None,
        realtime_enabled: bool = True,
        batch_registry: Optional[Any] = None,
    ) -> None:
        self._api_key = api_key
        # Slice 2B-ii — Aegis transport swap. Default ``base_url=None``
        # triggers instance-time resolution via the Aegis bridge: when
        # JARVIS_AEGIS_ENABLED is true we get ``{JARVIS_AEGIS_URL}/v1``
        # (so f"{self._base_url}/chat/completions" composes the
        # Aegis-allowlisted path); when disabled we get the legacy
        # ``DOUBLEWORD_BASE_URL`` env or ``api.doubleword.ai/v1``.
        # Resolution at __init__ time (not module-import) ensures the
        # Aegis preflight has already populated env when we read it.
        if base_url is None:
            base_url = _aegis_dw_base_url()
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._repo_root = repo_root or Path(".")
        self._repo_roots = repo_roots or {}
        # Phase 1 Step 3B — state hoist. aiohttp session, cumulative
        # stats, spend tracking, last-error-status, and stream activity
        # timestamps all live on a ``DoubleWordProviderState`` routed
        # through the process-lifetime singleton under
        # ``JARVIS_UNQUARANTINE_PROVIDERS=true``. The legacy path mints
        # a fresh state per instance — behavior is bit-for-bit
        # identical to the pre-hoist version.
        from ._governance_state import (
            DoubleWordProviderState,
            get_doubleword_provider_state,
            unquarantine_providers_enabled,
        )
        if unquarantine_providers_enabled():
            self._state = get_doubleword_provider_state()
        else:
            self._state = DoubleWordProviderState.fresh()
        # ``_stats`` is mutated in place (``self._stats.total_batches += 1``)
        # and never rebound, so an alias onto the state dataclass is
        # alias-safe — no property indirection needed.
        self._stats = self._state.stats
        self._rate_limiter = rate_limiter
        self._tool_loop = tool_loop
        self._batch_registry = batch_registry
        # Real-time mode uses /v1/chat/completions with SSE streaming —
        # zero polling, token-by-token output, Venom tool loop support.
        # Battle testing shows batch (16-22s) and real-time (20-40s) have
        # comparable latency, but real-time enables streaming + eliminates
        # the polling loop (Manifesto §3: Zero polling. Pure reflex.).
        # Default: ON. Opt out via DOUBLEWORD_REALTIME_ENABLED=false.
        self._realtime_enabled = (
            realtime_enabled
            and os.environ.get("DOUBLEWORD_REALTIME_ENABLED", "true").lower() != "false"
        )
        # Cost gating (matches ClaudeProvider pattern)
        self._max_cost_per_op = max_cost_per_op
        self._daily_budget = daily_budget
        self._mcp_client: Optional[Any] = None  # Injected by GLS for MCP tool forwarding (Gap #7)

    def _resolve_effective_model(self, ctx: Any) -> str:
        """Resolve the DW model for this call.

        Resolution order (first match wins):

          1. ``topology_sentinel.DW_MODEL_OVERRIDE_VAR`` — per-attempt
             override set by the AsyncTopologySentinel-driven dispatch
             in ``candidate_generator`` (Phase 10 P10.3+P10.3.6).
             When the sentinel is walking a route's ranked
             ``dw_models`` list, each attempt sets this ContextVar
             via ``set_dw_model_override(model_id)``; this method
             reads it via ``get_dw_model_override()``. ContextVar is
             async-safe per asyncio task, so concurrent ops can each
             have their own value without leaking. Replaces the
             Slice 3 ``setattr(ctx, "_dw_model_override", ...)``
             pattern, which raised ``FrozenInstanceError`` on the
             frozen ``OperationContext`` dataclass and silently
             defeated the dispatcher.
          2. ``topology.model_for_route(route)`` — v1 single-model
             per-route mapping. Honored when no per-attempt override
             is set (legacy path; default behavior when sentinel is
             disabled).
          3. ``self._model`` — instance default (env-configured).

        Falls back to ``self._model`` when the topology is disabled,
        the route is unmapped, or the ctx lacks a ``provider_route``
        attribute — identical to the pre-topology behavior.

        NEVER raises — every layer is defensive.
        """
        # (1) Per-attempt override from sentinel-driven dispatch via
        # ContextVar (async-safe; survives the frozen-ctx contract).
        try:
            from backend.core.ouroboros.governance.topology_sentinel import (
                get_dw_model_override,
            )
            attempt_override = get_dw_model_override()
            if isinstance(attempt_override, str) and attempt_override:
                return attempt_override
        except Exception:  # noqa: BLE001 — defensive
            # Sentinel module not importable (test environment, branch
            # without Slice 1) → silently fall through to legacy.
            pass
        # (2) v1 route → model mapping.
        route = getattr(ctx, "provider_route", "") or ""
        if not route:
            return self._model
        try:
            from backend.core.ouroboros.governance.provider_topology import (
                get_topology,
            )
        except Exception:
            return self._model
        # Phase 10 Slice 5a — unified deletion-side helper. Branches
        # on JARVIS_TOPOLOGY_SENTINEL_ENABLED internally; v2 path
        # returns first element of dw_models_for_route (catalog-first
        # → yaml fallback); v1 path is byte-identical to model_for_route.
        override = get_topology().model_for_route_unified(route)
        return override or self._model

    # ------------------------------------------------------------------
    # Hoisted state accessors (Phase 1 Step 3B)
    # ------------------------------------------------------------------
    # Every rebound field on ``DoubleWordProviderState`` gets paired
    # getter/setter descriptors so assignments like
    # ``self._session = aiohttp.ClientSession(...)`` on reload-surviving
    # instances can't plant a real instance attribute and drift from
    # ``self._state.session``.

    @property
    def _session(self) -> Any:
        return self._state.session

    @_session.setter
    def _session(self, value: Any) -> None:
        self._state.session = value

    @property
    def _daily_spend(self) -> float:
        return self._state.counters.daily_spend

    @_daily_spend.setter
    def _daily_spend(self, value: float) -> None:
        self._state.counters.daily_spend = value

    @property
    def _budget_reset_date(self) -> str:
        return self._state.counters.budget_reset_date

    @_budget_reset_date.setter
    def _budget_reset_date(self, value: str) -> None:
        self._state.counters.budget_reset_date = value

    @property
    def _last_error_status(self) -> int:
        return self._state.counters.last_error_status

    @_last_error_status.setter
    def _last_error_status(self, value: int) -> None:
        self._state.counters.last_error_status = value

    @property
    def _last_chunk_at(self) -> float:
        return self._state.counters.last_chunk_at

    @_last_chunk_at.setter
    def _last_chunk_at(self, value: float) -> None:
        self._state.counters.last_chunk_at = value

    def _record_ttft_safely(
        self,
        *,
        model_id: str,
        ttft_ms: int,
        op_id: str = "",
    ) -> None:
        """Phase 12.2 Slice C — feed first-chunk latency into:

          1. ``TtftObserver`` (rolling stats for promotion + cold-
             storage gates) — only when tracking_enabled() is true.
          2. ``PromotionLedger`` ``record_success`` (legacy count gate
             keep-alive so master-flag-off path stays bit-for-bit
             unchanged from Phase 12 Slice B).

        NEVER raises. All faults swallowed at this seam — a broken
        observer or ledger must NEVER take down the SSE stream.
        Singleton lookup is lazy (deferred until first call) so
        master-flag-off + tracking-off → zero observer instantiated."""
        if not model_id or ttft_ms < 0:
            return
        # Observer feed (TTFT mode)
        try:
            from backend.core.ouroboros.governance.dw_discovery_runner import (
                get_ttft_observer,
            )
            obs = get_ttft_observer()
            if obs is not None:
                obs.record_ttft(model_id, ttft_ms, op_id=op_id)
        except Exception:  # noqa: BLE001 — defensive
            pass
        # Ledger feed (legacy count gate keep-alive). The ledger's
        # auto-register-on-first-success path means we don't need to
        # know whether the model was previously quarantined — the
        # ledger handles it.
        try:
            from backend.core.ouroboros.governance.dw_discovery_runner import (
                _get_or_create_ledger,
            )
            led = _get_or_create_ledger()
            led.record_success(model_id, ttft_ms)
        except Exception:  # noqa: BLE001 — defensive
            pass

    @property
    def provider_name(self) -> str:
        """Human-readable name for CandidateProvider protocol."""
        return "doubleword-397b"

    @property
    def is_available(self) -> bool:
        """Check if Doubleword is configured.

        Slice 2B-ii.1 — Aegis-aware availability gate. Composes the
        canonical ``aegis.client.is_enabled()`` predicate as an
        OR-fallback when the local API key is absent. Under the
        Aegis Zero-Trust posture, the real credential lives in the
        out-of-process daemon and our local env is intentionally
        scrubbed at preflight (Slice 1) — without this fallback,
        the provider self-disables despite having a fully-functional
        upstream route (the Catch-22 surfaced by Aegis Detonation
        soak bt-2026-05-24-222008).

        Predicate is read at call-time (not cached at __init__) so
        the gate stays accurate if Aegis is enabled mid-session.
        """
        from backend.core.ouroboros.aegis.client import is_enabled as _aegis_is_enabled
        return _aegis_is_enabled() or bool(self._api_key)

    async def _get_session(self) -> Any:
        """Lazy-init persistent aiohttp session.

        NOTE: Content-Type is NOT set at session level. The session default
        ``application/json`` was overriding the multipart boundary generated
        by aiohttp.FormData during file uploads, causing Doubleword to reject
        the request with "Invalid boundary for multipart/form-data".
        Each request sets its own Content-Type as needed.
        """
        _needs_new = (
            self._session is None
            or self._session.closed
            # aiohttp connector can be poisoned by CancelledError during
            # connection attempts.  session.closed doesn't always reflect
            # this, so check the connector directly.
            or getattr(self._session.connector, "_closed", False)
        )
        if _needs_new:
            import aiohttp
            # Close the old session cleanly if it exists
            if self._session is not None and not self._session.closed:
                try:
                    await self._session.close()
                except Exception:
                    pass
            # CRITICAL: aiohttp 3.9+ requires ClientSession to be created
            # inside a running event loop task. The default timeout parameter
            # triggers "Timeout context manager should be used inside a task".
            # Solution: create with connector only, no timeout object at all.
            # Per-request timeouts are applied via _request_timeout() instead.
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            # Slice 2B-ii — Aegis transport. When Aegis is enabled,
            # ``_aegis_dw_auth_header()`` returns an empty dict so the
            # session holds NO real Bearer token (Aegis injects the
            # confiscated DOUBLEWORD_API_KEY upstream). When disabled,
            # it returns ``{"Authorization": "Bearer {DOUBLEWORD_API_KEY}"}``
            # for byte-identical legacy behavior. Per-call X-JARVIS-Lease
            # is layered on at each session.post/get site (operator
            # correction #4 — never client-wide).
            #
            # Slice 31 — the Aegis SESSION BEARER (Authorization: Bearer
            # <session_token>) is injected per-call via
            # ``_aegis_dw_session_auth_header()`` at every outbound site,
            # NOT at session creation. Rationale: session-bearer tokens
            # have TTL + may rotate; per-call fetch reads cached state
            # (no daemon round-trip in steady state) and avoids a stale
            # baked-in token surviving across reconnects. Per-call
            # headers override session headers in aiohttp.
            _session_headers = dict(_aegis_dw_auth_header())
            self._session = aiohttp.ClientSession(
                headers=_session_headers,
                connector=connector,
                trust_env=True,  # honour HTTP_PROXY / HTTPS_PROXY env vars
            )
        return self._session

    async def force_session_reset(self) -> None:
        """Slice 39 — hard-flush the aiohttp transport pool.

        Closes the current ClientSession (and its TCPConnector socket
        cache) and nulls it so the NEXT ``_get_session()`` rebuilds a
        fresh connector. Composes the existing rebuild path in
        ``_get_session`` — no duplicate connector logic. Used by the
        transport disambiguator ONLY for the transport-failure class
        (never for upstream ``done_before_content``). NEVER raises.
        """
        sess = self._session
        self._session = None
        if sess is not None and not getattr(sess, "closed", True):
            try:
                await sess.close()
            except Exception:  # noqa: BLE001 — flush must not raise
                pass

    @staticmethod
    def _request_timeout() -> "aiohttp.ClientTimeout":
        """Per-request timeout safe to use inside aiohttp 3.9+ tasks."""
        import aiohttp
        return aiohttp.ClientTimeout(
            total=_DW_REQUEST_TIMEOUT_S,
            connect=_DW_CONNECT_TIMEOUT_S,
        )

    # ------------------------------------------------------------------
    # Cost gating (matches ClaudeProvider pattern)
    # ------------------------------------------------------------------

    def _maybe_reset_daily_budget(self) -> None:
        """Reset daily spend if the UTC day has changed."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today > self._budget_reset_date:
            self._daily_spend = 0.0
            self._budget_reset_date = today

    def _check_budget(self) -> None:
        """Raise if daily budget is exhausted OR session-budget preflight
        refuses. Called before each generation (including from
        ``complete_sync``, which means the DW heavy non-streaming lane
        inherits the gate transparently)."""
        self._maybe_reset_daily_budget()
        if self._daily_spend >= self._daily_budget:
            raise DoublewordInfraError(
                f"doubleword_budget_exhausted: daily spend ${self._daily_spend:.4f} "
                f">= budget ${self._daily_budget:.2f}",
                status_code=0,
            )
        # PRD §session-budget-preflight: hard wallet gate. Refuses BEFORE
        # any network dispatch when the estimated cost (per-op cap as
        # conservative upper bound) exceeds remaining session budget.
        # Composes the duck-typed session_budget_authority — no parallel
        # ledger. No-op when no session authority is active (fail-OPEN).
        try:
            from backend.core.ouroboros.governance.session_budget_authority import (  # noqa: E501
                check_preflight as _sba_check_preflight,
            )
            # Slice 12Y Part 1 — Slice 12Z bug fix:
            # _check_budget(self) has no `context` parameter (DW
            # provider's structure differs from Claude's
            # generate(self, context)). Passing signal_source=None
            # preserves the pre-Slice-12Y behavior for DW —
            # background-spend ceiling does NOT apply at the DW
            # call site. This is intentional: DW per-call cost is
            # ~$0.002 (3 orders of magnitude smaller than Claude's
            # $0.50 estimate), so the background-ceiling primary
            # use case (foreground fixture starvation prevention)
            # is fully covered by the Claude provider path which
            # DOES carry context.
            _sba_check_preflight(
                provider_name="doubleword",
                estimated_cost_usd=float(self._max_cost_per_op or 0.0),
                signal_source=None,
            )
        except ImportError:
            # Module absent on this build — graceful fall-through to
            # legacy daily-only gate.
            pass
        # SessionBudgetPreflightRefused: NOT caught — propagates to
        # the orchestrator's cascade machinery which routes on the
        # structured `reason` field.

    def _record_cost(self, cost: float) -> None:
        """Record cost from a completed batch and check per-op limit."""
        self._daily_spend += cost
        if cost > self._max_cost_per_op:
            logger.warning(
                "[DoublewordProvider] Op cost $%.4f exceeds max_cost_per_op $%.2f",
                cost, self._max_cost_per_op,
            )

    # ------------------------------------------------------------------
    # Async decoupled API: submit_batch() + poll_and_retrieve()
    # ------------------------------------------------------------------

    async def submit_batch(
        self,
        ctx: OperationContext,
        *,
        prompt_override: Optional[str] = None,
    ) -> Optional[PendingBatch]:
        """Stage 1+2: Upload JSONL and create batch. Returns immediately.

        This is the fast path — typically completes in <2s. The caller
        should fire a background task to poll_and_retrieve() later.
        Returns None on failure (caller falls through to Tier 1).
        """
        if not self.is_available:
            return None
        self._check_budget()

        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        # Always use full_content schema (2b.1) — the 397B can't reliably
        # produce verbatim context lines for unified diffs (2b.1-diff).
        prompt = prompt_override or _build_codegen_prompt(
            ctx,
            repo_root=self._repo_root,
            repo_roots=self._repo_roots or None,
            force_full_content=True,
            provider_route=getattr(ctx, "provider_route", "") or "",
        )
        operation_id = getattr(ctx, "operation_id", f"dw-{int(time.time())}")
        _effective_model = self._resolve_effective_model(ctx)

        # Slice 38 — canonical JSONL composition via single helper.
        # Replaces raw ``json.dumps(...)`` which omitted the trailing
        # ``\n`` and made DW reject the upload with HTTP 500.
        jsonl_line = self._compose_jsonl_batch_entry({
            "custom_id": operation_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": _effective_model,
                "messages": [
                    {"role": "system", "content": (
                        "You are a code generation assistant. RESPOND WITH ONLY A SINGLE VALID JSON OBJECT. "
                        "RULES: "
                        "1. Start your response with { and end with }. "
                        "2. No text before or after the JSON. No markdown fences. No explanations. "
                        "3. All string values must use double quotes. Escape special characters: use \\n for newlines, \\t for tabs, \\\\ for backslashes. "
                        "4. No trailing commas before } or ]. "
                        "5. Use schema_version '2b.1' with full_content containing the COMPLETE file. "
                        "6. NEVER return unified diffs, patches, or partial file content. "
                        "7. CRITICAL: Every candidate MUST include a non-empty 'rationale' field "
                        "(1 sentence, max 200 chars). Missing rationale will be rejected."
                    )},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": self._max_tokens,
                "temperature": _DW_TEMPERATURE,
                # Slice 54/55 — Qwen3.5 reasoning control. reasoning_effort
                # (DW-honored; the old enable_thinking flag was ignored) is
                # derived from task_complexity (Slice 55) — leaf work stays
                # "none" (fast/cheap), high-impact core gets a CoT buffer.
                **_reasoning_request_params(
                    complexity=getattr(ctx, "task_complexity", "") or "",
                    model=_effective_model,
                ),
            },
        })

        # Slice 35 Phase 2 — batch path stage profiler (composes
        # Slice 34 dispatch_profiler). Default OFF; records stages
        # only when JARVIS_DISPATCH_PROFILER_ENABLED=1.
        from backend.core.ouroboros.telemetry import (
            dispatch_profiler as _s35b_dp,
        )
        _s35b_model = self._model or "(unspecified)"
        try:
            # Slice 2B-ii — thread operation_id to per-call Aegis lease.
            _s35b_t_upload = time.monotonic()
            file_id = await self._upload_file(
                jsonl_line, op_id=operation_id,
            )
            _s35b_dp.record_stage(
                "STAGE_BATCH_UPLOAD",
                op_id=operation_id, model_id=_s35b_model,
                duration_ms=(time.monotonic() - _s35b_t_upload) * 1000.0,
                outcome="ok" if file_id else "error",
            )
            if not file_id:
                logger.warning("[DoublewordProvider] submit_batch: file upload failed")
                return None

            _s35b_t_create = time.monotonic()
            batch_id = await self._create_batch(
                file_id, op_id=operation_id,
            )
            _s35b_dp.record_stage(
                "STAGE_BATCH_CREATE",
                op_id=operation_id, model_id=_s35b_model,
                duration_ms=(time.monotonic() - _s35b_t_create) * 1000.0,
                outcome="ok" if batch_id else "error",
            )
            if not batch_id:
                logger.warning("[DoublewordProvider] submit_batch: batch creation failed")
                return None

            logger.info(
                "[DoublewordProvider] Batch %s submitted async (model=%s, op=%s)",
                batch_id, _effective_model, operation_id,
            )
            return PendingBatch(
                op_id=operation_id,
                batch_id=batch_id,
                file_id=file_id,
                prompt=prompt,
                submitted_at=time.monotonic(),
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[DoublewordProvider] submit_batch failed")
            return None

    async def poll_and_retrieve(
        self,
        pending: PendingBatch,
        ctx: OperationContext,
    ) -> Optional[GenerationResult]:
        """Stage 3+4: Poll batch to completion and parse results.

        This is the slow path — may take minutes. Designed to run as a
        background task via asyncio.create_task(). Returns None on failure.
        """
        t0 = pending.submitted_at

        # Slice 35 Phase 2 — batch path stage profiler.
        from backend.core.ouroboros.telemetry import (
            dispatch_profiler as _s35p_dp,
        )
        _s35p_model = self._model or "(unspecified)"
        try:
            # Slice 2B-ii — forward pending.op_id for per-call Aegis lease.
            _s35p_t_await = time.monotonic()
            output_file_id = await self._await_batch_result(
                pending.batch_id, op_id=pending.op_id,
            )
            _s35p_dp.record_stage(
                "STAGE_BATCH_AWAIT",
                op_id=pending.op_id, model_id=_s35p_model,
                duration_ms=(time.monotonic() - _s35p_t_await) * 1000.0,
                outcome="ok" if output_file_id else "error",
            )
            if not output_file_id:
                self._stats.failed_batches += 1
                logger.warning(
                    "[DoublewordProvider] Batch %s failed or timed out",
                    pending.batch_id,
                )
                return None

            _s35p_t_retrieve = time.monotonic()
            content, usage = await self._retrieve_result(
                output_file_id, pending.op_id,
            )
            _s35p_dp.record_stage(
                "STAGE_BATCH_RETRIEVE",
                op_id=pending.op_id, model_id=_s35p_model,
                duration_ms=(time.monotonic() - _s35p_t_retrieve) * 1000.0,
                outcome="ok" if content else "error",
            )

            elapsed = time.monotonic() - t0
            self._stats.total_batches += 1
            self._stats.total_latency_s += elapsed

            _batch_cost = 0.0
            if usage:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                self._stats.total_input_tokens += input_tokens
                self._stats.total_output_tokens += output_tokens
                _batch_cost = (
                    input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
                    + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
                )
                self._stats.total_cost_usd += _batch_cost
                self._record_cost(_batch_cost)

            if not content:
                self._stats.empty_content_retries += 1
                logger.warning(
                    "[DoublewordProvider] Batch %s returned empty content "
                    "(reasoning model exhausted token budget).",
                    pending.batch_id,
                )
                return None

            from backend.core.ouroboros.governance.providers import (
                _parse_generation_response,
                _file_source_hash,
            )
            # Source hash: full SHA-256 of target file content (matches
            # _check_source_drift at GATE).  Old code hashed prompt[:500]
            # which guaranteed false-positive drift for every DW candidate.
            _src_hash = ""
            _src_path_str = ""
            if ctx.target_files:
                _src_path_str = ctx.target_files[0]
                _abs = (self._repo_root / _src_path_str).resolve()
                try:
                    if _abs.is_file():
                        _src_hash = _file_source_hash(
                            _abs.read_text(encoding="utf-8", errors="replace")
                        )
                except OSError:
                    pass

            # Log raw response preview for debugging parse failures
            _preview = content[:200].replace("\n", "\\n") if content else "(empty)"
            logger.info(
                "[DoublewordProvider] Batch %s response preview (%d chars): %s",
                pending.batch_id, len(content), _preview,
            )

            # Auto-fix: if the 397B returned natural language instead of JSON,
            # try to extract any JSON block that might be embedded deeper in the
            # response. If truly no JSON exists, _parse_generation_response will
            # raise and the caller handles the failure.
            from backend.core.ouroboros.governance.providers import _extract_json_block
            _extracted = _extract_json_block(content)
            if _extracted and not _extracted.lstrip().startswith("{"):
                logger.warning(
                    "[DoublewordProvider] 397B returned natural language instead of JSON "
                    "(batch %s). Response starts with: %s",
                    pending.batch_id, _extracted[:100].replace("\n", " "),
                )
                # Return None — caller treats as "no candidates" and retries
                return None

            # Slice 20B — _parse_with_heal wraps _parse_generation_response
            # with an LLM-heal retry on json_parse_error (master-flag gated;
            # zero-cost no-op when off → byte-identical to direct parser call).
            result = await self._parse_with_heal(
                raw=content,
                provider_name="doubleword",
                duration_s=elapsed,
                ctx=ctx,
                source_hash=_src_hash,
                source_path=_src_path_str,
                repo_roots=self._repo_roots or None,
                repo_root=self._repo_root,
            )
            # Attach token usage and cost from batch
            if usage or _batch_cost > 0:
                result = dataclasses.replace(
                    result,
                    total_input_tokens=input_tokens,
                    total_output_tokens=output_tokens,
                    cost_usd=_batch_cost,
                )

            # Slice 0 — provider-latency observability (pure side
            # channel; master-flag-gated; NEVER raises). Emitted ONLY
            # when ``usage`` carried a real DW server-side token count
            # — a fabricated token=0 sample would poison the Slice-1
            # regression, so we skip rather than guess. Batch API has
            # no streaming first-token, so ttft_ms=-1 (not measured);
            # total_ms is the authoritative latency dimension.
            if usage:
                try:
                    from backend.core.ouroboros.governance.providers import (
                        _emit_provider_latency,
                    )

                    _emit_provider_latency(
                        provider="doubleword-397b",
                        route=getattr(ctx, "provider_route", "") or "",
                        op_id=str(getattr(pending, "op_id", "") or ""),
                        input_tokens=int(input_tokens),
                        ttft_ms=-1,  # batch API: no first-token stream
                        total_ms=int(elapsed * 1000.0),
                        outcome="success",
                    )
                except Exception:  # noqa: BLE001 — never perturbs
                    pass
            return result

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stats.failed_batches += 1
            # Log the raw response for debugging parse failures
            logger.warning(
                "[DoublewordProvider] poll_and_retrieve failed for batch %s: %s. "
                "Raw response first 300 chars: %s",
                pending.batch_id,
                exc,
                content[:300].replace("\n", "\\n") if content else "(no content)",
            )
            return None

    # ------------------------------------------------------------------
    # Dynamic output-token budget (parallel to ClaudeProvider's
    # _compute_output_budget). Used by both the batch path and the
    # real-time SSE path so a "trivial" complexity task that happens
    # to target a large file still gets enough tokens to fit the full
    # rewrite in one response. Falls back to the complexity ceiling
    # when target files can't be resolved (new file / bad path).
    # ------------------------------------------------------------------

    def _compute_dynamic_max_tokens(
        self,
        context: Any,
        *,
        is_tool_round: bool = False,
    ) -> int:
        """Compute max_tokens for a DW generation call.

        Always starts from the complexity-derived ceiling, then scales *up*
        by the actual target file size so full-file rewrites don't truncate
        mid-string.

        Formula:
            raw = total_bytes / _DW_CHARS_PER_TOKEN
            needed = int(raw * _DW_OUTPUT_SAFETY) + _DW_OUTPUT_OVERHEAD_TOKENS
            result = max(needed, complexity_ceiling)
            result = min(result, _DW_MAX_TOKENS)

        Parameters
        ----------
        context:
            The current ``OperationContext``. Expected attributes:
            ``task_complexity`` (str) and ``target_files`` (seq of rel paths).
        is_tool_round:
            Advisory only — no longer affects the budget. The flag is set
            *before* the call based on ``round_index > 0``, but the model
            decides per-response whether to emit a short tool-call JSON or
            the final ``full_content`` candidate. Capping at 1024 on
            ``round > 0`` truncated the terminal round's patch mid-string
            (battle test bt-2026-04-11-065233). DW bills on actual output
            tokens, so a generous cap on every round costs nothing when the
            model naturally stops short on an intermediate tool-call round.
        """
        del is_tool_round  # advisory only — see docstring
        complexity = getattr(context, "task_complexity", "") or ""
        complexity_ceiling = _DW_COMPLEXITY_MAX_TOKENS.get(
            complexity, self._max_tokens,
        )

        # Resolve target files → total bytes. New files (non-existent
        # paths) contribute 0. Multi-file rewrites sum all bytes so the
        # budget covers the whole candidate array.
        target_files = getattr(context, "target_files", ()) or ()
        total_bytes = 0
        resolved = 0
        for rel in target_files:
            if not rel:
                continue
            try:
                abs_path = (self._repo_root / str(rel)).resolve()
                if abs_path.exists() and abs_path.is_file():
                    total_bytes += abs_path.stat().st_size
                    resolved += 1
            except (OSError, ValueError):
                continue

        if resolved == 0 or total_bytes == 0:
            # No file-size data — keep the complexity ceiling as-is.
            return min(int(complexity_ceiling), _DW_MAX_TOKENS)

        # Scale proportionally: bytes → tokens → safety margin → overhead.
        raw_tokens = total_bytes / _DW_CHARS_PER_TOKEN
        needed = int(raw_tokens * _DW_OUTPUT_SAFETY) + _DW_OUTPUT_OVERHEAD_TOKENS
        # Never squeeze below the complexity ceiling — dynamic budget is
        # strictly a floor-raiser, never a ceiling-lowerer.
        needed = max(needed, int(complexity_ceiling))
        return min(needed, _DW_MAX_TOKENS)

    # ------------------------------------------------------------------
    # Synchronous generate() — kept for backwards compatibility.
    # Combines submit_batch + poll_and_retrieve in a single blocking call.
    # ------------------------------------------------------------------

    async def generate(
        self,
        context: OperationContext,
        deadline: Any = None,
        repair_context: Optional[Any] = None,
        *,
        prompt_override: Optional[str] = None,
    ) -> GenerationResult:
        """Generate code via Doubleword batch API (blocking).

        Parameters
        ----------
        context:
            OperationContext with target files and description.
        deadline:
            datetime deadline from orchestrator (used to cap poll time).
            Conforms to CandidateProvider protocol.
        repair_context:
            Slice 8 — accepted to honor the ``CandidateProvider`` Protocol
            shape used by ``RepairEngine._generate_repair_candidate``.
            ClaudeProvider and PrimeProvider both accept this positional
            kwarg (see providers.py:4580 and providers.py:7568). Pre-Slice-8
            DoublewordProvider omitted it, so L2's call site
            ``self._prime.generate(ctx, deadline, repair_context=...)``
            raised ``TypeError: DoublewordProvider.generate() got an
            unexpected keyword argument 'repair_context'`` whenever the
            L2 prime provider was DW (bt-2026-05-25-205710 root, fully
            captured via Slice 7's traceback observability).

            CURRENT BEHAVIOR: accepted and stored as ``_dw_repair_context``
            attribute on the call (for future telemetry) but NOT
            incorporated into the DW prompt. DW's batch API isn't yet
            wired for repair-context-aware prompting — adding that is a
            separate slice (Slice 9 candidate). For now this preserves
            Protocol contract uniformity without changing DW's generation
            behavior. The L2 loop sees the Protocol contract honored;
            repair-loop reasoning lands on Claude via the route cascade.
        prompt_override:
            Optional prompt to use instead of building from context.

        Returns GenerationResult with provider_used="doubleword".
        Falls through to empty result on failure (caller handles fallback).

        For non-blocking usage, prefer submit_batch() + poll_and_retrieve().
        """
        # Slice 8 — repair_context is accepted to match the Protocol shape
        # used by Claude/Prime providers. Stored for future incorporation
        # into DW prompting (Slice 9 candidate); currently advisory-only.
        # NOT used in prompt assembly so byte-equivalence with the
        # pre-Slice-8 DW generation path is preserved.
        _dw_repair_context = repair_context  # noqa: F841 — reserved
        del _dw_repair_context
        if not self.is_available:
            return GenerationResult(
                candidates=(),
                provider_name="doubleword",
                generation_duration_s=0.0,
            )

        # Zero-Waste S1 (D2) cache gate. Eligible only when the
        # provider-response cache is enabled AND DW's tool loop will
        # be skipped (tool_loop is None OR task_complexity in
        # {trivial,simple} per _generate_realtime's _will_skip_tools
        # discipline). The prompt is assembled once at this layer and
        # passed through as prompt_override (existing plumbing) so the
        # gate keys on the actually-built prompt. Authority-asymmetry:
        # this file imports cached_or_generate ONLY (no inline cache
        # class / no OrderedDict LRU). NEVER raises (fail-open).
        try:
            from backend.core.ouroboros.governance.provider_response_cache import (  # noqa: E501
                cached_or_generate as _zw_cached_or_generate,
                response_cache_enabled as _zw_response_cache_enabled,
            )
        except Exception:  # noqa: BLE001 — substrate optional / fail-open
            _zw_cached_or_generate = None
            _zw_response_cache_enabled = lambda: False  # noqa: E731
        if (
            _zw_cached_or_generate is not None
            and _zw_response_cache_enabled()
        ):
            _zw_prompt = prompt_override
            if _zw_prompt is None:
                try:
                    from backend.core.ouroboros.governance.providers import (
                        _build_codegen_prompt,
                        _build_lean_codegen_prompt,
                        _should_use_lean_prompt,
                    )
                    _zw_complexity = getattr(
                        context, "task_complexity", "",
                    )
                    # Slice 45 note: this dormant response-cache path uses
                    # the legacy complexity-only skip test, NOT the
                    # terminal-worker policy. Harmless today — the cache is
                    # default-OFF and ``_zw_eligible`` below gates this path
                    # to loop-less / trivial-simple ops, so a moderate/heavy
                    # terminal-worker background op never assembles its
                    # prompt here (it falls through to the main path). If
                    # JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED is ever turned
                    # on, mirror background_is_terminal_worker here to keep
                    # the assembled prompt coherent with the main path.
                    _zw_will_skip = _zw_complexity in (
                        "trivial", "simple",
                    )
                    _zw_tools_available = (
                        self._tool_loop is not None
                        and not _zw_will_skip
                    )
                    _zw_preloaded: List[str] = []
                    if _should_use_lean_prompt(
                        context, tools_enabled=_zw_tools_available,
                    ):
                        _zw_prompt = _build_lean_codegen_prompt(
                            context,
                            repo_root=self._repo_root,
                            repo_roots=self._repo_roots or None,
                            force_full_content=True,
                            mcp_tools=None,
                            preloaded_out=_zw_preloaded,
                        )
                    else:
                        _zw_prompt = _build_codegen_prompt(
                            context,
                            repo_root=self._repo_root,
                            repo_roots=self._repo_roots or None,
                            force_full_content=True,
                            mcp_tools=None,
                            provider_route=getattr(
                                context, "provider_route", "",
                            ) or "",
                        )
                except Exception:  # noqa: BLE001 — fail-open
                    _zw_prompt = None
            _zw_will_skip = getattr(
                context, "task_complexity", "",
            ) in ("trivial", "simple")
            _zw_eligible = (self._tool_loop is None) or _zw_will_skip
            if _zw_prompt is not None and _zw_eligible:
                async def _dw_inner():
                    # Slice 9.1 — thread repair_context (closure-captured
                    # from the outer generate() scope, where it IS a parameter)
                    return await self._dispatch_internal(
                        context, deadline,
                        prompt_override=_zw_prompt,
                        repair_context=repair_context,
                    )

                try:
                    _zw_model = self._effective_model_id(context)
                except Exception:  # noqa: BLE001
                    _zw_model = getattr(self, "_model", "doubleword")

                # ── S2 — Predictive Budget Preemption (PRD §11.4) ──
                # Co-located with assembled _zw_prompt (B3 invariant).
                # Master OFF ⇒ skipped; behavior byte-identical. S2 is
                # ADVISORY — does NOT alter this op's dispatch path;
                # merely emits a preemption signal to nudge
                # sensor_governor against FUTURE low-priority sensor
                # emissions. High-urgency routes are immune at the
                # governor's signal-application surface. Uses
                # _effective_model_id(context) — the actual model
                # resolved for dispatch, NOT self._model. NEVER raises.
                try:
                    from backend.core.ouroboros.governance.s2_predictive_budget import (  # noqa: E501
                        evaluate_admission_pressure as _s2_pressure_check,
                        emit_preemption_signal as _s2_emit,
                    )
                    _s2_severity = _s2_pressure_check(
                        prompt_text=(_zw_prompt or ""),
                        route=getattr(
                            context, "provider_route", "",
                        ) or "standard",
                        model=str(_zw_model or ""),
                    )
                    if _s2_severity is not None:
                        _s2_emit(severity=_s2_severity,
                                 high_prio_queued=True)
                except Exception as _s2_exc:  # noqa: BLE001 — fail-open
                    logger.debug(
                        "[S2] DW admission integration degraded: %s",
                        _s2_exc,
                    )
                # ───────────────────────────────────────────────────

                _gr, _ = await _zw_cached_or_generate(
                    prompt=_zw_prompt,
                    model=_zw_model,
                    route=getattr(
                        context, "provider_route", "",
                    ) or "",
                    repo_root=self._repo_root,
                    produce=_dw_inner,
                )
                return _gr

        # Cache disabled / not eligible: fall through to the existing
        # dispatcher (RT + batch fall-back) verbatim — extracted into
        # _dispatch_internal so the gate's _dw_inner closure can call
        # the same code path without duplication.
        # Slice 9.1 — thread repair_context for L2 single-shot.
        return await self._dispatch_internal(
            context, deadline,
            prompt_override=prompt_override,
            repair_context=repair_context,
        )

    async def _dispatch_internal(
        self,
        context: OperationContext,
        deadline: Any = None,
        *,
        prompt_override: Optional[str] = None,
        repair_context: Optional[Any] = None,  # Slice 9.1 — thread for L2 single-shot
    ) -> GenerationResult:
        """RT + batch dispatcher. Extracted from :meth:`generate` so
        the Zero-Waste S1 cache gate's ``_dw_inner`` thunk can
        invoke the same dispatch path without duplicating it
        (single source of dispatch truth). Behavior is byte-
        identical to the pre-extraction body."""
        # ── DW Heavy Non-Streaming Lane (PRD: "Functions, Not Agents") ─
        # Master OFF (default) ⇒ entire block skipped; behavior
        # byte-identical to today. When master ON AND op eligibility
        # holds AND PREFER_OVER_SSE is on: skip SSE entirely and
        # dispatch via the non-streaming compose-of-complete_sync lane.
        # Failures cascade naturally (the wrapper raises
        # DoublewordInfraError which the orchestrator already handles).
        try:
            if (
                _dw_heavy_fn_lane_master_enabled()
                and self._should_use_heavy_nonstreaming_lane(
                    context, sentinel_recent_sse_stall=False,
                )
            ):
                logger.info(
                    "[DoublewordProvider] heavy-nonstreaming lane "
                    "engaged (PREFER_OVER_SSE) for op=%s route=%s "
                    "complexity=%s",
                    getattr(context, "op_id", "")[:24],
                    getattr(context, "provider_route", "") or "",
                    getattr(context, "task_complexity", "") or "",
                )
                return await self._generate_heavy_nonstreaming(
                    context, deadline, prompt_override=prompt_override,
                )
        except Exception as _heavy_exc:  # noqa: BLE001
            # Master-gated; any fault degrades to the legacy SSE/batch
            # path. We do NOT silently swallow — log + cascade.
            logger.warning(
                "[DoublewordProvider] heavy-nonstreaming lane "
                "degraded; falling through to legacy dispatch: %s",
                _heavy_exc,
            )

        # Slice 36 — adaptive transport selector. v31 proved DW RT
        # streaming TTFT p50 = 66.8s vs batch end-to-end 4-8s for
        # the same prompts. STANDARD/COMPLEX routes under pure-DW
        # config skip RT entirely and go straight to batch.
        _slice36_force_batch = _slice36_should_force_batch(context, model_id=_effective_model)
        if _slice36_force_batch:
            # Slice 171 — surface the Slice 170 capital save (records iff Claude was
            # available, i.e. a rupture-reroute we'd otherwise have cascaded to Claude).
            _record_intra_failover_telemetry(context, True)
            logger.info(
                "[DoublewordProvider] Slice 36 transport selector: "
                "STANDARD/COMPLEX + Claude-disabled → BATCH path "
                "(skipping RT; v31 evidence: RT TTFT 66s vs BATCH 4-8s) "
                "op=%s route=%s",
                getattr(context, "op_id", "?")[:16],
                getattr(context, "provider_route", "?"),
            )

        # Real-time mode: /v1/chat/completions with SSE streaming + Venom tool loop
        # On 429/503, fall back to batch within DW (stay cheap) instead of
        # cascading to the 150x more expensive Claude fallback.
        if self._realtime_enabled and not _slice36_force_batch:
            try:
                # Slice 9.1 — thread repair_context for L2 single-shot
                return await self._generate_realtime(
                    context, deadline,
                    prompt_override=prompt_override,
                    repair_context=repair_context,
                )
            except StreamRuptureError as _s181_rupture:
                # Slice 181 — INTRA-REQUEST HEDGE. The RT SSE stream RUPTURED
                # (ClientPayloadError: TransferEncodingError). Don't fail the model and
                # bubble live_transport to the sentinel — re-submit the SAME payload over the
                # stream-free BATCH lane within this same FSM tick. The op never sees the
                # rupture; it just succeeds, more slowly. Gated; flag-off → legacy re-raise.
                from backend.core.ouroboros.governance.dw_immortal import (
                    dw_hedge_enabled as _s181_hedge_on,
                    hedge_to_batch_on_rupture as _s181_hedge,
                )
                if _s181_hedge_on() and _s181_hedge(str(_s181_rupture) or "live_transport"):
                    logger.warning(
                        "[Immortal] RT stream RUPTURED → HEDGING to DW-batch within the same "
                        "tick (op=%s) — the op never sees the rupture",
                        getattr(context, "op_id", "?")[:16],
                    )
                    # fall through to batch mode below (the hedge)
                else:
                    raise
            except DoublewordInfraError as exc:
                if exc.status_code in (429, 503):
                    logger.info(
                        "[DoublewordProvider] Real-time returned %d, falling back to batch",
                        exc.status_code,
                    )
                    # Fall through to batch mode below
                else:
                    raise  # Non-retriable: propagate to CandidateGenerator FSM

        # Batch mode: 4-stage async batch API (fallback from real-time, or explicit opt-in)
        t0 = time.monotonic()
        self._last_error_status = 0  # reset before attempt

        pending = await self.submit_batch(context, prompt_override=prompt_override)
        if pending is None:
            raise DoublewordInfraError(
                "Batch submission failed", status_code=self._last_error_status,
            )

        result = await self.poll_and_retrieve(pending, context)
        if result is None:
            raise DoublewordInfraError(
                "Batch retrieval failed", status_code=self._last_error_status,
            )
        # ── S2 — record op_outcome for MAD sample stream (PRD §11 B4) ─
        # Reached ONLY on real provider success (post-batch return).
        # Belt-and-suspenders: skip if provider_name carries the
        # "+cache" reconstruction marker. Uses _effective_model_id
        # (the actual model used for dispatch). Master-gated;
        # NEVER raises.
        try:
            from backend.core.ouroboros.governance.s2_predictive_budget import (  # noqa: E501
                master_enabled as _s2_master_check,
            )
            if _s2_master_check():
                _pname = str(getattr(result, "provider_name", "") or "")
                if not _pname.endswith("+cache"):
                    try:
                        _s2_eff_model = self._effective_model_id(context)
                    except Exception:  # noqa: BLE001
                        _s2_eff_model = getattr(self, "_model", "") or ""
                    from backend.core.ouroboros.governance.admission_estimator import (  # noqa: E501
                        get_default_history as _s2_history,
                    )
                    _s2_history().record_op_outcome(
                        route=getattr(
                            context, "provider_route", "",
                        ) or "standard",
                        model=str(_s2_eff_model or ""),
                        output_tokens=int(
                            getattr(result, "total_output_tokens", 0) or 0,
                        ),
                        cost_usd=float(
                            getattr(result, "cost_usd", 0.0) or 0.0,
                        ),
                    )
        except Exception as _s2_rec_exc:  # noqa: BLE001 — fail-open
            logger.debug(
                "[S2] DW op_outcome record degraded: %s",
                _s2_rec_exc,
            )
        # ───────────────────────────────────────────────────────────
        return result

    # ------------------------------------------------------------------
    # DW Heavy Non-Streaming Lane — composes complete_sync()
    # ------------------------------------------------------------------
    # See module-level docstring above ``_dw_heavy_fn_lane_master_enabled``
    # for architectural framing. These two methods are the only
    # provider-side additions; the existing ``complete_sync`` primitive
    # is reused verbatim (only additively extended with
    # ``enable_thinking`` per operator design lock).

    def _should_use_heavy_nonstreaming_lane(
        self,
        context: Any,
        *,
        sentinel_recent_sse_stall: bool,
    ) -> bool:
        """Deterministic routing predicate composing EXISTING state —
        no new ``OperationContext`` fields. NEVER raises (fail-safe
        to ``False``).

        Eligibility derives from:
          * ``context.task_complexity`` ∈ env-configured set
            (default ``{complex, heavy_code}``)
          * ``self._tool_loop`` — if a Venom-style multi-turn loop
            is wired AND the op is heavy, the SSE path is the right
            tool unless SSE has demonstrably stalled
          * ``sentinel_recent_sse_stall`` — externally observed
            transport state from existing
            ``dw_topology_circuit_breaker`` / ``topology_sentinel``
            surfaces (caller passes in)

        Returns ``True`` only when the heavy lane should fire.
        Master-flag check must be done by the caller (this predicate
        assumes master ON; centralizing the master read at the
        caller keeps the predicate test-friendly)."""
        try:
            complexity = (
                getattr(context, "task_complexity", "") or ""
            ).strip().lower()
            eligible = set(_dw_heavy_fn_lane_eligible_complexities())
            if complexity not in eligible:
                return False
            has_active_tool_loop = (
                self._tool_loop is not None
                and complexity in eligible
            )
            if has_active_tool_loop:
                # Multi-turn agent op: stay on SSE unless transport
                # has stalled AND the operator opts-in via
                # PREFER_ON_SSE_STALL.
                return bool(
                    sentinel_recent_sse_stall
                    and _dw_heavy_fn_lane_prefer_on_sse_stall()
                )
            # No tool loop = naturally single-shot eligible.
            if _dw_heavy_fn_lane_prefer_over_sse():
                return True
            return bool(sentinel_recent_sse_stall)
        except Exception:  # noqa: BLE001 — defensive
            return False

    async def _generate_heavy_nonstreaming(
        self,
        context: Any,
        deadline: Any = None,
        *,
        prompt_override: Optional[str] = None,
    ) -> GenerationResult:
        """Heavy codegen non-streaming lane (PRD: "Functions, Not Agents"
        for codegen workloads). Composes the existing
        :meth:`complete_sync` primitive — does NOT duplicate the
        ``stream=false`` POST logic. Parses the response via the
        existing ``_parse_generation_response`` (no parallel parser).
        Returns :class:`GenerationResult` (not
        :class:`CompleteSyncResult`).

        Model resolved via :meth:`_resolve_effective_model` —
        **no hardcoded model name**. Prompt assembly composes the
        existing ``prompt_override`` / ``_build_codegen_prompt`` seam
        (S1 cache key compatibility preserved).

        Failure modes:
          * The wrapper raises the same exceptions ``complete_sync``
            does (``DoublewordInfraError``, ``asyncio.TimeoutError``,
            ``ValueError``). The orchestrator's existing
            cascade-to-Claude branch handles them unchanged.
        """
        from backend.core.ouroboros.governance.providers import (
            _build_codegen_prompt,
            _parse_generation_response,
        )

        if prompt_override is not None:
            assembled_prompt = prompt_override
        else:
            assembled_prompt = _build_codegen_prompt(
                context,
                repo_root=self._repo_root,
                repo_roots=self._repo_roots or None,
                force_full_content=True,
                mcp_tools=None,
                provider_route=getattr(
                    context, "provider_route", "",
                ) or "",
            )

        try:
            effective_model = self._resolve_effective_model(context)
        except Exception:  # noqa: BLE001 — defensive
            effective_model = getattr(self, "_model", "") or ""

        # Hand off to the canonical Functions-not-Agents primitive.
        # ``enable_thinking=True`` (env-driven; default TRUE) unlocks
        # DW reasoning over the stream-free transport — the whole
        # point of this lane.
        from backend.core.ouroboros.governance.providers import (
            _CODEGEN_SYSTEM_PROMPT,
        )
        t0 = time.monotonic()
        sync_result = await self.complete_sync(
            prompt=assembled_prompt,
            system_prompt=_CODEGEN_SYSTEM_PROMPT,
            caller_id="heavy_codegen",
            model=effective_model or None,
            max_tokens=_dw_heavy_fn_lane_max_tokens(),
            timeout_s=_dw_heavy_fn_lane_timeout_s(),
            temperature=None,                # use _DW_TEMPERATURE default
            response_format=None,
            enable_thinking=_dw_heavy_fn_lane_enable_thinking(),
        )

        # Parse the raw text through the existing codegen parser —
        # NO parallel parser. The parser builds the multi-candidate
        # GenerationResult shape the orchestrator expects.
        source_path = (
            context.target_files[0]
            if getattr(context, "target_files", None) else ""
        )
        source_hash = ""
        if source_path:
            try:
                from backend.core.ouroboros.governance.providers import (
                    _file_source_hash,
                )
                abs_path = (
                    (self._repo_root / source_path)
                    if self._repo_root else Path(source_path)
                )
                if abs_path.is_file():
                    source_hash = _file_source_hash(
                        abs_path.read_text(
                            encoding="utf-8", errors="replace",
                        )
                    )
            except Exception:  # noqa: BLE001 — best-effort
                pass

        duration = time.monotonic() - t0
        # Slice 20B — _parse_with_heal wraps _parse_generation_response
        # with an LLM-heal retry on json_parse_error.
        parsed = await self._parse_with_heal(
            raw=sync_result.content,
            provider_name="doubleword-heavy-nonstreaming",
            duration_s=duration,
            ctx=context,
            source_hash=source_hash,
            source_path=source_path,
            repo_roots=self._repo_roots,
            repo_root=self._repo_root,
        )
        # Attach token usage + cost from CompleteSyncResult so the
        # downstream orchestrator sees the same accounting fields it
        # gets from the legacy RT / batch paths.
        import dataclasses as _dc
        parsed = _dc.replace(
            parsed,
            total_input_tokens=int(sync_result.input_tokens or 0),
            total_output_tokens=int(sync_result.output_tokens or 0),
            cost_usd=float(sync_result.cost_usd or 0.0),
            model_id=str(sync_result.model or effective_model or ""),
        )
        logger.info(
            "[DoublewordProvider] heavy-nonstreaming ok in %.2fs: "
            "%d candidates, %d+%d tokens, cost=$%.4f (model=%s)",
            duration, len(parsed.candidates),
            parsed.total_input_tokens, parsed.total_output_tokens,
            parsed.cost_usd, effective_model,
        )
        return parsed

    # ------------------------------------------------------------------
    # Real-time generation via /v1/chat/completions (Venom-compatible)
    # ------------------------------------------------------------------

    async def _generate_realtime(
        self,
        context: OperationContext,
        deadline: Any = None,
        *,
        prompt_override: Optional[str] = None,
        repair_context: Optional[Any] = None,  # Slice 9.1 — L2 single-shot signal
    ) -> GenerationResult:
        """Generate code via DoubleWord real-time chat completions API.

        Uses ``/v1/chat/completions`` (OpenAI-compatible) instead of the
        batch API.  This enables the Venom tool loop: the provider can
        call read_file, search_code, run_tests, bash, etc. during
        generation — the same multi-turn agentic loop that ClaudeProvider
        supports.

        30-37x cheaper than Claude with the same tool-use capability.
        """
        from backend.core.ouroboros.governance.providers import (
            _build_codegen_prompt,
            _build_lean_codegen_prompt,
            _should_use_lean_prompt,
            _parse_generation_response,
        )
        from datetime import datetime, timezone

        self._check_budget()
        t0 = time.monotonic()
        total_cost = 0.0
        self._last_chunk_at = 0.0  # reset — prevents stale timestamps from prior generation

        # Slice 35 Phase 1 — RT stage profiler. Composes Slice 34's
        # dispatch_profiler (no parallel telemetry surface). Default
        # OFF via JARVIS_DISPATCH_PROFILER_ENABLED so production stays
        # byte-identical until operator opts into the v31 telemetry
        # probe. record_stage() never raises into the caller.
        from backend.core.ouroboros.telemetry import (
            dispatch_profiler as _s35_dp,
        )
        _s35_op_id = getattr(context, "op_id", "?") or "?"
        _s35_model = self._model or "(unspecified)"
        _s35_stage_t0 = time.monotonic()

        # Gap #7: discover MCP tools for prompt injection
        _mcp_tools = None
        if self._mcp_client is not None:
            try:
                _mcp_tools = await self._mcp_client.discover_tools()
            except Exception:
                pass

        # P0.1: Lean tool-first prompt — 60-70% smaller than the full prompt.
        # When Venom tool loop is available, send a minimal instruction and let
        # the model pull context incrementally via read_file/search_code/etc.
        # Manifesto §5: "Agentic intelligence handles the 5% that is novel."
        #
        # Slice 12AF Site 4 — extend the skip-tools predicate to cover
        # ALL routes in VENOM_SKIP_ROUTES (background / speculative /
        # wiring_validation) so DW's prompt path mirrors the Claude
        # provider's gate. Without this, a wiring-validation op routed
        # to DW would still get the lean tool-first prompt → DW returns
        # 2b.2-tool → no loop to consume → ``schema_invalid:
        # tool_call_without_tool_loop`` → cascade exhaustion. Composes
        # the canonical predicate from Slice 12AD's route_predicates
        # module — single source of truth for "this route skips Venom".
        from backend.core.ouroboros.governance.route_predicates import (
            should_skip_venom_for_route,
        )
        from backend.core.ouroboros.governance.dw_terminal_worker_policy import (
            background_is_terminal_worker,
        )
        _complexity = getattr(context, "task_complexity", "")
        _route = getattr(context, "provider_route", "") or ""
        # Slice 45 — keep the prompt-layer tool advertisement coherent with
        # the exec-layer tool-loop run condition. When DW is the terminal
        # worker (Claude disabled), a BACKGROUND op runs the Venom loop
        # whenever it is non-trivial (line ~2632 ``_skip_tools =
        # complexity == "trivial"``) and therefore MUST be told the tools
        # exist. We mirror that exec gate EXACTLY here (skip only trivial)
        # so the prompt's tool advertisement matches whether the loop
        # actually runs — for simple AND moderate/heavy background alike
        # (the simple-background half of the v40b deadlock would otherwise
        # remain: loop runs, tools suppressed). Env-gated + BACKGROUND-only
        # -> byte-identical legacy when Claude is enabled / flag off.
        if background_is_terminal_worker(str(_route)):
            _will_skip_tools = (_complexity == "trivial")
        else:
            _will_skip_tools = (
                _complexity in ("trivial", "simple")
                or should_skip_venom_for_route(str(_route))
            )
        # ──────────────────────────────────────────────────────────────
        # Slice 9 — L2 single-shot fast path (DW mirror)
        # See PrimeProvider providers.py:4710 for full rationale.
        # bt-2026-05-25-211028 deterministic tool_loop_starved bail
        # also affects DW when L2 routes to it. ``repair_context``
        # presence signals L2's _generate_repair_candidate is the
        # caller (Slice 8 made the kwarg accepted; Slice 9 routes
        # L2 around the tool loop entirely).
        # ──────────────────────────────────────────────────────────────
        if repair_context is not None and not _will_skip_tools:
            logger.info(
                "[DoublewordProvider] L2 repair_context detected — Slice 9 "
                "single-shot fast path: skipping Venom tool loop"
            )
            _will_skip_tools = True
        _tools_available = self._tool_loop is not None and not _will_skip_tools
        _preloaded_files: List[str] = []
        if prompt_override:
            prompt = prompt_override
        elif _should_use_lean_prompt(context, tools_enabled=_tools_available):
            prompt = _build_lean_codegen_prompt(
                context,
                repo_root=self._repo_root,
                repo_roots=self._repo_roots or None,
                force_full_content=True,
                mcp_tools=_mcp_tools,
                preloaded_out=_preloaded_files,
            )
            logger.info(
                "[DoublewordProvider] RT: using lean prompt (%d chars, ~%d tokens, preloaded=%d)",
                len(prompt), len(prompt) // 4, len(_preloaded_files),
            )
        else:
            prompt = _build_codegen_prompt(
                context,
                repo_root=self._repo_root,
                repo_roots=self._repo_roots or None,
                force_full_content=True,
                mcp_tools=_mcp_tools,
                provider_route=getattr(context, "provider_route", "") or "",
            )
            logger.info(
                "[DoublewordProvider] RT: using full prompt (%d chars, ~%d tokens, route=%s)",
                len(prompt), len(prompt) // 4,
                getattr(context, "provider_route", "") or "unknown",
            )

        # Slice 35 Phase 1 — record STAGE_RT_PROMPT_BUILD: prompt
        # assembly + MCP discovery + lean detection ended here.
        _s35_dp.record_stage(
            "STAGE_RT_PROMPT_BUILD",
            op_id=_s35_op_id, model_id=_s35_model,
            duration_ms=(time.monotonic() - _s35_stage_t0) * 1000.0,
        )

        _SYSTEM_PROMPT = (
            "You are a code generation assistant for the JARVIS Trinity AI Ecosystem. "
            "RESPOND WITH ONLY A SINGLE VALID JSON OBJECT. "
            "RULES: "
            "1. Start your response with { and end with }. "
            "2. No text before or after the JSON. No markdown fences. No explanations. "
            "3. All string values must use double quotes. Escape special characters: "
            "use \\n for newlines, \\t for tabs, \\\\ for backslashes. "
            "4. No trailing commas before } or ]. "
            "5. Use schema_version '2b.1' with full_content containing the COMPLETE file. "
            "6. NEVER return unified diffs, patches, or partial file content. "
            "7. CRITICAL: Every candidate MUST include a non-empty 'rationale' field "
            "(1 sentence, max 200 chars) explaining WHY the change is being made. "
            "Missing or empty rationale will cause the response to be rejected."
        )

        # Mutable container to capture token usage from _generate_raw
        _token_usage: Dict[str, int] = {"input": 0, "output": 0}

        # Resolve effective model once via topology — routes map to
        # distinct DW models under the Brain Selection Topology
        # (STANDARD→397B, BACKGROUND/SPECULATIVE→Gemma 4 31B). The lookup
        # is pure yaml-driven, no env overrides. Hard-blocked routes
        # (IMMEDIATE + COMPLEX) never reach this method.
        _effective_model = self._resolve_effective_model(context)
        if _effective_model != self._model:
            logger.info(
                "[DoublewordProvider] RT: topology override model=%s "
                "(default=%s, route=%s)",
                _effective_model, self._model,
                getattr(context, "provider_route", "?"),
            )

        async def _generate_raw(p: str) -> str:
            """Single chat completion call (used by tool_loop.run())."""
            nonlocal total_cost
            session = await self._get_session()

            # Dynamic max_tokens: complexity-aware ceiling as the floor,
            # scaled up by actual target file bytes so full-file rewrites
            # don't truncate mid-string. Tool rounds get a small fixed
            # budget (tool call JSON is ~1K tokens). See
            # _compute_dynamic_max_tokens for the formula.
            _is_tool_round = (
                self._tool_loop is not None
                and getattr(self._tool_loop, "is_tool_round", False)
            )
            _eff_max_tokens = self._compute_dynamic_max_tokens(
                context, is_tool_round=_is_tool_round,
            )

            # Streaming callback for token-by-token TUI output
            _stream_callback = None
            if self._tool_loop is not None:
                _stream_callback = getattr(self._tool_loop, "on_token", None)

            # Multi-modal user content — when ctx.attachments is non-empty and
            # the GENERATE purpose gate is open, splice OpenAI-compatible
            # image_url blocks alongside the text prompt. Manifesto §1:
            # Tri-Partite Microkernel — Mind perceives what Senses captured.
            # Lazy import avoids providers.py ↔ doubleword_provider.py cycle.
            from backend.core.ouroboros.governance.providers import (
                _serialize_attachments as _dw_serialize_attachments,
            )
            _att_blocks = _dw_serialize_attachments(
                context, provider_kind="doubleword", purpose="generate",
            )
            if _att_blocks:
                _user_content: Any = [{"type": "text", "text": p}, *_att_blocks]
                _atts = getattr(context, "attachments", ())
                _kinds = ",".join(sorted({a.kind for a in _atts})) or "-"
                _mimes = ",".join(sorted({a.mime_type for a in _atts})) or "-"
                _hashes = ",".join(a.hash8 for a in _atts) or "-"
                _bytes = 0
                for _a in _atts:
                    try:
                        _bytes += os.path.getsize(_a.image_path)
                    except OSError:
                        pass
                logger.info(
                    "[DoublewordProvider] multi_modal op=%s blocks=%d "
                    "attachments=%d bytes=%d kinds=[%s] mime_kinds=[%s] "
                    "hash8s=[%s] route=%s purpose=generate",
                    getattr(context, "operation_id", "-"),
                    len(_att_blocks), len(_atts), _bytes, _kinds, _mimes, _hashes,
                    (getattr(context, "provider_route", "") or "-"),
                )
            else:
                _user_content = p

            body = {
                "model": _effective_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _user_content},
                ],
                "max_tokens": _eff_max_tokens,
                "temperature": _DW_TEMPERATURE,
                # Slice 54/55 — reasoning control, effort derived from
                # task_complexity (see _reasoning_request_params).
                **_reasoning_request_params(complexity=_complexity or "", model=_effective_model),
            }

            if _stream_callback is not None:
                # Streaming path: SSE for token-by-token output.
                # Use a generous per-chunk timeout (30s between chunks)
                # to detect stalled streams without killing slow generation.
                body["stream"] = True
                content = ""
                input_tokens = 0
                output_tokens = 0
                _PER_CHUNK_TIMEOUT = 30.0  # seconds between SSE chunks

                # Priority 1 Slice 1 — confidence capture (PRD §26.5.1).
                # Master-flag-gated; when enabled, request OpenAI-compat
                # per-token logprobs from the provider so the streaming
                # parse below can capture the top-1/top-2 margin signal
                # into ctx.artifacts["confidence_capturer"]. Capture is
                # purely additive on the response; the request shape
                # only changes when the flag is on (byte-for-byte
                # preserved when off).
                from backend.core.ouroboros.governance.verification.confidence_capture import (
                    ConfidenceCapturer,
                    confidence_capture_enabled,
                    confidence_capture_top_k,
                    extract_openai_compat_logprobs_from_chunk,
                )
                # Priority 1 Slice 2 — confidence monitor + circuit-breaker.
                # When ENABLED, the monitor consumes the per-token margin
                # signal alongside the capturer and produces a verdict;
                # ENFORCE sub-flag governs whether BELOW_FLOOR raises
                # ConfidenceCollapseError mid-stream. Slice 2 ships
                # SHADOW only — both flags default false; ENFORCE flips
                # in Slice 5 graduation.
                from backend.core.ouroboros.governance.verification.confidence_monitor import (
                    ConfidenceMonitor,
                    ConfidenceVerdict,
                    confidence_monitor_enabled,
                    confidence_monitor_enforce,
                )
                _confidence_capturer: Optional[ConfidenceCapturer] = None
                _confidence_monitor: Optional[ConfidenceMonitor] = None
                _monitor_enforce_active: bool = False
                if confidence_capture_enabled():
                    body["logprobs"] = True
                    body["top_logprobs"] = confidence_capture_top_k()
                    _confidence_capturer = ConfidenceCapturer(
                        provider="doubleword",
                        model_id=str(_effective_model or ""),
                    )
                    # Slice 2 monitor wakes only when its own master flag
                    # is on. Capture without monitor remains valid (Slice 1
                    # observation-only mode for ledger/replay use).
                    if confidence_monitor_enabled():
                        _confidence_monitor = ConfidenceMonitor(
                            provider="doubleword",
                            model_id=str(_effective_model or ""),
                            op_id=str(
                                getattr(context, "op_id", "") or "",
                            ),
                        )
                        _monitor_enforce_active = (
                            confidence_monitor_enforce()
                        )
                    # Stash on ctx.artifacts so downstream phase runners
                    # (Slice 2 monitor) can read the trace post-stream.
                    try:
                        _artifacts = getattr(context, "artifacts", None)
                        if isinstance(_artifacts, dict):
                            _artifacts["confidence_capturer"] = (
                                _confidence_capturer
                            )
                            if _confidence_monitor is not None:
                                _artifacts["confidence_monitor"] = (
                                    _confidence_monitor
                                )
                    except Exception:  # noqa: BLE001 — capture must
                        pass        # never break the stream loop
                    # §37 Tier 2 #13 Slice 2 (2026-05-07) — propagate the
                    # capturer to tool_executor via async-safe ContextVar
                    # (PolicyContext doesn't carry artifacts dict). Each
                    # op runs in its own asyncio.Task so the var is
                    # task-local; new GENERATE rounds re-stamp the var
                    # with their own capturer. Defensive: set failure
                    # NEVER breaks the stream loop.
                    try:
                        from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
                            set_active_capturer as _toolconf_set_var,
                        )
                        _toolconf_set_var(_confidence_capturer)
                    except Exception:  # noqa: BLE001 — defensive
                        pass
                # Phase 12.2 Slice C — TTFT measurement window opens
                # the moment we issue the request and closes on first
                # non-empty content chunk. monotonic() is jump-proof
                # under wall-clock corrections.
                _ttft_request_start_monotonic = time.monotonic()
                _ttft_first_chunk_seen = False

                # Slice 35 Phase 1 — STAGE_RT_AEGIS_AUTH timer.
                _s35_auth_t0 = time.monotonic()
                # Slice 31 — Aegis session bearer (closes v24
                # missing_session_bearer 401 wedge on RT streaming).
                _call_auth = await _aegis_dw_session_auth_header()
                _call_auth["Content-Type"] = "application/json"
                # Slice 2B-ii — per-call Aegis lease (None when disabled
                # → header is skipped via merge helper).
                _aegis_lease = await _aegis_acquire_call_lease(
                    op_id=context.op_id,
                    route="standard",
                    estimated_cost_usd=0.05,
                )
                _s35_dp.record_stage(
                    "STAGE_RT_AEGIS_AUTH",
                    op_id=_s35_op_id, model_id=_s35_model,
                    duration_ms=(time.monotonic() - _s35_auth_t0) * 1000.0,
                )
                # Slice 35 Phase 1 — STAGE_RT_HTTP_POST + STAGE_RT_STREAM_CONSUME
                # timers. The post() context manager handle is the
                # HTTP handshake; the chunk loop INSIDE the `async
                # with` is the stream consumption. We capture the
                # handshake-only time by measuring up to the first
                # chunk arrival via _ttft_first_chunk_seen.
                _s35_post_t0 = time.monotonic()
                async with session.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=_aegis_merge_lease_headers(
                        _call_auth, _aegis_lease,
                    ),
                    timeout=self._request_timeout(),
                ) as resp:
                    if resp.status >= 300:
                        self._last_error_status = resp.status
                        err_body = await resp.text()
                        # Phase 12 Slice F — Substrate Error Unmasking.
                        # Preserve full response body + model_id so
                        # downstream classifier can distinguish modality
                        # 4xx from transient 5xx without regex on str(exc).
                        raise DoublewordInfraError(
                            f"Chat completions (stream) failed: "
                            f"{resp.status} {err_body[:200]}",
                            status_code=resp.status,
                            response_body=err_body,
                            model_id=_effective_model,
                        )

                    # Two-Phase Stream Rupture Breaker.
                    # Phase 1 (TTFT): generous timeout for first token.
                    # Phase 2 (Inter-Chunk): tight timeout once streaming.
                    _rupture_ttft = _stream_rupture_timeout_s()
                    _rupture_ic = _stream_inter_chunk_timeout_s()
                    _chunk_phase_timeout = _rupture_ttft  # Phase 1
                    _sse_has_tokens = False
                    # Phase-Aware Heartbeat — pulse the harness
                    # ActivityMonitor every Nth content chunk so a long
                    # DW stream stays observably fresh (Move 2 v4).
                    _stream_op_id = str(getattr(context, "op_id", "") or "")
                    _stream_chunk_count = 0
                    # Slice 87 — cognitive-stall watchdog state. The inter-chunk
                    # rupture watchdog above sees reasoning deltas and stays
                    # alive, so a model stuck reasoning with no content runs the
                    # FULL primary budget (the 240s/0-content capability stalls).
                    # Track elapsed-since-first-progress while content is empty;
                    # cascade to Tier 1 once it crosses the stall threshold.
                    _reasoning_seen = False
                    _content_seen = False
                    _first_progress_at = 0.0
                    _cognitive_stall_s = _cognitive_stall_timeout_s()
                    # Parse SSE stream with per-chunk timeout to detect stalled streams
                    while True:
                        try:
                            line = await asyncio.wait_for(
                                resp.content.readline(), timeout=_chunk_phase_timeout,
                            )
                        except asyncio.TimeoutError:
                            _rupt_elapsed = time.monotonic() - _ttft_request_start_monotonic
                            _rupt_phase = "ttft" if not _sse_has_tokens else "inter_chunk"
                            logger.error(
                                "[DoublewordProvider] STREAM RUPTURE "
                                "(phase=%s): no chunk for %.0fs "
                                "(elapsed=%.1fs, bytes=%d)",
                                _rupt_phase,
                                _chunk_phase_timeout,
                                _rupt_elapsed,
                                len(content),
                            )
                            raise StreamRuptureError(
                                provider="doubleword",
                                elapsed_s=_rupt_elapsed,
                                bytes_received=len(content),
                                rupture_timeout_s=_chunk_phase_timeout,
                                phase=_rupt_phase,
                            )
                        if not line:
                            break
                        line_str = line.decode("utf-8", errors="replace").strip()
                        if not line_str or not line_str.startswith("data: "):
                            continue
                        data_str = line_str[6:]  # Remove "data: " prefix
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            # Slice 54 — reasoning liveness. When reasoning is
                            # enabled, Qwen3.5 streams `reasoning` deltas before
                            # any `content`. Observing them marks affirmative
                            # progress so the long think phase is NOT misread as
                            # done_before_content / live_transport silence. The
                            # reasoning text itself is telemetry only — the
                            # candidate still comes from `content`.
                            _reasoning_delta = delta.get("reasoning", "") or ""
                            if _reasoning_delta and not token:
                                _reasoning_seen = True  # liveness marker
                            # Slice 87 — cognitive-stall watchdog. Mark first
                            # progress (any reasoning OR content byte), flip
                            # content_seen on the first real content token, and
                            # cascade if content stays empty past the threshold
                            # while the stream is otherwise alive.
                            if (_reasoning_delta or token) and _first_progress_at == 0.0:
                                _first_progress_at = time.monotonic()
                            if token:
                                _content_seen = True
                            if (
                                _cognitive_stall_s > 0
                                and not _content_seen
                                and _reasoning_seen
                                and _first_progress_at > 0.0
                                and (time.monotonic() - _first_progress_at)
                                > _cognitive_stall_s
                            ):
                                _stall_elapsed = (
                                    time.monotonic()
                                    - _ttft_request_start_monotonic
                                )
                                logger.warning(
                                    "[DoublewordProvider] COGNITIVE STALL: "
                                    "model=%s reasoned %.0fs with 0 content — "
                                    "cascading to Tier-1 fallback (op=%s)",
                                    _effective_model, _stall_elapsed,
                                    _stream_op_id[:24],
                                )
                                raise CognitiveStallError(
                                    provider="doubleword",
                                    elapsed_s=_stall_elapsed,
                                    bytes_received=len(content),
                                    stall_timeout_s=_cognitive_stall_s,
                                    reasoning_seen=_reasoning_seen,
                                )
                            # Priority 1 Slice 1 + Slice 2 — capture +
                            # monitor. Reads chunk's logprobs structure;
                            # feeds capturer (always when capture enabled)
                            # and monitor (when monitor enabled). Master-
                            # flag-gated upstream; when off, both are None.
                            # NEVER raises into the SSE loop EXCEPT for
                            # the explicit ConfidenceCollapseError path
                            # under ENFORCE — which propagates as a
                            # structured RuntimeError that the caller
                            # already handles via "background_dw_error" /
                            # GENERATE_RETRY routing.
                            if _confidence_capturer is not None:
                                try:
                                    for _t, _lp, _top in (
                                        extract_openai_compat_logprobs_from_chunk(
                                            chunk,
                                        )
                                    ):
                                        _confidence_capturer.append(
                                            token=_t,
                                            logprob=_lp,
                                            top_logprobs=_top,
                                        )
                                        # Slice 2 monitor: feed the
                                        # top-1/top-2 margin if there
                                        # are at least 2 alternatives.
                                        if _confidence_monitor is not None:
                                            try:
                                                if (
                                                    isinstance(_top, list)
                                                    and len(_top) >= 2
                                                ):
                                                    _entry0 = _top[0]
                                                    _entry1 = _top[1]
                                                    _lp0 = (
                                                        _entry0.get(
                                                            "logprob"
                                                        ) if isinstance(
                                                            _entry0, dict
                                                        ) else None
                                                    )
                                                    _lp1 = (
                                                        _entry1.get(
                                                            "logprob"
                                                        ) if isinstance(
                                                            _entry1, dict
                                                        ) else None
                                                    )
                                                    if (
                                                        _lp0 is not None
                                                        and _lp1 is not None
                                                    ):
                                                        _confidence_monitor.observe(
                                                            float(_lp0)
                                                            - float(_lp1)
                                                        )
                                            except Exception:  # noqa: BLE001
                                                pass
                                except Exception:  # noqa: BLE001
                                    pass

                                # Slice 2 mid-stream verdict check. Cheap
                                # (O(K)). Slice 5 graduation flips ENFORCE
                                # on; until then, the verdict is observed
                                # but never aborts. SHADOW mode tags
                                # ctx.artifacts only; ENFORCE mode raises
                                # ConfidenceCollapseError.
                                if _confidence_monitor is not None:
                                    try:
                                        _posture: Optional[str] = None
                                        try:
                                            _posture = (
                                                getattr(
                                                    context,
                                                    "current_posture", None,
                                                )
                                                or getattr(
                                                    context,
                                                    "posture", None,
                                                )
                                            )
                                        except Exception:  # noqa: BLE001
                                            _posture = None
                                        _verdict = (
                                            _confidence_monitor.evaluate(
                                                posture=(
                                                    str(_posture)
                                                    if _posture else None
                                                ),
                                            )
                                        )
                                        # Tier 1 #1 — fire SSE on
                                        # verdict state transitions.
                                        # Best-effort, defensive,
                                        # never raises. Master-flag-
                                        # gated by
                                        # JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED.
                                        try:
                                            from backend.core.ouroboros.governance.verification.confidence_sse_producer import (  # noqa: E501
                                                observe_streaming_verdict,
                                            )
                                            _snap = (
                                                _confidence_monitor
                                                .snapshot()
                                            )
                                            observe_streaming_verdict(
                                                op_id=getattr(
                                                    context,
                                                    "op_id", "",
                                                ),
                                                verdict=_verdict,
                                                rolling_margin=(
                                                    _snap.rolling_margin
                                                ),
                                                window_size=(
                                                    _snap.window_size
                                                ),
                                                observations_count=(
                                                    _snap.observations_count
                                                ),
                                                posture=(
                                                    str(_posture)
                                                    if _posture
                                                    else None
                                                ),
                                                provider="doubleword",
                                                model_id=getattr(
                                                    _confidence_monitor,
                                                    "model_id", "",
                                                ),
                                            )
                                        except Exception:  # noqa: BLE001
                                            pass
                                        if _verdict != ConfidenceVerdict.OK:
                                            try:
                                                _arts = getattr(
                                                    context,
                                                    "artifacts", None,
                                                )
                                                if isinstance(
                                                    _arts, dict,
                                                ):
                                                    _arts[
                                                        "confidence_verdict"
                                                    ] = _verdict.value
                                                    _arts[
                                                        "confidence_margin"
                                                    ] = (
                                                        _confidence_monitor
                                                        .current_margin()
                                                    )
                                            except Exception:  # noqa: BLE001
                                                pass
                                            if (
                                                _monitor_enforce_active
                                                and _verdict
                                                == ConfidenceVerdict.BELOW_FLOOR
                                            ):
                                                raise (
                                                    _confidence_monitor
                                                    .to_collapse_error(
                                                        verdict=_verdict,
                                                        posture=(
                                                            str(_posture)
                                                            if _posture
                                                            else None
                                                        ),
                                                    )
                                                )
                                    except (
                                        Exception
                                    ) as _conf_exc:  # noqa: BLE001
                                        # Re-raise ConfidenceCollapseError
                                        # so caller's retry path engages.
                                        # Other exceptions from the verdict
                                        # path are swallowed defensively.
                                        from backend.core.ouroboros.governance.verification.confidence_monitor import (
                                            ConfidenceCollapseError,
                                        )
                                        if isinstance(
                                            _conf_exc,
                                            ConfidenceCollapseError,
                                        ):
                                            raise
                            if token:
                                content += token
                                self._last_chunk_at = time.monotonic()
                                # Stream Rupture Breaker: Phase 2 step-down.
                                # Once first token arrives, tighten the
                                # watchdog to inter-chunk timeout.
                                if not _sse_has_tokens:
                                    _sse_has_tokens = True
                                    _chunk_phase_timeout = _rupture_ic
                                    # Phase-Aware Heartbeat: pulse activity
                                    # on first token (TTFT → producing).
                                    try:
                                        from backend.core.ouroboros.governance.providers import (
                                            _emit_stream_activity as _activity_pulse,
                                        )
                                        _activity_pulse(_stream_op_id)
                                    except Exception:  # noqa: BLE001
                                        pass
                                _stream_chunk_count += 1
                                # Phase-Aware Heartbeat — every Nth content
                                # chunk pulses ActivityMonitor so a long DW
                                # stream stays fresh between phase transitions.
                                if (
                                    _stream_chunk_count > 0
                                    and _stream_chunk_count % 8 == 0
                                ):
                                    try:
                                        from backend.core.ouroboros.governance.providers import (
                                            _emit_stream_activity as _activity_pulse,
                                        )
                                        _activity_pulse(_stream_op_id)
                                    except Exception:  # noqa: BLE001
                                        pass
                                # Phase 12.2 Slice C — record TTFT once
                                # per request on first non-empty content
                                # chunk. NEVER raises into the SSE loop:
                                # observer faults are swallowed so a
                                # broken observer can't kill generation.
                                if not _ttft_first_chunk_seen:
                                    _ttft_first_chunk_seen = True
                                    try:
                                        _ttft_ms = int(
                                            (self._last_chunk_at
                                             - _ttft_request_start_monotonic)
                                            * 1000.0
                                        )
                                        self._record_ttft_safely(
                                            model_id=_effective_model,
                                            ttft_ms=_ttft_ms,
                                            op_id=getattr(
                                                context, "op_id", "",
                                            ) or "",
                                        )
                                        # Slice 35 Phase 1 — record
                                        # STAGE_RT_HTTP_POST at first-chunk
                                        # arrival (TTFT closes the HTTP
                                        # handshake stage).
                                        _s35_dp.record_stage(
                                            "STAGE_RT_HTTP_POST",
                                            op_id=_s35_op_id,
                                            model_id=_s35_model,
                                            duration_ms=float(_ttft_ms),
                                        )
                                        # Reset stream-consume timer
                                        # to start AT first chunk.
                                        _s35_stream_t0 = time.monotonic()
                                    except Exception:  # noqa: BLE001
                                        pass
                                try:
                                    _stream_callback(token)
                                except Exception:
                                    pass
                            # Capture usage from final chunk
                            _usage = chunk.get("usage")
                            if _usage:
                                input_tokens = _usage.get("prompt_tokens", 0)
                                output_tokens = _usage.get("completion_tokens", 0)
                        except json.JSONDecodeError:
                            continue
            else:
                # Non-streaming path
                # Slice 31 — Aegis session bearer (closes v24
                # missing_session_bearer 401 wedge).
                _call_auth = await _aegis_dw_session_auth_header()
                _call_auth["Content-Type"] = "application/json"
                # Slice 2B-ii — per-call Aegis lease.
                _aegis_lease = await _aegis_acquire_call_lease(
                    op_id=context.op_id,
                    route="standard",
                    estimated_cost_usd=0.05,
                )
                async with session.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=_aegis_merge_lease_headers(
                        _call_auth, _aegis_lease,
                    ),
                    timeout=self._request_timeout(),
                ) as resp:
                    if resp.status >= 300:
                        self._last_error_status = resp.status
                        err_body = await resp.text()
                        # Slice F — preserve full body + model_id
                        raise DoublewordInfraError(
                            f"Chat completions failed: "
                            f"{resp.status} {err_body[:200]}",
                            status_code=resp.status,
                            response_body=err_body,
                            model_id=_effective_model,
                        )

                    data = await resp.json()
                    choices = data.get("choices", [])
                    usage = data.get("usage", {})

                    if not choices:
                        raise DoublewordInfraError("No choices in response", status_code=0)

                    content = choices[0].get("message", {}).get("content", "")
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)

            # Accumulate token usage for outer scope
            _token_usage["input"] += input_tokens
            _token_usage["output"] += output_tokens

            # Track cost
            cost = (
                input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
                + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
            )
            self._stats.total_input_tokens += input_tokens
            self._stats.total_output_tokens += output_tokens
            self._stats.total_cost_usd += cost
            self._record_cost(cost)
            total_cost += cost

            if total_cost >= self._max_cost_per_op:
                raise DoublewordInfraError(
                    f"doubleword_budget_exhausted_op:{total_cost:.4f}",
                    status_code=0,
                )

            return content

        def _parse_tool_call_response(raw: str) -> Optional[List[Any]]:
            """Parse tool call(s) from the model's response.

            Supports both singular ``tool_call`` and plural ``tool_calls``
            (parallel execution). Returns None if the response is a final
            answer (no tool call).
            """
            import re
            # Match either tool_call or tool_calls key
            match = re.search(
                r'\{\s*"schema_version"\s*:\s*"2b\.2-tool".*?"tool_call',
                raw, re.DOTALL,
            )
            if not match:
                return None
            # Extract the full JSON object
            try:
                brace_count = 0
                start = match.start()
                for i in range(start, len(raw)):
                    if raw[i] == "{":
                        brace_count += 1
                    elif raw[i] == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            tool_json = json.loads(raw[start:i + 1])
                            from backend.core.ouroboros.governance.tool_executor import ToolCall

                            def _parse_one(tc_dict: dict) -> Optional[Any]:
                                name = tc_dict.get("name", "")
                                if not name:
                                    return None
                                return ToolCall(
                                    name=name,
                                    arguments=tc_dict.get("arguments", {}),
                                )

                            # Parallel: tool_calls (plural)
                            plural = tool_json.get("tool_calls")
                            if isinstance(plural, list) and plural:
                                valid = [_parse_one(item) for item in plural if isinstance(item, dict)]
                                valid = [c for c in valid if c is not None]
                                return valid if valid else None

                            # Singular: tool_call
                            tc = tool_json.get("tool_call", {})
                            parsed = _parse_one(tc)
                            return [parsed] if parsed is not None else None
            except (json.JSONDecodeError, KeyError):
                pass
            return None

        # Execute with or without tool loop.
        # Complexity routing: skip Venom only for TRIVIAL tasks on DW.
        # Previously also skipped SIMPLE, but those still face the Iron Gate
        # exploration-first check (per CLAUDE.md: "trivial ops bypass").
        # Battle test bt-2026-04-11-085929 traced STANDARD route failures to
        # DW producing one-shot patches on simple ops → Iron Gate rejection
        # (0/2 exploration) → 71s of 120s budget burned → Claude fallback
        # starved to 48.7s → stream cut mid-output at 9KB. Keeping Venom on
        # for simple ops means DW does its own exploration and either passes
        # the gate directly or produces a correctly-shaped candidate for Claude.
        _complexity = getattr(context, "task_complexity", "")
        _ceiling = _DW_COMPLEXITY_MAX_TOKENS.get(_complexity, self._max_tokens)
        # Dynamic budget: complexity ceiling is the floor, scale up by
        # actual target file bytes. Matches what _generate_raw will
        # actually pass to the API (kept in sync so the log is truthful).
        _eff_mt = self._compute_dynamic_max_tokens(context, is_tool_round=False)
        _skip_tools = _complexity == "trivial"
        if _skip_tools:
            if _eff_mt > _ceiling:
                logger.info(
                    "[DoublewordProvider] \u26a1 %s task — skipping Venom tool loop "
                    "(one-shot, max_tokens=%d, dynamic: +%d above %s ceiling)",
                    _complexity or "trivial", _eff_mt, _eff_mt - _ceiling, _complexity or "trivial",
                )
            else:
                logger.info(
                    "[DoublewordProvider] \u26a1 %s task — skipping Venom tool loop "
                    "(one-shot, max_tokens=%d)", _complexity or "trivial", _eff_mt,
                )
        elif _eff_mt != self._max_tokens:
            logger.info(
                "[DoublewordProvider] Complexity=%s → max_tokens=%d (default=%d)",
                _complexity, _eff_mt, self._max_tokens,
            )

        tool_records: tuple = ()
        venom_edits: Tuple[Dict[str, Any], ...] = ()
        raw: str = ""

        # Slice 35 Phase 1 — STAGE_RT_STREAM_CONSUME: record the time
        # from first-chunk arrival to here (Venom loop entry / parse).
        # When tools are skipped, the stream consume window covered the
        # entire chat completion. _s35_stream_t0 was set at first chunk.
        try:
            _s35_dp.record_stage(
                "STAGE_RT_STREAM_CONSUME",
                op_id=_s35_op_id, model_id=_s35_model,
                duration_ms=(
                    (time.monotonic() - _s35_stream_t0) * 1000.0
                ) if "_s35_stream_t0" in dir() else 0.0,
            )
        except Exception:  # noqa: BLE001
            pass
        # Slice 35 Phase 1 — STAGE_RT_VENOM_TOOL_LOOP timer. Strongest
        # suspect for the v25-v29 30-111s inflation: tool_loop rounds
        # against DW often spin multiple times per op.
        _s35_venom_t0 = time.monotonic()
        if self._tool_loop is not None and not _skip_tools:
            deadline_mono = time.monotonic() + max(
                0.0,
                (deadline - datetime.now(tz=timezone.utc)).total_seconds()
                if deadline else 120.0,
            )
            # ── Upgrade 1 (PRD §31.2) Slice 5 wire-up ──
            # Lazy import; bridge returns None when master flag off →
            # per_round_observer=None preserves byte-identical pre-graduation
            # behavior.
            _eb_op_id = getattr(
                context, "operation_id",
                f"dw-rt-{int(time.time())}",
            )
            _eb_observer = None
            try:
                from backend.core.ouroboros.governance.epistemic_budget_provider_bridge import (
                    attach_to_provider_run as _eb_attach,
                )
                _eb_observer = _eb_attach(
                    op_id=_eb_op_id,
                    route=(
                        getattr(context, "provider_route", "")
                        or "standard"
                    ),
                    risk_tier=str(
                        getattr(context, "risk_tier", None) or ""
                    ),
                )
            except Exception:  # noqa: BLE001 — defensive
                _eb_observer = None
            try:
                raw, tool_records_list = await self._tool_loop.run(
                    prompt=prompt,
                    generate_fn=_generate_raw,
                    parse_fn=_parse_tool_call_response,
                    repo=getattr(context, "primary_repo", "jarvis"),
                    op_id=_eb_op_id,
                    deadline=deadline_mono,
                    risk_tier=getattr(context, "risk_tier", None),
                    is_read_only=bool(getattr(context, "is_read_only", False)),
                    per_round_observer=_eb_observer,
                )
            finally:
                try:
                    from backend.core.ouroboros.governance.epistemic_budget_provider_bridge import (
                        close_op as _eb_close,
                    )
                    _eb_close(op_id=_eb_op_id)
                except Exception:  # noqa: BLE001 — defensive
                    pass
            tool_records = tuple(tool_records_list)
            # Venom mutation audit — captured from per-op ToolExecutor at
            # run() exit. Empty when no edit/write/delete tools fired.
            _hist_fn = getattr(self._tool_loop, "get_last_edit_history", None)
            if callable(_hist_fn):
                try:
                    _hist_raw = _hist_fn()
                except Exception:
                    _hist_raw = None
                if isinstance(_hist_raw, list):
                    venom_edits = tuple(_hist_raw)
        else:
            raw = await _generate_raw(prompt)

        elapsed = time.monotonic() - t0
        self._stats.total_batches += 1
        self._stats.total_latency_s += elapsed

        if not raw:
            raise DoublewordInfraError("Empty response from real-time API", status_code=0)

        # Parse the response into GenerationResult
        # Source hash must match what _check_source_drift() computes at GATE:
        # full SHA-256 of the target file's content (not the prompt).
        from backend.core.ouroboros.governance.providers import (
            _extract_json_block,
            _file_source_hash,
        )
        _src_hash = ""
        _src_path_str = ""
        if context.target_files:
            _src_path_str = context.target_files[0]
            _abs = (self._repo_root / _src_path_str).resolve()
            try:
                if _abs.is_file():
                    _src_hash = _file_source_hash(
                        _abs.read_text(encoding="utf-8", errors="replace")
                    )
            except OSError:
                pass
        _extracted = _extract_json_block(raw)
        if _extracted and not _extracted.lstrip().startswith("{"):
            logger.warning(
                "[DoublewordProvider] RT: 397B returned natural language instead of JSON. "
                "Response starts with: %s",
                _extracted[:100].replace("\n", " "),
            )
            raise DoublewordInfraError("Non-JSON response from real-time API", status_code=0)

        # Slice 35 Phase 1 — record STAGE_RT_VENOM_TOOL_LOOP (covers
        # the entire Venom block whether or not the loop ran; when
        # _skip_tools is true this records the streaming-only-time as
        # a near-zero Venom stage). Then begin STAGE_RT_RESPONSE_PARSE.
        try:
            _s35_dp.record_stage(
                "STAGE_RT_VENOM_TOOL_LOOP",
                op_id=_s35_op_id, model_id=_s35_model,
                duration_ms=(time.monotonic() - _s35_venom_t0) * 1000.0,
            )
        except Exception:  # noqa: BLE001
            pass
        _s35_parse_t0 = time.monotonic()

        # Slice 20B — _parse_with_heal wraps _parse_generation_response
        # with an LLM-heal retry on json_parse_error (RT path).
        result = await self._parse_with_heal(
            raw=raw,
            provider_name="doubleword",
            duration_s=elapsed,
            ctx=context,
            source_hash=_src_hash,
            source_path=_src_path_str,
            repo_roots=self._repo_roots or None,
            repo_root=self._repo_root,
        )
        # Slice 35 Phase 1 — record STAGE_RT_RESPONSE_PARSE.
        try:
            _s35_dp.record_stage(
                "STAGE_RT_RESPONSE_PARSE",
                op_id=_s35_op_id, model_id=_s35_model,
                duration_ms=(time.monotonic() - _s35_parse_t0) * 1000.0,
            )
        except Exception:  # noqa: BLE001
            pass
        if _preloaded_files:
            result = dataclasses.replace(
                result, prompt_preloaded_files=tuple(_preloaded_files),
            )

        # Attach token usage and cost from _generate_raw
        if _token_usage["input"] or _token_usage["output"] or total_cost > 0:
            result = dataclasses.replace(
                result,
                total_input_tokens=_token_usage["input"],
                total_output_tokens=_token_usage["output"],
                cost_usd=total_cost,
            )

        logger.info(
            "[DoublewordProvider] RT: %d candidates in %.1fs ($%.4f, %d tool calls, %d+%d tokens)",
            len(result.candidates), elapsed, total_cost, len(tool_records),
            _token_usage["input"], _token_usage["output"],
        )

        # Slice 84 — attach the Venom tool-loop execution records so the Iron
        # Gate exploration gate can count this op's read_file/search_code calls
        # (mirrors ClaudeProvider, providers.py ~5048). Without this the records
        # were dropped and the gate saw 0/2 → rejected every DW candidate as
        # exploration_insufficient even after a full 7-tool-call loop.
        if tool_records:
            result = result.with_tool_records(tool_records)

        # Attach Venom mutation audit (empty when no mutating tools fired).
        if venom_edits:
            result = result.with_venom_edits(venom_edits)

        return result

    # ------------------------------------------------------------------
    # plan() — CandidateProvider protocol (used by ContextExpander)
    # ------------------------------------------------------------------

    async def plan(self, prompt: str, deadline: Any = None) -> str:
        """Send a lightweight planning prompt via the batch API.

        Used by ContextExpander for context expansion rounds. Returns
        raw string response. On failure, raises so the caller can skip
        the expansion round gracefully (ContextExpander expects this).

        The ``ouroboros_plan`` caller is mapped by the Brain Selection
        Topology to Gemma 4 31B (basal ganglia). If topology is disabled
        or the caller is unmapped, falls back to the provider default.
        """
        del deadline  # reserved for future budget-aware planning
        try:
            from backend.core.ouroboros.governance.provider_topology import (
                get_topology,
            )
            _caller_model = get_topology().model_for_caller("ouroboros_plan")
        except Exception:
            _caller_model = None
        result = await self.prompt_only(
            prompt=prompt,
            model=_caller_model,
            caller_id="ouroboros_plan",
            max_tokens=4000,
        )
        return result or ""

    # ------------------------------------------------------------------
    # Batch API stages (all deterministic — Tier 0 protocol)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Slice 38 — Canonical JSONL batch entry composer (single source
    # of truth for /v1/files content framing).
    #
    # Why this is its own function instead of two raw json.dumps()
    # call sites:
    #
    # The DW ``/v1/files`` endpoint validates uploaded files as
    # newline-delimited JSON ("JSONL" / ndjson per RFC 7464). A
    # single JSON object without a terminating ``\n`` is structurally
    # valid JSON but structurally INVALID JSONL. DW's validator
    # (post-2026-05-28 tightening, surfaced via direct support
    # contact peter@doubleword.ai) rejects this with HTTP 500
    # ``Internal server error`` and logs it on their side as
    # "invalid multi part files" — referring to the content of the
    # multipart ``file`` field, not the multipart envelope itself.
    #
    # Pre-Slice 38 the JSONL composition happened at two sites
    # (``submit_batch`` + ``prompt_only``), both calling
    # ``json.dumps(...)`` and both omitting the trailing ``\n``. The
    # v33 capability soak (2026-05-28, session bt-2026-05-28-162729)
    # captured 3/3 HTTP 500s via Slice 37's diagnostic logs — full
    # payload context confirmed the failure mode is structurally
    # identical across payload sizes (4 KB / 18 KB / 33 KB).
    #
    # This helper is now the single source of truth. ``_upload_file``
    # additionally enforces a belt-and-braces guard against any
    # future bypass.
    # ------------------------------------------------------------------

    @staticmethod
    def _compose_jsonl_batch_entry(entry: Dict[str, Any]) -> str:
        """Compose a single batch entry as a properly-terminated
        JSONL line (RFC 7464 / ndjson).

        Args:
            entry: dict with required keys ``custom_id``,
                ``method``, ``url``, ``body``. Additional keys are
                preserved as-is.

        Returns:
            ``json.dumps(entry) + "\\n"`` — RFC 7464 compliant.

        Raises:
            TypeError: if ``entry`` is not a dict.
            ValueError: if any required field is missing or if
                ``body`` is not a dict.
        """
        if not isinstance(entry, dict):
            raise TypeError(
                "_compose_jsonl_batch_entry expects dict, got "
                f"{type(entry).__name__}"
            )
        for _field in ("custom_id", "method", "url", "body"):
            if _field not in entry:
                raise ValueError(
                    "_compose_jsonl_batch_entry: missing required "
                    f"field {_field!r} (entry keys: "
                    f"{list(entry.keys())})"
                )
        if not isinstance(entry.get("body"), dict):
            raise ValueError(
                "_compose_jsonl_batch_entry: 'body' must be dict, "
                f"got {type(entry.get('body')).__name__}"
            )
        # NB: do NOT change serialization params (ensure_ascii /
        # separators) — keep the byte shape identical to pre-Slice-38
        # except for the structural \n terminator. That way any
        # remaining DW upload failures are unambiguously
        # newline-independent.
        return json.dumps(entry) + "\n"

    async def _upload_file(
        self, jsonl_content: str, *, op_id: str = "dw-batch-upload",
    ) -> Optional[str]:
        """Stage 1: Upload JSONL file to Doubleword.

        Slice 2B-ii — ``op_id`` is threaded for per-call Aegis lease.
        Default ``"dw-batch-upload"`` allows existing callers that
        haven't yet plumbed real op_id; production callers pass the
        live OperationContext op_id.

        Slice 37 Phase 1+2 — payload diagnostics + ironclad cleanup:

          * Per-call diagnostic log line BEFORE the POST: payload
            byte size + custom_id + model_id from the JSONL line.
            Lets operators correlate HTTP 500s with payload shape
            without re-running the soak.
          * Pre-flight size guard: ``JARVIS_DW_UPLOAD_MAX_BYTES``
            (default 5 MB) rejects oversized payloads BEFORE the
            HTTP round-trip with a structured error so the orchestrator
            can fail fast + the operator gets a named cause.
          * Full response body on HTTP error (was truncated to 500
            chars; now 2000 chars for diagnostic depth).
          * try/finally: explicit ``_aegis_release_call_lease`` call
            on any failure path (was implicit; now explicit + bounded).
        """
        session = await self._get_session()
        import aiohttp

        # Slice 38 belt-and-braces — guarantee the upload payload is
        # newline-terminated JSONL even if a future caller bypasses
        # ``_compose_jsonl_batch_entry``. A single JSON object without
        # a trailing ``\n`` is valid JSON but invalid JSONL/ndjson; DW
        # rejects it with HTTP 500 ``Internal server error``. Per
        # operator binding: no workarounds — the canonical fix is in
        # the composer at the call sites; this guard is defense in
        # depth and warns loudly so the offending site can be fixed.
        if jsonl_content and not jsonl_content.endswith("\n"):
            logger.warning(
                "[DoublewordProvider] _upload_file: payload missing "
                "trailing newline — auto-appending. Caller "
                "should use _compose_jsonl_batch_entry() to avoid "
                "this fallback (op=%s, len_before=%d)",
                op_id[:16], len(jsonl_content),
            )
            jsonl_content = jsonl_content + "\n"

        # Slice 37 Phase 1 — payload diagnostic. Extract custom_id +
        # model_id from the JSONL line (single line, JSON-parseable)
        # so operators can correlate HTTP 500s with payload shape.
        _payload_bytes = len(jsonl_content.encode("utf-8"))
        _payload_custom_id = "?"
        _payload_model = "?"
        try:
            # Strip trailing \n before json.loads — json.loads accepts
            # it but cleaner to parse the bare object so future
            # multi-line payloads (>1 entry) parse only the first
            # line for diagnostic extraction.
            _first_line = jsonl_content.split("\n", 1)[0]
            _parsed = json.loads(_first_line)
            _payload_custom_id = str(_parsed.get("custom_id", "?"))[:40]
            _payload_body = _parsed.get("body", {}) or {}
            _payload_model = str(_payload_body.get("model", "?"))
        except Exception:  # noqa: BLE001 — diagnostic only
            pass

        # Slice 37 Phase 1 — pre-flight size guard. DW's /v1/files
        # endpoint returns opaque HTTP 500 on oversized payloads;
        # fail-fast with named cause before the round-trip.
        _max_upload_bytes = 5 * 1024 * 1024  # 5 MB default
        try:
            _env_max = os.environ.get("JARVIS_DW_UPLOAD_MAX_BYTES", "").strip()
            if _env_max:
                _max_upload_bytes = max(1024, int(_env_max))
        except (TypeError, ValueError):
            pass
        if _payload_bytes > _max_upload_bytes:
            logger.error(
                "[DoublewordProvider] File upload PRE-FLIGHT REJECTED: "
                "payload %d bytes exceeds JARVIS_DW_UPLOAD_MAX_BYTES=%d "
                "(custom_id=%s model=%s op=%s) — failing fast",
                _payload_bytes, _max_upload_bytes,
                _payload_custom_id, _payload_model, op_id[:16],
            )
            self._last_error_status = 413  # Payload Too Large semantic
            return None

        logger.info(
            "[DoublewordProvider] File upload START: payload=%d bytes "
            "custom_id=%s model=%s op=%s",
            _payload_bytes, _payload_custom_id, _payload_model, op_id[:16],
        )

        data = aiohttp.FormData()
        data.add_field(
            "file",
            io.BytesIO(jsonl_content.encode()),
            filename="batch_input.jsonl",
            content_type="application/jsonl",
        )
        data.add_field("purpose", "batch")

        _rl_t0 = time.monotonic()
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.acquire("doubleword", "files_upload")
            except Exception:
                raise  # Let CircuitBreakerOpen propagate

        # Slice 37 Phase 2 — explicit lease acquire BEFORE try block so
        # the lease handle is visible to the finally cleanup.
        _aegis_lease = None
        try:
            # Slice 31 — Aegis-aware Authorization session bearer
            # injection (closes v24 missing_session_bearer 401 wedge).
            # When Aegis enabled: dw_session_auth_header() returns
            # {"Authorization": "Bearer <session_token>"} — the bearer
            # the Aegis passthrough endpoint requires for /files.
            # When disabled: returns legacy DW API key Bearer header.
            # Slice 2B-ii — per-call Aegis lease layered on top via
            # _aegis_merge_lease_headers (X-JARVIS-Lease).
            _call_headers = await _aegis_dw_session_auth_header()
            _aegis_lease = await _aegis_acquire_call_lease(
                op_id=op_id, route="standard", estimated_cost_usd=0.001,
            )
            _call_headers = _aegis_merge_lease_headers(
                _call_headers, _aegis_lease,
            )
            async with session.post(
                f"{self._base_url}/files",
                data=data,
                headers=_call_headers,
                timeout=self._request_timeout(),
            ) as resp:
                if self._rate_limiter is not None:
                    self._rate_limiter.record("doubleword", "files_upload",
                                              latency_s=time.monotonic() - _rl_t0, status=resp.status)
                if resp.status >= 300:
                    self._last_error_status = resp.status
                    body = await resp.text()
                    # Slice 37 Phase 1 — full body (2000 chars vs 500),
                    # payload size + model so operators can correlate
                    # HTTP errors with what was actually sent.
                    logger.error(
                        "[DoublewordProvider] File upload FAILED: "
                        "status=%d payload=%d bytes custom_id=%s "
                        "model=%s op=%s body=%s",
                        resp.status, _payload_bytes,
                        _payload_custom_id, _payload_model, op_id[:16],
                        body[:2000],
                    )
                    return None
                result = await resp.json()
                return result.get("id")
        except Exception as exc:
            self._last_error_status = 0  # non-HTTP failure
            logger.warning(
                "[DoublewordProvider] File upload exception: %s: %s "
                "(payload=%d bytes custom_id=%s model=%s op=%s)",
                type(exc).__name__, exc, _payload_bytes,
                _payload_custom_id, _payload_model, op_id[:16],
            )
            return None
        finally:
            # Slice 37 Phase 2 — explicit lease release on any path
            # (success, HTTP error, exception). Aegis client treats
            # release as advisory + idempotent; defensive no-op when
            # _aegis_lease is None (e.g. lease acquire failed) OR
            # when Aegis is disabled (release helper handles None).
            if _aegis_lease is not None:
                try:
                    from backend.core.ouroboros.governance.aegis_provider_bridge import (
                        release_call_lease as _aegis_release_call_lease,
                    )
                    await _aegis_release_call_lease(_aegis_lease)
                except (ImportError, AttributeError):
                    pass  # release helper not available — legacy behavior
                except Exception as _rel_exc:  # noqa: BLE001
                    logger.debug(
                        "[DoublewordProvider] lease release suppressed: %s",
                        _rel_exc,
                    )

    async def _create_batch(
        self, input_file_id: str, *, op_id: str = "dw-batch-create", _s181_attempt: int = 0,
    ) -> Optional[str]:
        """Stage 2: Create batch job.

        Slice 2B-ii — ``op_id`` is threaded for per-call Aegis lease.
        Slice 181 — ``_s181_attempt`` threads the Kevlar batch-retry recursion depth.
        """
        session = await self._get_session()
        _rl_t0 = time.monotonic()
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.acquire("doubleword", "batches_create")
            except Exception:
                raise  # Let CircuitBreakerOpen propagate

        # Slice 37 Phase 2 — explicit try/finally cleanup discipline.
        # Lease initialized to None before try so finally never
        # references an unbound name on early-throw paths.
        _aegis_lease = None
        _rate_limiter_recorded = False
        try:
            # Slice 31 — Aegis session bearer for /batches POST.
            _call_auth = await _aegis_dw_session_auth_header()
            _call_auth["Content-Type"] = "application/json"
            # Slice 2B-ii — per-call Aegis lease (None when disabled).
            _aegis_lease = await _aegis_acquire_call_lease(
                op_id=op_id, route="standard", estimated_cost_usd=0.001,
            )
            async with session.post(
                f"{self._base_url}/batches",
                json={
                    "input_file_id": input_file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": _DW_COMPLETION_WINDOW,
                },
                headers=_aegis_merge_lease_headers(
                    _call_auth, _aegis_lease,
                ),
                timeout=self._request_timeout(),
            ) as resp:
                if self._rate_limiter is not None:
                    self._rate_limiter.record("doubleword", "batches_create",
                                              latency_s=time.monotonic() - _rl_t0, status=resp.status)
                    _rate_limiter_recorded = True
                if resp.status >= 300:
                    self._last_error_status = resp.status
                    body = await resp.text()
                    logger.error(
                        "[DoublewordProvider] Batch create failed: %s %s",
                        resp.status, body[:2000],
                    )
                    # Slice 181 — KEVLAR batch net. The batch lane is NOT bulletproof. On a
                    # TRANSIENT 5xx (DW overload), re-submit with exponential backoff instead
                    # of bubbling None to the sentinel (which would exhaust the op). A 4xx
                    # (the 168 param-rejection class) is NOT retried — re-submitting won't help.
                    from backend.core.ouroboros.governance.dw_immortal import (
                        dw_batch_retry_enabled as _s181_br_on,
                        batch_should_retry as _s181_br,
                        dw_batch_max_retries as _s181_br_max,
                        immortal_backoff_s as _s181_br_backoff,
                    )
                    if _s181_br_on() and _s181_br(resp.status, _s181_attempt, max_retries=_s181_br_max()):
                        logger.warning(
                            "[Immortal] batch-create transient %d → backoff + re-submit #%d "
                            "(op=%s; batch lane kept alive)",
                            resp.status, _s181_attempt + 1, op_id,
                        )
                        await asyncio.sleep(_s181_br_backoff(_s181_attempt))
                        return await self._create_batch(
                            input_file_id, op_id=op_id, _s181_attempt=_s181_attempt + 1,
                        )
                    return None
                result = await resp.json()
                batch_id = result.get("id")
                # Register webhook future (Tier 1) if registry is wired
                if batch_id and self._batch_registry is not None:
                    self._batch_registry.register(batch_id)
                return batch_id
        except Exception:
            self._last_error_status = 0
            logger.exception("[DoublewordProvider] Batch create error")
            return None
        finally:
            # Slice 37 Phase 2 — rate-limiter accounting must never
            # drift on early-throw paths. If the response context
            # never entered (e.g., POST raised before headers), record
            # a synthetic status=0 with the elapsed wall clock.
            if not _rate_limiter_recorded and self._rate_limiter is not None:
                try:
                    self._rate_limiter.record(
                        "doubleword", "batches_create",
                        latency_s=time.monotonic() - _rl_t0,
                        status=self._last_error_status or 0,
                    )
                except Exception:
                    pass
            # Forward-looking lease release (no-op until Aegis bridge
            # publishes a release helper). ImportError suppressed so
            # the discipline is in place without coupling to bridge
            # surface that doesn't exist yet.
            if _aegis_lease is not None:
                try:
                    from backend.core.ouroboros.governance.aegis_provider_bridge import (  # noqa: E501
                        release_call_lease as _aegis_release_call_lease,
                    )
                    await _aegis_release_call_lease(_aegis_lease)
                except (ImportError, AttributeError):
                    pass
                except Exception as _rel_exc:
                    logger.debug(
                        "[DoublewordProvider] _create_batch lease "
                        "release suppressed: %s", _rel_exc,
                    )

    # ------------------------------------------------------------------
    # Batch result awaiting: Tier 1 (webhook future) → Tier 2 (adaptive poll)
    # ------------------------------------------------------------------

    async def _await_batch_result(
        self, batch_id: str, *, op_id: str = "dw-batch-await",
    ) -> Optional[str]:
        """Wait for batch result via webhook future or adaptive poll fallback.

        Tier 1: If a ``BatchFutureRegistry`` is wired and the batch has a
        registered future, await it (zero polling — webhook resolves it).

        Tier 2: Adaptive exponential backoff polling with jitter.

        Slice 2B-ii — ``op_id`` is forwarded to ``_adaptive_poll_batch``
        for per-call Aegis lease accounting.
        """
        # Tier 1: webhook-driven (if registry wired)
        registry = getattr(self, "_batch_registry", None)
        if registry is not None:
            try:
                return await registry.wait(batch_id, timeout=_DW_MAX_WAIT_S)
            except asyncio.TimeoutError:
                logger.warning("[DoublewordProvider] Webhook wait timed out for %s", batch_id)
                return None
            except Exception:
                pass  # No future registered or rejected — fall through to Tier 2

        # Tier 2: adaptive backoff poll
        return await self._adaptive_poll_batch(batch_id, op_id=op_id)

    @staticmethod
    def _next_poll_interval(attempt: int, *, network_error: bool = False) -> float:
        """Compute next poll interval with exponential backoff + jitter.

        Slice 35 Phase 2 — operator-tightened calibration based on
        Phase 0 probe empirical p99 of 4-8s. The pre-Slice-35 defaults
        (base 2s, cap 30s) were sized for a much slower baseline; the
        operator's calibration reflects the actual measured DW endpoint
        responsiveness:

          * Normal start: 1.5s (was 2.0s) — aggressive first 3 cycles
            (1.5s, 2.25s, 3.4s) match the 4-8s probe p99
          * Network error start: 8.0s (was 15.0s) — still defensive
            but proportional
          * Multiplier: 1.5x per attempt (unchanged)
          * Cap: 10s (was 30s) — caps at empirical p99 × ~1.5 safety
            factor instead of the prior 30s which exceeded the
            measured ceiling by 3-7x
          * Jitter: +/-25% (unchanged)
          * Floor: 0.5s (unchanged)

        Env knobs (operator-tunable without code change):
          JARVIS_DW_POLL_BASE_S         (default 1.5)
          JARVIS_DW_POLL_NETWORK_BASE_S (default 8.0)
          JARVIS_DW_POLL_MULTIPLIER     (default 1.5)
          JARVIS_DW_POLL_CAP_S          (default 10.0)
          JARVIS_DW_POLL_JITTER_FRACTION (default 0.25)
        """
        import os as _os, random
        try:
            _base_normal = float(_os.environ.get("JARVIS_DW_POLL_BASE_S", "1.5"))
            _base_network = float(_os.environ.get("JARVIS_DW_POLL_NETWORK_BASE_S", "8.0"))
            _mult = float(_os.environ.get("JARVIS_DW_POLL_MULTIPLIER", "1.5"))
            _cap = float(_os.environ.get("JARVIS_DW_POLL_CAP_S", "10.0"))
            _jit_frac = float(_os.environ.get("JARVIS_DW_POLL_JITTER_FRACTION", "0.25"))
        except (TypeError, ValueError):
            _base_normal, _base_network, _mult, _cap, _jit_frac = 1.5, 8.0, 1.5, 10.0, 0.25
        base = _base_network if network_error else _base_normal
        interval = min(base * (_mult ** attempt), _cap)
        jitter = interval * _jit_frac * (2 * random.random() - 1)
        return max(0.5, interval + jitter)

    async def _adaptive_poll_batch(
        self, batch_id: str, *, op_id: str = "dw-batch-poll",
    ) -> Optional[str]:
        """Stage 3: Adaptive backoff polling until batch completes.

        Replaces the fixed 5s poll with exponential backoff + jitter.
        Network-aware: connection errors trigger aggressive backoff.
        Returns output_file_id or None on failure/timeout.

        Slice 2B-ii — ``op_id`` is threaded for per-call Aegis lease
        on each poll request.
        """
        deadline = time.monotonic() + _DW_MAX_WAIT_S
        attempt = 0

        while time.monotonic() < deadline:
            # Slice 37 Phase 2 — per-iteration cleanup discipline.
            # Each poll iteration is its own lease/rate-limiter
            # accounting unit; cleanup MUST be scoped to the iteration
            # so a failure on iteration N doesn't leak resources into
            # iteration N+1.
            _aegis_lease = None
            _rate_limiter_recorded = False
            _rl_t0 = time.monotonic()
            try:
                # Re-acquire session each iteration: if the connector was
                # poisoned by a CancelledError on a prior iteration,
                # _get_session() detects session.closed and creates a fresh one.
                session = await self._get_session()

                if self._rate_limiter is not None:
                    try:
                        await self._rate_limiter.acquire("doubleword", "batches_poll")
                    except Exception:
                        raise  # Let CircuitBreakerOpen propagate

                # Slice 31 — Aegis session bearer for /batches GET poll.
                _call_auth = await _aegis_dw_session_auth_header()
                # Slice 2B-ii — per-call Aegis lease.
                _aegis_lease = await _aegis_acquire_call_lease(
                    op_id=op_id, route="background",
                    estimated_cost_usd=0.0001,
                )
                async with session.get(
                    f"{self._base_url}/batches/{batch_id}",
                    headers=_aegis_merge_lease_headers(_call_auth, _aegis_lease),
                    timeout=self._request_timeout(),
                ) as resp:
                    if self._rate_limiter is not None:
                        self._rate_limiter.record("doubleword", "batches_poll",
                                                  latency_s=time.monotonic() - _rl_t0, status=resp.status)
                        _rate_limiter_recorded = True
                    if resp.status >= 300:
                        self._last_error_status = resp.status
                        logger.warning("[DoublewordProvider] Poll error: %s", resp.status)
                        await asyncio.sleep(self._next_poll_interval(attempt))
                        attempt += 1
                        continue
                    data = await resp.json()
                    status = data.get("status", "unknown")

                    if status == "completed":
                        output_file_id = data.get("output_file_id")
                        logger.info(
                            "[DoublewordProvider] Batch %s completed (output=%s)",
                            batch_id, output_file_id,
                        )
                        return output_file_id
                    elif status in ("failed", "expired", "cancelled"):
                        logger.error(
                            "[DoublewordProvider] Batch %s terminal: %s",
                            batch_id, status,
                        )
                        return None
                    # Still in_progress — adaptive backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _is_network = "connect" in str(exc).lower() or "timeout" in str(exc).lower()
                logger.debug(
                    "[DoublewordProvider] Poll attempt %d: %s (network=%s)",
                    attempt, type(exc).__name__, _is_network,
                )
                if _is_network:
                    attempt = max(attempt, 3)  # Jump to higher backoff for network errors
            finally:
                # Slice 37 Phase 2 — per-iteration cleanup.
                if (
                    not _rate_limiter_recorded
                    and self._rate_limiter is not None
                ):
                    try:
                        self._rate_limiter.record(
                            "doubleword", "batches_poll",
                            latency_s=time.monotonic() - _rl_t0,
                            status=self._last_error_status or 0,
                        )
                    except Exception:
                        pass
                if _aegis_lease is not None:
                    try:
                        from backend.core.ouroboros.governance.aegis_provider_bridge import (  # noqa: E501
                            release_call_lease as _aegis_release_call_lease,
                        )
                        await _aegis_release_call_lease(_aegis_lease)
                    except (ImportError, AttributeError):
                        pass
                    except Exception as _rel_exc:
                        logger.debug(
                            "[DoublewordProvider] _adaptive_poll_batch "
                            "lease release suppressed: %s", _rel_exc,
                        )

            await asyncio.sleep(self._next_poll_interval(attempt))
            attempt += 1

        logger.error("[DoublewordProvider] Batch %s timed out after %ds", batch_id, _DW_MAX_WAIT_S)
        return None

    async def _retrieve_result(
        self, output_file_id: str, operation_id: str
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Stage 4: Retrieve and parse batch output. Returns (content, usage).

        Slice 2B-ii — ``operation_id`` doubles as the Aegis lease op_id
        (per-call lease binds the retrieve to the same op for cap
        accounting).
        """
        session = await self._get_session()
        _rl_t0 = time.monotonic()
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.acquire("doubleword", "batches_retrieve")
            except Exception:
                raise  # Let CircuitBreakerOpen propagate

        # Slice 37 Phase 2 — explicit cleanup discipline mirror.
        _aegis_lease = None
        _rate_limiter_recorded = False
        try:
            # Slice 31 — Aegis session bearer for /files retrieve.
            _call_auth = await _aegis_dw_session_auth_header()
            # Slice 2B-ii — per-call Aegis lease bound to operation_id.
            _aegis_lease = await _aegis_acquire_call_lease(
                op_id=operation_id, route="standard",
                estimated_cost_usd=0.001,
            )
            async with session.get(
                f"{self._base_url}/files/{output_file_id}/content",
                headers=_aegis_merge_lease_headers(_call_auth, _aegis_lease),
                timeout=self._request_timeout(),
            ) as resp:
                if self._rate_limiter is not None:
                    self._rate_limiter.record("doubleword", "batches_retrieve",
                                              latency_s=time.monotonic() - _rl_t0, status=resp.status)
                    _rate_limiter_recorded = True
                if resp.status >= 300:
                    self._last_error_status = resp.status
                    logger.error("[DoublewordProvider] Retrieve failed: %s", resp.status)
                    return ("", None)
                raw = await resp.text()

            # Parse JSONL — find the line matching our operation_id
            for line in raw.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("custom_id") == operation_id:
                        response = entry.get("response", {})
                        body = response.get("body", {})
                        choices = body.get("choices", [])
                        usage = body.get("usage")

                        if choices:
                            message = choices[0].get("message", {})
                            # Slice 54 — reasoning-model-aware extraction.
                            # content first; then the CORRECT reasoning fields
                            # (`reasoning` / `reasoning_details[].text`). The
                            # prior code used `reasoning_content`, which these
                            # models never emit, so the fallback always read
                            # empty. With reasoning_effort="none" content is
                            # populated directly and the fallback is inert.
                            content = message.get("content", "")
                            if not content:
                                content = _extract_completion_text(message)
                                if content:
                                    logger.info(
                                        "[DoublewordProvider] content empty — used "
                                        "reasoning field fallback for op=%s",
                                        operation_id,
                                    )
                            logger.debug(
                                "[DoublewordProvider] Response keys: %s, content_len=%d",
                                list(message.keys()), len(content),
                            )
                            return (content, usage)
                except json.JSONDecodeError:
                    continue

            logger.warning(
                "[DoublewordProvider] No matching result for operation_id=%s",
                operation_id,
            )
            return ("", None)

        except Exception:
            logger.exception("[DoublewordProvider] Retrieve error")
            return ("", None)
        finally:
            # Slice 37 Phase 2 — rate-limiter and lease cleanup mirror.
            if not _rate_limiter_recorded and self._rate_limiter is not None:
                try:
                    self._rate_limiter.record(
                        "doubleword", "batches_retrieve",
                        latency_s=time.monotonic() - _rl_t0,
                        status=self._last_error_status or 0,
                    )
                except Exception:
                    pass
            if _aegis_lease is not None:
                try:
                    from backend.core.ouroboros.governance.aegis_provider_bridge import (  # noqa: E501
                        release_call_lease as _aegis_release_call_lease,
                    )
                    await _aegis_release_call_lease(_aegis_lease)
                except (ImportError, AttributeError):
                    pass
                except Exception as _rel_exc:
                    logger.debug(
                        "[DoublewordProvider] _retrieve_result lease "
                        "release suppressed: %s", _rel_exc,
                    )

    # ------------------------------------------------------------------
    # Governance-free inference: prompt_only()
    # ------------------------------------------------------------------

    async def prompt_only(
        self,
        prompt: str,
        model: Optional[str] = None,
        caller_id: str = "ouroboros_cognition",
        response_format: Optional[Dict] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Direct inference via Doubleword batch API without OperationContext.

        Intended for cognition layers (Synthesis Engine, Architecture Agent)
        that need 397B inference without governance pipeline overhead.

        Runs the full 4-stage batch cycle synchronously (upload → create →
        poll → retrieve) and returns the raw text response.

        Parameters
        ----------
        prompt:
            User prompt text. A default system message is applied.
        model:
            Override the model slug. Defaults to self._model.
        caller_id:
            Identifier embedded in the JSONL custom_id for traceability.
        response_format:
            Optional response_format dict (e.g. ``{"type": "json_object"}``)
            passed directly to the chat completions body.
        max_tokens:
            Token cap. Defaults to self._max_tokens.

        Returns
        -------
        str
            The assistant message content from choices[0].message.content.
            Returns an empty string on failure (caller handles fallback).

        Raises
        ------
        ValueError
            If DOUBLEWORD_API_KEY is not configured.
        """
        # Slice 27 Phase 2 — Aegis-unified auth bridge.
        # ``self._api_key`` may be empty (post-Aegis env_scrub at boot) —
        # Aegis is the secure credential broker that injects the real
        # DOUBLEWORD_API_KEY server-side at the daemon's forwarding
        # handler. The presence of either credential source is valid:
        #
        #   * Aegis enabled       → Aegis daemon injects the key;
        #                            session uses `dw_authorization_header()`
        #                            which returns {} (no client-side bearer).
        #   * Aegis disabled      → legacy path; self._api_key must be set.
        #
        # Pre-Slice-27 this check raised even when Aegis was the
        # active credential broker, silently breaking all prompt_only
        # callers (SemanticTriage, IntentDiscovery, Slice 20B json_healer)
        # post-scrub. v20 forensic (bt-2026-05-27-011121) caught this:
        # "[SemanticTriage] DOUBLEWORD_API_KEY is not set — cannot call
        # prompt_only() — proceeding without triage".
        try:
            from backend.core.ouroboros.aegis.client import (
                is_enabled as _aegis_enabled,
            )
            _aegis_active = _aegis_enabled()
        except Exception:  # noqa: BLE001 — defensive
            _aegis_active = False
        if not self._api_key and not _aegis_active:
            raise ValueError(
                "DOUBLEWORD_API_KEY is not set AND Aegis is not "
                "enabled — cannot call prompt_only() without a "
                "credential source"
            )
        self._check_budget()

        await self._get_session()

        effective_model = model or self._model
        effective_max_tokens = max_tokens if max_tokens is not None else self._max_tokens
        custom_id = f"prompt_only_{caller_id}"

        body: Dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a senior AI reasoning engine for the JARVIS Trinity "
                        "ecosystem. Think step by step and return well-structured output."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": effective_max_tokens,
            "temperature": _DW_TEMPERATURE,
            # Slice 54 — reasoning control (see _reasoning_request_params).
            **_reasoning_request_params(model=effective_model),
        }
        if response_format is not None:
            body["response_format"] = response_format

        # Slice 38 — canonical JSONL composition via single helper.
        # Same fix as submit_batch — trailing ``\n`` mandatory for
        # DW ``/v1/files`` acceptance.
        jsonl_line = self._compose_jsonl_batch_entry({
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        })

        t0 = time.monotonic()

        try:
            # Slice 2B-ii — thread caller-derived synthetic op_id for
            # per-call Aegis lease. ``prompt_only`` is the no-context
            # fast-path; the synthetic id is purpose-tagged.
            _po_op_id = f"dw-prompt-only:{caller_id}:{custom_id}"
            file_id = await self._upload_file(jsonl_line, op_id=_po_op_id)
            if not file_id:
                logger.warning("[DoublewordProvider] prompt_only: file upload failed (caller=%s)", caller_id)
                return ""

            batch_id = await self._create_batch(file_id, op_id=_po_op_id)
            if not batch_id:
                logger.warning("[DoublewordProvider] prompt_only: batch creation failed (caller=%s)", caller_id)
                return ""

            logger.info(
                "[DoublewordProvider] prompt_only batch %s submitted (model=%s, caller=%s)",
                batch_id, effective_model, caller_id,
            )

            output_file_id = await self._await_batch_result(
                batch_id, op_id=_po_op_id,
            )
            if not output_file_id:
                self._stats.failed_batches += 1
                logger.warning(
                    "[DoublewordProvider] prompt_only: batch %s failed or timed out (caller=%s)",
                    batch_id, caller_id,
                )
                return ""

            content, usage = await self._retrieve_result(output_file_id, custom_id)

            elapsed = time.monotonic() - t0
            self._stats.total_batches += 1
            self._stats.total_latency_s += elapsed

            if usage:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                self._stats.total_input_tokens += input_tokens
                self._stats.total_output_tokens += output_tokens
                cost = (
                    input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
                    + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
                )
                self._stats.total_cost_usd += cost
                self._record_cost(cost)

            if not content:
                self._stats.empty_content_retries += 1
                logger.warning(
                    "[DoublewordProvider] prompt_only: empty content returned (caller=%s, batch=%s)",
                    caller_id, batch_id,
                )
                return ""

            logger.info(
                "[DoublewordProvider] prompt_only complete: %.1fs, %d chars (caller=%s)",
                elapsed, len(content), caller_id,
            )
            return content

        except asyncio.CancelledError:
            raise
        except Exception:
            self._stats.failed_batches += 1
            logger.exception(
                "[DoublewordProvider] prompt_only unexpected error (caller=%s)", caller_id
            )
            return ""

    # ------------------------------------------------------------------
    # Functions-not-Agents path: complete_sync()
    # ------------------------------------------------------------------

    async def complete_sync(
        self,
        prompt: str,
        *,
        system_prompt: str,
        caller_id: str,
        model: Optional[str] = None,
        max_tokens: int = 512,
        timeout_s: float = 10.0,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        enable_thinking: Optional[bool] = None,
    ) -> CompleteSyncResult:
        """Non-streaming, short-output, caller-timed synchronous completion.

        This is the **Functions-not-Agents** code path. It bypasses the SSE
        streaming endpoint entirely and instead hits ``/v1/chat/completions``
        with ``stream=false``, awaiting a single JSON body. It is the single
        entry point for structured-function callers (CompactionCaller,
        BlastRadius, FailureClustering, DreamSeed) that need short, bounded,
        schema-validated output without the agent cascade's tool loops.

        Calibration context: bt-2026-04-14-182446 and bt-2026-04-14-203740
        established that DW's SSE streaming endpoint stalls post-accept
        across Qwen 397B and Gemma 4 31B. This method avoids the stall
        surface by never opening an SSE stream. It is the load-bearing
        primitive of the reseated DW topology (Manifesto §5).

        The caller enforces the timeout via ``asyncio.wait_for()``. If the
        request exceeds ``timeout_s``, ``asyncio.TimeoutError`` propagates
        to the caller, which is expected to handle circuit-breaker logic
        and fall back to its deterministic path.

        Parameters
        ----------
        prompt:
            User prompt text. Passed verbatim as the user message.
        system_prompt:
            Caller-specific system prompt. Required — no default. Every
            caller is expected to own its system prompt so the Functions
            path has no implicit shared instructions.
        caller_id:
            Identifier used in log messages and telemetry. Short string
            like ``"compaction"``, ``"blast_radius"``, ``"dream_seed"``.
        model:
            Override the model slug. Defaults to ``self._model``. The
            reseated topology expects callers to pass the model from
            ``provider_topology.get_topology().model_for_caller(caller_id)``
            so the yaml remains the single source of truth.
        max_tokens:
            Output token ceiling. Defaults to 512 — the Functions path is
            for short structured output, not long-form generation.
        timeout_s:
            Hard caller-supplied timeout enforced via ``asyncio.wait_for``.
            Raises ``asyncio.TimeoutError`` on expiry.
        response_format:
            Optional OpenAI-style response_format dict. Typical usage:
            ``{"type": "json_object"}`` for JSON-mode output.
        temperature:
            Override sampling temperature. Defaults to ``_DW_TEMPERATURE``.
        enable_thinking:
            Optional override for the per-request ``chat_template_kwargs``
            ``enable_thinking`` flag. ``None`` (default) preserves the
            legacy behavior of disabling DW's reasoning mode for
            short structured-function callers (CompactionCaller,
            BlastRadius, FailureClustering, DreamSeed). The heavy
            codegen non-streaming lane passes ``True`` to unlock
            DW reasoning over a stream-free transport. Existing
            callers that omit this parameter remain byte-identical
            to pre-extension behavior.

        Returns
        -------
        CompleteSyncResult
            Structured result with content, token usage, cost, latency.

        Raises
        ------
        ValueError
            If DOUBLEWORD_API_KEY is not configured.
        asyncio.TimeoutError
            If the request exceeds ``timeout_s``.
        DoublewordInfraError
            On HTTP errors, empty choices, or cost-budget violations.
        """
        if not self._api_key:
            raise ValueError(
                "DOUBLEWORD_API_KEY is not set — cannot call complete_sync()"
            )
        self._check_budget()

        effective_model = model or self._model
        effective_temperature = (
            temperature if temperature is not None else _DW_TEMPERATURE
        )

        # Heavy-codegen non-streaming lane needs DW reasoning; legacy
        # Functions callers (CompactionCaller, BlastRadius, etc.) call
        # with enable_thinking=None and get the byte-identical legacy
        # behavior (False). The new heavy lane explicitly passes True.
        _effective_enable_thinking: bool = (
            bool(enable_thinking) if enable_thinking is not None
            else False
        )
        body: Dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": effective_temperature,
            "stream": False,
            "chat_template_kwargs": {
                "enable_thinking": _effective_enable_thinking,
            },
        }
        if response_format is not None:
            body["response_format"] = response_format

        session = await self._get_session()
        t0 = time.monotonic()

        async def _do_request() -> Tuple[str, int, int]:
            # Slice 31 — Aegis session bearer for sync complete().
            _call_auth = await _aegis_dw_session_auth_header()
            _call_auth["Content-Type"] = "application/json"
            # Slice 2B-ii — per-call Aegis lease bound to caller_id.
            _aegis_lease = await _aegis_acquire_call_lease(
                op_id=f"dw-complete-sync:{caller_id}",
                route="standard",
                estimated_cost_usd=0.005,
            )
            async with session.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers=_aegis_merge_lease_headers(
                    _call_auth, _aegis_lease,
                ),
                timeout=self._request_timeout(),
            ) as resp:
                if resp.status >= 300:
                    self._last_error_status = resp.status
                    err_body = await resp.text()
                    raise DoublewordInfraError(
                        f"complete_sync[{caller_id}] HTTP {resp.status}: {err_body[:200]}",
                        status_code=resp.status,
                    )
                data = await resp.json()
                choices = data.get("choices", [])
                if not choices:
                    raise DoublewordInfraError(
                        f"complete_sync[{caller_id}] no choices in response",
                        status_code=0,
                    )
                message = choices[0].get("message", {}) or {}
                _content = message.get("content", "") or ""
                usage = data.get("usage", {}) or {}
                _input_tokens = int(usage.get("prompt_tokens", 0) or 0)
                _output_tokens = int(usage.get("completion_tokens", 0) or 0)
                return _content, _input_tokens, _output_tokens

        try:
            content, input_tokens, output_tokens = await asyncio.wait_for(
                _do_request(), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            self._stats.failed_batches += 1
            logger.warning(
                "[DoublewordProvider] complete_sync[%s] timeout after %.1fs (model=%s)",
                caller_id, timeout_s, effective_model,
            )
            raise

        elapsed = time.monotonic() - t0
        cost = (
            input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
            + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
        )
        self._stats.total_batches += 1
        self._stats.total_latency_s += elapsed
        self._stats.total_input_tokens += input_tokens
        self._stats.total_output_tokens += output_tokens
        self._stats.total_cost_usd += cost
        self._record_cost(cost)

        if not content:
            self._stats.empty_content_retries += 1
            logger.warning(
                "[DoublewordProvider] complete_sync[%s] empty content (model=%s, %.2fs)",
                caller_id, effective_model, elapsed,
            )

        logger.info(
            "[DoublewordProvider] complete_sync[%s] ok: %.2fs, %d chars, $%.5f (model=%s)",
            caller_id, elapsed, len(content), cost, effective_model,
        )

        return CompleteSyncResult(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_s=elapsed,
            model=effective_model,
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _empty_result(self, t0: float, reason: str) -> GenerationResult:
        """Return empty GenerationResult with timing."""
        logger.debug("[DoublewordProvider] Empty result: %s", reason)
        return GenerationResult(
            candidates=(),
            provider_name="doubleword",
            generation_duration_s=time.monotonic() - t0,
        )

    async def health_probe(self) -> bool:
        """Quick health check — verify API key works and models endpoint responds.

        Slice 2B-ii — infrastructure-tier call with no upstream op
        context: a synthetic ``dw-health-probe`` op_id is used for
        the per-call Aegis lease. Aegis daemon's cap accounting
        treats these as low-cost system overhead.
        """
        if not self.is_available:
            return False
        try:
            session = await self._get_session()
            # Slice 31 — Aegis session bearer for /models probe.
            _call_auth = await _aegis_dw_session_auth_header()
            # Slice 2B-ii — per-call Aegis lease (synthetic infra op_id).
            _aegis_lease = await _aegis_acquire_call_lease(
                op_id="dw-health-probe",
                route="background",
                estimated_cost_usd=0.0,
            )
            async with session.get(
                f"{self._base_url}/models",
                headers=_aegis_merge_lease_headers(_call_auth, _aegis_lease),
                timeout=self._request_timeout(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Return cumulative stats for observability."""
        return {
            "provider": "doubleword",
            "model": self._model,
            "total_batches": self._stats.total_batches,
            "failed_batches": self._stats.failed_batches,
            "total_input_tokens": self._stats.total_input_tokens,
            "total_output_tokens": self._stats.total_output_tokens,
            "total_cost_usd": round(self._stats.total_cost_usd, 6),
            "total_latency_s": round(self._stats.total_latency_s, 1),
            "empty_content_retries": self._stats.empty_content_retries,
            "available": self.is_available,
        }

    async def _parse_with_heal(
        self,
        *,
        raw: str,
        provider_name: str,
        duration_s: float,
        ctx,
        source_hash: str,
        source_path: str,
        repo_roots=None,
        repo_root=None,
    ):
        """Slice 20B — call ``_parse_generation_response`` with LLM-heal retry.

        Wraps the sync parser in :func:`json_healer.heal_and_retry_parse`,
        binding ``self.prompt_only`` as the heal call (zero-governance
        Qwen3.5-35B fast path) and propagating ``op_id`` / provider
        identity for the audit ledger.

        Master flag ``JARVIS_JSON_HEAL_LLM_ENABLED`` gates the heal
        attempt — when off, behavior is byte-identical to a direct
        ``_parse_generation_response`` call (the helper raises the
        original ``json_parse_error`` without invoking the heal call,
        and writes NO audit row).

        Slice 20C — resolves the effective model_id via the existing
        ``_resolve_effective_model`` path (which reads the dispatcher's
        ContextVar override stamped at ``candidate_generator.py:2583``).
        The model_id is passed to ``heal_and_retry_parse`` so that on
        unrepairable parse failure the drift tracker can record
        "model X produced unrepairable JSON on op Y" — driving the next
        dispatch's rotation to a sibling fleet model.
        """
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )
        from backend.core.ouroboros.governance.json_healer import (
            heal_and_retry_parse,
        )

        def _do_parse(r: str):
            return _parse_generation_response(
                raw=r,
                provider_name=provider_name,
                duration_s=duration_s,
                ctx=ctx,
                source_hash=source_hash,
                source_path=source_path,
                repo_roots=repo_roots,
                repo_root=repo_root,
            )

        # Slice 20C — best-effort model_id resolution. The dispatcher's
        # _set_override(model_id) ContextVar is what _resolve_effective_model
        # reads; if no override is active (legacy single-model path),
        # _resolve_effective_model returns self._model. Empty string is
        # the "skip drift recording" signal honored by heal_and_retry_parse.
        try:
            _model_id_for_drift = self._resolve_effective_model(ctx) or ""
        except Exception:  # noqa: BLE001 — model resolution must not block parse
            _model_id_for_drift = ""

        return await heal_and_retry_parse(
            raw=raw,
            parse_fn=_do_parse,
            heal_call=self.prompt_only,
            op_id=getattr(ctx, "op_id", "") or "",
            provider_name=provider_name,
            model_id=_model_id_for_drift,
        )

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
