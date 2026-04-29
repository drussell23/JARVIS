"""Priority 1 Slice 1 — Logprob capture primitive.

The signal-acquisition layer for Priority 1 (Confidence-Aware
Execution, PRD §26.5.1). Provider-side logprobs / top-k alternatives
exist in the streaming response but are discarded after parse;
this module captures them into a bounded structural artifact that
Slice 2 (ConfidenceMonitor) consumes.

Honesty contract — provider reality (knowledge cutoff Jan 2026):
  * **OpenAI-compatible APIs** (DoubleWord, etc.) expose per-token
    logprobs via ``logprobs=true`` + ``top_logprobs=K`` in request.
    Each SSE chunk's ``choices[0].logprobs.content[]`` carries
    ``{token, logprob, top_logprobs: [{token, logprob}, ...]}``.
  * **Anthropic Messages API** does NOT expose per-token logprobs.
    Slice 1 captures NOTHING from the Claude provider — no fake
    proxies, no derived heuristics. A future slice may add proxy
    signals (token timing variance, thinking budget consumption)
    behind a separate sub-flag. The cost contract already routes
    most of the autonomous surface (BG/SPEC/STANDARD) through DW
    first, so DW capture covers the majority of ops.

Architecture
------------

  * ``ConfidenceToken`` — frozen, hashable; one observed token +
    its logprob + top-K alternatives.
  * ``ConfidenceTrace`` — frozen; ordered immutable tuple of
    ``ConfidenceToken``s + provider metadata + truncation flag.
  * ``ConfidenceCapturer`` — bounded ring buffer; thread-safe
    append; produces immutable ``ConfidenceTrace`` via ``freeze()``.
    Append after cap → ``capture_truncated=True``.
  * ``compute_summary(trace) -> ConfidenceSummary`` — bounded
    rolling-stats projection (count, mean_top1, mean_margin,
    min_margin, max_margin). Slice 2's monitor reads this; full
    trace stays on ``ctx.artifacts`` for replay.

Master flag
-----------

``JARVIS_CONFIDENCE_CAPTURE_ENABLED`` (default ``false``).
Asymmetric env semantics — empty/whitespace = unset = default
false (Slice 1 ships behind the flag); explicit truthy enables.

Per-flip semantics: when off, every public method is a pure no-op.
Provider integration sites short-circuit BEFORE requesting logprobs
from the provider, so the request shape is byte-for-byte identical
to pre-Slice-1 behavior.

Knobs (FlagRegistry-typed, posture-relevant once Wave 1 #2 consumes):
  * ``JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS`` (default 4096) —
    bounded ring buffer cap. Tokens beyond the cap drop with
    ``capture_truncated=True``. Prevents memory blowup on long
    streaming generations.
  * ``JARVIS_CONFIDENCE_CAPTURE_TOP_K`` (default 5) — number of
    top alternatives to request from the provider. Higher K = more
    bandwidth + clearer confidence signal. K=1 captures only the
    chosen token; K=2 is the minimum useful value (top-1 vs top-2
    margin is the canonical confidence signal).

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / phase_runners / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers (would be circular and authority-
    violating).
  * Pure stdlib (``logging``, ``math``, ``os``, ``threading``) +
    typing only.
  * NEVER raises out of any public method — pure capture is
    structurally read-only on the model output and side-effect-free.
  * Read-only over inputs — never modifies provider stream events.
  * No control-flow influence — Slice 1 captures; Slice 2 acts.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


CONFIDENCE_CAPTURE_SCHEMA_VERSION: str = "confidence_capture.1"


# ---------------------------------------------------------------------------
# Master flag — Slice 1 ships default false, flips in Slice 5
# ---------------------------------------------------------------------------


def confidence_capture_enabled() -> bool:
    """``JARVIS_CONFIDENCE_CAPTURE_ENABLED`` (default ``true`` —
    graduated in Priority 1 Slice 5).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy disables.
    Re-read at call time so monkeypatch works in tests + operators
    can toggle live without re-init.

    Hot-revert: ``export JARVIS_CONFIDENCE_CAPTURE_ENABLED=false``
    short-circuits every public method to a pure no-op."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_CAPTURE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5 — was false in Slice 1)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Knobs (FlagRegistry-typed; values clamped defensively)
# ---------------------------------------------------------------------------


_DEFAULT_MAX_TOKENS: int = 4096
_DEFAULT_TOP_K: int = 5
_MIN_TOP_K: int = 1
_MAX_TOP_K: int = 20  # provider-side hard cap on most OpenAI-compat APIs


def confidence_capture_max_tokens() -> int:
    """Bounded ring buffer cap. Prevents memory blowup on long
    generations. Floor 1, no upper bound (operator's choice)."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS", "",
    ).strip()
    if not raw:
        return _DEFAULT_MAX_TOKENS
    try:
        val = int(raw)
        return max(1, val)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOKENS


def confidence_capture_top_k() -> int:
    """Number of top alternatives to request + store per token.
    Floored at 1, capped at 20 (provider-side hard cap)."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_CAPTURE_TOP_K", "",
    ).strip()
    if not raw:
        return _DEFAULT_TOP_K
    try:
        val = int(raw)
        return max(_MIN_TOP_K, min(_MAX_TOP_K, val))
    except (TypeError, ValueError):
        return _DEFAULT_TOP_K


# ---------------------------------------------------------------------------
# ConfidenceToken — single observed token + alternatives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceToken:
    """One observed token along with its logprob and the top-K
    alternative logprobs the model considered. Frozen + hashable.

    Fields
    ------
    token:
        The chosen token text. May be the empty string for special
        events (e.g., role markers); empty tokens are still captured
        for replay-completeness but contribute zero to the margin
        signal.
    logprob:
        Natural-log probability of the chosen token. Float in
        ``(-inf, 0]`` (logprob of 1.0 = 0.0 for a certain choice).
        Defensively coerced — non-numeric values land as ``-inf``
        (signaling "fully uncertain" for downstream stats).
    top_logprobs:
        Tuple of ``(token, logprob)`` pairs in descending logprob
        order, length ≤ ``top_k``. The first entry SHOULD match
        ``token`` if the provider is well-formed. Empty tuple is
        valid (some providers omit alternatives below threshold).
    """

    token: str
    logprob: float
    top_logprobs: Tuple[Tuple[str, float], ...] = ()

    def margin_top1_top2(self) -> Optional[float]:
        """Return the top-1 minus top-2 logprob margin, or ``None``
        if fewer than 2 alternatives were captured. The canonical
        confidence signal: small margin = uncertain, large margin =
        confident. NEVER raises."""
        try:
            if len(self.top_logprobs) < 2:
                return None
            top1 = float(self.top_logprobs[0][1])
            top2 = float(self.top_logprobs[1][1])
            if not math.isfinite(top1) or not math.isfinite(top2):
                return None
            return top1 - top2
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# ConfidenceTrace — immutable per-op trace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceTrace:
    """Immutable tuple of ``ConfidenceToken``s + provenance.
    Produced by ``ConfidenceCapturer.freeze()``.

    Fields
    ------
    tokens:
        Ordered tuple of observed tokens. Same order as the model
        emitted. Empty tuple is a valid trace (capture was enabled
        but no logprobs were observed).
    provider:
        String identifier of the provider that produced this trace
        (e.g., ``"doubleword"``, ``"claude"``, ``"prime"``).
    model_id:
        Provider-specific model identifier
        (e.g., ``"Qwen/Qwen3.5-397B-A17B-FP8"``).
    captured_at_unix:
        Wall-clock timestamp (seconds since epoch) when the trace
        was frozen. For replay determinism, downstream consumers
        should compare ``ConfidenceToken``s, not this timestamp.
    capture_truncated:
        ``True`` iff the ring buffer hit its cap and dropped tokens.
        Slice 2 monitor MAY treat truncation as a confidence signal
        (very long generations correlate with epistemic distress).
    schema_version:
        Schema marker for the captured shape. Stays stable across
        slice graduations until a real schema change (additive
        backward-compat changes don't bump this).
    """

    tokens: Tuple[ConfidenceToken, ...] = ()
    provider: str = ""
    model_id: str = ""
    captured_at_unix: float = 0.0
    capture_truncated: bool = False
    schema_version: str = CONFIDENCE_CAPTURE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# ConfidenceCapturer — bounded ring buffer for live capture
# ---------------------------------------------------------------------------


class ConfidenceCapturer:
    """Per-op accumulator for streaming logprob events. NOT a
    singleton — one instance per GENERATE round, lives on
    ``ctx.artifacts["confidence_capturer"]``.

    Thread-safe (RLock). Bounded by
    ``JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS`` — appends past the cap
    are silently dropped and ``capture_truncated`` flips True.

    Master-flag-gated: when off, every method is a pure no-op
    (returns immediately, no state change). Callers MAY construct
    a capturer regardless of flag state — the flag governs whether
    appends actually land.
    """

    __slots__ = (
        "_tokens",
        "_provider",
        "_model_id",
        "_truncated",
        "_lock",
        "_max_tokens",
    )

    def __init__(
        self, *, provider: str = "", model_id: str = "",
        max_tokens: Optional[int] = None,
    ) -> None:
        self._tokens: List[ConfidenceToken] = []
        self._provider: str = provider
        self._model_id: str = model_id
        self._truncated: bool = False
        self._lock = threading.RLock()
        self._max_tokens: int = (
            max_tokens if isinstance(max_tokens, int) and max_tokens > 0
            else confidence_capture_max_tokens()
        )

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model_id(self) -> str:
        return self._model_id

    def __len__(self) -> int:
        with self._lock:
            return len(self._tokens)

    @property
    def capture_truncated(self) -> bool:
        with self._lock:
            return self._truncated

    def append(
        self, *, token: Any, logprob: Any,
        top_logprobs: Any = None,
    ) -> bool:
        """Append a single token observation. Returns True if the
        token was appended, False if dropped (master-off OR ring
        buffer full).

        Defensive normalization:
          * ``token``: coerced to str (None → empty string).
          * ``logprob``: coerced to float; non-numeric → ``-inf``.
          * ``top_logprobs``: coerced to tuple-of-pairs; malformed
            entries silently dropped. Default ``None`` → empty.

        NEVER raises."""
        if not confidence_capture_enabled():
            return False
        try:
            safe_token = "" if token is None else str(token)
            try:
                safe_lp = float(logprob)
                if not math.isfinite(safe_lp):
                    safe_lp = float("-inf")
            except (TypeError, ValueError):
                safe_lp = float("-inf")
            safe_top: List[Tuple[str, float]] = []
            if top_logprobs is not None:
                try:
                    for entry in top_logprobs:
                        try:
                            if isinstance(entry, dict):
                                t = entry.get("token", "")
                                lp = entry.get("logprob", float("-inf"))
                            else:
                                t, lp = entry  # tuple/list shape
                            t_norm = "" if t is None else str(t)
                            try:
                                lp_norm = float(lp)
                                if not math.isfinite(lp_norm):
                                    lp_norm = float("-inf")
                            except (TypeError, ValueError):
                                lp_norm = float("-inf")
                            safe_top.append((t_norm, lp_norm))
                        except Exception:  # noqa: BLE001
                            continue
                except TypeError:
                    pass  # not iterable; treat as empty
            ct = ConfidenceToken(
                token=safe_token,
                logprob=safe_lp,
                top_logprobs=tuple(safe_top),
            )
            with self._lock:
                if len(self._tokens) >= self._max_tokens:
                    self._truncated = True
                    return False
                self._tokens.append(ct)
                return True
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ConfidenceCapturer] append swallowed exception",
                exc_info=True,
            )
            return False

    def freeze(self) -> ConfidenceTrace:
        """Produce an immutable trace from the current accumulator
        state. Safe to call multiple times; each call snapshots the
        current state. NEVER raises.

        Master-flag-gated: when off, returns an empty trace with
        the recorded provenance fields preserved (so callers can
        still attribute the trace if they want to)."""
        try:
            import time as _time
            with self._lock:
                tokens = tuple(self._tokens) if confidence_capture_enabled() else ()
                truncated = self._truncated
            return ConfidenceTrace(
                tokens=tokens,
                provider=self._provider,
                model_id=self._model_id,
                captured_at_unix=_time.time(),
                capture_truncated=truncated,
            )
        except Exception:  # noqa: BLE001 — defensive
            return ConfidenceTrace(
                provider=self._provider,
                model_id=self._model_id,
            )

    def reset(self) -> None:
        """Drop all captured state. Useful when a tool-loop round
        completes and the next round wants a fresh trace. NEVER
        raises."""
        try:
            with self._lock:
                self._tokens.clear()
                self._truncated = False
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Summary projection — what Slice 2's monitor + phase_capture consume
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceSummary:
    """Bounded rolling-stats projection of a ``ConfidenceTrace``.
    Frozen + hashable. The structural shape ``phase_capture``
    will serialize when Slice 2 wires the annotation; far smaller
    than the full trace, suitable for persistence in the Merkle DAG
    ledger.

    Fields are all ``Optional[float]`` because an empty trace
    yields no signal. Consumers MUST handle None.

    NOTE on phase_capture integration: this summary is attached as
    a SIDE-CHANNEL annotation on captured records (Slice 2), NOT
    as part of the canonical-hashed inputs — confidence varies
    across replays for identical seeds, so including it in the hash
    would break determinism. ``to_dict()`` produces the JSON-friendly
    annotation form.
    """

    token_count: int = 0
    has_alternatives_count: int = 0  # tokens with len(top_logprobs) >= 2
    mean_top1_logprob: Optional[float] = None
    mean_top1_top2_margin: Optional[float] = None
    min_top1_top2_margin: Optional[float] = None
    max_top1_top2_margin: Optional[float] = None
    capture_truncated: bool = False
    provider: str = ""
    model_id: str = ""
    schema_version: str = CONFIDENCE_CAPTURE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        """JSON-friendly serialization for ledger / observability.
        NEVER raises."""
        try:
            return {
                "schema_version": self.schema_version,
                "token_count": int(self.token_count),
                "has_alternatives_count": int(self.has_alternatives_count),
                "mean_top1_logprob": (
                    float(self.mean_top1_logprob)
                    if self.mean_top1_logprob is not None else None
                ),
                "mean_top1_top2_margin": (
                    float(self.mean_top1_top2_margin)
                    if self.mean_top1_top2_margin is not None else None
                ),
                "min_top1_top2_margin": (
                    float(self.min_top1_top2_margin)
                    if self.min_top1_top2_margin is not None else None
                ),
                "max_top1_top2_margin": (
                    float(self.max_top1_top2_margin)
                    if self.max_top1_top2_margin is not None else None
                ),
                "capture_truncated": bool(self.capture_truncated),
                "provider": str(self.provider or ""),
                "model_id": str(self.model_id or ""),
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "schema_version": CONFIDENCE_CAPTURE_SCHEMA_VERSION,
                "token_count": 0,
            }


def compute_summary(
    trace: Optional[ConfidenceTrace],
) -> ConfidenceSummary:
    """Pure projection from trace → summary. NEVER raises.

    Empty trace / None → empty summary (all None metrics).
    Trace with no alternatives → top1 stats present, margin stats
    None.

    Slice 2's monitor reads these summaries; the full trace stays
    in ctx for replay only."""
    try:
        if trace is None:
            return ConfidenceSummary()
        tokens = trace.tokens
        if not tokens:
            return ConfidenceSummary(
                provider=trace.provider,
                model_id=trace.model_id,
                capture_truncated=trace.capture_truncated,
            )
        finite_top1: List[float] = []
        margins: List[float] = []
        with_alts = 0
        for tok in tokens:
            try:
                if math.isfinite(tok.logprob):
                    finite_top1.append(float(tok.logprob))
                m = tok.margin_top1_top2()
                if m is not None and math.isfinite(m):
                    margins.append(float(m))
                    with_alts += 1
            except Exception:  # noqa: BLE001
                continue
        mean_top1: Optional[float] = (
            sum(finite_top1) / len(finite_top1)
            if finite_top1 else None
        )
        mean_margin: Optional[float] = (
            sum(margins) / len(margins) if margins else None
        )
        min_margin: Optional[float] = min(margins) if margins else None
        max_margin: Optional[float] = max(margins) if margins else None
        return ConfidenceSummary(
            token_count=len(tokens),
            has_alternatives_count=with_alts,
            mean_top1_logprob=mean_top1,
            mean_top1_top2_margin=mean_margin,
            min_top1_top2_margin=min_margin,
            max_top1_top2_margin=max_margin,
            capture_truncated=trace.capture_truncated,
            provider=trace.provider,
            model_id=trace.model_id,
        )
    except Exception:  # noqa: BLE001 — defensive
        return ConfidenceSummary()


# ---------------------------------------------------------------------------
# Convenience: openai-compat chunk shape extractor
# ---------------------------------------------------------------------------


def extract_openai_compat_logprobs_from_chunk(
    chunk: Any,
) -> Tuple[Tuple[Any, Any, Any], ...]:
    """Best-effort extractor for OpenAI-compatible streaming chunks.
    Reads ``chunk["choices"][0]["logprobs"]["content"]`` and yields
    a tuple of ``(token, logprob, top_logprobs)`` triples ready for
    ``ConfidenceCapturer.append(...)``.

    Returns empty tuple on any malformed input, missing logprobs,
    or non-streaming chunks. NEVER raises.

    The shape (per OpenAI streaming spec):
        chunk["choices"][0]["logprobs"]["content"] = [
            {
              "token": "the",
              "logprob": -0.0123,
              "top_logprobs": [
                  {"token": "the", "logprob": -0.0123},
                  {"token": "a", "logprob": -3.45},
                  ...
              ]
            },
            ...
        ]
    """
    out: List[Tuple[Any, Any, Any]] = []
    try:
        if not isinstance(chunk, dict):
            return ()
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return ()
        first = choices[0]
        if not isinstance(first, dict):
            return ()
        # Streaming: the per-token logprobs may live under "logprobs"
        # at the choice level OR under "delta.logprobs" depending on
        # provider. Check both.
        logprobs_obj = first.get("logprobs")
        if not isinstance(logprobs_obj, dict):
            delta = first.get("delta")
            if isinstance(delta, dict):
                logprobs_obj = delta.get("logprobs")
        if not isinstance(logprobs_obj, dict):
            return ()
        content = logprobs_obj.get("content")
        if not isinstance(content, list):
            return ()
        for entry in content:
            if not isinstance(entry, dict):
                continue
            tok = entry.get("token", "")
            lp = entry.get("logprob", float("-inf"))
            top = entry.get("top_logprobs", ())
            out.append((tok, lp, top))
    except Exception:  # noqa: BLE001 — defensive
        return ()
    return tuple(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_CAPTURE_SCHEMA_VERSION",
    "ConfidenceCapturer",
    "ConfidenceSummary",
    "ConfidenceToken",
    "ConfidenceTrace",
    "compute_summary",
    "confidence_capture_enabled",
    "confidence_capture_max_tokens",
    "confidence_capture_top_k",
    "extract_openai_compat_logprobs_from_chunk",
]
