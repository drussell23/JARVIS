"""VALIDATERunner — Slice 4a.1 of Wave 2 item (5).

Extracts orchestrator.py lines ~4693-5440 (the VALIDATE phase body
through its advance-to-GATE transition) into a :class:`PhaseRunner`
behind ``JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED`` (default ``false``).

**Zero behavior change per slice.** Verbatim transcription with
``self.`` → ``orch.`` substitutions.

## Why VALIDATE warrants its own sub-slice

* Nested retry FSM (``validate_retries_remaining`` loop + per-iteration
  candidate validation + non-retryable escalation)
* L2 self-repair dispatch with deadline reconciliation (Session V fix)
* InteractiveRepair "micro-fix" before VALIDATE_RETRY
* Source-drift check (ties to Manifesto §6 Iron Gate)
* Shadow harness AST comparison (advisory)
* Entropy measurement (Pillar 4 Synthetic Soul + quadrant routing)
* Read-only APPLY short-circuit (Manifesto §1 Boundary Principle)
* 17+ distinct ``_fsm_log`` transition points (§8 Observability)

## Seven terminal exit paths

1. ``validation_budget_exhausted`` — pre-iteration budget check
2. ``validation_budget_exhausted`` (failure_class=budget, non-retryable)
3. ``validation_infra_failure`` — failure_class=infra, non-retryable
4. L2 ``cancel`` / ``fatal`` directive
5. ``no_candidate_valid`` — all retries exhausted, no L2 recovery
6. ``source_drift_detected`` — candidate hash differs from disk hash
7. ``read_only_complete`` — ``ctx.is_read_only`` → full COMPLETE advance

## Success paths

* ``next_phase = GATE`` with ``validation=best_validation`` stamped
* ``next_phase = COMPLETE`` on read-only short-circuit (terminal success)

## Cross-phase artifact — ``best_candidate``

37 downstream references in GATE (SecurityReviewer, SimilarityGate,
SemanticGuardian, MutationGate, 5a green preview, 5b yellow notify,
APPROVE, APPLY, VERIFY). The runner threads it via
``PhaseResult.artifacts["best_candidate"]``. The orchestrator hook
rebinds to the ``best_candidate`` local before GATE inline code runs.

``best_validation`` rides on ``ctx.validation`` after advance so no
separate artifact threading needed.

## Dependencies injected via constructor

* ``orchestrator`` — reads ``_config.*``, ``_stack.comm``,
  ``_record_ledger``, ``_run_validation``, ``_l2_hook``,
  ``_pre_action_narrator`` (optional), ``_check_source_drift``
* ``serpent`` — pipeline serpent handle (optional)
* ``generation`` — the GenerationResult from Phase 3 (has ``.candidates``,
  ``.tool_execution_records``, ``.provider_name``, ``.model_id``)
* ``generate_retries_remaining`` — entropy calc input (line 5324 in inline)
* ``episodic_memory`` — optional ``EpisodicFailureMemory`` instance,
  used for failure recording inside retry loop + entropy critique aggregation

## Authority invariant

Runner imports match the inline block's surface (``ledger``,
``op_context``, ``risk_engine`` — already policy-free reads), plus
local imports of ``shadow_harness`` / ``entropy_calculator`` /
``interactive_repair`` / ``structured_critique`` / ``lsp_checker``
identical to the inline block. No execution-authority widening.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)

if TYPE_CHECKING:  # pragma: no cover
    from backend.core.ouroboros.governance.orchestrator import Orchestrator


logger = logging.getLogger("Ouroboros.Orchestrator")


class VALIDATERunner(PhaseRunner):
    """Verbatim transcription of orchestrator.py VALIDATE block (~4693-5440)."""

    phase = OperationPhase.VALIDATE

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
        generation: Any,
        generate_retries_remaining: int,
        episodic_memory: Optional[Any] = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent
        self._generation = generation
        self._generate_retries_remaining = generate_retries_remaining
        self._episodic_memory = episodic_memory

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator
        _serpent = self._serpent
        generation = self._generation
        generate_retries_remaining = self._generate_retries_remaining
        _episodic_memory = self._episodic_memory

        # ---- VERBATIM transcription of orchestrator.py 4693-5440 ----
        # Store generation result in context
        if _serpent: _serpent.update_phase("VALIDATE")
        ctx = ctx.advance(OperationPhase.VALIDATE, generation=generation)

        # Heartbeat: VALIDATE phase starting (Manifesto §7)
        try:
            await orch._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="validate", progress_pct=55.0,
            )
        except Exception:
            pass

        # ── PreActionNarrator: voice WHAT before VALIDATE ──
        if orch._pre_action_narrator is not None:
            try:
                await orch._pre_action_narrator.narrate_phase(
                    "VALIDATE", {"test_count": str(len(ctx.target_files))},
                )
            except Exception:
                pass

        # ---- Phase 4: VALIDATE ----

        # ── LSP Type Check (fast, incremental) ──
        _lsp_result = None
        try:
            from backend.core.ouroboros.governance.lsp_checker import LSPTypeChecker
            _lsp = LSPTypeChecker(project_root=orch._config.project_root)
            if _lsp.detect_checker_sync():
                _changed = [str(orch._config.project_root / f) for f in ctx.target_files]
                _lsp_result = await asyncio.get_event_loop().run_in_executor(
                    None, _lsp.check_incremental, _changed,
                )
                if not _lsp_result.passed:
                    logger.info(
                        "[Orchestrator] LSP found %d type errors in %s",
                        _lsp_result.error_count, list(ctx.target_files)[:3],
                    )
        except Exception:
            logger.debug("[Orchestrator] LSP check skipped", exc_info=True)

        # ── Exploration-first enforcement ──
        _EXPLORATION_TOOLS = frozenset({"read_file", "search_code", "get_callers"})
        _min_explore = int(os.environ.get("JARVIS_MIN_EXPLORATION_CALLS", "2"))
        _exploration_count = 0
        _exploration_first_ok = True
        if generation.tool_execution_records:
            for _rec in generation.tool_execution_records:
                _tname = getattr(_rec, "tool_name", "")
                if _tname in _EXPLORATION_TOOLS:
                    _exploration_count += 1
            if _exploration_count < _min_explore:
                _exploration_first_ok = False
                logger.warning(
                    "[Orchestrator] Exploration-first violation: %d/%d exploration calls "
                    "(expected >= %d) for op %s — candidate may lack codebase context",
                    _exploration_count, len(generation.tool_execution_records),
                    _min_explore, ctx.op_id[:12],
                )

        best_candidate: Optional[Dict[str, Any]] = None
        best_validation: Optional[ValidationResult] = None
        validate_retries_remaining = orch._config.max_validate_retries

        # ── [ValidateRetryFSM] instrumentation (§8 Observability) ──
        def _fsm_log(state: str, extra: str = "") -> None:
            _fc = (
                getattr(best_validation, "failure_class", None)
                if best_validation is not None else None
            )
            logger.info(
                "[ValidateRetryFSM] %s op=%s ctx_id=%x phase=%s "
                "retries_remaining=%d best_fc=%r n_cands=%d%s",
                state,
                ctx.op_id[:16],
                id(ctx),
                ctx.phase.name,
                validate_retries_remaining,
                _fc,
                len(generation.candidates),
                f" {extra}" if extra else "",
            )

        for _iter_idx in range(1 + orch._config.max_validate_retries):
            _fsm_log("iter_start", f"iter={_iter_idx}")
            if ctx.pipeline_deadline is not None:
                remaining_s = (
                    ctx.pipeline_deadline - datetime.now(tz=timezone.utc)
                ).total_seconds()
            else:
                remaining_s = orch._config.validation_timeout_s

            if remaining_s <= 0.0:
                _fsm_log("budget_exhausted_pre", f"remaining_s={remaining_s:.1f}")
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="validation_budget_exhausted",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "validation_budget_exhausted"},
                )
                _fsm_log("budget_exhausted_return")
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="validation_budget_exhausted",
                    artifacts={"best_candidate": None, "best_validation": None},
                )

            # Try all candidates in parallel; pick first that passes
            async def _validate_one(cand: Dict[str, Any]) -> Tuple[Dict[str, Any], "ValidationResult", float]:
                _t0 = time.monotonic()
                _val = await orch._run_validation(ctx, cand, remaining_s)
                return (cand, _val, time.monotonic() - _t0)

            _validation_tasks = [_validate_one(c) for c in generation.candidates]
            _validation_results = await asyncio.gather(*_validation_tasks, return_exceptions=True)

            _early_return_ctx: Optional[OperationContext] = None
            for _vr in _validation_results:
                if isinstance(_vr, BaseException):
                    logger.debug("[Orchestrator] Candidate validation raised: %s", _vr)
                    continue
                candidate, validation, _validate_duration_s = _vr

                await orch._record_ledger(ctx, OperationState.GATING, {
                    "event": "candidate_validated",
                    "candidate_id": candidate.get("candidate_id", "unknown"),
                    "candidate_hash": candidate.get("candidate_hash", ""),
                    "validation_outcome": "pass" if validation.passed else "fail",
                    "failure_class": validation.failure_class,
                    "duration_s": round(_validate_duration_s, 3),
                    "provider": generation.provider_name,
                    "model": getattr(generation, "model_id", ""),
                    "exploration_first_ok": _exploration_first_ok,
                    "exploration_count": _exploration_count,
                })

                # Heartbeat: validation result for TUI (Manifesto §7)
                try:
                    _val_msg = type("_Msg", (), {
                        "payload": {
                            "phase": "validate",
                            "test_passed": validation.passed,
                            "test_count": getattr(validation, "test_count", 0),
                            "test_failures": getattr(validation, "failure_count", 0),
                            "failure_class": validation.failure_class or "",
                            "validation_output": str(getattr(validation, "output_preview", ""))[:300],
                        },
                        "op_id": ctx.op_id,
                        "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                    })()
                    for _t in getattr(orch._stack.comm, "_transports", []):
                        try:
                            await _t.send(_val_msg)
                        except Exception:
                            pass
                except Exception:
                    pass

                if validation.failure_class == "duplication":
                    try:
                        await orch._stack.comm.emit_decision(
                            op_id=ctx.op_id,
                            outcome="blocked",
                            reason_code="duplication",
                            target_files=list(ctx.target_files),
                        )
                    except Exception:
                        pass

                if validation.passed and best_candidate is None:
                    best_candidate = candidate
                    best_validation = validation
                    continue

                # Infra failure: non-retryable
                if validation.failure_class == "infra" and _early_return_ctx is None:
                    ctx = ctx.advance(
                        OperationPhase.POSTMORTEM,
                        validation=validation,
                        terminal_reason_code="validation_infra_failure",
                    )
                    await orch._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {
                            "reason": "validation_infra_failure",
                            "failure_class": "infra",
                            "adapter_names_run": list(validation.adapter_names_run),
                            "validation_duration_s": validation.validation_duration_s,
                            "short_summary": validation.short_summary,
                        },
                    )
                    _early_return_ctx = ctx
                    _fsm_log("infra_early_return_set")

                if validation.failure_class == "budget" and _early_return_ctx is None:
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        validation=validation,
                        terminal_reason_code="validation_budget_exhausted",
                    )
                    await orch._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {"reason": "validation_budget_exhausted"},
                    )
                    _early_return_ctx = ctx
                    _fsm_log("budget_early_return_set")

                if not validation.passed:
                    best_validation = validation

                    if _episodic_memory is not None and validation.failure_class in ("test", "build"):
                        try:
                            from backend.core.ouroboros.governance.structured_critique import CritiqueBuilder
                            critique_report = CritiqueBuilder.from_validation_output(
                                file_path=candidate.get("file_path", "unknown"),
                                failure_class=validation.failure_class or "test",
                                error_text=validation.error or "",
                                test_output=validation.short_summary or "",
                            )
                            _episodic_memory.record(
                                file_path=candidate.get("file_path", "unknown"),
                                attempt=orch._config.max_validate_retries - validate_retries_remaining + 1,
                                failure_class=validation.failure_class or "test",
                                error_summary=critique_report.summary,
                                specific_errors=[c.what_failed for c in critique_report.critiques],
                                line_numbers=[c.line_number for c in critique_report.critiques if c.line_number],
                            )
                            logger.info(
                                "[Orchestrator] Episodic memory recorded: %s — %s [%s]",
                                candidate.get("file_path", "?"),
                                critique_report.summary,
                                ctx.op_id,
                            )
                        except Exception:
                            logger.debug("[Orchestrator] Episodic/critique recording failed", exc_info=True)

            if _early_return_ctx is not None and best_candidate is None:
                _fsm_log("early_return")
                _reason = _early_return_ctx.terminal_reason_code or "validation_failed"
                return PhaseResult(
                    next_ctx=_early_return_ctx, next_phase=None, status="fail",
                    reason=_reason,
                    artifacts={"best_candidate": None, "best_validation": best_validation},
                )

            if best_candidate is not None:
                _fsm_log("candidate_passed_break")
                break

            # All candidates failed this attempt
            if best_validation is not None and getattr(best_validation, "test_count", -1) == 0:
                logger.info(
                    "[Orchestrator] Skipping retries — no tests discovered for op=%s",
                    ctx.op_id,
                )
                _fsm_log("no_tests_short_circuit")
                validate_retries_remaining = -1

            validate_retries_remaining -= 1
            if validate_retries_remaining < 0:
                # ── L2 self-repair dispatch ──
                if orch._config.repair_engine is not None and best_validation is not None:
                    # ── L2 deadline reconciliation (Session V fix) ──
                    _l2_timebox_s = float(
                        os.environ.get("JARVIS_L2_TIMEBOX_S", "120.0")
                    )
                    _now_dt = datetime.now(timezone.utc)
                    _l2_fresh_deadline = _now_dt + timedelta(
                        seconds=_l2_timebox_s
                    )
                    _orig_pl_deadline = ctx.pipeline_deadline
                    _orig_remaining_s = (
                        (_orig_pl_deadline - _now_dt).total_seconds()
                        if _orig_pl_deadline is not None else 0.0
                    )
                    if (
                        _orig_pl_deadline is None
                        or _orig_pl_deadline < _l2_fresh_deadline
                    ):
                        _l2_deadline = _l2_fresh_deadline
                        _winning_cap = "l2_timebox_fresh"
                        ctx = ctx.with_pipeline_deadline(_l2_fresh_deadline)
                    else:
                        _l2_deadline = _orig_pl_deadline
                        _winning_cap = "pipeline_deadline_inherited"
                    logger.info(
                        "[Orchestrator] L2 deadline reconciliation: "
                        "pipeline_remaining=%.1fs l2_timebox_env=%.1fs "
                        "effective=%.1fs winning_cap=%s op=%s",
                        _orig_remaining_s,
                        _l2_timebox_s,
                        (_l2_deadline - _now_dt).total_seconds(),
                        _winning_cap,
                        ctx.op_id[:16],
                    )
                    _fsm_log(
                        "l2_dispatch_pre",
                        f"effective_s={(_l2_deadline - _now_dt).total_seconds():.0f} "
                        f"cap={_winning_cap} l2_timebox_env={_l2_timebox_s:.0f}",
                    )
                    directive = await orch._l2_hook(
                        ctx, best_validation, _l2_deadline,
                    )
                    _fsm_log("l2_dispatch_post", f"directive={directive[0]!r}")
                    if directive[0] == "break":
                        best_candidate, best_validation = directive[1], directive[2]
                        logger.info(
                            "[Orchestrator] L2 broke VALIDATE_RETRY loop for op=%s — "
                            "proceeding to source-drift / shadow / entropy / GATE "
                            "(candidate_id=%s, file=%s, source_hash=%s)",
                            ctx.op_id,
                            best_candidate.get("candidate_id", "?"),
                            best_candidate.get("file_path", "?"),
                            (best_candidate.get("source_hash") or "")[:12],
                        )
                        _fsm_log("l2_converged_break")
                        break
                    elif directive[0] in ("cancel", "fatal"):
                        _fsm_log("l2_escape_return", f"directive={directive[0]!r}")
                        # directive[1] is the advanced ctx (CANCELLED/POSTMORTEM)
                        _reason = directive[1].terminal_reason_code or directive[0]
                        return PhaseResult(
                            next_ctx=directive[1], next_phase=None, status="fail",
                            reason=_reason,
                            artifacts={"best_candidate": None, "best_validation": best_validation},
                        )
                else:
                    _fsm_log(
                        "l2_skipped",
                        f"repair_engine={orch._config.repair_engine is not None} "
                        f"best_validation={best_validation is not None}",
                    )

                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="no_candidate_valid",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason_code": "no_candidate_valid",
                        "candidates_tried": [
                            c.get("candidate_id", "?") for c in generation.candidates
                        ],
                        "failure_class": best_validation.failure_class if best_validation else "test",
                        "adapter_names_run": list(best_validation.adapter_names_run) if best_validation else [],
                        "validation_duration_s": best_validation.validation_duration_s if best_validation else 0.0,
                        "short_summary": best_validation.short_summary if best_validation else "",
                    },
                )
                _fsm_log("no_candidate_valid_return")
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="no_candidate_valid",
                    artifacts={"best_candidate": None, "best_validation": best_validation},
                )

            # ── Micro-Fix: InteractiveRepair before VALIDATE_RETRY ──
            _fsm_log("micro_fix_pre")
            if orch._pre_action_narrator is not None:
                try:
                    await orch._pre_action_narrator.narrate_phase(
                        "MICRO_FIX", {"target": list(ctx.target_files)[:1]},
                    )
                except Exception:
                    pass
            try:
                from backend.core.ouroboros.governance.interactive_repair import InteractiveRepairLoop
                _repair = InteractiveRepairLoop(
                    provider=orch._generator,
                    project_root=orch._config.project_root,
                )
                _repair_target = list(ctx.target_files)[0] if ctx.target_files else None
                if _repair_target:
                    _repair_abs = orch._config.project_root / _repair_target
                    if _repair_abs.is_file():
                        _repair_content = _repair_abs.read_text(errors="replace")
                        _test_argv = ["python3", "-m", "pytest", "-x", "-q"]
                        _repair_result = await asyncio.wait_for(
                            _repair.repair(
                                file_path=str(_repair_target),
                                file_content=_repair_content,
                                test_argv=_test_argv,
                                op_id=ctx.op_id,
                            ),
                            timeout=90.0,
                        )
                        _fsm_log(
                            "micro_fix_returned",
                            f"fixed={_repair_result.fixed} "
                            f"iterations={_repair_result.iterations_used}",
                        )
                        if _repair_result.fixed:
                            logger.info(
                                "[Orchestrator] Micro-fix succeeded in %d iterations for op=%s",
                                _repair_result.iterations_used, ctx.op_id,
                            )
                            ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)
                            _fsm_log("micro_fix_succeeded_break")
                            break
                    else:
                        _fsm_log(
                            "micro_fix_skipped_new_file",
                            f"target={_repair_target!r}",
                        )
                else:
                    _fsm_log("micro_fix_skipped_no_target")
            except asyncio.CancelledError:
                _fsm_log("micro_fix_cancelled")
                raise
            except Exception as _repair_exc:
                logger.warning(
                    "[Orchestrator] Micro-fix failed (exc_class=%s): %s",
                    type(_repair_exc).__name__,
                    _repair_exc,
                    exc_info=True,
                )
                _fsm_log(
                    "micro_fix_exception_swallowed",
                    f"exc_class={type(_repair_exc).__name__}",
                )

            _vr_kwargs = {}
            if _episodic_memory is not None and _episodic_memory.has_failures():
                _vr_context = _episodic_memory.format_for_prompt()
                if _vr_context:
                    _existing_vr = getattr(ctx, "strategic_memory_prompt", "") or ""
                    _vr_kwargs["strategic_memory_prompt"] = (
                        f"{_existing_vr}\n\n{_vr_context}" if _existing_vr else _vr_context
                    )
            _fsm_log("retry_advance_pre")
            _pre_ctx_id = id(ctx)
            ctx = ctx.advance(OperationPhase.VALIDATE_RETRY, **_vr_kwargs)
            _fsm_log(
                "retry_advance_post",
                f"old_ctx_id={_pre_ctx_id:x} new_ctx_id={id(ctx):x}",
            )

        _fsm_log(
            "loop_exit_normal",
            f"best_candidate_present={best_candidate is not None}",
        )
        assert best_candidate is not None
        assert best_validation is not None

        # Source-drift check
        drift_hash = orch._check_source_drift(best_candidate, orch._config.project_root)
        if drift_hash is not None:
            logger.info(
                "[Orchestrator] Source drift detected for op=%s file=%s "
                "(expected=%s, actual=%s) — advancing to CANCELLED",
                ctx.op_id,
                best_candidate.get("file_path", "?"),
                (best_candidate.get("source_hash") or "")[:12],
                (drift_hash or "")[:12],
            )
            ctx = ctx.advance(
                OperationPhase.CANCELLED,
                terminal_reason_code="source_drift_detected",
            )
            await orch._record_ledger(ctx, OperationState.FAILED, {
                "reason_code": "source_drift_detected",
                "file_path": best_candidate.get("file_path"),
                "expected_source_hash": best_candidate.get("source_hash"),
                "actual_source_hash": drift_hash,
            })
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason="source_drift_detected",
                artifacts={"best_candidate": None, "best_validation": best_validation},
            )
        logger.info(
            "[Orchestrator] Source-drift check passed for op=%s — "
            "proceeding to shadow harness + entropy + GATE",
            ctx.op_id,
        )

        await orch._record_ledger(ctx, OperationState.GATING, {
            "event": "validation_complete",
            "winning_candidate_id": best_candidate.get("candidate_id"),
            "winning_candidate_hash": best_candidate.get("candidate_hash"),
            "winning_file_path": best_candidate.get("file_path"),
            "source_hash": best_candidate.get("source_hash"),
            "source_path": best_candidate.get("source_path"),
            "provider": generation.provider_name,
            "model": getattr(generation, "model_id", ""),
            "total_candidates_tried": len(generation.candidates),
        })

        # ── Shadow harness check (soft advisory) ──
        if orch._config.shadow_harness is not None and best_candidate is not None:
            import time as _sh_time
            from backend.core.ouroboros.governance.shadow_harness import (
                OutputComparator,
                SideEffectFirewall,
                CompareMode,
            )
            from backend.core.ouroboros.governance.op_context import ShadowResult
            _sh_start = _sh_time.monotonic()
            _violations: list = []
            _confidence = 0.0
            try:
                _content = (
                    best_candidate.get("full_content")
                    or best_candidate.get("unified_diff")
                    or ""
                )
                with SideEffectFirewall():
                    _confidence = OutputComparator().compare(
                        _content, _content, CompareMode.AST
                    )
            except Exception as _sh_exc:
                _violations.append(str(_sh_exc))
                _confidence = 0.0
            _sh_dur = _sh_time.monotonic() - _sh_start
            orch._config.shadow_harness.record_run(_confidence)
            _shadow_result = ShadowResult(
                confidence=_confidence,
                comparison_mode="ast",
                violations=tuple(_violations),
                shadow_duration_s=_sh_dur,
                production_match=(_confidence >= 0.7),
                disqualified=orch._config.shadow_harness.is_disqualified,
            )
            ctx = ctx.with_shadow_result(_shadow_result)
            if orch._config.shadow_harness.is_disqualified:
                logger.warning(
                    "[Orchestrator] ShadowHarness disqualified for op=%s "
                    "(confidence=%.2f, violations=%d) — proceeding to GATE with advisory",
                    ctx.op_id,
                    _confidence,
                    len(_violations),
                )

        # ── Entropy measurement (Pillar 4) ──
        try:
            from backend.core.ouroboros.governance.entropy_calculator import (
                compute_acute_signal,
                compute_chronic_signal,
                compute_systemic_entropy,
                build_cognitive_inefficiency_event,
                extract_domain_key,
                EntropyQuadrant,
            )

            _shadow_conf = 1.0
            if ctx.shadow is not None:
                _shadow_conf = getattr(ctx.shadow, "confidence", 1.0)

            _critique_errors = 0
            _critique_warnings = 0
            _critique_infos = 0
            if _episodic_memory is not None:
                try:
                    for ep in getattr(_episodic_memory, "_episodes", []):
                        _critique_errors += getattr(ep, "error_count", 0)
                        _critique_warnings += getattr(ep, "warning_count", 0)
                        _critique_infos += getattr(ep, "info_count", 0)
                except Exception:
                    pass

            _acute = compute_acute_signal(
                validation_passed=best_validation.passed,
                critique_errors=_critique_errors,
                critique_warnings=_critique_warnings,
                critique_infos=_critique_infos,
                shadow_confidence=_shadow_conf,
                retries_used=(orch._config.max_generate_retries - generate_retries_remaining),
                max_retries=orch._config.max_generate_retries,
            )

            _domain_key = extract_domain_key(ctx.target_files, ctx.description)
            _chronic_outcomes: list = []
            if hasattr(orch._stack, "learning_bridge") and orch._stack.learning_bridge is not None:
                try:
                    _history = await orch._stack.learning_bridge.get_domain_history(
                        _domain_key
                    )
                    _chronic_outcomes = _history if _history else []
                except Exception:
                    pass

            _chronic = compute_chronic_signal(_domain_key, _chronic_outcomes)
            _composite = compute_systemic_entropy(_acute, _chronic)

            logger.info(
                "[Orchestrator] Entropy: acute=%.3f chronic=%.3f systemic=%.3f "
                "quadrant=%s trigger=%s domain=%s (op=%s)",
                _acute.normalized_score, _chronic.normalized_score,
                _composite.systemic_score, _composite.quadrant.value,
                _composite.should_trigger, _domain_key, ctx.op_id,
            )

            await orch._record_ledger(ctx, OperationState.GATING, {
                "event": "entropy_measured",
                "acute_score": round(_acute.normalized_score, 4),
                "chronic_score": round(_chronic.normalized_score, 4),
                "systemic_score": round(_composite.systemic_score, 4),
                "quadrant": _composite.quadrant.value,
                "domain_key": _domain_key,
                "should_trigger": _composite.should_trigger,
            })

            if _composite.quadrant == EntropyQuadrant.IMMEDIATE_TRIGGER:
                _event = build_cognitive_inefficiency_event(ctx.op_id, _composite)
                try:
                    from backend.neural_mesh.synthesis.gap_signal_bus import (
                        GapSignalBus, CapabilityGapEvent,
                    )
                    _bus = GapSignalBus.get_instance()
                    if _bus is not None:
                        _gap_event = CapabilityGapEvent(
                            goal=ctx.description or "capability gap detected via entropy",
                            task_type=_domain_key,
                            target_app="ouroboros",
                            source="entropy_calculator",
                            resolution_mode="synthesis",
                        )
                        _bus.emit(_gap_event)
                        logger.warning(
                            "[Orchestrator] IMMEDIATE_TRIGGER: CognitiveInefficiencyEvent "
                            "emitted for domain=%s systemic=%.3f (op=%s)",
                            _domain_key, _composite.systemic_score, ctx.op_id,
                        )
                except Exception:
                    logger.debug("[Orchestrator] GapSignalBus emit failed", exc_info=True)

            elif _composite.quadrant == EntropyQuadrant.FALSE_CONFIDENCE:
                logger.warning(
                    "[Orchestrator] FALSE_CONFIDENCE: domain=%s has high chronic "
                    "failure rate (%.3f) despite passing validation. "
                    "Recommend sandbox re-verification. (op=%s)",
                    _domain_key, _chronic.failure_rate, ctx.op_id,
                )

        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Entropy computation failed", exc_info=True)

        # Read-only APPLY short-circuit (Manifesto §1 Boundary Principle)
        if ctx.is_read_only:
            logger.info(
                "[Orchestrator] Read-only APPLY short-circuit op=%s — "
                "skipping GATE/APPLY/VERIFY (no-mutation contract). "
                "Findings are delivered via POSTMORTEM + ledger.",
                ctx.op_id,
            )
            try:
                await orch._stack.comm.emit_decision(
                    op_id=ctx.op_id,
                    outcome="read_only_complete",
                    reason_code="read_only_complete",
                    diff_summary="",
                )
            except Exception:
                pass
            ctx = ctx.advance(
                OperationPhase.COMPLETE,
                terminal_reason_code="read_only_complete",
                validation=best_validation,
            )
            if _serpent:
                await _serpent.stop(success=True)
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="ok",
                reason="read_only_complete",
                artifacts={"best_candidate": best_candidate, "best_validation": best_validation},
            )

        # Advance to GATE
        ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)
        logger.info(
            "[Orchestrator] Entered GATE phase for op=%s — invoking "
            "can_write policy check on target_files=%s",
            ctx.op_id,
            list(ctx.target_files)[:3],
        )

        # Heartbeat: GATE phase (Manifesto §7)
        try:
            await orch._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="gate", progress_pct=75.0,
            )
        except Exception:
            pass
        # ---- end verbatim transcription ----

        return PhaseResult(
            next_ctx=ctx,
            next_phase=OperationPhase.GATE,
            status="ok",
            reason="validated",
            artifacts={
                "best_candidate": best_candidate,
                "best_validation": best_validation,
            },
        )


__all__ = ["VALIDATERunner"]
