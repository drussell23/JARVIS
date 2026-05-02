"""PlanFalsificationDetector Slice 4 — orchestrator bridge.

The orchestrator hook that runs the structural detector at the
GENERATE retry loop's reactive replan site (orchestrator.py near
the existing :class:`DynamicRePlanner` lookup). When the structural
detector trips ``REPLAN_TRIGGERED``, it pre-empts the regex-based
reactive suggestion with a typed, evidence-backed prompt block.
When it doesn't, the legacy reactive path remains the backstop.

Architectural reuse — composes with ZERO duplication
----------------------------------------------------

* Slice 1 :func:`pair_plan_step_with_hypothesis` — the per-entry
  PlanStepHypothesis builder. The bridge does NOT reconstruct the
  full :class:`PlanResult`; it does a minimal JSON peek for
  ``ordered_changes`` and lets Slice 1 own the per-entry shape.
* Slice 1 :class:`EvidenceItem` — typed evidence wrapper. The bridge
  classifies ONE source (the orchestrator's own VERIFY/build
  failure summary) into a VERIFY_REJECTED / REPAIR_STUCK
  EvidenceItem; it does NOT pattern-match the summary for content,
  only routes by ``failure_class``.
* Slice 2 :func:`detect_falsification` — async public detector.
  The bridge calls it; never duplicates its decision tree.
* Slice 3 :func:`PlanResult.to_plan_step_hypotheses` — bridge does
  NOT call this; instead, the bridge accepts ``plan_json`` from
  ``ctx.implementation_plan`` (a string, not the dataclass) and
  rebuilds hypotheses directly via Slice 1's convenience
  constructor. This keeps the bridge independent of PlanGenerator
  module-load order during the orchestrator wire-up.

Direct-solve principles
-----------------------

* **Asynchronous-ready** — :func:`bridge_to_replan` is async so it
  composes with the orchestrator's existing await-driven retry
  loop.
* **Dynamic** — every numeric (timeout, char cap on prompt block)
  flows from env-knob helpers with floor + ceiling clamps.
* **Adaptive** — every degraded path (corrupt JSON, garbage
  ordered_changes, detector FAILED) returns ``None`` so the
  legacy reactive ``DynamicRePlanner`` path remains a backstop.
  The bridge NEVER promotes a structural failure to a hard error.
* **Intelligent** — failure_class → FalsificationKind mapping is a
  closed table (no regex on summary text). When ``failure_class``
  is unknown / empty / outside the mapping, the bridge contributes
  ZERO upstream evidence and lets the filesystem probe drive the
  decision alone.
* **Robust** — every public function NEVER raises out. Caller-
  initiated ``asyncio.CancelledError`` propagates per convention.
* **No hardcoding** — bridge enable flag, prompt-inject sub-flag,
  failure-class → kind mapping all live in module-level constants
  / env helpers; the orchestrator wire-up reads them at call time
  so flag flips take effect without restart.

Authority invariants (AST-pinned by Slice 5 graduation)
-------------------------------------------------------

* MAY import: ``plan_falsification`` (Slice 1 primitive) +
  ``plan_falsification_detector`` (Slice 2 async detector).
* MUST NOT import: orchestrator / phase_runner / iron_gate /
  change_engine / candidate_generator / providers /
  doubleword_provider / urgency_router / auto_action_router /
  subagent_scheduler / tool_executor / semantic_guardian /
  semantic_firewall / risk_engine.
* No exec/eval/compile.
* The bridge NEVER mutates files / candidates / contexts. Strictly
  read-in / decision-out.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Mapping, Optional, Tuple

from backend.core.ouroboros.governance.plan_falsification import (
    EvidenceItem,
    FalsificationKind,
    FalsificationOutcome,
    FalsificationVerdict,
    PlanStepHypothesis,
    pair_plan_step_with_hypothesis,
)
from backend.core.ouroboros.governance.plan_falsification_detector import (
    detect_falsification,
)

logger = logging.getLogger(__name__)


PLAN_FALSIFICATION_BRIDGE_SCHEMA_VERSION: str = (
    "plan_falsification_bridge.1"
)


# ---------------------------------------------------------------------------
# Sub-flags
# ---------------------------------------------------------------------------


def bridge_enabled() -> bool:
    """``JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED`` (default ``true``).

    Wire-up sub-flag — composes with the master
    ``JARVIS_PLAN_FALSIFICATION_ENABLED`` flag (Slice 1). When this
    sub-flag is off, the orchestrator wire-up returns ``None``
    immediately; when the master is off, the detector itself returns
    DISABLED. Either condition keeps the legacy reactive path live.
    Asymmetric env semantics."""
    raw = os.environ.get(
        "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def prompt_inject_enabled() -> bool:
    """``JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED`` (default
    ``true``).

    Sub-sub-flag — when off, the detector still RUNS at the bridge
    site (so its verdict shows up in observability + telemetry)
    but the orchestrator wire-up does NOT inject the structural
    feedback into the GENERATE_RETRY prompt. Operators flip this
    to compare detector verdicts to model behavior in shadow mode.
    Asymmetric env semantics."""
    raw = os.environ.get(
        "JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def _feedback_max_chars() -> int:
    """``JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS`` (default
    1500, floor 200, ceiling 4000)."""
    raw = os.environ.get(
        "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS", "",
    ).strip()
    try:
        n = int(raw) if raw else 1500
    except ValueError:
        n = 1500
    return max(200, min(4000, n))


# ---------------------------------------------------------------------------
# failure_class → FalsificationKind closed mapping
# ---------------------------------------------------------------------------
#
# Closed table — no regex, no substring matching, no model call.
# When ``failure_class`` is not a key here, the bridge contributes
# ZERO upstream evidence and relies on the filesystem probe alone
# (or returns INSUFFICIENT_EVIDENCE if no fs miss either).
#
# This preserves Manifesto §5 deterministic routing — the source
# (orchestrator's own validation pipeline) classifies; the bridge
# routes. We do not reverse-engineer evidence kind from prose.
_FAILURE_CLASS_TO_KIND: Mapping[str, FalsificationKind] = {
    "test": FalsificationKind.VERIFY_REJECTED,
    "build": FalsificationKind.VERIFY_REJECTED,
    "verify": FalsificationKind.VERIFY_REJECTED,
    "validation": FalsificationKind.VERIFY_REJECTED,
    "repair": FalsificationKind.REPAIR_STUCK,
    # "infra" / "budget" / "" → no upstream evidence; fs probe
    # decides alone.
}


# ---------------------------------------------------------------------------
# Hypothesis extraction (no full PlanResult reconstruction needed)
# ---------------------------------------------------------------------------


def extract_hypotheses_from_plan_json(
    plan_json: str,
) -> Tuple[PlanStepHypothesis, ...]:
    """Parse just enough of the plan JSON to materialize hypotheses.
    Delegates per-entry construction to Slice 1's
    :func:`pair_plan_step_with_hypothesis`. NEVER raises.

    Empty / malformed / missing ``ordered_changes`` returns ``()``
    so the detector short-circuits to INSUFFICIENT_EVIDENCE without
    error.
    """
    if not plan_json or not isinstance(plan_json, str):
        return ()
    try:
        data = json.loads(plan_json)
    except (json.JSONDecodeError, ValueError):
        return ()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PlanFalsificationBridge] extract: unexpected JSON "
            "exception: %s", exc,
        )
        return ()
    if not isinstance(data, dict):
        return ()
    ordered_changes = data.get("ordered_changes", [])
    if not isinstance(ordered_changes, list):
        return ()
    out = []
    for idx, change in enumerate(ordered_changes):
        if not isinstance(change, dict):
            continue
        try:
            hyp = pair_plan_step_with_hypothesis(
                step_index=idx,
                ordered_change=change,
                expected_outcome=str(
                    change.get("expected_outcome", "") or "",
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[PlanFalsificationBridge] extract: skipped change "
                "idx=%d: %s", idx, exc,
            )
            continue
        out.append(hyp)
    return tuple(out)


# ---------------------------------------------------------------------------
# Validation → upstream evidence translation
# ---------------------------------------------------------------------------


def build_evidence_from_validation(
    *,
    failure_class: Optional[str],
    short_summary: str,
    target_files: Tuple[str, ...] = (),
    captured_monotonic: Optional[float] = None,
) -> Tuple[EvidenceItem, ...]:
    """Translate one orchestrator validation failure into a tuple
    of typed :class:`EvidenceItem` (zero or one item).

    The bridge classifies SOURCE → KIND via the closed
    ``_FAILURE_CLASS_TO_KIND`` table. When the source is unknown
    / unmapped / empty, returns ``()`` so the filesystem probe
    decides alone (no fabricated evidence).

    NEVER raises.
    """
    try:
        if not failure_class:
            return ()
        kind = _FAILURE_CLASS_TO_KIND.get(
            str(failure_class).strip().lower(),
        )
        if kind is None:
            return ()
        ts = (
            captured_monotonic
            if captured_monotonic is not None
            else time.monotonic()
        )
        # First target file (if any) becomes the file_path anchor.
        # When unknown, leave empty → detector matches by step_index
        # alone (and if the upstream source didn't supply step_index
        # either, the evidence lands as "unattributed" and the fs
        # probe drives the verdict).
        anchor = ""
        try:
            if target_files:
                first = target_files[0]
                if first and isinstance(first, str):
                    anchor = first
        except Exception:  # noqa: BLE001
            anchor = ""
        return (
            EvidenceItem(
                kind=kind,
                target_step_index=None,
                target_file_path=anchor,
                detail=str(short_summary or "")[:500],
                source="plan_falsification_bridge.validation",
                captured_monotonic=ts,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PlanFalsificationBridge] evidence build degraded: %s",
            exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Verdict → prompt block renderer
# ---------------------------------------------------------------------------


def render_falsification_feedback(
    verdict: FalsificationVerdict,
    *,
    plan_hypotheses: Tuple[PlanStepHypothesis, ...] = (),
) -> str:
    """Render the structural replan directive for the
    GENERATE_RETRY prompt. Returns an empty string for any
    non-REPLAN_TRIGGERED outcome (so callers can unconditionally
    chain ``feedback or fallback``). NEVER raises.

    The block is intentionally short, anchored on the falsified
    step + its expected_outcome predicate, and ASCII-only (Iron
    Gate strict-ASCII compatible)."""
    try:
        if not isinstance(verdict, FalsificationVerdict):
            return ""
        if verdict.outcome is not FalsificationOutcome.REPLAN_TRIGGERED:
            return ""
        step_idx = verdict.falsified_step_index
        # Find the matching hypothesis for richer context (if avail).
        matched: Optional[PlanStepHypothesis] = None
        try:
            for h in plan_hypotheses:
                if (
                    isinstance(h, PlanStepHypothesis)
                    and step_idx is not None
                    and int(h.step_index) == int(step_idx)
                ):
                    matched = h
                    break
        except Exception:  # noqa: BLE001
            matched = None

        kinds_str = ", ".join(verdict.falsifying_evidence_kinds) or "?"
        lines = [
            "## Plan Falsification -- replan required",
            "",
            (
                f"Step #{step_idx} of your previous plan was "
                f"falsified by structural evidence "
                f"(kinds: {kinds_str})."
            ),
        ]
        if matched is not None:
            if matched.file_path:
                lines.append(f"- File: `{matched.file_path}`")
            if matched.change_type:
                lines.append(f"- Change type: {matched.change_type}")
            if matched.expected_outcome:
                lines.append(
                    f"- You committed to: {matched.expected_outcome}"
                )
        if verdict.contradicting_detail:
            lines.append(
                f"- Contradicting evidence: "
                f"{verdict.contradicting_detail}"
            )
        lines.append("")
        lines.append(
            "Revise the implementation plan: address the falsified "
            "step's predicate or replace it. Other steps may stand "
            "if their predicates remain consistent."
        )
        block = "\n".join(lines)
        cap = _feedback_max_chars()
        if len(block) > cap:
            block = block[: cap - 3] + "..."
        return block
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PlanFalsificationBridge] render degraded: %s", exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Async one-shot bridge — orchestrator-callable surface
# ---------------------------------------------------------------------------


async def bridge_to_replan(
    *,
    plan_json: str,
    validation_failure_class: Optional[str] = None,
    validation_short_summary: str = "",
    target_files: Tuple[str, ...] = (),
    project_root: Optional[Path] = None,
    enabled: Optional[bool] = None,
    inject_prompt: Optional[bool] = None,
    op_id: str = "",
) -> Tuple[FalsificationVerdict, str]:
    """Run the structural detector against the current plan +
    upstream validation evidence; return (verdict, prompt_block).

    The orchestrator wires this in BEFORE the legacy
    ``DynamicRePlanner.suggest_replan`` lookup — when ``prompt_block``
    is non-empty the orchestrator uses it as the GENERATE_RETRY
    feedback (preempting the regex-based reactive suggestion).
    When empty the orchestrator falls through to the legacy path
    (backstop preserved).

    Returns:
      (verdict, prompt_block) — verdict is always a
      FalsificationVerdict (never None) so observability /
      telemetry consumers always see a typed value;
      prompt_block is empty string when:
        * bridge sub-flag is off
        * detector returns anything other than REPLAN_TRIGGERED
        * prompt_inject sub-flag is off (shadow mode)
        * any defensive-degradation path triggers

    NEVER raises out. asyncio.CancelledError propagates per
    convention.
    """
    captured_monotonic = time.monotonic()
    is_enabled = (
        enabled if enabled is not None else bridge_enabled()
    )
    if not is_enabled:
        return (
            FalsificationVerdict(
                outcome=FalsificationOutcome.DISABLED,
                monotonic_tightening_verdict="",
            ),
            "",
        )

    plan_hypotheses = extract_hypotheses_from_plan_json(plan_json)
    upstream_evidence = build_evidence_from_validation(
        failure_class=validation_failure_class,
        short_summary=validation_short_summary,
        target_files=target_files,
        captured_monotonic=captured_monotonic,
    )

    verdict = await detect_falsification(
        plan_hypotheses,
        upstream_evidence=upstream_evidence,
        project_root=project_root,
    )

    inject = (
        inject_prompt if inject_prompt is not None
        else prompt_inject_enabled()
    )
    feedback = ""
    if inject:
        feedback = render_falsification_feedback(
            verdict, plan_hypotheses=plan_hypotheses,
        )
    # Best-effort SSE publish (Slice 5 graduation). Never blocks
    # bridge return on stream failures.
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_plan_falsification_verdict,
        )
        publish_plan_falsification_verdict(
            op_id=op_id or "unknown",
            outcome=verdict.outcome.value,
            falsified_step_index=verdict.falsified_step_index,
            falsifying_evidence_kinds=verdict.falsifying_evidence_kinds,
            contradicting_detail=verdict.contradicting_detail,
            total_hypotheses=verdict.total_hypotheses,
            total_evidence=verdict.total_evidence,
            monotonic_tightening_verdict=(
                verdict.monotonic_tightening_verdict
            ),
            prompt_injected=bool(feedback),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug(
            "[PlanFalsificationBridge] SSE publish degraded: %s",
            exc,
        )
    return (verdict, feedback)


__all__ = [
    "PLAN_FALSIFICATION_BRIDGE_SCHEMA_VERSION",
    "bridge_enabled",
    "bridge_to_replan",
    "build_evidence_from_validation",
    "extract_hypotheses_from_plan_json",
    "prompt_inject_enabled",
    "register_flags",
    "register_shipped_invariants",
    "render_falsification_feedback",
]


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    """Module-owned :class:`FlagRegistry` registration for the
    bridge's 3 sub-flags. Auto-discovered. Returns count."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[PlanFalsificationBridge] register_flags degraded: %s",
            exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/"
        "plan_falsification_orchestrator_bridge.py"
    )
    specs = [
        FlagSpec(
            name="JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example=(
                "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED=true"
            ),
            description=(
                "Wire-up sub-flag for the orchestrator bridge. "
                "Composes with the Slice 1 master flag. When off, "
                "the orchestrator skips the structural detector "
                "and falls through to the legacy DynamicRePlanner "
                "regex backstop. Graduated default-true 2026-05-02."
            ),
        ),
        FlagSpec(
            name="JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example=(
                "JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED=true"
            ),
            description=(
                "Shadow-mode toggle: when off, the detector still "
                "runs (verdict surfaces in observability) but no "
                "prompt injection. Operators flip to false to "
                "compare structural verdicts against model "
                "behavior without pre-empting the legacy path."
            ),
        ),
        FlagSpec(
            name="JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS",
            type=FlagType.INT, default=1500,
            category=Category.CAPACITY,
            source_file=target,
            example=(
                "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS=2200"
            ),
            description=(
                "Truncation cap for the structural feedback block "
                "injected into the GENERATE_RETRY prompt. Floor "
                "200, ceiling 4000."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[PlanFalsificationBridge] register_flags spec %s "
                "skipped: %s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned shipped_code_invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Slice 4 invariants: authority allowlist (Slice 1 + Slice 2
    only) + bridge_to_replan async + render output ASCII-only."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _ALLOWED = {
        "plan_falsification",
        "plan_falsification_detector",
        # Additive observability + registration-contract.
        "ide_observability_stream",
        "flag_registry",
        "shipped_code_invariants",
    }
    _FORBIDDEN = {
        "orchestrator", "phase_runner", "iron_gate",
        "change_engine", "candidate_generator", "providers",
        "doubleword_provider", "urgency_router",
        "auto_action_router", "subagent_scheduler",
        "tool_executor", "semantic_guardian",
        "semantic_firewall", "risk_engine",
    }

    def _validate(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in _ast.walk(tree):
            if isinstance(fnode, _ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." not in module and "governance" not in module:
                    continue
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                tail = module.rsplit(".", 1)[-1]
                if tail in _FORBIDDEN:
                    violations.append(
                        f"line {lineno}: forbidden module {module!r}"
                    )
                elif tail not in _ALLOWED:
                    violations.append(
                        f"line {lineno}: unexpected governance "
                        f"import {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 4 MUST NOT {node.func.id}()"
                        )
        # bridge_to_replan must be async.
        async_seen = {
            n.name for n in _ast.walk(tree)
            if isinstance(n, _ast.AsyncFunctionDef)
        }
        if "bridge_to_replan" not in async_seen:
            violations.append(
                "missing async def bridge_to_replan"
            )
        # Renderer output must be ASCII-only at the source level
        # — flag any non-ASCII string-literal that ships into the
        # rendered prompt block. We constrain to the lines list
        # inside render_falsification_feedback to avoid pinning
        # docstrings.
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef) and (
                node.name == "render_falsification_feedback"
            ):
                for sub in _ast.walk(node):
                    if isinstance(sub, _ast.Constant) and isinstance(
                        sub.value, str,
                    ):
                        try:
                            sub.value.encode("ascii")
                        except UnicodeEncodeError:
                            violations.append(
                                f"line {getattr(sub, 'lineno', '?')}: "
                                f"render_falsification_feedback "
                                f"emits non-ASCII literal"
                            )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "plan_falsification_orchestrator_bridge.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="plan_falsification_bridge_authority",
            target_file=target,
            description=(
                "Slice 4 bridge authority: imports only "
                "plan_falsification (Slice 1) + "
                "plan_falsification_detector (Slice 2); "
                "bridge_to_replan stays async; render output is "
                "ASCII-only (Iron Gate strict-ASCII compatible); "
                "no exec/eval/compile."
            ),
            validate=_validate,
        ),
    ]
