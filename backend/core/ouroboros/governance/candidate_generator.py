"""
Candidate Generator & Failback State Machine
=============================================

Routes code generation requests to a primary provider (GCP J-Prime) or a
fallback provider (local model).  The :class:`FailbackStateMachine` prevents
flapping by requiring N consecutive health probes over a dwell period before
restoring the primary provider.

Key Design Decisions
--------------------

- **Asymmetric transitions**: failover is immediate (one failure triggers
  switch to fallback), but failback requires ``required_probes`` consecutive
  health checks spanning at least ``dwell_time_s`` seconds.
- **Concurrency quotas**: separate :class:`asyncio.Semaphore` instances for
  primary and fallback, preventing thundering-herd overload.
- **Deadline propagation**: every call computes remaining time from the
  caller-supplied deadline and applies it as an ``asyncio.wait_for`` timeout.
- **QUEUE_ONLY**: when both providers are down, the generator raises
  immediately rather than blocking -- the orchestrator is expected to queue
  the operation for later retry.

State Diagram
-------------

.. code-block:: text

    PRIMARY_READY ---[primary_failure]---> FALLBACK_ACTIVE
         ^                                      |     |
         |                                      |     +--[fallback_failure]--> QUEUE_ONLY
         |                              [probe_success]
         |                                      |
         |                                      v
         +---[N probes + dwell met]--- PRIMARY_DEGRADED
                                          |
                                  [probe_failure]
                                          |
                                          v
                                   FALLBACK_ACTIVE

Components
----------

- :class:`CandidateProvider` -- runtime-checkable protocol for generation backends
- :class:`FailbackState` -- 4-state enum
- :class:`FailbackStateMachine` -- asymmetric failover/failback logic
- :class:`CandidateGenerator` -- orchestration layer with concurrency and deadline
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Optional, Protocol, Tuple, runtime_checkable

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)
from backend.core.ouroboros.governance.dw_latency_tracker import (
    DwLatencyTracker,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deadline budget allocation — deterministic split (Manifesto §5)
# ---------------------------------------------------------------------------
# The skeleton (deterministic budget) decides how time is partitioned across
# tiers; the nervous system (agentic providers) works within its allocation.
# This prevents any single tier from starving downstream fallbacks.
#
# Tier 0 (DoubleWord batch): gets a capped fraction, MUST leave Tier 1 reserve.
# Tier 1 primary (J-Prime): gets a capped fraction, MUST leave fallback reserve.
# Tier 1 fallback (Claude): gets whatever remains — guaranteed minimum.

_TIER0_BUDGET_FRACTION = float(os.environ.get("OUROBOROS_TIER0_BUDGET_FRACTION", "0.65"))
_TIER0_MAX_WAIT_S = float(os.environ.get("OUROBOROS_TIER0_MAX_WAIT_S", "90"))
_TIER1_MIN_RESERVE_S = float(os.environ.get("OUROBOROS_TIER1_MIN_RESERVE_S", "25"))


# ---------------------------------------------------------------------------
# Slice 238 — cascade-to-dead-Claude guard (layer 8).
#
# The sentinel's ``fallback_tolerance=cascade_to_claude`` path invoked
# ``_call_fallback`` (the Claude lane) with NO breaker consult — so a DW
# transient hiccup poisoned the op via the credit-dead Claude lane
# (terminal_quota). The PRIMARY Claude lane already gates on the economic
# breaker; this makes the cascade read the SAME source-of-truth (the read-only
# ``doubleword_provider._claude_breaker_open`` predicate, no probe side-effect)
# so when Claude is economically/transport OPEN the cascade is suppressed and the
# op routes to the existing immortal DW-retry / clean-degrade branch instead.
# ---------------------------------------------------------------------------


def cascade_breaker_consult_enabled() -> bool:
    """Master switch for the Slice-238 cascade breaker consult. Default TRUE —
    failure-path-only (only changes behavior when the Claude breaker is OPEN,
    which is exactly when cascading to it is wrong); breaker-CLOSED is byte-
    identical to the legacy cascade. Kill switch is pure rollback. NEVER raises."""
    raw = (os.environ.get("JARVIS_CASCADE_BREAKER_CONSULT_ENABLED", "true") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _latency_quarantine_enabled() -> bool:
    """Master for the cold-storage latency quarantine at the DW selector seam.
    Default TRUE — failure-path-only: it only skips a model the TtftObserver has
    flagged as COLD_STORAGE (a real TTFT spike) AND only when another candidate
    remains. When the observer is absent/disabled it short-circuits, so this is a
    free no-op unless there's positive latency evidence. =0 reverts to the legacy
    (entitlement-breaker-only) selection. NEVER raises."""
    raw = (os.environ.get("JARVIS_DW_LATENCY_QUARANTINE_ENABLED", "true") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def autarky_backoff_wait_enabled() -> bool:
    """Master switch for the Sovereign Autarky Backoff-Wait (2026-06-20).

    Default TRUE — failure-path-only + autarky-only: it ONLY changes behavior
    when (a) the sole-provider primary is in transient backoff AND (b) there is
    NO fallback configured (DW-only mode). In every other state (fallback
    present, primary healthy) it is byte-identical. Without it, a transient DW
    TIMEOUT routes a STANDARD op to the absent Claude fallback and fails it with
    ``fallback_skipped`` despite ample remaining budget to wait out the short
    backoff and re-attempt the sole provider. Kill switch = pure rollback to the
    legacy degrade-immediately path. NEVER raises."""
    raw = (os.environ.get("JARVIS_AUTARKY_BACKOFF_WAIT_ENABLED", "true") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _autarky_backoff_max_wait_s() -> float:
    """Hard cap on a SINGLE autarky backoff-wait (don't sleep absurdly long even
    if budget allows). Env-tunable; defensive default 90s. NEVER raises."""
    raw = (os.environ.get("JARVIS_AUTARKY_BACKOFF_MAX_WAIT_S", "") or "").strip()
    try:
        v = float(raw) if raw else 90.0
        return v if v > 0 else 90.0
    except (TypeError, ValueError):
        return 90.0


def _autarky_retry_margin_s() -> float:
    """Budget that MUST remain AFTER the wait to attempt the primary call — so we
    never burn the whole budget sleeping and then have nothing left to generate.
    Env-tunable; defensive default 30s. NEVER raises."""
    raw = (os.environ.get("JARVIS_AUTARKY_RETRY_MARGIN_S", "") or "").strip()
    try:
        v = float(raw) if raw else 30.0
        return v if v > 0 else 30.0
    except (TypeError, ValueError):
        return 30.0


def autarky_should_wait_and_retry(
    *,
    has_fallback: bool,
    enabled: bool,
    eta_s: float,
    remaining_s: float,
    max_wait_s: float,
    margin_s: float,
) -> "Optional[float]":
    """Pure decision: in DW-only autarky, should we WAIT out the sole provider's
    transient backoff and re-attempt the primary (instead of routing to an absent
    fallback)? Returns the bounded wait in seconds when yes, else ``None``.

    Yes IFF: enabled AND no fallback AND a real positive backoff exists AND the
    bounded wait + the post-wait call margin fit inside the remaining budget.
    Pure + total — NEVER raises; trivially unit-testable."""
    if not enabled or has_fallback:
        return None
    try:
        if eta_s <= 0 or remaining_s <= 0:
            return None
        wait = min(float(eta_s), float(max_wait_s))
        if wait <= 0:
            return None
        if wait + float(margin_s) < float(remaining_s):
            return wait
        return None
    except (TypeError, ValueError):
        return None


def should_cascade_to_claude(
    *, has_fallback: bool, claude_breaker_open: bool, enabled: bool,
) -> bool:
    """Pure decision: should the sentinel actually cascade to the Claude fallback
    after DW exhaustion? Cascade ONLY when a fallback is configured AND it is not
    suppressed by an OPEN economic breaker. When *enabled* and the breaker is OPEN
    (Claude known-dead), suppress the cascade (→ caller routes to the immortal
    DW-retry / degrade branch). When *enabled* is False (kill switch), legacy
    behavior: cascade iff a fallback exists. No env / breaker reads here — the
    caller injects both — so this stays deterministic + unit-testable. Pure."""
    if not has_fallback:
        return False
    if enabled and claude_breaker_open:
        return False  # Claude lane is dead — do not poison the op via it
    return True

# Complexity-aware multipliers applied on top of _TIER0_BUDGET_FRACTION.
# Higher complexity => more time for DW 397B code generation.
_TIER0_COMPLEXITY_MULTIPLIER: Dict[str, float] = {
    "trivial": 0.31,           # 0.65 * 0.31 ≈ 0.20 → ~24s DW (one-file edits, RT SSE fast enough)
    "simple": 0.50,            # 0.65 * 0.50 ≈ 0.33 → ~39s DW, ~81s Claude
    "moderate": 1.077,         # 0.65 * 1.077 ≈ 0.70
    "standard": 1.077,         # alias for moderate
    "complex": 1.231,          # 0.65 * 1.231 ≈ 0.80
    "heavy_code": 1.231,       # alias for complex
}
_PRIMARY_BUDGET_FRACTION = float(os.environ.get("OUROBOROS_PRIMARY_BUDGET_FRACTION", "0.65"))
_FALLBACK_MIN_RESERVE_S = float(os.environ.get("OUROBOROS_FALLBACK_MIN_RESERVE_S", "30"))

# Tier 3 Reflex (Manifesto §5): aggressive hard cap on DoubleWord 397B
# across ALL cost-optimized call paths. If DW stalls on stream rendering
# or token generation for longer than this cap, the deterministic router
# severs the thread and cascades to the high-reliability frontier model
# (Claude). Single source of truth for the hard cap; both the Tier-0-first
# path (_compute_tier0_budget) and the Primary-first path
# (_compute_primary_budget) reference it via the aliases below.
#
# Problem this fixes — F1 Slice 4 S3 (bt-2026-04-24-204029) + S4
# (bt-2026-04-24-213248):
#   S3: primary held semaphore for up to 153.76s (DW SSE stream stall),
#       exceeding the then-current fraction-based cap of ~143s.
#   S4: patch landed but didn't fire — DW was promoted to BOTH Tier 0 AND
#       primary (J-Prime unhealthy), which routes via the Tier 0 fast path
#       (_compute_tier0_budget, max_wait=_TIER0_MAX_WAIT_S=90s), NOT via
#       _call_primary where the S3 patch lived. The _PRIMARY_MAX_TIMEOUT_S
#       cap was inert for this configuration because _call_primary was
#       never invoked. Same 153s DW semaphore hold pattern repeated.
#
# Manifesto §5 Tier 3 quote (verbatim):
#   "If a cost-optimized inference node (e.g., DW 397B) exhausts its
#    temporal budget without returning a valid execution plan, the
#    deterministic router autonomously severs the thread and triggers
#    an instant cascade to a high-reliability frontier model."
#
# This cap is a HARD TIME BOX applied at TWO sites:
#   1. _compute_primary_budget — for the "call primary first" path
#      (FSM PRIMARY_READY / PRIMARY_DEGRADED with J-Prime as primary).
#   2. _compute_tier0_budget — for the "Tier 0 fast-path first" (the
#      Manifesto §5 default cascade; DW-as-Tier-0 always tries here).
#
# Fraction + route-specific max_wait logic stays inside each function as
# inner floors; this cap is the strict outer ceiling that enforces the
# reflex regardless of route.
#
# Default 30s is calibrated from S3+S4 evidence: DW first_token_ms=1898
# was observed on a healthy call, but stream stall extended hold to
# 85-153s. A 30s cap forces a sever at any stall beyond the expected
# first-token-plus-generation window, which for docstring-expansion
# workloads should comfortably finish in <20s on a healthy DW endpoint.
#
# Env-tunable so operators can relax for legitimately slow workloads
# (e.g., extremely long CoT traces on architectural ops). The Claude
# fallback still sees its own `_FALLBACK_MAX_TIMEOUT_S` budget after
# the DW path is severed.
_TIER3_REFLEX_HARD_CAP_S = float(
    os.environ.get("OUROBOROS_TIER3_REFLEX_HARD_CAP_S", "30")
)

# ──────────────────────────────────────────────────────────────────────
# Slice 18c (2026-05-26) — route-aware Tier 0 RT budget cap
#
# Closes the cascade-to-Claude-on-premature-timeout pattern surfaced by
# soak bt-2026-05-26-070049 (FLEET v13): Slice 10A correctly routed
# SWE-Bench-Pro to STANDARD; Slice 10B-iii promoted Qwen 397B; Slice
# 10B-ii bridge unblocked the topology; candidate_generator dispatched
# DW Tier 0 RT — but the 30s default cap (above) clamped the budget
# below the 397B's actual TTFT envelope. Result: 8 EXHAUSTION events,
# each cascading to Claude which then refused on credit-balance.
#
# The 30s default was designed for IMMEDIATE-equivalent "reflex"
# semantics (per Manifesto §5 — speed permanently supersedes cost).
# Applying it to STANDARD + COMPLEX routes — which are explicitly
# cost-optimized (DW primary) and have no reflex-time SLA — is a
# category error.
#
# Fix: route-aware cap selector. STANDARD + COMPLEX use the new
# JARVIS_DW_TIER0_RT_BUDGET_S (default 90s — matches Qwen 397B + Kimi
# K2.6 TTFT envelope per §46.2). BG/SPEC + everything else keeps the
# 30s reflex cap (those are either cost-floored or DW-only routes
# where 30s is the right ceiling).
#
# Operator override per route via the env knob; future Slice 13B
# bandit (§45.7.2) can replace this static cap with per-shape
# empirical p95 envelope. Until then, 90s is the empirical floor
# observed on 397B cold-start cold-cache runs.
# ──────────────────────────────────────────────────────────────────────
_TIER0_RT_BUDGET_STANDARD_COMPLEX_S = float(
    os.environ.get("JARVIS_DW_TIER0_RT_BUDGET_S", "90"),
)


# ──────────────────────────────────────────────────────────────────────
# Slice 27 Phase 3 — Context-Aware Adaptive Timeboxing
# ──────────────────────────────────────────────────────────────────────
#
# v20 forensic (bt-2026-05-27-011121): 12 EXHAUSTION events, ALL with
# fsm_failure_mode=TIMEOUT, on a 3-model fleet (Qwen-397B + Qwen-35B +
# Kimi-K2.6). DW was reachable (cost recorded $0.0149) but every
# GENERATE call exceeded the static 90s Tier 0 budget. The model is
# given a fixed budget regardless of how heavy the prompt is or which
# model is processing it — defeating the purpose of having a
# multi-model fleet.
#
# Per operator directive: compute the streaming timeout window
# dynamically at dispatch time from (payload size, model tier).
#
#   base               = 60s
#   +15s per 5000 chars of input payload (step bonus)
#   × 1.5 scalar for heavy reasoning / long-context models
#                        (Qwen3.5-397B-A17B-FP8, Kimi-K2.6)
#   hard cap           = 240s (safe ceiling — no unbounded cost bleed)
#   non STANDARD/COMPLEX routes → preserve legacy 30s reflex cap
#
# Examples (STANDARD/COMPLEX route):
#   0 chars   + 397B  → 60.0 * 1.5  = 90.0s   (matches v18c default)
#   10000     + 397B  → (60+30)*1.5 = 135.0s  (50% more for 10KB SWE prompt)
#   30000     + 397B  → (60+90)*1.5 = 225.0s  (heavy prompt + heavy model)
#   50000     + 397B  → (60+150)*1.5 = 315 → capped 240.0s
#   0         + 35B   → 60.0s       (workhorse — no scalar)
#   10000     + 35B   → (60+30)     = 90.0s
#
# Hardcoding-free: every threshold reads from env at call time so
# operators can tune without code edits. Defaults match the operator's
# spec exactly.

_ADAPTIVE_BASE_S_DEFAULT = 60.0 # Base timeout in seconds for the adaptive formula when prompt_chars is zero. This is the starting point for the timeout calculation before adding the step bonus and applying the heavy model scalar. Default is 60s as per operator spec.
_ADAPTIVE_STEP_CHARS_DEFAULT = 5000 # Number of prompt characters that trigger each step bonus increment. For every multiple of this number of characters in the prompt, the step bonus is added to the base timeout. Default is 5000 chars as per operator spec.
_ADAPTIVE_STEP_BONUS_S_DEFAULT = 15.0 # Additional timeout in seconds added for each step of prompt_chars defined by _ADAPTIVE_STEP_CHARS_DEFAULT. For example, with a step_chars of 5000 and a step_bonus of 15s, a prompt of 10000 chars would add 30s to the base timeout. Default is 15s as per operator spec. 
_ADAPTIVE_HEAVY_SCALAR_DEFAULT = 1.5 # Scalar multiplier applied to the timeout when the model is identified as a heavy model (e.g., Qwen-397B or Kimi-K2.6). This accounts for the longer TTFT of heavy models. Default is 1.5x as per operator spec.
_ADAPTIVE_CAP_S_DEFAULT = 240.0 # Maximum timeout in seconds that can be returned by the adaptive formula, regardless of prompt size or model. This prevents unbounded timeouts for extremely large prompts. Default is 240s as a safe ceiling per operator spec.

# Slice 28 Phase 2 — Adaptive Streaming TTFT Horizon
# Heavy-reasoning / long-context models legitimately need more cold-start
# TTFT runway than the static 30s _PRIMARY_MAX_TIMEOUT_S allows. Scale
# _PRIMARY_MAX_TIMEOUT_S by this factor when the dispatched model is
# heavy (matched via _is_heavy_model — same Qwen-397B / Kimi-K2.6 markers
# Slice 27 Phase 3 uses). Hard ceiling at 240s prevents unbounded cost
# bleed. Per operator directive: base 30s × 2.5 = 75s for heavy models.
# v21 forensic (bt-2026-05-27-025855) showed 12 EXHAUSTION events on
# 397B all at elapsed=30.01s with remaining=329.86s — the static cap
# was the binding constraint, killing primary calls before the streaming
# layer's 120s TTFT could even fire on the wire.
_PRIMARY_HEAVY_TTFT_SCALAR_DEFAULT = 2.5 # Scalar multiplier for heavy models' TTFT horizon. When the dispatched model is identified as heavy (e.g., Qwen-397B or Kimi-K2.6), the primary timeout cap is scaled by this factor to allow for longer cold-start TTFT. This is applied on top of the existing _PRIMARY_MAX_TIMEOUT_S cap, which serves as a base for all models. Default is 2.5x as per operator directive, giving heavy models a 75s cap instead of 30s.
_PRIMARY_HEAVY_TTFT_CAP_S_DEFAULT = 240.0 # Maximum timeout in seconds for heavy models on the primary path. This serves as a hard ceiling to prevent unbounded timeouts even for heavy models. Default is 240s as a safe ceiling per operator directive, ensuring that even with the heavy scalar, the timeout does not exceed this limit.

# Slice 28 Phase 3 — Inline Fault Discriminator probe timeout.
# When the adaptive primary timeout fires on TimeoutError, this
# bounded probe (default 5s) discriminates context-lag vs
# infrastructure-outage. Short by design — the probe MUST NOT
# itself become a wedge.
_TTFT_PROBE_TIMEOUT_S_DEFAULT = 5.0
_TTFT_PROBE_PROMPT = "ping"
_TTFT_PROBE_MAX_TOKENS = 2


def _envb(name: str, default: bool = False) -> bool:
    """Stdlib-only truthy env reader. Lives alongside _envf/_envi helpers."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")

# Heavy-model substring matchers — checked case-insensitively against
# model_id. CSV-extensible via env var so operators can add new heavy
# variants without code edits (a Qwen3.5-512B-MoE release wouldn't need
# a code change to get the 1.5× scalar). Default set codifies operator's
# §46 fleet inventory: the 397B MoE workhorse + Kimi's 200K-context
# specialist (both warrant the heavy budget per §46 strengths).
_HEAVY_MODEL_DEFAULT_MARKERS = ("397B", "Kimi") # Default heavy model markers. 

# Defensive: this function is called on every Tier 0 dispatch, so we read and parse the env var once per call. The parsing logic is robust to empty/malformed env vars, falling back to the default marker set when necessary. The tuple of markers is returned for efficient substring checks in the hot path.
def _heavy_model_markers() -> Tuple[str, ...]:
    """CSV-tunable heavy-model match list. Default: ('397B', 'Kimi')."""
    raw = os.environ.get("JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS", "").strip() # Read the raw env var value as a string and strip whitespace. If the env var is not set or is empty after stripping, return the default heavy model markers. Defensive: if the raw value is empty, return the default immediately without trying to parse it. This handles both unset and explicitly empty env vars gracefully.
    if not raw: # Defensive: if the raw value is empty, return the default immediately without trying to parse it. This handles both unset and explicitly empty env vars gracefully.
        return _HEAVY_MODEL_DEFAULT_MARKERS # Return the default heavy model markers if the env var is not set or is empty. This ensures that we have a sensible default set of markers to identify heavy models without requiring operator configuration.
    return tuple(m.strip() for m in raw.split(",") if m.strip()) # Split the raw string by commas, strip whitespace from each marker, and return a tuple of non-empty markers. This allows operators to specify a custom list of heavy model markers via the env var, while ensuring that empty entries are ignored. 

# Float env vars are used for time thresholds to allow fractional seconds and to keep the env interface simple. Defensive: negative values are treated as zero.
def _envf_or_default(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip() # Read the raw env var value as a string and strip whitespace. If the env var is not set or is empty after stripping, return the default value.
    if not raw: # Defensive: if the raw value is empty, return the default immediately without trying to parse it. This handles both unset and explicitly empty env vars gracefully.
        return default # Return the default value if the env var is not set or is empty.
    try: # Try to parse the raw string as a float. If parsing fails (e.g., due to invalid format), catch the ValueError and return the default instead.
        return float(raw) # Convert the raw string to a float and return it. This allows for fractional seconds in time thresholds.
    except ValueError: # If the raw value cannot be parsed as a float, return the default. This ensures that invalid env var values don't cause crashes and instead fall back to safe defaults.
        return default # Return the default value if parsing fails due to invalid format.

# Integer env vars are used for char counts to avoid fractional chars and to keep the env interface simple. Defensive: negative values are treated as zero.
def _envi_or_default(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip() # Read the raw env var value as a string and strip whitespace. If the env var is not set or is empty after stripping, return the default value.
    if not raw: # Defensive: if the raw value is empty, return the default immediately without trying to parse it. This handles both unset and explicitly empty env vars gracefully.
        return default # Return the default value if the env var is not set or is empty.
    try: # Try to parse the raw string as an integer. If parsing fails (e.g., due to invalid format), catch the ValueError and return the default instead.
        return int(raw) # Convert the raw string to an integer and return it. This is used for char count thresholds where fractional chars don't make sense.
    except ValueError: # If the raw value cannot be parsed as an integer, return the default. This ensures that invalid env var values don't cause crashes and instead fall back to safe defaults.
        return default # Return the default value if parsing fails due to invalid format.

# Slice 84 — param-aware heavy threshold. The marker fast-path ("397B","Kimi")
# only covered two models; Slice 83 then ranked DeepSeek-V4-Pro (1000B) and
# GLM-5.1 (754B) FIRST, but they carried NO marker → got the bare 30s TTFT cap →
# killed at elapsed=30.01s before first token (the v44-v64 "DW down" mirage).
# Any model at/above this parameter count is treated as heavy (deserves the
# longer TTFT runway), so the whole frontier-coder fleet — present AND future —
# qualifies WITHOUT a per-model marker. 100B cleanly separates the 397B+/754B+
# workhorses (+ DeepSeek-V4-Flash 100B) from the cheap Qwen-35B fast-path model.
_HEAVY_MODEL_MIN_PARAMS_B_DEFAULT: float = 100.0


def _heavy_model_min_params_b() -> float:
    """Env-tunable parameter-count floor for param-aware heavy detection."""
    return _envf_or_default(
        "JARVIS_HEAVY_MODEL_MIN_PARAMS_B", _HEAVY_MODEL_MIN_PARAMS_B_DEFAULT,
    )


# Model ID matchers for heavy models. Two paths: (1) the curated/CSV marker
# fast-path, (2) Slice 84 param-aware fallback. Used to apply the heavy-model
# TTFT scalar in the adaptive primary-budget formula.
def _is_heavy_model(model_id: str) -> bool:
    """True iff ``model_id`` warrants the heavy-model TTFT runway.

    A model qualifies if EITHER it matches a curated/CSV marker
    (``397B``/``Kimi``, operator-extensible) OR — Slice 84 — its resolved
    parameter count is at/above ``JARVIS_HEAVY_MODEL_MIN_PARAMS_B`` (default
    100B). The param path reuses Slice 82's catalog resolver (curated map +
    ``\\d+B`` regex), so the strong DW coders (DeepSeek-V4-Pro 1000B, GLM-5.1
    754B, …) auto-qualify with no per-model hardcoding. Fail-soft: an
    unresolvable param count + no marker → not heavy. Pure; never raises."""
    if not model_id:  # Defensive: empty model_id is not heavy.
        return False
    mid_lower = model_id.lower()  # Lowercase once for efficiency.
    # (1) curated / CSV marker fast-path
    if any(m.lower() in mid_lower for m in _heavy_model_markers()):
        return True
    # (2) Slice 84 — param-aware fallback
    try:
        from backend.core.ouroboros.governance.dw_catalog_client import (
            parse_parameter_count,
        )
        pb = parse_parameter_count(model_id)
        if pb is not None and pb >= _heavy_model_min_params_b():
            return True
    except Exception:  # noqa: BLE001 — never block dispatch on a catalog hiccup
        pass
    return False

# Pure function for the adaptive Tier 0 timeout formula. Called by the route-aware cap selector (:func:`_tier0_rt_cap_for_route`) when the caller
def _compute_adaptive_tier0_timeout_s(
    *,
    prompt_chars: int, # Caller-provided prompt size in chars — used to compute the step bonus. Defensive: negative treated as zero.
    model_id: str, # Caller-provided model ID — used to determine if the heavy-model scalar applies. Defensive: empty treated as non-heavy.
    base_s: Optional[float] = None, # Optional override for the base timeout in seconds. If not provided, reads from env var JARVIS_ADAPTIVE_TIER0_BASE_S or defaults to _ADAPTIVE_BASE_S_DEFAULT.
    step_chars: Optional[int] = None, # Optional override for the number of chars per step in the adaptive formula. If not provided, reads from env var JARVIS_ADAPTIVE_TIER0_STEP_CHARS or defaults to _ADAPTIVE_STEP_CHARS_DEFAULT.
    step_bonus_s: Optional[float] = None, # Optional override for the step bonus in seconds. If not provided, reads from env var JARVIS_ADAPTIVE_TIER0_STEP_BONUS_S or defaults to _ADAPTIVE_STEP_BONUS_S_DEFAULT. 
    heavy_scalar: Optional[float] = None, # Optional override for the heavy model scalar. If not provided, reads from env var JARVIS_ADAPTIVE_TIER0_HEAVY_SCALAR or defaults to _ADAPTIVE_HEAVY_SCALAR_DEFAULT.
    cap_s: Optional[float] = None, # Optional override for the maximum timeout cap in seconds. If not provided, reads from env var JARVIS_ADAPTIVE_TIER0_CAP_S or defaults to _ADAPTIVE_CAP_S_DEFAULT.
) -> float:
    """Slice 27 Phase 3 — pure-function adaptive Tier 0 timeout.

    Operator's formula, fully env-tunable:

        timeout = (base + step_bonus × floor(prompt_chars / step_chars))
                  × (heavy_scalar if _is_heavy_model(model_id) else 1.0)
        timeout = min(timeout, cap)

    Caller-provided kwargs win over env defaults; env defaults win over
    code defaults. Pure function — no side effects, deterministic.
    """
    b = base_s if base_s is not None else _envf_or_default(
        "JARVIS_ADAPTIVE_TIER0_BASE_S", _ADAPTIVE_BASE_S_DEFAULT,
    )
    sc = step_chars if step_chars is not None else _envi_or_default(
        "JARVIS_ADAPTIVE_TIER0_STEP_CHARS", _ADAPTIVE_STEP_CHARS_DEFAULT,
    )
    sb = step_bonus_s if step_bonus_s is not None else _envf_or_default(
        "JARVIS_ADAPTIVE_TIER0_STEP_BONUS_S", _ADAPTIVE_STEP_BONUS_S_DEFAULT,
    )
    hs = heavy_scalar if heavy_scalar is not None else _envf_or_default(
        "JARVIS_ADAPTIVE_TIER0_HEAVY_SCALAR", _ADAPTIVE_HEAVY_SCALAR_DEFAULT,
    )
    cap = cap_s if cap_s is not None else _envf_or_default(
        "JARVIS_ADAPTIVE_TIER0_CAP_S", _ADAPTIVE_CAP_S_DEFAULT,
    )

    # Defensive — negative payload chars treated as zero
    safe_chars = max(0, int(prompt_chars or 0)) # Ensure prompt_chars is a non-negative integer. If prompt_chars is None or negative, treat it as zero. This prevents the formula from producing a smaller timeout due to negative char counts.
    steps = safe_chars // max(1, sc)  # avoid div-by-zero on misconfigured env
    timeout = b + sb * steps # Calculate the timeout based on the base, step bonus, and number of steps determined by the prompt size. The step bonus increases the timeout for larger prompts according to the operator's formula.
    if _is_heavy_model(model_id): # Apply the heavy model scalar if the model_id matches any of the heavy model markers. This accounts for the longer TTFT of heavy models.
        timeout *= hs # Scale the timeout by the heavy model scalar if applicable.
    return min(timeout, cap) # Apply the maximum cap to ensure the timeout does not exceed the specified limit, preventing unbounded timeouts for extremely large prompts or heavy models.


def _tier0_rt_cap_for_route(
    provider_route: str,
    *,
    model_id: str = "",
    prompt_chars: int = 0,
) -> float:
    """Tier 0 RT cap — adaptive when model_id/prompt_chars provided,
    legacy 90s/30s wall when not.

    Slice 18c semantics preserved for callers that don't pass the new
    kwargs (byte-identical to pre-Slice-27 behavior):
      STANDARD + COMPLEX → 90s default (env-tunable)
      everything else    → 30s reflex cap

    Slice 27 Phase 3 — when EITHER model_id or prompt_chars is provided,
    the STANDARD/COMPLEX path switches to the adaptive formula
    (:func:`_compute_adaptive_tier0_timeout_s`). The 30s reflex cap for
    other routes is preserved unconditionally (IMMEDIATE/BG/SPEC have
    cost-optimization semantics that should not pay the dispatch-time
    payload-sizing cost).
    """
    r = (provider_route or "").strip().lower()
    if r not in ("standard", "complex"):
        return _TIER3_REFLEX_HARD_CAP_S

    # Slice 27 Phase 3 — adaptive only when caller has context.
    # Legacy callers that pass only the route get the historical
    # 90s static cap (matches Slice 18c byte-identically).
    if not model_id and prompt_chars <= 0:
        return _TIER0_RT_BUDGET_STANDARD_COMPLEX_S
    return _compute_adaptive_tier0_timeout_s(
        prompt_chars=prompt_chars,
        model_id=model_id,
    )


# Legacy alias retained for downstream imports + existing test surface.
# Do not change to a different default without updating the test pins.
# Reads OUROBOROS_PRIMARY_MAX_TIMEOUT_S as a per-primary override — when
# set, wins over the shared Tier 3 cap for the _call_primary path only
# (the _compute_tier0_budget path continues to use _TIER3_REFLEX_HARD_CAP_S).
_PRIMARY_MAX_TIMEOUT_S = float(
    os.environ.get("OUROBOROS_PRIMARY_MAX_TIMEOUT_S", str(_TIER3_REFLEX_HARD_CAP_S))
)

# Minimum time worth attempting a fallback API call.  Below this threshold
# the call will almost certainly timeout before the model finishes; skip it
# and raise immediately to avoid burning network round-trip time.
_MIN_VIABLE_FALLBACK_S = float(os.environ.get("OUROBOROS_MIN_VIABLE_FALLBACK_S", "10"))

# Guaranteed minimum window for the fallback (Claude) regardless of how much
# parent-deadline budget Tier 0 consumed before failing. When the parent
# deadline is depleted (e.g. DW timed out after 80s of a 120s window),
# `_call_fallback` REFRESHES its own deadline so Claude gets at least this
# many seconds — otherwise legitimate doc-gen / patch streams (60-100s)
# get cut off mid-flight and the whole op fails to `all_providers_exhausted`.
# Diagnosed in bt-2026-04-11-211131 (24x exhaustion, 0 commits).
# This OVERRIDES the parent wall-clock deadline; the orchestrator's outer
# `wait_for(_gen_timeout + _OUTER_GATE_GRACE_S)` is the absolute Iron Gate.
_FALLBACK_MIN_GUARANTEED_S = float(
    os.environ.get("OUROBOROS_FALLBACK_MIN_GUARANTEED_S", "90"),
)

# Tier 3 reflex cap for PLAN phase (item B from F1 Slice 4 S5 triage,
# bt-2026-04-24-220418). PLAN is soft-fail — callers (PlanGenerator) catch
# exceptions and fall through to GENERATE without plan, so an aggressive cap
# is even more appropriate here than at GENERATE. Two surfaces:
#   (a) primary path reuses the same `_TIER3_REFLEX_HARD_CAP_S` as the
#       GENERATE Tier-0 budget (default 30s) — see plan() below.
#   (b) fallback (Claude) path uses this PLAN-specific override (default 60s,
#       half the GENERATE fallback cap) because PLAN's structured plan.1 JSON
#       is short — Claude doesn't need the full 120s reserve.
# S5 surfaced the gap: CandidateGenerator.plan() at line ~2244 was passing
# raw `remaining` (≈parent deadline) to wait_for, so DW could stall up to
# 90s before failing and Claude could stall up to 120s, total 210s — eating
# the entire BG worker pool ceiling (360s) before GENERATE got to run.
_PLAN_FALLBACK_MAX_TIMEOUT_S = float(
    os.environ.get("OUROBOROS_PLAN_FALLBACK_MAX_TIMEOUT_S", "60"),
)

# ---------------------------------------------------------------------------
# Outer-retry budget (rooted-problem fix 2026-04-25)
# ---------------------------------------------------------------------------
#
# F1 Slice 4 cadence S1b (`bt-2026-04-25-054256`) surfaced the rooted
# problem behind W3(6) Slice 5b's `live_reachability=blocked_by_provider_exhaustion`:
#
#   * `_call_fallback` invokes the provider ONCE.
#   * The provider's internal `_call_with_backoff` does ~3 attempts with
#     exponential 2s/4s backoff, recycling the httpx pool between attempts.
#   * When TCP connect or stream-read fails (anyio cancel scope fires
#     before the API even responds), all 3 internal attempts can exhaust
#     in ~70-80s.
#   * `_call_fallback` then catches the propagated CancelledError and
#     fires `EXHAUSTION cause=fallback_failed` — even when 100+s of
#     parent budget remains.
#
# The budget JARVIS authorized at ROUTE goes unused. Network conditions
# may have recovered by the time those retries would have fired.
#
# Operator binding 2026-04-25 (Option B closure of S1b):
#   "Will not mask provider latency by modifying the seed (Option C) or
#    artificially inflating the timeout boundaries (Option D). The
#    internal architecture is mathematically sound."
#
# This fix adds NO new budget — it just CONSUMES the budget already
# authorized. Outer retry loop re-invokes the provider (head-of-queue
# preserved by holding `_fallback_sem`) on transient failures while
# remaining budget exceeds `_MIN_VIABLE_FALLBACK_S` and the failure
# mode is in `_FALLBACK_TRANSIENT_MODES`. Cooperative cancel via
# `OperationCancelledError` (W3(7) cancel-token) is honored immediately
# — never retried.
_FALLBACK_OUTER_RETRY_MAX = int(
    os.environ.get("JARVIS_FALLBACK_OUTER_RETRY_MAX", "3")
)
_FALLBACK_OUTER_RETRY_BACKOFF_S = float(
    os.environ.get("JARVIS_FALLBACK_OUTER_RETRY_BACKOFF_S", "1.0")
)


# ---------------------------------------------------------------------------
# Slice 12N — ProviderRoute → CircuitTripOrigin mapping
# ---------------------------------------------------------------------------
#
# Blast-radius isolation: only FOREGROUND-origin per-op breakers
# escalate structural trips to the global session_exhausted
# threshold. Background / speculative ops get their own per-op
# breaker but their structural trips are ISOLATED — they cannot
# assassinate a healthy in-flight foreground op (the wedge that
# killed the SWE-Bench-Pro fixture in bt-2026-05-23-015723).
#
# Lookup is by lowercased provider_route string so this map stays
# robust to either Enum-as-value or bare-string population of
# ``context.provider_route``. Unknown / empty routes default to
# FOREGROUND at the call site (safer — preserves legacy escalation).
#
# Lazy import inside the dict construction is unavoidable here
# because circuit_breaker would otherwise cycle through
# candidate_generator at module load. Resolved once at module
# import time.
def _slice12n_build_route_origin_map() -> Dict[str, Any]:
    from backend.core.ouroboros.governance.circuit_breaker import (
        CircuitTripOrigin,
    )
    return {
        "immediate":   CircuitTripOrigin.FOREGROUND,
        "standard":    CircuitTripOrigin.FOREGROUND,
        "complex":     CircuitTripOrigin.FOREGROUND,
        "background":  CircuitTripOrigin.BACKGROUND,
        "speculative": CircuitTripOrigin.SPECULATIVE,
    }


_SLICE12N_ROUTE_TO_ORIGIN: Dict[str, Any] = _slice12n_build_route_origin_map()

# Anthropic resilience pack 2026-04-25 — failure-rate-aware outer-retry.
# When the FailbackStateMachine has logged transient failures recently
# (a window of consecutive_failures > 0 within the past few cycles), bump
# the outer-retry cap from `_FALLBACK_OUTER_RETRY_MAX` to
# `_FALLBACK_OUTER_RETRY_MAX_DEGRADED` for that op only. Healthy ops
# never pay the extra retry cost.
#
# Observed live in F1 Slice 4 S4b: 6 Claude transient failures + 8 pool
# recycles in 30min. The seed's 1 outer-retry attempt wasn't enough to
# survive the full anthropic_transport instability window. Bumping to 5
# attempts during instability gives ~3× more headroom to catch a
# recovery window.
#
# Default = 5 (vs base 3). Set via JARVIS_FALLBACK_OUTER_RETRY_MAX_DEGRADED.
# Master-off via DEGRADED == base (no extra retries even when degraded).
_FALLBACK_OUTER_RETRY_MAX_DEGRADED = int(
    os.environ.get(
        "JARVIS_FALLBACK_OUTER_RETRY_MAX_DEGRADED", "5",
    )
)

# ---------------------------------------------------------------------------
# Nervous System Reflex — BACKGROUND cascade for read-only ops
# ---------------------------------------------------------------------------
#
# Manifesto §5: "Intelligence-driven routing", but survival and execution
# speed permanently supersede cost optimization. For read-only ops on the
# BACKGROUND route we sever the DW thread on a strict temporal budget and
# cascade to Claude — regardless of topology skip_and_queue flags or the
# JARVIS_BACKGROUND_ALLOW_FALLBACK gate that gates the same reflex for
# mutating BG ops.
#
# Cost safety is preserved by the upstream is_read_only contract:
#   * policy Rule 0d refuses every mutating tool under is_read_only=True
#   * orchestrator short-circuits APPLY on read-only ops
# so a Claude cascade under is_read_only carries no write risk; it only
# loses cost optimality, which the Nervous System Reflex explicitly
# trades against lockup avoidance.
_BG_READONLY_DW_STALL_BUDGET_S = float(
    os.environ.get("JARVIS_BG_DW_STALL_BUDGET_S", "60"),
)


def _attribute_cancel(
    exc: BaseException,
    *,
    label: str,
    op_id: str,
    elapsed_s: float,
    remaining_s: float,
) -> str:
    """Best-effort cancel-source attribution for telemetry.

    Pure-observation helper added 2026-04-24 (post-S6 / bt-2026-04-24-225137)
    to disambiguate three cancel classes seen in F1 Slice 4 graduation:

    - **A** — `_FALLBACK_MAX_TIMEOUT_S=120s` per-call cap (`TimeoutError`).
    - **B** — ToolLoop per-round budget (`TimeoutError` at the per-round mark).
    - **C** — external cooperative cancel (`CancelledError` with non-zero
      remaining budget) — sibling-task cancel / retry-harness deadline /
      mid-flight TopologyBlock reroute.

    Walks `asyncio.current_task()` to capture this task's `cancelling()`
    counter (>0 means we were cancelled by an outer task; ==0 means we
    timed out from our own `wait_for`). Walks `asyncio.all_tasks()` to
    surface a likely-canceller name (best-effort; no guarantee).

    Returns a single-line structured string suitable for logging.
    Never raises — attribution failure is logged as `attribution_error=...`.
    """
    err_class = type(exc).__name__

    def _safe_cancelling(task: Any) -> int:
        # Task.cancelling() is Python 3.11+. We target 3.9+, so always go
        # through getattr/lambda to keep typecheckers + 3.9 runtime happy.
        fn = getattr(task, "cancelling", None)
        if fn is None:
            return 0
        try:
            return int(fn())
        except Exception:
            return 0

    try:
        current = asyncio.current_task()
        own_cancelling = _safe_cancelling(current)
        # Best-effort canceller search: any other live task with cancelling()>0
        # is a candidate. Walks at most 64 tasks to bound cost.
        canceller = "unknown"
        try:
            for t in list(asyncio.all_tasks())[:64]:
                if t is current:
                    continue
                if _safe_cancelling(t) > 0:
                    canceller = t.get_name()
                    break
        except RuntimeError:
            canceller = "no_running_loop"
        # Heuristic class assignment:
        #   - TimeoutError + own_cancelling==0 + remaining≈0 → Class A/B (own deadline)
        #   - CancelledError + own_cancelling>0              → Class C (external)
        #   - CancelledError + own_cancelling==0             → ambiguous (loop teardown?)
        if isinstance(exc, asyncio.TimeoutError):
            klass = "A_or_B_timeout"
        elif isinstance(exc, asyncio.CancelledError) and own_cancelling > 0:
            klass = "C_external_cancel"
        elif isinstance(exc, asyncio.CancelledError):
            klass = "C_ambiguous"
        else:
            klass = "non_cancel"
        return (
            f"label={label} op={op_id[:16]} class={klass} "
            f"err={err_class} elapsed={elapsed_s:.2f}s "
            f"remaining={remaining_s:.2f}s "
            f"own_cancelling={own_cancelling} "
            f"canceller_task={canceller}"
        )
    except Exception as e:
        return (
            f"label={label} op={op_id[:16]} class=attribution_failed "
            f"err={err_class} elapsed={elapsed_s:.2f}s "
            f"remaining={remaining_s:.2f}s "
            f"attribution_error={type(e).__name__}"
        )

# ---------------------------------------------------------------------------
# Route-scoped Claude fallback disable (isolation harnesses)
# ---------------------------------------------------------------------------
#
# ``JARVIS_DISABLE_CLAUDE_FALLBACK_ROUTES`` accepts a comma-separated list of
# route names. Any op whose ``provider_route`` matches will skip the Claude
# fallback entirely when Tier 0 fails, raising a clean
# ``fallback_disabled_by_env:{route}`` sentinel through the existing
# exhaustion path. Used by the Qwen 397B isolation benchmark to collect raw
# DW completion telemetry without Claude masking failures or burning tokens.
# Default unset → normal cascade behavior.
_DISABLE_FALLBACK_ROUTES_ENV = "JARVIS_DISABLE_CLAUDE_FALLBACK_ROUTES"


def _fallback_disabled_for_route(route: str) -> bool:
    raw = os.environ.get(_DISABLE_FALLBACK_ROUTES_ENV, "").strip()
    if not raw:
        return False
    disabled = {r.strip().lower() for r in raw.split(",") if r.strip()}
    return (route or "").strip().lower() in disabled


# ──────────────────────────────────────────────────────────────────────
# Slice 23 — Autonomous Registry-Driven Sentinel Activation
# ──────────────────────────────────────────────────────────────────────
#
# v16/v17 forensic exposed that locking dispatch to a single DW model
# when an entire trusted-seed fleet sits in the PromotionLedger is an
# architectural bottleneck. The fix is NOT a per-soak env flag — it is
# a structural decision the dispatcher makes at every call from the
# active registry state.
#
# Decision matrix (first-match-wins; closed and deterministic):
#
#   1. Operator explicit-on  (JARVIS_TOPOLOGY_SENTINEL_ENABLED=true)
#      → ACTIVATE (legacy explicit-on contract, preserved verbatim).
#
#   2. Operator explicit-off (JARVIS_TOPOLOGY_SENTINEL_ENABLED=false)
#      → DO NOT activate (operator rollback wins over every structural
#        condition — single-knob hot-revert preserved per §33).
#
#   3. Claude tier structurally absent  (JARVIS_PROVIDER_CLAUDE_DISABLED=true)
#      → ACTIVATE. Slice 19a declares "Claude removed → DW fleet IS
#        the only intelligence". Iterating the fleet is the architectural
#        contract that operator-binding implies. Composes with Slice 22
#        tier-decay (IMMEDIATE→STANDARD demotion when Claude absent).
#
#   4. Multi-model trusted fleet for this route  (≥2 promoted ledger
#      entries that pass the route's eligibility gate)
#      → ACTIVATE. A multi-model fleet exists precisely so dispatch can
#        rotate among them on failure. Locking to one when 2+ are
#        promoted defeats the PromotionLedger's purpose.
#
#   5. Default  (Claude enabled + single-model fleet + env unset)
#      → DO NOT activate. Phase 10 graduation contract preserved for
#        the Claude-enabled posture this contract was written about.
#
# The structural conditions (3, 4) compose `JARVIS_PROVIDER_CLAUDE_DISABLED`
# (Slice 19a) and `_trusted_seed_dw_models_for_route` (Slice 10B-ii) —
# both already-existing substrate. No new env knobs, no new state,
# no parallel ledgers. The PromotionLedger is the autonomous registry;
# the trusted-seed bridge already enforces per-route eligibility gates.
#
# The Phase 10 graduation contract AST pin
# (`phase10_graduation_contract.py`) asserts the master flag DEFAULT
# stays false — which it does. Slice 23 adds structural OVERRIDES on
# top of that default; the literal env-var default is unchanged.


_SENTINEL_ENABLED_ENV = "JARVIS_TOPOLOGY_SENTINEL_ENABLED"
_CLAUDE_DISABLED_ENV = "JARVIS_PROVIDER_CLAUDE_DISABLED"
_SLICE23_MIN_PROMOTED_FOR_AUTO = 2


def _claude_config_disabled() -> bool:
    """True when the Claude fallback tier is STRUCTURALLY disabled via
    ``JARVIS_PROVIDER_CLAUDE_DISABLED`` — the deadest possible fallback (the
    provider is never even constructed), distinct from a tripped circuit breaker.

    DW-autarky's full-runway grant (Slice 225) keys off ``_claude_breaker_open``,
    which reads only the breaker STATE — and a config-disabled Claude never trips
    the breaker, so it stays CLOSED and autarky NEVER engaged under
    ``JARVIS_PROVIDER_CLAUDE_DISABLED=true``. The sole-lane DW was then held to
    the 90s reflex cap and TIMED OUT on slow hosts (live container soak,
    2026-06-20). A config-disabled Claude is *more* dead than a breaker-open one;
    this predicate lets the autarky path treat it as such. Reuses the existing
    ``_CLAUDE_DISABLED_ENV`` constant (no new flag). NEVER raises."""
    try:
        return os.environ.get(_CLAUDE_DISABLED_ENV, "").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001 — fail-closed to legacy cascade
        return False


def _slice23_should_activate_sentinel(provider_route: str) -> Tuple[bool, str]:
    """Slice 23 — autonomous registry-driven sentinel activation.

    Returns ``(activate, reason)`` where ``reason`` is a short
    classifier string suitable for logging (one of: ``env_explicit_on``,
    ``env_explicit_off``, ``claude_disabled``, ``multi_model_fleet``,
    ``default_off_phase10_contract``, ``trusted_seed_probe_failed``).

    Pure function over env + PromotionLedger snapshot. No side effects.
    Defensive against trusted-seed probe failures — falls through to
    default-off rather than raising into dispatch.
    """
    env_raw = os.environ.get(_SENTINEL_ENABLED_ENV, "").strip().lower()
    if env_raw in ("1", "true", "yes", "on"):
        return True, "env_explicit_on"
    if env_raw in ("0", "false", "no", "off"):
        return False, "env_explicit_off"

    claude_raw = os.environ.get(_CLAUDE_DISABLED_ENV, "").strip().lower()
    if claude_raw in ("1", "true", "yes", "on"):
        return True, "claude_disabled"

    # Multi-model fleet probe — lazy import keeps candidate_generator
    # bootable when provider_topology is unavailable (e.g., isolated
    # unit tests). Defensive try/except — bridge failure must NEVER
    # block dispatch; fall through to default-off if the probe raises.
    try:
        from backend.core.ouroboros.governance.provider_topology import (
            _trusted_seed_dw_models_for_route,
        )
        promoted_for_route = _trusted_seed_dw_models_for_route(
            provider_route or "standard",
        )
        if len(promoted_for_route) >= _SLICE23_MIN_PROMOTED_FOR_AUTO:
            return True, "multi_model_fleet"
    except Exception:  # noqa: BLE001 — defensive probe
        return False, "trusted_seed_probe_failed"

    return False, "default_off_phase10_contract"


def gen_call_likely_thinking(route: str, task_complexity: str) -> bool:
    """SINGLE SOURCE OF TRUTH: will this generation call have extended
    thinking enabled (per ``providers._resolve_thinking_budget``)?

    Conservative superset (matches the historical inline rule in
    ``_call_fallback``): any non-trivial ``task_complexity`` on a
    non-reflex (non-IMMEDIATE) route. IMMEDIATE intentionally skips
    thinking (reflex path).

    Phase R1 (soak bt-2026-05-18-015317): the INNER fallback widens
    its cap to ``fallback_thinking_cap_s()`` for thinking-likely
    calls; the OUTER Iron-Gate ``_gen_timeout`` (generate_runner /
    orchestrator) MUST floor to the SAME cap or it kills GENERATE at
    240+15s before the inner 360s window completes (CancelledError@
    255s, psf never generated). Both inner and outer consume THIS
    function so the invariant `outer >= inner` holds by construction
    — no duplicated predicate, no per-path drift.
    """
    _tc = (task_complexity or "").strip().lower()
    _r = (route or "").strip().lower()
    return _tc not in ("", "trivial") and _r not in ("immediate",)


def fallback_thinking_cap_s() -> float:
    """The thinking-enabled timeout cap (env-tunable, default 360s).
    Single resolver shared by the inner fallback cap and the outer
    Iron-Gate ``_gen_timeout`` floor (Phase R1 coherence invariant)."""
    try:
        return float(os.environ.get(
            "JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S", "360.0",
        ))
    except (TypeError, ValueError):
        return 360.0


def force_batch_gen_timeout_floor_s() -> float:
    """Slice 50 Phase 2 — minimum GENERATE-phase deadline for a force-batch op.

    The DW BATCH lane's async poll legitimately runs up to
    ``JARVIS_DW_BATCH_TIMEOUT_S`` (Slice 43, default 300s). The OUTER
    GENERATE deadline must STRICTLY exceed that lease so the batch poll is
    never severed by the outer ``wait_for`` at exactly its own expiry —
    add a small overhead (``JARVIS_FORCE_BATCH_GEN_OVERHEAD_S``, default
    30s) for the sentinel + Iron-Gate processing that follows the poll.

    Derived from the Slice 43 batch-timeout constant, NOT a second
    hardcoded value: change ``JARVIS_DW_BATCH_TIMEOUT_S`` and the floor
    tracks it. Mirror of :func:`fallback_thinking_cap_s` — a single shared
    resolver for the outer/inner deadline-coherence invariant.
    """
    batch_cap = _envf_or_default("JARVIS_DW_BATCH_TIMEOUT_S", 300.0)
    overhead = _envf_or_default("JARVIS_FORCE_BATCH_GEN_OVERHEAD_S", 30.0)
    return batch_cap + overhead


def apply_force_batch_deadline_floor(
    gen_timeout_s: float, *, force_batch: bool
) -> float:
    """Floor a GENERATE-phase deadline so a force-batch op's outer window
    exceeds the DW batch lease (Slice 50 Phase 2).

    Forensic basis — v45 probe ``bt-2026-06-01-034745``: a
    ``route=standard, complexity=trivial`` op force-batched (Slice 36:
    Claude disabled + standard route) but its route-base GENERATE deadline
    was only ``JARVIS_GEN_TIMEOUT_STANDARD_S=220s`` — the R1 thinking-cap
    floor (-> 360s) does not fire for trivial ops. So
    ``_compute_primary_budget(remaining=220, force_batch=True) =
    min(220, 300) = 220`` and the async batch poll was severed at 220s
    while its own 300s lease still had runway (TimeoutError elapsed=220s).

    ``force_batch=False`` ops pass through unchanged (zero regression).
    The floor is a ``max()`` so an already-wide window (e.g. COMPLEX
    R1-floored to 360s) is preserved, never reduced. Safe by construction:
    Slice 36 force-batch only engages when Claude is disabled (pure-DW
    mode), so there is no Claude-cascade calibration to regress.
    """
    if not force_batch:
        return gen_timeout_s
    return max(gen_timeout_s, force_batch_gen_timeout_floor_s())


def structural_fast_cascade_enabled() -> bool:
    """Slice 73 master flag — default TRUE. When off, the dispatch loop tries
    every ranked DW model before cascading (byte-identical legacy behavior)."""
    raw = os.environ.get(
        "JARVIS_DW_STRUCTURAL_FAST_CASCADE_ENABLED", "true",
    ).strip().lower()
    return raw not in ("0", "false", "no", "off")


def should_sever_dw_lane(failure_source: Any) -> bool:
    """Slice 73 — True iff this failure is a STRUCTURAL transport break.

    A ``LIVE_TRANSPORT`` failure (socket/connection break, ``live_transport:
    RuntimeError``) means the transport to the DW endpoint is down — every
    ranked sibling model shares that dead transport, so trying the next one
    just burns another ~30s before the inevitable cascade. Sever the lane and
    hand Claude the full remaining budget.

    Model-SPECIFIC failures (429 rate-limit, 5xx, parse) are NOT severed — a
    sibling model may be healthy, so the loop still rotates to it. Pure;
    never raises (unknown source → don't sever).
    """
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (
            FailureSource,
        )
        return failure_source is FailureSource.LIVE_TRANSPORT
    except Exception:  # noqa: BLE001 — never block dispatch
        return False


def _live_transport_sever_threshold() -> int:
    """Slice 83 Phase 2 — consecutive LIVE_TRANSPORT failures required before
    the whole DW lane is severed (Slice 73 behavior).

    Slice 73 severed the lane on the FIRST ``live_transport`` failure on the
    theory that all ranked siblings share one dead transport. But Slice 82/83
    made the ranked stack HETEROGENEOUS — DeepSeek-V4-Pro, Kimi-K2.6, GLM-5.1,
    Qwen397B, Qwen35B are distinct served endpoints. One model being briefly
    unavailable (deploy bounce, per-model 5xx surfacing as a transport break)
    is NOT a lane outage: the next coder may be perfectly healthy. So we now
    ROTATE to the next model on a single failure and only sever once
    ``threshold`` consecutive models have all failed with LIVE_TRANSPORT — the
    signature of a genuine endpoint-wide blackout. A success (or a non-transport
    failure on a reachable model) resets the streak. Default 3; floored at 1 so
    ``=1`` reproduces exact Slice 73 first-failure sever. Env-tunable."""
    try:
        raw = os.environ.get("JARVIS_DW_LIVE_TRANSPORT_SEVER_THRESHOLD", "3")
        return max(1, int(str(raw).strip()))
    except Exception:  # noqa: BLE001 — bad value → safe default
        return 3


def _note_dw_total_outage(diagnostic: str) -> None:
    """Slice 53 — record one GENERATE op that exhausted ALL DW models with no
    candidate from streaming OR batch (the total-vendor-blackout signature).

    Routed through the dual-lane breaker singleton. NEVER raises (defensive
    lazy import) — recording is best-effort observability + breaker state, it
    must not perturb the generation error path it sits on.
    """
    try:
        from backend.core.ouroboros.governance.dual_lane_breaker import (
            get_dual_lane_breaker,
        )
        get_dual_lane_breaker().record_total_outage(diagnostic or "all_models_open")
    except Exception:  # noqa: BLE001 — never perturb the error path
        pass


def _note_dw_candidate_success() -> None:
    """Slice 53 — record that some DW lane (or fallback) yielded a candidate,
    resetting the breaker's consecutive-outage counter. Preserves Slice 41
    single-lane resilience. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.dual_lane_breaker import (
            get_dual_lane_breaker,
        )
        get_dual_lane_breaker().record_success()
    except Exception:  # noqa: BLE001
        pass
    # Slice 127 P3 — a DW completion succeeded → reset the dynamic-recovery
    # episode counter to 0 instantly so the next transient blip recovers at
    # ``base`` (gated, best-effort; never perturbs the success path).
    try:
        from backend.core.ouroboros.governance.dw_transport_recovery import (
            dw_dynamic_recovery_enabled as _s127_dyn_on,
            get_dw_transport_recovery as _s127_dwr,
        )
        if _s127_dyn_on():
            _s127_dwr().note_recovered()
    except Exception:  # noqa: BLE001
        pass


def _note_dw_live_transport_degraded(diagnostic: str = "", model_id: str = "") -> None:
    """Slice 77 — the millisecond a LIVE dispatch hits a transport break
    (``live_transport:RuntimeError`` / socket drop), stamp the
    ``dw_surface_health`` ledger ``DIRECT_STREAMING → TRANSPORT_DEGRADED`` so
    the NEXT op's Slice 76 P2 pre-flight gate (:func:`dw_transport_degraded_preflight`)
    fires and cascades straight to Claude with the full budget — instead of
    burning the next op's allowance on the same dead transport (the EVAL-2
    Phase-4 ``deadline_exhausted_pre_fallback`` failure, PRD §50.11).

    This converts the ledger from a one-shot BOOT probe into a live,
    event-driven status map. Recovery is automatic: once live generations stop
    failing, no further degraded records are written and the gate's freshness
    window lapses the stale verdict, re-enabling the DW lane. A fresh ledger
    instance per call reads-latest-then-saves, so this never clobbers a
    concurrent probe's record for another surface. NEVER raises (best-effort
    observability must not perturb the dispatch error path it sits on)."""
    try:
        from backend.core.ouroboros.governance.dw_surface_health import (
            SurfaceHealthLedger,
            SurfaceKind,
            SurfaceVerdict,
        )
        SurfaceHealthLedger(autosave=True).record(
            SurfaceKind.DIRECT_STREAMING,
            SurfaceVerdict.TRANSPORT_DEGRADED,
            diagnostic=(diagnostic or "live_transport")[:120],
        )
    except Exception:  # noqa: BLE001 — never perturb the dispatch error path
        pass
    # Slice 127 P3 — register a dynamic-recovery rupture episode (debounced by
    # ``base`` so a burst inside one outage = ONE episode). The dynamic window
    # grows the next probe interval exponentially for a chronically-rupturing
    # lane (gated, best-effort; never perturbs the dispatch error path).
    try:
        from backend.core.ouroboros.governance.dw_transport_recovery import (
            dw_dynamic_recovery_enabled as _s127_dyn_on,
            get_dw_transport_recovery as _s127_dwr,
        )
        if _s127_dyn_on():
            _s127_dwr().note_degraded()
    except Exception:  # noqa: BLE001 — never perturb the dispatch error path
        pass
    # Slice 172 — feed the predictive cortex the SAME rupture event (its own bounded
    # timestamp ring drives the recency-weighted Poisson forecast). Fire-and-forget,
    # lock-guarded append; never perturbs the dispatch error path. Record is
    # UNCONDITIONAL (the master flag gates *routing*, not data collection — so the
    # forecast is already warm the moment predictive routing is switched on).
    try:
        from backend.core.ouroboros.governance.dw_failure_predictor import (
            get_dw_failure_predictor as _s172_pred,
        )
        _s172_pred().record_rupture(model_id=model_id)  # Slice 175 — per-model ring
    except Exception:  # noqa: BLE001 — never perturb the dispatch error path
        pass


def _record_dw_failure_signal(model_id: str, failure_source: Any) -> None:
    """Slice 176 — fuse a classified NON-transport DW FailureSource into the predictive
    cortex as a weighted failure vector (economic 429 / upstream 5xx+parse / stall), per
    model. Transport ruptures are already fed via _note_dw_live_transport_degraded; this
    covers the rest of the spectrum (Blindspot D). Fire-and-forget, never perturbs the
    dispatch error path. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.topology_sentinel import FailureSource
        _kind = {
            FailureSource.LIVE_HTTP_429: "economic",   # quota / rate-limit — imminent lockdown
            FailureSource.LIVE_HTTP_5XX: "upstream",    # server error — localized
            FailureSource.LIVE_PARSE_ERROR: "upstream",  # malformed/empty completion
            FailureSource.LIVE_STREAM_STALL: "transport",  # stalled stream — transport class
        }.get(failure_source)
        if _kind is None:
            return
        from backend.core.ouroboros.governance.dw_failure_predictor import (
            get_dw_failure_predictor as _s176_pred,
        )
        _s176_pred().record_failure(model_id=model_id, kind=_kind)
    except Exception:  # noqa: BLE001 — never perturb the dispatch error path
        pass


def dw_preflight_gate_enabled() -> bool:
    """Slice 76 Phase 2 master flag — default TRUE. When off, dispatch is
    byte-identical to the pre-Slice-76 path (no pre-flight short-circuit)."""
    raw = os.environ.get(
        "JARVIS_DW_PREFLIGHT_GATE_ENABLED", "true",
    ).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _dw_preflight_freshness_s() -> float:
    """Max age (seconds) of a TRANSPORT_DEGRADED surface verdict for the
    pre-flight gate to act on it. Stale evidence is ignored so the gate never
    starves DW on an old reading. Env-tunable; non-positive / invalid → 120s."""
    raw = os.environ.get("JARVIS_DW_PREFLIGHT_FRESHNESS_S", "120").strip()
    try:
        val = float(raw)
        return val if val > 0 else 120.0
    except (ValueError, TypeError):
        return 120.0


def dw_transport_degraded_preflight() -> bool:
    """Slice 76 Phase 2 — pre-flight DW transport health gate.

    Consults the EXISTING ``dw_surface_health`` ledger (kept fresh by the
    surface probes — NO new probe is issued here): returns True iff the
    ``DIRECT_STREAMING`` surface carries a FRESH ``TRANSPORT_DEGRADED`` verdict.
    That means the socket/TLS to the DW endpoint is down RIGHT NOW — every
    ranked sibling model shares that dead transport (cf.
    :func:`should_sever_dw_lane`), so the op should cascade to Claude with its
    full budget BEFORE the ``_primary_sem`` wait + per-model timeout cascade
    burns it (the EVAL-2 ``terminal_timeout``, PRD §50.11).

    Conservative by construction: unknown / stale / HEALTHY / UPSTREAM_DEGRADED
    (server responded — transport is up) all return False, so the DW lane
    proceeds normally and we never starve DW on thin evidence. NEVER raises
    (fail-open: a gate error must not block DW dispatch)."""
    if not dw_preflight_gate_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.dw_surface_health import (
            SurfaceHealthLedger,
            SurfaceKind,
            SurfaceVerdict,
        )
        rec = SurfaceHealthLedger(autosave=False).verdict_for(
            SurfaceKind.DIRECT_STREAMING,
        )
        if rec is None or rec.verdict is not SurfaceVerdict.TRANSPORT_DEGRADED:
            return False
        age_s = time.time() - float(rec.last_probe_unix or 0.0)
        # Slice 127 P3 — the freshness window is how long the DW lane stays
        # severed before the next probe. When the dynamic-recovery master is ON,
        # use the full-jitter EXPONENTIAL window (widens for a chronically-
        # rupturing lane, resets on DW success) instead of the static default.
        # OFF → byte-identical to the pre-P3 fixed window. Fail-safe: a 0/invalid
        # dynamic window falls back to the static one (never starve DW).
        _window_s = _dw_preflight_freshness_s()
        try:
            from backend.core.ouroboros.governance.dw_transport_recovery import (
                dw_dynamic_recovery_enabled as _s127_dyn_on,
                get_dw_transport_recovery as _s127_dwr,
            )
            if _s127_dyn_on():
                _dyn = _s127_dwr().dynamic_recovery_window_s()
                if _dyn and _dyn > 0:
                    _window_s = _dyn
        except Exception:  # noqa: BLE001 — fail-open to the static window
            pass
        return 0.0 <= age_s <= _window_s
    except Exception:  # noqa: BLE001 — never block dispatch on a gate error
        return False


# ---------------------------------------------------------------------------
# Slice 127 P2.1 — fallback-skip gate (IMMEDIATE reroute to DW)
# ---------------------------------------------------------------------------
#
# The live soak proved P1+P2 (no terminal_config brick; economic reclassify +
# ECONOMIC TRIP). But `_generate_immediate` does "Claude direct, skip DW", so an
# IMMEDIATE op keeps grinding against a depleted Claude lane and exhausts instead
# of failing over to the funded DW lane — the existing should_allow_request gate
# only covers Claude-as-PRIMARY. This gate makes the Claude-direct path consult
# the Claude lane breaker first and reroute to the DW primary when it's OPEN.


def fallback_skip_gate_enabled() -> bool:
    """Slice 127 P2.1 master. Slice 146: graduated default-TRUE — when the Claude
    lane breaker is OPEN, IMMEDIATE ops skip the depleted fallback and reroute to
    funded DW (live-proven). Operator can still force-off with =0. NEVER raises."""
    try:
        return os.environ.get(
            "JARVIS_FALLBACK_SKIP_GATE_ENABLED", "true",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def _dw_autarky_enabled() -> bool:
    """Slice 225 Phase 2 master. Default-TRUE — when the Claude fallback breaker
    is OPEN/HALF_OPEN (terminal_quota / out-of-credits / transport), STANDARD and
    COMPLEX ops keep the DW primary on the full op budget instead of severing it
    at the 30s/75s reflex cap into a dead lane (the live GOAL-001::file-00
    generation_failed wedge). Sibling to the P2.1 IMMEDIATE-route gate above, for
    the STANDARD/COMPLEX primary-budget path. Operator force-off with =0. NEVER
    raises — fail-closed to legacy cascade."""
    try:
        return os.environ.get(
            "JARVIS_DW_AUTARKY_ENABLED", "true",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def _provider_quota_isolation_enabled() -> bool:
    """Sovereign State Isolation (2026-06-19) master. Default-TRUE — a
    provider's economic/quota death (e.g. Claude 402 'credit balance too
    low') is recorded on THAT provider's own lane breaker only, and is NOT
    allowed to trip the provider-NEUTRAL per-op circuit breaker into
    OPEN_TERMINAL. Without this, Claude's credit-death poisons the whole op
    so DW autarky can never carry it — the empirically-confirmed
    cross-provider contamination (terminal_quota 5->0 once isolated).
    Operator force-off with =0 -> byte-identical legacy. NEVER raises."""
    try:
        return os.environ.get(
            "JARVIS_PROVIDER_QUOTA_ISOLATION_ENABLED", "true",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def quota_isolation_skips_op_breaker(
    *, is_provider_economic_block: bool, isolation_enabled: bool,
) -> bool:
    """PURE predicate: should the per-op breaker trip be SKIPPED for this
    failure? True iff the failure is a provider economic block AND
    isolation is enabled — the provider's OWN lane breaker already owns the
    death, so tripping the op-neutral breaker would cross-contaminate the op
    for every other (still-viable) provider. NEVER raises."""
    return bool(is_provider_economic_block) and bool(isolation_enabled)


def immediate_reroute_to_dw(
    *,
    dw_is_primary: bool,
    gate_enabled: bool,
    claude_breaker_enabled: bool,
    claude_allows_request: bool,
) -> bool:
    """Pure decision: should an IMMEDIATE op reroute from Claude-direct to the
    DW primary? True iff DW is the primary lane, the gate is on, the Claude lane
    breaker is enabled, and the breaker is NOT allowing requests (OPEN within
    its window). When the breaker allows (CLOSED, or a HALF_OPEN probe), we keep
    Claude-direct so the lane self-heals. Pure — no I/O, no side effects."""
    return bool(
        dw_is_primary
        and gate_enabled
        and claude_breaker_enabled
        and not claude_allows_request
    )


# ---------------------------------------------------------------------------
# Content failure classification
# ---------------------------------------------------------------------------

# Keywords that identify content/model failures vs infrastructure failures.
# Content failures do NOT trigger FailbackFSM state transitions — the primary
# provider is still alive; it merely produced bad output (stale diff, invalid
# schema, etc.).  Infrastructure failures (timeout, connection error) DO
# trigger state transitions.
_CONTENT_FAILURE_PATTERNS: frozenset = frozenset({
    "diff_apply_failed",
    "stale_diff",
    "schema_invalid",
    "no_candidates",
    "validate_diff",
    "StaleDiffError",
})


# Defect #4 Slice A (2026-05-03) — task-leak prevention.
#
# Soak v5 (bt-2026-05-03-060330) recorded 4 "Task exception was never
# retrieved" asyncio errors. Root cause: ensure_future/create_task
# spawns of provider .generate() coroutines were wrapped in
# asyncio.shield(...) which prevents cancellation when the outer
# wait_for times out. The shielded task continues running; if it
# later raises (e.g., RuntimeError('all_providers_exhausted')) and
# nobody awaits the result, asyncio's default handler logs the
# unhandled exception.
#
# Fix: every ensure_future/create_task of .generate() (or background
# poll wrappers) gets _swallow_task_exception attached as a
# done_callback. The callback retrieves the exception, classifies
# it, and either logs at DEBUG (expected: all_providers_exhausted /
# CancelledError / TimeoutError) or WARNING (unexpected). The task
# exception is consumed either way.

_EXPECTED_BACKGROUND_EXC_PATTERNS = (
    "all_providers_exhausted",
    "deadline_exhausted_pre_fallback",
    "topology_block",
    "fallback_disabled_by_env",
    "queue_only_dispatch",
)


def _swallow_task_exception(task: "asyncio.Future") -> None:
    """Done-callback that retrieves + classifies + consumes a task
    exception so it never reaches asyncio's default handler.

    Attach to every ``asyncio.ensure_future(...)`` /
    ``asyncio.create_task(...)`` of provider .generate() or
    background poll coroutines that may outlive their primary
    awaiter (e.g., shielded tasks that survive outer wait_for
    timeouts).

    NEVER raises -- contract: even a misbehaving exception accessor
    must not propagate.
    """
    try:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        msg = str(exc) if exc else ""
        # Expected provider/orchestration exceptions: log at DEBUG.
        # The exception was already logged at the raise site; this
        # callback exists to CONSUME the exception, not re-log it.
        is_expected = (
            isinstance(exc, asyncio.CancelledError)
            or isinstance(exc, asyncio.TimeoutError)
            or any(p in msg for p in _EXPECTED_BACKGROUND_EXC_PATTERNS)
        )
        if is_expected:
            logger.debug(
                "[CandidateGenerator] background task expected exit: "
                "%s(%s)", type(exc).__name__, msg[:120],
            )
        else:
            logger.warning(
                "[CandidateGenerator] background task unhandled "
                "exception (consumed by _swallow_task_exception to "
                "prevent asyncio leak): %s(%s)",
                type(exc).__name__, msg[:200],
            )
    except Exception:  # noqa: BLE001 -- contract: never crash callback
        pass


def _is_content_failure(exc: BaseException) -> bool:
    """Return True if *exc* is a content/model failure (not infrastructure).

    Content failures: wrong diff, stale context, invalid JSON schema.
    Infrastructure failures: timeout, connection refused, OOM.
    """
    msg = str(exc).lower()
    return any(pattern.lower() in msg for pattern in _CONTENT_FAILURE_PATTERNS)


# ---------------------------------------------------------------------------
# Exhaustion log helpers
# ---------------------------------------------------------------------------


def _trim_exc_msg(exc: BaseException, limit: int = 200) -> str:
    """Stringify *exc*, clip to *limit* chars, collapse whitespace."""
    msg = str(exc)
    if len(msg) > limit:
        msg = msg[:limit] + "..."
    return msg.replace("\n", "\\n").replace("\t", " ")


def _fmt_val(value: Any) -> str:
    """Format *value* for a ``key=value`` structured log line.

    Values with whitespace are underscored so grep-based audits can
    treat one log line as a flat sequence of ``key=value`` tokens.
    """
    s = str(value)
    return s.replace(" ", "_")


# ---------------------------------------------------------------------------
# Local-tier failure classifier (Phase 3 Task 7)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LocalFailureVerdict:
    degrade: bool
    cascade_upstream: bool
    target_state: Optional[str]


def classify_local_failure(exc: BaseException) -> LocalFailureVerdict:
    """Map a local-tier exception to an FSM transition verdict.

    A terminal_lag_lockup degrades J-Prime to PRIMARY_DEGRADED and cascades the
    op upstream (the FailbackStateMachine already passes context on cascade, so no
    L2 sandbox teardown). All other exceptions are ordinary provider failures.
    """
    _LOCAL_DEGRADE_CLASSES = ("terminal_lag_lockup", "local_memory_critical")
    if getattr(exc, "failure_class", None) in _LOCAL_DEGRADE_CLASSES:
        return LocalFailureVerdict(
            degrade=True, cascade_upstream=True, target_state="PRIMARY_DEGRADED"
        )
    return LocalFailureVerdict(degrade=False, cascade_upstream=False, target_state=None)


# ---------------------------------------------------------------------------
# CandidateProvider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CandidateProvider(Protocol):
    """Runtime-checkable protocol for code generation backends.

    Any class that implements these methods can serve as a primary or
    fallback generation provider.
    """

    @property
    def provider_name(self) -> str:
        """Human-readable name of this provider (e.g. ``"gcp-jprime"``)."""
        ...  # pragma: no cover

    async def generate(
        self, context: OperationContext, deadline: datetime
    ) -> GenerationResult:
        """Generate candidate code changes for the given operation.

        Parameters
        ----------
        context:
            The operation context describing what needs to change.
        deadline:
            Absolute UTC deadline by which generation must complete.

        Returns
        -------
        GenerationResult
            The generated candidates with timing metadata.

        Raises
        ------
        Exception
            Any failure (timeout, OOM, network) should propagate as an exception.
        """
        ...  # pragma: no cover

    async def health_probe(self) -> bool:
        """Quick liveness check.

        Returns
        -------
        bool
            ``True`` if the provider is healthy and ready to serve requests.
        """
        ...  # pragma: no cover

    async def plan(self, prompt: str, deadline: datetime) -> str:
        """Send a lightweight planning prompt; return the raw string response.

        Used by ContextExpander. Planning failures are soft — callers tolerate
        exceptions and skip expansion rounds gracefully.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# FailbackState Enum
# ---------------------------------------------------------------------------


class FailbackState(Enum):
    """States in the failover/failback state machine."""

    PRIMARY_READY = auto()
    FALLBACK_ACTIVE = auto()
    PRIMARY_DEGRADED = auto()
    QUEUE_ONLY = auto()


class FailureMode(Enum):
    """Classification of provider failure for recovery prediction.

    Different failure modes have vastly different recovery profiles:
    rate limits clear in seconds, connection errors take minutes to hours.
    The FSM uses this to predict when the primary will be available again,
    minimizing expensive fallback spend (Manifesto §5 — deterministic routing).
    """

    RATE_LIMITED = auto()       # 429, CircuitBreakerOpen — seconds to recover
    TIMEOUT = auto()            # Request/connection timeout — minutes
    SERVER_ERROR = auto()       # 500/502/503 — minutes
    CONNECTION_ERROR = auto()   # Can't reach host — minutes to hours
    CONTENT_FAILURE = auto()    # Bad output, infra healthy — no penalty
    CONTEXT_OVERFLOW = auto()   # Tool loop prompt exceeded char limit — immediate fallback
    TRANSIENT_TRANSPORT = auto()  # HTTP/2 disconnect, premature stream close — seconds


# Mode-specific recovery parameters for exponential backoff.
# base_s * 2^(consecutive_failures - 1), capped at max_s.
_RECOVERY_PARAMS: dict[FailureMode, dict[str, float]] = {
    FailureMode.RATE_LIMITED:    {"base_s": 15.0,  "max_s": 120.0},
    FailureMode.TIMEOUT:         {"base_s": 45.0,  "max_s": 300.0},
    FailureMode.SERVER_ERROR:    {"base_s": 60.0,  "max_s": 600.0},
    FailureMode.CONNECTION_ERROR: {"base_s": 120.0, "max_s": 900.0},
    FailureMode.CONTENT_FAILURE: {"base_s": 0.0,   "max_s": 0.0},
    # CONTEXT_OVERFLOW: Tool loop prompt exceeded char limit. The provider
    # infrastructure is healthy — the prompt was just too large. Immediate
    # fallback to Tier 1 with zero backoff penalty (same profile as
    # CONTENT_FAILURE). No timeout ETA penalty on the FSM.
    FailureMode.CONTEXT_OVERFLOW: {"base_s": 0.0,  "max_s": 0.0},
    # TRANSIENT_TRANSPORT: HTTP/2 GOAWAY, RemoteProtocolError, ClosedResourceError.
    # The transport layer flapped (often a single dropped connection in a keep-alive
    # pool) but the upstream API is healthy. A 5s base backs off to 30s after 4
    # consecutive failures, then immediately retries — much shorter than TIMEOUT
    # (45s/300s) which it would otherwise be misclassified as. Diagnosed in
    # bt-2026-04-12-005521 where 9 consecutive ops died with all_providers_exhausted
    # because RemoteProtocolError fell through to the TIMEOUT default and the
    # CONNECTION_ERROR-only deep-backoff guard never engaged.
    FailureMode.TRANSIENT_TRANSPORT: {"base_s": 5.0, "max_s": 30.0},
}


# Exception class names that indicate transient transport-layer flap rather than
# upstream API failure. Match by name (not isinstance) so we don't pull in httpx
# or anyio at module import time — the actual SDK may not be installed on hosts
# where the FSM is constructed (battle test harness, planner-only deployments).
_TRANSIENT_TRANSPORT_NAMES: frozenset = frozenset({
    "RemoteProtocolError",     # httpx — server disconnected without response
    "ClosedResourceError",     # anyio — stream got closed mid-read
    "ProtocolError",           # h11/h2 — generic protocol violation
    "LocalProtocolError",      # h11 — local-side protocol violation
    "IncompleteRead",          # http.client — short read
    "StreamConsumed",          # httpx — re-read of consumed stream
    "StreamClosed",            # httpx — read after close
    "ResponseNotRead",         # httpx — async stream race
})


# FailureMode set safe to retry from `_call_fallback`'s outer loop. Any
# mode in this set indicates a transient infrastructure condition where
# re-invoking the provider may succeed on a fresh TCP connection / fresh
# pool generation. Permanent failure modes (CONTENT_FAILURE,
# CONTEXT_OVERFLOW) MUST NOT be retried — they would just re-fail.
# Defined as a frozenset (not the FailureMode enum directly) to avoid
# import ordering with the FailureMode definition below; populated lazily
# by `_is_outer_retry_eligible_mode()`.
_FALLBACK_OUTER_RETRY_TRANSIENT_MODE_NAMES: frozenset = frozenset({
    "TIMEOUT",
    "CONNECTION_ERROR",
    "TRANSIENT_TRANSPORT",
    "SERVER_ERROR",
    "RATE_LIMITED",
})


def _is_outer_retry_eligible_mode(mode: "FailureMode") -> bool:
    """Return True iff ``mode`` indicates a transient failure worth
    retrying within the remaining fallback budget.

    Used by `_call_fallback`'s outer retry loop (rooted-problem fix
    2026-04-25). Defined as a free function so unit tests can pin the
    classification → retry decision without instantiating the full
    `CandidateGenerator`.
    """
    return mode.name in _FALLBACK_OUTER_RETRY_TRANSIENT_MODE_NAMES


def _walk_exception_chain(exc: BaseException, max_depth: int = 8) -> tuple:
    """Walk __cause__/__context__ chain returning a tuple of exceptions.

    Anthropic SDK wraps httpx exceptions in APIConnectionError; the inner
    httpx exception is the actual signal we need to classify. Walks both
    __cause__ (explicit `raise X from Y`) and __context__ (implicit during
    `except` handler), with cycle protection.

    Returns the chain ordered outermost-first.
    """
    chain: list = []
    seen: set = set()
    current: Optional[BaseException] = exc
    depth = 0
    while current is not None and depth < max_depth:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(current)
        # Prefer __cause__ (explicit) over __context__ (implicit).
        nxt = getattr(current, "__cause__", None)
        if nxt is None:
            nxt = getattr(current, "__context__", None)
        current = nxt
        depth += 1
    return tuple(chain)


# ---------------------------------------------------------------------------
# FailbackStateMachine
# ---------------------------------------------------------------------------


class FailbackStateMachine:
    """Asymmetric failover/failback state machine.

    Failover is immediate (one failure), but failback requires
    ``required_probes`` consecutive health probes spanning at least
    ``dwell_time_s`` seconds.

    Parameters
    ----------
    required_probes:
        Number of consecutive successful health probes needed before
        promoting from PRIMARY_DEGRADED to PRIMARY_READY.
    dwell_time_s:
        Minimum wall-clock seconds that must elapse between the first
        successful probe and the promotion to PRIMARY_READY.
    """

    def __init__(
        self,
        required_probes: int = 3,
        dwell_time_s: float = 45.0,
    ) -> None:
        self._state: FailbackState = FailbackState.PRIMARY_READY
        self._required_probes: int = required_probes
        self._dwell_time_s: float = dwell_time_s
        self._consecutive_probes: int = 0
        self._first_probe_at: Optional[float] = None  # monotonic timestamp
        self.content_failure_count: int = 0  # content/model failures (not infra)
        # Adaptive recovery tracking (Manifesto §5 — deterministic routing)
        self._failure_mode: Optional[FailureMode] = None
        self._consecutive_failures: int = 0
        self._last_failure_at: float = 0.0   # monotonic
        self._last_success_at: float = 0.0   # monotonic

    @property
    def state(self) -> FailbackState:
        """Current FSM state."""
        return self._state

    def record_primary_failure(
        self, mode: FailureMode = FailureMode.TIMEOUT,
    ) -> None:
        """Record a primary provider failure with failure mode classification.

        Transitions immediately to FALLBACK_ACTIVE from any non-QUEUE_ONLY state.
        Tracks failure mode for recovery prediction (Manifesto §5).

        Parameters
        ----------
        mode:
            Classification of the failure. Defaults to TIMEOUT for backward
            compatibility with existing callers.
        """
        if self._state is FailbackState.QUEUE_ONLY:
            return
        if self._state in (
            FailbackState.PRIMARY_READY,
            FailbackState.FALLBACK_ACTIVE,
            FailbackState.PRIMARY_DEGRADED,
        ):
            self._state = FailbackState.FALLBACK_ACTIVE
            # Track failure mode for adaptive recovery — do NOT reset these
            # in _reset_probe_counters; they persist across probe cycles.
            self._failure_mode = mode
            self._consecutive_failures += 1
            self._last_failure_at = time.monotonic()
            self._reset_probe_counters()
            params = _RECOVERY_PARAMS.get(mode, _RECOVERY_PARAMS[FailureMode.TIMEOUT])
            # Phase 12.2 Slice C — full-jitter retrofit. Master-flag-off
            # preserves exact-exponential bit-for-bit. When enabled,
            # uniform jitter desynchronizes our probe waveform from
            # other JARVIS-class clients hammering the same DW endpoint
            # after recovery.
            try:
                from backend.core.ouroboros.governance.full_jitter import (
                    full_jitter_backoff_s,
                    full_jitter_enabled,
                )
                if full_jitter_enabled():
                    eta_s = full_jitter_backoff_s(
                        max(self._consecutive_failures - 1, 0),
                        base_s=params["base_s"],
                        cap_s=params["max_s"],
                    )
                else:
                    eta_s = min(
                        params["base_s"] * (2 ** max(self._consecutive_failures - 1, 0)),
                        params["max_s"],
                    )
            except Exception:  # noqa: BLE001 — defensive
                eta_s = min(
                    params["base_s"] * (2 ** max(self._consecutive_failures - 1, 0)),
                    params["max_s"],
                )
            logger.warning(
                "[FailbackFSM] Primary failure (mode=%s, consecutive=%d, "
                "recovery_eta=+%.0fs) -> FALLBACK_ACTIVE",
                mode.name, self._consecutive_failures, eta_s,
            )

    def record_fallback_failure(
        self, mode: FailureMode = FailureMode.TIMEOUT,
    ) -> None:
        """Record a fallback provider failure.

        FALLBACK_ACTIVE -> QUEUE_ONLY for permanent failures.
        For transient failures (TIMEOUT, RATE_LIMITED), stays in
        FALLBACK_ACTIVE so the system can retry on the next operation
        instead of permanently giving up.
        """
        if self._state is not FailbackState.FALLBACK_ACTIVE:
            return

        if mode in (FailureMode.TIMEOUT, FailureMode.RATE_LIMITED,
                    FailureMode.SERVER_ERROR, FailureMode.CONTEXT_OVERFLOW,
                    FailureMode.CONTENT_FAILURE):
            # Transient / non-infra: DON'T go to QUEUE_ONLY. The next
            # operation will re-evaluate should_attempt_primary() and may
            # succeed. CONTEXT_OVERFLOW is a prompt-size issue, not infra.
            logger.warning(
                "[FailbackFSM] Fallback transient failure (mode=%s) — "
                "staying FALLBACK_ACTIVE (recoverable)",
                mode.name,
            )
            return

        # Permanent failure (CONNECTION_ERROR, auth, unknown) → QUEUE_ONLY
        self._state = FailbackState.QUEUE_ONLY
        self._queue_only_at: float = time.monotonic()
        self._reset_probe_counters()
        logger.error(
            "[FailbackFSM] Fallback failure (mode=%s) -> QUEUE_ONLY "
            "(all providers exhausted)",
            mode.name,
        )

    def record_probe_success(self) -> None:
        """Record a successful health probe of the primary provider.

        FALLBACK_ACTIVE -> PRIMARY_DEGRADED (first probe).
        PRIMARY_DEGRADED stays until required_probes AND dwell_time_s met,
        then -> PRIMARY_READY.
        PRIMARY_READY -> no-op.
        QUEUE_ONLY -> FALLBACK_ACTIVE (auto-recovery: a successful probe
        means the primary is alive again, so we should exit the dead-end).
        """
        if self._state is FailbackState.PRIMARY_READY:
            return
        if self._state is FailbackState.QUEUE_ONLY:
            # Auto-recovery: primary is alive → exit dead-end
            self._state = FailbackState.FALLBACK_ACTIVE
            self._reset_probe_counters()
            elapsed = time.monotonic() - getattr(self, "_queue_only_at", 0.0)
            logger.info(
                "[FailbackFSM] QUEUE_ONLY auto-recovery: probe succeeded "
                "after %.1fs — transitioning to FALLBACK_ACTIVE",
                elapsed,
            )
            # Fall through to the FALLBACK_ACTIVE handler below
            # so the first probe is counted toward PRIMARY_DEGRADED.

        now = time.monotonic()

        if self._state is FailbackState.FALLBACK_ACTIVE:
            # First probe: transition to PRIMARY_DEGRADED
            self._state = FailbackState.PRIMARY_DEGRADED
            self._consecutive_probes = 1
            self._first_probe_at = now
            logger.info(
                "[FailbackFSM] First probe success -> PRIMARY_DEGRADED (1/%d)",
                self._required_probes,
            )
            self._maybe_promote(now)
            return

        if self._state is FailbackState.PRIMARY_DEGRADED:
            self._consecutive_probes += 1
            logger.info(
                "[FailbackFSM] Probe success (%d/%d)",
                self._consecutive_probes,
                self._required_probes,
            )
            self._maybe_promote(now)

    def record_probe_failure(self) -> None:
        """Record a failed health probe of the primary provider.

        PRIMARY_DEGRADED -> FALLBACK_ACTIVE (resets probe counters).
        Other states: no-op.
        """
        if self._state is FailbackState.PRIMARY_DEGRADED:
            self._state = FailbackState.FALLBACK_ACTIVE
            self._reset_probe_counters()
            logger.warning(
                "[FailbackFSM] Probe failure -> FALLBACK_ACTIVE (reset)"
            )

    def _maybe_promote(self, now: float) -> None:
        """Check if promotion criteria (probes + dwell) are met."""
        if self._state is not FailbackState.PRIMARY_DEGRADED:
            return
        if self._consecutive_probes < self._required_probes:
            return
        if self._first_probe_at is not None:
            elapsed = now - self._first_probe_at
            if elapsed < self._dwell_time_s:
                logger.info(
                    "[FailbackFSM] Probes met (%d/%d) but dwell not satisfied "
                    "(%.1fs / %.1fs)",
                    self._consecutive_probes,
                    self._required_probes,
                    elapsed,
                    self._dwell_time_s,
                )
                return
        # All criteria met
        self._state = FailbackState.PRIMARY_READY
        self._reset_probe_counters()
        self._reset_failure_tracking()
        logger.info("[FailbackFSM] Promoted -> PRIMARY_READY")

    def _reset_probe_counters(self) -> None:
        """Reset probe tracking state."""
        self._consecutive_probes = 0
        self._first_probe_at = None

    def _reset_failure_tracking(self) -> None:
        """Reset adaptive recovery state on successful recovery."""
        self._consecutive_failures = 0
        self._failure_mode = None
        self._last_success_at = time.monotonic()

    def record_primary_success(self) -> None:
        """Record a successful primary generation (explicit recovery signal).

        Called when the primary provider successfully generates candidates
        after a period of failure. Resets all failure tracking so subsequent
        failures start fresh with base-level backoff.
        """
        if self._consecutive_failures > 0:
            recovery_duration = time.monotonic() - self._last_failure_at
            logger.info(
                "[FailbackFSM] Primary recovered (was %s, %d consecutive failures, "
                "recovery took %.1fs)",
                self._failure_mode.name if self._failure_mode else "UNKNOWN",
                self._consecutive_failures,
                recovery_duration,
            )
        self._reset_failure_tracking()

    # ------------------------------------------------------------------
    # Recovery prediction (deterministic — Manifesto §5)
    # ------------------------------------------------------------------

    def recovery_eta(self) -> float:
        """Predicted monotonic timestamp when primary will be available.

        Uses mode-specific exponential backoff:
        ``last_failure_at + base_s * 2^(consecutive_failures - 1)``,
        capped at ``max_s``.

        Returns 0.0 if no failures recorded (primary is healthy).
        """
        if self._consecutive_failures == 0 or self._failure_mode is None:
            return 0.0
        if self._failure_mode is FailureMode.CONTENT_FAILURE:
            return time.monotonic()  # instant — no infra penalty
        params = _RECOVERY_PARAMS.get(
            self._failure_mode, _RECOVERY_PARAMS[FailureMode.TIMEOUT],
        )
        # Phase 12.2 Slice C — full-jitter retrofit (matches the sister
        # callsite in record_primary_failure). Master-flag-off preserves
        # exact-exponential bit-for-bit; on, uniform random delay
        # desynchronizes our probe schedule from the global herd.
        try:
            from backend.core.ouroboros.governance.full_jitter import (
                full_jitter_backoff_s,
                full_jitter_enabled,
            )
            if full_jitter_enabled():
                delay = full_jitter_backoff_s(
                    max(self._consecutive_failures - 1, 0),
                    base_s=params["base_s"],
                    cap_s=params["max_s"],
                )
            else:
                delay = min(
                    params["base_s"] * (2 ** max(self._consecutive_failures - 1, 0)),
                    params["max_s"],
                )
        except Exception:  # noqa: BLE001 — defensive
            delay = min(
                params["base_s"] * (2 ** max(self._consecutive_failures - 1, 0)),
                params["max_s"],
            )
        return self._last_failure_at + delay

    def should_attempt_primary(self) -> bool:
        """Should we try the primary (cheap) provider?

        Returns True if the primary is healthy or the predicted recovery
        window has elapsed. This enables cost-aware routing: always prefer
        the cheap provider when it's likely available.
        """
        if self._state is FailbackState.PRIMARY_READY:
            return True
        if self._consecutive_failures == 0:
            return True
        return time.monotonic() >= self.recovery_eta()

    def recommended_probe_interval(self) -> float:
        """Adaptive probe interval based on distance to recovery ETA.

        - Far from ETA (>60s away): 60s (relax — no point hammering)
        - Near ETA (<30s away): 10s (ramp up — catch recovery fast)
        - Past ETA: 5s (aggressive — recovery is imminent)
        - Primary healthy: 30s (normal cadence)

        Returns seconds to sleep before next health probe.
        """
        if self._state is FailbackState.PRIMARY_READY:
            return 30.0
        if self._consecutive_failures == 0:
            return 30.0

        eta = self.recovery_eta()
        distance = eta - time.monotonic()

        if distance > 60.0:
            return 60.0   # Deep backoff — relax probes
        elif distance > 30.0:
            return 20.0   # Approaching — moderate
        elif distance > 0.0:
            return 10.0   # Close — ramp up
        else:
            return 5.0    # Past ETA — aggressive probe

    @staticmethod
    def classify_exception(exc: BaseException) -> FailureMode:
        """Classify an exception into a failure mode for recovery prediction.

        Walks the ``__cause__`` / ``__context__`` chain because the Anthropic SDK
        (and other modern HTTP clients) wraps low-level transport errors in a
        higher-level wrapper class — e.g. ``APIConnectionError(cause=
        RemoteProtocolError("Server disconnected without sending a response."))``.
        Classifying only the outer wrapper would have us treat a 50ms HTTP/2
        keep-alive flap as a 120s CONNECTION_ERROR deep-backoff. Instead we walk
        every layer and let the most specific (transient transport) classification
        win.

        Uses string-based type checking to avoid hard dependency on httpx/anyio.
        """
        # Content failures first (don't penalize infra). Check the outermost
        # exception's full message — content failure markers are stamped on
        # the wrapper (e.g. RuntimeError("diff_apply_failed: ...")).
        if _is_content_failure(exc):
            return FailureMode.CONTENT_FAILURE

        # Stream Rupture Breaker: the typed exception carries a
        # provider_stream_rupture:... message. Classify as TRANSIENT_TRANSPORT
        # so the FSM uses the short 5s/30s recovery profile and cascades
        # to Tier 1 immediately.
        #
        # Slice 12F-B (2026-05-22) — StreamBudgetTooShortError is the
        # diagnostic sibling: not a network-side rupture, but a local
        # decision to refuse dispatch when wall_remaining < the
        # JARVIS_STREAM_MINIMUM_READ_BUDGET_S floor. Same classifier
        # mapping (TRANSIENT_TRANSPORT) — same Slice 7 fallback
        # behaviour — but the postmortem can tell the two apart.
        from backend.core.ouroboros.governance.stream_rupture import (
            StreamBudgetTooShortError,
            StreamRuptureError,
        )
        if isinstance(
            exc, (StreamRuptureError, StreamBudgetTooShortError),
        ):
            return FailureMode.TRANSIENT_TRANSPORT

        chain = _walk_exception_chain(exc)

        # First pass: any layer that names a known transient transport class
        # wins, regardless of how deep it is. This is the highest-priority
        # signal because the recovery profile (5s base / 30s max) is so much
        # cheaper than CONNECTION_ERROR (120s/900s).
        for layer in chain:
            if type(layer).__name__ in _TRANSIENT_TRANSPORT_NAMES:
                return FailureMode.TRANSIENT_TRANSPORT

        # Second pass: classic classification on the outermost exception.
        # Falls through layers using the existing rules.
        for layer in chain:
            mode = FailbackStateMachine._classify_single(layer)
            if mode is not FailureMode.TIMEOUT:
                # Anything more specific than the conservative TIMEOUT default
                # is preferred — e.g. an inner ConnectionError beats an outer
                # asyncio.TimeoutError because the connection layer is closer
                # to the truth.
                return mode

        # All layers landed on the conservative TIMEOUT default.
        return FailureMode.TIMEOUT

    @staticmethod
    def _classify_single(exc: BaseException) -> FailureMode:
        """Classify a single exception (no chain walking).

        Extracted from ``classify_exception`` so the chain walker can call
        it on each layer. Preserves the original classification rules
        verbatim minus the transient-transport handling (which is checked
        separately in the priority-1 pass).
        """
        exc_type = type(exc).__name__
        msg = str(exc).lower()

        # DoublewordInfraError carries a status code
        if exc_type == "DoublewordInfraError":
            status = getattr(exc, "status_code", 0)
            if status == 429:
                return FailureMode.RATE_LIMITED
            if status in (500, 502, 503):
                return FailureMode.SERVER_ERROR
            # status 0 or other — fall through to message analysis

        # Rate limiting signals
        if exc_type == "CircuitBreakerOpen":
            return FailureMode.RATE_LIMITED
        if "429" in msg or "rate" in msg or "too many" in msg:
            return FailureMode.RATE_LIMITED

        # Context overflow — tool loop prompt exceeded char limit.
        # Must be checked before server errors because the char count
        # in the message (e.g. "155000") can contain "500".
        if "tool_loop_budget_exceeded" in msg or "tool_loop_context_overflow" in msg:
            return FailureMode.CONTEXT_OVERFLOW

        # Connection errors
        if isinstance(exc, ConnectionError):
            return FailureMode.CONNECTION_ERROR
        if any(kw in msg for kw in ("connection", "refused", "dns", "unreachable")):
            return FailureMode.CONNECTION_ERROR
        if exc_type in (
            "ClientConnectionError", "ServerDisconnectedError",
            "ClientConnectorError",
        ):
            return FailureMode.CONNECTION_ERROR

        # Server errors
        if any(code in msg for code in ("500", "502", "503")):
            return FailureMode.SERVER_ERROR
        if exc_type == "ClientResponseError":
            status = getattr(exc, "status", 0)
            if status in (500, 502, 503):
                return FailureMode.SERVER_ERROR
            if status == 429:
                return FailureMode.RATE_LIMITED

        # Timeouts
        if isinstance(exc, (asyncio.TimeoutError,)):
            return FailureMode.TIMEOUT
        if "timeout" in msg:
            return FailureMode.TIMEOUT
        if exc_type in ("ServerTimeoutError", "ConnectionTimeoutError"):
            return FailureMode.TIMEOUT

        # Conservative default
        return FailureMode.TIMEOUT


# ---------------------------------------------------------------------------
# CandidateGenerator
# ---------------------------------------------------------------------------


class CandidateGenerator:
    """Orchestrates candidate generation with failover and concurrency control.

    Routes generation requests to the primary provider when healthy, falling
    back to the fallback provider on failure.  Each provider has its own
    :class:`asyncio.Semaphore` for concurrency limiting.

    Parameters
    ----------
    primary:
        The preferred (typically remote/powerful) generation provider.
    fallback:
        The backup (typically local/smaller) generation provider.
    primary_concurrency:
        Maximum concurrent calls to the primary provider.
    fallback_concurrency:
        Maximum concurrent calls to the fallback provider.
    """

    def __init__(
        self,
        primary: CandidateProvider,
        fallback: Optional[CandidateProvider] = None,
        primary_concurrency: int = 4,
        fallback_concurrency: int = 2,
        tier0: Optional[Any] = None,  # DoublewordProvider (batch, async)
        ledger: Optional[Any] = None,  # OperationLedger for batch traceability
        latency_tracker: Optional[DwLatencyTracker] = None,
        exhaustion_watcher: Optional[Any] = None,  # ProviderExhaustionWatcher
        jprime: Optional[Any] = None,  # PrimeProvider (Phase 3 Scope α primacy)
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._tier0 = tier0
        # Phase 3 Scope α — J-Prime primacy handle. Only consulted from
        # the BACKGROUND and SPECULATIVE dispatch paths, and only when
        # ``JARVIS_JPRIME_PRIMACY=true``. Can be ``None`` — primacy is
        # opt-in and test fixtures often don't build a PrimeProvider.
        # When the caller doesn't hand one in but ``self._primary`` is
        # already a PrimeProvider (the usual production wiring), we
        # detect and reuse it below to keep the API minimal for
        # existing call sites that don't know about Scope α yet.
        self._jprime = jprime
        if self._jprime is None and primary is not None and getattr(
            primary, "provider_name", ""
        ) == "gcp-jprime":
            self._jprime = primary
        self._ledger = ledger
        self._fallback_concurrency = fallback_concurrency
        # HIBERNATION_MODE step 5: optional watcher that counts
        # consecutive all_providers_exhausted raises and transitions
        # the SupervisorOuroborosController into HIBERNATION at the
        # configured threshold. Kept structural/optional so unit tests
        # of CandidateGenerator don't need to build a controller.
        self._exhaustion_watcher = exhaustion_watcher
        # Cap concurrent background polls to avoid connector exhaustion
        self._max_background_polls: int = 3
        # Per-op tier rotation config: belt-and-suspenders for the FSM
        # ETA-based skip. When the classifier mis-routes (e.g. an
        # unfamiliar wrapper exception falls through to TIMEOUT instead
        # of TRANSIENT_TRANSPORT) the FSM's `should_attempt_primary()`
        # keeps returning True and consecutive ops all hit the same
        # dead Tier 0. Once N failures land within W seconds, we
        # hard-skip Tier 0 for the next op regardless of FSM mode —
        # buying the human one cheap Claude success while DW recovers.
        self._tier0_skip_threshold: int = int(
            os.environ.get("OUROBOROS_TIER0_SKIP_THRESHOLD", "2")
        )
        self._tier0_skip_window_s: float = float(
            os.environ.get("OUROBOROS_TIER0_SKIP_WINDOW_S", "30")
        )

        # AdmissionGate Slice 2 — per-route rolling EWMA of
        # observed _fallback_sem wait times. Feeds the
        # admission gate's projected_wait_s input. Updated
        # post-acquire in _call_fallback after every successful
        # sem.acquire(). Master flag default-FALSE until Slice 3
        # graduation, so the gate is constructed but doesn't
        # change behavior — pre-Slice-2 path preserved when
        # disabled.
        try:
            from backend.core.ouroboros.governance.admission_estimator import (  # noqa: E501
                WaitTimeEstimator as _WaitTimeEstimator,
            )
            self._wait_estimator = _WaitTimeEstimator()
        except Exception:  # noqa: BLE001 — defensive
            self._wait_estimator = None

        # ── Phase 1 Step 3A: state hoist (un-quarantine blueprint) ──
        # Invariant: every mutable field that must survive
        # `importlib.reload(candidate_generator)` lives on `self._state`
        # (a ``GeneratorState``), not on ``self`` directly. The aliases
        # below are bound once in __init__ and share reference identity
        # with the state container — for dicts/FSM/sem that is enough;
        # the ``int``/``float`` counters live on ``self._counters`` (a
        # ``GeneratorCounters`` dataclass) so mutation-via-attribute
        # does not re-bind a local copy. Do NOT add new mutable fields
        # as ``self._*`` — extend ``GeneratorState`` instead.
        #
        # When ``JARVIS_UNQUARANTINE_GENERATOR`` is false (default), the
        # state is minted fresh per instance so today's tests and
        # production behavior stay bit-identical. Flipping the env to
        # true routes every new ``CandidateGenerator`` to the shared
        # singleton and retires the quarantine (follow-up PR).
        from ._governance_state import (
            GeneratorState,
            get_generator_state,
            unquarantine_generator_enabled,
        )
        if unquarantine_generator_enabled():
            self._state = get_generator_state(
                primary_concurrency=primary_concurrency,
                fallback_concurrency=fallback_concurrency,
                latency_tracker=latency_tracker,
            )
        else:
            self._state = GeneratorState.fresh(
                primary_concurrency=primary_concurrency,
                fallback_concurrency=fallback_concurrency,
                latency_tracker=latency_tracker,
            )
        # Aliases: all share reference identity with self._state so
        # reads AND writes via either name land on the same object.
        # Safe for Semaphores, FSM, dicts, trackers (objects / mutable
        # containers).
        self._primary_sem = self._state.primary_sem
        self._fallback_sem = self._state.fallback_sem
        self.fsm = self._state.fsm
        # Manifesto §5: rolling p95 DW RT latency → dynamic Tier 0 budget.
        # Cold endpoints get full ceiling, hot endpoints dial down aggressively.
        self._latency_tracker = self._state.latency_tracker
        # Async Tier 0 tracking: op_id → CompletedBatch (dict aliased).
        self._completed_batches: dict[str, Any] = self._state.completed_batches
        # Background polling tasks (kept to prevent GC; dict aliased).
        self._background_polls: dict[str, asyncio.Task[Any]] = (
            self._state.background_polls
        )
        # Counters container: lets ``self._counters.exhaustion_events +=
        # 1`` mutate the same dataclass instance stored on the state,
        # which a plain ``int`` alias could not. Do not rebind
        # ``self._counters`` — only mutate its fields.
        self._counters = self._state.counters

        # ── Phase 3 Scope α: J-Prime primacy state (process-lifetime) ──
        # The ``jprime_sem`` (Semaphore(1)) and ``model_stickiness``
        # placeholder MUST live on the hoisted ``JPrimeState`` even when
        # ``JARVIS_JPRIME_PRIMACY`` is off today — same binding
        # discipline as 3A/3B. Per Derek-locked middle path: never place
        # these roots on a hot ``CandidateGenerator`` instance, because
        # ``importlib.reload(candidate_generator)`` would silently reset
        # the client-side concurrency ceiling and let a burst hit the
        # 50-slot swap-transient queue at the server edge.
        #
        # ``get_jprime_state()`` is first-call-wins, so every generator
        # post-reload sees the same sem token and the same stickiness
        # dict. The alias here is reference-stable: the sem identity
        # never changes, so binding once is enough. The counters
        # container mutates in place for the same reason as
        # ``self._counters`` above.
        from ._governance_state import get_jprime_state
        self._jprime_state = get_jprime_state()
        self._jprime_sem = self._jprime_state.jprime_sem
        self._jprime_counters = self._jprime_state.counters

    def _raise_exhausted(
        self,
        cause: str,
        *,
        context: Optional[Any] = None,
        deadline: Optional[datetime] = None,
        primary_exc: Optional[BaseException] = None,
        fallback_exc: Optional[BaseException] = None,
        **breadcrumbs: Any,
    ) -> NoReturn:
        """Log a structured exhaustion breadcrumb line and raise RuntimeError.

        Never returns. Every raise of ``all_providers_exhausted`` from this
        class should go through this helper so the battle-test audit can
        grep a single log line and learn:

            * which cause fired (queue_only_dispatch, fallback_failed, ...)
            * the FailbackStateMachine state at that moment
            * the classified ``FailureMode`` of the most recent attempt
            * the route, op_id, complexity, and remaining deadline budget
            * the primary / fallback provider names
            * the underlying exception class + trimmed message (if any)
            * any cause-specific breadcrumbs passed as ``**breadcrumbs``

        The raised ``RuntimeError`` carries the full report dict as
        ``.exhaustion_report`` so downstream observers (orchestrator
        postmortem, ProviderExhaustionWatcher, ledger) can use it
        without re-parsing the log line.

        The exception message remains the stable ``"all_providers_exhausted"``
        prefix plus a ``:{cause}`` suffix, so every existing substring /
        regex match (``"all_providers_exhausted" in str(exc)``,
        ``pytest.raises(RuntimeError, match="all_providers_exhausted")``,
        and the orchestrator ``_INFRA_PATTERNS`` set) keeps working.
        """
        self._counters.exhaustion_events += 1
        try:
            # Slice 197 — durable charter counter: the graduation contract
            # reads provider exhaustions from the registry, not from logs.
            from backend.core.ouroboros.governance.observability_registry import (
                record_provider_exhaustion as _s197_record_exhaustion,
            )
            _s197_record_exhaustion()
        except Exception:  # noqa: BLE001
            pass

        fm = self.fsm._failure_mode
        report: Dict[str, Any] = {
            "event_n": self._counters.exhaustion_events,
            "cause": cause,
            "fsm_state": self.fsm.state.name,
            "fsm_failure_mode": fm.name if fm is not None else "NONE",
            "fsm_consecutive_failures": self.fsm._consecutive_failures,
            "tier0_consecutive_failures": self._counters.consecutive_tier0_failures,
            "primary_name": getattr(self._primary, "provider_name", "?"),
            "fallback_name": (
                getattr(self._fallback, "provider_name", "?")
                if self._fallback is not None else "none"
            ),
            "tier0_name": (
                getattr(self._tier0, "provider_name", "?")
                if self._tier0 is not None else "none"
            ),
        }
        if context is not None:
            report["op_id"] = (
                getattr(context, "op_id", None)
                or getattr(context, "operation_id", None)
                or "?"
            )
            report["route"] = getattr(context, "provider_route", "?") or "?"
            report["complexity"] = (
                getattr(context, "task_complexity", "?") or "?"
            )
        if deadline is not None:
            report["remaining_s"] = round(self._remaining_seconds(deadline), 2)
        if primary_exc is not None:
            report["primary_err_class"] = type(primary_exc).__name__
            report["primary_err_msg"] = _trim_exc_msg(primary_exc)
        if fallback_exc is not None:
            report["fallback_err_class"] = type(fallback_exc).__name__
            report["fallback_err_msg"] = _trim_exc_msg(fallback_exc)
        report.update(breadcrumbs)

        log_parts = " ".join(
            f"{k}={_fmt_val(v)}" for k, v in report.items()
        )
        logger.error(
            "[CandidateGenerator] EXHAUSTION %s", log_parts,
        )

        err = RuntimeError(f"all_providers_exhausted:{cause}")
        try:
            setattr(err, "exhaustion_report", report)
        except Exception:
            pass  # attribute attachment is best-effort — never mask the raise
        if fallback_exc is not None:
            raise err from fallback_exc
        if primary_exc is not None:
            raise err from primary_exc
        raise err

    async def generate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Generate candidate code changes, with automatic failover.

        Thin wrapper around :meth:`_generate_dispatch` that notifies
        the optional :class:`ProviderExhaustionWatcher` on the way out
        so the watcher can flip the controller into HIBERNATION once
        exhaustion events cross the configured threshold.

        Parameters
        ----------
        context:
            The operation context describing what needs to change.
        deadline:
            Absolute UTC deadline by which generation must complete.

        Returns
        -------
        GenerationResult
            The generated candidates from whichever provider succeeded.

        Raises
        ------
        RuntimeError
            If all providers are exhausted (``"all_providers_exhausted"``).
        asyncio.TimeoutError
            If the deadline is already past and no provider can be tried.
        """
        try:
            result = await self._generate_dispatch(context, deadline)
        except RuntimeError as exc:
            if "all_providers_exhausted" in str(exc):
                if self._exhaustion_watcher is not None:
                    try:
                        await self._exhaustion_watcher.record_exhaustion(
                            reason=str(exc),
                            op_id=getattr(context, "op_id", None) or None,
                        )
                    except Exception:
                        logger.debug(
                            "[CandidateGenerator] exhaustion_watcher "
                            "record_exhaustion failed",
                            exc_info=True,
                        )
                # Feed github_issue-sourced exhaustions into the sensor-side
                # cooldown registry so chronic unresolvable issues (e.g.
                # #16501 "Unlock Test Suite Failed" observed re-exhausting
                # across bt-2026-04-15-012736 and bt-2026-04-15-013455)
                # don't re-emit on the next scan, re-enter generation, and
                # re-exhaust — each such re-exhaustion currently counts
                # toward ExhaustionWatcher's global hibernation threshold
                # even when the reflex path is healthy. The registry is
                # module-level in the sensor file; env gate
                # JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S (default 900s,
                # set to 0 to disable). issue_key parsing is delegated to
                # issue_key_from_description so the returned key stays
                # byte-identical to the sensor's own dedup_key.
                if getattr(context, "signal_source", "") == "github_issue":
                    try:
                        from backend.core.ouroboros.governance.intake.sensors.github_issue_sensor import (
                            issue_key_from_description,
                            register_issue_exhaustion,
                        )
                        _desc = getattr(context, "description", "") or ""
                        _issue_key = issue_key_from_description(_desc)
                        if _issue_key is not None:
                            register_issue_exhaustion(
                                _issue_key, reason=str(exc)[:120]
                            )
                    except Exception:
                        logger.debug(
                            "[CandidateGenerator] github_issue cooldown "
                            "hook failed",
                            exc_info=True,
                        )
                # §3.6.2 vector #12 (2026-05-07) — Tier 3
                # deterministic fallback. When master flag on,
                # substitute a structured deferred GenerationResult
                # instead of re-raising. Prevents the organism
                # freeze when both Tier 0 + Tier 1 are out.
                # Master flag default-FALSE per §33.1 — when off,
                # byte-identical pre-slice behavior (re-raise).
                # NEVER raises into the dispatch path.
                try:
                    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
                        build_deferred_generation_result,
                        emit_substitution_telemetry,
                        should_intercept_exhaustion,
                    )
                    if should_intercept_exhaustion():
                        _deferred = build_deferred_generation_result(
                            op_id=getattr(
                                context, "op_id", "",
                            ) or "",
                            cause=str(exc)[:200],
                        )
                        if _deferred is not None:
                            emit_substitution_telemetry(
                                op_id=getattr(
                                    context, "op_id", "",
                                ) or "",
                                cause=str(exc)[:200],
                            )
                            return _deferred
                except Exception:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[CandidateGenerator] tier3 fallback "
                        "intercept failed (non-fatal); "
                        "re-raising original exhaustion",
                        exc_info=True,
                    )
                # Phase 3.3 Task 2 — J-Prime last-resort local handoff.
                # When JARVIS_JPRIME_LASTRESORT_ENABLED is true, route the op
                # to the local 3B tier with a topologically-pruned payload
                # instead of crashing the loop. Gate default OFF -> re-raise
                # (byte-identical legacy). Never masks the original error on
                # local failure or unhealthy probe.
                try:
                    from backend.core.ouroboros.governance.exhaustion_interceptor import (
                        should_intercept,
                        execute_local_last_resort,
                    )
                    if should_intercept(exc, jprime=self._jprime):
                        _ft = self._estimate_target_file_tokens(context)
                        _broker = self._resolve_sse_broker()
                        return await execute_local_last_resort(
                            jprime=self._jprime,
                            context=context,
                            deadline=deadline,
                            graph_backend=getattr(self, "_graph_backend", None),
                            broker=_broker,
                            file_tokens=_ft,
                            original_exc=exc,
                        )
                except RuntimeError:
                    # execute_local_last_resort re-raises original_exc on any
                    # local failure — let it propagate cleanly.
                    raise
                except Exception:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[CandidateGenerator] jprime lastresort "
                        "intercept failed (non-fatal); "
                        "re-raising original exhaustion",
                        exc_info=True,
                    )
            raise
        else:
            if self._exhaustion_watcher is not None:
                try:
                    await self._exhaustion_watcher.record_success()
                except Exception:
                    logger.debug(
                        "[CandidateGenerator] exhaustion_watcher "
                        "record_success failed",
                        exc_info=True,
                    )
            # Antivenom Vector 1: BG/SPEC routes structurally skip
            # Quorum (cost-gated). A single-roll candidate that
            # claims to modify code but produces an AST fingerprint
            # identical to the original is a Quine-class hallucination.
            # Filter such candidates out — empty result is a
            # correctness win (orchestrator's accept-failure branch
            # handles it gracefully, no apply, no harm).
            try:
                result = await self._apply_bg_spec_structural_filter(
                    context=context, result=result,
                )
            except Exception:  # noqa: BLE001 — never break generate()
                logger.debug(
                    "[CandidateGenerator] bg_spec_structural_filter "
                    "raised; passing through unfiltered result",
                    exc_info=True,
                )
            return result

    def _estimate_target_file_tokens(self, context: Any) -> Dict[str, int]:
        """Best-effort per-file token estimate for the exhaustion interceptor.

        Reads each file in ``context.target_files`` relative to the repo root
        (``self._repo_root`` when set, else cwd) and estimates token count as
        ``len(text) // 4``. Missing or unreadable files contribute 0. Never
        raises -- any error is swallowed so the interceptor path stays safe.
        """
        result: Dict[str, int] = {}
        try:
            files = list(getattr(context, "target_files", ()) or ())
            repo_root = getattr(self, "_repo_root", None)
            for f in files:
                try:
                    import os as _os
                    path = (
                        _os.path.join(repo_root, f)
                        if repo_root and not _os.path.isabs(f)
                        else f
                    )
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        result[f] = len(fh.read()) // 4
                except Exception:  # noqa: BLE001
                    result[f] = 0
        except Exception:  # noqa: BLE001
            pass
        return result

    def _resolve_sse_broker(self) -> Any:
        """Return the default SSE broker for beacon publishing, or None on failure."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (
                get_default_broker,
            )
            return get_default_broker()
        except Exception:  # noqa: BLE001
            return None

    async def _apply_bg_spec_structural_filter(
        self,
        *,
        context: OperationContext,
        result: GenerationResult,
    ) -> GenerationResult:
        """Antivenom Vector 1: structural Quine-class guard for
        BG/SPEC routes.

        Routes BACKGROUND/SPECULATIVE structurally skip the
        Quorum gate (``COST_GATED_ROUTES`` in
        ``cost_contract_assertion``). That leaves single-roll
        generation with no consensus check — a hallucinated
        candidate whose AST equals the original (different text,
        same shape) can ship.

        This filter runs ``compute_bg_spec_structural_check`` on
        each candidate's ``(file_path, full_content)`` pair (or
        each entry in a multi-file candidate's ``files`` list)
        against the on-disk original. Anomaly → drop the
        candidate. New files (no on-disk original) are passed
        through (no AST to compare).

        Cost: zero LLM calls. AST signature compute is bounded
        by file size; runs in a thread to avoid blocking the
        event loop. Master gate
        ``JARVIS_BG_SPEC_STRUCTURAL_CHECK_ENABLED`` (default
        ``true``) lives on the primitive in
        ``generative_quorum_gate``."""
        try:
            route = (
                getattr(context, "provider_route", "") or ""
            ).strip().lower()
            if route not in ("background", "speculative"):
                return result
            if not result.candidates:
                return result

            # Lazy import: keep generative_quorum_gate out of the
            # hot import path for non-BG/SPEC ops.
            try:
                from backend.core.ouroboros.governance.verification.generative_quorum_gate import (
                    compute_bg_spec_structural_check,
                )
            except ImportError:
                return result

            change_desc = (
                getattr(context, "description", "") or ""
            )
            # No claimed change → no Quine vector. Skip.
            if not change_desc.strip():
                return result

            cwd = Path.cwd()

            def _check_one(file_path: str, candidate_src: str) -> Tuple[bool, str]:
                """Return ``(anomaly_detected, reason)``. Best-effort
                — any failure → no anomaly (defense in depth)."""
                try:
                    if not file_path or not isinstance(candidate_src, str):
                        return (False, "")
                    p = Path(file_path)
                    if not p.is_absolute():
                        p = cwd / p
                    if not p.exists() or not p.is_file():
                        # New file — no original to compare.
                        return (False, "")
                    try:
                        original_src = p.read_text(
                            encoding="utf-8", errors="replace",
                        )
                    except OSError:
                        return (False, "")
                    chk = compute_bg_spec_structural_check(
                        candidate_source=candidate_src,
                        original_source=original_src,
                        change_description=change_desc,
                    )
                    return (chk.anomaly_detected, chk.anomaly_reason)
                except Exception:  # noqa: BLE001 — defensive
                    return (False, "")

            def _candidate_anomalous(cand: Dict[str, Any]) -> Tuple[bool, str]:
                """Multi-file candidate: anomalous iff EVERY entry is
                anomalous (a partial mix may still be a real change).
                Single-file candidate: direct check."""
                files_list = cand.get("files")
                if isinstance(files_list, list) and files_list:
                    entries = [
                        e for e in files_list
                        if isinstance(e, dict)
                    ]
                    if not entries:
                        return (False, "")
                    flags: list = []
                    reasons: list = []
                    for entry in entries:
                        anom, reason = _check_one(
                            entry.get("file_path", ""),
                            entry.get("full_content", ""),
                        )
                        flags.append(anom)
                        if reason:
                            reasons.append(reason)
                    # All-or-nothing: only drop when every entry
                    # is structurally identical to its original.
                    if flags and all(flags):
                        return (
                            True,
                            f"multi_file_all_quine: {'; '.join(reasons)[:200]}",
                        )
                    return (False, "")
                # Single-file legacy shape.
                return _check_one(
                    cand.get("file_path", ""),
                    cand.get("full_content", ""),
                )

            # Run AST signature compute off the event loop. Each
            # check reads a file; bound the parallelism via the
            # default thread pool (no extra knobs to tune).
            anomaly_results: list = await asyncio.gather(
                *[
                    asyncio.to_thread(_candidate_anomalous, cand)
                    for cand in result.candidates
                ],
                return_exceptions=True,
            )

            kept: list = []
            dropped: int = 0
            for cand, outcome in zip(result.candidates, anomaly_results):
                if isinstance(outcome, BaseException):
                    kept.append(cand)
                    continue
                anom, reason = outcome
                if anom:
                    dropped += 1
                    op_id = (
                        getattr(context, "op_id", "") or "?"
                    )[:12]
                    logger.warning(
                        "[CandidateGenerator] bg_spec_quine_drop "
                        "op=%s route=%s reason=%s",
                        op_id, route, (reason or "")[:160],
                    )
                else:
                    kept.append(cand)

            if dropped == 0:
                return result

            # Replace the candidates tuple. Keep all other
            # GenerationResult fields intact (provider_name,
            # duration, tool records, token usage, cost — these
            # describe what the provider actually did, not the
            # filter outcome).
            return dataclasses.replace(
                result, candidates=tuple(kept),
            )
        except Exception:  # noqa: BLE001 — last-resort defensive
            return result

    async def _generate_dispatch(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Internal dispatch — the original body of :meth:`generate`.

        Route-based dispatch with Tier 0 → fallback cascade. This is
        the hot path; the public ``generate()`` above wraps it only to
        observe exhaustion and success signals.
        """
        # ── Route-based dispatch (Manifesto §5 Tier 0: deterministic) ──
        _provider_route = getattr(context, "provider_route", "") or "standard"

        # ── Phase 10 P10.3+P10.3.5 — AsyncTopologySentinel gate ────
        # Pre-Slice-23: env-only check (``JARVIS_TOPOLOGY_SENTINEL_ENABLED=true``).
        # Slice 23 (autonomous registry-driven): the gate now consults
        # ``_slice23_should_activate_sentinel`` which composes 5
        # decision conditions (env explicit on/off / Claude disabled /
        # multi-model trusted fleet / Phase 10 default-off). See helper
        # docstring for the full closed decision matrix. The Phase 10
        # graduation contract pin (env DEFAULT stays false) is preserved
        # — Slice 23 adds structural overrides on top of that default,
        # the literal default is unchanged.
        #
        # Pre-flight handshake (directive 2026-04-27): instead of a
        # silent try/except that swallows boundary-isolation defects
        # (which is what bit session bt-2026-04-27-194550), we run
        # ``preflight_check()`` at the gate. If the sentinel fails to
        # initialize inside this subprocess for ANY reason — module
        # import, topology load, missing dw_models — we raise
        # ``SentinelInitializationError`` so the operator sees the
        # defect at the point of decision, not minutes later in the
        # postmortem. Master-flag-off remains byte-identical legacy
        # behavior: this entire block is bypassed.
        _slice23_activate, _slice23_reason = _slice23_should_activate_sentinel(
            _provider_route,
        )
        if _slice23_activate:
            logger.info(
                "[CandidateGenerator] Slice 23 sentinel activation: "
                "route=%s reason=%s — walking ranked DW fleet "
                "(skips OPEN breakers + Slice 20C drifted models)",
                _provider_route, _slice23_reason,
            )
            try:
                from backend.core.ouroboros.governance.topology_sentinel import (
                    preflight_check as _sentinel_preflight,
                    SentinelInitializationError as _SentinelInitError,
                )
            except ImportError as _imp_exc:
                # Master flag explicitly true but the module is
                # unimportable — this is a deployment defect, NOT a
                # silent fall-through. Raise so the orchestrator's
                # existing accept-failure branch records it visibly.
                raise RuntimeError(
                    f"sentinel_module_import_failed:"
                    f"{type(_imp_exc).__name__}:"
                    f"{str(_imp_exc)[:120]}"
                ) from _imp_exc
            _preflight = _sentinel_preflight()
            if not _preflight.healthy:
                raise _SentinelInitError(
                    _preflight.failed_assertions,
                    _preflight.diagnostics,
                )
            logger.info(
                "[CandidateGenerator] Phase 10 sentinel preflight: "
                "healthy=True schema=%s routes_with_dw_models=%s "
                "monitor_config=%s event_loop_bound=%s diagnostics=%s",
                _preflight.schema_version,
                list(_preflight.routes_with_dw_models),
                _preflight.monitor_config_present,
                _preflight.event_loop_bound,
                list(_preflight.diagnostics),
            )
            _result = await self._dispatch_via_sentinel(
                context, deadline, _provider_route,
            )
            if _result is not None:
                _note_dw_candidate_success()  # Slice 53 — a lane yielded a candidate
                return _result
            # _dispatch_via_sentinel returns None to signal "fall
            # through to legacy path" (e.g. the route has empty
            # dw_models — IMMEDIATE by design — so the existing
            # _generate_immediate handler still runs below).

        # Brain Selection Topology — hard segmentation (Manifesto §5).
        # When ``doubleword_topology`` marks a route as DW-forbidden,
        # the ``block_mode`` field decides what to do next:
        #
        #   cascade_to_claude — IMMEDIATE/COMPLEX: route straight to
        #     Claude via ``_generate_immediate``. Live-fire bbpst3ebf
        #     (2026-04-14) proved BOTH DW 397B and Gemma 4 31B time out
        #     on the 120s Tier 0 RT budget for architectural COMPLEX
        #     GENERATE; Claude is the intended brain for these routes.
        #
        #   skip_and_queue — BACKGROUND/SPECULATIVE: raise a sentinel
        #     RuntimeError the orchestrator already handles gracefully
        #     (background_dw_* / speculative_deferred). Do NOT cascade
        #     to Claude. Alignment test bt-2026-04-14-182446 produced
        #     0/13 Gemma BG successes with a right-sized 2.8K-token
        #     envelope — root cause is provider-side SSE stream stall,
        #     not prompt size. Routing continuous background daemons
        #     to Claude violates the unit economics of scalable
        #     autonomy. The queue stays dormant until a viable,
        #     cost-effective inference endpoint is secured.
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology as _get_topology,
        )
        _topology = _get_topology()
        # Phase 10 Slice 5a — unified deletion-side helper. Branches
        # on JARVIS_TOPOLOGY_SENTINEL_ENABLED internally so v1 yaml
        # fields can be deleted safely in Slice 5b after contract
        # green. block_mode preserved in v1 vocab — downstream
        # `== "skip_and_queue"` check unchanged.
        _is_blocked, _block_reason, _block_mode = (
            _topology.is_dw_blocked_for_route(_provider_route)
        )
        if _topology.enabled and _is_blocked:
            if _block_mode == "skip_and_queue":
                # Nervous System Reflex (Manifesto §5 — survival supersedes
                # cost optimization): read-only ops MUST NOT lock up on a
                # paused DW endpoint. When the topology has skipped DW on
                # BACKGROUND, cascade straight to Claude for the read-only
                # op instead of raising skip_and_queue. The is_read_only
                # contract (Rule 0d + APPLY short-circuit) makes this
                # structurally safe: no mutation can happen, so the cost
                # of the Claude call is bounded and observable.
                _is_read_only = bool(
                    getattr(context, "is_read_only", False)
                )
                if (
                    _is_read_only
                    and _provider_route == "background"
                    and self._fallback is not None
                ):
                    logger.info(
                        "[CandidateGenerator] Nervous-System Reflex: BG "
                        "topology skip_and_queue bypassed for read-only op "
                        "— cascading to Claude (reason=%s) [%s]",
                        _block_reason,
                        getattr(context, "op_id", "?")[:16],
                    )
                    try:
                        return await self._call_fallback(context, deadline)
                    except Exception as exc:
                        raise RuntimeError(
                            f"background_fallback_failed:"
                            f"topology_skip_read_only_cascade:"
                            f"{type(exc).__name__}:{str(exc)[:80]}"
                        ) from exc
                logger.info(
                    "[CandidateGenerator] Topology block: route=%s "
                    "block_mode=skip_and_queue reason=%s — skipping "
                    "generation (no Claude cascade)",
                    _provider_route, _block_reason,
                )
                # Sentinel-Pacemaker handshake (2026-04-29) —
                # when the topology layer blocks BG/SPEC ops because
                # the catalog is purged/empty, ask the Pacemaker to
                # bypass its 30-min cadence sleep and probe DW now.
                # If DW is reachable, the next refresh cycle
                # repopulates the catalog and subsequent ops flow.
                # Best-effort, never raises. Rate-limited at the
                # trigger site so a block-storm doesn't thrash /models.
                _reason_lower = (_block_reason or "").lower()
                _is_catalog_purge = (
                    "catalog" in _reason_lower
                    and (
                        "purged" in _reason_lower
                        or "empty" in _reason_lower
                        or "static list" in _reason_lower
                    )
                )
                if _is_catalog_purge:
                    try:
                        from backend.core.ouroboros.governance.dw_discovery_runner import (
                            request_force_refresh,
                        )
                        request_force_refresh(
                            reason=(
                                f"topology_block:{_provider_route}:"
                                f"{_block_reason[:80]}"
                            ),
                        )
                    except Exception:  # noqa: BLE001 — never raise
                        logger.debug(
                            "[CandidateGenerator] force_refresh "
                            "request failed", exc_info=True,
                        )
                if _provider_route == "speculative":
                    raise RuntimeError(
                        f"speculative_deferred:blocked_by_topology:"
                        f"{_block_reason[:120]}"
                    )
                # BACKGROUND (and any future skip_and_queue route)
                # routes through the orchestrator's
                # "background_dw_*" graceful-accept branch.
                raise RuntimeError(
                    f"background_dw_blocked_by_topology:"
                    f"{_block_reason[:120]}"
                )
            logger.info(
                "[CandidateGenerator] Topology block: route=%s "
                "block_mode=cascade_to_claude reason=%s — routing "
                "direct to Claude",
                _provider_route, _block_reason,
            )
            return await self._generate_immediate(context, deadline)

        # Quota Shield: a shield-selected (prefer_local) op tries the zero-cost local
        # tier first via the existing primacy path, regardless of urgency route. On
        # local decline (sem saturation / timeout / error) it returns None and falls
        # through to the normal route below -> graceful, no behavior change when unset.
        if (
            getattr(context, "prefer_local", False)
            and self._jprime is not None
            and _provider_route not in ("background", "speculative")
        ):
            _ql = await self._try_jprime_primacy(context, deadline, route_label="quota_shield")
            if _ql is not None:
                return _ql

        if _provider_route == "immediate":
            return await self._generate_immediate(context, deadline)
        if _provider_route == "background":
            return await self._generate_background(context, deadline)
        if _provider_route == "speculative":
            return await self._generate_speculative(context, deadline)
        # "standard" and "complex" are both intercepted above by the
        # topology hard-block in the default yaml (standard sealed as of
        # bt-2026-04-14-203740, complex sealed per bbpst3ebf). They reach
        # this fall-through only if operators explicitly unseal them in
        # brain_selection_policy.yaml — at which point the full DW→Claude
        # cascade below applies. Not recommended without fresh empirical
        # evidence that DW's SSE endpoint has recovered.

        # ── Tier 0: DoubleWord 397B ──────────────────────────────
        #
        # Manifesto §3: "Zero polling. Pure reflex."
        # Manifesto §5: "Tier 0 (Deterministic Fast-Path)"
        #
        # Two modes based on DW real-time SSE availability:
        #   RT enabled  → tier0.generate() → _generate_realtime (SSE stream)
        #                 Zero polling. Token-by-token streaming. Pure reflex.
        #                 Internal RT→batch fallback on 429/503 (stay cheap).
        #   RT disabled → submit_batch() → background poll (legacy path)
        #                 Used only when DOUBLEWORD_REALTIME_ENABLED=false.
        #
        # On any Tier 0 failure → cascade to Claude fallback (Tier 1).

        _op_id = getattr(context, "operation_id", "")
        _dw_is_primary = (self._tier0 is not None and self._primary is self._tier0)
        _dw_is_fallback = (self._tier0 is not None and self._fallback is self._tier0)
        _tier0_attempted = False

        if self._tier0 is not None and getattr(self._tier0, "is_available", False):
            # Skip if DW is in any failure mode whose recovery ETA hasn't elapsed.
            # Previously this only fired on CONNECTION_ERROR — meaning a misclassified
            # TRANSIENT_TRANSPORT or TIMEOUT could keep hammering DW back-to-back
            # and exhaust every op until the human stopped the loop. Generalized
            # in bt-2026-04-12-005521 fix to honor whichever mode is active.
            _fsm_in_backoff = (
                self.fsm._failure_mode is not None
                and self.fsm._failure_mode is not FailureMode.CONTENT_FAILURE
                and not self.fsm.should_attempt_primary()
            )
            # Per-op rotation guard: even when the FSM says "go", if N consecutive
            # ops just died on Tier 0 within the rotation window, give DW a break.
            _rotation_skip = self._should_skip_tier0_for_op()

            if _rotation_skip:
                logger.info(
                    "[CandidateGenerator] Tier 0 skipped: per-op rotation "
                    "(consecutive_failures=%d threshold=%d window=%.0fs)",
                    self._counters.consecutive_tier0_failures,
                    self._tier0_skip_threshold,
                    self._tier0_skip_window_s,
                )
            elif _fsm_in_backoff:
                logger.info(
                    "[CandidateGenerator] Tier 0 skipped: DW in %s backoff "
                    "(failures=%d, ETA=%.0fs)",
                    self.fsm._failure_mode.name if self.fsm._failure_mode else "UNKNOWN",
                    self.fsm._consecutive_failures,
                    max(0, self.fsm.recovery_eta() - time.monotonic()),
                )

            elif getattr(self._tier0, "_realtime_enabled", False):
                # ── Real-time SSE path (Manifesto §3: zero polling) ──
                # Call tier0.generate() directly — hits _generate_realtime.
                # Budget-capped via asyncio.wait_for; on timeout or failure,
                # cascade to Claude fallback with guaranteed reserve time.
                _tier0_attempted = True
                remaining = self._remaining_seconds(deadline)
                _complexity = getattr(context, "task_complexity", "trivial")
                tier0_budget = self._compute_tier0_budget_dynamic(
                    remaining, _complexity, _provider_route,
                )
                tier1_reserve = remaining - tier0_budget
                _tracker_p95 = self._latency_tracker.p95() if self._latency_tracker else None

                logger.info(
                    "[CandidateGenerator] Tier 0 RT: budget=%.1fs of %.1fs "
                    "(Tier 1 reserve=%.1fs), complexity=%s, model=%s, p95=%s",
                    tier0_budget, remaining, tier1_reserve, _complexity,
                    getattr(self._tier0, "_model", "unknown"),
                    f"{_tracker_p95:.1f}s" if _tracker_p95 is not None else "cold",
                )

                if tier0_budget <= 0:
                    logger.info(
                        "[CandidateGenerator] Tier 0 skipped: zero budget "
                        "for complexity=%s. Cascading to Tier 1 (%.1fs)",
                        _complexity, remaining,
                    )
                    # Fall through to Claude cascade below

                if tier0_budget > 0:
                    # Stream-aware timeout: use asyncio.shield so we can
                    # grant a grace extension if DW is actively streaming
                    # tokens when the base budget expires (Manifesto §3).
                    _gen_task = asyncio.ensure_future(
                        self._tier0.generate(context, deadline),
                    )
                    # Defect #4 Slice A — leak-prevention callback.
                    # The shield above means _gen_task survives outer
                    # wait_for cancellation; if it later raises with
                    # nobody awaiting, asyncio's default handler logs
                    # "Task exception was never retrieved". The
                    # callback consumes the exception cleanly.
                    _gen_task.add_done_callback(_swallow_task_exception)
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(_gen_task), timeout=tier0_budget,
                        )
                        if result is not None and len(result.candidates) > 0:
                            # RT success — record recovery if coming back from failure
                            if self.fsm._consecutive_failures > 0:
                                self.fsm.record_primary_success()
                            self._record_tier0_success()
                            if self._latency_tracker is not None:
                                self._latency_tracker.record_success(
                                    result.generation_duration_s,
                                )
                            logger.info(
                                "[CandidateGenerator] Tier 0 RT: %d candidates in %.1fs "
                                "(zero polling)",
                                len(result.candidates), result.generation_duration_s,
                            )
                            return result
                        # Empty result — fall through to Claude
                        logger.info(
                            "[CandidateGenerator] Tier 0 RT: no candidates — "
                            "cascading to Tier 1 (%.1fs remaining)",
                            self._remaining_seconds(deadline),
                        )
                    except asyncio.TimeoutError:
                        # Check if DW is actively streaming SSE tokens.
                        # If so, grant up to 30s extension while preserving
                        # Tier 1 reserve — don't kill a productive stream.
                        _last_chunk = getattr(self._tier0, "_last_chunk_at", 0.0)
                        _streaming = _last_chunk > 0 and (time.monotonic() - _last_chunk) < 10.0
                        _ext_cap = self._remaining_seconds(deadline) - _TIER1_MIN_RESERVE_S
                        _extension = min(30.0, _ext_cap)

                        if _streaming and _extension > 5.0:
                            logger.info(
                                "[CandidateGenerator] Tier 0 RT: actively streaming, "
                                "granting +%.0fs extension (Tier 1 reserve preserved)",
                                _extension,
                            )
                            # Use asyncio.wait (not wait_for) so a timeout does
                            # NOT cancel the task — avoids the race where DW
                            # completes between timeout fire and cancel delivery.
                            _done, _ = await asyncio.wait(
                                {_gen_task}, timeout=_extension,
                            )
                            if _gen_task in _done:
                                try:
                                    result = _gen_task.result()
                                except Exception as ext_exc:
                                    _mode = FailbackStateMachine.classify_exception(ext_exc)
                                    logger.warning(
                                        "[CandidateGenerator] Tier 0 RT: grace-period "
                                        "content failed (mode=%s, %s). Cascading.",
                                        _mode.name, ext_exc,
                                    )
                                    result = None
                                if result is not None and len(result.candidates) > 0:
                                    if self.fsm._consecutive_failures > 0:
                                        self.fsm.record_primary_success()
                                    self._record_tier0_success()
                                    if self._latency_tracker is not None:
                                        self._latency_tracker.record_success(
                                            result.generation_duration_s,
                                        )
                                    logger.info(
                                        "[CandidateGenerator] Tier 0 RT: %d candidates "
                                        "in %.1fs (stream extension saved it)",
                                        len(result.candidates),
                                        result.generation_duration_s,
                                    )
                                    return result
                        # Task still pending or no extension granted — cancel it.
                        # Check done() first: task may have completed in the
                        # instant between timeout and here (shield race window).
                        if not _gen_task.done():
                            _gen_task.cancel()
                        elif not _gen_task.cancelled():
                            try:
                                _late = _gen_task.result()
                                if _late is not None and len(_late.candidates) > 0:
                                    if self.fsm._consecutive_failures > 0:
                                        self.fsm.record_primary_success()
                                    self._record_tier0_success()
                                    logger.info(
                                        "[CandidateGenerator] Tier 0 RT: %d candidates "
                                        "recovered from timeout race",
                                        len(_late.candidates),
                                    )
                                    return _late
                            except Exception:
                                pass

                        logger.warning(
                            "[CandidateGenerator] Tier 0 RT: budget exhausted "
                            "(%.1fs). Cascading to Tier 1 (%.1fs remaining)",
                            tier0_budget, self._remaining_seconds(deadline),
                        )
                        self.fsm.record_primary_failure(mode=FailureMode.TIMEOUT)
                        self._record_tier0_failure()
                        if self._latency_tracker is not None:
                            self._latency_tracker.record_failure()
                    except asyncio.CancelledError:
                        _gen_task.cancel()
                        raise
                    except Exception as rt_exc:
                        _gen_task.cancel()
                        mode = FailbackStateMachine.classify_exception(rt_exc)
                        logger.warning(
                            "[CandidateGenerator] Tier 0 RT failed (mode=%s, %s: %s). "
                            "Cascading to Tier 1 (%.1fs remaining)",
                            mode.name, type(rt_exc).__name__, rt_exc,
                            self._remaining_seconds(deadline),
                        )
                        if mode is not FailureMode.CONTENT_FAILURE:
                            self.fsm.record_primary_failure(mode=mode)
                            self._record_tier0_failure()
                            if self._latency_tracker is not None:
                                self._latency_tracker.record_failure()

            else:
                # ── Legacy batch path (DOUBLEWORD_REALTIME_ENABLED=false) ──
                # submit_batch() → background poll → await result.
                _TIER0_COMPLEXITY_CLASSES = frozenset({"heavy_code", "complex"})
                _complexity = ""
                if context.routing is not None:
                    _complexity = getattr(context.routing, "task_complexity", "")
                _is_cross_repo = getattr(context, "cross_repo", False)
                _qualifies = _complexity in _TIER0_COMPLEXITY_CLASSES or _is_cross_repo

                _dw_is_only_provider = (
                    self._primary is self._tier0 or self._fallback is self._tier0
                )
                if _dw_is_only_provider:
                    _qualifies = True

                if _qualifies:
                    _tier0_attempted = True
                    try:
                        pending = await self._tier0.submit_batch(context)
                        if pending is not None:
                            logger.info(
                                "[CandidateGenerator] Tier 0 batch %s submitted",
                                pending.batch_id,
                            )
                            await self._record_tier0_ledger(
                                _op_id, "pending_tier0", {
                                    "batch_id": pending.batch_id,
                                    "file_id": pending.file_id,
                                    "model": getattr(self._tier0, "_model", "unknown"),
                                },
                            )
                            self._background_polls = {
                                k: t for k, t in self._background_polls.items()
                                if not t.done()
                            }
                            if len(self._background_polls) < self._max_background_polls:
                                task = asyncio.create_task(
                                    self._background_poll_tier0(pending, context),
                                    name=f"dw-poll-{pending.batch_id[:12]}",
                                )
                                # Defect #4 Slice A — defensive
                                # callback (background_poll_tier0
                                # already has try/except internally,
                                # but the callback ensures even an
                                # asyncio.CancelledError that bypasses
                                # the wrapper gets consumed).
                                task.add_done_callback(_swallow_task_exception)
                                self._background_polls[_op_id] = task
                    except asyncio.CancelledError:
                        raise
                    except Exception as t0_exc:
                        logger.warning(
                            "[CandidateGenerator] Tier 0 batch submit failed: %s",
                            t0_exc,
                        )

                # Await background poll if DW is primary/fallback
                _dw_poll_task = self._background_polls.get(_op_id)
                if _dw_poll_task is not None and (_dw_is_primary or _dw_is_fallback):
                    remaining = self._remaining_seconds(deadline)
                    _complexity = getattr(context, "task_complexity", "trivial")
                    tier0_budget = self._compute_tier0_budget(remaining, _complexity)

                    logger.info(
                        "[CandidateGenerator] Awaiting batch poll: "
                        "budget=%.1fs of %.1fs, complexity=%s",
                        tier0_budget, remaining, _complexity,
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(_dw_poll_task), timeout=tier0_budget,
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        logger.warning(
                            "[CandidateGenerator] Batch poll budget exhausted "
                            "(%.1fs)", tier0_budget,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[CandidateGenerator] Batch poll error: %s", exc,
                        )

                    _completed = self._completed_batches.pop(_op_id, None)
                    if _completed is not None and _completed.result is not None:
                        _result = _completed.result
                        if len(_result.candidates) > 0:
                            logger.info(
                                "[CandidateGenerator] Batch result: %d candidates",
                                len(_result.candidates),
                            )
                            return _result

        # ── Tier 1: Primary → Fallback cascade ───────────────────
        #
        # If Tier 0 was attempted and DW IS the primary, skip redundant
        # primary.generate() call — go straight to Claude fallback.
        # (Manifesto §3: no wasteful retries)

        state = self.fsm.state

        if state is FailbackState.QUEUE_ONLY:
            self._raise_exhausted(
                "queue_only_dispatch",
                context=context,
                deadline=deadline,
                tier0_attempted=_tier0_attempted,
                dw_is_primary=_dw_is_primary,
            )

        if _tier0_attempted and _dw_is_primary:
            logger.info(
                "[CandidateGenerator] Tier 0 IS primary — routing directly "
                "to Claude fallback (%.1fs remaining)",
                self._remaining_seconds(deadline),
            )
            return await self._call_fallback(context, deadline)

        if state is FailbackState.PRIMARY_READY:
            # P2.3: Model-selection learning — check if historical data
            # recommends the fallback for this complexity class.  Only
            # applies when both providers are healthy (PRIMARY_READY);
            # infrastructure health always takes precedence.
            _complexity = getattr(context, "task_complexity", "") or "unknown"
            _recommended = self._query_provider_recommendation(_complexity)
            if (
                _recommended is not None
                and self._fallback is not None
                and getattr(self._fallback, "provider_name", "") == _recommended
            ):
                logger.info(
                    "[CandidateGenerator] Learning override: '%s' recommended "
                    "for complexity=%s — trying fallback first (%.1fs remaining)",
                    _recommended, _complexity, self._remaining_seconds(deadline),
                )
                try:
                    return await self._call_fallback(context, deadline)
                except Exception as _fb_exc:
                    logger.info(
                        "[CandidateGenerator] Learning-recommended fallback failed: %s "
                        "— falling back to primary",
                        type(_fb_exc).__name__,
                    )
                    return await self._call_primary(context, deadline)

            return await self._try_primary_then_fallback(context, deadline)

        # FALLBACK_ACTIVE or PRIMARY_DEGRADED: adaptive recovery routing.
        if self.fsm.should_attempt_primary():
            logger.info(
                "[CandidateGenerator] Recovery window elapsed (mode=%s, "
                "failures=%d), re-attempting primary "
                "(cost-save: $0.10/$0.40 vs $3.00/$15.00 per M)",
                self.fsm._failure_mode.name if self.fsm._failure_mode else "NONE",
                self.fsm._consecutive_failures,
            )
            return await self._try_primary_then_fallback(context, deadline)

        eta_s = max(0, self.fsm.recovery_eta() - time.monotonic())
        logger.info(
            "[CandidateGenerator] Primary in backoff (mode=%s, ETA=%.0fs), "
            "using fallback",
            self.fsm._failure_mode.name if self.fsm._failure_mode else "NONE",
            eta_s,
        )
        return await self._call_fallback(context, deadline)

    # ------------------------------------------------------------------
    # Route-specific generation strategies (Manifesto §5)
    # ------------------------------------------------------------------

    async def _dispatch_via_sentinel(
        self,
        context: OperationContext,
        deadline: datetime,
        provider_route: str,
        *,
        _immortal_attempt: int = 0,
        _immortal_budget_deadline: Optional[float] = None,
    ) -> Optional[GenerationResult]:
        """Phase 10 P10.3 — sentinel-driven DW dispatch.

        Walks the route's ranked ``dw_models`` list (yaml v2). For each
        model whose breaker is not OPEN, stamps ``ctx._dw_model_override``
        and attempts DW. On per-model failure reports to the sentinel
        (with appropriate ``FailureSource`` weight) and continues to
        the next model. After exhausting all DW models, applies the
        route's ``fallback_tolerance``:

          * ``"cascade_to_claude"`` — invokes ``_call_fallback`` (Claude).
          * ``"queue"`` — raises the sentinel-already-known
            ``RuntimeError("dw_severed_queued:...")`` shape that the
            orchestrator's existing accept-failure branch handles.

        Returns:
          * ``GenerationResult`` on DW success or Claude cascade.
          * ``None`` to signal "fall through to legacy path" — used
            when the route has empty ``dw_models`` (e.g. IMMEDIATE,
            which is Claude-direct by Manifesto §5 design and is
            handled by the existing ``_generate_immediate`` dispatcher
            below).
        """
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology as _get_topology,
        )
        from backend.core.ouroboros.governance.topology_sentinel import (
            FailureSource,
            get_default_sentinel,
            reset_dw_model_override as _reset_override,
            set_dw_model_override as _set_override,
        )

        # Phase 12.2 Slice F — discovery is now armed eagerly by the
        # Autonomic Pacemaker in GovernedLoopService at orchestrator
        # boot, before any sensor signal is pulled. The dynamic catalog
        # is populated + the 30-min refresh task is heartbeating before
        # the dispatcher runs, so this code path never needs to bootstrap
        # discovery itself. Operator directive 2026-04-28 mandates a
        # single source of truth — the Pacemaker. If the Pacemaker fails
        # to arm, operators see the warning at boot rather than a silent
        # failure on first dispatch.

        topology = _get_topology()
        if not topology.enabled:
            return None
        ranked_models = topology.dw_models_for_route(provider_route)
        fallback_tolerance = topology.fallback_tolerance_for_route(
            provider_route,
        )

        # Slice 229 — exploration-floor driven route elevation. When this op
        # must satisfy the Iron Gate exploration floor (the SAME Slice-226
        # predicate that opens the tool loop + steers the hedge), prepend the
        # COMPLEX route's agentic-elite pool (active-param-ranked, family-
        # weighted) so tool-loop work is never starved onto low-active models
        # that cannot drive it. The live layer-5 wedge: Kimi/DeepSeek-V4-Pro/
        # GLM-5.1 all promoted=True yet UNREACHABLE from STANDARD — file-00's
        # 'simple' label kept it in a pool whose only capable member drifts.
        try:
            from backend.core.ouroboros.governance.exploration_engine import (
                exploration_gate_demands_tools as _s229_gate_demands,
            )
            from backend.core.ouroboros.governance.provider_topology import (
                elevate_pool_for_exploration as _s229_elevate,
            )
            _s229_demands = (
                provider_route not in ("background", "speculative")
                and _s229_gate_demands(
                    str(getattr(context, "task_complexity", "")),
                )
            )
            if _s229_demands and provider_route != "complex":
                _s229_elite = topology.dw_models_for_route("complex")
                _s229_pool = _s229_elevate(
                    tuple(ranked_models), tuple(_s229_elite),
                    demands_tools=True,
                )
                if tuple(_s229_pool) != tuple(ranked_models):
                    logger.warning(
                        "[CandidateGenerator] ⚡ ROUTE ELEVATION: op needs "
                        "Iron-Gate exploration — agentic-elite (COMPLEX) pool "
                        "prepended for route=%s: %s (op=%s)",
                        provider_route, list(_s229_pool)[:4],
                        getattr(context, "op_id", "?")[:16],
                    )
                    ranked_models = list(_s229_pool)
        except Exception:  # noqa: BLE001 — elevation is enhancement, never blocks
            pass

        # Slice 201 — Contextual Bandit Routing Advisor. ADVISORY-ONLY +
        # structurally fail-closed: the advisor reorders WITHIN ranked_models
        # (the brain_selection_policy active set for this route), so it can
        # only change the ORDER the sentinel tries policy-permitted models —
        # never select an out-of-policy arm. Gated (default OFF → no-op); any
        # error keeps the deterministic order. The hand-rolled router stays
        # authoritative.
        try:
            from backend.core.ouroboros.governance.bandit_router import (
                get_bandit_router as _s201_bandit,
            )
            _s201_order = _s201_bandit().advise(ranked_models)
            if _s201_order and set(_s201_order) == set(ranked_models):
                ranked_models = _s201_order
        except Exception:  # noqa: BLE001 — advisory, never blocks dispatch
            pass

        # Empty dw_models → fall through to legacy dispatch. IMMEDIATE
        # has empty models by design (Claude-direct); other routes
        # would fall here only if yaml is misconfigured.
        if not ranked_models:
            logger.debug(
                "[CandidateGenerator] Sentinel dispatch: route=%s "
                "has no dw_models — falling through to legacy",
                provider_route,
            )
            return None

        # Slice 76 Phase 2 — pre-flight DW transport gate. If the existing
        # dw_surface_health ledger shows the DIRECT_STREAMING surface FRESHLY
        # TRANSPORT_DEGRADED, the whole ranked list shares that dead transport
        # (cf. should_sever_dw_lane). Cascade to Claude with the FULL untouched
        # budget NOW — before the _primary_sem wait + per-model timeout cascade
        # burns it (the EVAL-2 terminal_timeout, PRD §50.11). Only when the
        # route already cascades to Claude (a "queue"-tolerance route keeps its
        # contract). Gated + fail-open; default-on.
        if (
            fallback_tolerance == "cascade_to_claude"
            and dw_transport_degraded_preflight()
        ):
            logger.info(
                "[CandidateGenerator] Slice 76 pre-flight: DW DIRECT_STREAMING "
                "TRANSPORT_DEGRADED (fresh) — severing DW lane pre-budget, "
                "cascading to Claude with full budget (op=%s route=%s)",
                getattr(context, "op_id", "?"), provider_route,
            )
            return await self._call_fallback(context, deadline)

        sentinel = get_default_sentinel()
        # Register every model in the ranked list (idempotent). The
        # sentinel needs to know about each model_id before it can
        # answer get_state.
        for model_id in ranked_models:
            sentinel.register_endpoint(model_id)

        op_id_short = (
            getattr(context, "op_id", "?")[:16]
            if hasattr(context, "op_id") else "?"
        )

        # Walk the ranked list. For each model not OPEN, attempt DW.
        attempts: List[str] = []
        last_failure: Optional[str] = None
        # Slice 83 Phase 2 — consecutive LIVE_TRANSPORT streak across the
        # heterogeneous coder stack. A single model's transport break rotates
        # to the next coder; only a `threshold`-long streak (genuine lane-wide
        # blackout) severs. Reset by any success / non-transport failure.
        _consecutive_lt: int = 0
        _lt_sever_threshold: int = _live_transport_sever_threshold()
        # Slice 182 — SENTINEL BATCH ENFORCEMENT (Gap 1). The per-model frozen context carries
        # an EMPTY provider_route, so the downstream _slice36_should_force_batch route gate
        # can't engage and every probe ruptured on RT (the v181 bleed). The sentinel KNOWS the
        # route + the risk — so if the stream is degraded / rupture-risk is high AND batch is
        # healthy, COMMAND every probe to batch at T=0 via the force-batch ContextVar.
        _s182_force_batch = False
        try:
            from backend.core.ouroboros.governance.doubleword_provider import (
                _dw_streaming_warm_degraded as _s182_warm,
                _dw_rupture_risk_high as _s182_risk,
                _dw_batch_lane_healthy as _s182_batch_ok,
                _dw_in_cold_start as _s184_cold,
                _dw_hedge_supersedes as _s192_supersedes,
            )
            # Slice 183 — LIVE TELEMETRY PROBE. Capture the EXACT boolean state of every
            # sub-gate AND the final computed decision, UNCONDITIONALLY (before the if), so the
            # live soak shows precisely why force-batch is False. Each gate is evaluated into
            # its own local — no short-circuit hiding which one fails.
            _g_route_ok = provider_route in ("standard", "complex")
            _g_batch = bool(_s182_batch_ok())
            _g_warm = bool(_s182_warm())
            _g_risk = bool(_s182_risk(""))
            # Slice 184 — cold-start is a degradation TRIGGER: at fresh boot the stream is
            # unproven, so the sentinel commands batch (fail-safe) even when warm/risk are blind.
            _g_cold = bool(_s184_cold())
            # Slice 192 — PROACTIVE HIERARCHY: the sentinel DEFERS to the hedge. When the hedge
            # supersedes (active + no storm), do NOT force batch here — let the op RACE. The
            # cold-start/warm-boot enforce only fires when the hedge is off or a storm is confirmed.
            _g_hedge = bool(_s192_supersedes(context, model_id))
            _s182_force_batch = (
                (not _g_hedge)
                and _g_route_ok and _g_batch and (_g_warm or _g_risk or _g_cold)
            )
            logger.warning(
                "[Slice183] dispatch-telemetry: op=%s route=%r route_ok=%s "
                "batch_lane_healthy=%s warm_degraded=%s rupture_risk=%s cold_start=%s → FORCE_BATCH=%s",
                op_id_short, provider_route, _g_route_ok, _g_batch, _g_warm, _g_risk, _g_cold,
                _s182_force_batch,
            )
            if _s182_force_batch:
                logger.warning(
                    "[Cortex] SENTINEL batch-enforce: stream degraded / rupture-risk high → "
                    "ALL probes via BATCH at T=0 (route=%s, op=%s) — RT bypass eradicated",
                    provider_route, op_id_short,
                )
        except Exception:  # noqa: BLE001 — enforcement is best-effort, never blocks dispatch
            # Slice 183 — DO NOT silently swallow. Log the full traceback so a hidden
            # ImportError / attribute error in the gate path is visible in the live soak.
            import traceback as _s183_tb
            logger.warning(
                "[Slice183] dispatch-telemetry EXCEPTION (NOT swallowed silently): %s",
                _s183_tb.format_exc(),
            )
            _s182_force_batch = False
        for model_id in ranked_models:
            state = sentinel.get_state(model_id)
            # Phase 12 Slice H — TERMINAL_OPEN bypasses dispatch
            # entirely (deterministic ground-truth ban from a 4xx
            # modality or 401/403 auth failure; doesn't auto-recover
            # via probes, only via explicit reset / catalog refresh).
            # Treated indistinguishably from OPEN at the dispatch
            # gate — both are "do not attempt"; the difference is
            # purely in the recovery model (probe vs explicit reset).
            if state in ("OPEN", "TERMINAL_OPEN"):
                logger.info(
                    "[CandidateGenerator] Sentinel dispatch: route=%s "
                    "model=%s state=%s — skipping (op=%s)",
                    provider_route, model_id, state, op_id_short,
                )
                attempts.append(f"{model_id}:skipped_{state.lower()}")
                continue
            # Latency quarantine (2026-06-20): the entitlement breaker (above)
            # bans 403'd models; this bans models the TtftObserver has flagged as
            # COLD STORAGE (latest TTFT > mean + Nσ — weights evicted from VRAM →
            # the 180s-timeout black hole). Reuses the existing observer; only
            # skips when there's at least one OTHER candidate left to try (never
            # quarantines the sole remaining model into a no-op). Gated on the
            # observer's own master flag; fail-open (never blocks dispatch).
            if _latency_quarantine_enabled() and model_id != ranked_models[-1]:
                try:
                    from backend.core.ouroboros.governance.dw_ttft_observer import (
                        get_ttft_observer as _get_ttft_obs,
                    )
                    _obs = _get_ttft_obs()
                    if _obs is not None and _obs.is_cold_storage(model_id):
                        logger.info(
                            "[CandidateGenerator] Latency quarantine: route=%s "
                            "model=%s COLD_STORAGE (TTFT spike) — skipping to a "
                            "warmer candidate (op=%s)",
                            provider_route, model_id, op_id_short,
                        )
                        attempts.append(f"{model_id}:skipped_cold_storage")
                        continue
                except Exception:  # noqa: BLE001 — observer must never block dispatch
                    pass
            # Slice 20C — schema drift rotation. If this model has
            # produced a structurally-bad output earlier in this same
            # op (json_parse_error_after_heal / schema_id_hallucination
            # / zero_candidate_return), skip it indistinguishably from
            # a sentinel-OPEN breaker. Master-flag gated; when off, the
            # has_drifted() consultation short-circuits to False so the
            # check is a free no-op (byte-identical legacy behavior).
            try:
                from backend.core.ouroboros.governance.schema_drift_tracker import (
                    get_default_tracker as _get_drift_tracker,
                )
                _drift_tracker = _get_drift_tracker()
                _full_op_id_drift = getattr(context, "op_id", "") or ""
                if _drift_tracker.has_drifted(_full_op_id_drift, model_id):
                    logger.info(
                        "[CandidateGenerator] Sentinel dispatch: route=%s "
                        "model=%s drifted_on_op — rotating to sibling (op=%s)",
                        provider_route, model_id, op_id_short,
                    )
                    attempts.append(f"{model_id}:skipped_drift")
                    continue
            except Exception:  # noqa: BLE001 — rotation is enhancement, not gate
                # Tracker consultation must NEVER block dispatch. If
                # the tracker module is missing / unimportable / raises,
                # fall through to normal attempt (legacy behavior).
                pass
            # Slice 194 — race-triage rotation. If BOTH hedge arms died on
            # this model earlier in this same op (confirmed hard blockage),
            # skip the corpse — the next iteration IS the next-highest-ranked
            # catalog candidate. OWN master (JARVIS_RACE_TRIAGE_ENABLED,
            # default TRUE, failure-path-only) — deliberately independent of
            # the default-FALSE drift-rotation master above.
            try:
                from backend.core.ouroboros.governance.race_triage import (
                    is_blacklisted_for_op as _s194_is_blacklisted,
                )
                _s194_op_id = getattr(context, "op_id", "") or ""
                if _s194_is_blacklisted(_s194_op_id, model_id):
                    logger.warning(
                        "[RaceTriage] Sentinel dispatch: route=%s model=%s "
                        "dual-arm-blacklisted on op — rotating to next ranked "
                        "candidate (op=%s)",
                        provider_route, model_id, op_id_short,
                    )
                    attempts.append(f"{model_id}:skipped_dual_arm")
                    continue
            except Exception:  # noqa: BLE001 — rotation is enhancement, not gate
                pass
            attempts.append(f"{model_id}:attempted")
            # Stamp the per-attempt override via ContextVar (async-safe
            # per asyncio task, survives the frozen OperationContext
            # contract). The ContextVar is reset in the finally block
            # so the next iteration's set is a clean state, and so
            # cascade-to-Claude after exhaustion doesn't carry a stale
            # override into the fallback provider.
            _override_token = _set_override(model_id)
            # Slice 182 — alongside the model override, COMMAND batch for this probe when the
            # sentinel determined degradation (Gap 1). Reset in the same finally as the model
            # override, so neither leaks into the post-exhaustion cascade.
            _s182_fb_token = None
            if _s182_force_batch:
                try:
                    from backend.core.ouroboros.governance.doubleword_provider import (
                        set_sentinel_force_batch as _s182_set_fb,
                    )
                    _s182_fb_token = _s182_set_fb(True)
                except Exception:  # noqa: BLE001
                    _s182_fb_token = None
            logger.info(
                "[CandidateGenerator] Sentinel dispatch: route=%s "
                "attempting model=%s (state=%s, op=%s)",
                provider_route, model_id, state, op_id_short,
            )
            _attempt_result: Any = None
            _attempt_exc: Optional[BaseException] = None
            try:
                if provider_route == "background":
                    _attempt_result = await self._generate_background(
                        context, deadline,
                    )
                elif provider_route == "speculative":
                    _attempt_result = await self._generate_speculative(
                        context, deadline,
                    )
                else:
                    # Slice 23 — standard / complex / unknown route uses
                    # the primary-first cascade. The Slice 23 sentinel
                    # walker still stamps the ContextVar for the
                    # provider's INTERNAL routing (DoublewordProvider.
                    # _resolve_effective_model reads it to pick which
                    # model to actually call).
                    #
                    # Slice 30 — ALSO threads model_id explicitly through
                    # the orchestrator-side call chain so
                    # _compute_primary_budget's heavy-model 2.5× scalar
                    # (Slice 28 Phase 2) engages deterministically. The
                    # v23 wiring gap (ContextVar invisible across
                    # async/semaphore boundaries) is eliminated for the
                    # TIMEOUT decision; provider routing still uses the
                    # ContextVar (legitimate per-provider internal use).
                    _attempt_result = await self._try_primary_then_fallback(
                        context, deadline, model_id=model_id,
                    )
            except Exception as exc:
                _attempt_exc = exc
            finally:
                # Reset ContextVar before either success-return or
                # failure-continue so the next iteration starts with a
                # clean slate AND the post-loop cascade-to-Claude
                # doesn't carry a stale override into the fallback.
                _reset_override(_override_token)
                # Slice 182 — clear the force-batch command too (never leak into cascade).
                if _s182_fb_token is not None:
                    try:
                        from backend.core.ouroboros.governance.doubleword_provider import (
                            reset_sentinel_force_batch as _s182_reset_fb,
                        )
                        _s182_reset_fb(_s182_fb_token)
                    except Exception:  # noqa: BLE001
                        pass

            if _attempt_result is not None:
                # Success — let the sentinel know. Phase 10 P10.4
                # also wires report_failure at existing failure sites
                # so a stream-stall mid-generation also lands in the
                # sentinel; this report_success closes the
                # corresponding successful-stream signal.
                try:
                    sentinel.report_success(model_id)
                except Exception:
                    logger.debug(
                        "[CandidateGenerator] sentinel.report_success raised",
                        exc_info=True,
                    )
                try:
                    # Slice 201 — feed the bandit a SUCCESS reward for this arm.
                    from backend.core.ouroboros.governance.bandit_router import (
                        get_bandit_router as _s201_bandit_ok,
                    )
                    _s201_bandit_ok().record_outcome(model_id, success=True)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    # Override Matrix — clear the model-pin soft-lock streak on a
                    # real success (passive observed outcome; no active probe).
                    from backend.core.ouroboros.governance.model_pinning_heuristic import (
                        note_pin_outcome as _pin_ok,
                    )
                    _pin_ok(model_id, success=True)
                except Exception:  # noqa: BLE001
                    pass
                # Slice 20C — zero-candidate drift detection. The
                # parser succeeded (we're on the success branch) but
                # may have returned an empty candidates tuple while
                # NOT signaling no-op. That's the v15 "model judgment
                # flaw" — Venom exploration ran, model returned valid
                # JSON, but candidates=(). Record drift so the next
                # GENERATE_RETRY for this op_id rotates to a sibling.
                try:
                    _cands = getattr(_attempt_result, "candidates", None)
                    _is_noop = getattr(_attempt_result, "is_noop", False)
                    if (
                        _cands is not None
                        and len(_cands) == 0
                        and not _is_noop
                    ):
                        from backend.core.ouroboros.governance.schema_drift_tracker import (
                            DriftType,
                            get_default_tracker as _zc_tracker,
                        )
                        _full_op_zc = getattr(context, "op_id", "") or ""
                        if _full_op_zc:
                            _zc_tracker().record(
                                op_id=_full_op_zc,
                                model_id=model_id,
                                drift_type=DriftType.ZERO_CANDIDATE_RETURN,
                                raw_excerpt=(
                                    f"route={provider_route} "
                                    f"is_noop=False candidates=()"
                                ),
                            )
                            logger.info(
                                "[CandidateGenerator] Slice 20C zero-candidate "
                                "drift recorded: op=%s model=%s — next retry "
                                "will rotate to sibling",
                                op_id_short, model_id,
                            )
                except Exception:  # noqa: BLE001 — drift is enhancement
                    pass
                return _attempt_result

            if _attempt_exc is not None:
                exc = _attempt_exc
                # Slice 185 Phase 2 — STRICT-TYPE EXCEPTION SEGREGATION. A Python LOGICAL error
                # (NameError/TypeError/AttributeError/…) is OUR codebase bug, NOT a vendor
                # network rupture. It must bypass the vendor resilience path entirely: never be
                # classified as live_transport, never recorded to the DW surface-health ledger
                # (which corrupts the learned rupture rate), never silently degraded. Bubble it
                # up as an INTERNAL_FAULT and crash LOUDLY so we fix OUR bug — the AI must never
                # again blame the vendor for its own internal codebase flaws.
                from backend.core.ouroboros.governance.dw_fault_taxonomy import (
                    is_internal_fault as _s185_internal,
                    is_generation_timeout as _s241_gen_timeout,
                    is_fsm_exhaustion as _fsm_exhausted,
                )
                if _s185_internal(exc):
                    logger.error(
                        "[CandidateGenerator] INTERNAL_FAULT (%s) — NOT a vendor rupture; "
                        "bubbling up + crashing loud, NOT touching the DW vendor ledger "
                        "(op=%s, model=%s): %s",
                        type(exc).__name__, op_id_short, model_id, exc,
                        exc_info=True,
                    )
                    raise exc
                err_str = str(exc)
                err_lower = err_str.lower()

                # Phase 12 Slice F — Substrate Error Unmasking. When
                # the exception is a DoublewordInfraError (or any
                # structurally-unmasked equivalent that carries a
                # ``status_code`` attribute), classify FROM THE
                # STRUCTURED FIELD instead of regex on str(exc). This
                # is the substrate of Slice H's terminal-vs-transient
                # distinction — we MUST know the actual HTTP status to
                # decide TERMINAL_OPEN vs OPEN.
                _status_code = getattr(exc, "status_code", None)
                _response_body = getattr(exc, "response_body", "") or ""
                _is_modality = bool(
                    getattr(exc, "is_modality_error", lambda: False)()
                )
                _is_auth_terminal = bool(
                    getattr(exc, "is_terminal_auth_error", lambda: False)()
                )

                # Zero-Shot latency quarantine (2026-06-20): an explicit
                # generation TimeoutError (the 180s wall) OR a tool-loop deadline
                # is unambiguous evidence THIS model is unusable now. Flag it
                # cold-storage IMMEDIATELY (bypassing the n>=3 σ window that would
                # let it taint 2 more soaks) so the selector skips it next op. The
                # ban self-decays after a TTL (autonomic forgiveness). Fail-soft.
                if isinstance(exc, asyncio.TimeoutError) or _s241_gen_timeout(exc):
                    try:
                        from backend.core.ouroboros.governance.dw_discovery_runner import (
                            get_ttft_observer as _zs_get_obs,
                        )
                        _zs_obs = _zs_get_obs()
                        if _zs_obs is not None and model_id:
                            _zs_obs.record_timeout(model_id, op_id=op_id_short)
                    except Exception:  # noqa: BLE001 — never block dispatch
                        pass
                if _s241_gen_timeout(exc):
                    # Slice 241 — OUR op-level tool-loop budget exhaustion
                    # (tool_loop_deadline / max_rounds / starved), NOT a DW
                    # transport rupture. Classify GENERATION_TIMEOUT so the
                    # ==LIVE_TRANSPORT degrade/sever consumers ignore it and the
                    # topology breaker (weight 0.0) never trips on OUR budget.
                    # Stops blaming DoubleWord's network for our generation budget.
                    failure_source = FailureSource.GENERATION_TIMEOUT
                elif _fsm_exhausted(exc):
                    # Sovereign Exception Taxonomy (2026-06-20) — OUR-side FSM
                    # dispatch exhaustion (DW produced no candidate AND no Claude
                    # fallback configured under pure-DW autarky). NOT a vendor
                    # rupture: no socket failed, the vendor rejected nothing. The
                    # cloud soak proved a single
                    # ``all_providers_exhausted:fallback_skipped:no_fallback_configured``
                    # was mislabeled LIVE_TRANSPORT on all 16 models, severing the
                    # whole DW lane + corrupting surface-health. Classify
                    # FSM_EXHAUSTED (weight 0.0) so it fails ONLY this op without
                    # severing the lane or touching the vendor ledger.
                    failure_source = FailureSource.FSM_EXHAUSTED
                elif _is_modality or _is_auth_terminal:
                    # Slice H — terminal failure class. Even though we
                    # report it as LIVE_HTTP_5XX semantics here for
                    # back-compat, the breaker (Slice H wiring) will
                    # read the structured exception fields when
                    # available and flip the model's state to
                    # TERMINAL_OPEN. For now, classify with a body-
                    # accurate failure source so observers can audit
                    # the unmasked status.
                    failure_source = FailureSource.LIVE_TRANSPORT
                elif _status_code is not None:
                    # Structured HTTP status drives classification
                    if _status_code == 429:
                        failure_source = FailureSource.LIVE_HTTP_429
                    elif _status_code in (500, 502, 503, 504):
                        failure_source = FailureSource.LIVE_HTTP_5XX
                    elif _status_code == 0:
                        # Non-HTTP failure: stream stall / DNS / TLS
                        if (
                            "stream" in err_lower
                            and ("stall" in err_lower or "timeout" in err_lower)
                        ) or "streamtimeouterror" in err_lower:
                            failure_source = FailureSource.LIVE_STREAM_STALL
                        else:
                            failure_source = FailureSource.LIVE_TRANSPORT
                    else:
                        failure_source = FailureSource.LIVE_TRANSPORT
                else:
                    # No status_code attribute → fall back to regex on
                    # str(exc) (legacy path for non-DW exceptions).
                    if (
                        "stream" in err_lower
                        and ("stall" in err_lower or "timeout" in err_lower)
                    ) or "streamtimeouterror" in err_lower:
                        failure_source = FailureSource.LIVE_STREAM_STALL
                    elif "429" in err_str:
                        failure_source = FailureSource.LIVE_HTTP_429
                    elif "5" in err_str[:5] and (
                        "500" in err_str or "502" in err_str
                        or "503" in err_str or "504" in err_str
                    ):
                        failure_source = FailureSource.LIVE_HTTP_5XX
                    elif "parse" in err_lower or "json" in err_lower:
                        failure_source = FailureSource.LIVE_PARSE_ERROR
                    else:
                        failure_source = FailureSource.LIVE_TRANSPORT
                try:
                    # Pass structured fields to the sentinel so Slice H
                    # can use them for terminal-vs-transient decisions.
                    # Backward-compatible: legacy report_failure
                    # signature is preserved; structured fields are
                    # added via best-effort kwargs that the sentinel
                    # silently drops if it doesn't yet support them.
                    try:
                        sentinel.report_failure(
                            model_id, failure_source,
                            f"{type(exc).__name__}:{err_str[:120]}",
                            status_code=_status_code,
                            response_body=_response_body,
                            is_terminal=(_is_modality or _is_auth_terminal),
                        )
                    except TypeError:
                        # Sentinel doesn't accept new kwargs yet (pre-
                        # Slice-H sentinel) — fall back to legacy call
                        sentinel.report_failure(
                            model_id, failure_source,
                            f"{type(exc).__name__}:{err_str[:120]}",
                        )
                except Exception:
                    logger.debug(
                        "[CandidateGenerator] sentinel.report_failure raised",
                        exc_info=True,
                    )
                last_failure = (
                    f"{model_id}:{failure_source.value}:"
                    f"{type(exc).__name__}"
                )
                # Slice F — log the unmasked status_code + body excerpt
                # alongside the legacy WARNING line so operators see
                # ground truth in debug.log immediately.
                if _status_code is not None and _status_code > 0:
                    logger.warning(
                        "[CandidateGenerator] Sentinel dispatch: model=%s "
                        "FAILED (source=%s, http_%d, body=%r%s%s) — "
                        "trying next (op=%s)",
                        model_id, failure_source.value, _status_code,
                        _response_body[:160],
                        ", modality_terminal=true" if _is_modality else "",
                        ", auth_terminal=true" if _is_auth_terminal else "",
                        op_id_short,
                    )
                else:
                    logger.warning(
                        "[CandidateGenerator] Sentinel dispatch: model=%s "
                        "FAILED (source=%s, exc=%s) — trying next (op=%s)",
                        model_id, failure_source.value,
                        # Observability (2026-06-20): un-swallow the message. The
                        # prior log emitted only ``type(exc).__name__`` — which hid
                        # that a "live_transport RuntimeError" was actually an
                        # internal ``...:no_fallback_configured`` FSM exhaustion,
                        # costing two long blind diagnosis passes. Include the
                        # (bounded) message so the real cause is visible at WARNING.
                        f"{type(exc).__name__}: {err_str[:300]!r}", op_id_short,
                    )
                attempts[-1] = f"{model_id}:failed:{failure_source.value}"
                try:
                    # Slice 201 — feed the bandit a FAILURE reward for this arm
                    # so its posterior learns which models actually deliver.
                    from backend.core.ouroboros.governance.bandit_router import (
                        get_bandit_router as _s201_bandit_fail,
                    )
                    _s201_bandit_fail().record_outcome(model_id, success=False)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    # Override Matrix — feed the model-pin soft-lock a real
                    # failure (429/500/live-transport). At threshold the pin
                    # enters cooldown and routing yields to the EWMA ranking.
                    from backend.core.ouroboros.governance.model_pinning_heuristic import (
                        note_pin_outcome as _pin_fail,
                    )
                    _pin_fail(model_id, success=False)
                except Exception:  # noqa: BLE001
                    pass
                # Slice 77 — dynamic transport telemetry. The moment a LIVE
                # generation confirms a transport break, feed it into the
                # dw_surface_health ledger so the NEXT op's Slice 76 P2
                # pre-flight gate fires and skips the dead DW lane (closes the
                # stale-boot-probe gap found in the EVAL-2 Phase-4 re-run,
                # §50.11). Only LIVE_TRANSPORT — 429/5xx/parse are model- or
                # request-specific, not a transport-wide break.
                if failure_source is FailureSource.LIVE_TRANSPORT:
                    _note_dw_live_transport_degraded(
                        f"{model_id}:{type(exc).__name__}",
                        model_id=model_id,  # Slice 175 — attribute the rupture to THIS model
                    )
                    _consecutive_lt += 1
                    # Slice 182 Gap 2 — HEDGE AT THE RUPTURE BOUNDARY. The first rupture is the
                    # absolute first line of defense: immediately COMMAND the remaining probes
                    # in THIS dispatch onto batch, so a fresh-session rupture (before Gap 1's
                    # persisted-degraded signal exists) doesn't walk all 6 models through RT.
                    if not _s182_force_batch:
                        try:
                            from backend.core.ouroboros.governance.doubleword_provider import (
                                dw_hedge_enabled as _s182_hedge_on,
                                _dw_batch_lane_healthy as _s182_bok,
                            )
                            if _s182_hedge_on() and _s182_bok():
                                _s182_force_batch = True
                                logger.warning(
                                    "[Immortal] rupture HEDGE at sentinel boundary: %s ruptured "
                                    "→ remaining probes switched to BATCH (op=%s)",
                                    model_id, op_id_short,
                                )
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    # Slice 83 Phase 2 — a non-transport failure (429/5xx/parse)
                    # proves THIS model's transport is reachable, so the prior
                    # transport breaks were per-model, not lane-wide. Reset the
                    # streak: a genuine blackout is N transport breaks in a row.
                    _consecutive_lt = 0
                    # Slice 176 — fuse the non-transport vector into the predictor
                    # (economic 429 / upstream 5xx+parse / stall), per-model + weighted.
                    _record_dw_failure_signal(model_id, failure_source)
                # Slice 73 + Slice 83 Phase 2 — structural transport short-circuit,
                # now streak-gated. A LIVE_TRANSPORT break MIGHT mean the whole DW
                # endpoint is down — but with the Slice 82/83 heterogeneous coder
                # stack (DeepSeek-V4-Pro / Kimi / GLM / Qwen are distinct served
                # endpoints) a single break may just be one model bouncing. So we
                # ROTATE to the next coder on the first break and only sever once
                # `threshold` consecutive models have ALL failed transport — the
                # signature of a real lane-wide blackout. Severing too early
                # starves the Claude fallback (bt-2026-06-03
                # deadline_exhausted_pre_fallback); rotating too long burns budget
                # on a dead lane. The streak threshold balances both. `=1`
                # reproduces exact Slice 73 first-failure sever.
                if (
                    structural_fast_cascade_enabled()
                    and should_sever_dw_lane(failure_source)
                    and _consecutive_lt >= _lt_sever_threshold
                ):
                    _severed = len(ranked_models) - len(attempts)
                    logger.warning(
                        "[CandidateGenerator] Slice 73/83 structural fast-cascade: "
                        "model=%s LIVE_TRANSPORT streak=%d>=%d — severing DW lane, "
                        "cascading to fallback with full budget (op=%s, skipped %d "
                        "sibling model(s))",
                        model_id, _consecutive_lt, _lt_sever_threshold,
                        op_id_short, max(0, _severed),
                    )
                    break
                if (
                    failure_source is FailureSource.LIVE_TRANSPORT
                    and structural_fast_cascade_enabled()
                ):
                    logger.info(
                        "[CandidateGenerator] Slice 83 granular isolation: "
                        "model=%s LIVE_TRANSPORT streak=%d<%d — rotating to next "
                        "coder, DW lane stays open (op=%s)",
                        model_id, _consecutive_lt, _lt_sever_threshold,
                        op_id_short,
                    )
                continue
        # All DW models exhausted (either OPEN or failed). The
        # per-attempt ContextVar was already reset by each loop
        # iteration's finally block (Slice 3.6) — no further cleanup
        # needed before cascade-to-Claude / queue.
        logger.warning(
            "[CandidateGenerator] Sentinel dispatch: route=%s exhausted "
            "all %d DW models [%s] — applying fallback_tolerance=%s "
            "(op=%s, last_failure=%s)",
            provider_route, len(ranked_models),
            ", ".join(attempts),
            fallback_tolerance, op_id_short, last_failure or "none",
        )
        if fallback_tolerance == "queue":
            # Defect #5 fix (2026-05-03) — Read-only cascade reflex.
            # Soak v5 (bt-2026-05-03-060330) had 17/19 BG ops terminal-
            # failing here with "background_dw_blocked_by_topology".
            # The legacy reflex in _generate_background() (line ~2806)
            # already turns this into a Claude cascade for read-only
            # ops, but THAT reflex is unreachable because we raise
            # BEFORE returning to _generate_background. Lift the same
            # logic here so it actually fires.
            #
            # Cost contract preserved: read-only ops are policy-safe
            # because Rule 0d (in policy_engine.py) refuses every
            # mutating tool under is_read_only=True. Cascading a
            # read-only op to Claude carries no write risk; only
            # synthesis cost (~$0.005/op).
            #
            # Mutating BG ops still respect JARVIS_BACKGROUND_ALLOW_
            # FALLBACK env knob — they fall through to the queue
            # raise below if the operator hasn't opted in.
            _is_read_only = bool(
                getattr(context, "is_read_only", False),
            )
            _allow_mutating_fallback = (
                provider_route == "background"
                and os.environ.get(
                    "JARVIS_BACKGROUND_ALLOW_FALLBACK", "",
                ).strip().lower() in {"1", "true", "yes", "on"}
            )
            _can_cascade = (
                self._fallback is not None
                and (_is_read_only or _allow_mutating_fallback)
            )
            # Slice 124 — Autonomous Economic Router. On a HARD economic block
            # (DW http_402 balance / 429 rate-limit), a small read-only (or
            # opt-in) BACKGROUND op should not dead-queue: cascade it to the
            # cheap Claude tier to preserve momentum, while MASSIVE ops stay
            # queued (don't pay Claude prices for a big background op). This
            # EXTENDS the read-only cascade with an economic size-gate; the
            # cheap model is resolved from JARVIS_ECONOMIC_FAILOVER_MODEL (no
            # hardcode). Gated + fail-open; default-off → byte-identical.
            try:
                from backend.core.ouroboros.governance import economic_router as _ER

                if _ER.economic_router_enabled() and self._fallback is not None:
                    _prompt_chars = len(str(getattr(context, "prompt", "") or "")) \
                        or len(str(getattr(context, "description", "") or ""))
                    _econ = _ER.decide(
                        route=provider_route,
                        error_text=last_failure or "",
                        prompt_chars=_prompt_chars,
                        is_read_only=_is_read_only,
                    )
                    if _econ.action is _ER.EconomicAction.CASCADE_CHEAP:
                        _can_cascade = True
                        logger.info(
                            "[CandidateGenerator] EconomicRouter: %s → cascade to "
                            "cheap tier '%s' (op=%s, %s)",
                            _econ.reason, _econ.model or "(default fallback)",
                            op_id_short, provider_route,
                        )
                    elif _econ.action is _ER.EconomicAction.QUEUE:
                        # Massive/unsafe op on a hard block — keep it queued
                        # (overrides a would-be read-only cascade for cost).
                        _can_cascade = False
                        logger.info(
                            "[CandidateGenerator] EconomicRouter: %s → staying "
                            "queued for cheap provider (op=%s)",
                            _econ.reason, op_id_short,
                        )
                    # Slice 136 — economic-router cognitive synapse. Fired from
                    # OUTSIDE the pure decide() (the AST-pinned classifier has no
                    # side effects), so the organism remembers its economic
                    # failover decisions. Coalesced per op; gated + non-blocking +
                    # fail-soft.
                    try:
                        from backend.core.ouroboros.governance.episodic_core import (
                            note_route_nowait as _note_route,
                        )
                        _note_route(
                            op_id=str(op_id_short or ""),
                            router="economic",
                            summary=(f"economic {_econ.action.value} → "
                                     f"{_econ.model or 'cheap-default'}"),
                            context={
                                "action": _econ.action.value,
                                "tier": _econ.model or "cheap_default",
                                "route": str(provider_route),
                                "reason": _econ.reason,
                            },
                        )
                    except Exception:  # noqa: BLE001 — synapse never perturbs routing
                        pass
            except Exception:  # noqa: BLE001 - economic routing is best-effort
                logger.debug("[CandidateGenerator] EconomicRouter consult skipped", exc_info=True)
            if _can_cascade:
                _cascade_reason = (
                    "read_only_cost_safe"
                    if _is_read_only
                    else "operator_allow_fallback_env"
                )
                logger.info(
                    "[CandidateGenerator] Sentinel queue tolerance "
                    "OVERRIDE: route=%s cascading to Claude (%s, "
                    "op=%s, fallback_tolerance=queue but is_read_only=%s "
                    "or allow_fallback_env=%s) — Defect #5 fix "
                    "2026-05-03 lifts the read-only reflex from "
                    "_generate_background where it was unreachable "
                    "after sentinel raise",
                    provider_route, _cascade_reason, op_id_short,
                    _is_read_only, _allow_mutating_fallback,
                )
                return await self._call_fallback(context, deadline)
            # Same exception shape the orchestrator's existing
            # accept-failure branch already handles for BG/SPEC.
            if provider_route == "speculative":
                _note_dw_total_outage(last_failure or "")  # Slice 53
                raise RuntimeError(
                    f"speculative_deferred:dw_severed_queued:"
                    f"{(last_failure or 'all_models_open')[:120]}"
                )
            _note_dw_total_outage(last_failure or "")  # Slice 53
            raise RuntimeError(
                f"background_dw_blocked_by_topology:"
                f"dw_severed_queued:"
                f"{(last_failure or 'all_models_open')[:120]}"
            )
        # cascade_to_claude — Claude is the explicit cost contract, BUT only when
        # the Claude lane is actually alive. Slice 238: consult the SAME economic
        # breaker the primary lane respects (read-only ``_claude_breaker_open`` —
        # no probe side-effect) before cascading. When it is OPEN (Claude
        # economically/transport dead) the cascade is suppressed and the op routes
        # to the immortal DW-retry / clean-degrade branch below instead of
        # poisoning the op via a known-dead lane (terminal_quota). Breaker CLOSED
        # → byte-identical legacy cascade (a funded Claude is used normally).
        _claude_lane_open = False
        try:
            from backend.core.ouroboros.governance.doubleword_provider import (
                _claude_breaker_open as _cascade_breaker_open,
            )
            _claude_lane_open = _cascade_breaker_open()
        except Exception:  # noqa: BLE001 — advisory; never block dispatch
            _claude_lane_open = False
        _do_cascade = should_cascade_to_claude(
            has_fallback=self._fallback is not None,
            claude_breaker_open=_claude_lane_open,
            enabled=cascade_breaker_consult_enabled(),
        )
        if not _do_cascade and self._fallback is not None and _claude_lane_open:
            logger.warning(
                "[CandidateGenerator] Slice238 cascade-to-claude SUPPRESSED: "
                "Claude breaker OPEN (economic/transport) — not poisoning op via "
                "the known-dead lane (terminal_quota); routing to immortal "
                "DW-retry/degrade (op=%s, last=%s)",
                op_id_short, (last_failure or "?")[:60],
            )
        # cascade_to_claude — Claude is the explicit cost contract.
        if not _do_cascade:
            # Slice 180 — THE IMMORTAL EXECUTION LAYER. Raising here DELETES the op (the
            # soak's all_providers_exhausted bleed). With NO fallback configured, exhausting
            # is unacceptable. Instead → QUEUE_ONLY: exponential-backoff and RE-ATTEMPT the
            # full DW dispatch until the vendor recovers, bounded by the op's own deadline +
            # a capped attempt count. A transient TOTAL DW outage is survived (the warm-boot
            # + intra-DW failover route the recovered attempt to batch); a permanently-dead
            # DW still fails — but only after exhausting the queue budget, never instantly.
            try:
                from backend.core.ouroboros.governance.dw_immortal import (
                    immortal_should_retry as _imm_should_retry,
                    immortal_backoff_s as _imm_backoff,
                    immortal_max_attempts as _imm_max,
                    immortal_max_wait_s as _imm_max_wait,
                    immortal_per_attempt_window_s as _imm_window,
                )
                import time as _imm_time
                from datetime import datetime as _imm_dt, timezone as _imm_tz, timedelta as _imm_td
                _imm_now = _imm_time.time()
                # Slice 182 Gap 3 — the immortal budget is DETACHED from the op's 120s generation
                # deadline: a separate, much-longer wall (default 1h) computed ONCE and threaded
                # across the retry recursion, so a sustained DW outage doesn't expire the op.
                _imm_budget = (
                    _immortal_budget_deadline if _immortal_budget_deadline is not None
                    else (_imm_now + _imm_max_wait())
                )
                if _imm_should_retry(
                    deadline=_imm_budget, now=_imm_now, claude_available=False,
                    attempt=_immortal_attempt, max_attempts=_imm_max(),
                ):
                    _imm_delay = _imm_backoff(_immortal_attempt)
                    logger.warning(
                        "[Immortal] DW exhausted + NO fallback → QUEUE_ONLY (deadline-detached): "
                        "backoff %.1fs then re-attempt #%d, budget %.0fs remaining (op NEVER lost; "
                        "op=%s, last=%s)",
                        _imm_delay, _immortal_attempt + 1, max(0.0, _imm_budget - _imm_now),
                        op_id_short, (last_failure or "?")[:60],
                    )
                    await asyncio.sleep(_imm_delay)
                    # FRESH generation window for the retry (not the original op's elapsed deadline)
                    _imm_fresh_deadline = _imm_dt.now(_imm_tz.utc) + _imm_td(seconds=_imm_window())
                    return await self._dispatch_via_sentinel(
                        context, _imm_fresh_deadline, provider_route,
                        _immortal_attempt=_immortal_attempt + 1,
                        _immortal_budget_deadline=_imm_budget,
                    )
            except Exception as _imm_exc:  # noqa: BLE001 — the immortal layer must never itself break the op
                logger.debug("[Immortal] queue-retry path swallowed: %r", _imm_exc)
            _note_dw_total_outage(last_failure or "")  # Slice 53
            raise RuntimeError(
                f"sentinel_dispatch_no_fallback:"
                f"{(last_failure or 'all_models_open')[:120]}"
            )
        return await self._call_fallback(context, deadline)

    async def _generate_immediate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """IMMEDIATE route: Claude direct, skip DW entirely.

        For critical-urgency operations where every second counts:
        test failures, voice commands, runtime health critical.

        Cost: ~$0.03/op (Claude only)
        Latency: 15-30s (no DW overhead)
        """
        logger.info(
            "[CandidateGenerator] IMMEDIATE route: Claude direct "
            "(skip DW, urgency=%s, source=%s) [%.1fs remaining]",
            getattr(context, "signal_urgency", "?"),
            getattr(context, "signal_source", "?"),
            self._remaining_seconds(deadline),
        )

        # Try Claude as primary first, then fallback if available.
        # Skip the entire Tier 0 / DW path.
        state = self.fsm.state
        if state is FailbackState.QUEUE_ONLY:
            self._raise_exhausted(
                "queue_only_immediate",
                context=context,
                deadline=deadline,
            )

        # If DW IS the primary, go straight to Claude (the fallback).
        _dw_is_primary = (self._tier0 is not None and self._primary is self._tier0)
        if _dw_is_primary:
            # Slice 127 P2.1 — fallback-skip gate. Claude-direct would just
            # grind an IMMEDIATE op against a depleted Claude lane (the live
            # soak: terminal_quota x N, no completion). When the Claude lane
            # breaker is OPEN (economic/transport), reroute to the funded DW
            # primary instead. Slice 162 — read the breaker STATE (read-only) via the
            # Slice 161 predicate, NOT should_allow_request(): the latter flickers True
            # during a HALF_OPEN probe AND has a side effect (consumes the probe slot),
            # so an IMMEDIATE op kept hammering a dead-but-probing Claude and exhausted
            # before the gate. Now CLOSED → Claude-direct (self-heal); OPEN/HALF_OPEN →
            # reroute to funded DW. Gated default-FALSE → OFF is unchanged Claude-direct.
            if fallback_skip_gate_enabled():
                try:
                    from backend.core.ouroboros.governance.claude_circuit_breaker import (  # noqa: E501
                        get_claude_circuit_breaker as _p21_ccb,
                        is_enabled as _p21_ccb_enabled,
                    )
                    from backend.core.ouroboros.governance.doubleword_provider import (
                        _claude_breaker_open as _p21_breaker_open,
                    )
                    _p21_allows = not _p21_breaker_open(getter=_p21_ccb)
                    if immediate_reroute_to_dw(
                        dw_is_primary=True,
                        gate_enabled=True,
                        claude_breaker_enabled=_p21_ccb_enabled(),
                        claude_allows_request=_p21_allows,
                    ):
                        logger.warning(
                            "[CandidateGenerator] IMMEDIATE reroute → DW: "
                            "Claude lane breaker OPEN (economic/transport) — "
                            "bypassing depleted Claude, routing to funded DW "
                            "primary (op=%s)",
                            getattr(context, "op_id", "?"),
                        )
                        return await self._call_primary(context, deadline)
                except Exception:  # noqa: BLE001 — never block dispatch
                    pass
            return await self._call_fallback(context, deadline)

        # Otherwise try primary (Claude/J-Prime), then fallback.
        return await self._try_primary_then_fallback(context, deadline)

    async def _try_jprime_primacy(
        self,
        context: OperationContext,
        deadline: datetime,
        *,
        route_label: str,
    ) -> Optional[GenerationResult]:
        """Phase 3 Scope α: try J-Prime first for BACKGROUND/SPECULATIVE.

        Returns the ``GenerationResult`` on success, or ``None`` to
        signal "fall through to DW". Never raises — all failure modes
        (flag off, no handle, sem saturated, generate error, empty
        result) are translated into a ``None`` return plus a counter
        bump so the caller can take the DW-only path unchanged.

        Parameters
        ----------
        context, deadline:
            The usual generation args, forwarded to J-Prime unchanged.
        route_label:
            ``"BACKGROUND"`` or ``"SPECULATIVE"``. Used only in log
            messages so operators can tell which route took which
            branch of the primacy path.

        Why a pre-check on ``self._jprime_sem.locked()``:
            ``asyncio.Semaphore(1)`` with overflow-fall-through has no
            clean primitive. We want "try to grab it right now, and if
            already held, don't queue — go to DW instead." The
            ``locked()`` check is a tiny race (a sibling op could take
            the token in the gap between the check and the acquire),
            but the worst case is that we serialize two ops for one
            J-Prime call, which is harmless. Using
            ``wait_for(acquire, timeout=0)`` would raise
            ``CancelledError`` on some asyncio versions and obscure
            the intent; ``locked()`` is clearer.
        """
        # Deferred import — ``jprime_primacy_enabled`` is a module-level
        # function in ``_governance_state``, and fetching it at call
        # time keeps the hot-path branch cheap when the flag is off.
        from ._governance_state import jprime_primacy_enabled

        # Quota Shield: prefer_local ops use the same local-first primacy path even
        # when jprime_primacy is otherwise off for this route.
        if not (jprime_primacy_enabled() or getattr(context, "prefer_local", False)):
            return None
        if self._jprime is None or not getattr(
            self._jprime, "provider_name", ""
        ):
            return None

        # Sem saturation — a sibling op is already using the single
        # client-side slot. Don't queue; fall through to DW so the
        # background workload doesn't serialize behind one J-Prime call.
        if self._jprime_sem.locked():
            self._jprime_counters.jprime_sem_overflows += 1
            self._jprime_counters.fallthrough_to_dw += 1
            logger.info(
                "[CandidateGenerator] %s: J-Prime sem saturated (overflows=%d) "
                "— falling through to DW",
                route_label,
                self._jprime_counters.jprime_sem_overflows,
            )
            return None

        remaining = self._remaining_seconds(deadline)
        if remaining <= 0.0:
            # No budget left — fall through silently so the DW path
            # can emit its own deadline-exceeded diagnostic.
            return None

        async with self._jprime_sem:
            try:
                result = await asyncio.wait_for(
                    self._jprime.generate(context, deadline),
                    timeout=min(remaining, 180.0),
                )
            except asyncio.TimeoutError:
                self._jprime_counters.jprime_failures += 1
                self._jprime_counters.fallthrough_to_dw += 1
                logger.info(
                    "[CandidateGenerator] %s: J-Prime primacy timeout after "
                    "%.1fs — falling through to DW",
                    route_label,
                    remaining,
                )
                return None
            except Exception as exc:
                self._jprime_counters.jprime_failures += 1
                self._jprime_counters.fallthrough_to_dw += 1
                logger.info(
                    "[CandidateGenerator] %s: J-Prime primacy error "
                    "%s(%s) — falling through to DW",
                    route_label,
                    type(exc).__name__,
                    exc,
                )
                return None

        if result is None or len(getattr(result, "candidates", ()) or ()) == 0:
            self._jprime_counters.jprime_failures += 1
            self._jprime_counters.fallthrough_to_dw += 1
            logger.info(
                "[CandidateGenerator] %s: J-Prime primacy returned no "
                "candidates — falling through to DW",
                route_label,
            )
            return None

        self._jprime_counters.jprime_hits += 1
        logger.info(
            "[CandidateGenerator] %s: J-Prime primacy hit — %d candidates "
            "in %.1fs (hits=%d, overflows=%d, failures=%d)",
            route_label,
            len(result.candidates),
            getattr(result, "generation_duration_s", 0.0) or 0.0,
            self._jprime_counters.jprime_hits,
            self._jprime_counters.jprime_sem_overflows,
            self._jprime_counters.jprime_failures,
        )
        return result

    async def _generate_background(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """BACKGROUND route: DW primary, optional Claude safety-net cascade.

        For low-urgency background sensors: opportunity mining,
        doc staleness, TODO scanning, backlog items.

        Default behavior (``JARVIS_BACKGROUND_ALLOW_FALLBACK`` unset): DW
        only, no Claude cascade. Cost ~$0.002/op. Raises
        ``RuntimeError("background_dw_*")`` on failure — the orchestrator
        accepts it gracefully and the sensor re-detects if still relevant.

        Nervous-system reflex (``JARVIS_BACKGROUND_ALLOW_FALLBACK=true``):
        when DW times out, is empty, or errors, cascade to Claude via
        :meth:`_call_fallback`. Diagnosed after bt-2026-04-14-041952
        showed **11/11 BACKGROUND ops dying on `background_dw_timeout:180s`**
        — every op exhausted its DW window, nothing reached the Iron
        Gate, and the cost-optimization invariant of the route became a
        100% failure mode. Staking survival of background cognition on a
        single latency-bound provider without a safety net violates
        Manifesto §5 (intelligence-driven routing) and §6
        (threshold-triggered neuroplasticity). On fallback failure, we
        raise ``RuntimeError("background_fallback_failed:...")`` so the
        orchestrator's existing BACKGROUND accept-failure branch still
        fires (no retry loop thrash).

        Bypass (``FORCE_CLAUDE_BACKGROUND=true``): skip DW entirely and
        call Claude directly. Used by the live-fire harness to unblock
        parity validation when DW 397B is degraded — hands BACKGROUND
        cognition straight to Claude so the generation actually reaches
        the tool loop and the Iron Gate can be exercised.

        Phase 3 Scope α (``JARVIS_JPRIME_PRIMACY``): when enabled and a
        PrimeProvider handle is wired, :meth:`_try_jprime_primacy` is
        consulted first. Sem saturation or any failure falls through to
        the DW path below unchanged.
        """
        _urgency = getattr(context, "signal_urgency", "?")
        _source = getattr(context, "signal_source", "?")
        _is_read_only = bool(getattr(context, "is_read_only", False))
        remaining = self._remaining_seconds(deadline)

        _force_claude = os.environ.get(
            "FORCE_CLAUDE_BACKGROUND", "",
        ).strip().lower() in {"1", "true", "yes", "on"}
        _allow_fallback = os.environ.get(
            "JARVIS_BACKGROUND_ALLOW_FALLBACK", "",
        ).strip().lower() in {"1", "true", "yes", "on"}
        # Nervous System Reflex: read-only ops ALWAYS get the Claude cascade
        # on DW failure, regardless of the env gate. Locking a read-only
        # cartography op onto a paused DW endpoint is the exact failure
        # mode that prompted this reflex (bt-2026-04-18-032820).
        if _is_read_only and self._fallback is not None:
            _allow_fallback = True

        # ── FORCE_CLAUDE_BACKGROUND bypass ─────────────────────────────
        # Skip DW entirely and route straight to Claude. No DW attempt,
        # no timeout, no cascade — used when DW is known-degraded and
        # we need BACKGROUND ops to actually reach the tool loop.
        if _force_claude:
            if self._fallback is None:
                raise RuntimeError(
                    "background_dw_unavailable:force_claude_set_but_no_fallback"
                )
            logger.info(
                "[CandidateGenerator] BACKGROUND: FORCE_CLAUDE_BACKGROUND=true "
                "— bypassing DW, calling Claude directly "
                "(urgency=%s, source=%s) [%.1fs budget]",
                _urgency, _source, remaining,
            )
            try:
                return await self._call_fallback(context, deadline)
            except Exception as exc:
                raise RuntimeError(
                    f"background_fallback_failed:forced:"
                    f"{type(exc).__name__}:{str(exc)[:100]}"
                ) from exc

        logger.info(
            "[CandidateGenerator] BACKGROUND route: DW primary%s "
            "(urgency=%s, source=%s, read_only=%s) [%.1fs budget"
            "%s]",
            " + Claude cascade" if _allow_fallback else " (no Claude cascade)",
            _urgency, _source, _is_read_only, remaining,
            f", DW stall budget={_BG_READONLY_DW_STALL_BUDGET_S:.0f}s"
            if _is_read_only else "",
        )

        # Phase 3 Scope α — J-Prime primacy pre-check. Returns
        # ``None`` when the flag is off, no handle is wired, the sem is
        # saturated, or the J-Prime call failed. On ``None``, drop into
        # the existing DW path below.
        _primacy_result = await self._try_jprime_primacy(
            context, deadline, route_label="BACKGROUND",
        )
        if _primacy_result is not None:
            return _primacy_result

        if self._tier0 is None or not getattr(self._tier0, "is_available", False):
            # DW not configured — cascade to Claude if allowed, else raise.
            if _allow_fallback and self._fallback is not None:
                logger.info(
                    "[CandidateGenerator] BACKGROUND: DW unavailable — "
                    "cascading to Claude fallback [%s]",
                    getattr(context, "op_id", "?")[:16],
                )
                try:
                    return await self._call_fallback(context, deadline)
                except Exception as exc:
                    raise RuntimeError(
                        f"background_fallback_failed:dw_unavailable:"
                        f"{type(exc).__name__}:{str(exc)[:100]}"
                    ) from exc
            raise RuntimeError(
                "background_dw_unavailable:tier0_not_configured"
            )

        # Reserve a slice of the BACKGROUND budget for Claude when
        # cascade is enabled so DW can't burn the entire window. The
        # DW cap here and the urgency_router's max_dw_wait_s for
        # BACKGROUND must agree — both tightened to 150s when fallback
        # is enabled.
        #
        # Nervous-System Reflex: read-only ops get a MUCH tighter DW
        # stall budget (default 60s via JARVIS_BG_DW_STALL_BUDGET_S)
        # so lockup is bounded. The Trinity cartography op is the
        # canonical case — it needs to reach the tool loop quickly so
        # dispatch_subagent can fan out; spending 150s on a stalled DW
        # stream is dead time the subagent fleet will never recover.
        if _is_read_only:
            _dw_cap = _BG_READONLY_DW_STALL_BUDGET_S
        else:
            _dw_cap = 150.0 if _allow_fallback else 180.0
        _dw_timeout = min(remaining, _dw_cap)
        _dw_error: Optional[str] = None

        # DW attempt — RT SSE preferred, batch fallback.
        # Phase 12 Slice F — Substrate Error Unmasking. Preserve the
        # underlying DoublewordInfraError on this attempt so the
        # sentinel-driven dispatcher can read its status_code +
        # response_body fields directly. The exception is still
        # caught here (so the legacy non-sentinel path can fall
        # through to Claude as before via _dw_error string), but
        # _structured_error captures the structured object for the
        # caller — when present, the caller re-raises it instead of
        # stringifying it through RuntimeError(_dw_error).
        _structured_error: Optional[Exception] = None
        if getattr(self._tier0, "_realtime_enabled", False):
            try:
                result = await asyncio.wait_for(
                    self._tier0.generate(context, deadline),
                    timeout=_dw_timeout,
                )
                if result is not None and len(result.candidates) > 0:
                    logger.info(
                        "[CandidateGenerator] BACKGROUND: DW produced %d candidates "
                        "in %.1fs ($%.4f)",
                        len(result.candidates),
                        result.generation_duration_s,
                        getattr(result, "cost_usd", 0.0),
                    )
                    return result
                _dw_error = "background_dw_empty_result"
            except asyncio.TimeoutError:
                _dw_error = f"background_dw_timeout:{_dw_timeout:.0f}s"
            except Exception as exc:
                _structured_error = exc  # Slice F preserves the object
                # Build a richer _dw_error that surfaces status_code
                # + a body excerpt when available (DoublewordInfraError),
                # so legacy log-line consumers see ground truth too.
                _status = getattr(exc, "status_code", None)
                _body = getattr(exc, "response_body", "") or ""
                if _status is not None:
                    _dw_error = (
                        f"background_dw_error:{type(exc).__name__}:"
                        f"http_{_status}:{_body[:120]}"
                    )
                else:
                    _dw_error = (
                        f"background_dw_error:{type(exc).__name__}:{exc}"
                    )
        else:
            # Legacy batch path
            try:
                pending = await self._tier0.submit_batch(context)
                if pending is None:
                    _dw_error = "background_dw_batch_submit_failed"
                else:
                    result = await asyncio.wait_for(
                        self._tier0.poll_and_retrieve(pending, context),
                        timeout=_dw_timeout,
                    )
                    if result is not None and len(result.candidates) > 0:
                        logger.info(
                            "[CandidateGenerator] BACKGROUND batch: DW produced "
                            "%d candidates",
                            len(result.candidates),
                        )
                        return result
                    _dw_error = "background_dw_batch_empty"
            except asyncio.TimeoutError:
                _dw_error = "background_dw_batch_timeout"
            except Exception as exc:
                _dw_error = (
                    f"background_dw_batch_error:{type(exc).__name__}"
                )

        # DW exhausted. Either cascade to Claude or raise.
        if _allow_fallback and self._fallback is not None:
            _post_dw_remaining = self._remaining_seconds(deadline)
            logger.info(
                "[CandidateGenerator] BACKGROUND: DW failed (%s) — "
                "cascading to Claude fallback, %.1fs parent remaining [%s]",
                _dw_error, _post_dw_remaining, getattr(context, "op_id", "?")[:16],
            )
            try:
                return await self._call_fallback(context, deadline)
            except Exception as exc:
                raise RuntimeError(
                    f"background_fallback_failed:dw={_dw_error[:80]}:"
                    f"{type(exc).__name__}:{str(exc)[:80]}"
                ) from exc

        # Phase 12 Slice F — Substrate Error Unmasking. When DW raised
        # a structured DoublewordInfraError (status_code + response_body
        # available), re-raise the ORIGINAL object so the sentinel
        # dispatch classifier can introspect status_code directly
        # without regex on str(exc). Falls through to RuntimeError when
        # the failure was a timeout / empty-result (no structured
        # exception to preserve).
        if _structured_error is not None:
            raise _structured_error
        raise RuntimeError(_dw_error)

    async def _generate_speculative(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """SPECULATIVE route: DW fire-and-forget pre-computation.

        For intent discovery, dream engine, proactive exploration.
        Submit to DW and don't block — store result for later use.

        Cost: ~$0.001/op (DW batch, tolerate high discard)
        Latency: N/A (async, result consumed later)

        Normally raises ``RuntimeError("speculative_deferred")`` — the
        orchestrator should mark this as a deferred operation, not a
        failure.

        Phase 3 Scope α (``JARVIS_JPRIME_PRIMACY``): when enabled and a
        PrimeProvider handle is wired, :meth:`_try_jprime_primacy` is
        consulted first. Because J-Prime on primacy runs synchronously
        inside the sem, a successful primacy hit *returns the result
        directly* instead of raising ``speculative_deferred`` — the
        caller gets a real synchronous result that can be used
        immediately, which is strictly better than a deferred batch.
        Sem saturation or any failure falls through to the existing
        DW fire-and-forget path below unchanged.
        """
        _source = getattr(context, "signal_source", "?")
        _op_id = getattr(context, "op_id", "unknown")

        logger.info(
            "[CandidateGenerator] SPECULATIVE route: DW fire-and-forget "
            "(source=%s, op=%s)",
            _source, _op_id,
        )

        # Phase 3 Scope α — J-Prime primacy pre-check. Synchronous hit
        # upgrades the op from deferred to completed; any miss falls
        # through to the legacy DW fire-and-forget path below.
        _primacy_result = await self._try_jprime_primacy(
            context, deadline, route_label="SPECULATIVE",
        )
        if _primacy_result is not None:
            return _primacy_result

        if self._tier0 is not None and getattr(self._tier0, "is_available", False):
            if getattr(self._tier0, "_realtime_enabled", False):
                # Use RT path but don't block — create background task.
                _gen_task = asyncio.ensure_future(
                    self._tier0.generate(context, deadline),
                )
                # Defect #4 Slice A — speculative pre-dispatch site.
                # Stored for later retrieval, but if op completes
                # without retrieving (timeout / route change /
                # demotion), the task continues. Callback consumes
                # exceptions silently.
                _gen_task.add_done_callback(_swallow_task_exception)
                # Store for later retrieval
                self._background_polls[_op_id] = _gen_task
                logger.info(
                    "[CandidateGenerator] SPECULATIVE: DW RT task dispatched "
                    "as background (op=%s)",
                    _op_id,
                )
            else:
                # Batch path — submit and background poll
                try:
                    pending = await self._tier0.submit_batch(context)
                    if pending is not None:
                        task = asyncio.create_task(
                            self._background_poll_tier0(pending, context),
                            name=f"speculative-{_op_id[:12]}",
                        )
                        # Defect #4 Slice A — defensive callback.
                        task.add_done_callback(_swallow_task_exception)
                        self._background_polls[_op_id] = task
                        logger.info(
                            "[CandidateGenerator] SPECULATIVE: DW batch submitted "
                            "(op=%s, batch=%s)",
                            _op_id, getattr(pending, "batch_id", "?"),
                        )
                except Exception as exc:
                    logger.debug(
                        "[CandidateGenerator] SPECULATIVE: batch submit failed: %s",
                        exc,
                    )

        # Always raise — speculative ops are deferred, not completed.
        raise RuntimeError("speculative_deferred")

    async def _background_poll_tier0(
        self, pending: Any, context: OperationContext,
    ) -> None:
        """Background task: poll Doubleword batch and store result when ready."""
        _op_id = pending.op_id
        try:
            assert self._tier0 is not None  # guaranteed by caller
            result = await self._tier0.poll_and_retrieve(pending, context)
            if result is not None and len(result.candidates) > 0:
                from backend.core.ouroboros.governance.doubleword_provider import (
                    CompletedBatch,
                )
                self._completed_batches[_op_id] = CompletedBatch(
                    op_id=_op_id,
                    batch_id=pending.batch_id,
                    result=result,
                    completed_at=time.monotonic(),
                )
                # Record TIER0_COMPLETE in ledger
                await self._record_tier0_ledger(
                    _op_id, "tier0_complete", {
                        "batch_id": pending.batch_id,
                        "candidates": len(result.candidates),
                        "provider": result.provider_name,
                        "duration_s": round(result.generation_duration_s, 1),
                    },
                )
                logger.info(
                    "[CandidateGenerator] Tier 0 background poll complete: "
                    "batch %s → %d candidates stored for op %s",
                    pending.batch_id, len(result.candidates), _op_id,
                )
            else:
                logger.info(
                    "[CandidateGenerator] Tier 0 background poll: "
                    "batch %s returned no usable candidates",
                    pending.batch_id,
                )
        except asyncio.CancelledError:
            logger.debug(
                "[CandidateGenerator] Tier 0 background poll cancelled: %s",
                pending.batch_id,
            )
        except Exception:
            logger.warning(
                "[CandidateGenerator] Tier 0 background poll failed: %s",
                pending.batch_id,
                exc_info=True,
            )
        finally:
            self._background_polls.pop(_op_id, None)

    def get_completed_batch(self, op_id: str) -> Optional[Any]:
        """Check if a Tier 0 async result is available for the given op_id."""
        return self._completed_batches.get(op_id)

    def pop_completed_batch(self, op_id: str) -> Optional[Any]:
        """Retrieve and remove a completed Tier 0 result for the given op_id."""
        return self._completed_batches.pop(op_id, None)

    async def _record_tier0_ledger(
        self, op_id: str, state_name: str, data: dict[str, Any],
    ) -> None:
        """Record a Tier 0 batch event in the governance ledger.

        Fails silently — ledger writes must never crash the pipeline.
        """
        if self._ledger is None:
            return
        try:
            from backend.core.ouroboros.governance.ledger import (
                LedgerEntry,
                OperationState,
            )
            entry = LedgerEntry(
                op_id=op_id,
                state=OperationState(state_name),
                data=data,
                entry_id=data.get("batch_id"),
            )
            await self._ledger.append(entry)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "[CandidateGenerator] Ledger write failed for %s:%s",
                op_id, state_name, exc_info=True,
            )

    async def run_health_probe(self) -> bool:
        """Probe the primary provider and update the FSM.

        Returns
        -------
        bool
            ``True`` if the primary is healthy.
        """
        try:
            healthy = await self._primary.health_probe()
        except Exception:
            logger.warning(
                "[CandidateGenerator] Health probe raised exception, treating as failure",
                exc_info=True,
            )
            healthy = False

        if healthy:
            self.fsm.record_probe_success()
        else:
            self.fsm.record_probe_failure()

        return healthy

    async def plan(self, prompt: str, deadline: datetime) -> str:
        """Send a planning prompt to the active provider, with soft fallback.

        Does NOT update the failback state machine on failure — planning errors
        are non-fatal and the orchestrator continues to GENERATE regardless.

        Raises RuntimeError("all_providers_exhausted") only if QUEUE_ONLY.
        """
        state = self.fsm.state

        if state is FailbackState.QUEUE_ONLY:
            self._raise_exhausted(
                "queue_only_plan",
                deadline=deadline,
                phase="plan",
            )

        if state is FailbackState.PRIMARY_READY:
            try:
                async with self._primary_sem:
                    remaining = self._remaining_seconds(deadline)
                    primary_budget = min(remaining, _TIER3_REFLEX_HARD_CAP_S)
                    if (
                        remaining > _TIER3_REFLEX_HARD_CAP_S + 1.0
                        and primary_budget >= _TIER3_REFLEX_HARD_CAP_S - 0.01
                    ):
                        logger.info(
                            "[CandidateGenerator] Plan Tier3_cap_active: "
                            "primary_budget=%.1fs (hard cap _TIER3_REFLEX_HARD_CAP_S=%.1fs), "
                            "remaining=%.1fs — PLAN primary will sever at cap "
                            "for Manifesto §5 cascade",
                            primary_budget, _TIER3_REFLEX_HARD_CAP_S, remaining,
                        )
                    return await asyncio.wait_for(
                        self._primary.plan(prompt, deadline),
                        timeout=primary_budget,
                    )
            except (Exception, asyncio.CancelledError) as exc:
                logger.warning(
                    "[CandidateGenerator] Primary plan() failed (%s: %s), trying fallback",
                    type(exc).__name__,
                    exc,
                )

        # FALLBACK_ACTIVE, PRIMARY_DEGRADED, or primary plan() just failed
        _sem_t0 = time.monotonic()
        logger.debug(
            "[CandidateGenerator] Plan fallback sem acquire: slots_free=%d/%d",
            self._fallback_sem._value, self._fallback_concurrency,
        )
        # Slice 12F-A — priority-aware acquisition. The plan() entry
        # point doesn't carry context (the prompt was already
        # composed by the orchestrator), so we use the empty-route
        # default which falls through to DEFAULT_PRIORITY (STANDARD
        # bucket — FIFO-equivalent within the bucket). The dominant
        # starvation wedge is on the call() path which DOES have
        # _op_route in scope. plan() acquisitions are rare relative
        # to call() acquisitions in the soak; keeping them at default
        # priority is structurally safe.
        from backend.core.ouroboros.governance.priority_semaphore import (  # noqa: E501
            acquire_priority_aware as _slice12f_acquire,
        )
        async with _slice12f_acquire(self._fallback_sem, ""):
            _sem_wait_s = time.monotonic() - _sem_t0
            _parent_remaining = self._remaining_seconds(deadline)
            _budget_target = max(_parent_remaining, _FALLBACK_MIN_GUARANTEED_S)
            remaining = min(_budget_target, _PLAN_FALLBACK_MAX_TIMEOUT_S)
            if remaining > _parent_remaining + 1.0:
                deadline = datetime.now(tz=timezone.utc) + timedelta(
                    seconds=remaining,
                )
            if _sem_wait_s > 1.0:
                logger.info(
                    "[CandidateGenerator] Plan fallback sem_wait=%.1fs "
                    "(budget=%.1fs)", _sem_wait_s, remaining,
                )
            return await asyncio.wait_for(
                self._fallback.plan(prompt, deadline),
                timeout=remaining,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _query_provider_recommendation(self, complexity: str) -> Optional[str]:
        """Query ProviderPerformanceTracker for a routing recommendation.

        Returns a provider name if learning data strongly recommends a
        non-default provider for this complexity class, else None.
        Fault-isolated — returns None on any error.
        """
        try:
            from backend.core.ouroboros.governance.adaptive_learning import (
                ProviderPerformanceTracker,
            )
            candidates = []
            if self._primary is not None:
                candidates.append(getattr(self._primary, "provider_name", "primary"))
            if self._fallback is not None:
                candidates.append(getattr(self._fallback, "provider_name", "fallback"))
            if len(candidates) < 2:
                return None
            tracker = ProviderPerformanceTracker()
            return tracker.recommend_provider(complexity, candidates)
        except Exception:
            return None

    async def _try_primary_then_fallback(
        self,
        context: OperationContext,
        deadline: datetime,
        *,
        model_id: str = "",
    ) -> GenerationResult:
        """Try primary, fall back on any failure.

        Slice 30 — Explicit Parameter Threading & Transport Determinism
        ─────────────────────────────────────────────────────────────────
        ``model_id`` is now an explicit keyword-only parameter threaded
        from the Slice 23 sentinel walker. Forwarded to ``_call_primary``
        so Slice 28 Phase 2's heavy-model 2.5× scalar engages
        deterministically. Legacy callers that don't have a specific
        model_id (pre-Slice-23 fallthroughs) pass nothing → empty
        string → legacy 30s cap path preserved byte-identically.


        Note: In Python 3.9, ``CancelledError`` is a ``BaseException`` (not
        ``Exception``), so we must catch it explicitly to handle
        ``asyncio.wait_for`` cancellation of the primary call.

        Move 2 v6 — Dynamic Provider Fallback. Before paying for a
        primary call we know is statistically likely to fail, consult
        the FSM. If it has classified the primary as in active backoff
        (consecutive transport failures + recovery ETA hasn't elapsed),
        route directly to fallback. The FSM bookkeeping already exists
        (``record_primary_failure`` + ``recovery_eta``); we just need to
        actually consult it at this critical dispatch site. Without
        this, repeated Claude transport failures used to keep retrying
        Claude — and the resulting cascade left ``_active_ops`` empty
        for an hour, idling the soak even after the v5 fixes.

        Cost-safe: dynamic fallback for IMMEDIATE/COMPLEX is Claude →
        DW (~30× cheaper). For STANDARD where DW is primary, fallback
        is Claude (more expensive, but the cost contract guard at
        ClaudeProvider boundary still rejects BG/SPEC).
        """
        # Move 2 v7 — Circuit Breaker pre-call gate. The Claude provider's
        # internal _call_with_backoff retry loop absorbs transport failures
        # within its 3-attempt window. The FSM only sees failures that
        # bubble through this dispatcher, missing exhaustions consumed by
        # the provider's internal retries (e.g. PLAN-phase calls). The
        # cross-cutting breaker sits at the provider boundary and trips
        # on consecutive transport-exhaustion events regardless of which
        # dispatch path triggered them. When OPEN, route directly to
        # fallback. The breaker only gates calls when the primary is the
        # Claude/Anthropic tier — it does not block DW/Tier-0 traffic.
        try:
            _is_claude_primary = (
                self._tier0 is None or self._primary is not self._tier0
            )
        except Exception:  # noqa: BLE001 — defensive
            _is_claude_primary = True
        if _is_claude_primary:
            try:
                from backend.core.ouroboros.governance.claude_circuit_breaker import (
                    get_claude_circuit_breaker,
                    is_enabled as _breaker_enabled,
                    CircuitState,
                )
                if _breaker_enabled():
                    _breaker = get_claude_circuit_breaker()
                    if not _breaker.should_allow_request():
                        _snap = _breaker.snapshot()
                        logger.warning(
                            "[CandidateGenerator] Circuit breaker OPEN — "
                            "routing %s op to fallback (state=%s, "
                            "consecutive_transport_failures=%d, "
                            "total_trips=%d)",
                            getattr(context, "provider_route", "?"),
                            _snap["state"],
                            _snap["consecutive_transport_failures"],
                            _snap["total_trips"],
                        )
                        return await self._call_fallback(context, deadline)
            except Exception as _exc:  # noqa: BLE001
                # Breaker failure must never block dispatch — treat as
                # CLOSED and fall through to normal flow.
                logger.debug(
                    "[CandidateGenerator] Circuit breaker check failed "
                    "(treating as CLOSED): %s", _exc,
                )

        # Dynamic fallback: skip a primary in active backoff.
        if not self.fsm.should_attempt_primary():
            _eta_s = max(0.0, self.fsm.recovery_eta() - time.monotonic())
            _mode_name = (
                self.fsm._failure_mode.name
                if self.fsm._failure_mode is not None else "UNKNOWN"
            )
            # Sovereign Autarky Backoff-Wait (2026-06-20): in DW-only mode there
            # is no fallback — routing to it is a guaranteed fallback_skipped
            # failure. If the sole provider's transient backoff clears within our
            # remaining budget, WAIT it out and re-attempt the primary instead of
            # failing the op. Bounded to ONE wait-and-retry per dispatch: if the
            # re-attempt also fails, control falls to the existing degrade path
            # (record_primary_failure → _call_fallback → clean fallback_skipped),
            # so a genuinely-dead provider self-limits and never loops.
            _autarky_wait = autarky_should_wait_and_retry(
                has_fallback=self._fallback is not None,
                enabled=autarky_backoff_wait_enabled(),
                eta_s=_eta_s,
                remaining_s=self._remaining_seconds(deadline),
                max_wait_s=_autarky_backoff_max_wait_s(),
                margin_s=_autarky_retry_margin_s(),
            )
            if _autarky_wait is not None:
                logger.info(
                    "[CandidateGenerator] Sovereign autarky backoff-wait — sole "
                    "provider %s in %s backoff (consecutive_failures=%d, "
                    "eta=+%.0fs); waiting %.0fs then RE-ATTEMPTING primary "
                    "(remaining_s=%.0f, route=%s) — no absent-fallback failure",
                    self.fsm.primary_name if hasattr(self.fsm, "primary_name")
                    else "primary",
                    _mode_name, self.fsm._consecutive_failures, _eta_s,
                    _autarky_wait, self._remaining_seconds(deadline),
                    getattr(context, "provider_route", "?"),
                )
                try:
                    await asyncio.sleep(_autarky_wait)
                except asyncio.CancelledError:
                    raise
                # Fall through to the primary attempt below (do NOT route to the
                # absent fallback). The backoff window has now elapsed, so
                # should_attempt_primary() is True on the next FSM read.
            else:
                logger.warning(
                    "[CandidateGenerator] Dynamic fallback engaged — primary "
                    "in %s backoff (consecutive_failures=%d, "
                    "recovery_eta=+%.0fs) — routing %s op to fallback "
                    "without re-attempting primary",
                    _mode_name,
                    self.fsm._consecutive_failures,
                    _eta_s,
                    getattr(context, "provider_route", "?"),
                )
                return await self._call_fallback(context, deadline)

        try:
            # Slice 30 — explicit model_id propagation (no ContextVar magic)
            result = await self._call_primary(
                context, deadline, model_id=model_id,
            )
            # Primary succeeded — record recovery if we were in a failure state
            if self.fsm._consecutive_failures > 0:
                self.fsm.record_primary_success()
            return result
        except (Exception, asyncio.CancelledError) as exc:
            mode = FailbackStateMachine.classify_exception(exc)
            # Phase 3.1 observability: surface local-tier degradations (memory /
            # latency) distinctly in operator logs. Pure telemetry -- the FSM
            # transition above is authoritative; this never changes control flow.
            try:
                _lv = classify_local_failure(exc)
                if _lv.degrade:
                    logger.info(
                        "[LocalTier] degrade class=%s -> %s (cascading upstream)",
                        getattr(exc, "failure_class", "unknown"),
                        _lv.target_state,
                    )
            except Exception:
                pass
            logger.warning(
                "[CandidateGenerator] Primary failed (mode=%s, %s: %s), "
                "falling back",
                mode.name, type(exc).__name__, exc,
            )
            if mode is FailureMode.CONTENT_FAILURE:
                # Content failure: model produced bad output, but primary infra is healthy.
                # Do NOT penalise the FSM — only count for observability.
                self.fsm.content_failure_count += 1
                logger.info(
                    "[CandidateGenerator] Content failure (count=%d), FSM unchanged",
                    self.fsm.content_failure_count,
                )
            else:
                self.fsm.record_primary_failure(mode=mode)
            return await self._call_fallback(context, deadline)

    async def _slice28_phase3_classify_ttft_failure(
        self,
        *,
        attempted_model_id: str,
        op_id: str,
        elapsed_s: float,
    ) -> None:
        """Slice 28 Phase 3 — Inline Fault Discriminator.

        Fires after a TimeoutError in ``_call_primary`` to classify
        the failure as either:

          * ``context_lag`` — endpoint is alive (probe returns fast);
            THIS prompt+model combo was just too slow. Sentinel walker
            will rotate to the next ranked model and may succeed there.
          * ``infrastructure_outage`` — endpoint is unresponsive (probe
            also times out). Sentinel rotation is unlikely to help
            because every model shares the same upstream tier.

        Pure-observability hook — NEVER raises into the caller, NEVER
        changes return values. The sentinel walker handles rotation
        structurally on the original raise (which still propagates
        normally after this returns). The classification just
        documents WHY the rotation is happening for postmortem
        attribution.

        Probe uses the Slice 27 Phase 2 Aegis-stabilized prompt_only
        lane with a 2-token cap + 5s wall budget. Fires only when
        ``JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED=true``.
        """
        probe_timeout = _envf_or_default(
            "JARVIS_TTFT_PROBE_TIMEOUT_S", _TTFT_PROBE_TIMEOUT_S_DEFAULT,
        )
        # Resolve the prompt_only-capable primary surface. Not every
        # primary has prompt_only (e.g., Claude); skip cleanly if absent.
        prompt_only_fn = getattr(self._primary, "prompt_only", None)
        if prompt_only_fn is None:
            logger.info(
                "[Slice28.Phase3] op=%s elapsed=%.1fs model=%s — "
                "primary has no prompt_only lane; classification skipped",
                op_id[:16], elapsed_s, attempted_model_id,
            )
            return

        probe_start = time.monotonic()
        probe_ok = False
        probe_err = ""
        try:
            result_text = await asyncio.wait_for(
                prompt_only_fn(
                    _TTFT_PROBE_PROMPT,
                    model=attempted_model_id or None,
                    caller_id="ttft_fault_discriminator",
                    max_tokens=_TTFT_PROBE_MAX_TOKENS,
                ),
                timeout=probe_timeout,
            )
            probe_ok = bool(result_text and result_text.strip())
        except asyncio.TimeoutError:
            probe_err = f"probe_timeout_{probe_timeout}s"
        except Exception as exc:  # noqa: BLE001 — probe MUST NOT raise
            probe_err = f"probe_exception:{type(exc).__name__}"

        probe_elapsed = time.monotonic() - probe_start
        # Classification:
        #   probe_ok with fast latency → endpoint alive → context_lag
        #   probe failed → endpoint unresponsive → infrastructure_outage
        classification = (
            "context_lag" if probe_ok
            else "infrastructure_outage"
        )
        logger.warning(
            "[Slice28.Phase3] op=%s model=%s primary_elapsed=%.1fs "
            "probe_elapsed=%.2fs probe_ok=%s probe_err=%s "
            "classification=%s — sentinel walker will rotate to next "
            "ranked model (structural rotation already engaged by raise)",
            op_id[:16], attempted_model_id, elapsed_s,
            probe_elapsed, probe_ok, probe_err or "(none)",
            classification,
        )

    async def _call_primary(
        self,
        context: OperationContext,
        deadline: datetime,
        *,
        model_id: str = "",
    ) -> GenerationResult:
        """Call primary provider with concurrency and budget-capped deadline.

        The primary gets at most ``_PRIMARY_BUDGET_FRACTION`` of the
        remaining time, guaranteeing ``_FALLBACK_MIN_RESERVE_S`` for the
        fallback provider if the primary hangs until timeout.

        Slice 30 — Explicit Parameter Threading & Transport Determinism
        ─────────────────────────────────────────────────────────────────
        ``model_id`` is now an explicit keyword-only parameter threaded
        from the Slice 23 sentinel walker → ``_try_primary_then_fallback``
        → here. This eliminates the v23 wiring gap where Slice 28's
        ContextVar-based model_id resolution silently returned empty
        across async/semaphore task boundaries, causing the heavy-model
        2.5× scalar to never engage in production (12 EXHAUSTION events
        across v20/v21/v23 all firing at the static 30s
        _PRIMARY_MAX_TIMEOUT_S cap instead of the adaptive 75s budget).

        Legacy callers that don't have a specific model_id (pre-Slice-23
        dispatch paths, IMMEDIATE route fallthrough, etc.) pass nothing
        → empty string → _compute_primary_budget skips the heavy scalar
        → legacy 30s cap behavior preserved byte-identically.
        """
        _primary_sem_t0 = time.monotonic()
        _primary_phase_hint = getattr(getattr(context, "phase", None), "name", "?")
        logger.info(
            "[CandidateGenerator] Primary sem acquire: slots_free=%d "
            "route=%s phase=%s op=%s model_id=%s",
            self._primary_sem._value,
            getattr(context, "provider_route", "?"),
            _primary_phase_hint,
            getattr(context, "op_id", "?")[:16],
            model_id or "(unspecified)",
        )
        async with self._primary_sem:
            _primary_sem_wait_s = time.monotonic() - _primary_sem_t0
            remaining = self._remaining_seconds(deadline)
            # Slice 30 — explicit model_id parameter (no ContextVar magic).
            # Slice 28 Phase 2's heavy-model 2.5× scalar now engages
            # deterministically when the sentinel walker passes a heavy
            # model_id. Empty model_id from legacy callers → legacy 30s
            # cap path (byte-identical to pre-Slice-28).
            # Slice 43 — if this op will force-batch (Slice 36/41), compute a
            # batch-appropriate budget so the outer wait_for doesn't sever the
            # async batch poll at the 30s RT reflex cap. The force-batch
            # decision is owned by the provider; we consult the same pure
            # predicate. NEVER raises → legacy budget on any failure.
            try:
                from backend.core.ouroboros.governance.doubleword_provider import (
                    _slice36_should_force_batch,
                )
                _force_batch = _slice36_should_force_batch(context)
            except Exception:  # noqa: BLE001 — defensive, legacy budget
                _force_batch = False
            # Slice 225 Phase 2 — Sovereign DW Autarky. Read the Claude fallback
            # breaker (read-only, no probe side effect — same _claude_breaker_open
            # predicate the Slice 127 P2.1 IMMEDIATE reroute uses). When the
            # fallback lane is OPEN/HALF_OPEN (incl. terminal_quota / out-of-
            # credits), there's no live lane to sever DW into — give DW the full
            # runway instead of the 30s/75s reflex cap. Gated default-TRUE;
            # OFF (or breaker CLOSED) is the byte-identical legacy cascade.
            _fallback_dead = False
            _autarky_reason = ""  # "structural" (expected) | "breaker_open" (abnormal)
            if _dw_autarky_enabled():
                # A CONFIG-disabled Claude (JARVIS_PROVIDER_CLAUDE_DISABLED) is the
                # deadest fallback of all — never constructed — yet it leaves the
                # circuit breaker CLOSED, so the breaker-state check below misses it.
                # Check it first so the sole-lane DW gets the full runway instead of
                # the reflex cap (the live-soak TIMEOUT root, 2026-06-20).
                _fallback_dead = _claude_config_disabled()
                if _fallback_dead:
                    _autarky_reason = "structural"
                else:
                    try:
                        from backend.core.ouroboros.governance.doubleword_provider import (
                            _claude_breaker_open as _autarky_breaker_open,
                        )
                        _fallback_dead = _autarky_breaker_open()
                        if _fallback_dead:
                            _autarky_reason = "breaker_open"
                    except Exception:  # noqa: BLE001 — fail-closed to legacy cascade
                        _fallback_dead = False
            primary_budget = self._compute_primary_budget(
                remaining, model_id=model_id, force_batch=_force_batch,
                fallback_dead=_fallback_dead,
            )
            if _fallback_dead and primary_budget > _PRIMARY_MAX_TIMEOUT_S:
                # Severity matches reality: STRUCTURAL autarky (operator-attested
                # JARVIS_PROVIDER_CLAUDE_DISABLED) is the intended steady state →
                # INFO (observable, not alarming, no warning-count inflation). An
                # actual breaker OPEN (economic/transport failures) is a real
                # fallback-lane degradation → WARNING. The message now states the
                # ACCURATE reason instead of always claiming "breaker OPEN".
                _structural = _autarky_reason == "structural"
                _emit = logger.info if _structural else logger.warning
                _emit(
                    "[CandidateGenerator] ⚡ DW AUTARKY ENGAGED: Claude fallback "
                    "%s — granting DW the full %.1fs budget (vs %.1fs reflex cap), "
                    "no dead-lane handoff. route=%s op=%s model=%s",
                    "structurally disabled (autarky)" if _structural
                    else "breaker OPEN (degraded)",
                    primary_budget, _PRIMARY_MAX_TIMEOUT_S,
                    getattr(context, "provider_route", "?"),
                    getattr(context, "op_id", "?")[:16],
                    model_id or "(unspecified)",
                )
            # Slice 34 Phase 2 — dispatch profiler (default OFF; zero
            # overhead when disabled). Records the sem-wait + budget
            # stages into the per-op summary; STAGE_PROVIDER_GENERATE
            # below brackets the actual provider call so we can see
            # how much time is spent IN the orchestrator overhead vs
            # the provider's own dispatch path.
            from backend.core.ouroboros.telemetry import (
                dispatch_profiler as _dp_mod,
            )
            _dp_op_id = getattr(context, "op_id", "?") or "?"
            _dp_route = getattr(context, "provider_route", "?") or "?"
            _dp_model = model_id or "(unspecified)"
            if _dp_mod.is_enabled():
                _dp_key = _dp_mod._active_key(_dp_op_id, _dp_model)
                _dp_summary = _dp_mod.OpDispatchSummary(
                    op_id=_dp_op_id, model_id=_dp_model,
                    route=_dp_route, started_unix=time.time(),
                )
                with _dp_mod._active_ops_lock:
                    _dp_mod._active_ops[_dp_key] = _dp_summary
                # Record the already-measured sem-wait as a stage.
                _dp_summary.stages.append(_dp_mod.StageRecord(
                    stage_name="STAGE_SEM_WAIT",
                    duration_ms=_primary_sem_wait_s * 1000.0,
                ))
                # And a synthetic ~0 stage for the trivial budget
                # computation (kept for shape consistency in the
                # per-op summary — actual sub-ms math is recorded
                # below via the dispatch_stage wrap if anyone ever
                # makes _compute_primary_budget heavy).
                _dp_summary.stages.append(_dp_mod.StageRecord(
                    stage_name="STAGE_BUDGET_COMPUTATION",
                    duration_ms=0.0,
                ))
            # Tier 3 Reflex observability (Manifesto §5): log at INFO when
            # the hard cap is the binding constraint (not the fraction or
            # the fallback-reserve). Operators can grep for
            # "Tier3_cap_active" to see sessions where the aggressive
            # circuit breaker is forcing fast fallback cascades.
            _tier3_cap_active = (
                primary_budget >= _PRIMARY_MAX_TIMEOUT_S - 0.01
                and remaining > _PRIMARY_MAX_TIMEOUT_S + _FALLBACK_MIN_RESERVE_S
            )
            if _tier3_cap_active:
                logger.info(
                    "[CandidateGenerator] Tier3_cap_active: primary_budget=%.1fs "
                    "(hard cap _PRIMARY_MAX_TIMEOUT_S=%.1fs), remaining=%.1fs "
                    "fallback_reserve=%.1fs route=%s phase=%s op=%s — "
                    "primary will sever at budget expiry for Manifesto §5 cascade",
                    primary_budget, _PRIMARY_MAX_TIMEOUT_S, remaining,
                    remaining - primary_budget,
                    getattr(context, "provider_route", "?"),
                    _primary_phase_hint,
                    getattr(context, "op_id", "?")[:16],
                )
            else:
                logger.debug(
                    "[CandidateGenerator] Primary budget: %.1fs of %.1fs remaining "
                    "(fallback reserve: %.1fs)",
                    primary_budget, remaining, remaining - primary_budget,
                )
            try:
                # W3(7) Slice 2 — race against ambient cancel token (if any).
                # `current_cancel_token()` reads the ContextVar set by
                # `dispatch_pipeline`; None outside dispatcher (unit tests,
                # pre-W3(7) callers) → falls through to plain wait_for.
                from backend.core.ouroboros.governance.cancel_token import (
                    current_cancel_token as _curr_cancel_token,
                    race_or_wait_for as _race_or_wait_for,
                )
                # Slice 34 Phase 2 — Stage 3: STAGE_PROVIDER_GENERATE
                # brackets the entire provider call so we can see how
                # much wall-time is spent in the provider's own dispatch
                # path (Aegis auth + lease + HTTP POST + response parse).
                # Profiler is fail-closed default-OFF — zero overhead
                # when JARVIS_DISPATCH_PROFILER_ENABLED is unset.
                from backend.core.ouroboros.telemetry.dispatch_profiler import (
                    dispatch_stage as _dp_stage,
                )
                _dp_provider_outcome = "ok"
                _dp_provider_err = ""
                _dp_provider_t0 = time.monotonic()
                try:
                    _pri_result = await _race_or_wait_for(
                        self._primary.generate(context, deadline),
                        timeout=primary_budget,
                        cancel_token=_curr_cancel_token(),
                    )
                except asyncio.CancelledError:
                    _dp_provider_outcome = "cancelled"
                    raise
                except Exception as _dp_exc:  # noqa: BLE001
                    _dp_provider_outcome = "error"
                    _dp_provider_err = type(_dp_exc).__name__
                    raise
                finally:
                    # Slice 34 Phase 2 — record STAGE_PROVIDER_GENERATE
                    # + emit the per-op summary. Fail-closed if profiler
                    # is disabled or accumulator was never created.
                    try:
                        _dp_provider_ms = (
                            time.monotonic() - _dp_provider_t0
                        ) * 1000.0
                        if _dp_mod.is_enabled():
                            _dp_key2 = _dp_mod._active_key(_dp_op_id, _dp_model)
                            with _dp_mod._active_ops_lock:
                                _dp_summary2 = _dp_mod._active_ops.pop(
                                    _dp_key2, None,
                                )
                            if _dp_summary2 is not None:
                                _dp_summary2.stages.append(
                                    _dp_mod.StageRecord(
                                        stage_name="STAGE_PROVIDER_GENERATE",
                                        duration_ms=_dp_provider_ms,
                                        outcome=_dp_provider_outcome,
                                        error_class=_dp_provider_err,
                                    )
                                )
                                _dp_summary2.total_duration_ms = sum(
                                    s.duration_ms for s in _dp_summary2.stages
                                )
                                _dp_summary2.outcome = _dp_provider_outcome
                                _dp_summary2.error_class = _dp_provider_err
                                with _dp_mod._recent_summaries_lock:
                                    _dp_mod._recent_summaries.append(_dp_summary2)
                                logger.info(
                                    "[DispatchProfiler] op_summary %s",
                                    _dp_summary2.to_log_kv(),
                                )
                    except Exception:  # noqa: BLE001 — never raise from profiler
                        pass
                logger.info(
                    "[CandidateGenerator] Primary sem release: "
                    "hold=%.1fs sem_wait=%.1fs route=%s phase=%s op=%s outcome=ok",
                    time.monotonic() - _primary_sem_t0, _primary_sem_wait_s,
                    getattr(context, "provider_route", "?"),
                    _primary_phase_hint,
                    getattr(context, "op_id", "?")[:16],
                )
                return _pri_result
            except (Exception, asyncio.CancelledError) as _exc:
                logger.info(
                    "[CandidateGenerator] Primary sem release: "
                    "hold=%.1fs sem_wait=%.1fs route=%s phase=%s op=%s outcome=fail",
                    time.monotonic() - _primary_sem_t0, _primary_sem_wait_s,
                    getattr(context, "provider_route", "?"),
                    _primary_phase_hint,
                    getattr(context, "op_id", "?")[:16],
                )
                logger.info(
                    "[CancelAttribution] %s",
                    _attribute_cancel(
                        _exc,
                        label="_call_primary",
                        op_id=getattr(context, "op_id", "?"),
                        elapsed_s=time.monotonic() - _primary_sem_t0,
                        remaining_s=self._remaining_seconds(deadline),
                    ),
                )
                # Slice 28 Phase 3 — Inline Fault Discriminator
                # ────────────────────────────────────────────────
                # On TimeoutError specifically, fire a lightweight
                # 2-token probe via the primary's prompt_only lane
                # (now Aegis-stabilized via Slice 27 Phase 2) to
                # discriminate between:
                #   * context_lag — endpoint alive, THIS prompt+model
                #     combination is just slow (probe completes fast)
                #   * infrastructure_outage — endpoint not responding
                #     (probe also times out)
                # The sentinel walker ALREADY rotates to the next
                # model in ranked_models on any raise from
                # _call_primary, so Phase 3 doesn't need to add
                # rotation — it adds the CLASSIFICATION SIGNAL so
                # postmortem analysis can attribute the rotation
                # reason structurally. Probe is bounded to 5s; on
                # outage, the cost is small and the diagnostic is
                # invaluable. Env-gated default-off; graduate after
                # v22 proves the probe yields actionable signal.
                if (
                    isinstance(_exc, asyncio.TimeoutError)
                    and _envb("JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED", False)
                ):
                    # Slice 30 — use explicit model_id param (was ContextVar)
                    await self._slice28_phase3_classify_ttft_failure(
                        attempted_model_id=model_id,
                        op_id=getattr(context, "op_id", "?"),
                        elapsed_s=time.monotonic() - _primary_sem_t0,
                    )
                raise

    # Hard ceiling for fallback provider — fail fast when unreachable
    # rather than burning the entire pipeline budget (Manifesto §6: Iron Gate).
    # Raised from 60s to 120s after bt-2026-04-11-085020 diagnosed tool_round
    # full_content patches legitimately needing 60-90s of stream time. IMMEDIATE
    # route also funnels through this cap, and a 60s cap was cutting mid-stream
    # healthy generation (23KB received at 365 bytes/s — normal Claude rate).
    _FALLBACK_MAX_TIMEOUT_S: float = float(
        os.environ.get("JARVIS_FALLBACK_MAX_TIMEOUT_S", "120.0")
    )

    # Route-aware ceiling for complex-route generate. Session
    # bt-2026-04-15-065523 (Session F, 2026-04-14) diagnosed a
    # complex-route retry synthesis hitting the 120s cap by exactly 2
    # seconds: elapsed=122.1s, fallback_err_class=CancelledError,
    # all_providers_exhausted with 131s of nominal generation budget
    # still remaining. Complex ops under ledger enforcement legitimately
    # need wider synthesis windows because their tool-result prompts
    # exceed 40KB (44104 chars observed in Session F attempt 2) and
    # Claude needs 150-180s to produce a coherent multi-file patch.
    # 120s remains the default for all other routes; complex gets 180s.
    # Env-tunable so ops can tune without a code change — the default
    # 180.0 is the post-Session-F calibration.
    _FALLBACK_MAX_TIMEOUT_COMPLEX_S: float = float(
        os.environ.get("JARVIS_FALLBACK_MAX_TIMEOUT_COMPLEX_S", "180.0")
    )

    # ── Synthesis reserve for read-only BG subagent fan-out (Session 5) ──
    # Session 5 (bt-2026-04-18-035817) proved the graduation signal: three
    # parallel subagents dispatched, 80 findings returned, all with Iron
    # Gate diversity=3. But the parent Claude synthesis round died with
    # TimeoutError because the BG fallback cap (120s) was sized for a
    # single-shot Claude completion, not "Claude fans out → 3 subagents
    # consume 135s → Claude synthesizes the findings". Per Derek's
    # 2026-04-17 directive, the cap for read-only BG ops must dynamically
    # expand to account for subagent wall-clock PLUS a hard synthesis
    # reserve. The formula is:
    #
    #     _max_cap = base_cap  (=_FALLBACK_MAX_TIMEOUT_S, 120s default)
    #              + MAX_PARALLEL_SCOPES * PRIMARY_PROVIDER_TIMEOUT_S
    #              + _BG_READONLY_SYNTHESIS_RESERVE_S
    #
    # With Phase 1 constants (MAX_PARALLEL_SCOPES=3,
    # PRIMARY_PROVIDER_TIMEOUT_S=90) and the mandated 90s synthesis
    # reserve, this evaluates to 480s — about 4× the mutating-BG cap,
    # but strictly bounded by what the actual wall-clock needs for a
    # 3-subagent cartography op. Env-tunable so operators can retune
    # after graduation data accumulates.
    # Default sized from Session-12 empirical data (bt-2026-04-18-055042).
    # Session 11 synthesized 80 findings in 472s (8s under 480s cap).
    # Session 12 with 108 findings took 491.93s — 11.93s over. Subagent
    # finding counts are model-driven (exploration depth varies per
    # provider + cache state), so a fixed 90s reserve was too tight for
    # the high-yield end of the distribution. 180s absorbs another ~40%
    # finding-count drift before the cap bites.
    _BG_READONLY_SYNTHESIS_RESERVE_S: float = float(
        os.environ.get("JARVIS_BG_READONLY_SYNTHESIS_RESERVE_S", "180.0")
    )

    def _fallback_is_claude(self) -> bool:
        """Slice 238 — True iff the configured fallback is the Claude lane (so the
        Claude economic breaker is the right health signal to gate it). Reads the
        fallback's ``provider_name`` (e.g. ``claude-api``); a non-Claude fallback
        (e.g. Prime) returns False so the Claude breaker never suppresses it.
        NEVER raises → fail-soft to False (legacy: don't suppress)."""
        try:
            if self._fallback is None:
                return False
            name = (getattr(self._fallback, "provider_name", "") or "").strip().lower()
            return "claude" in name
        except Exception:  # noqa: BLE001
            return False

    async def _call_fallback(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Call fallback provider with concurrency and deadline enforcement.

        Budget computation happens AFTER acquiring ``_fallback_sem`` so that
        time spent queued behind other ops doesn't silently zero out
        ``_parent_remaining``.  The post-acquire refresh guarantees at least
        ``_FALLBACK_MIN_GUARANTEED_S`` regardless of how long the wait was.

        The orchestrator's outer ``wait_for(_gen_timeout + _OUTER_GATE_GRACE_S)``
        is still the absolute Iron Gate — grace raised from 5s to 15s after
        bt-2026-04-12-061609 diagnosed 129s Claude streams cut by 125s gate.
        """
        # ──────────────────────────────────────────────────────────────
        # Slice 19b (2026-05-26) — fallback=None semantic correction
        #
        # Pre-Slice-19b: when self._fallback is None (e.g., Slice 19a
        # JARVIS_PROVIDER_CLAUDE_DISABLED=true), _call_fallback fell
        # through to the semaphore acquire + provider call. The
        # ``self._fallback.generate(...)`` would raise AttributeError,
        # the exception handler at line ~4731 classified it as
        # ``fallback_failed`` cause, and ExhaustionWatcher incremented
        # the consecutive counter. 3 consecutive ops with no-fallback
        # cascade → hibernation, even though DW (primary) was healthy.
        #
        # bt-2026-05-26-180129 (PURE-DW v14 soak) proved this:
        # DW completed a 265s, 23-tool-call, 76K-token Venom loop on
        # the SWE-Bench Ansible op and returned 0 candidates (model
        # judgment). The orchestrator wanted to retry via fallback,
        # fallback was None (Slice 19a intentional), instant
        # "fallback_failed", 3 consecutive → hibernation cycle 1.
        #
        # Fix: emit a DISTINCT cause prefix ``fallback_skipped:`` for
        # the "no fallback configured" case (vs ``fallback_failed:``
        # for genuine fallback failures). ExhaustionWatcher will
        # filter ``fallback_skipped:`` out of the consecutive count
        # (separate edit in provider_exhaustion_watcher.py). Hibernation
        # stays reserved for genuine provider distress, not for the
        # operator-attested DW-only mode.
        # ──────────────────────────────────────────────────────────────
        if self._fallback is None:
            logger.info(
                "[CandidateGenerator] Slice 19b: fallback=None "
                "(provider intentionally absent, e.g., Slice 19a "
                "JARVIS_PROVIDER_CLAUDE_DISABLED) — raising "
                "fallback_skipped sentinel (NOT counted toward "
                "ExhaustionWatcher hibernation threshold)"
            )
            self._raise_exhausted(
                "fallback_skipped:no_fallback_configured",
                context=context,
                deadline=deadline,
                fallback_state="absent_by_configuration",
            )

        # Isolation override: if the op's route is listed in
        # JARVIS_DISABLE_CLAUDE_FALLBACK_ROUTES, skip the fallback entirely
        # and raise through the existing exhaustion path. Used by the Qwen
        # 397B benchmark to collect raw DW telemetry without Claude masking.
        _op_route = (getattr(context, "provider_route", "") or "").strip().lower()
        if _fallback_disabled_for_route(_op_route):
            logger.info(
                "[CandidateGenerator] Fallback disabled by env for route=%s "
                "(%s) — raising fallback_disabled_by_env sentinel",
                _op_route, _DISABLE_FALLBACK_ROUTES_ENV,
            )
            self._raise_exhausted(
                f"fallback_disabled_by_env:{_op_route}",
                context=context,
                deadline=deadline,
                disabled_routes=os.environ.get(_DISABLE_FALLBACK_ROUTES_ENV, ""),
            )

        # Slice 238 — cascade breaker consult (CENTRAL seam). The s237 soak proved
        # the cascade-to-dead-Claude poison (BadRequestError 400 → terminal_quota →
        # cooldown cycle) reaches Claude from EVERY _call_fallback caller, not just
        # the sentinel cascade_to_claude path. Guard it here, where all callers
        # converge: when the fallback IS the Claude lane AND the economic breaker
        # is OPEN (read-only _claude_breaker_open — same source-of-truth the
        # primary lane respects, no probe side-effect), do NOT call the known-dead
        # lane. Raise the EXISTING fallback_skipped sentinel (Slice 19b — NOT
        # counted toward ExhaustionWatcher hibernation) so the op degrades cleanly
        # instead of burning a 400 and poisoning the consecutive-failure counter.
        # Breaker CLOSED → byte-identical (a funded Claude fallback is used).
        if cascade_breaker_consult_enabled() and self._fallback_is_claude():
            _claude_lane_open = False
            try:
                from backend.core.ouroboros.governance.doubleword_provider import (
                    _claude_breaker_open as _cf_breaker_open,
                )
                _claude_lane_open = _cf_breaker_open()
            except Exception:  # noqa: BLE001 — advisory; never block dispatch
                _claude_lane_open = False
            if _claude_lane_open:
                logger.warning(
                    "[CandidateGenerator] Slice238 fallback SUPPRESSED (central): "
                    "Claude lane breaker OPEN (economic/transport) — skipping the "
                    "known-dead Claude fallback (no terminal_quota poison); "
                    "raising fallback_skipped so the op degrades cleanly "
                    "(route=%s)", _op_route or "?",
                )
                self._raise_exhausted(
                    "fallback_skipped:claude_breaker_open",
                    context=context,
                    deadline=deadline,
                    fallback_state="economic_open",
                )

        _pre_sem_remaining = self._remaining_seconds(deadline)
        _sem_t0 = time.monotonic()
        _phase_hint = getattr(getattr(context, "phase", None), "name", "?")

        # Defect #4 Slice B (2026-05-03) — pre-fallback budget short-
        # circuit. Soak v5 saw 3 EXHAUSTION events with remaining_s=0.0
        # and fallback_err_class=CancelledError -- ops were entering
        # _call_fallback with insufficient budget, the call attempt
        # was cancelled mid-flight, and the resulting CancelledError
        # was relabeled as "fallback_failed". Wasted CPU + provider
        # call attempt + log noise + the unhandled-exception cascade.
        #
        # Fix: detect the "deadline already exhausted" pre-condition
        # and raise a clean cause sentinel instead of attempting an
        # invariably-doomed call. Env-tunable floor protects against
        # over-aggressive shedding (e.g., complex routes with legit
        # 4-5s remaining might still complete in fast paths).
        try:
            raw_min_viable = os.environ.get(
                "JARVIS_FALLBACK_MIN_VIABLE_BUDGET_S", "",
            ).strip()
            min_viable_s = max(
                1.0, min(60.0, float(raw_min_viable) if raw_min_viable else 5.0),
            )
        except (TypeError, ValueError):
            min_viable_s = 5.0
        if _pre_sem_remaining <= min_viable_s:
            logger.info(
                "[CandidateGenerator] Pre-fallback short-circuit: "
                "remaining=%.2fs <= min_viable=%.2fs route=%s "
                "(Defect #4 Slice B fix avoids attempting a doomed "
                "fallback call that would CancelledError mid-flight)",
                _pre_sem_remaining, min_viable_s, _op_route,
            )
            self._raise_exhausted(
                "deadline_exhausted_pre_fallback",
                context=context,
                deadline=deadline,
                pre_sem_remaining_s=round(_pre_sem_remaining, 2),
                min_viable_s=round(min_viable_s, 2),
                phase=_phase_hint,
                route=_op_route,
            )

        # Route-aware fallback ceiling: complex routes get a wider
        # synthesis window (180s) because their tool-result prompts are
        # legitimately larger and their multi-file patches take longer
        # to generate coherently. Non-complex routes keep the 120s cap.
        # _op_route was already computed earlier in this method for the
        # fallback-disabled-by-env check, so we reuse the lowercased value.
        #
        # Read-only BG subagent fan-out override (Session 5 graduation
        # directive, Derek 2026-04-17): when the op is read-only AND on
        # BG route, dynamically extend the cap to account for parallel
        # subagent wall-clock plus a 90s synthesis reserve. The three
        # parallel ExploreAgents consume up to
        # MAX_PARALLEL_SCOPES * PRIMARY_PROVIDER_TIMEOUT_S seconds of
        # wall-clock before the parent Claude begins synthesizing the
        # rolled-up findings — charging the parent's 120s cap for that
        # wait is the arithmetic that killed Session 5 at 134.56s.
        _is_read_only = bool(getattr(context, "is_read_only", False))
        if _is_read_only and _op_route == "background":
            # Lazy import to avoid a new top-level dependency on
            # subagent_contracts — this module is imported eagerly
            # at provider boot, subagent_contracts is imported by
            # the orchestrator later.
            try:
                from backend.core.ouroboros.governance.subagent_contracts import (
                    MAX_PARALLEL_SCOPES,
                    PRIMARY_PROVIDER_TIMEOUT_S,
                )
                _subagent_wallclock_budget_s = (
                    MAX_PARALLEL_SCOPES * PRIMARY_PROVIDER_TIMEOUT_S
                )
            except Exception:
                # Defensive fallback — hardcode the current Phase 1
                # constants so the cap still extends meaningfully if
                # the import fails for any reason.
                _subagent_wallclock_budget_s = 3 * 90  # = 270s
            _max_cap = (
                self._FALLBACK_MAX_TIMEOUT_S
                + _subagent_wallclock_budget_s
                + self._BG_READONLY_SYNTHESIS_RESERVE_S
            )
        elif _op_route == "complex":
            _max_cap = self._FALLBACK_MAX_TIMEOUT_COMPLEX_S
        else:
            _max_cap = self._FALLBACK_MAX_TIMEOUT_S

        # Task #88b — thinking-aware outer-budget widening (2026-05-13).
        #
        # v14-rev6 graduation soak proved: Task #88's inner rupture
        # widening (120s -> 360s for thinking-enabled calls) is correct
        # but insufficient — the OUTER asyncio.wait_for budget computed
        # from _max_cap fires FIRST and kills the Claude stream before
        # the inner rupture matters.  Log evidence:
        # ``elapsed=290.0s budget=218.7s first_token=NEVER thinking=on``.
        # Direct-host streaming probes confirmed Claude responds in
        # seconds with thinking events; the harness's outer budget was
        # the load-bearing constraint.
        #
        # Single policy with Task #88: outer >= inner for thinking-on.
        # When the op's task_complexity + route would produce a
        # thinking-enabled call (see providers.py:_resolve_thinking_*
        # rules), widen _max_cap to at least
        # JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S (default 360s, matches
        # Task #88's inner default).  Apply via ``max()`` so it never
        # SHRINKS the existing route-specific cap (e.g. COMPLEX's 180s,
        # read-only-BG's 480s+).
        #
        # The thinking-likelihood signal is conservative but correct
        # for the dominant case: any non-trivial task_complexity on a
        # non-reflex (non-IMMEDIATE) route will have thinking enabled
        # per the existing _resolve_thinking_budget rules.  We avoid
        # reaching back into providers._resolve_thinking_budget to keep
        # this module orchestration-free; the inline check matches the
        # decision rule structurally.
        # Single source of truth (Phase R1): the SAME predicate + cap
        # the OUTER Iron-Gate _gen_timeout uses, so outer >= inner by
        # construction (no duplicated rule, no 255-vs-360 drift).
        _likely_thinking = gen_call_likely_thinking(
            _op_route, getattr(context, "task_complexity", "") or "",
        )
        if _likely_thinking:
            _max_cap = max(_max_cap, fallback_thinking_cap_s())

        # Seed Arc Path 3 follow-up — PLAN-EXPLOIT per-stream override.
        # When ``plan_exploit_active_var`` is True (set by
        # ``try_parallel_generate`` before its gather), the per-stream
        # cap uses ``plan_exploit_per_stream_timeout_s()`` instead of
        # the default _FALLBACK_MAX_TIMEOUT_S. The 120s default was sized
        # for serial calls with retry rounds; applying it per-stream in
        # parallel mode artificially constrains streams doing legitimate
        # full-file generation when the parent has 220s+ remaining.
        # Outside PLAN-EXPLOIT context (the common case), behavior is
        # unchanged. The override clamps with max() against the existing
        # _max_cap so it never SHRINKS an already-larger cap (e.g. the
        # COMPLEX-route cap or the BG/SPEC subagent extension above).
        try:
            from backend.core.ouroboros.governance.plan_exploit import (
                plan_exploit_active_var as _plan_exploit_active,
                plan_exploit_per_stream_timeout_s as _plan_exploit_timeout,
            )
            if _plan_exploit_active.get(False):
                _max_cap = max(_max_cap, _plan_exploit_timeout())
        except Exception:  # noqa: BLE001 — override is best-effort
            pass

        # Promoted to INFO with phase label so traces distinguish first
        # GENERATE from GENERATE_RETRY contention on the shared fallback
        # semaphore — Session bt-2026-04-15-041413 (2026-04-14) saw a
        # retry wait 121.5s behind cohort ops with no visibility into
        # which acquisition phase was queuing. max_cap added after
        # Session F (bt-2026-04-15-065523) so the route-aware ceiling
        # that was actually applied is visible at acquire time.
        logger.info(
            "[CandidateGenerator] Fallback sem acquire: slots_free=%d/%d "
            "remaining=%.1fs route=%s phase=%s op=%s max_cap=%.0fs",
            self._fallback_sem._value, self._fallback_concurrency,
            _pre_sem_remaining,
            getattr(context, "provider_route", "?"),
            _phase_hint,
            getattr(context, "op_id", "?")[:16],
            _max_cap,
        )

        # AdmissionGate Slice 2 — pre-acquire viability check.
        # Refuses admission when projected wait + min-viable
        # call exceeds remaining budget, sheds load BEFORE
        # consuming a semaphore slot. Master flag default-FALSE
        # until Slice 3 — disabled gate degrades to ADMIT
        # (preserves pre-Slice-2 behavior). NEVER raises;
        # adopting a fail-open posture so a gate bug cannot
        # itself starve a legitimate op.
        try:
            from backend.core.ouroboros.governance.admission_gate import (  # noqa: E501
                AdmissionContext as _AdmissionContext,
                admission_gate_enabled as _admission_gate_enabled,
                compute_admission_decision as _compute_admission_decision,
            )
            _wait_est = getattr(self, "_wait_estimator", None)
            _projected_wait = (
                _wait_est.project_wait(_op_route)
                if _wait_est is not None else 0.0
            )
            # _fallback_sem._value is "slots free"; depth =
            # capacity − free.
            _live_depth = max(
                0,
                self._fallback_concurrency
                - int(getattr(self._fallback_sem, "_value", 0)),
            )
            _admission_ctx = _AdmissionContext(
                route=_op_route,
                remaining_s=_pre_sem_remaining,
                queue_depth=_live_depth,
                projected_wait_s=_projected_wait,
                op_id=str(getattr(context, "op_id", ""))[:48],
            )
            _admission = _compute_admission_decision(
                _admission_ctx,
                enabled=_admission_gate_enabled(),
                decided_at_ts=time.time(),
            )
            # Slice 3 — record EVERY decision (admit + shed) to
            # the bounded ring so the GET /observability/admission-
            # gate route shows recent admission patterns.
            # Best-effort, NEVER raises into the call path.
            try:
                from backend.core.ouroboros.governance.admission_estimator import (  # noqa: E501
                    get_default_history as _admit_history,
                )
                _admit_history().record(_admission.to_dict())
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[CandidateGenerator] admission history "
                    "record degraded: %s", exc,
                )
            if _admission.is_shed():
                logger.info(
                    "[CandidateGenerator] Pre-admission shed "
                    "decision=%s reason=%s route=%s "
                    "remaining=%.2fs projected_wait=%.2fs "
                    "queue_depth=%d required_budget=%.2fs",
                    _admission.decision.value,
                    _admission.reason, _admission.route,
                    _admission.remaining_s,
                    _admission.projected_wait_s,
                    _admission.queue_depth,
                    _admission.required_budget_s,
                )
                # Slice 3 — publish SSE event for IDE
                # consumers to surface saturation in real time.
                # Best-effort.
                try:
                    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                        EVENT_TYPE_ADMISSION_DECISION_EMITTED,
                        get_default_broker,
                    )
                    _br = get_default_broker()
                    if _br is not None:
                        _br.publish(
                            event_type=(
                                EVENT_TYPE_ADMISSION_DECISION_EMITTED
                            ),
                            op_id=str(
                                getattr(context, "op_id", "")
                                or "",
                            )[:48],
                            payload=_admission.to_dict(),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[CandidateGenerator] admission SSE "
                        "publish degraded: %s", exc,
                    )
                self._raise_exhausted(
                    "pre_admission_shed",
                    context=context,
                    deadline=deadline,
                    sem_wait_total_s=0.0,
                    pre_sem_remaining_s=round(
                        _pre_sem_remaining, 2,
                    ),
                    admission_decision=(
                        _admission.decision.value
                    ),
                    admission_reason=_admission.reason,
                    projected_wait_s=round(
                        _admission.projected_wait_s, 2,
                    ),
                    queue_depth_at_check=(
                        _admission.queue_depth
                    ),
                    required_budget_s=round(
                        _admission.required_budget_s, 2,
                    ),
                )
        except RuntimeError:
            # _raise_exhausted raises RuntimeError — don't
            # swallow our own structural shed.
            raise
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug(
                "[CandidateGenerator] Admission gate "
                "degraded — proceeding to acquire: %s", exc,
            )

        try:
            # Slice 12F-A — priority-aware fallback sem acquire.
            # urgency=high SWE-Bench-Pro foreground ops (IMMEDIATE
            # route, priority=0) now preempt urgency=low /
            # BACKGROUND OpportunityMiner ops (priority=4) on slot
            # release. Hard concurrency cap preserved by the
            # underlying PrioritySemaphore counter. Hot-revert via
            # JARVIS_PRIORITY_SEM_ENABLED=false returns to FIFO.
            from backend.core.ouroboros.governance.priority_semaphore import (  # noqa: E501
                acquire_priority_aware as _slice12f_acquire,
            )
            async with _slice12f_acquire(self._fallback_sem, _op_route):
                _sem_wait_s = time.monotonic() - _sem_t0
                _parent_remaining = self._remaining_seconds(deadline)

                # D2 (Task #95, 2026-05-14) — sem-exhausted fast-fail.
                # Per operator binding: "after the semaphore wait is
                # charged, remaining_budget_for_network = max(0,
                # outer_remaining - sem_wait_total); if ≤ 0, fail fast
                # with a structured reason (sem_exhausted_zero_budget)
                # instead of still opening a stream that will always
                # violate outer wait_for."
                #
                # This sits BEFORE the post-acquire floor refresh (#88c
                # territory) by design: #88c's refresh is the explicit
                # op-envelope extension when budget is tight but
                # *nonzero* — honest enforcement.  D2 is the new
                # invariant for the *zero* case: do not pretend time
                # exists that does not.  When the entire pre-sem budget
                # was consumed waiting for the semaphore, the outer
                # asyncio.wait_for is already arithmetically violated;
                # opening a stream now guarantees a TimeoutError /
                # CancelledError 130s later (httpx connect+read
                # surrender latency).  Fast-fail here is observability +
                # cost win.
                #
                # Slice 12F-B (2026-05-22) — raise the D2 floor from
                # absolute-zero to JARVIS_STREAM_MINIMUM_READ_BUDGET_S
                # (default 10s). Phase 3A acceptance (bt-2026-05-22-
                # 184422) proved the gap: sem_wait_total=142.2s drained
                # the op's wall to ~0.01s — JUST above the 0.0 floor —
                # so D2 didn't fire and the stream opened with a
                # 0.01-second read budget. The subsequent inter-chunk
                # watchdog fired a misleading "no event for 0s" rupture.
                # That was a budget-too-short refusal masquerading as a
                # network rupture. Slice 12F-B raises the typed
                # StreamBudgetTooShortError BEFORE dispatch, which the
                # classifier maps to TRANSIENT_TRANSPORT →
                # RetryDecision.RETRY_TRANSIENT (NOT terminal
                # structural — Slice 7 fallback handles it as a
                # transient transport fault).
                from backend.core.ouroboros.governance.stream_rupture import (  # noqa: E501
                    StreamBudgetTooShortError,
                    stream_minimum_read_budget_s,
                )
                _min_read_budget_s = stream_minimum_read_budget_s()
                if _parent_remaining < _min_read_budget_s:
                    logger.info(
                        "[CandidateGenerator] Post-sem budget-floor "
                        "shed (Slice 12F-B): sem_wait=%.1fs drained "
                        "pre_sem_remaining=%.1fs → parent_remaining=%.2fs "
                        "below floor=%.1fs (route=%s). Refusing to "
                        "dispatch a stream that the wall budget cannot "
                        "honor; raising StreamBudgetTooShortError → "
                        "RETRY_TRANSIENT (Slice 7 fallback).",
                        _sem_wait_s, _pre_sem_remaining,
                        _parent_remaining, _min_read_budget_s, _op_route,
                    )
                    raise StreamBudgetTooShortError(
                        provider="claude-api",
                        op_id=str(getattr(context, "op_id", ""))[:48],
                        wall_remaining_s=round(_parent_remaining, 2),
                        minimum_required_s=_min_read_budget_s,
                        sem_wait_s=round(_sem_wait_s, 2),
                        route=str(_op_route or ""),
                    )

                # AdmissionGate Slice 2 — feed observed wait
                # back to the EWMA estimator so the next op's
                # projection reflects actual queue pressure.
                # NEVER raises into the call path.
                try:
                    _wait_est_post = getattr(
                        self, "_wait_estimator", None,
                    )
                    if _wait_est_post is not None:
                        _wait_est_post.update_observed(
                            _op_route, _sem_wait_s,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[CandidateGenerator] Estimator "
                        "update degraded: %s", exc,
                    )

                if _sem_wait_s > 1.0:
                    logger.info(
                        "[CandidateGenerator] Fallback sem_wait=%.1fs "
                        "(pre=%.1fs → post=%.1fs)",
                        _sem_wait_s, _pre_sem_remaining, _parent_remaining,
                    )

                # Post-acquire refresh: guarantee _FALLBACK_MIN_GUARANTEED_S
                # even when the parent deadline burned during sem wait or
                # Tier 0 consumed most of the window.  The orchestrator's
                # outer wait_for is the absolute Iron Gate. ``_max_cap`` is
                # the route-aware ceiling computed at acquire time (180s
                # for complex, 120s otherwise).
                #
                # Task #88c (2026-05-13) — thinking-aware floor reservation.
                # v14-rev7 proved the third budget layer: even with Task #88
                # (inner 360s) and #88b (outer _max_cap 360s) widened, the
                # actual Claude timeout was 90s because the DW cascade had
                # already consumed ~140s of the ~200s op deadline. The post-
                # acquire refresh's floor (_FALLBACK_MIN_GUARANTEED_S=90s)
                # was the binding constraint — and 90s is nowhere near the
                # 360s thinking-on inner/outer single-policy floor.
                #
                # Fix: when the call is likely-thinking (signal reused from
                # Task #88b, computed earlier in this method), promote the
                # floor to JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S (default
                # 360s, matching the #88/#88b inner+outer caps).  Same env-
                # tunable pattern.  Single-policy invariant the spine pins:
                # thinking floor >= max(inner, outer) for thinking-on calls.
                #
                # NOTE: the floor is OVERRIDDEN by ``_max_cap`` via the
                # subsequent ``min(..., _max_cap)``, so as long as #88b's
                # _max_cap=360 lands first, the math is:
                #   _budget_target = max(60.1s remaining, 360s floor) = 360s
                #   remaining = min(360s budget_target, 360s _max_cap) = 360s
                # Claude gets a guaranteed 360s, even when parent_remaining
                # was nearly exhausted by the DW cascade.  This is the
                # "Claude-floor reservation against op deadline" the operator
                # binding mandates — DW cannot force Claude below the floor.
                _min_guaranteed_s = (
                    float(os.environ.get(
                        "JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S", "360.0",
                    ))
                    if _likely_thinking
                    else _FALLBACK_MIN_GUARANTEED_S
                )
                _budget_target = max(_parent_remaining, _min_guaranteed_s)
                remaining = min(_budget_target, _max_cap)
                _refreshed = remaining > _parent_remaining + 1.0

                if remaining < _MIN_VIABLE_FALLBACK_S:
                    self._raise_exhausted(
                        "fallback_budget_starved",
                        context=context,
                        deadline=deadline,
                        sem_wait_s=round(_sem_wait_s, 2),
                        pre_sem_remaining_s=round(_pre_sem_remaining, 2),
                        parent_remaining_s=round(_parent_remaining, 2),
                        fallback_budget_s=round(remaining, 2),
                        min_viable_fallback_s=_MIN_VIABLE_FALLBACK_S,
                    )

                if _refreshed:
                    logger.info(
                        "[CandidateGenerator] Fallback: budget=%.1fs REFRESHED "
                        "(parent=%.1fs, guaranteed_min=%.0fs, cap=%.0fs, "
                        "sem_wait=%.1fs, thinking=%s)",
                        remaining, _parent_remaining,
                        _min_guaranteed_s, _max_cap,
                        _sem_wait_s,
                        "yes" if _likely_thinking else "no",
                    )
                    deadline = datetime.now(tz=timezone.utc) + timedelta(
                        seconds=remaining,
                    )
                else:
                    logger.info(
                        "[CandidateGenerator] Fallback: budget=%.1fs "
                        "(cap=%.0fs, sem_wait=%.1fs)",
                        remaining, _max_cap, _sem_wait_s,
                    )

                # W3(7) Slice 2 — race against ambient cancel token (if any).
                # Outer-retry loop (rooted-problem fix 2026-04-25): re-invoke
                # the provider on transient failures while remaining budget
                # exceeds `_MIN_VIABLE_FALLBACK_S`. Holds `_fallback_sem`
                # across attempts so head-of-queue position is preserved
                # (paying the wait fee twice would penalize the op for
                # provider flakiness — semantically incorrect).
                from backend.core.ouroboros.governance.cancel_token import (
                    OperationCancelledError as _OperationCancelledError,
                    current_cancel_token as _curr_cancel_token,
                    race_or_wait_for as _race_or_wait_for,
                )
                # Slice 7e (Provider Circuit Breaker) — wire the
                # state machine into the retry loop. Constructed once
                # per _call_fallback invocation (per op_id). Consumes
                # Slice 7a's classify() output; emits SSE telemetry.
                # On TERMINATE_UNRESOLVED short-circuits the loop +
                # fires _raise_exhausted with the breaker's reason
                # code — closing the empirical 35-min retry storm
                # from bt-2026-05-21-214521.
                #
                # Master flag ``JARVIS_PROVIDER_CIRCUIT_BREAKER_ENABLED``
                # default-FALSE. When off, ``breaker.evaluate()`` always
                # returns RETRY_OK → byte-identical to the pre-7e retry
                # loop (FailureMode / outer-retry cap / backoff constant
                # stay authoritative).
                #
                # Lazy imports avoid governance-package cycles
                # (mirrors the cancel_token import above).
                from backend.core.ouroboros.governance.circuit_breaker import (  # noqa: E501
                    CircuitBreaker as _Slice7e_CircuitBreaker,
                    CircuitScope as _Slice7e_CircuitScope,
                    CircuitTripOrigin as _Slice12N_CircuitTripOrigin,
                    VerdictAction as _Slice7e_VerdictAction,
                )
                from backend.core.ouroboros.governance.provider_retry_classifier import (  # noqa: E501
                    classify as _slice7e_classify,
                )
                # Slice 127 — economic reclassification gate (default-FALSE,
                # §33.1). Lives in economic_router so the PURE-DATA classifier
                # stays env-free (AST-pinned).
                from backend.core.ouroboros.governance.economic_router import (  # noqa: E501
                    economic_reclassify_enabled as _s127_econ_reclassify_enabled,
                )
                from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                    publish_provider_failure_classified as _slice7e_publish_classified,
                    publish_circuit_breaker_state_change as _slice7e_publish_state_change,
                    publish_circuit_breaker_tripped as _slice7e_publish_tripped,
                )

                _slice7e_op_id = str(
                    getattr(context, "op_id", "") or "",
                )
                # Slice 12N — blast-radius isolation. Map the op's
                # ProviderRoute to a CircuitTripOrigin so background /
                # speculative ops can trip their per-op breaker
                # WITHOUT escalating to the global session_exhausted
                # threshold. Foreground (IMMEDIATE / STANDARD /
                # COMPLEX) routes still escalate byte-identically to
                # pre-Slice-12N behavior. Unknown routes default to
                # FOREGROUND (safer — preserves legacy escalation).
                _slice12n_route = str(
                    getattr(context, "provider_route", "") or "",
                ).strip().lower()
                _slice12n_origin = _SLICE12N_ROUTE_TO_ORIGIN.get(
                    _slice12n_route,
                    _Slice12N_CircuitTripOrigin.FOREGROUND,
                )
                _slice7e_breaker = _Slice7e_CircuitBreaker(
                    op_id=_slice7e_op_id,
                    scope=_Slice7e_CircuitScope.PER_OP,
                    origin=_slice12n_origin,
                )
                _outer_attempt = 0
                # Anthropic resilience pack 2026-04-25 — failure-rate-aware
                # outer-retry max. When the FSM shows recent transient
                # failures (consecutive_failures > 0), bump the outer-retry
                # cap to give the op more headroom to catch a recovery
                # window during external instability. Healthy ops keep
                # the base cap (no extra cost).
                _fsm_consec_fails = getattr(
                    self.fsm, "_consecutive_failures", 0,
                )
                if _fsm_consec_fails > 0 and _FALLBACK_OUTER_RETRY_MAX_DEGRADED > _FALLBACK_OUTER_RETRY_MAX:
                    _outer_max = _FALLBACK_OUTER_RETRY_MAX_DEGRADED
                    logger.info(
                        "[CandidateGenerator] Fallback outer-retry: degraded mode "
                        "detected (FSM consecutive_failures=%d) — bumping outer-retry "
                        "cap from %d to %d for op=%s (rooted-problem fix — failure-"
                        "rate-aware retry headroom)",
                        _fsm_consec_fails,
                        _FALLBACK_OUTER_RETRY_MAX, _outer_max,
                        getattr(context, "op_id", "?")[:16],
                    )
                else:
                    _outer_max = _FALLBACK_OUTER_RETRY_MAX
                _last_inner_exc: Optional[BaseException] = None
                # ── Slice 3C — outer-retry tool-record carryover ──
                # Iron Gate (post-GENERATE) inspects
                # ``GenerationResult.tool_execution_records`` to verify the
                # model met the exploration floor. Each provider attempt
                # carries only its OWN tool calls (the coordinator resets
                # ``_last_records`` at run start). If attempt N raises after
                # genuine exploration but attempt N+1 succeeds with NO tool
                # calls (the bt-2026-05-25-033000 cascade — direct patch
                # emit after retries), Iron Gate sees 0 records and rejects
                # even though the model explored the codebase across
                # attempts. Accumulator harvests records from each failed
                # attempt's exception (Slice-3C-stamped via
                # ``_attach_tool_records`` in tool_executor) and merges
                # into the winning attempt's GenerationResult below.
                _carryover_tool_records: List[Any] = []
                while True:
                    _outer_attempt += 1
                    _attempt_t0 = time.monotonic()
                    _attempt_remaining = self._remaining_seconds(deadline)
                    if _attempt_remaining < _MIN_VIABLE_FALLBACK_S:
                        # Budget exhausted — break to outer except handler
                        # which fires `fallback_budget_starved` if no prior
                        # exception, else fallback_failed with last exc.
                        if _last_inner_exc is not None:
                            raise _last_inner_exc
                        self._raise_exhausted(
                            "fallback_budget_starved",
                            context=context,
                            deadline=deadline,
                            sem_wait_s=round(_sem_wait_s, 2),
                            pre_sem_remaining_s=round(_pre_sem_remaining, 2),
                            parent_remaining_s=round(_attempt_remaining, 2),
                            fallback_budget_s=round(_attempt_remaining, 2),
                            min_viable_fallback_s=_MIN_VIABLE_FALLBACK_S,
                        )
                    # Slice 89 — Build ExplorationManifest from DW's just-
                    # completed tool loop and stamp onto context BEFORE the
                    # Claude fallback generate() call.  NEVER raises
                    # (try/except guards the full block).  When
                    # JARVIS_EXPLORATION_MANIFEST_ENABLED is OFF (default
                    # §33.1), behavior is byte-identical to today.
                    #
                    # Gate: stamp only on the FIRST Claude attempt —
                    # DW's `_last_salient_args`/`_last_records` are only
                    # valid until DW's next run() resets them.  On outer
                    # attempt 1 _carryover_tool_records is always empty
                    # (populated only INSIDE the except block below), so
                    # the old `_carryover_tool_records and _outer_attempt==1`
                    # condition was mutually exclusive — dead code (C1 fix).
                    # We now harvest directly from the primary coordinator.
                    if _outer_attempt == 1:
                        try:
                            _s89_enabled = os.environ.get(
                                "JARVIS_EXPLORATION_MANIFEST_ENABLED", "false",
                            ).strip().lower() not in ("false", "0", "no", "off")
                            if _s89_enabled:
                                from backend.core.ouroboros.governance.op_context import (
                                    ExplorationManifest as _ExplorationManifest,
                                )
                                # Harvest BOTH records and salient_args from
                                # the primary provider's coordinator — these
                                # are the DW tool loop results just before
                                # DW's generate() failed/timed-out.  Using
                                # the same coordinator source keeps them
                                # length-aligned (C2 alignment preserved).
                                _coord = getattr(
                                    self._primary, "_tool_loop", None,
                                )
                                _s89_records = tuple(
                                    getattr(_coord, "_last_records", ()) or ()
                                ) if _coord is not None else ()
                                _s89_salient = list(
                                    getattr(_coord, "_last_salient_args", ()) or ()
                                ) if _coord is not None else []
                                _s89_manifest = _ExplorationManifest.from_telemetry(
                                    records=_s89_records,
                                    salient_args=_s89_salient,
                                    reason="dw_failure",
                                )
                                context = context.with_exploration_manifest(_s89_manifest)
                                logger.info(
                                    "[CandidateGenerator] Slice 89: stamped "
                                    "ExplorationManifest onto context "
                                    "(tool_calls=%d, target_files=%d, "
                                    "search_tokens=%d, failed_tests=%d) op=%s",
                                    _s89_manifest.tool_call_count,
                                    len(_s89_manifest.verified_target_files),
                                    len(_s89_manifest.high_signal_search_tokens),
                                    len(_s89_manifest.failed_test_commands),
                                    getattr(context, "op_id", "?")[:16],
                                )
                        except Exception:  # noqa: BLE001 — never break the cascade
                            pass
                    try:
                        _fb_result = await _race_or_wait_for(
                            self._fallback.generate(context, deadline),
                            timeout=_attempt_remaining,
                            cancel_token=_curr_cancel_token(),
                        )
                        if _outer_attempt > 1:
                            logger.info(
                                "[CandidateGenerator] Fallback outer-retry "
                                "succeeded on attempt %d/%d after %.1fs "
                                "(rooted-problem fix consumed %.1fs of "
                                "previously-unused budget)",
                                _outer_attempt, _outer_max,
                                time.monotonic() - _sem_t0,
                                time.monotonic() - _sem_t0 - (
                                    _attempt_t0 - _sem_t0
                                ),
                            )
                        logger.info(
                            "[CandidateGenerator] Fallback sem release: "
                            "hold=%.1fs sem_wait=%.1fs route=%s phase=%s op=%s outcome=ok",
                            time.monotonic() - _sem_t0, _sem_wait_s,
                            getattr(context, "provider_route", "?"),
                            _phase_hint,
                            getattr(context, "op_id", "?")[:16],
                        )
                        # Slice 3C — merge carryover records into the
                        # winning attempt's GenerationResult so Iron Gate
                        # sees the cumulative exploration across attempts.
                        # No-op when carryover is empty (single-attempt
                        # success path) — byte-identical legacy behavior.
                        if _carryover_tool_records:
                            try:
                                _winning_records = tuple(
                                    getattr(
                                        _fb_result,
                                        "tool_execution_records",
                                        (),
                                    ) or ()
                                )
                                _merged = (
                                    tuple(_carryover_tool_records)
                                    + _winning_records
                                )
                                _with_tool_records = getattr(
                                    _fb_result, "with_tool_records", None,
                                )
                                if callable(_with_tool_records):
                                    _fb_result = _with_tool_records(_merged)
                                    logger.info(
                                        "[CandidateGenerator] Slice 3C: "
                                        "merged %d carryover tool records "
                                        "from %d failed attempt(s) into "
                                        "winning GenerationResult "
                                        "(winning=%d, total=%d) op=%s",
                                        len(_carryover_tool_records),
                                        _outer_attempt - 1,
                                        len(_winning_records),
                                        len(_merged),
                                        getattr(
                                            context, "op_id", "?",
                                        )[:16],
                                    )
                            except Exception:  # noqa: BLE001 — defensive
                                # Carryover merge must NEVER break a
                                # successful generate. Log + fall through
                                # with the unmerged result.
                                logger.exception(
                                    "[CandidateGenerator] Slice 3C "
                                    "carryover merge degraded — returning "
                                    "winning-attempt GenerationResult "
                                    "unmodified for op=%s",
                                    getattr(context, "op_id", "?")[:16],
                                )
                        return _fb_result
                    except _OperationCancelledError:
                        # W3(7) cooperative cancel — operator/watchdog/signal.
                        # NEVER retry; honor the cancel immediately.
                        raise
                    except (Exception, asyncio.CancelledError) as inner_exc:
                        # Slice 3C — harvest tool-records from the failed
                        # attempt BEFORE any other handling. Best-effort:
                        # if the exception didn't carry records (legacy
                        # provider, untagged path), getattr returns (),
                        # extend is a no-op, behavior matches pre-Slice-3C.
                        #
                        # Slice 3D (2026-05-24) — coordinator-attribute
                        # fallback. The Slice 3C exception attachment only
                        # fires on raises through ToolLoopCoordinator's
                        # ``_attach_tool_records`` sites. When the outer
                        # ``_race_or_wait_for(... timeout=_attempt_remaining)``
                        # hits its deadline, it raises asyncio.TimeoutError
                        # / CancelledError that DOES NOT traverse the tool
                        # executor's raise sites — the records sit untouched
                        # in ``coordinator._last_records``. The
                        # bt-2026-05-25-041717 attempt 1: 244.2s tool loop
                        # made 13+ tool calls then the outer race timed
                        # out; Slice 3C harvested nothing because the
                        # TimeoutError was opaque; Iron Gate saw 0/2 on
                        # the final attempt and the cumulative exploration
                        # was lost.
                        #
                        # Fallback path: when the exception carries no
                        # records, read directly from the coordinator's
                        # instance attribute. ``_last_records`` is reset to
                        # ``[]`` at every ``run()`` start (tool_executor.py
                        # line 5250) and re-populated at each round
                        # boundary, so at except-block time it reflects
                        # exactly the just-failed attempt's records — no
                        # cross-attempt double-counting.
                        try:
                            _harvested = getattr(
                                inner_exc, "tool_execution_records", (),
                            ) or ()
                            if not _harvested:
                                # Slice 3D fallback — coordinator probe.
                                # Defensive getattr chain: any provider
                                # without ``_tool_loop`` (tools disabled
                                # config) or coordinator without
                                # ``_last_records`` (legacy/test stub)
                                # falls through to empty harvest.
                                _coord = getattr(
                                    self._fallback, "_tool_loop", None,
                                )
                                if _coord is not None:
                                    _harvested = tuple(
                                        getattr(
                                            _coord, "_last_records", (),
                                        ) or ()
                                    )
                                    if _harvested:
                                        logger.info(
                                            "[CandidateGenerator] Slice 3D: "
                                            "harvested %d records from "
                                            "coordinator._last_records "
                                            "(exception %s carried 0; "
                                            "fallback succeeded) op=%s",
                                            len(_harvested),
                                            type(inner_exc).__name__,
                                            getattr(
                                                context, "op_id", "?",
                                            )[:16],
                                        )
                            if _harvested:
                                _carryover_tool_records.extend(_harvested)
                        except Exception:  # noqa: BLE001 — never block retry
                            pass
                        # Pre-instrumented (e.g. fallback_budget_starved
                        # from a different code path) → propagate as-is.
                        if hasattr(inner_exc, "exhaustion_report"):
                            raise
                        _last_inner_exc = inner_exc
                        _inner_mode = (
                            FailbackStateMachine.classify_exception(inner_exc)
                        )
                        # Slice 7e — Consult the Circuit Breaker on
                        # every failure. When master flag is OFF,
                        # ``evaluate()`` returns RETRY_OK → byte-
                        # identical to the pre-7e path. When ON,
                        # TERMINAL_STRUCTURAL / TERMINAL_CONFIG short-
                        # circuit immediately (closing the 35-min
                        # retry storm); TERMINAL_QUOTA + repeated
                        # RETRY_TRANSIENT trigger Full-Jitter backoff;
                        # the FSM / outer-retry cap / existing
                        # eligibility check below remain as additional
                        # gates (defense in depth — no breaker bypass
                        # of the pre-existing semantics).
                        # Slice 127 — pass the raw message + economic gate so a
                        # "credit balance too low" 400 (class BadRequestError)
                        # reclassifies to recoverable TERMINAL_QUOTA instead of
                        # sticky TERMINAL_CONFIG. Gate default-FALSE → OFF is
                        # byte-identical to pre-127. (bt-2026-06-07-040933 root
                        # cause: 16 sticky terminal_config trips.)
                        try:
                            _s127_econ_on = _s127_econ_reclassify_enabled()
                        except Exception:  # noqa: BLE001 — failure-soft
                            _s127_econ_on = False
                        _slice7e_decision = _slice7e_classify(
                            failure_class=type(inner_exc).__name__,
                            failure_mode=_inner_mode.name,
                            failure_message=str(inner_exc),
                            economic_reclassify=_s127_econ_on,
                        )
                        # Slice 127 Phase 2 — per-provider economic breaker.
                        # The fallback IS the Claude/Anthropic lane; when this
                        # failure is an economic block ("credit balance too
                        # low" / 402), trip the per-lane self-healing breaker so
                        # FUTURE ops route around the broke lane (existing
                        # should_allow_request gate) and it recovers after the
                        # window — no sticky session brick. Gated + defensive;
                        # detail is the redacted economic code, never a secret.
                        _claude_econ_block = False
                        try:
                            if _s127_econ_on:
                                from backend.core.ouroboros.governance.economic_router import (  # noqa: E501
                                    is_hard_economic_block as _s127_is_econ,
                                )
                                _s127_block = _s127_is_econ(str(inner_exc))
                                if _s127_block is not None:
                                    _claude_econ_block = True
                                    from backend.core.ouroboros.governance.claude_circuit_breaker import (  # noqa: E501
                                        get_claude_circuit_breaker as _s127_get_ccb,
                                    )
                                    _s127_get_ccb().record_economic_exhaustion(
                                        f"claude_lane_economic:{_s127_block}",
                                    )
                        except Exception:  # noqa: BLE001 — never block cascade
                            pass
                        # Telemetry — every classification is logged,
                        # regardless of breaker state. Best-effort.
                        _slice7e_publish_classified(
                            failure_class=type(inner_exc).__name__,
                            failure_mode=_inner_mode.name,
                            decision=_slice7e_decision.value,
                            provider="claude",
                            op_id=_slice7e_op_id,
                        )
                        # Sovereign State Isolation (2026-06-19): a confirmed
                        # Claude economic death is OWNED by Claude's lane breaker
                        # (recorded above). Do NOT let it trip the provider-
                        # NEUTRAL per-op breaker into the sticky OPEN_TERMINAL —
                        # that poisons the op for DW too (empirically-confirmed
                        # cross-provider contamination: terminal_quota 5->0 once
                        # isolated). Downgrade ONLY the decision the OP breaker
                        # sees to RETRY_TRANSIENT (non-poisoning) so the op stays
                        # viable for DW autarky; the Claude lane is already marked
                        # dead by its global breaker + the Slice238 suppression.
                        # Telemetry above already published the TRUE decision.
                        if quota_isolation_skips_op_breaker(
                            is_provider_economic_block=_claude_econ_block,
                            isolation_enabled=_provider_quota_isolation_enabled(),
                        ):
                            logger.warning(
                                "[CandidateGenerator] QUOTA ISOLATION: Claude "
                                "economic death contained to Claude lane breaker; "
                                "per-op breaker NOT terminal-tripped (op stays "
                                "viable for DW autarky) op=%s",
                                _slice7e_op_id,
                            )
                            _slice7e_decision = type(
                                _slice7e_decision
                            ).RETRY_TRANSIENT
                        _slice7e_prior_state = _slice7e_breaker.state.value
                        _slice7e_verdict = _slice7e_breaker.evaluate(
                            _slice7e_decision,
                        )
                        _slice7e_new_state = _slice7e_verdict.state_after \
                            and _slice7e_verdict.state_after.value or \
                            _slice7e_breaker.state.value
                        if _slice7e_prior_state != _slice7e_new_state:
                            # State change → SSE telemetry. Trip
                            # events use the more-specific publisher
                            # below; non-trip transitions go here.
                            if _slice7e_verdict.action != (
                                _Slice7e_VerdictAction.TERMINATE_UNRESOLVED
                            ):
                                _slice7e_publish_state_change(
                                    prior_state=_slice7e_prior_state,
                                    new_state=_slice7e_new_state,
                                    op_id=_slice7e_op_id,
                                    scope="per_op",
                                )
                        if _slice7e_verdict.action == (
                            _Slice7e_VerdictAction.TERMINATE_UNRESOLVED
                        ):
                            # Breaker trip — emit the trip SSE event
                            # + raise exhausted with the breaker's
                            # reason code. The orchestrator's existing
                            # exhaustion handler picks up the cause
                            # tag end-to-end; the parallel evaluator
                            # can subscribe to circuit_breaker_tripped
                            # for early-collapse instead of waiting
                            # on operation_terminal.
                            _slice7e_reason = (
                                _slice7e_verdict.terminal_reason_code
                                or "circuit_breaker_tripped:unknown"
                            )
                            _slice7e_publish_tripped(
                                terminal_reason_code=_slice7e_reason,
                                op_id=_slice7e_op_id,
                                scope="per_op",
                                backoff_attempt=(
                                    _slice7e_breaker.backoff_attempt
                                ),
                            )
                            self._raise_exhausted(
                                _slice7e_reason,
                                context=context,
                                deadline=deadline,
                                fallback_exc=inner_exc,
                                fallback_failure_mode=_inner_mode.name,
                                slice7e_decision=(
                                    _slice7e_decision.value
                                ),
                            )
                        # Permanent failures — never retry.
                        if not _is_outer_retry_eligible_mode(_inner_mode):
                            raise
                        # Hit the outer-retry cap.
                        if _outer_attempt >= _outer_max:
                            raise
                        # Budget check before backoff.
                        _attempt_elapsed = time.monotonic() - _attempt_t0
                        _budget_after = self._remaining_seconds(deadline)
                        if _budget_after < _MIN_VIABLE_FALLBACK_S:
                            raise
                        logger.info(
                            "[CandidateGenerator] Fallback outer-retry: "
                            "attempt %d/%d failed (%s/%s) after %.1fs; "
                            "%.1fs budget remains, retrying op=%s "
                            "(rooted-problem fix — consuming budget JARVIS "
                            "already authorized, not inflating)",
                            _outer_attempt, _outer_max,
                            type(inner_exc).__name__,
                            _inner_mode.name,
                            _attempt_elapsed, _budget_after,
                            getattr(context, "op_id", "?")[:16],
                        )
                        # Brief backoff between outer attempts. Capped at
                        # remaining-budget/4 so a 12s budget doesn't sleep
                        # 1s of it (which would risk underflow into the
                        # min_viable floor on the next attempt).
                        #
                        # Slice 7e — when the breaker returned
                        # RETRY_AFTER_BACKOFF with a non-None backoff_s,
                        # use the Full-Jitter delay (AWS algorithm)
                        # instead of the fixed constant. This is the
                        # anti-thundering-herd path. The budget/4 clamp
                        # still applies so a tight remaining budget
                        # doesn't oversleep.
                        if (
                            _slice7e_verdict.action == (
                                _Slice7e_VerdictAction.RETRY_AFTER_BACKOFF
                            )
                            and _slice7e_verdict.backoff_s is not None
                        ):
                            _backoff = min(
                                float(_slice7e_verdict.backoff_s),
                                max(0.1, _budget_after / 4.0),
                            )
                        else:
                            _backoff = min(
                                _FALLBACK_OUTER_RETRY_BACKOFF_S,
                                max(0.1, _budget_after / 4.0),
                            )
                        await asyncio.sleep(_backoff)
                        continue
                # Unreachable — loop either returns or raises.
        except (Exception, asyncio.CancelledError) as exc:
            # Cooperative cancel via W3(7) cancel-token — propagate
            # immediately (NEVER treat as exhaustion). The inner loop
            # raises OperationCancelledError; this outer handler must
            # not swallow it into the fallback_failed taxonomy or the
            # operator's cancel signal would be silently downgraded
            # into "another transport failure".
            from backend.core.ouroboros.governance.cancel_token import (
                OperationCancelledError as _OperationCancelledError_outer,
            )
            if isinstance(exc, _OperationCancelledError_outer):
                raise
            # If the exception is already instrumented (e.g. the inner
            # ``fallback_budget_starved`` raise), re-raise as-is so we
            # preserve the more-specific cause and don't double-count
            # the exhaustion event counter.
            if hasattr(exc, "exhaustion_report"):
                raise
            logger.info(
                "[CancelAttribution] %s",
                _attribute_cancel(
                    exc,
                    label="_call_fallback",
                    op_id=getattr(context, "op_id", "?"),
                    elapsed_s=time.monotonic() - _sem_t0,
                    remaining_s=self._remaining_seconds(deadline),
                ),
            )
            mode = FailbackStateMachine.classify_exception(exc)
            self.fsm.record_fallback_failure(mode=mode)
            # Distinct cause tag when the tool-loop pre-round viability
            # gate fired. This is NOT a transport/API failure — it's a
            # round-level budget exhaustion that the ToolLoopCoordinator
            # caught before a doomed sub-floor call. Keeping the cause
            # distinct in breadcrumbs lets grep audits see "round_starved"
            # vs generic "fallback_failed" without reading full messages.
            _cause = "fallback_failed"
            if "tool_loop_round_budget_starved" in str(exc):
                _cause = "fallback_round_starved"
            self._raise_exhausted(
                _cause,
                context=context,
                deadline=deadline,
                fallback_exc=exc,
                fallback_failure_mode=mode.name,
                sem_wait_total_s=round(time.monotonic() - _sem_t0, 2),
                pre_sem_remaining_s=round(_pre_sem_remaining, 2),
            )

    @staticmethod
    def _remaining_seconds(deadline: datetime) -> float:
        """Compute seconds remaining until *deadline*.

        Returns a non-negative float.  If the deadline has already passed,
        returns 0.0 (which will cause ``asyncio.wait_for`` to time out
        immediately).
        """
        now = datetime.now(tz=timezone.utc)
        remaining = (deadline - now).total_seconds()
        return max(remaining, 0.0)

    # ------------------------------------------------------------------
    # Per-op Tier 0 rotation (Manifesto §5 — defensive cost guard)
    # ------------------------------------------------------------------

    def _should_skip_tier0_for_op(self) -> bool:
        """Return True if Tier 0 should be skipped for the current op.

        Skips when ``_consecutive_tier0_failures`` reaches the threshold
        AND the most recent failure happened within ``_tier0_skip_window_s``
        seconds. Outside the window the counter resets implicitly because
        the elapsed-time check fails — equivalent to a stale-feed reset.

        This is independent of the FSM's mode-based ETA: even if the
        classifier mis-routes a transport flap to TIMEOUT (default), this
        guard still kicks in after N back-to-back failures.
        """
        if self._counters.consecutive_tier0_failures < self._tier0_skip_threshold:
            return False
        elapsed = time.monotonic() - self._counters.last_tier0_failure_at
        return elapsed < self._tier0_skip_window_s

    def _record_tier0_failure(self) -> None:
        """Increment the per-op rotation counter and stamp the timestamp."""
        self._counters.consecutive_tier0_failures += 1
        self._counters.last_tier0_failure_at = time.monotonic()

    def _record_tier0_success(self) -> None:
        """Reset the per-op rotation counter on Tier 0 success."""
        if self._counters.consecutive_tier0_failures > 0:
            logger.info(
                "[CandidateGenerator] Tier 0 rotation reset after %d "
                "consecutive failures",
                self._counters.consecutive_tier0_failures,
            )
        self._counters.consecutive_tier0_failures = 0
        self._counters.last_tier0_failure_at = 0.0

    # ------------------------------------------------------------------
    # Deadline budget allocation (deterministic — Manifesto §5)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_tier0_budget(
        total_s: float,
        complexity: str = "trivial",
        provider_route: str = "standard",
    ) -> float:
        """Deterministic Tier 0 (DoubleWord) budget with Tier 1 reserve.

        Tier 0 is the preferred path (cheap, Manifesto §5 Tier 0 fast-path).
        It gets 65% of the total budget by default.  When the total budget is
        tight (< 90s), we log a warning — both tiers may starve.

        *complexity* scales the base fraction via ``_TIER0_COMPLEXITY_MULTIPLIER``
        so that complex operations receive proportionally more Tier 0 time
        (e.g. 80% instead of 65%).

        *provider_route* overrides the budget profile for non-standard routes:
          - "complex":     80% fraction, 120s max, 20s reserve (DW executes plan)
          - "background":  100% fraction, 180s max, 0s reserve (DW only)
          - "immediate":   0% (skip DW entirely)

        Invariants:
          - tier0_budget <= total_s * effective_fraction
          - tier0_budget <= max_wait_s
          - total_s - tier0_budget >= tier1_reserve (when possible)
        """
        if total_s <= 0:
            return 0.0

        # Route-aware budget profile overrides
        if provider_route == "immediate":
            return 0.0
        if provider_route == "background":
            # DW only — no Claude reserve needed
            return min(total_s, 180.0)
        if provider_route == "speculative":
            return min(total_s, 300.0)

        if total_s < 90.0 and provider_route == "standard":
            logger.warning(
                "[CandidateGenerator] Generation budget tight (%.0fs < 90s). "
                "Consider increasing JARVIS_GENERATION_TIMEOUT_S for reliable "
                "2-tier cascade.",
                total_s,
            )

        multiplier = _TIER0_COMPLEXITY_MULTIPLIER.get(complexity, 1.0)

        # COMPLEX route: DW gets more budget (Claude already planned)
        if provider_route == "complex":
            effective_fraction = min(_TIER0_BUDGET_FRACTION * max(multiplier, 1.231), 0.90)
            max_wait = 120.0
            min_reserve = 20.0
        else:
            effective_fraction = min(_TIER0_BUDGET_FRACTION * multiplier, 0.90)
            max_wait = _TIER0_MAX_WAIT_S
            min_reserve = _TIER1_MIN_RESERVE_S

        # Reserve Tier 1 budget first (defensive — Tier 1 must always get a chance)
        tier1_reserve = min(min_reserve, total_s * (1.0 - effective_fraction))
        # Tier 3 Reflex (Manifesto §5): absolute hard cap on DW calls.
        # Strictest of four constraints wins (fraction, route max_wait,
        # tier1 reserve, Tier 0 RT cap). Added 2026-04-24 after F1 Slice 4 S4
        # (bt-2026-04-24-213248) proved the previous patch (inside
        # _call_primary) was inert for the DW-is-Tier0-AND-Primary
        # configuration — this code path is where DW actually gets its
        # 90s max_wait in that configuration.
        #
        # Slice 18c (2026-05-26) — the 4th constraint is now route-aware.
        # STANDARD + COMPLEX get the new JARVIS_DW_TIER0_RT_BUDGET_S
        # (default 90s — matches 397B/Kimi TTFT envelope) instead of the
        # 30s reflex cap. Eliminates the FLEET-v13-soak premature-timeout
        # cascade pattern (8 EXHAUSTION events, each on a DW dispatch
        # that needed >30s to complete). IMMEDIATE/BG/SPEC preserved at
        # 30s for cost-optimization semantics.
        budget = min(
            total_s * effective_fraction,
            max_wait,
            total_s - tier1_reserve,
            _tier0_rt_cap_for_route(provider_route),
        )
        return max(budget, 0.0)

    def _compute_tier0_budget_dynamic(
        self,
        total_s: float,
        complexity: str = "trivial",
        provider_route: str = "standard",
    ) -> float:
        """Tier 0 budget with rolling p95 awareness (Manifesto §5).

        Computes the static deterministic budget first (preserving all Tier 1
        reserve invariants), then tightens it using the latency tracker's p95
        recommendation when the endpoint is hot. On cold start (few samples
        or recent failures), falls through to the static budget so the first
        calls get full runway.

        The tracker NEVER loosens beyond the static ceiling — it only dials
        down when DW RT has proven fast enough.
        """
        static_budget = self._compute_tier0_budget(total_s, complexity, provider_route)
        if static_budget <= 0:
            return 0.0

        # Routes that skip tracker scaling entirely.
        if provider_route in ("immediate", "background", "speculative"):
            return static_budget

        tracker = self._latency_tracker
        if tracker is None:
            return static_budget

        # Use the static budget as the caller-provided ceiling — the tracker
        # can only dial down from here, never above it. Tier 1 reserve is
        # already guaranteed by _compute_tier0_budget.
        complexity_mult = _TIER0_COMPLEXITY_MULTIPLIER.get(complexity, 1.0)
        recommended = tracker.recommended_budget(
            route_ceiling_s=static_budget,
            complexity_multiplier=complexity_mult,
        )
        final_budget = max(0.0, min(static_budget, recommended))

        if final_budget < static_budget - 0.5:
            logger.info(
                "[CandidateGenerator] DW dynamic budget: %.1fs → %.1fs "
                "(hot endpoint, p95=%.1fs)",
                static_budget, final_budget, tracker.p95() or 0.0,
            )
        return final_budget

    @staticmethod
    def _compute_primary_budget(
        total_s: float,
        *,
        model_id: str = "",
        force_batch: bool = False,
        fallback_dead: bool = False,
    ) -> float:
        """Deterministic Tier 1 primary budget with fallback reserve + Tier 3 cap.

        Invariants (enforced via ``min()`` — strictest wins):
          - primary_budget <= total_s * _PRIMARY_BUDGET_FRACTION
          - total_s - primary_budget >= _FALLBACK_MIN_RESERVE_S (when possible)
          - primary_budget <= effective_max (Slice 28 adaptive Tier 3 cap)

        Tier 3 cap added 2026-04-24 after F1 Slice 4 S3 (bt-2026-04-24-204029)
        exposed a 153s DW primary hold that exhausted the session before
        Claude fallback could produce a candidate.

        Slice 28 Phase 2 — Adaptive Streaming TTFT Horizon
        ---------------------------------------------------
        v21 forensic (bt-2026-05-27-025855) revealed the actual wedge: 12
        EXHAUSTION events on the 397B model, all classified as TIMEOUT, all
        firing at elapsed=30.01s with remaining=329.86s. The static
        ``_PRIMARY_MAX_TIMEOUT_S`` (30s default) was killing primary calls
        long before the streaming layer's 120s TTFT could even fire on the
        wire. Cold-start TTFT for a 397B MoE on a contended endpoint
        legitimately exceeds 30s — per §46 fleet inventory the 397B is
        characterized as a heavy-reasoning workhorse whose TTFT envelope
        is materially larger than the 35B sibling.

        When ``model_id`` is a heavy-reasoning / long-context model
        (matched against the same marker set Slice 27 Phase 3 uses for the
        adaptive Tier 0 timeout), multiply ``_PRIMARY_MAX_TIMEOUT_S`` by
        a heavy scalar (default 2.5×) so the call has runway to receive
        the first token. Hard ceiling at 240s matches the Slice 27 Phase 3
        cap (no unbounded cost bleeding).

        Legacy callers that pass only ``total_s`` (no ``model_id``) get the
        byte-identical pre-Slice-28 behavior — the 30s cap is preserved as
        the binding constraint. The adaptive widening engages only when
        the dispatcher has stamped the per-attempt model_id via the
        topology ContextVar.
        """
        if total_s <= 0:
            return 0.0

        # Slice 43 — Async Batch Timeout Alignment.
        # When the op will be dispatched through the BATCH lane (Slice 36/41
        # FORCE_BATCH), the provider's internal poll_and_retrieve legitimately
        # runs for minutes — the batch_future_registry waits up to
        # _DW_MAX_WAIT_S (3600s). Wrapping that in the 30s RT reflex cap
        # (_PRIMARY_MAX_TIMEOUT_S) severs the async batch mid-flight (v37
        # bt-2026-05-28-235234: batch 7b7a7b52 submitted then abandoned at
        # 30s). Give batch ops a batch-appropriate budget instead, capped by
        # remaining session time. force_batch implies Claude is disabled
        # (Slice 36 precondition) → no fallback to reserve for, so the batch
        # gets the full remaining runway up to the batch cap.
        if force_batch:
            batch_cap = _envf_or_default("JARVIS_DW_BATCH_TIMEOUT_S", 300.0)
            return max(min(total_s, batch_cap), 0.0)

        # Slice 225 Phase 2 — Sovereign DW Autarky. When the Claude fallback
        # lane is unreliable (breaker OPEN/HALF_OPEN — incl. the terminal_quota
        # / out-of-credits economic refusal), there is NO live fallback to hand
        # off to. Severing DW at the 30s/75s reflex cap only accelerates
        # exhaustion into a dead lane — the live-soak GOAL-001::file-00 wedge:
        # DW cut at 30s -> Claude 400 "credit balance too low" -> EXHAUSTION,
        # generation_failed, no patch ever produced. Give DW the full remaining
        # runway up to a cost-safety ceiling instead (default 180s = the COMPLEX
        # generation window). Mirrors the force_batch precedent directly above
        # ("Claude disabled -> no fallback to reserve -> full runway"). The
        # caller stamps fallback_dead from the read-only _claude_breaker_open
        # predicate; default False is byte-identical to the legacy cascade.
        if fallback_dead:
            autarky_cap = _envf_or_default(
                "JARVIS_DW_AUTARKY_MAX_BUDGET_S", 180.0,
            )
            return max(min(total_s, autarky_cap), 0.0)

        fb_reserve = min(_FALLBACK_MIN_RESERVE_S, total_s * 0.35)

        # Slice 28 Phase 2 — adaptive Tier 3 cap for heavy models
        effective_max = _PRIMARY_MAX_TIMEOUT_S
        if model_id and _is_heavy_model(model_id):
            scalar = _envf_or_default(
                "JARVIS_PRIMARY_HEAVY_TTFT_SCALAR",
                _PRIMARY_HEAVY_TTFT_SCALAR_DEFAULT,
            )
            cap = _envf_or_default(
                "JARVIS_PRIMARY_HEAVY_TTFT_CAP_S",
                _PRIMARY_HEAVY_TTFT_CAP_S_DEFAULT,
            )
            effective_max = min(_PRIMARY_MAX_TIMEOUT_S * scalar, cap)

        budget = min(
            total_s * _PRIMARY_BUDGET_FRACTION,
            total_s - fb_reserve,
            effective_max,
        )
        return max(budget, 0.0)


# ---------------------------------------------------------------------------
# Defect #4 fix (2026-05-03) — substrate AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Defect #4 substrate pin. Pins:

      * ``_swallow_task_exception`` helper present (Slice A
        task-leak prevention).
      * ``deadline_exhausted_pre_fallback`` cause string present
        (Slice B pre-fallback budget short-circuit).
      * Every ``asyncio.ensure_future(...)`` / ``asyncio.create_task(...)``
        of provider .generate() OR background-poll coroutines has a
        paired ``add_done_callback(_swallow_task_exception)`` within
        the surrounding statements (catches the regression to
        unprotected task spawns).
      * No exec/eval/compile.
    """
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "_swallow_task_exception",
        "register_shipped_invariants",
    )
    REQUIRED_LITERALS = (
        "deadline_exhausted_pre_fallback",
        "JARVIS_FALLBACK_MIN_VIABLE_BUDGET_S",
        "_EXPECTED_BACKGROUND_EXC_PATTERNS",
        # Defect #5 (2026-05-03) — read-only cascade reflex lifted
        # into _dispatch_via_sentinel queue branch. Pinned via the
        # cascade-reason marker so a regression that re-removes the
        # reflex (e.g., reverting to the unconditional raise that
        # killed 17/19 BG ops in soak v5) fires the AST pin.
        "Sentinel queue tolerance OVERRIDE",
        "read_only_cost_safe",
    )

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        # Compute the line range of _swallow_task_exception so we can
        # exclude its body (docstring mentions ensure_future/create_task
        # in documentation, would be false positives).
        helper_line_range: tuple = (-1, -1)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                seen_funcs.add(node.name)
                if node.name == "_swallow_task_exception":
                    helper_line_range = (
                        node.lineno,
                        getattr(node, "end_lineno", node.lineno + 80) or 0,
                    )
            elif isinstance(node, _ast.AsyncFunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"candidate_generator MUST NOT call "
                            f"{node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for lit in REQUIRED_LITERALS:
            if lit not in source:
                violations.append(
                    f"missing string literal {lit!r}"
                )
        # Pairing pin: every ensure_future / create_task of a
        # provider .generate() must have add_done_callback in the
        # next ~10 source lines. Source-level heuristic (cheap +
        # robust to AST traversal noise from nested coroutines).
        # Skip lines inside _swallow_task_exception body (its
        # docstring mentions the spawn primitives as documentation).
        lines = source.splitlines()
        helper_lo, helper_hi = helper_line_range
        for idx, line in enumerate(lines):
            line_no = idx + 1
            if helper_lo <= line_no <= helper_hi:
                continue  # skip inside helper body
            stripped = line.strip()
            if (
                ("asyncio.ensure_future" in stripped
                 or "asyncio.create_task" in stripped)
                and (".generate(" in stripped
                     or "_background_poll_tier0" in stripped
                     or ("generate" in stripped and "self._tier0" in stripped))
            ):
                # Look for add_done_callback in next 10 source lines.
                window = lines[idx:idx + 10]
                if not any(
                    "add_done_callback" in w
                    and "_swallow_task_exception" in w
                    for w in window
                ):
                    violations.append(
                        f"line {line_no}: ensure_future/create_task "
                        "of .generate() / background-poll must have "
                        "paired add_done_callback(_swallow_task_"
                        "exception) within 10 lines (Defect #4 "
                        "Slice A task-leak prevention)"
                    )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/candidate_generator.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="candidate_generator_defect4_substrate",
            target_file=target,
            description=(
                "Defect #4: _swallow_task_exception helper + paired "
                "add_done_callback for every ensure_future/create_task "
                "of provider .generate() / background-poll + "
                "deadline_exhausted_pre_fallback short-circuit cause; "
                "no dynamic-code calls."
            ),
            validate=_validate,
        ),
    ]
