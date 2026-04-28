"""GATERunner — Slice 4a.2 of Wave 2 item (5).

Extracts orchestrator.py GATE body (~600 lines, post-Slice-4a.1 lines
~5496-6098) into a :class:`PhaseRunner` behind
``JARVIS_PHASE_RUNNER_GATE_EXTRACTED`` (default ``false``).

**Zero behavior change per slice.** Verbatim transcription with
``self.`` → ``orch.`` substitutions.

## GATE body composition (ORDER MATTERS for approval semantics)

1. ``self._stack.can_write`` policy check (hard terminal on deny)
2. SecurityReviewer (LLM-as-a-Judge) — BLOCK terminal or WARN alert
3. Diff-Aware Similarity Gate — escalates to APPROVAL_REQUIRED
4. Frozen autonomy tier ("observe" → APPROVAL_REQUIRED)
5. JARVIS_RISK_CEILING env floor (REPL /risk)
6. SemanticGuardian (10 AST/regex patterns, hard→APPROVAL_REQUIRED,
   soft→NOTIFY_APPLY) — always emits the ``[SemanticGuard]`` structured line
7. REVIEW subagent shadow (observer-only)
8. MutationGate (enforce mode upgrades to APPROVAL_REQUIRED or BLOCKED)
9. MIN_RISK_TIER floor (paranoia + quiet hours composed)
10. RR Pass B Slice 2b: ORDER_2_GOVERNANCE floor (governance-code
    paths from .jarvis/order2_manifest.yaml escalate to the
    Order-2 tier, strictly above BLOCKED). Default-off behind the
    dual flag (Slice 1 manifest + Slice 2 risk-class).
11. Phase 5a-green: SAFE_AUTO diff preview + cancel-check window
12. Phase 5b: NOTIFY_APPLY diff preview + cancel-check window

## Four terminal exit paths

* ``gate_blocked:<reason>`` (can_write denied)
* ``security_review_blocked`` (SecurityReviewer BLOCK verdict)
* ``user_rejected_safe_auto_preview`` (5a green cancel)
* ``user_rejected_notify_apply`` (5b yellow cancel)

## Success path — ``next_phase = APPROVE``

ctx stays in GATE phase (APPROVE inline decides its own advance). The
``risk_tier`` local mutates extensively inside GATE and is the primary
artifact passed back for APPROVE to read.

## Cross-phase artifact — ``risk_tier``

GATE mutates ``risk_tier`` at up to 7 sites: SimilarityGate (→APPROVAL),
frozen_tier==observe (→APPROVAL), JARVIS_RISK_CEILING (env floor),
SemanticGuardian (soft/hard upgrade), MutationGate (block/approval upgrade),
MIN_RISK_TIER (paranoia/quiet hours floor), ORDER_2_GOVERNANCE floor
(RR Pass B governance-code paths). Runner returns the final
``risk_tier`` via ``PhaseResult.artifacts["risk_tier"]`` so the
orchestrator hook rebinds the local before APPROVE-inline reads it.

## Dependencies injected via constructor

* ``orchestrator`` — reads ``_stack.*``, ``_config.project_root``,
  ``_run_review_shadow``, ``_is_cancel_requested``,
  ``_discover_tests_for_gate``, ``_record_ledger``
* ``serpent`` — pipeline serpent handle (optional)
* ``best_candidate`` — from VALIDATE artifacts (37 refs in GATE)
* ``risk_tier`` — from CLASSIFY (mutated inside GATE)

## Authority invariant

Runner imports match inline block's surface: ``ledger``, ``op_context``,
``phase_runner``, ``risk_engine.RiskTier``, plus function-local imports
(``security_reviewer``, ``similarity_gate``, ``semantic_guardian``,
``mutation_gate``, ``risk_tier_floor``). No execution-authority widening.

## Operator-visible affordance preserved

The ``_human_is_watching()`` TTY detection (SAFE_AUTO preview trigger)
is resolved through the orchestrator module so env-driven overrides
(``JARVIS_DIFF_PREVIEW_ALL``) still work identically.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier

if TYPE_CHECKING:  # pragma: no cover
    from backend.core.ouroboros.governance.orchestrator import Orchestrator


logger = logging.getLogger("Ouroboros.Orchestrator")


class GATERunner(PhaseRunner):
    """Verbatim transcription of orchestrator.py GATE block (~5496-6098)."""

    phase = OperationPhase.GATE

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
        best_candidate: Optional[Any],
        risk_tier: RiskTier,
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent
        self._best_candidate = best_candidate
        self._risk_tier = risk_tier

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator
        _serpent = self._serpent
        best_candidate = self._best_candidate
        risk_tier = self._risk_tier

        # Resolve _human_is_watching through the orchestrator module
        # namespace so env overrides remain test-patchable.
        from backend.core.ouroboros.governance.orchestrator import (
            _human_is_watching,
        )

        # ---- VERBATIM transcription of orchestrator.py 5496-6098 ----
        if _serpent: _serpent.update_phase("GATE")
        # ---- Phase 5: GATE ----
        allowed, reason = orch._stack.can_write(
            {"files": list(ctx.target_files)}
        )
        logger.info(
            "[Orchestrator] GATE can_write decision for op=%s: "
            "allowed=%s reason=%s",
            ctx.op_id, allowed, reason,
        )
        if not allowed:
            logger.warning(
                "[Orchestrator] GATE BLOCKED: can_write=%s for op=%s files=%s",
                reason, ctx.op_id, list(ctx.target_files)[:3],
            )
            ctx = ctx.advance(
                OperationPhase.CANCELLED,
                terminal_reason_code=f"gate_blocked:{reason}",
            )
            await orch._record_ledger(
                ctx,
                OperationState.BLOCKED,
                {"reason": f"gate_blocked:{reason}"},
            )
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason=f"gate_blocked:{reason}",
                artifacts={"risk_tier": risk_tier, "best_candidate": best_candidate},
            )

        # ---- Security Review (LLM-as-a-Judge) before APPROVE gate ----
        try:
            from backend.core.ouroboros.governance.security_reviewer import SecurityReviewer, SecurityVerdict
            _sec_client = getattr(orch._stack, "prime_client", None)
            _sec_reviewer = SecurityReviewer(prime_client=_sec_client)
            if _sec_reviewer.is_enabled and best_candidate is not None:
                _sec_result = await _sec_reviewer.review(
                    candidate=best_candidate,
                    target_files=list(ctx.target_files),
                    description=ctx.description,
                )
                if _sec_result.verdict == SecurityVerdict.BLOCK:
                    logger.warning(
                        "[Orchestrator] Security review BLOCKED: %s [%s]",
                        _sec_result.summary, ctx.op_id,
                    )
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="security_review_blocked",
                    )
                    await orch._record_ledger(
                        ctx, OperationState.BLOCKED,
                        {"reason": "security_review_blocked", "summary": _sec_result.summary},
                    )
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="fail",
                        reason="security_review_blocked",
                        artifacts={"risk_tier": risk_tier, "best_candidate": best_candidate},
                    )
                elif _sec_result.verdict == SecurityVerdict.WARN:
                    logger.info(
                        "[Orchestrator] Security review WARN: %s [%s]",
                        _sec_result.summary, ctx.op_id,
                    )
                    try:
                        _warn_msg = type("_Msg", (), {
                            "payload": {
                                "proactive_alert": True,
                                "alert_title": "Security Review Warning",
                                "alert_body": _sec_result.summary or "Potential security concern detected.",
                                "alert_severity": "warning",
                                "alert_source": "SecurityReviewer",
                            },
                            "op_id": ctx.op_id,
                            "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                        })()
                        for _t in getattr(orch._stack.comm, "_transports", []):
                            try:
                                await _t.send(_warn_msg)
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception:
            logger.debug("[Orchestrator] SecurityReviewer not available", exc_info=True)

        # ---- Diff-Aware Similarity Gate (Sub-project C) ----
        if best_candidate is not None:
            try:
                from backend.core.ouroboros.governance.similarity_gate import check_similarity
                _src_content = ""
                if ctx.target_files:
                    _src_path = orch._config.project_root / ctx.target_files[0]
                    if _src_path.exists():
                        _src_content = _src_path.read_text(encoding="utf-8", errors="replace")
                _cand_content = ""
                if isinstance(best_candidate, dict):
                    _cand_content = best_candidate.get("full_content", "") or ""
                    if not _cand_content and isinstance(best_candidate.get("files"), list):
                        _target0 = ctx.target_files[0] if ctx.target_files else None
                        for _entry in best_candidate["files"]:
                            if not isinstance(_entry, dict):
                                continue
                            if _target0 is None or _entry.get("file_path") == _target0:
                                _cand_content = _entry.get("full_content", "") or ""
                                if _cand_content:
                                    break
                if _src_content and _cand_content:
                    _sim_reason = check_similarity(_cand_content, _src_content)
                    if _sim_reason is not None:
                        logger.info(
                            "[Orchestrator] GATE similarity escalation: %s [%s]",
                            _sim_reason, ctx.op_id,
                        )
                        if risk_tier is not RiskTier.APPROVAL_REQUIRED:
                            risk_tier = RiskTier.APPROVAL_REQUIRED
                        try:
                            await orch._stack.comm.emit_decision(
                                op_id=ctx.op_id,
                                outcome="escalated",
                                reason_code="similarity_escalation",
                                target_files=list(ctx.target_files),
                            )
                        except Exception:
                            pass
            except Exception:
                logger.debug("[Orchestrator] Similarity gate skipped", exc_info=True)

        # Autonomy tier gate
        _frozen_tier = getattr(ctx, "frozen_autonomy_tier", "governed")
        if _frozen_tier == "observe" and risk_tier is not RiskTier.APPROVAL_REQUIRED:
            risk_tier = RiskTier.APPROVAL_REQUIRED
            logger.info(
                "[Orchestrator] GATE: frozen_tier=observe → APPROVAL_REQUIRED; op=%s",
                ctx.op_id,
            )

        # ---- Risk floor override (REPL /risk command) ----
        _risk_floor_str = os.environ.get("JARVIS_RISK_CEILING", "")
        if _risk_floor_str:
            _floor_map = {
                "SAFE_AUTO": RiskTier.SAFE_AUTO,
                "NOTIFY_APPLY": RiskTier.NOTIFY_APPLY,
                "APPROVAL_REQUIRED": RiskTier.APPROVAL_REQUIRED,
            }
            _floor = _floor_map.get(_risk_floor_str.upper())
            if _floor is not None and risk_tier.value < _floor.value:
                logger.info(
                    "[Orchestrator] GATE: risk floor %s → escalating %s to %s; op=%s",
                    _risk_floor_str, risk_tier.name, _floor.name, ctx.op_id,
                )
                risk_tier = _floor

        # ---- SemanticGuardian: deterministic pre-APPLY pattern check ----
        _guardian_findings: list = []
        if best_candidate is not None:
            try:
                from backend.core.ouroboros.governance.semantic_guardian import (
                    SemanticGuardian,
                    recommend_tier_floor,
                )
                _guardian = SemanticGuardian()
                _pairs: list = []
                _candidate_files = best_candidate.get("files") if isinstance(
                    best_candidate.get("files"), list,
                ) else None
                if _candidate_files:
                    _iter = [
                        (entry.get("file_path", ""), entry.get("full_content", ""))
                        for entry in _candidate_files
                        if isinstance(entry, dict)
                    ]
                else:
                    _iter = [(
                        best_candidate.get("file_path", ""),
                        best_candidate.get("full_content", ""),
                    )]
                for _path, _new in _iter:
                    if not _path or not isinstance(_new, str):
                        continue
                    _old = ""
                    try:
                        _abs = (
                            orch._config.project_root / _path
                            if not Path(_path).is_absolute()
                            else Path(_path)
                        )
                        if _abs.is_file():
                            _old = _abs.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        _old = ""
                    _pairs.append((_path, _old, _new))

                _sg_t0 = time.monotonic()
                _guardian_findings = _guardian.inspect_batch(_pairs)
                _sg_duration_ms = int((time.monotonic() - _sg_t0) * 1000)

                _hard_count = sum(
                    1 for f in _guardian_findings if f.severity == "hard"
                )
                _soft_count = sum(
                    1 for f in _guardian_findings if f.severity == "soft"
                )
                _risk_before_name = risk_tier.name

                _floor_name = recommend_tier_floor(_guardian_findings)
                _upgrade: Optional[RiskTier] = None
                if _floor_name is not None:
                    _upgrade_map = {
                        "notify_apply": RiskTier.NOTIFY_APPLY,
                        "approval_required": RiskTier.APPROVAL_REQUIRED,
                    }
                    _upgrade = _upgrade_map.get(_floor_name)
                    if _upgrade is not None and risk_tier.value < _upgrade.value:
                        risk_tier = _upgrade
                    else:
                        _upgrade = None

                _pattern_names = (
                    ",".join(sorted({f.pattern for f in _guardian_findings}))
                    if _guardian_findings else "none"
                )
                logger.info(
                    "[SemanticGuard] op=%s findings=%d hard=%d soft=%d "
                    "patterns=[%s] risk_before=%s risk_after=%s "
                    "duration_ms=%d files_scanned=%d",
                    ctx.op_id,
                    len(_guardian_findings),
                    _hard_count, _soft_count,
                    _pattern_names,
                    _risk_before_name, risk_tier.name,
                    _sg_duration_ms,
                    len(_pairs),
                )
            except Exception:
                logger.debug(
                    "[Orchestrator] SemanticGuardian skipped",
                    exc_info=True,
                )

        # ---- REVIEW subagent (shadow observer) ----
        await orch._run_review_shadow(ctx, best_candidate)

        # ---- MutationGate: APPLY-phase execution boundary (cached) ----
        if best_candidate is not None:
            try:
                from backend.core.ouroboros.governance import mutation_gate as _mg
                if _mg.gate_enabled():
                    _mg_allowlist = _mg.load_allowlist()
                    _candidate_pairs = []
                    _candidate_files_mg = best_candidate.get("files") if isinstance(
                        best_candidate.get("files"), list,
                    ) else None
                    if _candidate_files_mg:
                        _candidate_pairs = [
                            entry.get("file_path", "")
                            for entry in _candidate_files_mg
                            if isinstance(entry, dict)
                        ]
                    else:
                        _single = best_candidate.get("file_path", "")
                        if _single:
                            _candidate_pairs = [_single]
                    _critical = [
                        Path(p) for p in _candidate_pairs
                        if _mg.is_path_critical(Path(p), allowlist=_mg_allowlist)
                    ]
                    if _critical:
                        _verdicts = []
                        for _sp in _critical:
                            _abs_sp = (
                                orch._config.project_root / _sp
                                if not _sp.is_absolute() else _sp
                            )
                            _tests = orch._discover_tests_for_gate(_sp)
                            _verdicts.append(
                                _mg.evaluate_file(_abs_sp, _tests)
                            )
                        if _verdicts:
                            _merged = _mg.merge_verdicts(_verdicts)
                            _risk_before_mg = risk_tier.name
                            _mg_mode = _mg.gate_mode()
                            _enforced = (_mg_mode == _mg.MODE_ENFORCE)
                            _applied_change = ""
                            if _enforced:
                                if _merged.decision == "block":
                                    risk_tier = RiskTier.BLOCKED
                                    _applied_change = (
                                        f"{_risk_before_mg}->BLOCKED"
                                    )
                                elif _merged.decision == "upgrade_to_approval":
                                    if risk_tier.value < RiskTier.APPROVAL_REQUIRED.value:
                                        risk_tier = RiskTier.APPROVAL_REQUIRED
                                        _applied_change = (
                                            f"{_risk_before_mg}->APPROVAL_REQUIRED"
                                        )
                            try:
                                _mg.append_ledger(
                                    op_id=ctx.op_id, verdict=_merged,
                                    mode=_mg_mode, enforced=_enforced,
                                    applied_tier_change=_applied_change,
                                )
                            except Exception:
                                logger.debug(
                                    "[MutationGate] ledger append skipped",
                                    exc_info=True,
                                )
                            logger.info(
                                "[MutationGate] op=%s mode=%s enforced=%s "
                                "decision=%s score=%.2f grade=%s "
                                "caught=%d/%d survivors=%d cache_hits=%d "
                                "cache_misses=%d duration=%.1fs "
                                "risk_before=%s risk_after=%s",
                                ctx.op_id, _mg_mode, _enforced,
                                _merged.decision, _merged.score,
                                _merged.grade, _merged.caught,
                                _merged.total_mutants, len(_merged.survivors),
                                _merged.cache_hits, _merged.cache_misses,
                                _merged.duration_s,
                                _risk_before_mg, risk_tier.name,
                            )
            except Exception:
                logger.debug(
                    "[Orchestrator] MutationGate skipped",
                    exc_info=True,
                )

        # ---- MIN_RISK_TIER floor (paranoia mode + quiet hours) ----
        try:
            from backend.core.ouroboros.governance.risk_tier_floor import (
                apply_floor_to_name,
                floor_reason,
            )
            _cur_name = risk_tier.name.lower()
            _effective, _applied = apply_floor_to_name(_cur_name)
            if _applied is not None:
                _floor_tier_map = {
                    "safe_auto": RiskTier.SAFE_AUTO,
                    "notify_apply": RiskTier.NOTIFY_APPLY,
                    "approval_required": RiskTier.APPROVAL_REQUIRED,
                    "blocked": RiskTier.BLOCKED,
                }
                _tgt = _floor_tier_map.get(_effective)
                if _tgt is not None and risk_tier.value < _tgt.value:
                    logger.info(
                        "[Orchestrator] GATE: MIN_RISK_TIER floor → %s→%s "
                        "op=%s reason=%s",
                        risk_tier.name, _tgt.name, ctx.op_id, floor_reason(),
                    )
                    risk_tier = _tgt
        except Exception:
            logger.debug(
                "[Orchestrator] MIN_RISK_TIER floor skipped",
                exc_info=True,
            )

        # ---- RR Pass B Slice 2b: ORDER_2_GOVERNANCE floor ----
        # Runs AFTER the MIN_RISK_TIER floor so paranoia/quiet-hours
        # can't accidentally lower an Order-2 op below itself.
        # Default-off behind dual flag (Slice 1 manifest + Slice 2
        # risk-class). When either flag is off, this block is a
        # no-op. See memory/project_reverse_russian_doll_pass_b.md
        # §4.2 + tests/governance/test_order2_classifier.py.
        try:
            from backend.core.ouroboros.governance.meta.order2_classifier import (
                apply_order2_floor,
            )
            _order2_tier = apply_order2_floor(
                risk_tier, list(ctx.target_files), repo="jarvis",
            )
            if _order2_tier is not risk_tier:
                logger.info(
                    "[Orchestrator] GATE: ORDER_2 floor → %s→%s "
                    "op=%s files=%d",
                    risk_tier.name, _order2_tier.name, ctx.op_id,
                    len(ctx.target_files),
                )
                risk_tier = _order2_tier
        except Exception:
            logger.debug(
                "[Orchestrator] ORDER_2 floor skipped",
                exc_info=True,
            )

        # ---- Phase 5a-green: SAFE_AUTO diff preview ----
        if risk_tier is RiskTier.SAFE_AUTO and _human_is_watching():
            _green_delay_s = float(
                os.environ.get("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "2")
            )
            if best_candidate is not None and _green_delay_s > 0:
                _diff_preview = (
                    best_candidate.get("unified_diff")
                    or best_candidate.get("full_content", "")
                )
                if _diff_preview:
                    try:
                        for _t in getattr(orch._stack.comm, "_transports", []):
                            try:
                                _preview_msg = type("_Msg", (), {
                                    "payload": {
                                        "phase": "safe_auto_diff_preview",
                                        "diff_preview": str(_diff_preview)[:4000],
                                        "delay_s": _green_delay_s,
                                        "target_files": list(ctx.target_files),
                                    },
                                    "op_id": ctx.op_id,
                                    "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                                })()
                                await _t.send(_preview_msg)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    logger.info(
                        "[Orchestrator] SAFE_AUTO diff preview shown (human watching), "
                        "waiting %.0fs for /reject; op=%s",
                        _green_delay_s, ctx.op_id,
                    )
                    await asyncio.sleep(_green_delay_s)
                    if orch._is_cancel_requested(ctx.op_id):
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code="user_rejected_safe_auto_preview",
                        )
                        await orch._record_ledger(
                            ctx, OperationState.FAILED,
                            {"reason": "user_rejected_safe_auto_preview"},
                        )
                        return PhaseResult(
                            next_ctx=ctx, next_phase=None, status="fail",
                            reason="user_rejected_safe_auto_preview",
                            artifacts={"risk_tier": risk_tier, "best_candidate": best_candidate},
                        )

        # ---- Phase 5b: NOTIFY_APPLY ----
        if risk_tier is RiskTier.NOTIFY_APPLY:
            _reason = getattr(ctx, "risk_reason_code", "notify_apply")
            logger.info(
                "[Orchestrator] GATE: NOTIFY_APPLY (Yellow) — auto-applying with notice; op=%s reason=%s",
                ctx.op_id, _reason,
            )
            try:
                await orch._stack.comm.emit_decision(
                    op_id=ctx.op_id,
                    outcome="notify_apply",
                    reason_code=_reason,
                    target_files=list(ctx.target_files),
                )
            except Exception:
                pass

            _notify_delay_s = float(os.environ.get("JARVIS_NOTIFY_APPLY_DELAY_S", "5"))
            if best_candidate is not None and _notify_delay_s > 0:
                _changes: list = []
                try:
                    from backend.core.ouroboros.battle_test.diff_preview import (
                        build_changes_from_candidate,
                    )
                    _changes = build_changes_from_candidate(
                        best_candidate, orch._config.project_root,
                    )
                except Exception:
                    logger.debug(
                        "[Orchestrator] build_changes_from_candidate failed; "
                        "using legacy plain preview",
                        exc_info=True,
                    )
                    _changes = []

                _serpent_flow = getattr(orch._stack, "serpent_flow", None)
                _cancel_check = lambda: orch._is_cancel_requested(ctx.op_id)
                _cancelled = False

                if _serpent_flow is not None and hasattr(_serpent_flow, "show_notify_apply_preview"):
                    logger.info(
                        "[Orchestrator] NOTIFY_APPLY rich preview — op=%s "
                        "files=%d delay=%.1fs",
                        ctx.op_id, len(_changes), _notify_delay_s,
                    )
                    try:
                        _cancelled = await _serpent_flow.show_notify_apply_preview(
                            op_id=ctx.op_id,
                            reason=_reason,
                            changes=_changes,
                            delay_s=_notify_delay_s,
                            cancel_check=_cancel_check,
                        )
                    except Exception:
                        logger.debug(
                            "[Orchestrator] rich NOTIFY_APPLY preview raised; "
                            "plain-sleep fallback",
                            exc_info=True,
                        )
                        await asyncio.sleep(_notify_delay_s)
                        _cancelled = _cancel_check()
                else:
                    _diff_preview = (
                        best_candidate.get("unified_diff")
                        or best_candidate.get("full_content", "")
                    )
                    if _diff_preview:
                        try:
                            for _t in getattr(orch._stack.comm, "_transports", []):
                                try:
                                    _preview_msg = type("_Msg", (), {
                                        "payload": {
                                            "phase": "notify_apply_diff",
                                            "diff_preview": str(_diff_preview)[:4000],
                                            "delay_s": _notify_delay_s,
                                            "target_files": list(ctx.target_files),
                                        },
                                        "op_id": ctx.op_id,
                                        "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                                    })()
                                    await _t.send(_preview_msg)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    logger.info(
                        "[Orchestrator] NOTIFY_APPLY diff preview shown, "
                        "waiting %.0fs for /reject",
                        _notify_delay_s,
                    )
                    await asyncio.sleep(_notify_delay_s)
                    _cancelled = _cancel_check()

                if _cancelled:
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="user_rejected_notify_apply",
                    )
                    await orch._record_ledger(
                        ctx, OperationState.FAILED,
                        {"reason": "user_rejected_notify_apply"},
                    )
                    return PhaseResult(
                        next_ctx=ctx, next_phase=None, status="fail",
                        reason="user_rejected_notify_apply",
                        artifacts={"risk_tier": risk_tier, "best_candidate": best_candidate},
                    )
        # ---- end verbatim transcription ----

        # Phase 1 Slice 1.3.c — capture the GATE risk-tier verdict.
        # Audit-only: risk_tier mutates through 7+ sites inside this
        # runner (SimilarityGate, frozen_tier, RISK_CEILING,
        # SemanticGuardian, MutationGate, MIN_RISK_TIER floor); we
        # capture the FINAL value at the success path. Fail paths
        # have their own structured reason codes already.
        # Closure-over-risk_tier means REPLAY does NOT alter the
        # mutation flow — gate logic always runs live; only the
        # terminal verdict is recorded/replayed/verified.
        try:
            from backend.core.ouroboros.governance.determinism.phase_capture import (
                capture_phase_decision,
            )

            async def _gate_digest_compute() -> Any:
                return {
                    "risk_tier": (
                        risk_tier.name
                        if hasattr(risk_tier, "name")
                        else str(risk_tier)
                    ),
                    "has_best_candidate": bool(best_candidate),
                }

            await capture_phase_decision(
                op_id=ctx.op_id,
                phase="GATE",
                kind="risk_tier_assignment",
                ctx=ctx,
                compute=_gate_digest_compute,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[Orchestrator] capture_phase_decision failed for "
                "GATE/risk_tier_assignment; gate verdict still applies",
                exc_info=True,
            )

        return PhaseResult(
            next_ctx=ctx,
            next_phase=OperationPhase.APPROVE,
            status="ok",
            reason="gated",
            artifacts={
                "risk_tier": risk_tier,
                "best_candidate": best_candidate,
            },
        )


__all__ = ["GATERunner"]
