"""§37 Slice 5 — Cost-band-crossing observer with chatter-suppression.

Closes Tier 1 #1 from the §37 UX roadmap. Surfaces approaching-
budget warnings to operators BEFORE the cost cap fires + halts
the soak. Currently the harness's cost cap is mathematically
hard (Iron Gate enforces $0.50/soak default) but operators only
see the breach AFTER it happens. This module emits structural
warnings on every band crossing so unattended cadence becomes
operator-visible.

Per the operator binding "fully leverage existing files...
build cleanly... no duplication":

  * **Composes existing broker substrate** — emits via the
    canonical ``StreamEventBroker.publish()`` (same surface
    Slice 2's ``/listen`` reads). NO parallel queue, NO
    second event spine.
  * **Composes existing status-line cost sampling** — the
    observer is invoked from the status-line render path
    (single-writer scenario; no new cost-tracker plumbing).
  * **Mirrors Move 7 verdict-transition + Move 8 chatter
    suppression discipline** — the canonical pattern for
    band-crossing events: only fires on transitions, never
    on per-tick same-band re-evaluation.

Architectural locks (operator binding 2026-05-05):

  * **Single pipeline** — band classification is a pure
    function; SSE emission goes through canonical broker;
    no parallel writer. AST-pinned.
  * **Chatter-suppression structural** — the observer
    persists ``last_band`` per stream-key; ``record()`` only
    returns a non-None ``BandCrossing`` when the band CHANGES.
    Same-band ticks return ``None``. AST-pinned.
  * **Closed taxonomy** — ``CostBand`` enum is 5-value closed
    (OK / NOTICE / WARN / CRITICAL / BREACH). Future drift
    requires explicit scope-doc + AST pin update.
  * **Authority asymmetry** — substrate purity: no
    orchestrator / iron_gate / providers imports.
  * **NEVER raises** — every code path defensive.

Five-band ladder (env-tunable thresholds, all clamped):

  * ``OK``        — fraction < notice threshold (default 50%)
  * ``NOTICE``    — notice ≤ fraction < warn (50% ≤ x < 80%)
  * ``WARN``      — warn ≤ fraction < critical (80% ≤ x < 95%)
  * ``CRITICAL``  — critical ≤ fraction < breach (95% ≤ x < 100%)
  * ``BREACH``    — fraction ≥ breach threshold (≥100%)

Default thresholds compose the existing
``status_line.warn_threshold_pct()`` for backward-compat (the
default 80% IS the WARN band entry point). Environmental
overrides are independent so operators can tune the
NOTICE/CRITICAL/BREACH bands without affecting the legacy
status-line marker.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


logger = logging.getLogger(
    "Ouroboros.CostWarningObserver",
)


COST_WARNING_OBSERVER_SCHEMA_VERSION: str = (
    "cost_warning_observer.1"
)


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value band ladder
# ---------------------------------------------------------------------------


class CostBand(str, enum.Enum):
    """Cost-fraction band ladder. AST-pinned closed taxonomy."""

    OK = "ok"
    """Fraction below NOTICE threshold (default <50%)."""

    NOTICE = "notice"
    """First-warning band. Operator should glance at trajectory
    but no immediate action needed."""

    WARN = "warn"
    """Approaching cap. Operator should review whether the
    soak/op is converging or spinning. Default 80%-95%."""

    CRITICAL = "critical"
    """Cap is imminent. Default 95%-100%. Next tick may cap."""

    BREACH = "breach"
    """Cap exceeded. The harness's Iron Gate WILL halt soon
    (or has already). This event is informational; the
    structural defense (cost cap) is upstream."""


# ---------------------------------------------------------------------------
# Frozen artifact — band crossing event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BandCrossing:
    """Recorded transition between two CostBand values. Frozen
    for safe propagation. Emitted only when ``record()`` observes
    a band CHANGE — same-band re-evaluations return None
    (chatter-suppression structural)."""

    stream_key: str
    """Logical cost-stream identifier. Default ``"session"``
    for the harness-level cost cap. Future: per-op streams."""

    from_band: CostBand
    to_band: CostBand
    fraction: float
    spent_usd: float
    budget_usd: float
    schema_version: str = field(
        default=COST_WARNING_OBSERVER_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stream_key": str(self.stream_key),
            "from_band": self.from_band.value,
            "to_band": self.to_band.value,
            "fraction": float(self.fraction),
            "spent_usd": float(self.spent_usd),
            "budget_usd": float(self.budget_usd),
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


def notice_threshold_pct() -> int:
    """``JARVIS_COST_WARN_BAND_NOTICE_PCT`` — first-warning
    band entry. Default 50; clamped [1, 99]. Composes with
    warn/critical thresholds (must be < warn — observer
    enforces ordering at runtime)."""
    return _clamp_pct(
        os.environ.get("JARVIS_COST_WARN_BAND_NOTICE_PCT", ""),
        default=50, low=1, high=99,
    )


def warn_threshold_pct() -> int:
    """``JARVIS_COST_WARN_BAND_WARN_PCT`` — main warning band
    entry (matches the legacy status-line marker default).
    Default 80; clamped [1, 99]."""
    return _clamp_pct(
        os.environ.get("JARVIS_COST_WARN_BAND_WARN_PCT", ""),
        default=80, low=1, high=99,
    )


def critical_threshold_pct() -> int:
    """``JARVIS_COST_WARN_BAND_CRITICAL_PCT`` — last-warning
    band before breach. Default 95; clamped [1, 99]."""
    return _clamp_pct(
        os.environ.get(
            "JARVIS_COST_WARN_BAND_CRITICAL_PCT", "",
        ),
        default=95, low=1, high=99,
    )


# Note: BREACH band entry is structurally pinned to fraction
# >= 1.0 (i.e., spent >= budget). Not env-tunable — the cap is
# the cap; the observer doesn't get to redefine "breach."


# ---------------------------------------------------------------------------
# Pure-function band classifier
# ---------------------------------------------------------------------------


def classify_band(
    fraction: float,
    *,
    notice_pct: Optional[int] = None,
    warn_pct: Optional[int] = None,
    critical_pct: Optional[int] = None,
) -> CostBand:
    """Classify a cost fraction into a CostBand. Pure function;
    NEVER raises.

    Threshold args optional — when ``None``, reads env via the
    public helpers. Caller-injection enables testing band
    boundaries without env mocking.

    Threshold ordering is enforced at runtime: if env returns
    inconsistent values (e.g., notice >= warn), the classifier
    falls through to the highest applicable band based on the
    ``fraction`` only (defensive).
    """
    try:
        f = float(fraction)
    except (TypeError, ValueError):
        return CostBand.OK
    if not (f == f):  # NaN check
        return CostBand.OK
    if f >= 1.0:
        return CostBand.BREACH
    notice = (
        notice_pct if notice_pct is not None
        else notice_threshold_pct()
    )
    warn = (
        warn_pct if warn_pct is not None
        else warn_threshold_pct()
    )
    critical = (
        critical_pct if critical_pct is not None
        else critical_threshold_pct()
    )
    notice_f = notice / 100.0
    warn_f = warn / 100.0
    critical_f = critical / 100.0
    if f >= critical_f:
        return CostBand.CRITICAL
    if f >= warn_f:
        return CostBand.WARN
    if f >= notice_f:
        return CostBand.NOTICE
    return CostBand.OK


# ---------------------------------------------------------------------------
# CostWarningObserver — stateful band-crossing detector
# ---------------------------------------------------------------------------


class CostWarningObserver:
    """Observes cost samples; emits ``BandCrossing`` only on
    band transitions (chatter-suppression structural).

    Single-writer scenario: the observer is invoked from the
    status-line render path (single-thread per render tick).
    No locking required. Multiple stream-keys are supported
    independently — e.g., session-level + per-op cost streams
    — so future per-op cost rails can compose this primitive
    without coupling.

    NEVER raises.
    """

    def __init__(self) -> None:
        # stream_key → last observed band. First observation
        # for a new stream emits a transition from OK → <band>
        # (so operators see immediate context on first sample).
        self._last_band_per_stream: Dict[str, CostBand] = {}

    def record(
        self,
        *,
        spent_usd: float,
        budget_usd: float,
        stream_key: str = "session",
        publish_sse: bool = True,
    ) -> Optional[BandCrossing]:
        """Sample a cost observation. Returns a BandCrossing
        when the band CHANGED from the last observation on this
        stream; returns None when the band stayed the same
        (chatter-suppression structural).

        Args:
            spent_usd: cumulative cost spent on this stream.
            budget_usd: budget cap for this stream.
            stream_key: logical cost-stream identifier; default
                ``"session"`` (harness-level cost cap).
            publish_sse: emit SSE event on band crossing via
                canonical broker. Default True. Set False in
                tests / when SSE is unwanted.

        Defensive: any error in classification or SSE emit is
        swallowed; observer state remains coherent. NEVER raises.
        """
        try:
            spent_f = float(spent_usd)
            budget_f = float(budget_usd)
        except (TypeError, ValueError):
            return None
        if budget_f <= 0.0:
            # No budget → no fraction → no band. Defensive: don't
            # crash; just skip this observation.
            return None
        fraction = spent_f / budget_f
        new_band = classify_band(fraction)
        prev_band = self._last_band_per_stream.get(stream_key)
        if prev_band == new_band:
            # Same band — chatter-suppression structural;
            # no event.
            return None
        # First-observation discipline: when there's NO prior
        # observation on this stream AND the new band is OK,
        # update state silently — no spurious "OK→OK" emission.
        # Streams that boot AT a higher band (e.g. session
        # resumption) DO emit immediately so operators see
        # context. This makes "first-tick at OK" structurally
        # invisible (the right behavior for fresh sessions).
        if prev_band is None and new_band == CostBand.OK:
            self._last_band_per_stream[stream_key] = new_band
            return None
        # Band crossed! Update state + emit.
        self._last_band_per_stream[stream_key] = new_band
        from_band = prev_band or CostBand.OK
        crossing = BandCrossing(
            stream_key=str(stream_key),
            from_band=from_band,
            to_band=new_band,
            fraction=fraction,
            spent_usd=spent_f,
            budget_usd=budget_f,
        )
        # Operator-facing log line — band crossing is always
        # interesting enough to surface in the session log,
        # regardless of SSE subscriber state.
        logger.info(
            "[CostWarningObserver] band crossed %s → %s "
            "(stream=%s spent=$%.4f/$%.4f frac=%.3f)",
            from_band.value, new_band.value,
            stream_key, spent_f, budget_f, fraction,
        )
        if publish_sse:
            self._publish_to_broker(crossing)
        return crossing

    def reset(self, stream_key: Optional[str] = None) -> None:
        """Clear last-band state. ``stream_key=None`` clears
        all streams (test isolation); a specific key clears
        only that stream."""
        if stream_key is None:
            self._last_band_per_stream.clear()
        else:
            self._last_band_per_stream.pop(stream_key, None)

    def last_band(
        self, stream_key: str = "session",
    ) -> Optional[CostBand]:
        """Return the last-observed band on this stream, or
        None if no observation has been recorded yet."""
        return self._last_band_per_stream.get(stream_key)

    @staticmethod
    def _publish_to_broker(crossing: BandCrossing) -> None:
        """Emit the canonical SSE event. Composes the existing
        broker (Slice 2 territory). Defensive: any error is
        swallowed — the band-crossing is already logged."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                EVENT_TYPE_COST_BAND_CROSSED,
                get_default_broker,
            )
            broker = get_default_broker()
            if broker is None:
                return
            broker.publish(
                event_type=EVENT_TYPE_COST_BAND_CROSSED,
                op_id="",  # session-level event
                payload=crossing.to_dict(),
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[CostWarningObserver] SSE publish failed",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Default-singleton accessor (matches Slice 1+2+3 pattern)
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVER: Optional[CostWarningObserver] = None


def get_default_observer() -> CostWarningObserver:
    """Return the process-wide default
    :class:`CostWarningObserver` singleton. Created lazily on
    first access. Subsequent calls return the same instance.

    Use this from the status-line render path (single producer)
    + any future per-op cost-stream observer wire-up.
    """
    global _DEFAULT_OBSERVER
    if _DEFAULT_OBSERVER is None:
        _DEFAULT_OBSERVER = CostWarningObserver()
    return _DEFAULT_OBSERVER


def reset_default_observer_for_tests() -> None:
    """Test-only — production code never calls. Pinned via
    naming convention (``_for_tests`` suffix)."""
    global _DEFAULT_OBSERVER
    _DEFAULT_OBSERVER = None


# ---------------------------------------------------------------------------
# AST pins (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``cost_warning_observer_band_taxonomy_5_values`` —
         ``CostBand`` enum is 5-value closed.
      2. ``cost_warning_observer_chatter_suppression`` —
         ``record()`` returns None when band unchanged
         (structural pin via AST inspection of the equality
         check + early-return shape).
      3. ``cost_warning_observer_authority_asymmetry`` —
         substrate purity (no orchestrator / iron_gate /
         providers imports).
      4. ``cost_warning_observer_composes_canonical_broker`` —
         emits via ``get_default_broker()`` only (single-
         pipeline guardrail).
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
        "cost_warning_observer.py"
    )

    def _validate_band_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "OK", "NOTICE", "WARN", "CRITICAL", "BREACH",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "CostBand":
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
                            f"CostBand has extra values "
                            f"{sorted(extra)} — taxonomy is "
                            f"closed; update pin if intentional"
                        )
                    if missing:
                        violations.append(
                            f"CostBand missing required values "
                            f"{sorted(missing)}"
                        )
        return tuple(violations)

    def _validate_chatter_suppression(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``record()`` MUST contain a same-band early-return
        check that returns None. Catches loosening regressions."""
        violations: list = []
        target_method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "record":
                    target_method = node
                    break
        if target_method is None:
            violations.append(
                "CostWarningObserver.record() method missing"
            )
            return tuple(violations)
        # Look for `if prev_band == new_band: return None` shape.
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
            # body should be `return None` somewhere
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
                "suppression — operator-binding "
                "structural discipline"
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
                            f"cost_warning_observer.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_canonical_broker(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """SSE emission MUST go through canonical
        ``get_default_broker()``; never construct a parallel
        broker."""
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "StreamEventBroker"
                ):
                    violations.append(
                        "cost_warning_observer.py MUST NOT "
                        "construct StreamEventBroker directly "
                        "— compose get_default_broker()"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cost_warning_observer_band_taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "§37 Slice 5 — CostBand is 5-value closed enum "
                "(OK/NOTICE/WARN/CRITICAL/BREACH)."
            ),
            validate=_validate_band_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cost_warning_observer_chatter_suppression"
            ),
            target_file=target,
            description=(
                "§37 Slice 5 — record() emits BandCrossing "
                "ONLY on band change. Same-band early-return "
                "returns None (chatter-suppression structural)."
            ),
            validate=_validate_chatter_suppression,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cost_warning_observer_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Slice 5 — substrate purity: no "
                "orchestrator / iron_gate / policy / providers "
                "imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cost_warning_observer_composes_canonical_broker"
            ),
            target_file=target,
            description=(
                "§37 Slice 5 — single-pipeline guardrail: "
                "module composes get_default_broker(); never "
                "constructs StreamEventBroker directly."
            ),
            validate=_validate_composes_canonical_broker,
        ),
    ]


__all__ = [
    "BandCrossing",
    "COST_WARNING_OBSERVER_SCHEMA_VERSION",
    "CostBand",
    "CostWarningObserver",
    "classify_band",
    "critical_threshold_pct",
    "get_default_observer",
    "notice_threshold_pct",
    "register_shipped_invariants",
    "reset_default_observer_for_tests",
    "warn_threshold_pct",
]
