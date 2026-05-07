"""§37 Tier 2 #13 Slice 1 — Per-tool confidence-band-crossing observer.

Closes the highest-leverage Antivenom extension identified in the
2026-05-07 brutal review: per-tool-call confidence is structurally
unobservable today. Provider-level confidence (DW logprobs) is
already captured by ``verification/confidence_capture.py`` (§26.5.1
Slice 1, default-true), but no per-tool-call layer exists. This
module closes that gap.

**Why per-tool, not just per-op**: Move 9 single-roll Quine-class
hallucination is the residual structural-defense gap (§35 Move 9
🟡 PARTIAL). When the model crafts a plausible-but-vacuous test
pattern that converges across K-way Quorum rolls, the per-op
provider summary may stay above the model-confidence floor while
*individual tool calls* in the chain were low-confidence guesses.
Per-tool confidence catches converged-vacuity at the tool layer
BEFORE the K-way signature gate fires (defense in depth).

**Composition (operator binding 2026-05-07)**:

  * **Composes** ``confidence_capture.compute_summary`` — pure
    function over the bounded ring already populated by DW
    provider; NO parallel logprob extraction, NO new provider
    call.
  * **Mirrors** ``cost_warning_observer.py`` (§37 Slice 5)
    canonical 5-band ladder shape verbatim — same
    ``record() -> Optional[BandCrossing]`` chatter-suppressed
    state machine; same singleton accessor; same
    ``register_shipped_invariants()`` AST-pin shape.
  * **Reuses** the canonical ``StreamEventBroker`` via
    ``get_default_broker()`` — single SSE pipeline, no parallel
    queue.
  * **Stays substrate-pure** — no orchestrator / iron_gate /
    providers / urgency_router imports. AST-pinned.

**Five-band ladder** (env-tunable thresholds; all clamped):

  * ``CERTAIN``  — confidence ≥ certain threshold (default 90%)
  * ``HIGH``     — high ≤ confidence < certain (default 70%-90%)
  * ``MEDIUM``   — medium ≤ confidence < high (default 50%-70%)
  * ``LOW``      — low ≤ confidence < medium (default 30%-50%)
  * ``UNKNOWN``  — confidence < low threshold OR no signal
    available (Claude provider has no logprob path; defensive
    fallback when DW logprobs absent on a given tool call)

Note that the band semantics are **inverted from CostBand**:
CERTAIN is "good" (operator wants to see this), UNKNOWN is "bad"
(low-confidence call — risk-tier should clamp upward). This
inversion is intentional and AST-pinned via taxonomy regression.

**Architectural locks**:

  * **Single pipeline** — band classification is pure; SSE goes
    via canonical broker. AST-pinned.
  * **Chatter-suppression structural** — ``record()`` returns
    ``None`` when the band is unchanged. Per-stream state.
  * **Closed taxonomy** — ``ToolConfidenceBand`` is 5-value;
    additions require explicit pin update.
  * **First-observation discipline** — first-tick at CERTAIN/HIGH
    is silent (no spurious "OK→OK" emission); first-tick at
    MEDIUM/LOW/UNKNOWN emits immediately so operators see
    context.
  * **Authority asymmetry** — substrate purity AST-pinned.
  * **NEVER raises** — every code path defensive.

**Master flag** ``JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED``
default-FALSE per §33.1 graduation contract pattern. The
observer's *internal state* stays alive flag-off (callers can
record observations); SSE publish is gated by master flag at the
producer site. Slice 4 ships the graduation contract harness.
"""
from __future__ import annotations

import contextvars
import enum
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


logger = logging.getLogger(
    "Ouroboros.ToolConfidenceWarningObserver",
)


TOOL_CONFIDENCE_OBSERVER_SCHEMA_VERSION: str = (
    "tool_confidence_observer.1"
)


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value confidence band
# ---------------------------------------------------------------------------


class ToolConfidenceBand(str, enum.Enum):
    """Per-tool-call confidence band ladder. AST-pinned closed
    taxonomy.

    Inverted from CostBand semantics: CERTAIN is the "good" pole,
    UNKNOWN is the "bad" pole. The risk-tier floor consumer
    (Slice 3) clamps tier upward when band ≤ MEDIUM.
    """

    CERTAIN = "certain"
    """Confidence ≥ certain threshold (default ≥0.90). Operator
    can trust this call structurally."""

    HIGH = "high"
    """High confidence (default 0.70–0.90). Default-trusted
    band."""

    MEDIUM = "medium"
    """Hedged confidence (default 0.50–0.70). Operator should
    glance; risk-tier consumer may clamp upward."""

    LOW = "low"
    """Low confidence (default 0.30–0.50). Risk-tier consumer
    SHOULD clamp upward (NOTIFY_APPLY floor)."""

    UNKNOWN = "unknown"
    """No confidence signal OR signal below low threshold.
    Includes the Claude provider (no logprob API) — defensive
    fallback when the capturer is empty for this call.
    Risk-tier consumer SHOULD clamp upward."""


# ---------------------------------------------------------------------------
# Frozen artifact — band crossing event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceBandCrossing:
    """Recorded transition between two ToolConfidenceBand values.
    Frozen for safe propagation. Emitted only when ``record()``
    observes a band CHANGE — same-band re-evaluations return
    ``None`` (chatter-suppression structural).

    Adopts §33.5 Versioned-Artifact-Contract via
    ``schema_version`` + symmetric ``to_dict``."""

    stream_key: str
    """Logical confidence-stream identifier. Default
    ``"<op_id>::<tool_name>"`` so per-op + per-tool streams stay
    independent (different tool calls within an op DON'T mask
    each other's transitions)."""

    op_id: str
    """Owning op for this confidence sample. ``""`` for session-
    level."""

    tool_name: str
    """The tool whose confidence is being observed
    (read_file / search_code / edit_file / bash / etc.)."""

    from_band: ToolConfidenceBand
    to_band: ToolConfidenceBand
    confidence: float
    sample_size: int
    """Number of underlying tokens that contributed to this
    confidence sample (length of the ConfidenceTrace window).
    Operator interprets confidence + sample_size jointly —
    confidence=0.95 over 1 token is weaker evidence than
    confidence=0.85 over 50 tokens."""

    schema_version: str = field(
        default=TOOL_CONFIDENCE_OBSERVER_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stream_key": str(self.stream_key),
            "op_id": str(self.op_id),
            "tool_name": str(self.tool_name),
            "from_band": self.from_band.value,
            "to_band": self.to_band.value,
            "confidence": float(self.confidence),
            "sample_size": int(self.sample_size),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Env-tunable thresholds (all clamped, sane defaults)
# ---------------------------------------------------------------------------


def _clamp_pct(raw: str, default: int, low: int, high: int) -> int:
    """Parse percentage env-var value. Clamps to [low, high].
    Parse failure → default."""
    try:
        n = int(raw) if raw.strip() else default
    except (TypeError, ValueError):
        return default
    if n < low:
        return low
    if n > high:
        return high
    return n


def certain_threshold_pct() -> int:
    """``JARVIS_TOOL_CONFIDENCE_BAND_CERTAIN_PCT`` — top-band
    entry. Default 90; clamped [1, 99]. Bands above this are
    CERTAIN; below are HIGH or lower."""
    return _clamp_pct(
        os.environ.get(
            "JARVIS_TOOL_CONFIDENCE_BAND_CERTAIN_PCT", "",
        ),
        default=90, low=1, high=99,
    )


def high_threshold_pct() -> int:
    """``JARVIS_TOOL_CONFIDENCE_BAND_HIGH_PCT`` — HIGH-band
    entry. Default 70; clamped [1, 99]."""
    return _clamp_pct(
        os.environ.get(
            "JARVIS_TOOL_CONFIDENCE_BAND_HIGH_PCT", "",
        ),
        default=70, low=1, high=99,
    )


def medium_threshold_pct() -> int:
    """``JARVIS_TOOL_CONFIDENCE_BAND_MEDIUM_PCT`` — MEDIUM-band
    entry. Default 50; clamped [1, 99]."""
    return _clamp_pct(
        os.environ.get(
            "JARVIS_TOOL_CONFIDENCE_BAND_MEDIUM_PCT", "",
        ),
        default=50, low=1, high=99,
    )


def low_threshold_pct() -> int:
    """``JARVIS_TOOL_CONFIDENCE_BAND_LOW_PCT`` — LOW-band entry.
    Default 30; clamped [1, 99]. Below this is UNKNOWN."""
    return _clamp_pct(
        os.environ.get(
            "JARVIS_TOOL_CONFIDENCE_BAND_LOW_PCT", "",
        ),
        default=30, low=1, high=99,
    )


def master_enabled() -> bool:
    """``JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED`` — master
    switch for SSE publication + risk-tier-floor consumer wiring.
    Default-FALSE per §33.1 graduation contract pattern. Slice 4
    ships the graduation harness; operator flips to true after
    empirical confidence-distribution baseline accumulates.

    The observer's internal state remains coherent regardless —
    callers can record observations; only SSE publication +
    downstream consumers gate on this flag.
    """
    raw = os.environ.get(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Pure-function band classifier
# ---------------------------------------------------------------------------


def classify_band(
    confidence: float,
    *,
    certain_pct: Optional[int] = None,
    high_pct: Optional[int] = None,
    medium_pct: Optional[int] = None,
    low_pct: Optional[int] = None,
) -> ToolConfidenceBand:
    """Classify a confidence value into a ToolConfidenceBand.
    Pure function; NEVER raises.

    Threshold args optional — when ``None``, reads env via the
    public helpers. Caller-injection enables testing band
    boundaries without env mocking.

    Defensive against malformed input: ``None`` / NaN / non-numeric
    / out-of-range all return ``UNKNOWN`` (the safe band — risk-
    tier consumer treats UNKNOWN identically to LOW).

    Threshold ordering enforced at runtime: if env returns
    inconsistent values (e.g., low >= medium), the classifier
    falls through to the highest applicable band based on the
    confidence value alone (defensive — no exception).
    """
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return ToolConfidenceBand.UNKNOWN
    if not (c == c):  # NaN check
        return ToolConfidenceBand.UNKNOWN
    if c < 0.0 or c > 1.0:
        # Out-of-range — defensive fallback rather than crash.
        return ToolConfidenceBand.UNKNOWN
    certain = (
        certain_pct if certain_pct is not None
        else certain_threshold_pct()
    )
    high = (
        high_pct if high_pct is not None
        else high_threshold_pct()
    )
    medium = (
        medium_pct if medium_pct is not None
        else medium_threshold_pct()
    )
    low = (
        low_pct if low_pct is not None
        else low_threshold_pct()
    )
    certain_f = certain / 100.0
    high_f = high / 100.0
    medium_f = medium / 100.0
    low_f = low / 100.0
    if c >= certain_f:
        return ToolConfidenceBand.CERTAIN
    if c >= high_f:
        return ToolConfidenceBand.HIGH
    if c >= medium_f:
        return ToolConfidenceBand.MEDIUM
    if c >= low_f:
        return ToolConfidenceBand.LOW
    return ToolConfidenceBand.UNKNOWN


# ---------------------------------------------------------------------------
# Helper: cross-band severity ordering
# ---------------------------------------------------------------------------


_BAND_SEVERITY: Dict[ToolConfidenceBand, int] = {
    ToolConfidenceBand.CERTAIN: 0,
    ToolConfidenceBand.HIGH: 1,
    ToolConfidenceBand.MEDIUM: 2,
    ToolConfidenceBand.LOW: 3,
    ToolConfidenceBand.UNKNOWN: 4,
}


def band_severity(band: ToolConfidenceBand) -> int:
    """Severity rank — higher = riskier. CERTAIN=0, UNKNOWN=4.
    Pure helper for risk-tier consumers (Slice 3) deciding
    whether to clamp tier upward."""
    return _BAND_SEVERITY.get(band, 4)


# ---------------------------------------------------------------------------
# ToolConfidenceObserver — stateful band-crossing detector
# ---------------------------------------------------------------------------


class ToolConfidenceObserver:
    """Observes per-tool-call confidence samples; emits
    :class:`ConfidenceBandCrossing` only on band transitions
    (chatter-suppression structural).

    Multiple stream-keys supported independently — default
    convention: ``"<op_id>::<tool_name>"`` so per-op + per-tool
    streams stay independent. A single op invoking ``read_file``
    + ``search_code`` produces TWO streams; the same tool called
    twice in a row uses the same stream (so a same-band re-call
    structurally suppresses). Operators can override via
    ``stream_key`` arg.

    Single-writer-per-stream scenario: each tool call invokes
    ``record()`` exactly once on a unique-by-construction stream
    key. No locking required at the observer (single-thread per
    Venom round).

    NEVER raises.
    """

    def __init__(self) -> None:
        # stream_key -> last observed band.
        self._last_band_per_stream: Dict[
            str, ToolConfidenceBand,
        ] = {}

    def record(
        self,
        *,
        confidence: float,
        op_id: str = "",
        tool_name: str = "",
        sample_size: int = 0,
        stream_key: Optional[str] = None,
        publish_sse: bool = True,
    ) -> Optional[ConfidenceBandCrossing]:
        """Sample a confidence observation. Returns a
        :class:`ConfidenceBandCrossing` when the band CHANGED
        from the last observation on this stream; returns
        ``None`` when the band stayed the same (chatter-
        suppression structural).

        Args:
            confidence: confidence value in [0.0, 1.0]. Out-of-
                range → UNKNOWN band (defensive).
            op_id: owning op id. ``""`` for session-level.
            tool_name: tool whose confidence is observed.
            sample_size: tokens that contributed to the
                confidence sample (length of the ConfidenceTrace
                window).
            stream_key: logical stream identifier. ``None``
                derives ``"<op_id>::<tool_name>"`` (recommended).
                Operators can override for cross-call streams.
            publish_sse: emit SSE event on band crossing via
                canonical broker. Default ``True``. Set ``False``
                in tests / when SSE is unwanted. SSE publish is
                ALSO gated by the master flag at the producer
                site (defense in depth).

        Defensive: any error in classification or SSE emit is
        swallowed; observer state remains coherent. NEVER raises.
        """
        try:
            new_band = classify_band(confidence)
        except Exception:  # noqa: BLE001 — defensive
            return None
        # Derive stream key.
        if stream_key is None:
            stream_key = f"{op_id}::{tool_name}"
        prev_band = self._last_band_per_stream.get(stream_key)
        if prev_band == new_band:
            # Same band — chatter-suppression structural.
            return None
        # First-observation discipline: first-tick at CERTAIN or
        # HIGH is silent (the safe pole, fresh op observation
        # shouldn't fire just because we observed for the first
        # time). First-tick at MEDIUM/LOW/UNKNOWN emits
        # immediately so operators see context. This makes
        # "first-tick at safe pole" structurally invisible (the
        # right behavior for fresh ops).
        if prev_band is None and new_band in (
            ToolConfidenceBand.CERTAIN,
            ToolConfidenceBand.HIGH,
        ):
            self._last_band_per_stream[stream_key] = new_band
            return None
        # Band crossed — update state + emit.
        self._last_band_per_stream[stream_key] = new_band
        from_band = prev_band or ToolConfidenceBand.CERTAIN
        try:
            confidence_clean = float(confidence)
        except (TypeError, ValueError):
            confidence_clean = 0.0
        if not (confidence_clean == confidence_clean):  # NaN
            confidence_clean = 0.0
        try:
            sample_size_clean = int(sample_size)
        except (TypeError, ValueError):
            sample_size_clean = 0
        if sample_size_clean < 0:
            sample_size_clean = 0
        crossing = ConfidenceBandCrossing(
            stream_key=str(stream_key),
            op_id=str(op_id),
            tool_name=str(tool_name),
            from_band=from_band,
            to_band=new_band,
            confidence=confidence_clean,
            sample_size=sample_size_clean,
        )
        # Operator-facing log line — band crossing is always
        # interesting enough to surface in the session log,
        # regardless of SSE subscriber state OR master flag.
        # Local log != SSE broadcast.
        logger.info(
            "[ToolConfidenceWarningObserver] band crossed "
            "%s -> %s (op=%s tool=%s conf=%.3f n=%d)",
            from_band.value, new_band.value,
            op_id, tool_name,
            confidence_clean, sample_size_clean,
        )
        if publish_sse and master_enabled():
            self._publish_to_broker(crossing)
        return crossing

    def reset(self, stream_key: Optional[str] = None) -> None:
        """Clear last-band state. ``stream_key=None`` clears all
        streams (test isolation); a specific key clears only
        that stream."""
        if stream_key is None:
            self._last_band_per_stream.clear()
        else:
            self._last_band_per_stream.pop(stream_key, None)

    def last_band(
        self, stream_key: str,
    ) -> Optional[ToolConfidenceBand]:
        """Return the last-observed band on this stream, or
        ``None`` if no observation has been recorded yet."""
        return self._last_band_per_stream.get(stream_key)

    def stream_count(self) -> int:
        """Operator visibility — number of distinct streams the
        observer is currently tracking. Bounded only by the
        number of (op_id, tool_name) pairs the session sees;
        callers that worry about unbounded growth should call
        :meth:`reset` between sessions."""
        return len(self._last_band_per_stream)

    @staticmethod
    def _publish_to_broker(
        crossing: ConfidenceBandCrossing,
    ) -> None:
        """Emit canonical SSE event. Composes the existing
        broker (Slice 2 territory of §37 Tier 1). Defensive:
        any error is swallowed — the band-crossing is already
        logged."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                EVENT_TYPE_TOOL_CONFIDENCE_BAND_CROSSED,
                get_default_broker,
            )
            broker = get_default_broker()
            if broker is None:
                return
            broker.publish(
                event_type=(
                    EVENT_TYPE_TOOL_CONFIDENCE_BAND_CROSSED
                ),
                op_id=crossing.op_id,
                payload=crossing.to_dict(),
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ToolConfidenceWarningObserver] SSE publish "
                "failed", exc_info=True,
            )


# ---------------------------------------------------------------------------
# Slice 2 — Per-tool confidence extraction substrate
# ---------------------------------------------------------------------------
#
# Slice 2 (2026-05-07) wires per-tool-call confidence observation by
# composing the canonical capture substrate
# (``verification/confidence_capture.py``). Zero parallel logprob math.
#
# **ContextVar bridge**: the DW provider creates a ConfidenceCapturer
# per GENERATE round and parks it in ``ctx.artifacts``. PolicyContext
# (passed to ``ToolExecutor.execute_async``) does NOT carry the
# artifacts dict, so we propagate the capturer via async-safe
# ContextVar — the same pattern used by ``plan_exploit_active_var``
# and ``current_curiosity_budget_var``. The DW provider sets the var
# at round start; tool_executor reads it; the var resets on round exit.
# ContextVar inherits across asyncio.Task creation, so tool calls
# within the round see the same capturer.
#
# **Projection**: ``ConfidenceSummary.mean_top1_logprob`` is a log-
# probability in (-inf, 0]. We project to a [0, 1] confidence value
# via ``exp(mean_top1_logprob)``: a logprob of -0.1 (very confident)
# → 0.905, a logprob of -2.3 (uncertain) → 0.100. Defensive against
# missing fields / NaN — projection returns 0.0 (UNKNOWN band)
# rather than raising. The ``sample_size`` carries the trace token
# count so risk-tier consumers (Slice 3) can weight the signal by
# evidence depth.
#
# **Cumulative semantic**: each freeze() returns the cumulative trace
# for the GENERATE round so far. A second tool call within the same
# round observes confidence projected over MORE tokens than the
# first. This is acceptable v1 semantics (per-tool ≈ cumulative-as-
# of-tool-N); per-tool-exclusive would require checkpoint deltas
# (deferred enhancement).


_ACTIVE_CAPTURER_VAR: "contextvars.ContextVar[Optional[Any]]" = (
    contextvars.ContextVar(
        "tool_confidence_active_capturer", default=None,
    )
)
"""Async-safe pointer to the ConfidenceCapturer for the current
GENERATE round. ``Optional[Any]`` rather than ``Optional[
ConfidenceCapturer]`` to keep the import lazy (avoid eager pull of
the verification subsystem at module-load time)."""


def set_active_capturer(
    capturer: Optional[Any],
) -> "contextvars.Token[Optional[Any]]":
    """Stamp the active ConfidenceCapturer. Returns a Token the
    caller MUST pass to :func:`reset_active_capturer` to restore
    the previous value.

    Convention: providers (DW today; future: J-Prime) call this
    when starting a GENERATE round and reset it in the matching
    ``finally`` block. Idempotent — passing the same capturer
    twice is harmless.
    """
    return _ACTIVE_CAPTURER_VAR.set(capturer)


def reset_active_capturer(
    token: "contextvars.Token[Optional[Any]]",
) -> None:
    """Restore the previous capturer pointer using the Token
    returned by :func:`set_active_capturer`. Defensive: invalid
    Token errors are swallowed so a stale reset doesn't crash
    the GENERATE finally."""
    try:
        _ACTIVE_CAPTURER_VAR.reset(token)
    except (ValueError, LookupError, TypeError):
        # Token was created in a different context OR caller
        # passed a non-Token sentinel — defensive silent
        # recovery rather than crash the finally. NEVER raises.
        logger.debug(
            "[ToolConfidenceWarningObserver] reset_active_"
            "capturer received stale/invalid token (non-fatal)",
        )


def get_active_capturer() -> Optional[Any]:
    """Read the active ConfidenceCapturer (if any). Returns
    ``None`` when no provider has stamped one for the current
    async context (e.g., Claude provider rounds, or pre-GENERATE
    paths). NEVER raises."""
    try:
        return _ACTIVE_CAPTURER_VAR.get()
    except LookupError:
        return None


# ---------------------------------------------------------------------------
# Frozen artifact — extracted confidence signal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceSignal:
    """Single per-tool-call confidence observation. Composes the
    output of :func:`verification.confidence_capture.compute_summary`
    via :func:`project_summary_to_confidence`. Adopts §33.5
    versioned-artifact contract (``schema_version`` + symmetric
    ``to_dict``)."""

    confidence: float
    """Projected confidence in [0.0, 1.0]. ``0.0`` when the
    capture trace was empty / projection failed (UNKNOWN band
    fallback)."""

    sample_size: int
    """Tokens that contributed to the underlying confidence
    trace. Risk-tier consumers (Slice 3) weight the signal by
    sample_size — a high confidence over 1 token is weaker
    evidence than a high confidence over 50 tokens."""

    schema_version: str = field(
        default=TOOL_CONFIDENCE_OBSERVER_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "confidence": float(self.confidence),
            "sample_size": int(self.sample_size),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Pure-function projection — ConfidenceSummary → [0, 1] scalar
# ---------------------------------------------------------------------------


def project_summary_to_confidence(
    summary: Optional[Any],
) -> ConfidenceSignal:
    """Project a ``ConfidenceSummary`` into a single [0, 1]
    confidence scalar + sample_size.

    Mapping: ``exp(mean_top1_logprob)``. Log-probability domain
    is (-inf, 0]; the exp transform gives a probability in (0, 1].

    Defensive: ``None`` summary, missing fields, NaN, or out-of-
    range values all return ``ConfidenceSignal(0.0, 0)`` (UNKNOWN
    band fallback). NEVER raises.

    Pure function — no I/O, no side effects, no env reads. Pin-
    eligible for AST regression on composition discipline (no
    parallel logprob math allowed in callers)."""
    if summary is None:
        return ConfidenceSignal(confidence=0.0, sample_size=0)
    try:
        mean_logprob = getattr(
            summary, "mean_top1_logprob", None,
        )
        token_count = getattr(summary, "token_count", 0)
    except Exception:  # noqa: BLE001 — defensive
        return ConfidenceSignal(confidence=0.0, sample_size=0)
    # token_count first — bound the sample_size regardless of
    # logprob shape.
    try:
        size = int(token_count) if token_count is not None else 0
    except (TypeError, ValueError):
        size = 0
    if size < 0:
        size = 0
    # mean_logprob may be None when capturer saw zero tokens.
    if mean_logprob is None:
        return ConfidenceSignal(confidence=0.0, sample_size=size)
    try:
        lp = float(mean_logprob)
    except (TypeError, ValueError):
        return ConfidenceSignal(confidence=0.0, sample_size=size)
    if not (lp == lp):  # NaN
        return ConfidenceSignal(confidence=0.0, sample_size=size)
    # Logprob domain is (-inf, 0]. Defensive cap at 0 (which maps
    # to confidence=1.0) — values above 0 are spec violations
    # but we treat them as fully confident rather than crash.
    if lp > 0.0:
        lp = 0.0
    # Avoid extremely small probabilities crashing math.exp on
    # subnormal floats — clamp at -50 (exp(-50) ≈ 1.9e-22, still
    # representable; below that is structurally unknown).
    if lp < -50.0:
        return ConfidenceSignal(confidence=0.0, sample_size=size)
    try:
        confidence = math.exp(lp)
    except (OverflowError, ValueError):
        return ConfidenceSignal(confidence=0.0, sample_size=size)
    # Clamp to [0, 1] defensively (math.exp(0) is exactly 1.0
    # but float arithmetic can drift).
    if confidence < 0.0:
        confidence = 0.0
    elif confidence > 1.0:
        confidence = 1.0
    return ConfidenceSignal(
        confidence=confidence, sample_size=size,
    )


# ---------------------------------------------------------------------------
# extract_confidence_signal — composes capture substrate end-to-end
# ---------------------------------------------------------------------------


def extract_confidence_signal_from_active_capturer() -> (
    Optional[ConfidenceSignal]
):
    """Read the active capturer (ContextVar) and project its
    current trace into a :class:`ConfidenceSignal`. Returns
    ``None`` when no capturer is active (e.g., Claude provider
    rounds, pre-GENERATE paths) — caller treats absence as "no
    signal" and skips the observation rather than recording
    UNKNOWN band noise.

    Composition path (zero parallel math):

      1. ``get_active_capturer()`` → Optional[ConfidenceCapturer]
      2. ``capturer.freeze()`` → ConfidenceTrace (immutable)
      3. ``compute_summary(trace)`` → ConfidenceSummary
      4. :func:`project_summary_to_confidence` → ConfidenceSignal

    The lazy import of ``compute_summary`` keeps Slice 1 module
    importable without eagerly pulling the verification subsystem
    (test isolation discipline).

    Defensive: any error in the chain is swallowed; returns
    ``None``. NEVER raises.
    """
    capturer = get_active_capturer()
    if capturer is None:
        return None
    try:
        # Lazy import — keeps module-load cycle clean.
        from backend.core.ouroboros.governance.verification.confidence_capture import (  # noqa: E501
            compute_summary,
        )
        trace = capturer.freeze()
        summary = compute_summary(trace)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ToolConfidenceWarningObserver] active capturer "
            "extraction failed (non-fatal)", exc_info=True,
        )
        return None
    return project_summary_to_confidence(summary)


def observe_active_signal(
    *,
    op_id: str,
    tool_name: str,
    publish_sse: bool = True,
) -> Optional[ConfidenceBandCrossing]:
    """High-level convenience: extract from active capturer and
    feed the singleton observer in one call. Returns the band
    crossing (or ``None`` if the band is unchanged or no signal
    was available).

    Wired into ``tool_executor.execute_async`` at the per-tool-
    result return path (Slice 2 fire-points). Master-flag-gated
    at the SITE (caller checks :func:`master_enabled`) to avoid
    the freeze/compute_summary cost when off.

    NEVER raises.
    """
    try:
        signal = extract_confidence_signal_from_active_capturer()
    except Exception:  # noqa: BLE001 — defensive
        return None
    if signal is None:
        return None
    try:
        return get_default_observer().record(
            confidence=signal.confidence,
            op_id=op_id,
            tool_name=tool_name,
            sample_size=signal.sample_size,
            publish_sse=publish_sse,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ToolConfidenceWarningObserver] observe_active_"
            "signal record() failed (non-fatal)", exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Default-singleton accessor (matches §37 Slice 5 / Slice 8 pattern)
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVER: Optional[ToolConfidenceObserver] = None


def get_default_observer() -> ToolConfidenceObserver:
    """Return the process-wide default
    :class:`ToolConfidenceObserver` singleton. Created lazily on
    first access. Subsequent calls return the same instance.

    Use this from per-tool-call observation sites (Slice 2 wiring
    in tool_executor) and from any future per-op confidence
    aggregator."""
    global _DEFAULT_OBSERVER
    if _DEFAULT_OBSERVER is None:
        _DEFAULT_OBSERVER = ToolConfidenceObserver()
    return _DEFAULT_OBSERVER


def reset_default_observer_for_tests() -> None:
    """Test-only — production code never calls. Pinned via
    naming convention (``_for_tests`` suffix)."""
    global _DEFAULT_OBSERVER
    _DEFAULT_OBSERVER = None


# ---------------------------------------------------------------------------
# FlagRegistry seeds — auto-discovered via register_flags()
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered by :class:`FlagRegistry`. Seeds the 5
    knobs this module reads: master flag + 4 band thresholds.

    Defensive: if the registry's ``register`` interface differs,
    we silently skip rather than crashing module import."""
    try:
        registry.register(
            name="JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for §37 Tier 2 #13 per-tool "
                "confidence band SSE publication + risk-tier "
                "consumer wiring. Default-FALSE per §33.1 "
                "graduation contract; flips to true after "
                "Slice 4 graduation harness reports ready."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tool_confidence_warning_observer.py"
            ),
            example=(
                "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED=true"
            ),
        )
        registry.register(
            name="JARVIS_TOOL_CONFIDENCE_BAND_CERTAIN_PCT",
            type_="int",
            default="90",
            description=(
                "CERTAIN-band entry threshold (percent). Default "
                "90; clamped [1, 99]. Confidence ≥ this fraction "
                "is the safe pole."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tool_confidence_warning_observer.py"
            ),
            example=(
                "JARVIS_TOOL_CONFIDENCE_BAND_CERTAIN_PCT=92"
            ),
        )
        registry.register(
            name="JARVIS_TOOL_CONFIDENCE_BAND_HIGH_PCT",
            type_="int",
            default="70",
            description=(
                "HIGH-band entry threshold (percent). Default "
                "70; clamped [1, 99]."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tool_confidence_warning_observer.py"
            ),
            example="JARVIS_TOOL_CONFIDENCE_BAND_HIGH_PCT=75",
        )
        registry.register(
            name="JARVIS_TOOL_CONFIDENCE_BAND_MEDIUM_PCT",
            type_="int",
            default="50",
            description=(
                "MEDIUM-band entry threshold (percent). Default "
                "50; clamped [1, 99]. Below this is LOW or "
                "UNKNOWN."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tool_confidence_warning_observer.py"
            ),
            example="JARVIS_TOOL_CONFIDENCE_BAND_MEDIUM_PCT=55",
        )
        registry.register(
            name="JARVIS_TOOL_CONFIDENCE_BAND_LOW_PCT",
            type_="int",
            default="30",
            description=(
                "LOW-band entry threshold (percent). Default 30; "
                "clamped [1, 99]. Below this is UNKNOWN — risk-"
                "tier consumer SHOULD clamp tier upward."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "tool_confidence_warning_observer.py"
            ),
            example="JARVIS_TOOL_CONFIDENCE_BAND_LOW_PCT=25",
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ToolConfidenceWarningObserver] FlagRegistry "
            "seeding failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins (auto-discovered via register_shipped_invariants)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``tool_confidence_observer_band_taxonomy_5_values`` —
         ``ToolConfidenceBand`` is 5-value closed enum.
      2. ``tool_confidence_observer_chatter_suppression`` —
         ``record()`` returns ``None`` when band unchanged.
      3. ``tool_confidence_observer_authority_asymmetry`` —
         substrate purity (no orchestrator / iron_gate /
         providers / urgency_router / change_engine /
         semantic_guardian / candidate_generator imports).
      4. ``tool_confidence_observer_composes_canonical_broker``
         — emits via ``get_default_broker()`` only.
      5. ``tool_confidence_observer_master_flag_default_false`` —
         §33.1 graduation contract: producer flag stays default-
         FALSE until Slice 4 contract reports ready.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "tool_confidence_warning_observer.py"
    )

    def _validate_band_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "CERTAIN", "HIGH", "MEDIUM", "LOW", "UNKNOWN",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "ToolConfidenceBand":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    extra = seen - required
                    missing = required - seen
                    if extra:
                        violations.append(
                            f"ToolConfidenceBand has extra values "
                            f"{sorted(extra)} — taxonomy is "
                            f"closed; update pin if intentional"
                        )
                    if missing:
                        violations.append(
                            f"ToolConfidenceBand missing required "
                            f"values {sorted(missing)}"
                        )
        return tuple(violations)

    def _validate_chatter_suppression(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``record()`` MUST contain a same-band early-return
        check that returns None."""
        violations: list = []
        target_method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "record":
                    target_method = node
                    break
        if target_method is None:
            violations.append(
                "ToolConfidenceObserver.record() method missing"
            )
            return tuple(violations)
        has_same_band_early_return = False
        for sub in ast.walk(target_method):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            if not isinstance(test, ast.Compare):
                continue
            if not test.ops or not isinstance(
                test.ops[0], ast.Eq,
            ):
                continue
            for body_stmt in sub.body:
                if isinstance(body_stmt, ast.Return):
                    if (
                        isinstance(body_stmt.value, ast.Constant)
                        and body_stmt.value.value is None
                    ):
                        has_same_band_early_return = True
                        break
        if not has_same_band_early_return:
            violations.append(
                "record() MUST contain `if prev == new: "
                "return None` early-return for chatter-"
                "suppression — operator-binding structural "
                "discipline (§37 Slice 5 + Slice 8 + Tier 2 #13)"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"tool_confidence_warning_observer.py "
                            f"MUST NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_canonical_broker(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "StreamEventBroker"
                ):
                    violations.append(
                        "tool_confidence_warning_observer.py "
                        "MUST NOT construct StreamEventBroker "
                        "directly — compose get_default_broker()"
                    )
        return tuple(violations)

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``master_enabled()`` MUST return False on empty
        env-var string (§33.1 graduation contract). Verifies
        AST-structurally rather than bytes-pinning so refactors
        that preserve semantics don't trip the pin."""
        violations: list = []
        master_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    master_func = node
                    break
        if master_func is None:
            violations.append(
                "master_enabled() helper missing — required "
                "for §33.1 graduation contract pattern"
            )
            return tuple(violations)
        # AST shape: find an `if X == "": return False` (or
        # equivalent: `if X == "" or X is None: return False`)
        # — empty-string guard MUST return False.
        empty_guard_returns_false = False
        for sub in ast.walk(master_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            # Collect every Compare node anywhere in the test
            # (covers `X == ""` directly, `X == "" or ...`,
            # nested BoolOps, etc.).
            compares: list = []
            for sub_test in ast.walk(test):
                if isinstance(sub_test, ast.Compare):
                    compares.append(sub_test)
            compares_empty_str = False
            for cmp_node in compares:
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        compares_empty_str = True
                        break
                if compares_empty_str:
                    break
            if not compares_empty_str:
                continue
            # body must contain `return False`.
            for body_stmt in sub.body:
                if isinstance(body_stmt, ast.Return):
                    if (
                        isinstance(body_stmt.value, ast.Constant)
                        and body_stmt.value.value is False
                    ):
                        empty_guard_returns_false = True
                        break
            if empty_guard_returns_false:
                break
        if not empty_guard_returns_false:
            violations.append(
                "master_enabled() MUST return False on empty "
                "env-var string per §33.1 — required AST shape: "
                "`if <X> == \"\": return False`"
            )
        return tuple(violations)

    def _validate_slice2_composes_capture(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """§37 Tier 2 #13 Slice 2 — extraction MUST compose
        ``verification.confidence_capture.compute_summary``.
        Forbids parallel logprob math (e.g., reading
        ``capturer._tokens`` directly). Canonical projection
        path is single-call: lazy-import compute_summary +
        call freeze() + call compute_summary(trace) +
        project."""
        violations: list = []
        extract_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if (
                    node.name
                    == (
                        "extract_confidence_signal_"
                        "from_active_capturer"
                    )
                ):
                    extract_func = node
                    break
        if extract_func is None:
            violations.append(
                "extract_confidence_signal_from_active_"
                "capturer missing — Slice 2 wiring requires "
                "this composition entry-point"
            )
            return tuple(violations)
        has_lazy_import = False
        calls_compute_summary = False
        calls_freeze = False
        forbidden_dunder_access = False
        for sub in ast.walk(extract_func):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if (
                    "verification.confidence_capture" in module
                    and any(
                        n.name == "compute_summary"
                        for n in sub.names
                    )
                ):
                    has_lazy_import = True
            if isinstance(sub, ast.Call):
                func = sub.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "compute_summary"
                ):
                    calls_compute_summary = True
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "freeze"
                ):
                    calls_freeze = True
            if isinstance(sub, ast.Attribute):
                if sub.attr in (
                    "_tokens", "_ring", "_buffer",
                ):
                    forbidden_dunder_access = True
        if not has_lazy_import:
            violations.append(
                "extract_confidence_signal_from_active_"
                "capturer MUST lazy-import compute_summary "
                "from verification.confidence_capture "
                "(composition discipline)"
            )
        if not calls_compute_summary:
            violations.append(
                "extract_confidence_signal_from_active_"
                "capturer MUST call compute_summary(trace) "
                "— no parallel logprob math allowed"
            )
        if not calls_freeze:
            violations.append(
                "extract_confidence_signal_from_active_"
                "capturer MUST call capturer.freeze() to "
                "obtain the immutable trace before projection"
            )
        if forbidden_dunder_access:
            violations.append(
                "extract_confidence_signal_from_active_"
                "capturer MUST NOT access capturer._tokens / "
                "_ring / _buffer — composition discipline "
                "forbids parallel logprob math"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "tool_confidence_observer_"
                "band_taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 1 — ToolConfidenceBand is "
                "5-value closed enum (CERTAIN/HIGH/MEDIUM/LOW/"
                "UNKNOWN)."
            ),
            validate=_validate_band_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_confidence_observer_chatter_suppression"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 1 — record() emits "
                "ConfidenceBandCrossing ONLY on band change. "
                "Same-band early-return returns None."
            ),
            validate=_validate_chatter_suppression,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_confidence_observer_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 1 — substrate purity: no "
                "orchestrator / iron_gate / policy / providers "
                "/ urgency_router / change_engine / "
                "semantic_guardian / candidate_generator imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_confidence_observer_"
                "composes_canonical_broker"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 1 — single-pipeline "
                "guardrail: composes get_default_broker(); never "
                "constructs StreamEventBroker directly."
            ),
            validate=_validate_composes_canonical_broker,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_confidence_observer_"
                "master_flag_default_false"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 1 — §33.1 graduation "
                "contract: producer master flag "
                "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED stays "
                "default-FALSE until Slice 4 contract reports "
                "ready."
            ),
            validate=_validate_master_flag_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_confidence_observer_"
                "slice2_composes_confidence_capture"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #13 Slice 2 — extraction MUST "
                "compose verification.confidence_capture."
                "compute_summary (no parallel logprob math). "
                "extract_confidence_signal_from_active_capturer "
                "MUST lazy-import compute_summary AND call it "
                "exactly once on the frozen trace."
            ),
            validate=_validate_slice2_composes_capture,
        ),
    ]


__all__ = [
    "ConfidenceBandCrossing",
    "ConfidenceSignal",
    "TOOL_CONFIDENCE_OBSERVER_SCHEMA_VERSION",
    "ToolConfidenceBand",
    "ToolConfidenceObserver",
    "band_severity",
    "certain_threshold_pct",
    "classify_band",
    "extract_confidence_signal_from_active_capturer",
    "get_active_capturer",
    "get_default_observer",
    "high_threshold_pct",
    "low_threshold_pct",
    "master_enabled",
    "medium_threshold_pct",
    "observe_active_signal",
    "project_summary_to_confidence",
    "register_flags",
    "register_shipped_invariants",
    "reset_active_capturer",
    "reset_default_observer_for_tests",
    "set_active_capturer",
]
