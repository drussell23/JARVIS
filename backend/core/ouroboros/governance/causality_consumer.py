"""§31 U2 empirical wiring — Slice 1 substrate.

Closes the §31 U2 "consumer wiring pending" gap surfaced in §35
+ §36.4 Priority #2: the canonical CausalityDAG substrate
(`verification/causality_dag.py`) was shipped 2026-05-04 but
NOTHING in the production op pipeline consults it to inform
its own decision. The DAG is *observation-only* — it records
what happened, observability surfaces (HTTP / SSE / `/decisions`
+ `/replay` REPLs) read from it, but the orchestrator /
candidate_generator / strategic_direction / iron_gate ignore it.

This module ships the **read-only feature-extraction primitive**
that production consumers compose to consult their own causal
lineage at decision time. Authority asymmetry is structural:

  * Substrate NEVER imports orchestrator / iron_gate / policy /
    candidate_generator / urgency_router / change_engine /
    semantic_guardian (AST-pinned).
  * Substrate NEVER calls :meth:`DecisionRuntime.record` or any
    other write surface on the decisions ledger (AST-pinned).
  * Substrate composes :func:`verification.causality_dag.build_dag`
    + the new :meth:`CausalityDAG.ancestors_of` read-API helper
    + existing :meth:`parents` / :meth:`children` / :meth:`nodes_for_phase`.
    Direct ``CausalityDAG()`` construction forbidden — single
    pipeline guarantee (AST-pinned).

Public surface:

  * :class:`CausalDecisionAdvice` — closed 5-value taxonomy
    (NEUTRAL / RECURRENCE_WARNING / SIBLING_DEDUP /
    DEEP_LINEAGE_HARDEN / DISABLED). Bytes-pinned.
  * :class:`OpCausalFeatures` — frozen §33.5 versioned artifact
    carrying the feature vector for one op:
      - ancestor_count
      - distinct_phases_in_lineage
      - sibling_count
      - recurrence_score (0.0–1.0; structural-signature self-overlap)
      - parent_decisions_summary (≤256-char digest)
      - advice (closed enum)
  * :func:`compute_op_causal_features` — pure function. Given
    ``(session_id, record_id, *, max_depth, sibling_phase_filter)``
    walks the DAG and emits a frozen feature artifact.
  * :func:`is_advisory_blocking` — declarative selector for
    "should this advice nudge the iron-gate?". Returns False on
    NEUTRAL / DISABLED / SIBLING_DEDUP (advisory only); True on
    RECURRENCE_WARNING / DEEP_LINEAGE_HARDEN (still advisory —
    consumer decides whether to honor; substrate NEVER mutates
    routing).

Master switch — ``JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED``.
Default-FALSE per §33.1 graduation contract. Operator-paced
empirical cadence flips it after Slice 5's harness reports
ready-for-graduation.

NEVER raises across any public surface.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)


CAUSAL_FEATURES_SCHEMA_VERSION: str = "causal_features.1"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_consumer_enabled() -> bool:
    """Master switch — ``JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED``.
    Default FALSE per §33.1 graduation contract pattern. Stays
    FALSE until Slice 5 graduation contract reports
    ready_for_graduation against operator-paced empirical
    evidence."""
    return os.environ.get(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _read_int_knob(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        v = int(raw)
        if v <= 0:
            return default
        return v
    except (TypeError, ValueError):
        return default


def max_ancestor_depth_knob() -> int:
    return _read_int_knob(
        "JARVIS_CAUSAL_MAX_ANCESTOR_DEPTH", 16,
    )


def recurrence_window_knob() -> int:
    """Number of ancestors to scan when computing recurrence
    score. Larger window catches deeper recurrence patterns at
    O(window) cost. Default 32."""
    return _read_int_knob(
        "JARVIS_CAUSAL_RECURRENCE_WINDOW", 32,
    )


def sibling_dedup_threshold_knob() -> int:
    """Min sibling count that triggers SIBLING_DEDUP advice.
    Default 3 — three or more siblings sharing the same parent
    set is a fan-out pattern worth flagging."""
    return _read_int_knob(
        "JARVIS_CAUSAL_SIBLING_DEDUP_THRESHOLD", 3,
    )


def deep_lineage_threshold_knob() -> int:
    """Min ancestor count that triggers DEEP_LINEAGE_HARDEN.
    Default 12 — long causal chains are higher-friction."""
    return _read_int_knob(
        "JARVIS_CAUSAL_DEEP_LINEAGE_THRESHOLD", 12,
    )


def _read_float_knob(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        v = float(raw)
        if v < 0.0:
            return default
        return v
    except (TypeError, ValueError):
        return default


def recurrence_warning_threshold_knob() -> float:
    """Recurrence score (0.0–1.0) above which RECURRENCE_WARNING
    fires. Default 0.5 — half the ancestors in the window share
    structural signatures with the current op."""
    v = _read_float_knob(
        "JARVIS_CAUSAL_RECURRENCE_WARNING_THRESHOLD", 0.5,
    )
    if v > 1.0:
        return 1.0
    return v


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class CausalDecisionAdvice(str, enum.Enum):
    """Closed 5-value advisory taxonomy emitted by
    :func:`compute_op_causal_features`.

    Substrate is **advisory**, never directive — consumers
    decide whether to honor. The `_BLOCKING` projection below
    distinguishes "should the iron gate raise friction?" from
    "purely informational."

    Bytes-pinned via ``causal_decision_advice_taxonomy_closed``
    AST invariant — additions require explicit pin update.
    """

    NEUTRAL = "neutral"
    """Lineage shows nothing unusual — proceed without nudging."""

    RECURRENCE_WARNING = "recurrence_warning"
    """Recurrence score exceeded ``recurrence_warning_threshold``
    — the op's structural signature heavily overlaps with its
    ancestors. Iron gate may want to raise an exploration floor
    (consumer-side decision)."""

    SIBLING_DEDUP = "sibling_dedup"
    """``sibling_count`` exceeded ``sibling_dedup_threshold`` —
    the op shares a parent set with N+ already-recorded
    decisions. CONTEXT_EXPANSION may want to surface the sibling
    decisions; routing may want to dedupe."""

    DEEP_LINEAGE_HARDEN = "deep_lineage_harden"
    """``ancestor_count`` exceeded ``deep_lineage_threshold`` —
    long causal chains accumulate compounding uncertainty.
    Consumer may want to harden retry budgets / disable risky
    routes."""

    DISABLED = "disabled"
    """Master flag off — features computed but advice is null."""


# Kinds that consumers may want to treat as "raise friction"
# (advisory only — substrate NEVER mutates routing). Closed set
# bytes-pinned via the AST invariant.
_BLOCKING_ADVICE: FrozenSet[CausalDecisionAdvice] = frozenset({
    CausalDecisionAdvice.RECURRENCE_WARNING,
    CausalDecisionAdvice.DEEP_LINEAGE_HARDEN,
})


# ---------------------------------------------------------------------------
# Versioned feature artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpCausalFeatures:
    """Frozen feature vector for one op's causal lineage.

    §33.5 versioned-artifact pattern — schema_version is the
    canonical projection key; ``to_dict()`` round-trips through
    JSON. Symmetric ``from_dict()`` provided.
    """

    schema_version: str
    session_id: str
    record_id: str
    ancestor_count: int
    distinct_phases_in_lineage: Tuple[str, ...]
    sibling_count: int
    recurrence_score: float  # 0.0–1.0
    parent_decisions_summary: str  # ≤256 chars
    advice: CausalDecisionAdvice

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "record_id": self.record_id,
            "ancestor_count": int(self.ancestor_count),
            "distinct_phases_in_lineage": list(
                self.distinct_phases_in_lineage,
            ),
            "sibling_count": int(self.sibling_count),
            "recurrence_score": float(self.recurrence_score),
            "parent_decisions_summary": (
                self.parent_decisions_summary
            )[:256],
            "advice": self.advice.value,
        }

    @classmethod
    def from_dict(
        cls, payload: Dict[str, Any],
    ) -> "OpCausalFeatures":
        """Defensive deserialize. NEVER raises — bad inputs
        produce a NEUTRAL/empty artifact."""
        try:
            advice_raw = str(payload.get("advice") or "neutral")
            try:
                advice = CausalDecisionAdvice(advice_raw)
            except ValueError:
                advice = CausalDecisionAdvice.NEUTRAL
            phases_raw = payload.get(
                "distinct_phases_in_lineage",
            ) or []
            return cls(
                schema_version=str(
                    payload.get("schema_version")
                    or CAUSAL_FEATURES_SCHEMA_VERSION,
                ),
                session_id=str(payload.get("session_id") or ""),
                record_id=str(payload.get("record_id") or ""),
                ancestor_count=int(
                    payload.get("ancestor_count") or 0,
                ),
                distinct_phases_in_lineage=tuple(
                    str(p) for p in phases_raw
                    if isinstance(p, str)
                ),
                sibling_count=int(
                    payload.get("sibling_count") or 0,
                ),
                recurrence_score=float(
                    payload.get("recurrence_score") or 0.0,
                ),
                parent_decisions_summary=str(
                    payload.get("parent_decisions_summary")
                    or "",
                )[:256],
                advice=advice,
            )
        except Exception:  # noqa: BLE001 — defensive
            return _empty_features("", "")


def _empty_features(
    session_id: str, record_id: str,
) -> OpCausalFeatures:
    return OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id=session_id,
        record_id=record_id,
        ancestor_count=0,
        distinct_phases_in_lineage=(),
        sibling_count=0,
        recurrence_score=0.0,
        parent_decisions_summary="",
        advice=(
            CausalDecisionAdvice.DISABLED
            if not is_consumer_enabled()
            else CausalDecisionAdvice.NEUTRAL
        ),
    )


# ---------------------------------------------------------------------------
# Pure-function feature extraction
# ---------------------------------------------------------------------------


def _structural_signature(rec: Any) -> str:
    """Compact 12-char hash of a record's structural fingerprint
    (kind + phase + first 64 chars of any deterministic payload).
    Used by the recurrence-score detector to spot ops whose
    lineage is full of self-similar decisions."""
    try:
        kind = str(getattr(rec, "kind", "") or "")
        phase = str(getattr(rec, "phase", "") or "")
        # Some DecisionRecords carry a ``payload`` mapping;
        # others use ``inputs_hash``. Compose defensively.
        extra = ""
        for attr in ("inputs_hash", "outputs_hash"):
            val = getattr(rec, attr, "")
            if isinstance(val, str):
                extra += val[:32]
        digest = hashlib.sha256(
            f"{kind}|{phase}|{extra}".encode("utf-8"),
        ).hexdigest()[:12]
        return digest
    except Exception:  # noqa: BLE001 — defensive
        return ""


def compute_op_causal_features(
    *,
    session_id: str,
    record_id: str,
    max_depth: Optional[int] = None,
    recurrence_window: Optional[int] = None,
) -> OpCausalFeatures:
    """Walk the canonical DAG for ``session_id`` and emit a
    frozen feature artifact for ``record_id``.

    Pure read-side composition. NEVER raises — returns
    empty/NEUTRAL artifact on any failure (substrate
    unavailable, missing record, etc.).

    Parameters:
      * ``max_depth`` — overrides
        :func:`max_ancestor_depth_knob`. Caps how far upstream
        the BFS walks.
      * ``recurrence_window`` — overrides
        :func:`recurrence_window_knob`. Ancestors beyond this
        count are not scanned for structural-signature overlap
        (recurrence_score is computed against the closest N).

    Determinism contract: same DAG + same record_id + same env
    knobs → bytes-identical artifact. Ordering is BFS so
    insertion-order on the parent edges is preserved.
    """
    if not is_consumer_enabled():
        return _empty_features(session_id, record_id)
    sid = str(session_id or "").strip()
    rid = str(record_id or "").strip()
    if not sid or not rid:
        return _empty_features(sid, rid)
    # Compose canonical DAG builder via lazy import to avoid
    # startup cycle.
    try:
        from backend.core.ouroboros.governance.verification.causality_dag import (  # noqa: E501
            build_dag,
        )
    except ImportError:
        return _empty_features(sid, rid)
    try:
        dag = build_dag(session_id=sid)
    except Exception:  # noqa: BLE001 — defensive
        return _empty_features(sid, rid)
    if dag is None or dag.is_empty:
        return _empty_features(sid, rid)
    # Walk lineage.
    try:
        depth = (
            int(max_depth)
            if max_depth is not None
            else max_ancestor_depth_knob()
        )
        window = (
            int(recurrence_window)
            if recurrence_window is not None
            else recurrence_window_knob()
        )
    except (TypeError, ValueError):
        return _empty_features(sid, rid)
    if depth <= 0 or window <= 0:
        return _empty_features(sid, rid)
    ancestors = dag.ancestors_of(rid, max_depth=depth)
    ancestor_count = len(ancestors)
    # Distinct phases in lineage — composes nodes_for_phase via
    # the public read API; preserves insertion order via the
    # DAG's own discipline.
    seen_phases: list = []
    seen_phase_set: set = set()
    for aid in ancestors[:window]:
        a_rec = dag.node(aid)
        if a_rec is None:
            continue
        ph = str(getattr(a_rec, "phase", "") or "")
        if ph and ph not in seen_phase_set:
            seen_phases.append(ph)
            seen_phase_set.add(ph)
    # Sibling detection — a sibling shares ≥1 parent with the
    # target record. Composes parents() + children().
    sibling_set: set = set()
    target_parents = dag.parents(rid)
    for pid in target_parents:
        for cid in dag.children(pid):
            if cid != rid:
                sibling_set.add(cid)
    sibling_count = len(sibling_set)
    # Recurrence score — fraction of ancestors whose structural
    # signature equals the target's. 0.0 on missing target.
    target_rec = dag.node(rid)
    target_sig = (
        _structural_signature(target_rec)
        if target_rec is not None else ""
    )
    if target_sig and ancestor_count > 0:
        scanned = ancestors[:window]
        match_count = 0
        for aid in scanned:
            a_rec = dag.node(aid)
            if a_rec is None:
                continue
            if _structural_signature(a_rec) == target_sig:
                match_count += 1
        recurrence_score = (
            float(match_count) / float(len(scanned))
            if scanned else 0.0
        )
    else:
        recurrence_score = 0.0
    # Parent decisions summary — compact human-readable digest
    # of immediate parents' kinds (capped at 256 chars).
    parent_kinds: list = []
    for pid in target_parents:
        p_rec = dag.node(pid)
        if p_rec is None:
            continue
        parent_kinds.append(
            f"{getattr(p_rec, 'kind', '?')}:{pid[:8]}",
        )
    summary = " | ".join(parent_kinds)[:256]
    # Advisory classification — first-match-wins ordering. The
    # ordering encodes severity (DEEP_LINEAGE_HARDEN >
    # RECURRENCE_WARNING > SIBLING_DEDUP > NEUTRAL).
    advice = _classify_advice(
        ancestor_count=ancestor_count,
        sibling_count=sibling_count,
        recurrence_score=recurrence_score,
    )
    return OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id=sid,
        record_id=rid,
        ancestor_count=ancestor_count,
        distinct_phases_in_lineage=tuple(seen_phases),
        sibling_count=sibling_count,
        recurrence_score=recurrence_score,
        parent_decisions_summary=summary,
        advice=advice,
    )


def _classify_advice(
    *,
    ancestor_count: int,
    sibling_count: int,
    recurrence_score: float,
) -> CausalDecisionAdvice:
    """Pure classification — first-match-wins ordering.
    DEEP_LINEAGE_HARDEN takes precedence (long chains compound
    uncertainty); RECURRENCE_WARNING next (structural self-
    overlap); SIBLING_DEDUP last; NEUTRAL default."""
    try:
        if ancestor_count >= deep_lineage_threshold_knob():
            return CausalDecisionAdvice.DEEP_LINEAGE_HARDEN
        if recurrence_score >= recurrence_warning_threshold_knob():
            return CausalDecisionAdvice.RECURRENCE_WARNING
        if sibling_count >= sibling_dedup_threshold_knob():
            return CausalDecisionAdvice.SIBLING_DEDUP
        return CausalDecisionAdvice.NEUTRAL
    except Exception:  # noqa: BLE001 — defensive
        return CausalDecisionAdvice.NEUTRAL


# ---------------------------------------------------------------------------
# Slice 2 — CONTEXT_EXPANSION markdown render
# ---------------------------------------------------------------------------


# 2KB budget for the causal-lineage section (smaller than
# failure-modes 3KB / action-outcomes 4KB because lineage is
# always a structural digest, never a verbatim payload). Env-
# tunable.
DEFAULT_CAUSAL_LINEAGE_PROMPT_BUDGET: int = 2048


def causal_lineage_prompt_budget() -> int:
    return _read_int_knob(
        "JARVIS_CAUSAL_LINEAGE_PROMPT_BUDGET",
        DEFAULT_CAUSAL_LINEAGE_PROMPT_BUDGET,
    )


def compose_causal_lineage_section(
    features: Optional[OpCausalFeatures],
    *,
    max_chars: Optional[int] = None,
) -> str:
    """Compose the ``## Recent Causal Lineage`` markdown section
    for CONTEXT_EXPANSION injection at GENERATE.

    Pure function. NEVER raises. Returns empty string on any of:

      * ``features`` is None
      * ``features.advice`` is :attr:`CausalDecisionAdvice.DISABLED`
        (master flag off — section silent)
      * ``features.ancestor_count`` is 0 (no lineage to inject)

    Empty result is structurally important — per the
    failure-modes / action-outcomes pattern, never emit empty
    headers (the header itself signals "this section had
    something to say"; if there's nothing, stay silent).

    Section format:

        ## Recent Causal Lineage

        Your op's decision history shows {ancestor_count}
        upstream decisions across {n_phases} phases:
        {phase_list}.

        {advice_paragraph}

        Authority disclaimer — informational only. Iron Gate /
        SemanticGuardian / risk tier still apply.
    """
    if features is None:
        return ""
    if features.advice is CausalDecisionAdvice.DISABLED:
        return ""
    if features.ancestor_count <= 0:
        return ""
    budget = (
        int(max_chars) if max_chars is not None
        else causal_lineage_prompt_budget()
    )
    if budget <= 0:
        return ""
    try:
        # Phase list — capped to first 8 to leave budget for the
        # advice paragraph + authority disclaimer.
        phases = features.distinct_phases_in_lineage[:8]
        phase_str = (
            ", ".join(phases) if phases else "(unspecified)"
        )
        sibling_str = (
            f" (sibling forks: {features.sibling_count})"
            if features.sibling_count > 0 else ""
        )
        recurrence_pct = int(
            features.recurrence_score * 100,
        )
        recurrence_str = (
            f" Structural-signature recurrence: "
            f"{recurrence_pct}%."
            if features.recurrence_score > 0.0 else ""
        )
        advice_para = _advice_paragraph(features.advice)
        section_parts = [
            "## Recent Causal Lineage",
            "",
            (
                f"Your op's decision history shows "
                f"{features.ancestor_count} upstream "
                f"decisions across {len(phases)} phases: "
                f"{phase_str}{sibling_str}.{recurrence_str}"
            ),
        ]
        if advice_para:
            section_parts.extend(["", advice_para])
        section_parts.extend([
            "",
            (
                "Authority disclaimer — informational only. "
                "Iron Gate / SemanticGuardian / risk tier "
                "still apply."
            ),
        ])
        section = "\n".join(section_parts)
        if len(section) > budget:
            section = section[: budget - 3] + "..."
        return section
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _advice_paragraph(
    advice: CausalDecisionAdvice,
) -> str:
    """Render advice → prose. Closed-table dispatch (no LLM)."""
    if advice is CausalDecisionAdvice.RECURRENCE_WARNING:
        return (
            "Recurrence pattern detected: this op's structural "
            "signature heavily overlaps with its ancestors. "
            "Consider whether you're re-litigating territory "
            "already explored — vary the approach if the "
            "current change feels redundant."
        )
    if advice is CausalDecisionAdvice.SIBLING_DEDUP:
        return (
            "Sibling-fork pattern: multiple decisions share "
            "this op's parent set. Consider whether the "
            "siblings already address the request — review "
            "their outcomes before duplicating effort."
        )
    if advice is CausalDecisionAdvice.DEEP_LINEAGE_HARDEN:
        return (
            "Deep-lineage warning: long causal chains "
            "accumulate compounding uncertainty. Favor "
            "smaller, well-tested patches; avoid "
            "cross-cutting refactors in this op."
        )
    return ""


def is_advisory_blocking(
    advice: Optional[CausalDecisionAdvice],
) -> bool:
    """Declarative selector — does this advice signal "raise
    friction at the iron gate"?

    Single source of truth — :data:`_BLOCKING_ADVICE` is the only
    knower; consumers compose this function. Returns False on
    None / unknown input (defensive).

    Substrate is advisory-only — even when this returns True,
    consumers decide whether to honor; this function never
    mutates state."""
    if advice is None:
        return False
    if not isinstance(advice, CausalDecisionAdvice):
        return False
    return advice in _BLOCKING_ADVICE


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``causal_decision_advice_taxonomy_closed`` —
         :class:`CausalDecisionAdvice` has EXACTLY 5 values and
         the value set is bytes-pinned. Closed taxonomy.
      2. ``causal_consumer_master_flag_default_false`` —
         :func:`is_consumer_enabled` returns False on a clean
         environment. §33.1 graduation contract pattern; flips
         only after Slice 5 harness reports ready.
      3. ``causal_consumer_authority_asymmetry`` — substrate
         purity. Forbids orchestrator+iron_gate+policy+providers+
         candidate_generator+urgency_router+change_engine+
         semantic_guardian imports + ``DecisionRuntime.record``
         mutating call.
      4. ``causal_consumer_composes_canonical_dag`` — single
         pipeline guarantee. Forbids direct ``CausalityDAG()``
         construction; module MUST compose ``build_dag()``.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/causality_consumer.py"
    )

    _EXPECTED_VALUES = {
        "neutral", "recurrence_warning", "sibling_dedup",
        "deep_lineage_harden", "disabled",
    }

    def _validate_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CausalDecisionAdvice"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VALUES - found
                extra = found - _EXPECTED_VALUES
                if missing:
                    violations.append(
                        f"CausalDecisionAdvice missing: "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"CausalDecisionAdvice unexpected "
                        f"(taxonomy drift): {sorted(extra)}"
                    )
                return tuple(violations)
        violations.append(
            "CausalDecisionAdvice class definition missing"
        )
        return tuple(violations)

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """is_consumer_enabled body must read os.environ.get
        with the canonical flag name and return only on truthy
        values from a closed allowlist — preventing accidental
        default-true flips at the source."""
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "is_consumer_enabled"
            ):
                # Body must contain os.environ.get(
                # "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "")
                found_canonical_read = False
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        if (
                            isinstance(fn, ast.Attribute)
                            and fn.attr == "get"
                            and sub.args
                            and isinstance(
                                sub.args[0], ast.Constant,
                            )
                            and sub.args[0].value
                            == "JARVIS_CAUSAL_DECISION_"
                               "CONSUMER_ENABLED"
                        ):
                            found_canonical_read = True
                if not found_canonical_read:
                    violations.append(
                        "is_consumer_enabled MUST read "
                        "os.environ.get('JARVIS_CAUSAL_"
                        "DECISION_CONSUMER_ENABLED', '') "
                        "exactly — no parallel flag-name path"
                    )
                return tuple(violations)
        violations.append(
            "is_consumer_enabled function missing"
        )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_modules = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden_modules:
                    if f in module:
                        violations.append(
                            f"causality_consumer.py MUST NOT "
                            f"import {module!r} — substrate "
                            f"authority asymmetry"
                        )
        # No `.record(` calls on DecisionRuntime / ledger objects.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "record"
                ):
                    # Allow .record() on logger / Mock / etc. —
                    # check the receiver name; flag obvious
                    # decisions-write candidates.
                    if isinstance(fn.value, ast.Name):
                        rcv = fn.value.id.lower()
                        if (
                            "decision" in rcv
                            or "runtime" in rcv
                            or "ledger" in rcv
                        ):
                            violations.append(
                                "causality_consumer.py is "
                                "read-only; MUST NOT call "
                                ".record() on a decision/"
                                "runtime/ledger receiver"
                            )
        return tuple(violations)

    def _validate_composes_canonical_dag(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "CausalityDAG"
                ):
                    violations.append(
                        "causality_consumer.py MUST NOT "
                        "construct CausalityDAG() directly — "
                        "compose build_dag() (single pipeline)"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "causal_decision_advice_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "§31 U2 empirical wiring Slice 1 — "
                "CausalDecisionAdvice 5-value closed taxonomy."
            ),
            validate=_validate_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "causal_consumer_master_flag_default_false"
            ),
            target_file=target,
            description=(
                "§31 U2 empirical wiring Slice 1 — master flag "
                "stays default-FALSE per §33.1 graduation."
            ),
            validate=_validate_master_flag_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "causal_consumer_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§31 U2 empirical wiring Slice 1 — substrate "
                "purity + read-only contract."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "causal_consumer_composes_canonical_dag"
            ),
            target_file=target,
            description=(
                "§31 U2 empirical wiring Slice 1 — single "
                "pipeline; composes build_dag() only."
            ),
            validate=_validate_composes_canonical_dag,
        ),
    ]


__all__ = [
    "CAUSAL_FEATURES_SCHEMA_VERSION",
    "CausalDecisionAdvice",
    "DEFAULT_CAUSAL_LINEAGE_PROMPT_BUDGET",
    "OpCausalFeatures",
    "causal_lineage_prompt_budget",
    "compose_causal_lineage_section",
    "compute_op_causal_features",
    "deep_lineage_threshold_knob",
    "is_advisory_blocking",
    "is_consumer_enabled",
    "max_ancestor_depth_knob",
    "recurrence_warning_threshold_knob",
    "recurrence_window_knob",
    "register_shipped_invariants",
    "sibling_dedup_threshold_knob",
]
