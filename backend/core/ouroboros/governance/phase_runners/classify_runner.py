"""CLASSIFYRunner — Slice 2 of Wave 2 item (5) — PhaseRunner extraction.

Extracts the CLASSIFY phase body from ``orchestrator.py`` lines 1235–1994
into a :class:`PhaseRunner` subclass behind
``JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED`` (default ``false``).

**Zero behavior change per slice.** The runner body is a verbatim
transcription of the inline block with ``self.`` → ``orch.``
substitutions. Parity tests
(``tests/governance/phase_runner/test_classify_runner_parity.py``)
pin the observable output across inline and runner paths.

## Why CLASSIFY for Slice 2

Per the scope doc (``memory/project_wave2_scope_draft.md``):

- Second-smallest extraction (~760 lines vs GENERATE's 1,926)
- Linear — emergency gate, advisor gate, then the actual risk
  classification with 8 prompt-injection blocks + a couple of
  early-terminate paths
- Fully pins down the multi-prompt-injection pattern that ROUTE /
  CONTEXT_EXPANSION / PLAN share in Slice 3

## Three terminal exit paths (all ``status="fail"``)

1. **Emergency protocol ORANGE+** → ``CANCELLED`` with
   ``terminal_reason_code=f"emergency_{level}"``
2. **Operation Advisor BLOCK** → ``CANCELLED`` with
   ``terminal_reason_code="advisor_blocked"``
3. **Risk tier BLOCKED** (policy engine or risk engine) →
   ``CANCELLED`` with ``terminal_reason_code=classification.reason_code``
   + ledger entry with ``OperationState.BLOCKED``

## Success path (``status="ok"``, ``next_phase=ROUTE``)

Runs the 8 prompt-injection blocks (complexity classifier,
consciousness regression check, goal memory, strategic direction,
conversation bridge, semantic index, task board, TDD directive, goal
inference, LastSessionSummary, goals, user preferences), emits INTENT,
optionally runs reasoning-bridge classification, emits intent_chain
heartbeat, advances ctx to ROUTE with ``risk_tier`` stamped, runs the
narrator + dialogue start hooks, then attempts the
ClassifyClarify operator question (default off). Returns
``PhaseResult(next_ctx=<ROUTE ctx>, next_phase=ROUTE, status="ok",
reason="classified", artifacts={"advisory": _advisory})``.

## Artifact handoff: the ``_advisory`` leak

The inline CLASSIFY block produces a local ``_advisory`` that is
consumed downstream at line 2779 by Tier 6 personality-engine voice
lines (``_advisory.chronic_entropy``). The runner preserves this data
flow by returning the advisory object in ``PhaseResult.artifacts``.
The orchestrator delegation hook pulls it back into a local
``_advisory`` before the ROUTE phase continues.

## Dependencies injected via constructor

* ``orchestrator`` — the :class:`Orchestrator` instance. Runner reads:
    - ``_stack.risk_engine``, ``_stack.topology``, ``_stack.ledger``,
      ``_stack.consciousness_bridge``, ``_stack.comm``,
      ``_stack.governed_loop_service``, ``_stack._emergency_engine``,
      ``_stack.policy_engine``
    - ``_config.project_root``
    - ``_build_profile``, ``_record_ledger``
    - ``_reasoning_bridge``, ``_reasoning_narrator`` (opt),
      ``_dialogue_store`` (opt)
* ``serpent`` — the pipeline-wide serpent handle (``None`` in headless).

## Authority invariant

Runner MAY import the same policy / risk_tier symbols that the inline
CLASSIFY block imports (``PolicyEngine``, ``PolicyDecision``,
``RiskTier``) since those are consumed by the inline block as reads
(not widened execution authority). Extraction does not add any new
imports beyond what the verbatim transcription requires.
"""
from __future__ import annotations

import dataclasses as _dc
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.policy_engine import (
    PolicyDecision,
    PolicyEngine,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier

if TYPE_CHECKING:  # pragma: no cover
    from backend.core.ouroboros.governance.orchestrator import Orchestrator


# Use the orchestrator's logger name so observability is identical
# across inline and runner paths (grep-able ``[Orchestrator] ...`` lines).
logger = logging.getLogger("Ouroboros.Orchestrator")


# Phase 1 Slice 1.3.a — register the CLASSIFY/advisor_verdict adapter
# at module load. The adapter converts an ``Advisory`` dataclass into
# a JSON-friendly dict for storage, and reconstitutes the dataclass
# on REPLAY so callers receive the same Python shape they'd have
# received from the live ``_advisor.advise(...)`` call.
def _register_classify_adapter() -> None:
    """Idempotent — safe to import multiple times. Defensive
    (NEVER raises) so a missing determinism module doesn't break
    the classify runner import chain."""
    try:
        from backend.core.ouroboros.governance.determinism.phase_capture import (
            OutputAdapter,
            register_adapter,
        )
        from backend.core.ouroboros.governance.operation_advisor import (
            Advisory,
            AdvisoryDecision,
        )

        def _serialize(advisory: Any) -> Any:
            try:
                return {
                    "decision": str(advisory.decision.value)
                    if hasattr(advisory.decision, "value")
                    else str(advisory.decision),
                    "reasons": [str(r) for r in (advisory.reasons or [])],
                    "blast_radius": int(advisory.blast_radius or 0),
                    "test_coverage": float(advisory.test_coverage or 0.0),
                    "chronic_entropy": float(
                        advisory.chronic_entropy or 0.0,
                    ),
                    "risk_score": float(advisory.risk_score or 0.0),
                    "voice_message": str(advisory.voice_message or ""),
                }
            except Exception:  # noqa: BLE001 — defensive
                # Unknown shape — fall back to repr so storage
                # succeeds even if the dataclass evolves.
                return {
                    "decision": "recommend",  # safe default
                    "reasons": [],
                    "blast_radius": 0,
                    "test_coverage": 0.0,
                    "chronic_entropy": 0.0,
                    "risk_score": 0.0,
                    "voice_message": str(advisory)[:200],
                }

        def _deserialize(stored: Any) -> Any:
            try:
                if not isinstance(stored, dict):
                    return stored
                decision_str = str(stored.get("decision", "recommend"))
                # AdvisoryDecision is str-Enum
                try:
                    decision = AdvisoryDecision(decision_str)
                except ValueError:
                    decision = AdvisoryDecision.RECOMMEND
                return Advisory(
                    decision=decision,
                    reasons=list(stored.get("reasons", [])),
                    blast_radius=int(stored.get("blast_radius", 0)),
                    test_coverage=float(
                        stored.get("test_coverage", 0.0),
                    ),
                    chronic_entropy=float(
                        stored.get("chronic_entropy", 0.0),
                    ),
                    risk_score=float(stored.get("risk_score", 0.0)),
                    voice_message=str(stored.get("voice_message", "")),
                )
            except (ValueError, KeyError, TypeError):
                return stored

        register_adapter(
            phase="CLASSIFY",
            kind="advisor_verdict",
            adapter=OutputAdapter(
                serialize=_serialize,
                deserialize=_deserialize,
                name="advisor_verdict_adapter",
            ),
        )
    except Exception:  # noqa: BLE001 — defensive (import-time)
        # Determinism module unavailable — wiring still works as a
        # pure passthrough via capture_phase_decision's internal
        # short-circuit. No log spam at import time.
        pass


_register_classify_adapter()


class CLASSIFYRunner(PhaseRunner):
    """Risk classification + prompt memory injection + ROUTE hand-off.

    Verbatim transcription of ``orchestrator.py`` lines 1235–1994.
    """

    phase = OperationPhase.CLASSIFY

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator
        _serpent = self._serpent

        # ---- VERBATIM transcription of orchestrator.py 1235-1994 -------
        # Any divergence from the inline block is a parity-test failure.

        # ── JARVIS Tier 2: Emergency Protocol Check ──────────────────────
        # If emergency level is ORANGE or higher, block autonomous operations
        try:
            from backend.core.ouroboros.governance.emergency_protocols import (
                EmergencyProtocolEngine, AlertLevel,
            )
            _emergency = getattr(orch._stack, "_emergency_engine", None)
            if _emergency is not None and not _emergency.can_proceed():
                state = _emergency.get_state()
                logger.warning(
                    "[Orchestrator] Emergency level %s — operation blocked (op=%s)",
                    state.level.name, ctx.op_id,
                )
                if _serpent:
                    await _serpent.stop(success=False)
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code=f"emergency_{state.level.name.lower()}",
                )
                return PhaseResult(
                    next_ctx=ctx,
                    next_phase=None,
                    status="fail",
                    reason=f"emergency_{state.level.name.lower()}",
                    artifacts={
                        "advisory": None,
                        "consciousness_bridge": None,
                    },
                )
        except ImportError:
            pass
        except Exception:
            pass

        # ── JARVIS Tier 1: Operation Advisor ────────────────────────────
        # "Sir, I wouldn't recommend that."
        _advisory = None
        try:
            from backend.core.ouroboros.governance.operation_advisor import (
                OperationAdvisor, AdvisoryDecision, infer_read_only_intent,
            )
            # Stamp read-only intent onto the hash-chained context BEFORE
            # advising. The Advisor's bypass of blast_radius + test_coverage
            # is mathematically safe only because ctx.is_read_only is
            # enforced downstream by tool_executor (mutating tools refused)
            # and the orchestrator's APPLY short-circuit.
            if not ctx.is_read_only:
                _inferred_ro = infer_read_only_intent(ctx.description)
                if _inferred_ro:
                    ctx = ctx.with_read_only_intent(True)
                    logger.info(
                        "[Orchestrator] Read-only intent inferred op=%s "
                        "— Advisor blast/coverage bypass active; tool_executor "
                        "will refuse mutations; APPLY phase will short-circuit",
                        ctx.op_id,
                    )
            _advisor = OperationAdvisor(orch._config.project_root)

            # Phase 1 Slice 1.3.a — wrap the advisor verdict in
            # capture_phase_decision so RECORD/REPLAY/VERIFY work for
            # the CLASSIFY phase's load-bearing decision. When the
            # master flag is off, this is a pure passthrough that
            # calls _advisor.advise(...) directly with negligible
            # overhead. Adapter is registered at module load below.
            try:
                from backend.core.ouroboros.governance.determinism.phase_capture import (
                    capture_phase_decision,
                )

                async def _advise_op() -> Any:
                    return _advisor.advise(
                        ctx.target_files, ctx.description, ctx.op_id,
                        is_read_only=ctx.is_read_only,
                    )

                _advisory = await capture_phase_decision(
                    op_id=ctx.op_id,
                    phase="CLASSIFY",
                    kind="advisor_verdict",
                    ctx=ctx,
                    compute=_advise_op,
                    extra_inputs={
                        "description_hash": (
                            len(ctx.description or "")
                        ),
                        "target_count": len(ctx.target_files or ()),
                        "is_read_only": bool(ctx.is_read_only),
                    },
                )
            except Exception:  # noqa: BLE001 — defensive
                # Capture wrapper failed → fall back to direct call.
                # Determinism is best-effort; advisor verdict must
                # always materialize.
                logger.debug(
                    "[Orchestrator] capture_phase_decision failed for "
                    "CLASSIFY/advisor_verdict; falling back to direct "
                    "advise call", exc_info=True,
                )
                _advisory = _advisor.advise(
                    ctx.target_files, ctx.description, ctx.op_id,
                    is_read_only=ctx.is_read_only,
                )

            if _advisory.decision == AdvisoryDecision.BLOCK:
                logger.warning(
                    "[Orchestrator] Advisor BLOCKED operation: %s (op=%s)",
                    "; ".join(_advisory.reasons), ctx.op_id,
                )
                if _serpent:
                    await _serpent.stop(success=False)
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="advisor_blocked",
                )
                return PhaseResult(
                    next_ctx=ctx,
                    next_phase=None,
                    status="fail",
                    reason="advisor_blocked",
                    artifacts={
                        "advisory": _advisory,
                        "consciousness_bridge": None,
                    },
                )

            if _advisory.decision != AdvisoryDecision.RECOMMEND:
                # Inject advisory into context for generation awareness
                _adv_prompt = _advisor.format_for_prompt(_advisory)
                if _adv_prompt:
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=_existing + "\n\n" + _adv_prompt,
                        strategic_memory_digest=ctx.strategic_memory_digest,
                    )

                # Voice the warning
                if _advisory.voice_message and orch._reasoning_narrator is not None:
                    try:
                        orch._reasoning_narrator.record_classify(
                            ctx.op_id, _advisory.decision.value,
                            _advisory.voice_message,
                        )
                    except Exception:
                        pass

                logger.info(
                    "[Orchestrator] Advisor: %s (risk=%.2f) — %s",
                    _advisory.decision.value, _advisory.risk_score,
                    _advisory.reasons[0] if _advisory.reasons else "no specific reason",
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Advisor failed", exc_info=True)

        # ---- Phase 1: CLASSIFY ----
        profile = orch._build_profile(ctx)
        classification = orch._stack.risk_engine.classify(profile)
        risk_tier = classification.tier

        # ---- Complexity + Persistence classification (Assimilation Gate) ----
        _complexity_result = None
        try:
            from backend.core.ouroboros.governance.complexity_classifier import (
                OperationComplexityClassifier,
            )
            _classifier = OperationComplexityClassifier(
                topology=getattr(orch._stack, "topology", None),
                ledger=getattr(orch._stack, "ledger", None),
            )
            _complexity_result = _classifier.classify(
                description=ctx.description,
                target_files=list(ctx.target_files),
            )
            # Stamp complexity on context for downstream routing decisions.
            # task_complexity is a declared field on OperationContext, so
            # object.__setattr__ values survive dataclasses.replace() in
            # advance() and all with_*() methods.
            object.__setattr__(ctx, "task_complexity", _complexity_result.complexity.value)

            logger.info(
                "[Orchestrator] \U0001f4ca Complexity: %s, Persistence: %s, auto_approve=%s, fast_path=%s [%s]",
                _complexity_result.complexity.value,
                _complexity_result.persistence.value,
                _complexity_result.auto_approve_eligible,
                _complexity_result.fast_path_eligible,
                ctx.op_id,
            )
        except Exception:
            logger.debug("[Orchestrator] ComplexityClassifier not available", exc_info=True)

        # ---- Consciousness regression detection (ProphecyEngine + MemoryEngine) ----
        _consciousness_bridge = getattr(orch._stack, "consciousness_bridge", None)
        if _consciousness_bridge is None:
            # Check if GLS has the bridge (wired by Zone 6.12)
            _gls = getattr(orch._stack, "governed_loop_service", None)
            if _gls is not None:
                _consciousness_bridge = getattr(_gls, "_consciousness_bridge", None)
        if _consciousness_bridge is not None:
            try:
                _regression = await _consciousness_bridge.assess_regression_risk(
                    list(ctx.target_files)
                )
                if _regression and _regression.get("risk_level") in ("high", "critical"):
                    logger.warning(
                        "[Orchestrator] Consciousness regression alert: %s risk for %s — %s [%s]",
                        _regression["risk_level"],
                        ctx.target_files,
                        _regression.get("reasoning", ""),
                        ctx.op_id,
                    )
            except Exception:
                logger.debug("[Orchestrator] Consciousness regression check failed", exc_info=True)

        # ---- Goal Memory injection (cross-session learning via ChromaDB) ----
        _goal_memory_bridge = None
        _gls_for_gmb = getattr(orch._stack, "governed_loop_service", None)
        if _gls_for_gmb is not None:
            _goal_memory_bridge = getattr(_gls_for_gmb, "_goal_memory_bridge", None)
        if _goal_memory_bridge is not None:
            try:
                _goal_ctx = await _goal_memory_bridge.get_relevant_context(
                    description=ctx.description,
                    target_files=ctx.target_files,
                )
                if _goal_ctx:
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=_existing + "\n\n" + _goal_ctx,
                        strategic_memory_digest=ctx.strategic_memory_digest,
                    )
            except Exception:
                logger.debug("[Orchestrator] Goal memory injection failed", exc_info=True)

        # ---- Strategic Direction injection (Manifesto + architecture docs) ----
        _strategic_svc = None
        if _gls_for_gmb is not None:
            _strategic_svc = getattr(_gls_for_gmb, "_strategic_direction", None)
        if _strategic_svc is not None and getattr(_strategic_svc, "is_loaded", False):
            try:
                _strat_prompt = _strategic_svc.format_for_prompt()
                if _strat_prompt:
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id="manifesto-v4",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=_strat_prompt + "\n\n" + _existing,
                        strategic_memory_digest=(
                            ctx.strategic_memory_digest
                            or _strategic_svc.digest[:500]
                        ),
                    )
                    logger.debug(
                        "[Orchestrator] Strategic direction injected (%d principles)",
                        len(_strategic_svc.principles),
                    )
            except Exception:
                logger.debug("[Orchestrator] Strategic direction injection failed", exc_info=True)

        # ---- ConversationBridge (v0.1): TUI dialogue as untrusted soft bias ----
        # Injects the user's recent TUI turns BETWEEN the trusted manifesto
        # block (above) and the trusted goals + user-preferences blocks
        # (below). Untrusted-in-the-middle ordering preserves attention-
        # mechanism dominance for FORBIDDEN_PATH / style prefs (which come
        # last) while still surfacing conversational intent to the model.
        #
        # Authority invariant (plan v0.1 §9): this block has zero authority
        # over Iron Gate, UrgencyRouter, risk tier, policy engine,
        # FORBIDDEN_PATH, tool protected-path checks, or approval gating.
        # Consumed ONLY by StrategicDirection at this injection site.
        try:
            from backend.core.ouroboros.governance.conversation_bridge import (
                get_default_bridge,
            )
            _bridge = get_default_bridge()
            (
                _bridge_enabled,
                _n_turns,
                _n_user,
                _n_assistant,
                _n_postmortem,
                _chars_in,
                _redacted,
                _hash8,
            ) = _bridge.inject_metrics()
            if _bridge_enabled:
                _conv_prompt = _bridge.format_for_prompt()
                if _conv_prompt:
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=ctx.strategic_intent_id or "conv-bridge-v1",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=(
                            _existing + "\n\n" + _conv_prompt
                            if _existing else _conv_prompt
                        ),
                        strategic_memory_digest=ctx.strategic_memory_digest,
                    )
                # §8 one-line observability contract (v1.1 source breakdown).
                # Logged whether or not there were turns to inject —
                # operators need to see that the wiring fired.
                logger.info(
                    "[ConversationBridge] op=%s enabled=true n_turns=%d "
                    "n_user=%d n_assistant=%d n_postmortem=%d chars_in=%d "
                    "inject_site=context_expansion redacted=%s hash8=%s",
                    ctx.op_id, _n_turns, _n_user, _n_assistant, _n_postmortem,
                    _chars_in, _redacted, _hash8,
                )
            else:
                # §8 §7-tweak: DEBUG line at inject site when master switch
                # is off so "is wiring live?" is answerable without content.
                logger.debug(
                    "[ConversationBridge] op=%s enabled=false "
                    "inject_site=context_expansion",
                    ctx.op_id,
                )
        except Exception:
            logger.debug(
                "[Orchestrator] ConversationBridge injection skipped",
                exc_info=True,
            )

        # ---- SemanticIndex v0.1: recency-weighted focus + closures ----
        # Soft semantic prior drawn from the recency-weighted centroid
        # over recent commits + active goals + recent conversation.
        # Injected BETWEEN the ConversationBridge block (above) and the
        # Goals block (below) so the ordering reads top-to-bottom as:
        # Strategic → Bridge (untrusted dialogue) → Semantic (untrusted
        # prior) → Goals (trusted) → UserPreferences (highest trust).
        #
        # Authority invariant: this block has **zero** authority over
        # Iron Gate, UrgencyRouter, risk tier, policy engine, FORBIDDEN_PATH,
        # or approval gating. It affects ONLY the prompt surface the model
        # reads at CONTEXT_EXPANSION — §4 (data sovereignty, local
        # embedder) + §8 (hashes + counts, no raw vectors in logs).
        try:
            from backend.core.ouroboros.governance.semantic_index import (
                get_default_index,
            )
            _semi = get_default_index(orch._config.project_root)
            # Lazy build (hits interval gate on repeat).
            _semi.build()
            _semi_prompt = _semi.format_prompt_sections()
            if _semi_prompt:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=ctx.strategic_intent_id or "semantic-v1",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=(
                        _existing + "\n\n" + _semi_prompt
                        if _existing else _semi_prompt
                    ),
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                _semi_stats = _semi.stats()
                logger.info(
                    "[SemanticIndex] op=%s corpus_n=%d centroid_hash8=%s "
                    "inject_site=context_expansion prompt_chars=%d",
                    ctx.op_id, _semi_stats.corpus_n,
                    _semi_stats.centroid_hash8, len(_semi_prompt),
                )
            else:
                logger.debug(
                    "[SemanticIndex] op=%s no prompt section (disabled or empty)",
                    ctx.op_id,
                )
        except Exception:
            logger.debug(
                "[Orchestrator] SemanticIndex injection skipped",
                exc_info=True,
            )

        # ---- TaskBoard advisory prompt injection (Gap #5 Slice 3) ----
        #
        # Read-only + authority-free. We do NOT lazily create a board
        # here — only render when the model has already touched a task
        # tool during this op (i.e. a board exists in the registry).
        # Avoids injecting an empty "Current tasks" section on every
        # op. Per authorization: NEVER gates Iron Gate / policy /
        # approval (Manifesto §1 + §6). Tier -1 sanitation inside
        # TaskBoard.render_prompt_section() handles model content
        # safety — we don't fight the sanitizer here.
        try:
            from backend.core.ouroboros.governance.task_tool import (
                _BOARDS,
            )
            _tb = _BOARDS.get(ctx.op_id)
            if _tb is not None:
                _tb_prompt = _tb.render_prompt_section()
                if _tb_prompt:
                    _tb_existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=(
                            ctx.strategic_intent_id or "task-board-v1"
                        ),
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=(
                            _tb_existing + "\n\n" + _tb_prompt
                            if _tb_existing else _tb_prompt
                        ),
                        strategic_memory_digest=ctx.strategic_memory_digest,
                    )
                    logger.info(
                        "[TaskBoard] op=%s inject_site=context_expansion "
                        "prompt_chars=%d",
                        ctx.op_id, len(_tb_prompt),
                    )
        except Exception:
            logger.debug(
                "[Orchestrator] TaskBoard injection skipped", exc_info=True,
            )

        # ---- TDD directive (Feature 1 V1 — prompt contract, NOT red-green) ----
        #
        # When the intent envelope carries evidence["tdd_mode"]=True,
        # prepend a prompt directive instructing the model to emit
        # tests + impl together (test file first in files: [...]).
        # Honest scope: this is a prompt contract, not a red-green
        # proof. True test-first orchestration (run tests → confirm
        # fail → generate impl → run tests → confirm pass) is a
        # separate multi-commit project scoped for V1.1. The V1
        # module ships the declarative layer so ops can be marked
        # TDD now; V1.1 flips the flag from "prompt hint" to
        # "pipeline sub-phase trigger" without client-side changes.
        try:
            from backend.core.ouroboros.governance.tdd_directive import (
                is_tdd_op,
                tdd_prompt_directive,
            )
            if is_tdd_op(ctx):
                _tdd_text = tdd_prompt_directive()
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=ctx.strategic_intent_id or "tdd-v1",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=(
                        _existing + "\n\n" + _tdd_text
                        if _existing else _tdd_text
                    ),
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[TDDDirective] op=%s tdd_mode=true directive_chars=%d "
                    "scope=prompt_contract_not_red_green",
                    ctx.op_id, len(_tdd_text),
                )
        except Exception:
            logger.debug(
                "[Orchestrator] TDD directive injection skipped",
                exc_info=True,
            )

        # ---- Goal inference — hypothesized direction from multi-signal cross-corr ----
        #
        # Closes the "read the room" gap: watch commits, REPL inputs,
        # memory, completed ops, file hotspots, and declared goals;
        # synthesize ranked hypotheses about where the operator is
        # headed. Injected as a clearly-labeled "Inferred Direction
        # (hypotheses — not declared goals)" section so the model
        # weights it BELOW explicit goals. Default OFF, fail-closed.
        #
        # Authority invariant: hypotheses inform prompt surface only.
        # They NEVER affect risk tier, route, guardian findings, gate
        # verdicts, or approval. Operator accepts/rejects via /infer.
        try:
            from backend.core.ouroboros.governance.goal_inference import (
                GoalInferenceEngine,
                get_default_engine,
                inference_enabled,
                render_prompt_section,
            )
            if inference_enabled():
                _engine = get_default_engine(orch._config.project_root)
                if _engine is None:
                    _engine = GoalInferenceEngine(
                        repo_root=orch._config.project_root,
                    )
                _inf_result = _engine.build()
                _inf_text = render_prompt_section(_inf_result)
                if _inf_text:
                    _existing = getattr(
                        ctx, "strategic_memory_prompt", "",
                    ) or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=(
                            ctx.strategic_intent_id or "goal-inference-v1"
                        ),
                        strategic_memory_fact_ids=(
                            ctx.strategic_memory_fact_ids
                        ),
                        strategic_memory_prompt=(
                            _existing + "\n\n" + _inf_text
                            if _existing else _inf_text
                        ),
                        strategic_memory_digest=(
                            ctx.strategic_memory_digest
                        ),
                    )
                    logger.info(
                        "[GoalInference] op=%s injected hypotheses=%d "
                        "top_conf=%.2f chars=%d",
                        ctx.op_id,
                        min(
                            len(_inf_result.inferred),
                            # top_k applied inside render
                            5,
                        ),
                        (_inf_result.inferred[0].confidence
                         if _inf_result.inferred else 0.0),
                        len(_inf_text),
                    )
        except Exception:
            logger.debug(
                "[Orchestrator] Goal inference injection skipped",
                exc_info=True,
            )

        # ---- LastSessionSummary v0.1: session-to-session episodic continuity ----
        # Read-only structured summary of past session(s), rendered as
        # a dense untrusted block. Injected between SemanticIndex (above)
        # and Goals (below) so the untrusted stack stays contiguous:
        # Strategic → Bridge → Semantic → LastSession → Goals → UserPrefs.
        # Helper extracted for integration-test coverage of the composed
        # CONTEXT_EXPANSION prompt (see test_last_session_summary_composition).
        from backend.core.ouroboros.governance.orchestrator import (
            _inject_last_session_summary_impl,
        )
        ctx = _inject_last_session_summary_impl(orch._config.project_root, ctx)

        # ---- P2.4 + Week 2: Goal-directed context injection ----
        # Append the *most relevant* active user goals to the strategic
        # memory prompt so the generation model aligns its decisions with
        # current priorities. Scoped by target_files + description so a
        # noisy goal tracker doesn't hijack unrelated ops.
        #
        # Increment 3: after prompt injection, compute the full activity
        # entry set (direct matches + descendant credits + optional
        # sibling bumps) and append to the GoalActivityLedger. Every op
        # that reaches CLASSIFY writes at least one row so the session-end
        # drift aggregator sees it as "reached CLASSIFY", even when no
        # goal scored.
        try:
            from backend.core.ouroboros.governance.strategic_direction import (
                GoalActivityLedger,
                GoalTracker,
                get_active_session_id,
            )
            _goal_tracker = GoalTracker(orch._config.project_root)
            _goal_prompt = _goal_tracker.format_for_prompt(
                target_files=list(ctx.target_files),
                description=ctx.description or "",
            )
            if _goal_prompt:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=ctx.strategic_intent_id or "goals-v1",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _goal_prompt if _existing else _goal_prompt,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.debug(
                    "[Orchestrator] Goal context injected (%d active / scoped)",
                    len(_goal_tracker.active_goals),
                )

            # Activity ledger append (Increment 3). Ledger-only — does
            # not feed intake priority math. Zero-match ops still get a
            # marker row so the drift denominator counts them.
            _session_id = get_active_session_id() or ""
            if _session_id:
                try:
                    _activity_entries = _goal_tracker.compute_activity_entries(
                        description=ctx.description or "",
                        target_files=list(ctx.target_files),
                    )
                    GoalActivityLedger(orch._config.project_root).append(
                        session_id=_session_id,
                        op_id=ctx.op_id,
                        entries=_activity_entries,
                    )
                    logger.debug(
                        "[Orchestrator] GoalActivity ledger: wrote %d entries for op=%s",
                        len(_activity_entries) or 1,  # 1 marker row on zero-match
                        ctx.op_id,
                    )
                except Exception:
                    logger.debug(
                        "[Orchestrator] GoalActivity ledger append failed",
                        exc_info=True,
                    )
        except Exception:
            logger.debug("[Orchestrator] Goal injection skipped", exc_info=True)

        # ---- Task #195: User Preference Memory injection ----
        # Append typed user-preference memories (facts about the user,
        # feedback rules, forbidden paths, style choices) scoped by
        # relevance to the current op. Zero model inference — pure
        # deterministic scoring. Empty when no memory matches the op
        # shape, so silent on fresh repos.
        try:
            from backend.core.ouroboros.governance.user_preference_memory import (
                get_default_store,
            )
            _user_prefs = get_default_store(orch._config.project_root)
            _pref_prompt = _user_prefs.format_for_prompt(
                target_files=list(ctx.target_files),
                description=ctx.description,
                risk_tier=str(getattr(ctx, "risk_tier", "") or ""),
            )
            if _pref_prompt:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=ctx.strategic_intent_id or "user-prefs-v1",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=(
                        _existing + "\n\n" + _pref_prompt if _existing else _pref_prompt
                    ),
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.debug(
                    "[Orchestrator] User preferences injected (%d chars)",
                    len(_pref_prompt),
                )
        except Exception:
            logger.debug("[Orchestrator] User preference injection skipped", exc_info=True)

        # ---- Policy engine check (declarative YAML rules) ----
        # Evaluated BEFORE the risk-engine BLOCKED short-circuit so that
        # explicit deny rules in policy files can override the risk engine.
        # Wrapped in hasattr + try/except so the pipeline is never broken
        # by a missing or misconfigured policy_engine attribute.
        if hasattr(orch._stack, "policy_engine") and orch._stack.policy_engine is not None:
            try:
                _policy_engine: PolicyEngine = orch._stack.policy_engine
                for _tf in ctx.target_files:
                    _policy_decision = _policy_engine.classify(tool="edit", target=str(_tf))
                    if _policy_decision is PolicyDecision.BLOCKED:
                        logger.info(
                            "[Orchestrator] PolicyEngine BLOCKED op=%s target=%r",
                            ctx.op_id, _tf,
                        )
                        risk_tier = RiskTier.BLOCKED
                        break
            except Exception:
                logger.warning(
                    "[Orchestrator] PolicyEngine raised during CLASSIFY for op=%s; continuing",
                    ctx.op_id, exc_info=True,
                )

        if risk_tier is RiskTier.BLOCKED:
            ctx = ctx.advance(
                OperationPhase.CANCELLED,
                risk_tier=risk_tier,
                terminal_reason_code=classification.reason_code,
            )
            await orch._record_ledger(
                ctx,
                OperationState.BLOCKED,
                {
                    "reason_code": classification.reason_code,
                    "risk_tier": risk_tier.name,
                },
            )
            return PhaseResult(
                next_ctx=ctx,
                next_phase=None,
                status="fail",
                reason=classification.reason_code,
                artifacts={
                    "advisory": _advisory,
                    "consciousness_bridge": _consciousness_bridge,
                },
            )

        # Announce operation start — VoiceNarrator fires here (INTENT type)
        try:
            await orch._stack.comm.emit_intent(
                op_id=ctx.op_id,
                goal=ctx.description,
                target_files=list(ctx.target_files),
                risk_tier=risk_tier.name,
                blast_radius=len(ctx.target_files),
            )
        except Exception:
            logger.debug("emit_intent failed for op=%s", ctx.op_id, exc_info=True)

        # ---- Reasoning chain classification (optional, pre-routing) ----
        reasoning_result = None
        if orch._reasoning_bridge and orch._reasoning_bridge.is_active:
            try:
                reasoning_result = await orch._reasoning_bridge.classify_with_reasoning(
                    command=ctx.description,
                    op_id=ctx.op_id,
                )
            except Exception:
                logger.debug("Reasoning chain bridge error", exc_info=True)

        # P3.1: Emit intent chain heartbeat — full reasoning chain for the
        # SerpentFlow display.  Deterministic: all data already computed.
        try:
            _chain_payload: Dict[str, Any] = {
                "phase": "intent_chain",
                "risk_tier": risk_tier.name,
                "complexity": (
                    _complexity_result.complexity.value
                    if _complexity_result is not None else ""
                ),
                "auto_approve": (
                    _complexity_result.auto_approve_eligible
                    if _complexity_result is not None else False
                ),
                "fast_path": (
                    _complexity_result.fast_path_eligible
                    if _complexity_result is not None else False
                ),
            }
            await orch._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="intent_chain", progress_pct=10.0,
                **_chain_payload,
            )
        except Exception:
            pass  # Intent chain visibility is best-effort

        # Advance to ROUTE with risk_tier set (and optional reasoning result)
        if _serpent: _serpent.update_phase("ROUTE")
        ctx = ctx.advance(
            OperationPhase.ROUTE,
            risk_tier=risk_tier,
            reasoning_chain_result=reasoning_result,
        )

        # ── P0 Wiring: Start ReasoningNarrator + OperationDialogue ──────
        if orch._reasoning_narrator is not None:
            try:
                orch._reasoning_narrator.start_trace(ctx.op_id)
                orch._reasoning_narrator.record_classify(
                    ctx.op_id,
                    risk_tier.value if hasattr(risk_tier, "value") else str(risk_tier),
                    f"files={list(ctx.target_files)[:3]}, "
                    f"complexity={getattr(_complexity_result, 'complexity', 'unknown')}",
                )
            except Exception:
                pass

        if orch._dialogue_store is not None:
            try:
                from backend.core.ouroboros.governance.entropy_calculator import extract_domain_key
                _dk = extract_domain_key(ctx.target_files, ctx.description)
                orch._dialogue_store.start_dialogue(
                    op_id=ctx.op_id,
                    domain_key=_dk,
                    description=ctx.description,
                    target_files=ctx.target_files,
                )
                _dialogue = orch._dialogue_store.get_active(ctx.op_id)
                if _dialogue:
                    _dialogue.add_entry(
                        "CLASSIFY",
                        f"Risk={risk_tier}, complexity={getattr(_complexity_result, 'complexity', 'unknown')}",
                    )
            except Exception:
                pass

        # ---- ClassifyClarify: one operator question at the CLASSIFY→ROUTE boundary ----
        #
        # Closes the "intake description is ambiguous" gap. Narrow
        # ambiguity heuristic (short desc + no target files, or generic
        # target list, or no goal-keyword match). On trigger, ask the
        # operator ONE concise question with a bounded timeout. The
        # answer enriches ctx.description + evidence only — it has NO
        # authority over risk classification, routing law, SemanticGuardian
        # findings, or any deterministic engine input (Manifesto §1
        # Boundary Principle).
        #
        # Default OFF (JARVIS_CLASSIFY_CLARIFY_ENABLED=0). Opt-in means
        # no session is interrupted until the operator explicitly
        # enables the feature + the heuristic actually fires.
        try:
            from backend.core.ouroboros.governance.classify_clarify import (
                ask_operator as _clarify_ask,
                merge_into_context as _clarify_merge,
                clarify_enabled as _clarify_enabled,
            )
            if _clarify_enabled():
                # Extract goal keywords from the active GoalTracker so
                # the heuristic can check "no goal keyword match".
                _goal_keywords: tuple = ()
                try:
                    from backend.core.ouroboros.governance.strategic_direction import (
                        GoalTracker,
                    )
                    _kws: list = []
                    for _g in GoalTracker(
                        orch._config.project_root,
                    ).active_goals:
                        _kws.extend(getattr(_g, "keywords", ()) or ())
                    _goal_keywords = tuple(_kws)
                except Exception:
                    _goal_keywords = ()
                _clarify_response = await _clarify_ask(
                    op_id=ctx.op_id,
                    description=ctx.description or "",
                    target_files=tuple(ctx.target_files or ()),
                    goal_keywords=_goal_keywords,
                )
                if _clarify_response.outcome == "answered":
                    # Merge the sanitized answer into the description.
                    # The risk classifier has ALREADY run above — we do
                    # not re-classify. The clarification only affects
                    # downstream prompt content (description + evidence).
                    _new_desc, _patch = _clarify_merge(
                        original_description=ctx.description or "",
                        response=_clarify_response,
                    )
                    try:
                        ctx = _dc.replace(ctx, description=_new_desc)
                    except Exception:
                        logger.debug(
                            "[Orchestrator] ClassifyClarify ctx merge skipped",
                            exc_info=True,
                        )
        except Exception:
            logger.debug(
                "[Orchestrator] ClassifyClarify skipped",
                exc_info=True,
            )
        # ---- end verbatim transcription --------------------------------

        return PhaseResult(
            next_ctx=ctx,
            next_phase=OperationPhase.ROUTE,
            status="ok",
            reason="classified",
            artifacts={
                "advisory": _advisory,
                "consciousness_bridge": _consciousness_bridge,
            },
        )


__all__ = ["CLASSIFYRunner"]
