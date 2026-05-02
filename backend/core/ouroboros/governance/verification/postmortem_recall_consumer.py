"""Priority #2 Slice 4 — Recurrence consumer.

Activates Priority #1 Slice 4's currently-dormant
``INJECT_POSTMORTEM_RECALL_HINT`` advisory action. When Priority
#1's coherence auditor detects ``RECURRENCE_DRIFT`` (same
failure_class postmortem appears >threshold times in window) it
writes an advisory to ``.jarvis/coherence_advisory.jsonl``.
Without a consumer those advisories are operator-readable but
operationally inert. Slice 4 closes the loop: the consumer reads
those advisories, extracts the matched failure_class, and
produces a ``RecurrenceBoost`` that extends the recall budget
for the next-N-ops on that failure_class.

Effect: when recurrence drift is detected, the next op on a
matching failure_class sees MORE prior-failure context in the
``## Recent Failures (advisory)`` prompt section (Slice 3) —
biasing the model toward the prior remediation patterns and
away from the recurring failure mode.

Composition with the existing pipeline:

  1. Priority #1 coherence auditor detects ``RECURRENCE_DRIFT``
     finding via ``compute_behavioral_drift``.
  2. Priority #1 Slice 4 ``coherence_action_bridge`` writes a
     ``CoherenceAdvisory`` with action=
     ``INJECT_POSTMORTEM_RECALL_HINT`` and detail string carrying
     ``failure_class '<class>' appeared N times > budget M``.
  3. **Slice 4 of Priority #2 (this module)** reads the
     advisory log via ``read_coherence_advisories``, filters
     to recurrence-recall-hint advisories, applies TTL decay,
     extracts failure_class, and emits ``RecurrenceBoost``
     records.
  4. Slice 5 wires the boost into the orchestrator's
     CONTEXT_EXPANSION pipeline: the effective top-K passed to
     Slice 3's ``compose_for_op_context`` is extended by the
     active boost's ``boost_count`` (clamped to
     ``recall_top_k_ceiling()``).

**Cost contract preserved by construction**:

  * Consumer is read-only over ``.jarvis/coherence_advisory.jsonl``.
  * Boost adjustment is in-memory only — actual operator-tunable
    flag flips still require ``MetaAdaptationGovernor`` approval
    (Phase C cage rule).
  * No LLM calls. No additional generation amplification —
    boost just adjusts how many existing index records are
    rendered (each record was already going to be considered;
    boost includes more of them in the prompt).
  * AST-pinned: bridge MUST NOT import ``providers`` /
    ``doubleword_provider`` / ``urgency_router`` /
    ``candidate_generator`` (Slice 5 pin).

Source material — what we leverage (no duplication):

  * **Priority #1 Slice 4's canonical reader**:
    ``coherence_action_bridge.read_coherence_advisories`` —
    schema-tolerant, since_ts-filtered, drift_kind-filterable.
    AST-pinned via importfrom.
  * **Priority #1 Slice 4's CoherenceAdvisory + CoherenceAdvisory
    Action** — frozen advisory shape; closed-taxonomy enum value
    ``INJECT_POSTMORTEM_RECALL_HINT`` already defined.
  * **Priority #1 Slice 1's BehavioralDriftKind** — closed-
    taxonomy enum; ``RECURRENCE_DRIFT`` is the kind that
    produces these advisories.
  * **adaptation.ledger.MonotonicTighteningVerdict** — Phase C
    universal cage rule canonical strings. Every emitted boost
    stamps ``MonotonicTighteningVerdict.PASSED.value`` (boost is
    a TIGHTENING — increases recall budget, model sees more
    context, decisions become more constrained).
  * **Slice 1's recall_top_k_ceiling** — absolute cap for
    boost-extended top-K. Operator-bounded by construction.

Direct-solve principles:

  * **Asynchronous-ready** — sync API; Slice 5 will wrap via
    ``asyncio.to_thread``.

  * **Dynamic** — TTL hours + max boost count + sub-gate all
    env-tunable with floor + ceiling clamps.

  * **Adaptive** — TTL decay automatic: advisories older than
    ``boost_ttl_hours()`` are silently excluded. Multiple
    advisories for same failure_class within window collapse
    to a single boost (max boost_count taken).

  * **Intelligent** — failure_class extracted from advisory
    detail via dedicated regex (NOT generic dict-repr eval —
    same defense-in-depth pattern as Slice 2). Brittle by
    intent: malformed detail → no boost emitted (skeleton
    pipeline still ships).

  * **Robust** — every public function NEVER raises. Disabled /
    empty / corrupt advisory log all map to empty boost
    mapping. The orchestrator NEVER sees a raise from this
    module.

  * **No hardcoding** — TTL hours + max boost count + ceiling
    via env knobs; failure_class regex is module-level constant
    (auditable).

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + Slice 1 (``postmortem_recall``) +
    Priority #1 Slice 4 (``coherence_action_bridge``) +
    Priority #1 Slice 1 (``coherence_auditor``) +
    ``adaptation.ledger`` (``MonotonicTighteningVerdict`` ONLY)
    ONLY.
  * MUST reference ``MonotonicTighteningVerdict`` (Phase C
    universal cage rule integration).
  * MUST reference ``read_coherence_advisories`` (canonical
    reader reuse from Priority #1 Slice 4).
  * MUST reference ``INJECT_POSTMORTEM_RECALL_HINT`` (filter
    target — catches refactor that drops the action filter).
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * No mutation tools.
  * No bare eval-family calls.
  * No async (Slice 5 wraps via to_thread).
"""
from __future__ import annotations

import enum
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    Mapping,
    Optional,
)

from backend.core.ouroboros.governance.adaptation.ledger import (
    MonotonicTighteningVerdict,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (
    CoherenceAdvisory,
    CoherenceAdvisoryAction,
    read_coherence_advisories,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (
    BehavioralDriftKind,
)
from backend.core.ouroboros.governance.verification.postmortem_recall import (
    recall_top_k,
    recall_top_k_ceiling,
)

logger = logging.getLogger(__name__)


POSTMORTEM_RECALL_CONSUMER_SCHEMA_VERSION: str = (
    "postmortem_recall_consumer.1"
)


# ---------------------------------------------------------------------------
# Sub-gate flag
# ---------------------------------------------------------------------------


def postmortem_recurrence_boost_enabled() -> bool:
    """``JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED`` (default
    ``true`` post Slice 5 graduation 2026-05-01).

    Sub-gate for the recurrence consumer. When false,
    ``get_active_recurrence_boosts`` returns empty mapping and
    ``compute_effective_top_k`` returns base_top_k unchanged.
    Master flag (``JARVIS_POSTMORTEM_RECALL_ENABLED``) must also
    be true."""
    raw = os.environ.get(
        "JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-01 (Priority #2 Slice 5)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def boost_ttl_hours() -> float:
    """``JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS`` (default 6.0,
    floor 1.0, ceiling 168.0).

    Time-to-live for an INJECT_POSTMORTEM_RECALL_HINT advisory:
    advisories older than this are silently excluded from boost
    computation. Keeps the boost short-window responsive (~6h)
    while ceiling caps stale advisories at 1 week."""
    return _env_float_clamped(
        "JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS",
        6.0, floor=1.0, ceiling=168.0,
    )


def boost_max_count() -> int:
    """``JARVIS_POSTMORTEM_RECALL_BOOST_MAX_COUNT`` (default 5,
    floor 1, ceiling 20).

    Maximum number of additional records the boost can request.
    Combined with ``recall_top_k_ceiling`` (Slice 1) the
    effective top-K cannot exceed ceiling regardless of boost
    magnitude — operator-bounded by construction."""
    return _env_int_clamped(
        "JARVIS_POSTMORTEM_RECALL_BOOST_MAX_COUNT",
        5, floor=1, ceiling=20,
    )


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of consumer outcomes (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class RecurrenceBoostStatus(str, enum.Enum):
    """5-value closed taxonomy. Used in observability +
    diagnostics — boost mapping presence is the active signal,
    but operators querying via Slice 5 surfaces see the explicit
    status string.

    ``ACTIVE``      — advisory in TTL window; boost applied.
    ``EXPIRED``     — advisory exists but TTL passed; no boost.
    ``DISABLED``    — sub-gate off; no boost.
    ``NO_ADVISORY`` — no INJECT_POSTMORTEM_RECALL_HINT advisory
                      found in advisory log.
    ``FAILED``      — defensive sentinel."""

    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"
    NO_ADVISORY = "no_advisory"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Frozen RecurrenceBoost dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecurrenceBoost:
    """One per-failure_class boost record. Frozen for safe
    propagation. ``monotonic_tightening_verdict`` is the canonical
    string from ``adaptation.ledger.MonotonicTighteningVerdict``
    — every emitted boost stamps ``PASSED`` because increasing
    recall budget IS a tightening (more context → more
    constrained decisions)."""

    failure_class: str
    boost_count: int
    expires_at: float
    source_advisory_id: str
    monotonic_tightening_verdict: str = (
        MonotonicTighteningVerdict.PASSED.value
    )
    schema_version: str = (
        POSTMORTEM_RECALL_CONSUMER_SCHEMA_VERSION
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "boost_count": self.boost_count,
            "expires_at": self.expires_at,
            "source_advisory_id": self.source_advisory_id,
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }

    def is_active(self, *, now_ts: float) -> bool:
        """True iff ``now_ts < expires_at``. NEVER raises."""
        try:
            return float(now_ts) < float(self.expires_at)
        except Exception:  # noqa: BLE001 — defensive
            return False


# ---------------------------------------------------------------------------
# Internal: failure_class extraction from advisory.detail
# ---------------------------------------------------------------------------


# Format produced by Priority #1 Slice 1's compute_behavioral_drift
# for RECURRENCE_DRIFT findings:
#   detail = f"failure_class {failure_class!r} appeared {count}
#             times > budget {b.recurrence_count}"
# where {failure_class!r} is the Python repr of the string,
# producing single-quoted literal: 'timeout_failure'.
#
# Dedicated regex extractor (NOT generic dict-repr evaluator) —
# same defense-in-depth pattern as Slice 2.
_FAILURE_CLASS_RE = re.compile(
    r"failure_class\s+'((?:[^'\\]|\\.)*)'",
)


def _extract_failure_class(detail: str) -> str:
    """Extract the Python-repr-quoted failure_class from a
    RECURRENCE_DRIFT detail string. Returns empty string on no
    match. NEVER raises."""
    try:
        if not detail:
            return ""
        m = _FAILURE_CLASS_RE.search(detail)
        if m is None:
            return ""
        return m.group(1) or ""
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Antivenom Vector 3: failure_class semantic plausibility
# ---------------------------------------------------------------------------


# Governance-originated failure classes that ALWAYS exist. Sourced
# from the real production emitters (every ``failure_class="…"``
# string used inside ``backend/core/ouroboros/governance/``) plus
# the legacy *_failure aliases retained for forward compatibility.
# Operators extend via ``JARVIS_KNOWN_FAILURE_CLASSES`` env CSV.
_CORE_FAILURE_CLASSES: frozenset = frozenset({
    # Phase 2A canonical (ValidationResult.failure_class)
    "test", "build", "infra", "budget", "none",
    # Sub-classes emitted by validators / change_engine / orchestrator
    "ascii", "cancelled", "content", "cost_contract_violation",
    "dep_file_rename", "diff_apply", "duplication", "env",
    "exploration", "failed", "json_parse", "multi_file_coverage",
    "rollback", "schema", "security", "worktree_isolation",
    # Legacy *_failure aliases (kept for FailureEpisode back-compat)
    "timeout_failure", "validation_failure", "generation_failure",
    "apply_failure", "verify_failure", "parse_failure",
    "cost_limit_failure", "context_overflow", "tool_loop_failure",
    "provider_error", "stream_rupture",
    # Test-fixture aliases used in regression suites
    "test_failure",
})


def _advisory_plausibility_enabled() -> bool:
    """``JARVIS_ADVISORY_PLAUSIBILITY_CHECK_ENABLED`` (default
    ``true``). Kill switch for failure_class semantic plausibility
    validation. Explicit ``false`` disables."""
    raw = os.environ.get(
        "JARVIS_ADVISORY_PLAUSIBILITY_CHECK_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def _build_known_failure_classes() -> frozenset:
    """Aggregate known failure classes from:

      1. ``_CORE_FAILURE_CLASSES`` (production-derived structural floor)
      2. ``JARVIS_KNOWN_FAILURE_CLASSES`` env CSV (operator-extensible)

    Returns a frozenset of all known failure class strings. The CSV
    knob is the only operator-controlled extension surface — keeps
    the AST authority invariant clean (no test-tree imports from
    governance code). NEVER raises."""
    try:
        classes: set = set(_CORE_FAILURE_CLASSES)

        # Operator-extensible via env CSV.
        env_csv = os.environ.get(
            "JARVIS_KNOWN_FAILURE_CLASSES", "",
        ).strip()
        if env_csv:
            for c in env_csv.split(","):
                c = c.strip()
                if c:
                    classes.add(c)

        return frozenset(classes)
    except Exception:  # noqa: BLE001 — defensive
        return _CORE_FAILURE_CLASSES


# ---------------------------------------------------------------------------
# Public: compute_recurrence_boosts
# ---------------------------------------------------------------------------


def compute_recurrence_boosts(
    advisories: Iterable[CoherenceAdvisory],
    *,
    ttl_hours: Optional[float] = None,
    max_count: Optional[int] = None,
    now_ts: Optional[float] = None,
) -> Mapping[str, RecurrenceBoost]:
    """Pure decision: groups INJECT_POSTMORTEM_RECALL_HINT
    advisories by extracted failure_class, applies TTL decay,
    computes boost count, stamps Phase C canonical verdict.
    NEVER raises.

    Decision tree:

      1. Filter advisories to ``action ==
         INJECT_POSTMORTEM_RECALL_HINT``.
      2. Filter advisories to ``drift_kind ==
         RECURRENCE_DRIFT`` (defense in depth — Priority #1 only
         emits this combination, but pin in case schema drifts).
      3. Extract failure_class from each advisory's detail via
         ``_extract_failure_class``. Skip records with empty
         extraction.
      4. Apply TTL filter: skip advisories older than
         ``ttl_hours()`` from now_ts.
      5. Group by failure_class; boost_count = number of valid
         advisories in window for that class, clamped to
         ``max_count``.
      6. ``expires_at`` = newest advisory's recorded_at_ts +
         ttl_hours × 3600.
      7. Stamp ``MonotonicTighteningVerdict.PASSED`` on every
         emitted boost."""
    try:
        if advisories is None:
            return {}
        eff_ttl = (
            float(ttl_hours) if ttl_hours is not None
            else boost_ttl_hours()
        )
        eff_max = (
            int(max_count) if max_count is not None
            else boost_max_count()
        )
        # Defensive clamp — caller may pass < floor
        eff_max = max(1, eff_max)
        import time as _time
        ref_ts = (
            float(now_ts) if now_ts is not None
            else _time.time()
        )
        cutoff_ts = ref_ts - (eff_ttl * 3600.0)

        # Group by failure_class
        per_class: Dict[str, list] = {}
        for adv in advisories:
            try:
                if not isinstance(adv, CoherenceAdvisory):
                    continue
                # Filter by action AND drift_kind (defense in
                # depth)
                if adv.action is not (
                    CoherenceAdvisoryAction
                    .INJECT_POSTMORTEM_RECALL_HINT
                ):
                    continue
                if (
                    adv.drift_kind
                    is not BehavioralDriftKind.RECURRENCE_DRIFT
                ):
                    continue
                # TTL filter
                if adv.recorded_at_ts < cutoff_ts:
                    continue
                # Extract failure_class
                fc = _extract_failure_class(adv.detail)
                if not fc:
                    continue
                # Antivenom Vector 3: plausibility gate — cross-
                # reference against known failure classes.
                if _advisory_plausibility_enabled():
                    known = _build_known_failure_classes()
                    if fc not in known:
                        logger.warning(
                            "[PostmortemRecallConsumer] unknown "
                            "failure_class %r in advisory %s — "
                            "skipping (plausibility check)",
                            fc, adv.advisory_id,
                        )
                        continue
                if fc not in per_class:
                    per_class[fc] = []
                per_class[fc].append(adv)
            except Exception:  # noqa: BLE001 — per-advisory defensive
                continue

        # Build per-class boost
        boosts: Dict[str, RecurrenceBoost] = {}
        for fc, advs in per_class.items():
            try:
                if not advs:
                    continue
                # boost_count = clamped count of advisories in
                # window
                boost_count = min(eff_max, len(advs))
                if boost_count < 1:
                    continue
                # expires_at = newest advisory ts + ttl
                newest_ts = max(a.recorded_at_ts for a in advs)
                expires_at = newest_ts + (eff_ttl * 3600.0)
                # source_advisory_id = newest advisory's id
                newest_adv = max(
                    advs, key=lambda a: a.recorded_at_ts,
                )
                boosts[fc] = RecurrenceBoost(
                    failure_class=fc,
                    boost_count=boost_count,
                    expires_at=expires_at,
                    source_advisory_id=newest_adv.advisory_id,
                )
            except Exception:  # noqa: BLE001 — per-class defensive
                continue
        return boosts
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemRecallConsumer] compute_recurrence_boosts "
            "raised: %s", exc,
        )
        return {}


# ---------------------------------------------------------------------------
# Public: compute_effective_top_k
# ---------------------------------------------------------------------------


def compute_effective_top_k(
    boosts: Mapping[str, RecurrenceBoost],
    *,
    base_top_k: Optional[int] = None,
    target_failure_class: Optional[str] = None,
    now_ts: Optional[float] = None,
) -> int:
    """Apply the matching boost to base_top_k. NEVER raises.

    Decision tree:

      1. ``base_top_k`` defaults to ``recall_top_k()``.
      2. If ``target_failure_class`` non-None AND in boosts AND
         the matched boost is_active → return min(ceiling,
         base + boost.boost_count).
      3. If ``target_failure_class`` is None: take the maximum
         boost_count across all active boosts (any active boost
         indicates recurrence pressure — extend recall broadly).
      4. Otherwise → return base_top_k.

    Effective top-K is always clamped to
    ``recall_top_k_ceiling()`` — operator-bounded."""
    try:
        eff_base = (
            int(base_top_k) if base_top_k is not None
            else recall_top_k()
        )
        eff_base = max(1, eff_base)
        ceiling = recall_top_k_ceiling()
        # Defensive: ceiling should be >= base
        ceiling = max(ceiling, eff_base)

        if not boosts:
            return min(ceiling, eff_base)
        import time as _time
        ref_ts = (
            float(now_ts) if now_ts is not None
            else _time.time()
        )

        if target_failure_class is not None:
            boost = boosts.get(target_failure_class)
            if (
                boost is not None
                and boost.is_active(now_ts=ref_ts)
            ):
                return min(
                    ceiling, eff_base + int(boost.boost_count),
                )
            return min(ceiling, eff_base)

        # target_failure_class is None — take max active boost
        active_counts = [
            b.boost_count for b in boosts.values()
            if b.is_active(now_ts=ref_ts)
        ]
        if not active_counts:
            return min(ceiling, eff_base)
        max_boost = max(active_counts)
        return min(ceiling, eff_base + int(max_boost))
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemRecallConsumer] compute_effective_top_k "
            "raised: %s", exc,
        )
        # Defensive fall-through: return base_top_k unchanged
        try:
            if base_top_k is not None:
                return max(1, int(base_top_k))
        except Exception:  # noqa: BLE001 — defensive
            pass
        try:
            return recall_top_k()
        except Exception:  # noqa: BLE001 — defensive
            return 3


# ---------------------------------------------------------------------------
# Public: get_active_recurrence_boosts (high-level entry)
# ---------------------------------------------------------------------------


def get_active_recurrence_boosts(
    *,
    advisory_path: Optional[Path] = None,
    ttl_hours: Optional[float] = None,
    max_count: Optional[int] = None,
    now_ts: Optional[float] = None,
    enabled_override: Optional[bool] = None,
) -> Mapping[str, RecurrenceBoost]:
    """Read coherence_advisory.jsonl + compute boosts. High-
    level entry for Slice 5 orchestrator wiring. NEVER raises.

    Returns:
      * Mapping {failure_class: RecurrenceBoost} — active boosts.
      * Empty mapping when:
          - sub-gate off
          - master flag off
          - advisory file missing / empty
          - no INJECT_POSTMORTEM_RECALL_HINT advisories in TTL
          - any error (defensive fall-through)"""
    try:
        is_enabled = (
            enabled_override if enabled_override is not None
            else postmortem_recurrence_boost_enabled()
        )
        if not is_enabled:
            return {}
        # Master flag check — boost is part of the broader
        # PostmortemRecall arc, gated by master
        from backend.core.ouroboros.governance.verification.postmortem_recall import (  # noqa: E501
            postmortem_recall_enabled,
        )
        if not postmortem_recall_enabled():
            return {}

        eff_ttl = (
            float(ttl_hours) if ttl_hours is not None
            else boost_ttl_hours()
        )
        # Read advisories from Priority #1 Slice 4's canonical
        # reader. We use a since_ts cutoff matching our TTL so
        # we don't load the full history just to filter it.
        import time as _time
        ref_ts = (
            float(now_ts) if now_ts is not None
            else _time.time()
        )
        since_ts = ref_ts - (eff_ttl * 3600.0)
        try:
            advisories = read_coherence_advisories(
                since_ts=since_ts,
                path=advisory_path,
                drift_kind=(
                    BehavioralDriftKind.RECURRENCE_DRIFT
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[PostmortemRecallConsumer] read advisories "
                "raised: %s", exc,
            )
            return {}

        if not advisories:
            return {}

        return compute_recurrence_boosts(
            advisories,
            ttl_hours=eff_ttl,
            max_count=max_count,
            now_ts=ref_ts,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemRecallConsumer] get_active_boosts "
            "raised: %s", exc,
        )
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration for the V3 (Coherence
    advisory plausibility) Antivenom-v2 surface.

    Discovery contract: the seed loader walks ``verification/`` for
    modules exposing this name + invokes once at boot.

    Returns count of FlagSpecs registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0
    specs = [
        FlagSpec(
            name="JARVIS_KNOWN_FAILURE_CLASSES",
            type=FlagType.STR, default="",
            description=(
                "Antivenom V3 — comma-separated list of operator-"
                "supplied failure-class names that extend the "
                "structural ``_CORE_FAILURE_CLASSES`` (28 entries "
                "derived from real production emitters). Empty = "
                "unset = use core list only. Operators add domain-"
                "specific failure classes here without editing the "
                "consumer module — the plausibility check accepts "
                "them on top of the structural floor. Closes a §29 "
                "schema-shape-gaming bypass vector by rejecting "
                "advisories whose extracted failure_class isn't in "
                "the union."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "postmortem_recall_consumer.py"
            ),
            example="my_custom_failure,another_class",
            since="Antivenom v2 (Priority #6)",
        ),
        FlagSpec(
            name="JARVIS_RECURRENCE_BOOST_PLAUSIBILITY_ENABLED",
            type=FlagType.BOOL, default=True,
            description=(
                "Sub-gate for the V3 plausibility check. When false, "
                "advisories pass without failure-class validation. "
                "Default true."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "postmortem_recall_consumer.py"
            ),
            example="true",
            since="Antivenom v2 (Priority #6)",
        ),
    ]
    try:
        registry.bulk_register(specs, override=True)
    except Exception:  # noqa: BLE001 — defensive
        return 0
    return len(specs)


__all__ = [
    "POSTMORTEM_RECALL_CONSUMER_SCHEMA_VERSION",
    "RecurrenceBoost",
    "RecurrenceBoostStatus",
    "boost_max_count",
    "boost_ttl_hours",
    "compute_effective_top_k",
    "compute_recurrence_boosts",
    "get_active_recurrence_boosts",
    "postmortem_recurrence_boost_enabled",
    "register_flags",
]
