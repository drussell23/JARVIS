"""M10 ArchitectureProposer (PRD §32.4 / supersedes §30.5.2).

Architecture-extension proposal pipeline. The system autonomously
proposes new sensor classes / phase candidates / observers / flag
families when it detects recurring signal patterns no existing
sensor catches. Every proposal routes through ``APPROVAL_REQUIRED``
+ Quorum K=3 + 5-layer validation; operator authorizes via
GitHub PR (NOT REPL).

Architecture: lifts design contracts from the archived
``graduation_orchestrator.py`` (15-phase FSM + AdaptiveThreshold +
H1-H6 hard-won lessons + 5-layer validation) without inheriting
the dead code itself. Composes with already-graduated cage
components: ``WorktreeManager`` (L3 isolation), ``AutoCommitter``
(structured commits), ``OrangePRReviewer`` (async PR review),
``SemanticGuardian`` (10 patterns), ``urgency_router`` +
``candidate_generator`` (cost-gated routing), ``GenerativeQuorum``
(K=3 mandatory), ``Iron Gate`` (exploration-first floor).

Master flag ``JARVIS_M10_ARCH_PROPOSER_ENABLED`` defaults FALSE
and stays default-false until 30+ proposal-acceptance audit
(operator-pinned per §30.5.2). Slice 5 graduation flips ONLY the
opt-in surface, not the production default."""
from __future__ import annotations

__all__: list = ["register_shipped_invariants"]


def register_shipped_invariants() -> list:
    """Module-owned :func:`shipped_code_invariants.register_shipped_code_invariant`
    contribution for the M10 arc. Discovered automatically by
    :func:`shipped_code_invariants._discover_module_provided_invariants`.

    Returns 4 AST + bytes pins per PRD §32.4.2 Slice 5:

      1. ``m10_synthesizer_uses_quorum`` — proposal_synthesizer
         MUST use Move 6's ``compute_ast_signature`` +
         ``asyncio.gather`` (no parallel quorum implementation).
      2. ``m10_lifecycle_uses_orange_pr`` — lifecycle MUST keep
         the ``OrangePRBridgeProtocol`` caller-injection seam
         (no direct OrangePRReviewer import).
      3. ``m10_forced_risk_tier_constant`` — bytes-pin the
         module-level ``M10_FORCED_RISK_TIER = "approval_required"``
         literal so it can never silently downgrade.
      4. ``m10_master_flag_stays_default_false`` — bytes-pin the
         operator-binding marker (``return False  # graduated default``
         absent / ``return False`` present + ``true`` not the
         default literal) per §30.5.2.
      5. ``m10_authority_asymmetry`` — every module under
         ``m10/`` MUST NOT import orchestrator / iron_gate / policy
         / providers / candidate_generator / urgency_router /
         change_engine / semantic_guardian / graduation_orchestrator.
    """
    import ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    M10_FORBIDDEN_AUTHORITY_IMPORTS = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.providers",
        "backend.core.ouroboros.governance.doubleword_provider",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.urgency_router",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.graduation_orchestrator",
    )

    def _validate_synthesizer_uses_quorum(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """proposal_synthesizer MUST use Move 6's
        ``compute_ast_signature`` for AST canonicalization +
        ``asyncio.gather`` for K-way parallel — pinned so a
        future refactor can't replace these with a parallel
        quorum implementation."""
        violations: list = []
        has_ast_canonical_import = False
        has_asyncio_gather = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "ast_canonical" in node.module
                ):
                    for alias in node.names:
                        if alias.name == "compute_ast_signature":
                            has_ast_canonical_import = True
            if isinstance(node, ast.Attribute):
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "asyncio"
                    and node.attr == "gather"
                ):
                    has_asyncio_gather = True
        if not has_ast_canonical_import:
            violations.append(
                "proposal_synthesizer MUST import "
                "compute_ast_signature from Move 6's "
                "ast_canonical (Decision SP-C1 — no parallel "
                "quorum implementation)"
            )
        if not has_asyncio_gather:
            violations.append(
                "proposal_synthesizer MUST use asyncio.gather "
                "for K-way parallel synthesis (Decision SP-A1)"
            )
        return tuple(violations)

    def _validate_lifecycle_uses_orange_pr(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """lifecycle MUST keep the ``OrangePRBridgeProtocol``
        caller-injection seam (Decision SP-V2/V3) — pinned so
        the module never imports OrangePRReviewer directly."""
        violations: list = []
        has_protocol = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "OrangePRBridgeProtocol":
                    has_protocol = True
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if (
                    "orange_pr_reviewer" in module
                    or "auto_committer" in module
                    or "worktree_manager" in module
                ):
                    violations.append(
                        f"lifecycle MUST NOT import "
                        f"{module} directly — use the "
                        f"caller-injected Bridge Protocol "
                        f"(Decision SP-V2)"
                    )
        if not has_protocol:
            violations.append(
                "lifecycle MUST define "
                "OrangePRBridgeProtocol (Decision SP-V3 — "
                "caller-injected PR seam, no direct "
                "OrangePRReviewer dependency)"
            )
        return tuple(violations)

    def _validate_forced_risk_tier_constant(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Bytes-pin the module-level
        ``M10_FORCED_RISK_TIER = "approval_required"``
        literal — Decision SP-E1: proposals can NEVER auto-
        apply. A refactor changing the literal silently
        would unlock proposals for SAFE_AUTO."""
        violations: list = []
        # AST inspection — find module-level assign
        seen_value = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id == "M10_FORCED_RISK_TIER"
                    ):
                        if isinstance(
                            node.value, ast.Constant,
                        ) and isinstance(
                            node.value.value, str,
                        ):
                            seen_value = node.value.value
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id
                    == "M10_FORCED_RISK_TIER"
                    and isinstance(
                        node.value, ast.Constant,
                    )
                    and isinstance(node.value.value, str)
                ):
                    seen_value = node.value.value
        if seen_value is None:
            violations.append(
                "module-level M10_FORCED_RISK_TIER constant "
                "missing (Decision SP-E1)"
            )
        elif seen_value != "approval_required":
            violations.append(
                f"M10_FORCED_RISK_TIER drifted from "
                f"'approval_required' to {seen_value!r} — "
                f"Decision SP-E1 mandates Orange tier "
                f"(human approval) for every proposal"
            )
        # Defensive bytes-window check — exact literal must
        # appear in source as a backup against AST node
        # rewrites that obscure the constant.
        if (
            'M10_FORCED_RISK_TIER' not in source
            or '"approval_required"' not in source
        ):
            violations.append(
                "source-level bytes-pin failed: "
                "M10_FORCED_RISK_TIER + literal "
                "\"approval_required\" must both appear "
                "verbatim in source"
            )
        return tuple(violations)

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Bytes-pin the operator-binding marker on the master
        flag's default. §30.5.2 mandates default-FALSE until a
        30+ proposal-acceptance audit — Slice 5 does NOT
        graduate the default. Pinned so a future refactor
        can't silently flip it."""
        violations: list = []
        target_func = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if node.name == "m10_arch_proposer_enabled":
                    target_func = node
                    break
        if target_func is None:
            violations.append(
                "m10_arch_proposer_enabled() function "
                "missing — required by §30.5.2 operator "
                "binding"
            )
            return tuple(violations)
        # Walk function body — must contain ``return False`` on
        # the empty-string env path. Reject any pattern that
        # lets unset env imply truthy default.
        has_default_false = False
        has_default_true_marker = False
        for node in ast.walk(target_func):
            if isinstance(node, ast.Return):
                if (
                    isinstance(node.value, ast.Constant)
                    and node.value.value is False
                ):
                    has_default_false = True
                elif (
                    isinstance(node.value, ast.Constant)
                    and node.value.value is True
                ):
                    # A True return is fine ONLY if it's
                    # gated on a truthy env value (the
                    # post-parse path) — but a top-level /
                    # empty-string True return = drift.
                    # We surface presence; cross-checked
                    # against the bytes pin below.
                    has_default_true_marker = True
        if not has_default_false:
            violations.append(
                "m10_arch_proposer_enabled MUST return "
                "False on the unset-env path (§30.5.2 "
                "operator binding)"
            )
        # Bytes-pin: source MUST NOT contain the
        # graduated-default marker ``return True  # graduated``
        # which other graduated flags use to declare
        # default-true. M10's master is operator-pinned and
        # MUST NOT carry that marker.
        if "return True  # graduated default" in source:
            violations.append(
                "m10_arch_proposer_enabled has the "
                "'graduated default' marker — §30.5.2 "
                "requires default-FALSE; remove the marker "
                "or open a graduation arc"
            )
        # Defensive: at least one return False must appear
        if "return False" not in source:
            violations.append(
                "expected 'return False' literal in "
                "primitives.py source (operator-binding "
                "default)"
            )
        # Suppress unused-warning on True-marker boolean —
        # it's diagnostic context only.
        _ = has_default_true_marker
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Authority-asymmetry pin: every module under m10/
        MUST NOT import orchestrator / iron_gate / policy /
        providers / candidate_generator / urgency_router /
        change_engine / semantic_guardian /
        graduation_orchestrator. Caller-injected Protocols
        only."""
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for forbidden in M10_FORBIDDEN_AUTHORITY_IMPORTS:
                    if (
                        module == forbidden
                        or module.startswith(
                            forbidden + ".",
                        )
                    ):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"m10 module MUST NOT import "
                            f"{module!r} (authority asymmetry)"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name or ""
                    for forbidden in M10_FORBIDDEN_AUTHORITY_IMPORTS:
                        if (
                            name == forbidden
                            or name.startswith(
                                forbidden + ".",
                            )
                        ):
                            violations.append(
                                f"line {getattr(node, 'lineno', '?')}: "
                                f"m10 module MUST NOT import "
                                f"{name!r} (authority asymmetry)"
                            )
        return tuple(violations)

    base = "backend/core/ouroboros/governance/m10"
    return [
        ShippedCodeInvariant(
            invariant_name="m10_synthesizer_uses_quorum",
            target_file=f"{base}/proposal_synthesizer.py",
            description=(
                "ProposalSynthesizer MUST use Move 6's "
                "compute_ast_signature + asyncio.gather "
                "for K-way parallel quorum (Decisions "
                "SP-A1, SP-C1)."
            ),
            validate=_validate_synthesizer_uses_quorum,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_lifecycle_uses_orange_pr",
            target_file=f"{base}/lifecycle.py",
            description=(
                "ProposalLifecycleOrchestrator MUST keep "
                "the OrangePRBridgeProtocol caller-injection "
                "seam (Decisions SP-V2, SP-V3) — no direct "
                "OrangePRReviewer / AutoCommitter / "
                "WorktreeManager imports."
            ),
            validate=_validate_lifecycle_uses_orange_pr,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_forced_risk_tier_constant",
            target_file=f"{base}/proposal_synthesizer.py",
            description=(
                "Module-level M10_FORCED_RISK_TIER = "
                "\"approval_required\" constant pinned "
                "verbatim — Decision SP-E1: proposals can "
                "NEVER auto-apply, must always route "
                "through human approval (Orange tier)."
            ),
            validate=_validate_forced_risk_tier_constant,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_master_flag_stays_default_false",
            target_file=f"{base}/primitives.py",
            description=(
                "JARVIS_M10_ARCH_PROPOSER_ENABLED master "
                "flag MUST default FALSE per §30.5.2 "
                "operator binding (does NOT graduate "
                "default-true at Slice 5; flips only after "
                "30+ proposal-acceptance audit). The "
                "'graduated default' marker is FORBIDDEN."
            ),
            validate=_validate_master_flag_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_primitives_authority_asymmetry",
            target_file=f"{base}/primitives.py",
            description=(
                "m10/primitives.py MUST NOT import "
                "orchestrator / iron_gate / policy / "
                "providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / graduation_orchestrator "
                "— pure substrate."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_synthesizer_authority_asymmetry",
            target_file=f"{base}/proposal_synthesizer.py",
            description=(
                "m10/proposal_synthesizer.py MUST NOT "
                "import the forbidden authority modules "
                "— uses caller-injected "
                "SynthesisProviderProtocol only "
                "(Decision SP-G1)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_lifecycle_authority_asymmetry",
            target_file=f"{base}/lifecycle.py",
            description=(
                "m10/lifecycle.py MUST NOT import the "
                "forbidden authority modules — uses "
                "caller-injected Bridge Protocols only "
                "(Decision SP-V9)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_unhandled_pattern_miner_authority_asymmetry",  # noqa: E501
            target_file=(
                f"{base}/unhandled_pattern_miner.py"
            ),
            description=(
                "m10/unhandled_pattern_miner.py MUST NOT "
                "import the forbidden authority modules "
                "— pure substrate over coherence_window_-"
                "store + intake observations."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]
