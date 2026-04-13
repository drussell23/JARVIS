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
from typing import Any, Dict, NoReturn, Optional, Protocol, runtime_checkable

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
_FALLBACK_MIN_RESERVE_S = float(os.environ.get("OUROBOROS_FALLBACK_MIN_RESERVE_S", "20"))

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
            if (
                self._exhaustion_watcher is not None
                and "all_providers_exhausted" in str(exc)
            ):
                try:
                    await self._exhaustion_watcher.record_exhaustion(
                        reason=str(exc),
                    )
                except Exception:
                    logger.debug(
                        "[CandidateGenerator] exhaustion_watcher "
                        "record_exhaustion failed",
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

        if _provider_route == "immediate":
            return await self._generate_immediate(context, deadline)
        if _provider_route == "background":
            return await self._generate_background(context, deadline)
        if _provider_route == "speculative":
            return await self._generate_speculative(context, deadline)
        # "complex" and "standard" both use the full DW→Claude cascade,
        # but "complex" gets more DW budget via route_budget_profile.
        # Fall through to unified cascade below.

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
        """BACKGROUND route: DW only. No Claude fallback.

        For low-urgency background sensors: opportunity mining,
        doc staleness, TODO scanning, backlog items.

        Cost: ~$0.002/op (DW batch only)
        Latency: relaxed (no deadline pressure)

        Raises RuntimeError("background_dw_*") on failure — orchestrator
        should re-queue or accept failure, NOT cascade to Claude.

        Phase 3 Scope α (``JARVIS_JPRIME_PRIMACY``): when enabled and a
        PrimeProvider handle is wired, :meth:`_try_jprime_primacy` is
        consulted first. Sem saturation or any failure falls through to
        the DW-only path below unchanged — Claude is still never
        consulted on this route.
        """
        _urgency = getattr(context, "signal_urgency", "?")
        _source = getattr(context, "signal_source", "?")
        remaining = self._remaining_seconds(deadline)

        logger.info(
            "[CandidateGenerator] BACKGROUND route: DW only, no Claude "
            "(urgency=%s, source=%s) [%.1fs budget]",
            _urgency, _source, remaining,
        )

        # Phase 3 Scope α — J-Prime primacy pre-check. Returns
        # ``None`` when the flag is off, no handle is wired, the sem is
        # saturated, or the J-Prime call failed. On ``None``, drop into
        # the existing DW-only path below.
        _primacy_result = await self._try_jprime_primacy(
            context, deadline, route_label="BACKGROUND",
        )
        if _primacy_result is not None:
            return _primacy_result

        if self._tier0 is None or not getattr(self._tier0, "is_available", False):
            raise RuntimeError(
                "background_dw_unavailable:tier0_not_configured"
            )

        # DW gets the full budget — no Claude reserve needed.
        # Use RT SSE if available, else batch.
        if getattr(self._tier0, "_realtime_enabled", False):
            try:
                result = await asyncio.wait_for(
                    self._tier0.generate(context, deadline),
                    timeout=min(remaining, 180.0),
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
                raise RuntimeError("background_dw_empty_result")
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"background_dw_timeout:{remaining:.0f}s"
                )
            except RuntimeError:
                raise  # Re-raise our own errors
            except Exception as exc:
                raise RuntimeError(
                    f"background_dw_error:{type(exc).__name__}:{exc}"
                ) from exc
        else:
            # Legacy batch path
            try:
                pending = await self._tier0.submit_batch(context)
                if pending is None:
                    raise RuntimeError("background_dw_batch_submit_failed")
                result = await asyncio.wait_for(
                    self._tier0.poll_and_retrieve(pending, context),
                    timeout=min(remaining, 180.0),
                )
                if result is not None and len(result.candidates) > 0:
                    logger.info(
                        "[CandidateGenerator] BACKGROUND batch: DW produced %d "
                        "candidates",
                        len(result.candidates),
                    )
                    return result
                raise RuntimeError("background_dw_batch_empty")
            except asyncio.TimeoutError:
                raise RuntimeError("background_dw_batch_timeout")
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"background_dw_batch_error:{type(exc).__name__}"
                ) from exc

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
                    return await asyncio.wait_for(
                        self._primary.plan(prompt, deadline),
                        timeout=remaining,
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
            remaining = min(_budget_target, self._FALLBACK_MAX_TIMEOUT_S)
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
        async with self._primary_sem:
            remaining = self._remaining_seconds(deadline)
            primary_budget = self._compute_primary_budget(remaining)
            logger.debug(
                "[CandidateGenerator] Primary budget: %.1fs of %.1fs remaining "
                "(fallback reserve: %.1fs)",
                primary_budget, remaining, remaining - primary_budget,
            )
            return await asyncio.wait_for(
                self._primary.generate(context, deadline),
                timeout=primary_budget,
            )

    # Hard ceiling for fallback provider — fail fast when unreachable
    # rather than burning the entire pipeline budget (Manifesto §6: Iron Gate).
    # Raised from 60s to 120s after bt-2026-04-11-085020 diagnosed tool_round
    # full_content patches legitimately needing 60-90s of stream time. IMMEDIATE
    # route also funnels through this cap, and a 60s cap was cutting mid-stream
    # healthy generation (23KB received at 365 bytes/s — normal Claude rate).
    _FALLBACK_MAX_TIMEOUT_S: float = 120.0

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
        _pre_sem_remaining = self._remaining_seconds(deadline)
        _sem_t0 = time.monotonic()
        logger.debug(
            "[CandidateGenerator] Fallback sem acquire: slots_free=%d/%d "
            "remaining=%.1fs route=%s op=%s",
            self._fallback_sem._value, self._fallback_concurrency,
            _pre_sem_remaining,
            getattr(context, "provider_route", "?"),
            getattr(context, "op_id", "?")[:16],
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
                # outer wait_for is the absolute Iron Gate.
                _budget_target = max(_parent_remaining, _FALLBACK_MIN_GUARANTEED_S)
                remaining = min(_budget_target, self._FALLBACK_MAX_TIMEOUT_S)
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
                        _FALLBACK_MIN_GUARANTEED_S, self._FALLBACK_MAX_TIMEOUT_S,
                        _sem_wait_s,
                    )
                    deadline = datetime.now(tz=timezone.utc) + timedelta(
                        seconds=remaining,
                    )
                else:
                    logger.info(
                        "[CandidateGenerator] Fallback: budget=%.1fs "
                        "(cap=%.0fs, sem_wait=%.1fs)",
                        remaining, self._FALLBACK_MAX_TIMEOUT_S, _sem_wait_s,
                    )

                return await asyncio.wait_for(
                    self._fallback.generate(context, deadline),
                    timeout=remaining,
                )
        except (Exception, asyncio.CancelledError) as exc:
            # If the exception is already instrumented (e.g. the inner
            # ``fallback_budget_starved`` raise), re-raise as-is so we
            # preserve the more-specific cause and don't double-count
            # the exhaustion event counter.
            if hasattr(exc, "exhaustion_report"):
                raise
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
        budget = min(
            total_s * effective_fraction,
            max_wait,
            total_s - tier1_reserve,
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
        """Deterministic Tier 1 primary budget with fallback reserve.

        Invariants:
          - primary_budget <= total_s * _PRIMARY_BUDGET_FRACTION
          - total_s - primary_budget >= _FALLBACK_MIN_RESERVE_S (when possible)
        """
        if total_s <= 0:
            return 0.0
        fb_reserve = min(_FALLBACK_MIN_RESERVE_S, total_s * 0.35)
        budget = min(
            total_s * _PRIMARY_BUDGET_FRACTION,
            total_s - fb_reserve,
        )
        return max(budget, 0.0)
