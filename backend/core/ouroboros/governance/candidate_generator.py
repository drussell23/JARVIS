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
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Any, Dict, List, NoReturn, Optional, Protocol, runtime_checkable

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
        from backend.core.ouroboros.governance.stream_rupture import (
            StreamRuptureError,
        )
        if isinstance(exc, StreamRuptureError):
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
        fallback: CandidateProvider,
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
        # When ``JARVIS_TOPOLOGY_SENTINEL_ENABLED=true``, the sentinel
        # walks the route's ranked ``dw_models`` list (yaml v2) and
        # picks the first model whose breaker is not OPEN.
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
        _flag_raw = os.environ.get(
            "JARVIS_TOPOLOGY_SENTINEL_ENABLED", "",
        ).strip().lower()
        if _flag_raw in ("1", "true", "yes", "on"):
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
        if _topology.enabled and not _topology.dw_allowed_for_route(
            _provider_route,
        ):
            _block_reason = _topology.reason_for_route(_provider_route)
            _block_mode = _topology.block_mode_for_route(_provider_route)
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
            attempts.append(f"{model_id}:attempted")
            # Stamp the per-attempt override via ContextVar (async-safe
            # per asyncio task, survives the frozen OperationContext
            # contract). The ContextVar is reset in the finally block
            # so the next iteration's set is a clean state, and so
            # cascade-to-Claude after exhaustion doesn't carry a stale
            # override into the fallback provider.
            _override_token = _set_override(model_id)
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
                    # standard / complex / unknown — use the primary-
                    # first cascade. This walks the existing tier-0
                    # → tier-1 logic which honors the ContextVar via
                    # DoublewordProvider._resolve_effective_model.
                    _attempt_result = await self._try_primary_then_fallback(
                        context, deadline,
                    )
            except Exception as exc:
                _attempt_exc = exc
            finally:
                # Reset ContextVar before either success-return or
                # failure-continue so the next iteration starts with a
                # clean slate AND the post-loop cascade-to-Claude
                # doesn't carry a stale override into the fallback.
                _reset_override(_override_token)

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
                return _attempt_result

            if _attempt_exc is not None:
                exc = _attempt_exc
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

                if _is_modality or _is_auth_terminal:
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
                        type(exc).__name__, op_id_short,
                    )
                attempts[-1] = f"{model_id}:failed:{failure_source.value}"
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
            # Same exception shape the orchestrator's existing
            # accept-failure branch already handles for BG/SPEC.
            if provider_route == "speculative":
                raise RuntimeError(
                    f"speculative_deferred:dw_severed_queued:"
                    f"{(last_failure or 'all_models_open')[:120]}"
                )
            raise RuntimeError(
                f"background_dw_blocked_by_topology:"
                f"dw_severed_queued:"
                f"{(last_failure or 'all_models_open')[:120]}"
            )
        # cascade_to_claude — Claude is the explicit cost contract.
        if self._fallback is None:
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

        if not jprime_primacy_enabled():
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
        async with self._fallback_sem:
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
    ) -> GenerationResult:
        """Try primary, fall back on any failure.

        Note: In Python 3.9, ``CancelledError`` is a ``BaseException`` (not
        ``Exception``), so we must catch it explicitly to handle
        ``asyncio.wait_for`` cancellation of the primary call.
        """
        try:
            result = await self._call_primary(context, deadline)
            # Primary succeeded — record recovery if we were in a failure state
            if self.fsm._consecutive_failures > 0:
                self.fsm.record_primary_success()
            return result
        except (Exception, asyncio.CancelledError) as exc:
            mode = FailbackStateMachine.classify_exception(exc)
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

    async def _call_primary(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Call primary provider with concurrency and budget-capped deadline.

        The primary gets at most ``_PRIMARY_BUDGET_FRACTION`` of the
        remaining time, guaranteeing ``_FALLBACK_MIN_RESERVE_S`` for the
        fallback provider if the primary hangs until timeout.
        """
        _primary_sem_t0 = time.monotonic()
        _primary_phase_hint = getattr(getattr(context, "phase", None), "name", "?")
        logger.info(
            "[CandidateGenerator] Primary sem acquire: slots_free=%d "
            "route=%s phase=%s op=%s",
            self._primary_sem._value,
            getattr(context, "provider_route", "?"),
            _primary_phase_hint,
            getattr(context, "op_id", "?")[:16],
        )
        async with self._primary_sem:
            _primary_sem_wait_s = time.monotonic() - _primary_sem_t0
            remaining = self._remaining_seconds(deadline)
            primary_budget = self._compute_primary_budget(remaining)
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
                _pri_result = await _race_or_wait_for(
                    self._primary.generate(context, deadline),
                    timeout=primary_budget,
                    cancel_token=_curr_cancel_token(),
                )
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

        _pre_sem_remaining = self._remaining_seconds(deadline)
        _sem_t0 = time.monotonic()
        _phase_hint = getattr(getattr(context, "phase", None), "name", "?")

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

        try:
            async with self._fallback_sem:
                _sem_wait_s = time.monotonic() - _sem_t0
                _parent_remaining = self._remaining_seconds(deadline)

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
                _budget_target = max(_parent_remaining, _FALLBACK_MIN_GUARANTEED_S)
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
                        "sem_wait=%.1fs)",
                        remaining, _parent_remaining,
                        _FALLBACK_MIN_GUARANTEED_S, _max_cap,
                        _sem_wait_s,
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
                        return _fb_result
                    except _OperationCancelledError:
                        # W3(7) cooperative cancel — operator/watchdog/signal.
                        # NEVER retry; honor the cancel immediately.
                        raise
                    except (Exception, asyncio.CancelledError) as inner_exc:
                        # Pre-instrumented (e.g. fallback_budget_starved
                        # from a different code path) → propagate as-is.
                        if hasattr(inner_exc, "exhaustion_report"):
                            raise
                        _last_inner_exc = inner_exc
                        _inner_mode = (
                            FailbackStateMachine.classify_exception(inner_exc)
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
        # tier1 reserve, Tier 3 cap). Added 2026-04-24 after F1 Slice 4 S4
        # (bt-2026-04-24-213248) proved the previous patch (inside
        # _call_primary) was inert for the DW-is-Tier0-AND-Primary
        # configuration — this code path is where DW actually gets its
        # 90s max_wait in that configuration.
        budget = min(
            total_s * effective_fraction,
            max_wait,
            total_s - tier1_reserve,
            _TIER3_REFLEX_HARD_CAP_S,
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
    def _compute_primary_budget(total_s: float) -> float:
        """Deterministic Tier 1 primary budget with fallback reserve + Tier 3 cap.

        Invariants (enforced via ``min()`` — strictest wins):
          - primary_budget <= total_s * _PRIMARY_BUDGET_FRACTION
          - total_s - primary_budget >= _FALLBACK_MIN_RESERVE_S (when possible)
          - **primary_budget <= _PRIMARY_MAX_TIMEOUT_S** (Tier 3 hard cap,
            Manifesto §5 — prevents a stalled primary from consuming the
            whole session budget and starving the Claude fallback)

        Tier 3 cap added 2026-04-24 after F1 Slice 4 S3 (bt-2026-04-24-204029)
        exposed a 153s DW primary hold that exhausted the session before
        Claude fallback could produce a candidate.
        """
        if total_s <= 0:
            return 0.0
        fb_reserve = min(_FALLBACK_MIN_RESERVE_S, total_s * 0.35)
        budget = min(
            total_s * _PRIMARY_BUDGET_FRACTION,
            total_s - fb_reserve,
            _PRIMARY_MAX_TIMEOUT_S,
        )
        return max(budget, 0.0)
