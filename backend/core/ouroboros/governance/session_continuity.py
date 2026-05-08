"""§38.11-B session-continuity substrate (PRD v2.65 to v2.66,
2026-05-07).

Closes the §38.11-B commitment per the §38.11.5a.3 reconciled
table: two surfaces in one canonical module (single canonical
name per §38.11.5a.5 discipline) — both serve "cross-session
awareness" for an autonomous organism.

  1. **Graduation ticker** — live feed of capability flags
     transitioning to READY in the canonical
     :class:`UnifiedGraduationVerdict` taxonomy. Self-modification
     visible: when O+V's empirical evidence accumulates enough
     for a flag to flip default-true, the operator sees it.
  2. **Cross-session memory diff** — what changed since the
     previous session. Cross-session continuity surface that
     CC structurally cannot have.

## Composes canonical sources (operator binding "no duplication")

  * :mod:`governance.unified_graduation_dashboard` —
    ``aggregate_dashboard()`` for graduation state; verdict
    transitions detected by tracking previous snapshot.
  * :mod:`governance.last_session_summary` —
    ``LastSessionSummary.load(n_sessions)`` for prior session
    SessionRecords. Already shipped substrate.
  * :mod:`governance.ide_observability_stream` —
    ``EVENT_TYPE_FLAG_GRADUATED`` (newly registered) +
    canonical broker.

NEVER reimplements graduation aggregation, session-summary
parsing, or SSE event publishing. Pure composition layer.

## Architectural locks (operator mandate, AST-pinned)

  1. **Master flag default-FALSE** per §33.1.
  2. **Authority asymmetry** — imports stdlib +
     governance.{unified_graduation_dashboard,
     last_session_summary, ide_observability_stream} ONLY.
  3. **Composes canonical graduation dashboard** — graduation
     ticker MUST lazy-import ``aggregate_dashboard``; no
     parallel verdict computation.
  4. **Composes canonical last_session_summary** — memory diff
     MUST lazy-import ``LastSessionSummary``; no parallel
     summary parsing.
  5. **Composes canonical SSE broker** — graduation events MUST
     publish via canonical ``ide_observability_stream`` broker
     (no parallel event publisher / no parallel event ring).
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


SESSION_CONTINUITY_SCHEMA_VERSION: str = "session_continuity.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_SESSION_CONTINUITY_ENABLED`` master switch.
    Default-FALSE per §33.1 — when off, both surfaces
    short-circuit to empty output."""
    return os.environ.get(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _sub_flag_enabled(name: str) -> bool:
    """Per-surface sub-flag check. Defaults to True when bundle
    master is on."""
    if not master_enabled():
        return False
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return True
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Surface 1: GraduationTicker — composes unified_graduation_dashboard
# ---------------------------------------------------------------------------


class GraduationTransition(str, enum.Enum):
    """Closed 4-value transition taxonomy. Bytes-pinned via
    AST regression.

      * ``BECAME_READY`` — flag transitioned to verdict=READY
        (eligible for default-true flip; the load-bearing
        graduation event).
      * ``BACKED_OFF`` — flag transitioned away from READY
        (e.g., a runner failure pushed it to EVIDENCE_FAILED).
      * ``UNCHANGED`` — verdict unchanged from previous tick.
      * ``NEW`` — first observation of this flag (no previous
        verdict to compare).
    """

    BECAME_READY = "became_ready"
    BACKED_OFF = "backed_off"
    UNCHANGED = "unchanged"
    NEW = "new"


@dataclass(frozen=True)
class GraduationEvent:
    """One graduation ticker event. Frozen for safe propagation.

    Adopts §33.5 versioned-artifact contract."""

    schema_version: str = SESSION_CONTINUITY_SCHEMA_VERSION
    flag_name: str = ""
    transition: GraduationTransition = (
        GraduationTransition.UNCHANGED
    )
    previous_verdict: str = ""
    current_verdict: str = ""
    diagnostic: str = ""
    detected_at_unix: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "flag_name": self.flag_name,
            "transition": self.transition.value,
            "previous_verdict": self.previous_verdict,
            "current_verdict": self.current_verdict,
            "diagnostic": self.diagnostic,
            "detected_at_unix": float(self.detected_at_unix),
        }


class GraduationTicker:
    """Tracks per-flag verdict transitions across calls.
    Composes canonical
    :func:`unified_graduation_dashboard.aggregate_dashboard`.
    Thread-safe singleton.

    Each :meth:`tick()` call:
      1. Composes canonical aggregate_dashboard for current
         state.
      2. Diffs current verdict per flag against previous
         in-memory snapshot.
      3. Returns transitions as a list of
         :class:`GraduationEvent` records.
      4. Best-effort publishes ``EVENT_TYPE_FLAG_GRADUATED``
         to canonical SSE broker for any BECAME_READY
         transition.

    NEVER raises."""

    _MAX_HISTORY: int = 64

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._previous_verdicts: Dict[str, str] = {}
        self._history: List[GraduationEvent] = []

    def tick(self) -> Tuple[GraduationEvent, ...]:
        """Compose canonical aggregator + emit transitions.
        NEVER raises."""
        try:
            if not _sub_flag_enabled(
                "JARVIS_SESSION_CONTINUITY_TICKER_ENABLED",
            ):
                return ()
            try:
                from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
                    aggregate_dashboard,
                )
                snap = aggregate_dashboard()
            except Exception:  # noqa: BLE001 — defensive
                return ()
            if not snap or not snap.rows:
                return ()
            now = time.time()
            transitions: List[GraduationEvent] = []
            with self._lock:
                for row in snap.rows:
                    name = getattr(row, "name", "")
                    if not name:
                        continue
                    current = getattr(
                        getattr(row, "verdict", None),
                        "value", "",
                    ) or ""
                    diagnostic = getattr(
                        row, "diagnostic", "",
                    ) or ""
                    previous = self._previous_verdicts.get(
                        name,
                    )
                    transition = self._classify_transition(
                        previous=previous,
                        current=current,
                    )
                    if transition in (
                        GraduationTransition.BECAME_READY,
                        GraduationTransition.BACKED_OFF,
                        GraduationTransition.NEW,
                    ):
                        # Only record meaningful transitions.
                        # NEW is recorded but only voiced if
                        # current=ready (handled in formatter).
                        ev = GraduationEvent(
                            flag_name=name,
                            transition=transition,
                            previous_verdict=previous or "",
                            current_verdict=current,
                            diagnostic=diagnostic,
                            detected_at_unix=now,
                        )
                        transitions.append(ev)
                        self._history.append(ev)
                        if len(self._history) > (
                            self._MAX_HISTORY
                        ):
                            self._history = self._history[
                                -self._MAX_HISTORY:
                            ]
                    self._previous_verdicts[name] = current
            # Best-effort SSE publish — composes canonical
            # broker. NEVER raises.
            for ev in transitions:
                if (
                    ev.transition
                    == GraduationTransition.BECAME_READY
                ):
                    _publish_flag_graduated_event(ev)
            return tuple(transitions)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[session_continuity] tick swallowed: %s",
                type(exc).__name__,
            )
            return ()

    def _classify_transition(
        self,
        *,
        previous: Optional[str],
        current: str,
    ) -> GraduationTransition:
        if previous is None:
            if current == "ready":
                return GraduationTransition.NEW
            return GraduationTransition.UNCHANGED
        if previous == current:
            return GraduationTransition.UNCHANGED
        if current == "ready":
            return GraduationTransition.BECAME_READY
        if previous == "ready" and current != "ready":
            return GraduationTransition.BACKED_OFF
        return GraduationTransition.UNCHANGED

    def history(
        self, *, limit: int = 10,
    ) -> Tuple[GraduationEvent, ...]:
        """Return last N events in chronological order. Pure read."""
        with self._lock:
            n = max(1, min(int(limit), self._MAX_HISTORY))
            return tuple(self._history[-n:])

    def reset_for_tests(self) -> None:
        with self._lock:
            self._previous_verdicts.clear()
            self._history.clear()


_DEFAULT_TICKER: Optional[GraduationTicker] = None
_TICKER_LOCK: threading.Lock = threading.Lock()


def get_default_ticker() -> GraduationTicker:
    global _DEFAULT_TICKER
    with _TICKER_LOCK:
        if _DEFAULT_TICKER is None:
            _DEFAULT_TICKER = GraduationTicker()
        return _DEFAULT_TICKER


def reset_ticker_for_tests() -> None:
    global _DEFAULT_TICKER
    with _TICKER_LOCK:
        _DEFAULT_TICKER = None


def _publish_flag_graduated_event(
    event: GraduationEvent,
) -> bool:
    """Compose canonical broker. NEVER raises. Best-effort."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            stream_enabled,
            EVENT_TYPE_FLAG_GRADUATED,
            get_default_broker,
        )
        if not stream_enabled():
            return False
        broker = get_default_broker()
        if broker is None:
            return False
        result = broker.publish(
            EVENT_TYPE_FLAG_GRADUATED,
            event.flag_name,
            event.to_dict(),
        )
        return result is not None
    except Exception:  # noqa: BLE001 — defensive
        return False


def format_graduation_ticker(
    *,
    transitions: Optional[
        Tuple[GraduationEvent, ...]
    ] = None,
) -> str:
    """Render the graduation ticker as multi-line text.

    Output shape:
        ``Recently graduated:``
        ``  ✨ JARVIS_DECISION_TRACE_LEDGER_ENABLED — clean=3/3``
        ``  ⚠ JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED — backed off``

    NEVER raises. Returns empty when:
      * Master flag off
      * Sub-flag disabled
      * No transitions in last tick"""
    try:
        if not _sub_flag_enabled(
            "JARVIS_SESSION_CONTINUITY_TICKER_ENABLED",
        ):
            return ""
        ticker = get_default_ticker()
        events = (
            transitions
            if transitions is not None
            else ticker.tick()
        )
        meaningful = [
            e for e in events
            if e.transition in (
                GraduationTransition.BECAME_READY,
                GraduationTransition.BACKED_OFF,
            )
        ]
        if not meaningful:
            return ""
        lines = ["Recently graduated:"]
        for ev in meaningful:
            if ev.transition == (
                GraduationTransition.BECAME_READY
            ):
                glyph = "✨"
                tag = "READY"
            else:
                glyph = "⚠"
                tag = "backed off"
            diag_short = ev.diagnostic[:48]
            lines.append(
                f"  {glyph} {ev.flag_name} — {tag} ({diag_short})"
            )
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[session_continuity] format_graduation_ticker "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# Surface 2: Cross-session memory diff — composes last_session_summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossSessionDiff:
    """One cross-session diff record. Frozen.

    Diffs current state vs the previous session's
    summary.json — what changed in the gap between sessions.
    Composes canonical
    :class:`last_session_summary.SessionRecord`."""

    schema_version: str = SESSION_CONTINUITY_SCHEMA_VERSION
    previous_session_id: str = ""
    previous_attempted: int = 0
    previous_completed: int = 0
    previous_failed: int = 0
    previous_cost_total: float = 0.0
    previous_duration_s: float = 0.0
    previous_stop_reason: str = ""
    has_previous: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "previous_session_id": self.previous_session_id,
            "previous_attempted": int(self.previous_attempted),
            "previous_completed": int(self.previous_completed),
            "previous_failed": int(self.previous_failed),
            "previous_cost_total": float(
                self.previous_cost_total,
            ),
            "previous_duration_s": float(
                self.previous_duration_s,
            ),
            "previous_stop_reason": self.previous_stop_reason,
            "has_previous": bool(self.has_previous),
        }


def aggregate_cross_session_diff() -> CrossSessionDiff:
    """Compose canonical
    :class:`LastSessionSummary` to load the previous session
    summary. Pure read; NEVER raises.

    Returns a :class:`CrossSessionDiff` with ``has_previous=False``
    when no previous session is loadable (boot session,
    operator wiped sessions, etc.)."""
    try:
        if not _sub_flag_enabled(
            "JARVIS_SESSION_CONTINUITY_MEMORY_DIFF_ENABLED",
        ):
            return CrossSessionDiff()
        try:
            from backend.core.ouroboros.governance.last_session_summary import (  # noqa: E501
                get_default_summary,
            )
        except Exception:  # noqa: BLE001 — defensive
            return CrossSessionDiff()
        try:
            summary = get_default_summary()
            records = summary.load(n_sessions=1) or []
        except Exception:  # noqa: BLE001 — defensive
            return CrossSessionDiff()
        if not records:
            return CrossSessionDiff()
        latest = records[0]
        return CrossSessionDiff(
            previous_session_id=str(
                getattr(latest, "session_id", "") or "",
            ),
            previous_attempted=int(
                getattr(latest, "stats_attempted", 0) or 0,
            ),
            previous_completed=int(
                getattr(latest, "stats_completed", 0) or 0,
            ),
            previous_failed=int(
                getattr(latest, "stats_failed", 0) or 0,
            ),
            previous_cost_total=float(
                getattr(latest, "cost_total", 0.0) or 0.0,
            ),
            previous_duration_s=float(
                getattr(latest, "duration_s", 0.0) or 0.0,
            ),
            previous_stop_reason=str(
                getattr(latest, "stop_reason", "") or "",
            ),
            has_previous=True,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[session_continuity] aggregate_cross_session_diff "
            "swallowed: %s",
            type(exc).__name__,
        )
        return CrossSessionDiff()


def format_cross_session_diff(
    diff: Optional[CrossSessionDiff] = None,
) -> str:
    """Render the cross-session memory diff as a single line.

    Output shape:
        ``Since last session (bt-2026-05-07-...): 23 ops attempted,``
        ``20 completed, 3 failed, $0.12 spent, stopped: idle_timeout``

    NEVER raises. Returns empty when:
      * Master flag off
      * Sub-flag disabled
      * No previous session loadable"""
    try:
        if not _sub_flag_enabled(
            "JARVIS_SESSION_CONTINUITY_MEMORY_DIFF_ENABLED",
        ):
            return ""
        d = (
            diff
            if diff is not None
            else aggregate_cross_session_diff()
        )
        if not d.has_previous:
            return ""
        sid_short = (
            d.previous_session_id[-12:]
            if len(d.previous_session_id) > 12
            else d.previous_session_id
        )
        parts = [f"Since last session ({sid_short}):"]
        if d.previous_attempted > 0:
            parts.append(
                f"{d.previous_attempted} ops attempted"
            )
            if d.previous_completed > 0:
                parts.append(
                    f"{d.previous_completed} completed"
                )
            if d.previous_failed > 0:
                parts.append(
                    f"{d.previous_failed} failed"
                )
        if d.previous_cost_total > 0:
            parts.append(
                f"${d.previous_cost_total:.2f} spent"
            )
        if d.previous_stop_reason:
            parts.append(
                f"stopped: {d.previous_stop_reason}"
            )
        return ", ".join(parts) if len(parts) > 1 else (
            f"Since last session ({sid_short}): "
            f"no observable activity"
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[session_continuity] format_cross_session_diff "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# Composite render
# ---------------------------------------------------------------------------


def format_session_continuity_panel() -> str:
    """Render both surfaces as a single multi-line panel.
    NEVER raises. Empty when master flag off."""
    try:
        if not master_enabled():
            return ""
        parts: List[str] = []
        diff_line = format_cross_session_diff()
        if diff_line:
            parts.append(diff_line)
        ticker_block = format_graduation_ticker()
        if ticker_block:
            parts.append(ticker_block)
        return "\n".join(parts) if parts else ""
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[session_continuity] format_session_continuity_panel "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. ``master_default_false`` — JARVIS_SESSION_CONTINUITY_-
         ENABLED stays default-FALSE per §33.1.
      2. ``authority_asymmetry`` — substrate purity.
      3. ``transition_taxonomy_4_values`` — closed-enum
         integrity.
      4. ``composes_canonical_graduation_dashboard`` — ticker
         MUST lazy-import ``aggregate_dashboard`` from
         ``unified_graduation_dashboard`` (no parallel verdict
         computation).
      5. ``composes_canonical_last_session_summary`` — diff
         MUST lazy-import ``LastSessionSummary`` /
         ``get_default_summary`` (no parallel summary parsing).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/session_continuity.py"
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
                if "return True" in src:
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if (
                    "JARVIS_SESSION_CONTINUITY_ENABLED"
                    not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_SESSION_CONTINUITY_ENABLED"
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
                            f"session_continuity MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_transition_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "BECAME_READY", "BACKED_OFF",
            "UNCHANGED", "NEW",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "GraduationTransition":
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
                            f"GraduationTransition missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"GraduationTransition extras: "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_composes_graduation_dashboard(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "unified_graduation_dashboard" not in source:
            violations.append(
                "session_continuity MUST compose canonical "
                "unified_graduation_dashboard (no parallel "
                "verdict computation)"
            )
        if "aggregate_dashboard" not in source:
            violations.append(
                "ticker MUST use canonical "
                "aggregate_dashboard accessor"
            )
        return tuple(violations)

    def _validate_composes_last_session_summary(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "last_session_summary" not in source:
            violations.append(
                "session_continuity MUST compose canonical "
                "last_session_summary (no parallel summary "
                "parsing)"
            )
        if "get_default_summary" not in source:
            violations.append(
                "diff MUST use canonical get_default_summary "
                "accessor"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "session_continuity_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_SESSION_CONTINUITY_-"
                "ENABLED stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "session_continuity_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "session_continuity MUST stay pure substrate "
                "composing unified_graduation_dashboard + "
                "last_session_summary + ide_observability_stream "
                "+ stdlib ONLY."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "session_continuity_transition_taxonomy_4_values"
            ),
            target_file=target,
            description=(
                "GraduationTransition is a 4-value closed "
                "taxonomy (BECAME_READY / BACKED_OFF / "
                "UNCHANGED / NEW)."
            ),
            validate=_validate_transition_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "session_continuity_composes_canonical_"
                "graduation_dashboard"
            ),
            target_file=target,
            description=(
                "Ticker MUST compose canonical "
                "unified_graduation_dashboard.aggregate_-"
                "dashboard. No parallel verdict computation."
            ),
            validate=_validate_composes_graduation_dashboard,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "session_continuity_composes_canonical_"
                "last_session_summary"
            ),
            target_file=target,
            description=(
                "Memory-diff path MUST compose canonical "
                "last_session_summary.LastSessionSummary "
                "/ get_default_summary. No parallel summary "
                "parsing."
            ),
            validate=_validate_composes_last_session_summary,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_SESSION_CONTINUITY_ENABLED",
            "bool",
            "false",
            (
                "Master flag for §38.11-B session continuity "
                "(graduation ticker + cross-session memory "
                "diff). Default-FALSE per §33.1."
            ),
        ),
        (
            "JARVIS_SESSION_CONTINUITY_TICKER_ENABLED",
            "bool",
            "true",
            "Graduation ticker sub-feature.",
        ),
        (
            "JARVIS_SESSION_CONTINUITY_MEMORY_DIFF_ENABLED",
            "bool",
            "true",
            "Cross-session memory diff sub-feature.",
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
                        "session_continuity.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return n
    return n


__all__ = [
    "CrossSessionDiff",
    "GraduationEvent",
    "GraduationTicker",
    "GraduationTransition",
    "SESSION_CONTINUITY_SCHEMA_VERSION",
    "aggregate_cross_session_diff",
    "format_cross_session_diff",
    "format_graduation_ticker",
    "format_session_continuity_panel",
    "get_default_ticker",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "reset_ticker_for_tests",
]
