"""Active-thinking progress aggregator (PRD §37 Phase 2,
2026-05-07).

Closes the operator-flagged "active-thinking timer missing" gap
from the v2.53 UX comparison: CC's screenshot shows
``* Investigating runner attribution root cause… (6m 52s ·
↓ 24.0k tokens · almost done thinking with high effort)`` as a
single rendered line. Pre-Phase-2 O+V's ``narrative_channel``
emits per-frame ``🤔`` lines via ``narrative_renderer.py:115``
without an aggregated timer / token / effort signal.

This module is the SOLE knower of the thinking-progress
aggregation. It composes existing canonical sources:

  * :mod:`battle_test.narrative_channel` — verb-phrase derivation
    from active THINKING frames + elapsed time (canonical
    ``frame.started_at`` monotonic timestamp)
  * :mod:`battle_test.stream_renderer` — token counts via the
    canonical ``StreamRenderer._token_count`` /
    ``_first_token_mono`` / ``_start_mono`` private state
    (singleton accessor exposed via :func:`get_stream_renderer`)
  * :mod:`governance.ide_observability_stream` — SSE event
    ``thinking_progress_tick`` for IDE consumers

NEVER reimplements any of those — pure composition.

## Architectural locks (operator mandate, AST-pinned)

  1. **Pure substrate** — no I/O beyond what's needed for the
     observer state. NEVER raises.
  2. **Authority asymmetry** — imports stdlib + governance/meta
     ONLY at top-level. NEVER imports orchestrator / iron_gate /
     policy / providers / candidate_generator / change_engine /
     semantic_guardian.
  3. **Composes canonical sources** — every metric (verb-phrase,
     elapsed, tokens) MUST come from the canonical module via
     lazy-import. AST-pinned: forbidden to track parallel
     state for any of these axes.
  4. **Closed effort taxonomy** — :class:`EffortBand` is a
     4-value frozen enum. New bands require explicit scope-doc +
     pin update.
  5. **Chatter-suppression structural** — :meth:`update` records
     the snapshot but emits SSE only when the verb-phrase OR
     effort-band CROSSES a value. Identical re-update is silent.
"""
from __future__ import annotations

import enum
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)


THINKING_PROGRESS_SCHEMA_VERSION: str = "thinking_progress.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_THINKING_PROGRESS_ENABLED`` master switch.
    Default-FALSE per §33.1 — when off, :func:`format_thinking_line`
    returns empty string and :class:`ThinkingProgressObserver`
    state is unused. Operator flips after observing the substrate
    via the status-line composition."""
    if os.environ.get( "JARVIS_THINKING_PROGRESS_ENABLED", "", ).strip().lower() in _TRUTHY:
        return True
    # §40 polish pack opt-in — when JARVIS_UX_POLISH_PACK_ENABLED
    # is on AND the operator hasn't explicitly disabled this
    # substrate via its own env flag, the pack predicate
    # activates it. Preserves §33.1 default-FALSE discipline:
    # the canonical _flag(...) / _TRUTHY check above is intact
    # so the substrate's master_default_false AST pin still
    # fires structurally.
    try:
        from backend.core.ouroboros.governance.ux_polish_pack import (
            is_substrate_in_active_pack,
        )
        return is_substrate_in_active_pack('thinking_progress_aggregator')
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Closed effort taxonomy (4 values, AST-pinned)
# ---------------------------------------------------------------------------


class EffortBand(str, enum.Enum):
    """Closed 4-value effort taxonomy. Bytes-pinned via AST
    regression.

    Mapping rule (deterministic; pure-function
    :func:`compute_effort_band`):

      * ``LOW``       — elapsed < 30s AND tokens < 5k
      * ``MEDIUM``    — elapsed < 2min OR tokens < 15k
      * ``HIGH``      — elapsed < 5min OR tokens < 30k
      * ``VERY_HIGH`` — elapsed >= 5min OR tokens >= 30k

    Strictest threshold wins (whichever axis is higher pushes
    the band up). Mirrors CC's effort-rendering vocabulary
    ("low" / "medium" / "high" / "very high effort").
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


# ---------------------------------------------------------------------------
# Threshold knobs — env-overridable (operator-binding "no hardcoding")
# ---------------------------------------------------------------------------


_LOW_ELAPSED_S_DEFAULT: float = 30.0
_MEDIUM_ELAPSED_S_DEFAULT: float = 120.0
_HIGH_ELAPSED_S_DEFAULT: float = 300.0
_LOW_TOKENS_DEFAULT: int = 5_000
_MEDIUM_TOKENS_DEFAULT: int = 15_000
_HIGH_TOKENS_DEFAULT: int = 30_000


def _safe_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _safe_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def low_elapsed_threshold_s() -> float:
    return _safe_float_env(
        "JARVIS_THINKING_PROGRESS_LOW_ELAPSED_S",
        _LOW_ELAPSED_S_DEFAULT,
    )


def medium_elapsed_threshold_s() -> float:
    return _safe_float_env(
        "JARVIS_THINKING_PROGRESS_MEDIUM_ELAPSED_S",
        _MEDIUM_ELAPSED_S_DEFAULT,
    )


def high_elapsed_threshold_s() -> float:
    return _safe_float_env(
        "JARVIS_THINKING_PROGRESS_HIGH_ELAPSED_S",
        _HIGH_ELAPSED_S_DEFAULT,
    )


def low_tokens_threshold() -> int:
    return _safe_int_env(
        "JARVIS_THINKING_PROGRESS_LOW_TOKENS",
        _LOW_TOKENS_DEFAULT,
    )


def medium_tokens_threshold() -> int:
    return _safe_int_env(
        "JARVIS_THINKING_PROGRESS_MEDIUM_TOKENS",
        _MEDIUM_TOKENS_DEFAULT,
    )


def high_tokens_threshold() -> int:
    return _safe_int_env(
        "JARVIS_THINKING_PROGRESS_HIGH_TOKENS",
        _HIGH_TOKENS_DEFAULT,
    )


# ---------------------------------------------------------------------------
# Pure-function decision: effort-band computation
# ---------------------------------------------------------------------------


def compute_effort_band(
    *,
    elapsed_s: float,
    tokens_total: int,
) -> EffortBand:
    """Map ``(elapsed_s, tokens_total)`` to a :class:`EffortBand`.
    Pure function. NEVER raises.

    Strictest threshold wins (whichever axis is higher pushes
    the band up). Defensive on negative / NaN inputs: clamped
    to 0 / 0."""
    try:
        e = max(0.0, float(elapsed_s) if elapsed_s == elapsed_s else 0.0)
    except (TypeError, ValueError):
        e = 0.0
    try:
        t = max(0, int(tokens_total))
    except (TypeError, ValueError):
        t = 0
    e_band = _band_for_elapsed(e)
    t_band = _band_for_tokens(t)
    return _max_band(e_band, t_band)


def _band_for_elapsed(elapsed_s: float) -> EffortBand:
    if elapsed_s >= high_elapsed_threshold_s():
        return EffortBand.VERY_HIGH
    if elapsed_s >= medium_elapsed_threshold_s():
        return EffortBand.HIGH
    if elapsed_s >= low_elapsed_threshold_s():
        return EffortBand.MEDIUM
    return EffortBand.LOW


def _band_for_tokens(tokens: int) -> EffortBand:
    if tokens >= high_tokens_threshold():
        return EffortBand.VERY_HIGH
    if tokens >= medium_tokens_threshold():
        return EffortBand.HIGH
    if tokens >= low_tokens_threshold():
        return EffortBand.MEDIUM
    return EffortBand.LOW


_BAND_ORDER: Dict[EffortBand, int] = {
    EffortBand.LOW: 0,
    EffortBand.MEDIUM: 1,
    EffortBand.HIGH: 2,
    EffortBand.VERY_HIGH: 3,
}


def _max_band(a: EffortBand, b: EffortBand) -> EffortBand:
    return a if _BAND_ORDER[a] >= _BAND_ORDER[b] else b


# ---------------------------------------------------------------------------
# Verb-phrase derivation
# ---------------------------------------------------------------------------


_GERUND_PATTERN = re.compile(r"^([A-Z][a-z]+ing)\b")
_FALLBACK_VERB_PHRASE: str = "Thinking"


def derive_verb_phrase(prose: str) -> str:
    """Extract a 1-3 word verb-phrase from a THINKING frame's
    prose. Pure function. NEVER raises.

    Heuristic (deterministic; no LLM call):

      1. If first non-empty line starts with a capitalized
         English gerund (``Investigating``, ``Considering``,
         ``Reviewing``, etc.), return the gerund word.
      2. Otherwise, return the first 1-3 words of the first
         line up to first sentence-ending punctuation, capped
         at 60 chars.
      3. Empty / non-string → :data:`_FALLBACK_VERB_PHRASE`.
    """
    if not isinstance(prose, str):
        return _FALLBACK_VERB_PHRASE
    text = prose.strip()
    if not text:
        return _FALLBACK_VERB_PHRASE
    # Take first non-empty line.
    first_line = text.split("\n", 1)[0].strip()
    if not first_line:
        return _FALLBACK_VERB_PHRASE
    # Try gerund pattern first.
    m = _GERUND_PATTERN.match(first_line)
    if m:
        return m.group(1)
    # Fallback: first 1-3 words up to ., !, ?, …, or 60 chars.
    truncated = re.split(r"[.!?…]", first_line, maxsplit=1)[0]
    truncated = truncated[:60].strip()
    words = truncated.split()
    if not words:
        return _FALLBACK_VERB_PHRASE
    short = " ".join(words[:3])
    return short or _FALLBACK_VERB_PHRASE


# ---------------------------------------------------------------------------
# Versioned snapshot artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThinkingProgressSnapshot:
    """One thinking-progress aggregation for an op_id. Frozen
    for safe propagation across asyncio tasks."""

    schema_version: str = THINKING_PROGRESS_SCHEMA_VERSION
    op_id: str = ""
    verb_phrase: str = _FALLBACK_VERB_PHRASE
    elapsed_s: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    effort_band: EffortBand = EffortBand.LOW
    is_active: bool = False
    captured_at_unix: float = 0.0

    @property
    def tokens_total(self) -> int:
        return int(self.tokens_input) + int(self.tokens_output)

    def to_dict(self) -> Dict[str, Any]:
        """§33.5 symmetric projection. NEVER raises."""
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "verb_phrase": self.verb_phrase,
            "elapsed_s": float(self.elapsed_s),
            "tokens_input": int(self.tokens_input),
            "tokens_output": int(self.tokens_output),
            "tokens_total": self.tokens_total,
            "effort_band": self.effort_band.value,
            "is_active": bool(self.is_active),
            "captured_at_unix": float(self.captured_at_unix),
        }


# ---------------------------------------------------------------------------
# Format thinking line — pure function
# ---------------------------------------------------------------------------


_EFFORT_LABELS: Dict[EffortBand, str] = {
    EffortBand.LOW: "low effort",
    EffortBand.MEDIUM: "medium effort",
    EffortBand.HIGH: "high effort",
    EffortBand.VERY_HIGH: "very high effort",
}


def _format_elapsed(seconds: float) -> str:
    s = int(max(0.0, float(seconds)))
    if s < 60:
        return f"{s}s"
    minutes = s // 60
    secs = s % 60
    return f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"


def _format_tokens(n: int) -> str:
    nv = max(0, int(n))
    if nv < 1000:
        return f"{nv} tokens"
    if nv < 10_000:
        return f"{nv / 1000:.1f}k tokens"
    return f"{nv // 1000}k tokens"


def format_thinking_line(
    snapshot: ThinkingProgressSnapshot,
) -> str:
    """Render the single thinking-progress line.

    Output shape (matches CC visual format):
      ``* Investigating root cause… (6m 52s · ↓ 24.0k tokens
      · high effort)``

    NEVER raises. Returns empty string on bad inputs / when
    snapshot is not active."""
    try:
        if not snapshot.is_active:
            return ""
        verb = (snapshot.verb_phrase or _FALLBACK_VERB_PHRASE).strip()
        elapsed = _format_elapsed(snapshot.elapsed_s)
        tokens = _format_tokens(snapshot.tokens_total)
        effort = _EFFORT_LABELS.get(
            snapshot.effort_band, "unknown effort",
        )
        return (
            f"* {verb}… ({elapsed} · ↓ {tokens} · {effort})"
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ThinkingProgress] format_thinking_line "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# Observer singleton — composes canonical sources
# ---------------------------------------------------------------------------


class ThinkingProgressObserver:
    """Per-op aggregator. Composes canonical narrative_channel
    THINKING frames + stream_renderer token counts. Thread-safe.

    Chatter-suppression structural: :meth:`update` returns an
    SSE-eligible flag (True) only when the band OR verb-phrase
    crossed a value; identical re-update returns False (silent
    SSE).

    NEVER raises — every read path is defensive."""

    def __init__(self) -> None:
        self._snapshots: Dict[str, ThinkingProgressSnapshot] = {}
        self._last_band: Dict[str, EffortBand] = {}
        self._last_verb: Dict[str, str] = {}
        self._lock = threading.RLock()

    def update(
        self,
        *,
        op_id: str,
        now_unix: Optional[float] = None,
    ) -> Tuple[Optional[ThinkingProgressSnapshot], bool]:
        """Compose canonical sources, build a fresh snapshot,
        store it. Returns ``(snapshot, sse_eligible)``.

        ``sse_eligible`` is True only on band/verb crossings —
        chatter-suppression structural. NEVER raises."""
        try:
            op_safe = str(op_id or "").strip()
            if not op_safe:
                return (None, False)
            verb_phrase, elapsed_s, is_active = (
                self._compose_narrative(op_safe)
            )
            tokens_in, tokens_out = self._compose_tokens()
            band = compute_effort_band(
                elapsed_s=elapsed_s,
                tokens_total=tokens_in + tokens_out,
            )
            now = (
                float(now_unix)
                if now_unix is not None
                else time.time()
            )
            snap = ThinkingProgressSnapshot(
                op_id=op_safe,
                verb_phrase=verb_phrase,
                elapsed_s=elapsed_s,
                tokens_input=tokens_in,
                tokens_output=tokens_out,
                effort_band=band,
                is_active=is_active,
                captured_at_unix=now,
            )
            with self._lock:
                self._snapshots[op_safe] = snap
                prev_band = self._last_band.get(op_safe)
                prev_verb = self._last_verb.get(op_safe)
                self._last_band[op_safe] = band
                self._last_verb[op_safe] = verb_phrase
            crossed = (
                prev_band != band or prev_verb != verb_phrase
            )
            return (snap, crossed)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[ThinkingProgress] update swallowed: %s",
                type(exc).__name__,
            )
            return (None, False)

    def get(
        self, op_id: str,
    ) -> Optional[ThinkingProgressSnapshot]:
        try:
            with self._lock:
                return self._snapshots.get(
                    str(op_id or "").strip(),
                )
        except Exception:  # noqa: BLE001 — defensive
            return None

    def all_active(
        self,
    ) -> Tuple[ThinkingProgressSnapshot, ...]:
        try:
            with self._lock:
                return tuple(
                    s for s in self._snapshots.values()
                    if s.is_active
                )
        except Exception:  # noqa: BLE001 — defensive
            return ()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._snapshots.clear()
            self._last_band.clear()
            self._last_verb.clear()

    # --- canonical-source composers (lazy-import; AST-pinned) ---

    def _compose_narrative(
        self, op_id: str,
    ) -> Tuple[str, float, bool]:
        """Extract verb-phrase + elapsed_s from
        narrative_channel THINKING frame for ``op_id``. NEVER
        raises — degrades to (_FALLBACK_VERB_PHRASE, 0.0,
        False) on any failure."""
        try:
            from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
                get_default_channel,
                NarrativeKind,
            )
            channel = get_default_channel()
            frame = channel.active_thinking_frame(op_id=op_id)
            if frame is None:
                # No active THINKING frame — check for committed
                # frames as fallback. If none, op is not actively
                # thinking.
                committed = channel.frames_by_op_kind(
                    op_id=op_id,
                    kind=NarrativeKind.THINKING,
                )
                if not committed:
                    return (_FALLBACK_VERB_PHRASE, 0.0, False)
                # Use the most recent committed frame's
                # verb-phrase but mark as inactive.
                latest = committed[-1]
                verb = derive_verb_phrase(latest.prose)
                elapsed = max(
                    0.0,
                    time.monotonic() - float(latest.started_at),
                )
                return (verb, elapsed, False)
            verb = derive_verb_phrase(frame.prose)
            elapsed = max(
                0.0,
                time.monotonic() - float(frame.started_at),
            )
            return (verb, elapsed, True)
        except Exception:  # noqa: BLE001 — defensive
            return (_FALLBACK_VERB_PHRASE, 0.0, False)

    def _compose_tokens(self) -> Tuple[int, int]:
        """Read token counts from canonical
        ``stream_renderer.StreamRenderer``. NEVER raises —
        degrades to (0, 0) when renderer is absent.

        StreamRenderer tracks output (model-emitted) tokens
        only — input tokens come from the provider's prompt
        accounting, which the renderer doesn't have access to.
        Phase 2 reports output tokens via
        :class:`StreamRenderer._token_count`; input tokens stay
        0 unless a future arc wires provider prompt accounting.
        """
        try:
            from backend.core.ouroboros.battle_test.stream_renderer import (  # noqa: E501
                get_stream_renderer,
            )
            renderer = get_stream_renderer()
            if renderer is None:
                return (0, 0)
            tokens_out = int(getattr(renderer, "_token_count", 0))
            return (0, tokens_out)
        except Exception:  # noqa: BLE001 — defensive
            return (0, 0)


# Module singleton.
_DEFAULT_OBSERVER: Optional[ThinkingProgressObserver] = None
_OBSERVER_LOCK: threading.Lock = threading.Lock()


def get_default_observer() -> ThinkingProgressObserver:
    global _DEFAULT_OBSERVER
    with _OBSERVER_LOCK:
        if _DEFAULT_OBSERVER is None:
            _DEFAULT_OBSERVER = ThinkingProgressObserver()
        return _DEFAULT_OBSERVER


def reset_observer_for_tests() -> None:
    global _DEFAULT_OBSERVER
    with _OBSERVER_LOCK:
        _DEFAULT_OBSERVER = None


# ---------------------------------------------------------------------------
# SSE event publisher — composes canonical broker (no parallel)
# ---------------------------------------------------------------------------


def publish_thinking_progress_event(
    snapshot: ThinkingProgressSnapshot,
) -> bool:
    """Publish a ``thinking_progress_tick`` event to the canonical
    SSE broker. NEVER raises. Returns True on best-effort
    publish, False on any failure.

    Composes canonical broker via lazy-import — same shape as
    sibling event publishers (e.g.,
    :func:`publish_multi_prior_dispatch_event`,
    :func:`publish_execution_graph_progress_event`)."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            stream_enabled,
            EVENT_TYPE_THINKING_PROGRESS_TICK,
            get_default_broker,
        )
        if not stream_enabled():
            return False
        broker = get_default_broker()
        if broker is None:
            return False
        result = broker.publish(
            EVENT_TYPE_THINKING_PROGRESS_TICK,
            snapshot.op_id or "",
            snapshot.to_dict(),
        )
        return result is not None
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. ``master_default_false`` — JARVIS_THINKING_PROGRESS_-
         ENABLED stays default-FALSE per §33.1.
      2. ``effort_band_taxonomy_4_values`` — closed-enum
         integrity.
      3. ``authority_asymmetry`` — substrate purity.
      4. ``composes_canonical_narrative`` — observer MUST
         lazy-import narrative_channel for verb-phrase + elapsed
         (no parallel state tracking).
      5. ``composes_canonical_stream_renderer`` — observer MUST
         lazy-import stream_renderer for tokens (no parallel
         token counter).
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
        "thinking_progress_aggregator.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                src = ast.unparse(node)
                # §40 polish-pack composition: walk only the
                # top-level body + unconditional containers (Try)
                # so `if env_check: return True` is correctly
                # recognized as gated. Naive `"return True" in src`
                # would fire on the conditional path too.
                def _has_unconditional_return_true(stmts):
                    for stmt in stmts:
                        if (
                            isinstance(stmt, ast.Return)
                            and isinstance(stmt.value, ast.Constant)
                            and stmt.value.value is True
                        ):
                            return True
                        if isinstance(stmt, ast.Try):
                            if _has_unconditional_return_true(
                                stmt.body,
                            ):
                                return True
                            if _has_unconditional_return_true(
                                stmt.finalbody,
                            ):
                                return True
                    return False

                if _has_unconditional_return_true(node.body):
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if (
                    "JARVIS_THINKING_PROGRESS_ENABLED"
                    not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_THINKING_PROGRESS_ENABLED"
                    )
        return tuple(violations)

    def _validate_effort_band_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {"LOW", "MEDIUM", "HIGH", "VERY_HIGH"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "EffortBand":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"EffortBand missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"EffortBand has extras "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
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
                            f"thinking_progress_aggregator MUST "
                            f"NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_narrative(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "narrative_channel" not in source:
            violations.append(
                "thinking_progress_aggregator MUST compose "
                "narrative_channel (no parallel verb-phrase / "
                "elapsed tracking)"
            )
        if "active_thinking_frame" not in source:
            violations.append(
                "observer MUST use canonical "
                "NarrativeChannel.active_thinking_frame "
                "accessor"
            )
        return tuple(violations)

    def _validate_composes_stream_renderer(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "stream_renderer" not in source:
            violations.append(
                "thinking_progress_aggregator MUST compose "
                "stream_renderer (no parallel token counter)"
            )
        if "get_stream_renderer" not in source:
            violations.append(
                "observer MUST use canonical "
                "get_stream_renderer accessor"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "thinking_progress_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_THINKING_PROGRESS_ENABLED "
                "stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "thinking_progress_effort_band_taxonomy_4_values"
            ),
            target_file=target,
            description=(
                "EffortBand is a 4-value closed taxonomy "
                "(LOW / MEDIUM / HIGH / VERY_HIGH). New values "
                "require explicit scope-doc + pin update."
            ),
            validate=_validate_effort_band_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "thinking_progress_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Aggregator MUST stay pure substrate composing "
                "narrative_channel + stream_renderer + stdlib "
                "ONLY. NEVER imports orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "change_engine / semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "thinking_progress_composes_canonical_narrative"
            ),
            target_file=target,
            description=(
                "Observer MUST compose canonical "
                "NarrativeChannel.active_thinking_frame for "
                "verb-phrase + elapsed. No parallel state for "
                "either axis."
            ),
            validate=_validate_composes_narrative,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "thinking_progress_composes_canonical_stream_"
                "renderer"
            ),
            target_file=target,
            description=(
                "Observer MUST compose canonical "
                "get_stream_renderer for tokens. No parallel "
                "token counter."
            ),
            validate=_validate_composes_stream_renderer,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    """Register thinking-progress flags with the FlagRegistry."""
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_THINKING_PROGRESS_ENABLED",
            "bool",
            "false",
            (
                "Master flag for the active-thinking progress "
                "aggregator (§37 Phase 2). Default-FALSE per "
                "§33.1; flips after operator validates the "
                "render against status-line composition."
            ),
        ),
        (
            "JARVIS_THINKING_PROGRESS_LOW_ELAPSED_S",
            "float",
            str(_LOW_ELAPSED_S_DEFAULT),
            "Elapsed-seconds threshold for LOW→MEDIUM band.",
        ),
        (
            "JARVIS_THINKING_PROGRESS_MEDIUM_ELAPSED_S",
            "float",
            str(_MEDIUM_ELAPSED_S_DEFAULT),
            "Elapsed-seconds threshold for MEDIUM→HIGH band.",
        ),
        (
            "JARVIS_THINKING_PROGRESS_HIGH_ELAPSED_S",
            "float",
            str(_HIGH_ELAPSED_S_DEFAULT),
            "Elapsed-seconds threshold for HIGH→VERY_HIGH band.",
        ),
        (
            "JARVIS_THINKING_PROGRESS_LOW_TOKENS",
            "int",
            str(_LOW_TOKENS_DEFAULT),
            "Token-count threshold for LOW→MEDIUM band.",
        ),
        (
            "JARVIS_THINKING_PROGRESS_MEDIUM_TOKENS",
            "int",
            str(_MEDIUM_TOKENS_DEFAULT),
            "Token-count threshold for MEDIUM→HIGH band.",
        ),
        (
            "JARVIS_THINKING_PROGRESS_HIGH_TOKENS",
            "int",
            str(_HIGH_TOKENS_DEFAULT),
            "Token-count threshold for HIGH→VERY_HIGH band.",
        ),
    )
    n = 0
    try:
        for name, kind, default, desc in seeds:
            try:
                registry.register(
                    name=name,
                    type_=kind,
                    default=default,
                    description=desc,
                    category="ux",
                    posture_relevance="RELEVANT",
                    source_file=(
                        "backend/core/ouroboros/governance/"
                        "thinking_progress_aggregator.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001 — defensive
                continue
    except Exception:  # noqa: BLE001 — defensive
        return n
    return n


__all__ = [
    "EffortBand",
    "THINKING_PROGRESS_SCHEMA_VERSION",
    "ThinkingProgressObserver",
    "ThinkingProgressSnapshot",
    "compute_effort_band",
    "derive_verb_phrase",
    "format_thinking_line",
    "get_default_observer",
    "high_elapsed_threshold_s",
    "high_tokens_threshold",
    "low_elapsed_threshold_s",
    "low_tokens_threshold",
    "master_enabled",
    "medium_elapsed_threshold_s",
    "medium_tokens_threshold",
    "publish_thinking_progress_event",
    "register_flags",
    "register_shipped_invariants",
    "reset_observer_for_tests",
]
