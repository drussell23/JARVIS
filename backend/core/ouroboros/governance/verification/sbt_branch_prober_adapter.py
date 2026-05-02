"""SBT-Probe Escalation Bridge Slice 3 — production BranchProber adapter.

The production wiring that lets the SBT runner consume Move 5's
existing :class:`ReadonlyEvidenceProber` substrate. Each SBT
branch becomes one probe-question with a different
``resolution_method`` rotated across :data:`READONLY_TOOL_ALLOWLIST`,
producing branch diversity by tool — when multiple tools converge
on the same answer (same fingerprint), that's strong evidence;
when they diverge, it's genuine ambiguity.

Architectural reuse — three existing surfaces compose with ZERO
new substrate:

  * :data:`READONLY_TOOL_ALLOWLIST` — the same 9-tool frozenset
    Move 5's probe loop already consumes. The SBT runner already
    enforces it via ``_filter_evidence_to_allowlist`` defense-in-
    depth. This adapter rotates ``resolution_method`` across it
    to widen the per-branch evidence search.
  * :class:`ReadonlyEvidenceProber` — Move 5's ``QuestionResolver``.
    Calls a ``ReadonlyToolBackend`` filtered to the allowlist,
    aggregates results into ``ProbeAnswer`` text. We adapt one
    branch call into one ``ProbeQuestion``+``resolve()``.
  * :class:`BranchEvidence` — the SBT primitive's frozen evidence
    dataclass. ``content_hash`` derives from ``ProbeAnswer.answer_text``
    (sha256), enabling cross-branch fingerprint convergence.

The adapter is a single class implementing the
:class:`BranchProber` Protocol — no parallel implementations,
no duplicated allowlist, no parallel resolver.

Direct-solve principles
-----------------------

* **Asynchronous-ready** — the underlying prober is sync; SBT
  runner wraps in ``asyncio.to_thread``. We don't double-wrap.
* **Dynamic** — branch-method rotation is deterministic from
  ``branch_id`` (sha256-hashed and indexed into the allowlist).
  Same branch_id always picks the same method (idempotent
  re-spawn after retry).
* **Adaptive** — degraded paths (resolver crash, empty answer,
  None backend) return empty evidence tuple → branches
  classified as PARTIAL → tree INCONCLUSIVE → wrapper
  INCONCLUSIVE. Safe.
* **Intelligent** — branch diversity by tool: branch 0 might use
  ``read_file``, branch 1 ``search_code``, branch 2 ``get_callers``.
  Multi-tool agreement is a stronger signal than single-tool
  repetition.
* **Robust** — every public method NEVER raises. Falls back to
  empty evidence on any underlying failure.
* **No hardcoding** — the rotation set IS ``READONLY_TOOL_ALLOWLIST``
  (no parallel allowlist). Confidence default is a tuned constant
  exposed as a module-level symbol.

Authority invariants (AST-pinned by Slice 3 graduation)
-------------------------------------------------------

* MAY import: ``confidence_probe_bridge`` (ProbeQuestion / ProbeAnswer
  shapes), ``readonly_evidence_prober`` (the ReadonlyEvidenceProber
  + READONLY_TOOL_ALLOWLIST), ``speculative_branch`` (BranchEvidence
  + EvidenceKind + BranchTreeTarget shapes).
* MUST NOT import: orchestrator / phase_runner / iron_gate /
  change_engine / candidate_generator / providers / doubleword_provider /
  urgency_router / auto_action_router / subagent_scheduler /
  tool_executor / semantic_guardian / semantic_firewall / risk_engine.
* No exec/eval/compile.
* The set of resolution_methods this adapter emits MUST be a
  subset of READONLY_TOOL_ALLOWLIST (no escape hatch to mutation
  tools by construction).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, FrozenSet, Optional, Tuple

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (
    ProbeQuestion,
)
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (
    READONLY_TOOL_ALLOWLIST,
    ReadonlyEvidenceProber,
    get_default_prober,
)
from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchEvidence,
    BranchTreeTarget,
    EvidenceKind,
)

logger = logging.getLogger(__name__)


SBT_BRANCH_PROBER_ADAPTER_SCHEMA_VERSION: str = (
    "sbt_branch_prober_adapter.1"
)


# ---------------------------------------------------------------------------
# Tunable constants — Slice 3 AST pin will assert presence
# ---------------------------------------------------------------------------

#: Default per-evidence confidence stamped on adapter-emitted
#: BranchEvidence. The SBT primitive's _classify_evidence_outcome
#: requires confidence > 0 to classify a branch as SUCCESS;
#: anything > 0 works, but a tuned default communicates the
#: relative strength to downstream comparators. 0.7 = "moderate
#: read-only evidence, not authoritative".
ADAPTER_DEFAULT_CONFIDENCE: float = 0.7

#: Sorted tuple form of the allowlist used for deterministic
#: branch rotation. ``frozenset`` ordering is unspecified so we
#: sort once at import for stable mod-N rotation.
_ALLOWLIST_ROTATION: Tuple[str, ...] = tuple(
    sorted(READONLY_TOOL_ALLOWLIST),
)


# ---------------------------------------------------------------------------
# Branch-id → resolution_method deterministic rotation
# ---------------------------------------------------------------------------


def _select_method_for_branch(branch_id: str) -> str:
    """Deterministic mapping from branch_id to a resolution_method
    in :data:`READONLY_TOOL_ALLOWLIST`. Same branch_id always
    picks the same method (idempotent across retries).

    Hashes the branch_id with sha256 to spread collisions
    uniformly, then mods by allowlist size. Defensive on garbage
    input — falls back to the first allowlist entry."""
    try:
        if not _ALLOWLIST_ROTATION:
            return ""
        material = str(branch_id or "anon").encode(
            "utf-8", errors="replace",
        )
        h = hashlib.sha256(material).hexdigest()
        # Use the first 8 hex chars (32 bits) as the rotation index.
        idx = int(h[:8], 16) % len(_ALLOWLIST_ROTATION)
        return _ALLOWLIST_ROTATION[idx]
    except Exception:  # noqa: BLE001 — defensive
        return _ALLOWLIST_ROTATION[0] if _ALLOWLIST_ROTATION else ""


def _evidence_kind_for_method(method: str) -> EvidenceKind:
    """Map the resolution_method (an allowlisted tool name) to the
    closest :class:`EvidenceKind` value. Used to type the emitted
    BranchEvidence so downstream comparators can group by kind."""
    # Pure data — no I/O, no raise. Static mapping.
    table = {
        "read_file": EvidenceKind.PATTERN_MATCH,
        "search_code": EvidenceKind.PATTERN_MATCH,
        "get_callers": EvidenceKind.CALLER_GRAPH,
        "list_symbols": EvidenceKind.SYMBOL_LOOKUP,
        "list_dir": EvidenceKind.FILE_READ,
        "glob_files": EvidenceKind.FILE_READ,
        "git_blame": EvidenceKind.FILE_READ,
        "git_log": EvidenceKind.FILE_READ,
        "git_diff": EvidenceKind.FILE_READ,
    }
    return table.get(str(method or ""), EvidenceKind.PATTERN_MATCH)


# ---------------------------------------------------------------------------
# Question composition from BranchTreeTarget
# ---------------------------------------------------------------------------


def _compose_question_text(
    target: BranchTreeTarget,
    prior_evidence: Tuple[BranchEvidence, ...] = (),
) -> str:
    """Build a probe-question text from the target's ambiguity
    payload + prior evidence. NEVER raises.

    The question takes the form:
        ``Resolve <ambiguity_kind> for <decision_id>: <payload>``

    Prior evidence is summarized in a one-liner suffix so later
    branches can ask sharper follow-ups (the SBT runner
    aggregates prior evidence on tie-breaker spawns)."""
    try:
        kind = str(getattr(target, "ambiguity_kind", "") or "ambiguity")
        decision_id = str(getattr(target, "decision_id", "") or "anon")
        payload = getattr(target, "ambiguity_payload", None) or {}
        # Bound payload rendering so a giant dict can't bloat the
        # question.
        payload_str = repr(dict(payload))[:512]
        base = (
            f"Resolve {kind} for {decision_id}: {payload_str}"
        )
        if prior_evidence:
            n = len(prior_evidence)
            base += (
                f" (level >= 1; prior_evidence n={n} — sharpen "
                f"the search beyond what level 0 already covered)"
            )
        return base[:2048]
    except Exception:  # noqa: BLE001 — defensive
        return "Resolve ambiguity (degraded payload)"


# ---------------------------------------------------------------------------
# Adapter — production BranchProber implementation
# ---------------------------------------------------------------------------


class ReadonlyBranchProberAdapter:
    """Production :class:`BranchProber` adapter wrapping Move 5's
    :class:`ReadonlyEvidenceProber`. Each SBT branch becomes one
    ``ProbeQuestion`` + ``resolve()`` call, with
    ``resolution_method`` rotated deterministically across
    :data:`READONLY_TOOL_ALLOWLIST` for branch diversity.

    NEVER raises. Underlying resolver failures map to empty
    evidence tuple → SBT classifies branch as PARTIAL → tree
    INCONCLUSIVE → wrapper INCONCLUSIVE = same outcome as
    executor's existing EXHAUSTED branch. Safe degraded path.
    """

    def __init__(
        self,
        *,
        resolver: Optional[ReadonlyEvidenceProber] = None,
        confidence: float = ADAPTER_DEFAULT_CONFIDENCE,
    ) -> None:
        self._resolver = (
            resolver if resolver is not None else get_default_prober()
        )
        # Clamp confidence to (0, 1].
        try:
            c = float(confidence)
        except (TypeError, ValueError):
            c = ADAPTER_DEFAULT_CONFIDENCE
        self._confidence = max(0.01, min(1.0, c))

    @property
    def allowlist(self) -> FrozenSet[str]:
        """The allowlist this adapter rotates over. Re-exported for
        callers + tests without reaching into the prober."""
        return READONLY_TOOL_ALLOWLIST

    def probe_branch(
        self,
        *,
        target: BranchTreeTarget,
        branch_id: str,
        depth: int,
        prior_evidence: Tuple[BranchEvidence, ...] = (),
    ) -> Tuple[BranchEvidence, ...]:
        """Run one branch via the underlying ReadonlyEvidenceProber.
        NEVER raises."""
        try:
            method = _select_method_for_branch(branch_id)
            # Defense-in-depth: confirm method is in allowlist
            # (should always be by construction, but the SBT
            # runner re-checks anyway).
            if method not in READONLY_TOOL_ALLOWLIST:
                logger.warning(
                    "[SBTBranchProberAdapter] selected method %r "
                    "not in allowlist — returning empty evidence "
                    "for branch %s", method, branch_id,
                )
                return ()
            question_text = _compose_question_text(
                target, prior_evidence,
            )
            question = ProbeQuestion(
                question=question_text,
                resolution_method=method,
            )
            answer = self._resolver.resolve(question)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[SBTBranchProberAdapter] resolver raised "
                "(should not happen): %s — empty evidence "
                "for branch %s", exc, branch_id,
            )
            return ()

        if answer is None:
            return ()
        answer_text = getattr(answer, "answer_text", "") or ""
        if not answer_text:
            # Empty answer → empty evidence → SBT classifies
            # branch PARTIAL.
            return ()

        try:
            content_hash = hashlib.sha256(
                answer_text.encode("utf-8", errors="replace"),
            ).hexdigest()
            kind = _evidence_kind_for_method(method)
            evidence = BranchEvidence(
                kind=kind,
                content_hash=content_hash,
                confidence=self._confidence,
                source_tool=method,
                snippet=answer_text[:256],
            )
            return (evidence,)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[SBTBranchProberAdapter] BranchEvidence ctor "
                "raised: %s — empty evidence for branch %s",
                exc, branch_id,
            )
            return ()


# ---------------------------------------------------------------------------
# Singleton accessor — production wire-up entry point
# ---------------------------------------------------------------------------


_default_adapter: Optional[ReadonlyBranchProberAdapter] = None


def get_default_branch_prober() -> ReadonlyBranchProberAdapter:
    """Process-global :class:`ReadonlyBranchProberAdapter` instance.
    Lazy-constructed on first call. Used by Slice 2's executor
    wire-up so production callers don't have to thread their own
    adapter through the call chain."""
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = ReadonlyBranchProberAdapter()
    return _default_adapter


def reset_default_branch_prober_for_tests() -> None:
    """Test helper — drops the singleton so the next get_default_*
    call constructs a fresh instance. NEVER called from production."""
    global _default_adapter
    _default_adapter = None


# ---------------------------------------------------------------------------
# Slice 3 — Module-owned shipped_code_invariants contribution
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Register adapter's structural invariants. Discovered
    automatically. Returns :class:`ShippedCodeInvariant` instances."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_authority_allowlist(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        allowed = {
            "backend.core.ouroboros.governance.verification.confidence_probe_bridge",
            "backend.core.ouroboros.governance.verification.readonly_evidence_prober",
            "backend.core.ouroboros.governance.verification.speculative_branch",
        }
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
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                for ban in banned_substrings:
                    if ban in module:
                        violations.append(
                            f"line {lineno}: BANNED orchestrator-tier "
                            f"substring {ban!r} in {module!r}"
                        )
                if "backend." in module or (
                    "governance" in module and module
                ):
                    if module not in allowed:
                        violations.append(
                            f"line {lineno}: import outside adapter "
                            f"allowlist: {module!r}"
                        )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"MUST NOT {node.func.id}()"
                        )
        return tuple(violations)

    def _validate_uses_readonly_allowlist(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        """The adapter MUST reference the canonical
        READONLY_TOOL_ALLOWLIST symbol (no parallel/duplicated
        allowlist). Drift here would silently widen the read-only
        scope."""
        violations: list = []
        if "READONLY_TOOL_ALLOWLIST" not in source:
            violations.append(
                "adapter must reference READONLY_TOOL_ALLOWLIST "
                "(no parallel/duplicated allowlist)"
            )
        # Method-rotation table must reference at least
        # 'read_file' and 'search_code' — the two foundational
        # read tools. If they vanish the adapter quietly stopped
        # working.
        for required_tool in ("read_file", "search_code"):
            if f'"{required_tool}"' not in source:
                violations.append(
                    f"_evidence_kind_for_method missing "
                    f"required tool {required_tool!r}"
                )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/verification/"
        "sbt_branch_prober_adapter.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="sbt_branch_prober_adapter_authority_allowlist",
            target_file=target,
            description=(
                "Adapter imports stay within {confidence_probe_bridge, "
                "readonly_evidence_prober, speculative_branch} (+ "
                "registration-contract exemption). Banned: "
                "orchestrator-tier."
            ),
            validate=_validate_authority_allowlist,
        ),
        ShippedCodeInvariant(
            invariant_name="sbt_branch_prober_adapter_uses_readonly_allowlist",
            target_file=target,
            description=(
                "Adapter rotates over the canonical "
                "READONLY_TOOL_ALLOWLIST symbol — no parallel "
                "allowlist. Required tools (read_file, search_code) "
                "remain in the kind-mapping table."
            ),
            validate=_validate_uses_readonly_allowlist,
        ),
    ]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "ADAPTER_DEFAULT_CONFIDENCE",
    "ReadonlyBranchProberAdapter",
    "SBT_BRANCH_PROBER_ADAPTER_SCHEMA_VERSION",
    "get_default_branch_prober",
    "register_shipped_invariants",
    "reset_default_branch_prober_for_tests",
]
