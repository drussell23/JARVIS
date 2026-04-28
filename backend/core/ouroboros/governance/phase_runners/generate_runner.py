"""GENERATERunner — Slice 5a of Wave 2 item (5). The beast.

Extracts orchestrator.py lines ~3138-4748 (1611 lines: GENERATE phase
prelude + retry loop + CandidateGenerator dispatch + per-op cost cap +
forward-progress detector + productivity detector + Iron Gate suite
(exploration ledger, ASCII strict, config format, dependency-file
integrity, multi-file coverage) + retry feedback composition + L2
escape terminals) into a single :class:`PhaseRunner` behind
``JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED`` (default ``false``).

**Zero behavior change per slice.** Verbatim transcription with
``self.`` → ``orch.`` substitutions. Scripted extraction via
``build_generate_runner.py`` keeps parity exact.

## Sub-slice delivery order

* **5a** (this commit): spine extraction + parity tests covering
  prelude, retry loop skeleton, CandidateGenerator dispatch, cost
  cap, happy path, bounded retry, L2 escape terminals.
* **5b** (next commit): Iron Gate suite parity depth —
  exploration ledger category-aware diversity scoring (§6 heart),
  ASCII strict gate, dependency-file integrity (hallucinated-rename
  catcher), multi-file coverage gate, retry-feedback composition.

Same runner module. Same flag. Split is *parity test depth*, not code.

## ~8 terminal exit paths (carry over from inline)

1. ``op_cost_cap_exceeded`` — per-op cost cap tripped pre-attempt
2. ``no_forward_progress`` — forward-progress detector EC8 trip
3. ``stalled_productivity`` — productivity detector EC9 trip
4. L2 ``cancel`` / ``fatal`` — from L2 escape on terminal retry
5. ``ascii_gate_violation`` — ASCII strict gate (§6 Iron Gate)
6. ``exploration_floor_not_met`` — exploration ledger insufficient
7. ``dependency_file_integrity_failed`` — hallucinated rename blocked
8. ``config_format_invalid`` — config format gate

## Success path

``next_phase = VALIDATE`` with ``generation`` stamped on result artifacts.
The orchestrator hook reads ``artifacts["generation"]`` and
``artifacts["episodic_memory"]`` for VALIDATE.

## Cross-phase artifacts

* ``generation`` — the GenerationResult (consumed by VALIDATERunner)
* ``episodic_memory`` — EpisodicFailureMemory (consumed by VALIDATERunner)

Both threaded via ``PhaseResult.artifacts``. Orchestrator hook rebinds
``generation`` + ``_episodic_memory`` locals before VALIDATE inline /
runner reads them.

## Dependencies injected via constructor

* ``orchestrator`` — reads many helpers (see verbatim block for full list)
* ``serpent`` — pipeline serpent handle (optional)
* ``consciousness_bridge`` — from CLASSIFY artifacts (fragile-file injection)

## Authority invariant

Runner imports: ledger, op_context, phase_runner, plus function-local
imports matching inline block. NO ``iron_gate`` module import — the
Iron Gate suite is inlined here same as orchestrator.py. No new
authority-widening; grep-pinned.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.ascii_strict_gate import (
    AsciiStrictGate,
    build_retry_feedback as _ascii_gate_retry_feedback,
)
from backend.core.ouroboros.governance.forward_progress import (
    candidate_content_hash,
)
from backend.core.ouroboros.governance.productivity_detector import (
    productivity_content_hash,
)
from dataclasses import asdict as _dc_asdict

from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)

if TYPE_CHECKING:  # pragma: no cover
    from backend.core.ouroboros.governance.orchestrator import Orchestrator

# Match orchestrator's outer-gate grace constant (read at import time is
# fine — env won't change mid-op; inline does the same).
_OUTER_GATE_GRACE_S = float(os.environ.get("JARVIS_OUTER_GATE_GRACE_S", "15"))
_TRUTHY = frozenset({"1", "true", "yes", "on"})


logger = logging.getLogger("Ouroboros.Orchestrator")


class GENERATERunner(PhaseRunner):
    """Verbatim transcription of orchestrator.py GENERATE block (~3138-4748)."""

    phase = OperationPhase.GENERATE

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
        consciousness_bridge: Optional[Any] = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent
        self._consciousness_bridge = consciousness_bridge

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator
        _serpent = self._serpent
        _consciousness_bridge = self._consciousness_bridge

        # Resolve orchestrator module-level helpers/classes referenced by
        # the verbatim inline block. Late import to avoid circular deps.
        from backend.core.ouroboros.governance.orchestrator import (
            _PreloadedExplorationRecord,
        )

        # W2(4) Slice 2 — bind the per-op CuriosityBudget to the ambient
        # ContextVar so tool_executor Rule 14 can consult it during the
        # Venom tool loop. Master flag default-off → curiosity_enabled()
        # returns False → CuriosityBudget.try_charge() always denies →
        # tool_executor's curiosity widening short-circuits to the
        # legacy SAFE_AUTO reject. Byte-for-byte pre-W2(4) when master
        # is off. Best-effort: any exception here must not block GENERATE.
        try:
            from backend.core.ouroboros.governance.curiosity_engine import (
                CuriosityBudget as _CuriosityBudget,
                curiosity_budget_var as _curiosity_budget_var,
                curiosity_enabled as _curiosity_enabled,
            )
            if _curiosity_enabled():
                # Resolve current posture via Wave 1 #1 DirectionInferrer
                # observer. If unavailable (test orchestrator without a
                # PostureStore), default to "UNKNOWN" so the posture
                # allowlist gate denies cleanly.
                _posture_str = "UNKNOWN"
                try:
                    from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
                        get_default_store as _get_default_posture_store,
                    )
                    _store = _get_default_posture_store()
                    _reading = (
                        _store.current_reading() if _store is not None else None
                    )
                    if _reading is not None:
                        _posture_str = str(
                            getattr(_reading, "posture", _reading)
                        )
                        # Some posture types are Enum.NAME — strip the prefix
                        if "." in _posture_str:
                            _posture_str = _posture_str.split(".", 1)[1]
                except Exception:  # noqa: BLE001
                    pass
                # Resolve session_dir for the JSONL ledger (best-effort).
                _session_dir = None
                try:
                    _gls = getattr(orch._stack, "governed_loop_service", None)
                    if _gls is not None:
                        _sd = getattr(_gls, "_session_dir", None)
                        if _sd is not None:
                            from pathlib import Path as _Path
                            _session_dir = (
                                _sd if isinstance(_sd, _Path) else _Path(_sd)
                            )
                except Exception:  # noqa: BLE001
                    pass
                _curiosity_budget_var.set(_CuriosityBudget(
                    op_id=ctx.op_id,
                    posture_at_arm=_posture_str,
                    session_dir=_session_dir,
                ))
        except Exception:  # noqa: BLE001 — best-effort, never blocks GENERATE
            pass

        # ---- VERBATIM transcription of orchestrator.py 3138-4748 ----
        if _serpent: _serpent.update_phase("GENERATE")
        # ---- Phase 3: GENERATE (with retry + episodic failure memory) ----
        generation: Optional[GenerationResult] = None
        generate_retries_remaining = orch._config.max_generate_retries

        # Episodic failure memory — per-operation, injected into retries
        _episodic_memory = None
        try:
            from backend.core.ouroboros.governance.episodic_memory import EpisodicFailureMemory
            _episodic_memory = EpisodicFailureMemory(ctx.op_id)
        except ImportError:
            pass

        # ── Inject cumulative session lessons into context ──
        # Filter out infrastructure failures (timeouts, provider outages) to
        # avoid poisoning the model with environmentally-caused failures.
        if orch._session_lessons:
            _code_lessons = [
                text for (ltype, text) in orch._session_lessons
                if ltype == "code"
            ][-orch._session_lessons_max:]
            if _code_lessons:
                _lessons_text = "\n".join(f"- {lesson}" for lesson in _code_lessons)
                ctx = dataclasses.replace(
                    ctx,
                    session_lessons=_lessons_text,
                )

        # ── Consciousness: inject fragile-file memory into first generation ──
        # Manifesto §4: "The organism possesses episodic memory and metacognition"
        if _consciousness_bridge is not None:
            try:
                _fragile_ctx = _consciousness_bridge.get_fragile_file_context(
                    ctx.target_files
                )
                if _fragile_ctx:
                    _existing_mem = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = dataclasses.replace(
                        ctx,
                        strategic_memory_prompt=(
                            f"{_existing_mem}\n\n{_fragile_ctx}" if _existing_mem else _fragile_ctx
                        ),
                    )
                    logger.info(
                        "[Orchestrator] Consciousness memory injected into GENERATE context "
                        "(%d chars) [%s]",
                        len(_fragile_ctx), ctx.op_id,
                    )
            except Exception:
                logger.debug("[Orchestrator] Consciousness injection failed", exc_info=True)

        # ── Stale-exploration guard: snapshot file hashes at GENERATE time ──
        _gen_hashes: list = []
        for _tf in ctx.target_files:
            _tf_path = orch._config.project_root / _tf
            try:
                _tf_bytes = _tf_path.read_bytes()
                _gen_hashes.append((_tf, hashlib.sha256(_tf_bytes).hexdigest()))
            except (OSError, IOError):
                _gen_hashes.append((_tf, ""))  # new file — no hash
        if _gen_hashes:
            ctx = dataclasses.replace(ctx, generate_file_hashes=tuple(_gen_hashes))

        # Cumulative exploration credit across the GENERATE retry loop. When a
        # prior attempt satisfied the floor but failed downstream gates (ASCII,
        # dependency integrity, etc.), the retry feedback embeds the rejected
        # file content — re-reading via read_file is wasteful, so the credit
        # carries forward instead of forcing the model to spend tool rounds on
        # the same file twice (bt-2026-04-11-204228 / op-019d7e4c).
        _op_explore_credit = 0
        # Ledger-path counterpart to _op_explore_credit (#103).
        # When JARVIS_EXPLORATION_LEDGER_ENABLED is true the Iron Gate consults
        # ExplorationLedger.from_records(_op_explore_records) instead of the
        # int counter. Records accumulate across retries so the ledger sees
        # the union of every tool call the model has made for this op, then
        # dedup-by-(tool, arguments_hash) happens inside diversity_score().
        _op_explore_records: List[Any] = []

        for attempt in range(1 + orch._config.max_generate_retries):
            # ── Per-op cost cap check (Manifesto §5/§7) ──
            # If the cumulative spend across previous attempts has already
            # exceeded the dynamic cap, refuse to initiate another provider
            # call. Routes through the phase-aware terminal picker.
            if orch._cost_governor.is_exceeded(ctx.op_id):
                _cost_summary = orch._cost_governor.summary(ctx.op_id) or {}
                logger.warning(
                    "[Orchestrator] Per-op cost cap exceeded before attempt %d: "
                    "cumulative=$%.4f cap=$%.4f route=%s complexity=%s [%s]",
                    attempt + 1,
                    _cost_summary.get("cumulative_usd", 0.0),
                    _cost_summary.get("cap_usd", 0.0),
                    _cost_summary.get("route", "?"),
                    _cost_summary.get("complexity", "?"),
                    ctx.op_id,
                )
                _terminal = orch._l2_escape_terminal(ctx.phase)
                ctx = ctx.advance(
                    _terminal,
                    terminal_reason_code="op_cost_cap_exceeded",
                )
                await orch._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": "op_cost_cap_exceeded",
                        "cost_summary": dict(_cost_summary),
                        "entry_phase": "GENERATE",
                    },
                )
                orch._cost_governor.finish(ctx.op_id)
                return PhaseResult(

                    next_ctx=ctx, next_phase=None, status="fail",

                    reason=ctx.terminal_reason_code or "generate_terminal",

                    artifacts={"generation": generation, "episodic_memory": _episodic_memory},

                )

            try:
                # Heartbeat: GENERATE phase starting (Manifesto §7: Absolute Observability)
                try:
                    await orch._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="generate",
                        progress_pct=30.0 + attempt * 5.0,
                    )
                except Exception:
                    pass

                # Route-aware generation timeout (Manifesto §5):
                #   IMMEDIATE: 120s — fast reflex, but must accommodate Venom
                #              tool-round + full_content patch generation.
                #              Raised from 60s after bt-2026-04-11-085020
                #              diagnosed tool_round=yes calls needing 63s+ to
                #              stream 23KB+ full_content patches (first token
                #              at 1.1s, healthy ~365 bytes/s). The 60s cap was
                #              cutting legitimate streams off mid-output.
                #   STANDARD:  220s — DW primary then Claude fallback in
                #              series. Bumped from 120s after
                #              bt-2026-04-11-211131 diagnosed every standard
                #              op as `all_providers_exhausted`: DW eats 60-90s
                #              of the budget then Claude only has 30-40s left
                #              — too short for legitimate doc-gen / patch
                #              streams (which need 60-100s). 220s = ~90s DW
                #              cap + ~120s fallback hard cap + grace.
                #   COMPLEX:   240s — Claude planning + DW execution serial
                #   BACKGROUND/SPECULATIVE: 180s — no urgency
                _route = getattr(ctx, "provider_route", "") or "standard"
                # Per-route generation window — env-tunable so battle-test
                # harnesses (e.g. live_fire_exploration_gate.py) can widen
                # the architectural COMPLEX window without patching code.
                # Defaults preserve the 2026-04-12 calibration.
                _route_timeouts = {
                    "immediate": float(os.environ.get(
                        "JARVIS_GEN_TIMEOUT_IMMEDIATE_S", "120")),
                    "standard": float(os.environ.get(
                        "JARVIS_GEN_TIMEOUT_STANDARD_S", "220")),
                    "complex": float(os.environ.get(
                        "JARVIS_GEN_TIMEOUT_COMPLEX_S", "240")),
                    "background": float(os.environ.get(
                        "JARVIS_GEN_TIMEOUT_BACKGROUND_S", "180")),
                    "speculative": float(os.environ.get(
                        "JARVIS_GEN_TIMEOUT_SPECULATIVE_S", "180")),
                }
                _gen_timeout = _route_timeouts.get(
                    _route, orch._config.generation_timeout_s
                )
                # Read-only BG/SPEC subagent fan-out override (Session 6,
                # Derek 2026-04-17). The outer asyncio.wait_for at line
                # below enforces this timeout absolutely — when the op
                # is read-only and routed BG/SPEC, three parallel
                # subagents can consume MAX_PARALLEL_SCOPES *
                # PRIMARY_PROVIDER_TIMEOUT_S seconds of wall-clock before
                # the parent Claude begins synthesis. 180s is the
                # Session-5/6 killer. The cap-extension in candidate_
                # generator._call_fallback is necessary but insufficient
                # — this outer gate must also widen.
                if (
                    bool(getattr(ctx, "is_read_only", False))
                    and _route in ("background", "speculative")
                ):
                    try:
                        from backend.core.ouroboros.governance.subagent_contracts import (
                            MAX_PARALLEL_SCOPES,
                            PRIMARY_PROVIDER_TIMEOUT_S,
                        )
                        _fanout_budget_s = (
                            MAX_PARALLEL_SCOPES * PRIMARY_PROVIDER_TIMEOUT_S
                        )
                    except Exception:
                        _fanout_budget_s = 3 * 90  # Phase 1 Defaults
                    # Default 180s matches candidate_generator
                    # _BG_READONLY_SYNTHESIS_RESERVE_S — the two must
                    # stay aligned so the inner fallback cap and the
                    # outer orchestrator wait_for use the same reserve
                    # assumption. Session 12 empirically sized this.
                    _synthesis_reserve_s = float(os.environ.get(
                        "JARVIS_GEN_TIMEOUT_READONLY_SYNTHESIS_RESERVE_S",
                        "180",
                    ))
                    _gen_timeout_readonly = _gen_timeout + _fanout_budget_s + _synthesis_reserve_s
                    # Allow operator override via dedicated env var.
                    _gen_timeout_readonly = float(os.environ.get(
                        "JARVIS_GEN_TIMEOUT_BACKGROUND_READONLY_S",
                        str(_gen_timeout_readonly),
                    ))
                    logger.info(
                        "[Orchestrator] Read-only %s route: extending "
                        "gen_timeout %.0fs → %.0fs (fanout_budget=%.0fs, "
                        "synthesis_reserve=%.0fs) op=%s",
                        _route, _gen_timeout, _gen_timeout_readonly,
                        _fanout_budget_s, _synthesis_reserve_s, ctx.op_id,
                    )
                    _gen_timeout = _gen_timeout_readonly
                deadline = datetime.now(tz=timezone.utc) + timedelta(
                    seconds=_gen_timeout
                )
                # Emit streaming=start so SerpentFlow can render the
                # "synthesizing" header before tokens begin flowing.
                # Provider is unknown at this point (chosen during adaptive failback).
                try:
                    await orch._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="generate", progress_pct=31.0,
                        streaming="start", provider="",
                    )
                except Exception:
                    pass
                # Operator-visible token streaming (UX Priority 2 — closes
                # the "spinner for 2 minutes" gap). Gated on (1) the
                # JARVIS_UI_STREAMING_ENABLED env flag (checked inside the
                # renderer), and (2) the route: only IMMEDIATE / STANDARD /
                # COMPLEX are operator-visible. BACKGROUND and SPECULATIVE
                # skip — no operator is watching, and streaming serialization
                # would waste CPU that should go to inference.
                _stream_renderer = None
                if _route not in ("background", "speculative"):
                    try:
                        from backend.core.ouroboros.battle_test.stream_renderer import (
                            get_stream_renderer,
                        )
                        _stream_renderer = get_stream_renderer()
                        if _stream_renderer is not None:
                            # Provider name is unknown at this point
                            # (adaptive failback chooses mid-generate).
                            # Pass empty string; the renderer's INFO line
                            # will show provider="" rather than mislabeling
                            # with task_complexity.
                            _stream_renderer.start(
                                op_id=ctx.op_id,
                                provider="",
                            )
                    except Exception:
                        logger.debug(
                            "[Orchestrator] stream renderer start failed",
                            exc_info=True,
                        )
                        _stream_renderer = None
                # Hard timeout — the deadline is advisory to the generator,
                # but asyncio.wait_for is the Iron Gate (Manifesto §6).
                try:
                    # Phase B parallel-edge exploitation (Manifesto §2 + §3).
                    # Attempt DAG-driven fan-out first; on ANY fallback
                    # condition (flag off, no DAG, invalid DAG, edges>0,
                    # single-unit, BG route, read-only, per-unit error /
                    # timeout / noop) returns None — legacy single-stream
                    # path runs byte-identically below.
                    _parallel_gen = None
                    try:
                        from backend.core.ouroboros.governance.plan_exploit import (
                            try_parallel_generate,
                        )
                        _parallel_gen = await try_parallel_generate(
                            ctx,
                            deadline,
                            _gen_timeout,
                            orch._generator,
                            outer_grace_s=_OUTER_GATE_GRACE_S,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Observer contract: the exploit hook must NEVER
                        # break the FSM. Any unexpected failure routes
                        # straight to the legacy path.
                        logger.debug(
                            "[Orchestrator] plan_exploit fan-out raised — "
                            "falling back to legacy generate",
                            exc_info=True,
                        )
                        _parallel_gen = None

                    if _parallel_gen is not None:
                        generation = _parallel_gen
                    else:
                        generation = await asyncio.wait_for(
                            orch._generator.generate(ctx, deadline),
                            timeout=_gen_timeout + _OUTER_GATE_GRACE_S,
                        )

                    # Phase 1 Slice 1.3.b — capture the provider
                    # selection digest. Audit-only: the actual
                    # generation always runs live (LLM output isn't
                    # deterministic-replayable in this slice). The
                    # closure-over-`generation` pattern means
                    # capture_phase_decision just records the digest
                    # without re-invoking .generate(). RECORD writes;
                    # REPLAY looks up + verifies; VERIFY warns on
                    # provider drift.
                    try:
                        from backend.core.ouroboros.governance.determinism.phase_capture import (
                            capture_phase_decision,
                        )

                        async def _digest_compute() -> Any:
                            return {
                                "provider_name": str(
                                    getattr(
                                        generation, "provider_name", "",
                                    ) or "",
                                ),
                                "model_id": str(
                                    getattr(
                                        generation, "model_id", "",
                                    ) or "",
                                ),
                                "candidate_count": int(len(
                                    getattr(
                                        generation, "candidates", (),
                                    ) or (),
                                )),
                                "is_noop": bool(
                                    getattr(generation, "is_noop", False),
                                ),
                            }

                        await capture_phase_decision(
                            op_id=ctx.op_id,
                            phase="GENERATE",
                            kind="provider_selection",
                            ctx=ctx,
                            compute=_digest_compute,
                            extra_inputs={
                                "provider_route": str(
                                    getattr(
                                        ctx, "provider_route", "",
                                    ) or "",
                                ),
                                "parallel_gen_used": bool(
                                    _parallel_gen is not None,
                                ),
                            },
                        )
                    except Exception:  # noqa: BLE001 — defensive
                        # Capture failure does NOT propagate — the
                        # generation already succeeded, audit capture
                        # is best-effort.
                        logger.debug(
                            "[Orchestrator] capture_phase_decision "
                            "failed for GENERATE/provider_selection",
                            exc_info=True,
                        )
                finally:
                    # End the stream regardless of success / failure so the
                    # Live widget closes and the observability INFO line
                    # emits TTFT + TPS even when generation times out.
                    if _stream_renderer is not None:
                        try:
                            _stream_renderer.end()
                        except Exception:
                            logger.debug(
                                "[Orchestrator] stream renderer end failed",
                                exc_info=True,
                            )
                # Charge the CostGovernor with the actual generation cost.
                # Non-positive costs (cache hits, fallback stubs) are a no-op.
                try:
                    _cost_this_call = float(getattr(generation, "cost_usd", 0.0) or 0.0)
                    _prov_name = getattr(generation, "provider_name", "") or ""
                    if _cost_this_call > 0.0:
                        # Slice 2 of Per-Phase Cost Drill-Down arc:
                        # tag charge with current phase so the operator
                        # can answer "why did this op cost $X" per-phase.
                        _phase_tag = getattr(
                            getattr(ctx, "phase", None), "name", "",
                        ) or ""
                        orch._cost_governor.charge(
                            ctx.op_id, _cost_this_call, _prov_name,
                            phase=_phase_tag,
                        )
                        await orch._emit_route_cost_heartbeat(
                            ctx,
                            cost_usd=_cost_this_call,
                            provider=_prov_name,
                            route=getattr(ctx, "provider_route", "") or "standard",
                            cost_event="generation_attempt",
                        )
                except Exception:
                    logger.debug(
                        "[Orchestrator] CostGovernor.charge failed", exc_info=True,
                    )
                # Emit streaming=end to close the streaming block
                try:
                    await orch._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="generate", progress_pct=49.0,
                        streaming="end",
                    )
                except Exception:
                    pass

                # is_noop=True means the model signalled the change is already present.
                # Empty candidates is correct in this case — do not treat as a failure.
                if generation is not None and generation.is_noop:
                    break

                if generation is None or len(generation.candidates) == 0:
                    generation = None
                    raise RuntimeError("no_candidates_returned")

                # ── Forward-progress detector ──
                # Hash the first candidate's content and flag if the
                # retry loop is producing the same candidate repeatedly.
                # A trip means we're burning retries without any actual
                # change — escape the loop via the phase-aware terminal.
                try:
                    _fp_hash = candidate_content_hash(generation.candidates[0])
                    if _fp_hash and orch._forward_progress.observe(
                        ctx.op_id, _fp_hash,
                    ):
                        _fp_summary = orch._forward_progress.summary(ctx.op_id) or {}
                        logger.warning(
                            "[Orchestrator] Forward-progress trip: op=%s "
                            "stuck after %d repeats — escaping retry loop",
                            ctx.op_id,
                            _fp_summary.get("repeat_count", 0),
                        )
                        _terminal = orch._l2_escape_terminal(ctx.phase)
                        ctx = ctx.advance(
                            _terminal,
                            terminal_reason_code="no_forward_progress",
                        )
                        await orch._record_ledger(
                            ctx,
                            OperationState.FAILED,
                            {
                                "reason": "no_forward_progress",
                                "progress_summary": dict(_fp_summary),
                                "entry_phase": "GENERATE",
                            },
                        )
                        orch._forward_progress.finish(ctx.op_id)
                        return PhaseResult(

                            next_ctx=ctx, next_phase=None, status="fail",

                            reason=ctx.terminal_reason_code or "generate_terminal",

                            artifacts={"generation": generation, "episodic_memory": _episodic_memory},

                        )
                except Exception:
                    logger.debug(
                        "[Orchestrator] ForwardProgress.observe failed",
                        exc_info=True,
                    )

                # ── Productivity-ratio detector (EC9) ──
                # Complements EC8: EC8 catches byte-identical repetition;
                # EC9 catches *semantic* stagnation — candidates whose
                # normalized form (AST dump / canonical JSON / whitespace-
                # stripped) hasn't changed while the model keeps charging
                # us for retries. Trip = $ burned since last semantic
                # change exceeded the threshold AND we've seen enough
                # stable observations. Escape via phase-aware terminal.
                try:
                    _pd_hash = productivity_content_hash(
                        generation.candidates[0],
                        level=orch._productivity_detector.level,
                    )
                    if _pd_hash and orch._productivity_detector.observe(
                        ctx.op_id, _cost_this_call, _pd_hash,
                    ):
                        _pd_summary = orch._productivity_detector.summary(ctx.op_id) or {}
                        logger.warning(
                            "[Orchestrator] Productivity stall: op=%s "
                            "burned=$%.4f stable=%d level=%s — escaping retry loop",
                            ctx.op_id,
                            _pd_summary.get("cost_since_last_change_usd", 0.0),
                            _pd_summary.get("consecutive_stable", 0),
                            _pd_summary.get("config", {}).get("normalization_level", "?"),
                        )
                        _terminal = orch._l2_escape_terminal(ctx.phase)
                        ctx = ctx.advance(
                            _terminal,
                            terminal_reason_code="stalled_productivity",
                        )
                        await orch._record_ledger(
                            ctx,
                            OperationState.FAILED,
                            {
                                "reason": "stalled_productivity",
                                "productivity_summary": dict(_pd_summary),
                                "entry_phase": "GENERATE",
                            },
                        )
                        orch._productivity_detector.finish(ctx.op_id)
                        return PhaseResult(

                            next_ctx=ctx, next_phase=None, status="fail",

                            reason=ctx.terminal_reason_code or "generate_terminal",

                            artifacts={"generation": generation, "episodic_memory": _episodic_memory},

                        )
                except Exception:
                    logger.debug(
                        "[Orchestrator] ProductivityDetector.observe failed",
                        exc_info=True,
                    )

                # ── Iron Gate: deterministic post-generation quality checks ──
                # Manifesto §6: agentic intelligence proposes, deterministic
                # code validates. These checks hard-fail BEFORE validation
                # adapters run, routing back through the GENERATE retry loop
                # with explicit error feedback so the model learns in-flight.
                #
                # Gate 1 — Exploration-first enforcement (no patch without
                # reading the codebase). Trivial ops bypass (small-surface
                # rewrites don't need the floor).
                #
                # Complexity-scaled threshold (bt-2026-04-11-090651 root cause):
                # simple ops (single target file, mechanical change) need only
                # 1 exploration call — one read_file IS reading the codebase.
                # moderate/complex ops keep the 2-call floor because they
                # touch multiple surfaces. Claude-sonnet-4-6 reliably refused
                # retry feedback on simple ops ("1/2 → 0/2") because the
                # exploration demand didn't match the task size; scaling by
                # complexity restores intent-alignment while preserving the
                # gate's purpose.
                _task_complexity = getattr(ctx, "task_complexity", "") or ""
                _EXPLORATION_TOOLS = frozenset({
                    "read_file", "search_code", "get_callers", "list_symbols",
                    "glob_files", "list_dir",
                })
                _env_min = os.environ.get("JARVIS_MIN_EXPLORATION_CALLS")
                if _env_min is not None:
                    _min_explore = int(_env_min)
                elif _task_complexity == "simple":
                    _min_explore = 1
                else:
                    _min_explore = 2
                _explore_gate_enabled = (
                    os.environ.get("JARVIS_EXPLORATION_GATE", "true").lower() == "true"
                    and _task_complexity != "trivial"
                )
                if _explore_gate_enabled:
                    _explore_count = sum(
                        1 for _rec in (generation.tool_execution_records or ())
                        if getattr(_rec, "tool_name", "") in _EXPLORATION_TOOLS
                    )
                    # Preloaded-prompt credit: when the lean prompt builder
                    # inlines target regions directly into the generation
                    # prompt, the model has already "seen" those files without
                    # needing a read_file tool call — semantically equivalent
                    # exploration. Gives DW BACKGROUND route (no tool loop)
                    # and simple/trivial ops a fair path through the gate.
                    _preloaded_credit = len(
                        getattr(generation, "prompt_preloaded_files", ()) or ()
                    )
                    # Roll the per-attempt count into the per-op credit BEFORE
                    # comparing — a prior attempt that already satisfied the
                    # floor lets a no-tool retry pass (the rejected file is
                    # already in the retry-feedback prompt).
                    _op_explore_credit += _explore_count + _preloaded_credit

                    # Accumulate ledger records across retry attempts (#103).
                    # Cumulative semantics mirror _op_explore_credit — the
                    # ledger sees every tool call the model has made for this
                    # op, then dedup-by-(tool, arguments_hash) happens inside
                    # diversity_score(). Preloaded files become synthetic
                    # read_file records so the ledger grants comprehension
                    # credit matching the legacy counter's preload behavior.
                    _op_explore_records.extend(
                        generation.tool_execution_records or ()
                    )
                    for _pf in (
                        getattr(generation, "prompt_preloaded_files", ()) or ()
                    ):
                        _op_explore_records.append(
                            _PreloadedExplorationRecord(str(_pf))
                        )

                    from backend.core.ouroboros.governance.exploration_engine import (  # noqa: E501
                        ExplorationFloors,
                        ExplorationInsufficientError,
                        ExplorationLedger,
                        evaluate_exploration,
                        is_ledger_enabled,
                    )

                    if is_ledger_enabled():
                        # ── DECISION path (#103) ──
                        # Ledger is authoritative. Legacy int-counter gate is
                        # skipped entirely. Emit ``(decision)`` log tag — kept
                        # distinct from ``(shadow)`` so ops can grep either
                        # mode without ambiguity.
                        try:
                            _ledger = ExplorationLedger.from_records(
                                _op_explore_records
                            )
                            _floors = ExplorationFloors.from_env_with_adapted(_task_complexity)
                            _verdict = evaluate_exploration(_ledger, _floors)
                        except Exception:
                            # If the ledger itself blows up, fall through to
                            # the legacy counter gate so we never leave the op
                            # ungated. Log once so the failure is visible.
                            logger.exception(
                                "[Orchestrator] ExplorationLedger(decision) "
                                "evaluation failed — falling back to counter"
                            )
                            _verdict = None
                        if _verdict is not None:
                            _covered_names = sorted(
                                c.value for c in _verdict.categories_covered
                            )
                            logger.info(
                                "[Orchestrator] ExplorationLedger(decision) "
                                "op=%s complexity=%s score=%.2f min_score=%.2f "
                                "unique=%d categories=%s would_pass=%s",
                                ctx.op_id[:12],
                                _task_complexity or "unknown",
                                _verdict.score,
                                _floors.min_score,
                                _ledger.unique_call_count(),
                                ",".join(_covered_names) or "-",
                                _verdict.sufficient,
                            )
                            if _verdict.insufficient:
                                _missing = sorted(
                                    c.value for c in _verdict.missing_categories
                                )
                                _decision_msg = (
                                    f"exploration_insufficient: "
                                    f"score={_verdict.score:.1f}/"
                                    f"{_floors.min_score:.1f} "
                                    f"categories={len(_verdict.categories_covered)}/"
                                    f"{_floors.min_categories} "
                                    f"missing={','.join(_missing) or '-'}"
                                )
                                logger.warning(
                                    "[Orchestrator] Iron Gate — "
                                    "ExplorationLedger(decision) insufficient "
                                    "op=%s %s (attempt=%d)",
                                    ctx.op_id[:12],
                                    _decision_msg,
                                    attempt + 1,
                                )
                                generation = None
                                raise ExplorationInsufficientError(
                                    _decision_msg,
                                    verdict=_verdict,
                                    floors=_floors,
                                )
                            # Ledger PASSED — skip legacy counter gate
                            # entirely. Jump to the ASCII gate below.
                        else:
                            # Ledger eval crashed → fall through to legacy gate
                            pass

                    # ── LEGACY path (flag off) or ledger-eval fallback ──
                    # Shadow log + int-counter gate. Shadow log is suppressed
                    # when enforcement is on (the decision log above covers
                    # that path) so operators don't see duplicate lines.
                    if not is_ledger_enabled():
                        _shadow_on = (
                            os.environ.get(
                                "JARVIS_EXPLORATION_SHADOW_LOG", "",
                            ).strip().lower() in _TRUTHY
                        )
                        if _shadow_on:
                            try:
                                _sledger = ExplorationLedger.from_records(
                                    _op_explore_records
                                )
                                _sfloors = ExplorationFloors.from_env_with_adapted(
                                    _task_complexity
                                )
                                _sverdict = evaluate_exploration(
                                    _sledger, _sfloors
                                )
                                _scovered = sorted(
                                    c.value for c in _sverdict.categories_covered
                                )
                                logger.info(
                                    "[Orchestrator] ExplorationLedger(shadow) "
                                    "op=%s complexity=%s legacy_credit=%d "
                                    "score=%.2f min_score=%.2f unique=%d "
                                    "categories=%s would_pass=%s",
                                    ctx.op_id[:12],
                                    _task_complexity or "unknown",
                                    _op_explore_credit,
                                    _sverdict.score,
                                    _sfloors.min_score,
                                    _sledger.unique_call_count(),
                                    ",".join(_scovered) or "-",
                                    _sverdict.sufficient,
                                )
                            except Exception:
                                logger.debug(
                                    "[Orchestrator] ExplorationLedger shadow "
                                    "log error",
                                    exc_info=True,
                                )

                    if (
                        not is_ledger_enabled()
                        and _op_explore_credit < _min_explore
                    ):
                        _explore_err = (
                            f"exploration_insufficient: {_op_explore_credit}/{_min_explore} "
                            f"exploration tool calls (expected >= {_min_explore}). "
                            f"You MUST call read_file/search_code/get_callers at least "
                            f"{_min_explore} times BEFORE proposing any patch. "
                            f"Use the tool loop to read the target file and grep for "
                            f"callers, then return your patch."
                        )
                        logger.warning(
                            "[Orchestrator] Iron Gate — exploration_insufficient: "
                            "%d/%d (attempt=%d cumulative, preloaded=%d) for op=%s",
                            _op_explore_credit, _min_explore, attempt + 1,
                            _preloaded_credit, ctx.op_id[:12],
                        )
                        generation = None
                        raise RuntimeError(_explore_err)

                # Gate 2 — ASCII/Unicode strictness (prevent rapidفuzz-class
                # typos where model emits non-ASCII code points in identifier
                # positions). Deterministic scan; O(n) on candidate size.
                # Delegates to AsciiStrictGate which:
                #   1) auto-repairs common punctuation drift (em-dash →
                #      hyphen, curly quotes → straight, ellipsis → ...,
                #      nbsp → space, zero-width strip) IN-PLACE on the
                #      candidate dict — healing the deterministic training-
                #      data artifact where Claude always inserts U+2014 at
                #      the same byte offset of requirements.txt.
                #   2) hard-rejects any residue (Unicode letters in
                #      identifier positions, unlisted symbols) per the
                #      original Iron Gate contract.
                _ascii_gate = AsciiStrictGate()
                if _ascii_gate.enabled:
                    for _cand in generation.candidates:
                        _ok, _ascii_err, _bad_list = _ascii_gate.check(_cand)
                        _repairs = _cand.get("_ascii_repair_count", 0) if isinstance(_cand, dict) else 0
                        if _repairs:
                            logger.info(
                                "[Orchestrator] Iron Gate — ascii_auto_repaired: "
                                "%d codepoint(s) healed file=%s op=%s",
                                _repairs,
                                _cand.get("file_path", "?") if isinstance(_cand, dict) else "?",
                                ctx.op_id[:12],
                            )
                        if not _ok:
                            _samples_str = ", ".join(
                                bc.format_sample() for bc in _bad_list
                            )
                            logger.warning(
                                "[Orchestrator] Iron Gate — ascii_corruption: "
                                "%d offender(s) [%s] op=%s",
                                len(_bad_list), _samples_str, ctx.op_id[:12],
                            )
                            # Stash the rejected content + offenders on the
                            # exception so the retry feedback builder can
                            # extract the specific offending lines and show
                            # them back to the model in context. Without
                            # this, the model only sees "U+0641 at L106:C6"
                            # which isn't enough to locate the bad identifier
                            # in a 200-line file.
                            _rejected_content = ""
                            if isinstance(_cand, dict):
                                _rejected_content = (
                                    _cand.get("full_content", "")
                                    or _cand.get("raw_content", "")
                                    or ""
                                )
                                if not _rejected_content and isinstance(_cand.get("files"), list):
                                    # Multi-file shape — grab the first file matching an offender
                                    _bad_path = _bad_list[0].file_path if _bad_list else ""
                                    for _entry in _cand["files"]:
                                        if isinstance(_entry, dict) and _entry.get("file_path") == _bad_path:
                                            _rejected_content = _entry.get("full_content", "") or ""
                                            break
                            generation = None
                            _ascii_exc = RuntimeError(_ascii_err or "ascii_corruption")
                            # Private attributes — read back in the retry feedback builder.
                            _ascii_exc._ascii_bad_codepoints = _bad_list  # type: ignore[attr-defined]
                            _ascii_exc._ascii_rejected_content = _rejected_content  # type: ignore[attr-defined]
                            raise _ascii_exc

                # Gate 3 — Dependency file integrity. Catches hallucinated
                # package-name renames/truncations in requirements.txt (and
                # future: package.json, Cargo.toml, etc.). Engineered in
                # response to bt-2026-04-10-184157, where Claude emitted a
                # requirements.txt patch renaming ``anthropic`` →
                # ``anthropichttp`` and ``rapidfuzz`` → ``rapidfu`` — two
                # pure-ASCII corruptions that slipped past every other gate.
                try:
                    from backend.core.ouroboros.governance.dependency_file_gate import (
                        check_candidate as _dep_check,
                    )
                except ImportError:
                    _dep_check = None  # type: ignore[assignment]
                if _dep_check is not None:
                    for _cand in generation.candidates:
                        _dep_result = _dep_check(_cand, orch._config.project_root)
                        if _dep_result is None:
                            continue
                        _dep_reason, _dep_offenders = _dep_result
                        logger.warning(
                            "[Orchestrator] Iron Gate — dependency_file_integrity: "
                            "%d offender(s) [%s] op=%s",
                            len(_dep_offenders),
                            ", ".join(_dep_offenders[:5]),
                            ctx.op_id[:12],
                        )
                        # Extract the rejected content for retry feedback.
                        _rejected_content = ""
                        if isinstance(_cand, dict):
                            _rejected_content = _cand.get("full_content", "") or ""
                            if not _rejected_content and isinstance(_cand.get("files"), list):
                                for _entry in _cand["files"]:
                                    if not isinstance(_entry, dict):
                                        continue
                                    _ep = _entry.get("file_path", "") or ""
                                    from backend.core.ouroboros.governance.dependency_file_gate import is_dependency_file
                                    if is_dependency_file(_ep):
                                        _rejected_content = _entry.get("full_content", "") or ""
                                        break
                        generation = None
                        _dep_exc = RuntimeError(_dep_reason)
                        # Private attributes — retry feedback builder reads these.
                        _dep_exc._dep_file_offenders = _dep_offenders  # type: ignore[attr-defined]
                        _dep_exc._dep_file_rejected_content = _rejected_content  # type: ignore[attr-defined]
                        raise _dep_exc

                # Gate 4 — Docstring multi-line collapse detection. Catches
                # the regression where Claude rewrites a multi-line module
                # or function docstring as a single-line literal containing
                # ``\n`` escape sequences (bt-2026-04-11-211131,
                # headless_cli.py). Valid Python that breaks every reader.
                try:
                    from backend.core.ouroboros.governance.docstring_collapse_gate import (
                        check_candidate as _docstring_check,
                    )
                except ImportError:
                    _docstring_check = None  # type: ignore[assignment]
                if _docstring_check is not None:
                    for _cand in generation.candidates:
                        _ds_result = _docstring_check(_cand, orch._config.project_root)
                        if _ds_result is None:
                            continue
                        _ds_reason, _ds_offenders = _ds_result
                        logger.warning(
                            "[Orchestrator] Iron Gate — docstring_collapse: "
                            "%d offender(s) [%s] op=%s",
                            len(_ds_offenders),
                            ", ".join(_ds_offenders[:5]),
                            ctx.op_id[:12],
                        )
                        _rejected_content = ""
                        if isinstance(_cand, dict):
                            _rejected_content = _cand.get("full_content", "") or ""
                            if not _rejected_content and isinstance(_cand.get("files"), list):
                                for _entry in _cand["files"]:
                                    if isinstance(_entry, dict) and (
                                        _entry.get("file_path", "") or ""
                                    ).endswith(".py"):
                                        _rejected_content = _entry.get("full_content", "") or ""
                                        break
                        generation = None
                        _ds_exc = RuntimeError(_ds_reason)
                        _ds_exc._docstring_collapse_offenders = _ds_offenders  # type: ignore[attr-defined]
                        _ds_exc._docstring_collapse_rejected_content = _rejected_content  # type: ignore[attr-defined]
                        raise _ds_exc

                # Gate 5 — Multi-file coverage. Session O (bt-2026-04-15-
                # 175547) closed the full governed APPLY arc but only 1
                # of 4 target files landed on disk because the winning
                # candidate returned legacy {file_path, full_content}
                # instead of {files: [...]}, so _apply_multi_file_candidate
                # was never invoked. This gate rejects any multi-target op
                # whose candidate fails to cover every path in
                # context.target_files via a populated files: [...] list.
                # The retry-feedback builder names the missing paths and
                # reiterates the multi-file contract. Master switch:
                # JARVIS_MULTI_FILE_ENFORCEMENT (default true).
                try:
                    from backend.core.ouroboros.governance.multi_file_coverage_gate import (
                        check_candidate as _mf_check,
                    )
                except ImportError:
                    _mf_check = None  # type: ignore[assignment]
                if _mf_check is not None:
                    for _cand in generation.candidates:
                        _mf_result = _mf_check(
                            _cand,
                            ctx.target_files,
                            orch._config.project_root,
                        )
                        if _mf_result is None:
                            continue
                        _mf_reason, _mf_missing = _mf_result
                        logger.warning(
                            "[Orchestrator] Iron Gate — multi_file_coverage: "
                            "missing %d/%d [%s] op=%s",
                            len(_mf_missing),
                            len(ctx.target_files),
                            ", ".join(_mf_missing[:5]),
                            ctx.op_id[:12],
                        )
                        generation = None
                        _mf_exc = RuntimeError(_mf_reason)
                        # Private attributes — retry feedback builder reads these.
                        _mf_exc._mf_missing_paths = _mf_missing  # type: ignore[attr-defined]
                        _mf_exc._mf_target_files = tuple(ctx.target_files)  # type: ignore[attr-defined]
                        raise _mf_exc

                # Heartbeat: generation succeeded with candidates
                try:
                    await orch._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="generate",
                        progress_pct=50.0,
                    )
                    # Also emit rich payload for BattleDiffTransport
                    _gen_msg = type(
                        "_Msg", (), {
                            "payload": {
                                "phase": "generate",
                                "candidates_count": len(generation.candidates),
                                "provider": generation.provider_name,
                                "model_id": getattr(generation, "model_id", ""),
                                "generation_duration_s": generation.generation_duration_s,
                                "tool_records": len(getattr(generation, "tool_execution_records", ()) or ()),
                                "total_input_tokens": getattr(generation, "total_input_tokens", 0),
                                "total_output_tokens": getattr(generation, "total_output_tokens", 0),
                                "cost_usd": getattr(generation, "cost_usd", 0.0),
                                # Include candidate file paths and preview for TUI display
                                "candidate_files": [
                                    getattr(c, "file_path", "") for c in generation.candidates[:3]
                                ],
                                "candidate_rationales": [
                                    (c.get("rationale", "") or "")[:80]
                                    for c in generation.candidates[:3]
                                ],
                                "candidate_preview": (
                                    getattr(generation.candidates[0], "raw_content", "")[:500]
                                    if generation.candidates else ""
                                ),
                            },
                            "op_id": ctx.op_id,
                            "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                        },
                    )()
                    for _t in getattr(orch._stack.comm, "_transports", []):
                        try:
                            await _t.send(_gen_msg)
                        except Exception:
                            pass
                except Exception:
                    pass

                # Success -- record reasoning trace + dialogue
                if orch._reasoning_narrator is not None:
                    try:
                        orch._reasoning_narrator.record_generate(
                            ctx.op_id, generation.provider_name,
                            len(generation.candidates), generation.generation_duration_s,
                        )
                    except Exception:
                        pass
                if orch._dialogue_store is not None:
                    try:
                        _d = orch._dialogue_store.get_active(ctx.op_id)
                        if _d:
                            _d.add_entry(
                                "GENERATE",
                                f"{generation.provider_name} produced {len(generation.candidates)} "
                                f"candidates in {generation.generation_duration_s:.1f}s",
                            )
                    except Exception:
                        pass

                # Success -- break out of retry loop
                break

            except Exception as exc:
                _err_msg = str(exc)
                _route = getattr(ctx, "provider_route", "")

                # ── Partial shadow log (widened) ──
                # Fire the ExplorationLedger shadow pass for EVERY
                # generation failure, regardless of route/cause. The
                # original BG-DW-only branch missed failure modes like
                # doubleword_schema_invalid, all_providers_exhausted,
                # APITimeout. We classify the cause from _err_msg so the
                # log line still tells you what killed the attempt, and
                # we pull whatever tool_execution_records are reachable
                # off the exception (may be empty). No-op when shadow
                # logging is off so this stays free in production.
                _shadow_on_partial = (
                    os.environ.get(
                        "JARVIS_EXPLORATION_SHADOW_LOG", "",
                    ).strip().lower() in {"1", "true", "yes", "on"}
                )
                if _shadow_on_partial:
                    try:
                        from backend.core.ouroboros.governance.exploration_engine import (  # noqa: E501
                            ExplorationFloors,
                            ExplorationLedger,
                            evaluate_exploration,
                        )
                        _partial_records = getattr(
                            exc, "tool_execution_records", ()
                        ) or ()
                        _pledger = ExplorationLedger.from_records(_partial_records)
                        _ptask_complexity = getattr(
                            ctx, "task_complexity", "",
                        ) or ""
                        _pfloors = ExplorationFloors.from_env_with_adapted(_ptask_complexity)
                        _pverdict = evaluate_exploration(_pledger, _pfloors)
                        _pcovered = sorted(
                            c.value for c in _pverdict.categories_covered
                        )
                        # Classify cause from error string — cheap
                        # substring match, no regex. Order matters:
                        # most specific first.
                        if "background_dw_" in _err_msg:
                            _pcause = "bg_dw_failure"
                        elif "doubleword_schema_invalid" in _err_msg:
                            _pcause = "dw_schema_invalid"
                        elif "all_providers_exhausted" in _err_msg:
                            _pcause = "all_providers_exhausted"
                        elif "APITimeout" in _err_msg or "timeout" in _err_msg.lower():
                            _pcause = "provider_timeout"
                        else:
                            _pcause = "generic_gen_failure"
                        logger.info(
                            "[Orchestrator] ExplorationLedger(shadow,partial) "
                            "op=%s complexity=%s route=%s cause=%s "
                            "records=%d score=%.2f min_score=%.2f unique=%d "
                            "categories=%s would_pass=%s",
                            ctx.op_id[:12],
                            _ptask_complexity or "unknown",
                            _route or "unknown",
                            _pcause,
                            len(_partial_records),
                            _pverdict.score,
                            _pfloors.min_score,
                            _pledger.unique_call_count(),
                            ",".join(_pcovered) or "-",
                            _pverdict.sufficient,
                        )
                    except Exception:
                        logger.debug(
                            "[Orchestrator] ExplorationLedger partial shadow log error",
                            exc_info=True,
                        )

                # ── BACKGROUND / SPECULATIVE route failures ──
                # These routes intentionally avoid Claude. Don't retry
                # with expensive providers — accept failure gracefully.
                if _route == "speculative" and "speculative_deferred" in _err_msg:
                    # Speculative ops are fire-and-forget — not a failure.
                    logger.info(
                        "[Orchestrator] SPECULATIVE op deferred (DW background) [%s]",
                        ctx.op_id,
                    )
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="speculative_deferred",
                    )
                    await orch._record_ledger(
                        ctx, OperationState.COMPLETED,
                        {"reason": "speculative_deferred", "route": "speculative"},
                    )
                    return PhaseResult(

                        next_ctx=ctx, next_phase=None, status="fail",

                        reason=ctx.terminal_reason_code or "generate_terminal",

                        artifacts={"generation": generation, "episodic_memory": _episodic_memory},

                    )

                if _route == "background" and (
                    "background_dw_" in _err_msg
                    or "background_fallback_failed" in _err_msg
                ):
                    # Background failure — accept gracefully, don't
                    # hammer the retry loop. Covers both the legacy
                    # DW-only failure mode ("background_dw_*") and the
                    # new cascade failure mode
                    # ("background_fallback_failed:...") introduced when
                    # JARVIS_BACKGROUND_ALLOW_FALLBACK=true and the
                    # Claude cascade itself also fails. In either case,
                    # the sensor will re-detect if the underlying work
                    # is still relevant.
                    _is_cascade_failure = "background_fallback_failed" in _err_msg
                    logger.info(
                        "[Orchestrator] BACKGROUND route: %s failed (%s), "
                        "accepting [%s]",
                        "DW+Claude cascade" if _is_cascade_failure else "DW",
                        _err_msg[:120], ctx.op_id,
                    )
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code=f"background_accepted:{_err_msg[:80]}",
                    )
                    await orch._record_ledger(
                        ctx, OperationState.FAILED,
                        {
                            "reason": (
                                "background_cascade_failure"
                                if _is_cascade_failure else "background_dw_failure"
                            ),
                            "error": _err_msg[:200],
                            "route": "background",
                        },
                    )
                    return PhaseResult(

                        next_ctx=ctx, next_phase=None, status="fail",

                        reason=ctx.terminal_reason_code or "generate_terminal",

                        artifacts={"generation": generation, "episodic_memory": _episodic_memory},

                    )

                logger.warning(
                    "Generation attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    1 + orch._config.max_generate_retries,
                    ctx.op_id,
                    exc,
                )
                generate_retries_remaining -= 1
                if generate_retries_remaining < 0:
                    # ── IMMEDIATE → STANDARD demotion ──
                    # If IMMEDIATE exhausted Claude retries, demote to
                    # STANDARD (DW primary → Claude fallback) for one
                    # last attempt.  Direct call — don't rely on the
                    # exhausted for-loop range.
                    if _route == "immediate":
                        logger.info(
                            "[Orchestrator] IMMEDIATE exhausted — demoting "
                            "to STANDARD route for DW attempt [%s]",
                            ctx.op_id,
                        )
                        object.__setattr__(ctx, "provider_route", "standard")
                        object.__setattr__(
                            ctx, "provider_route_reason",
                            f"demotion:immediate_exhausted:{_err_msg[:60]}",
                        )
                        try:
                            await orch._stack.comm.emit_decision(
                                op_id=ctx.op_id,
                                outcome="standard",
                                reason_code="route_demoted:immediate_exhausted",
                                details={
                                    "route": "standard",
                                    "previous_route": "immediate",
                                    "route_description": "Demoted to STANDARD after IMMEDIATE exhaustion",
                                    "budget_profile": "220s fallback budget",
                                    "route_reason": getattr(ctx, "provider_route_reason", ""),
                                },
                            )
                        except Exception:
                            pass
                        _route = "standard"  # update local for timeout calc
                        # Refresh the cost-governor cap for the new route so
                        # the demotion gets a proportional budget headroom.
                        try:
                            orch._cost_governor.start(
                                op_id=ctx.op_id,
                                route="standard",
                                complexity=getattr(ctx, "task_complexity", "") or "",
                                is_read_only=bool(getattr(ctx, "is_read_only", False)),
                            )
                        except Exception:
                            pass
                        # Guard the demotion call itself: if cumulative spend
                        # already blew past the new cap, skip the demotion.
                        if orch._cost_governor.is_exceeded(ctx.op_id):
                            logger.warning(
                                "[Orchestrator] Skipping STANDARD demotion — "
                                "cost cap already exceeded [%s]",
                                ctx.op_id,
                            )
                        else:
                            try:
                                _dem_deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=220.0)
                                generation = await asyncio.wait_for(
                                    orch._generator.generate(ctx, _dem_deadline),
                                    timeout=220.0 + _OUTER_GATE_GRACE_S,
                                )
                                # Charge demotion call cost (may be zero).
                                try:
                                    _dem_cost = float(getattr(generation, "cost_usd", 0.0) or 0.0)
                                    _dem_prov = getattr(generation, "provider_name", "") or ""
                                    if _dem_cost > 0.0:
                                        _dem_phase = getattr(
                                            getattr(ctx, "phase", None),
                                            "name", "",
                                        ) or ""
                                        orch._cost_governor.charge(
                                            ctx.op_id, _dem_cost, _dem_prov,
                                            phase=_dem_phase,
                                        )
                                        await orch._emit_route_cost_heartbeat(
                                            ctx,
                                            cost_usd=_dem_cost,
                                            provider=_dem_prov,
                                            route="standard",
                                            cost_event="demotion_attempt",
                                        )
                                except Exception:
                                    pass
                                if generation is not None and len(generation.candidates) > 0:
                                    break  # success — continue pipeline
                                generation = None
                            except Exception as dem_exc:
                                logger.warning(
                                    "[Orchestrator] STANDARD demotion also failed: %s [%s]",
                                    dem_exc, ctx.op_id,
                                )

                    # All retries truly exhausted
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="generation_failed",
                    )
                    await orch._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {"reason": "generation_failed", "error": str(exc)},
                    )
                    return PhaseResult(

                        next_ctx=ctx, next_phase=None, status="fail",

                        reason=ctx.terminal_reason_code or "generate_terminal",

                        artifacts={"generation": generation, "episodic_memory": _episodic_memory},

                    )
                # P2: Dynamic Re-Planning — suggest alternative strategy on failure
                try:
                    from backend.core.ouroboros.governance.self_evolution import DynamicRePlanner
                    _attempt_num = orch._config.max_generate_retries - generate_retries_remaining + 1
                    _fc = validation.failure_class or "" if 'validation' in dir() else ""
                    _em = validation.short_summary or "" if 'validation' in dir() else ""
                    _replan = DynamicRePlanner.suggest_replan(_fc, _em, _attempt_num)
                    if _replan:
                        _replan_text = DynamicRePlanner.format_for_prompt(_replan)
                        logger.info(
                            "[Orchestrator] Dynamic re-plan: %s (attempt %d)",
                            _replan.trigger[:50], _attempt_num,
                        )
                except Exception:
                    _replan_text = ""
                    pass

                # Retry: advance to GENERATE_RETRY with episodic memory context
                _retry_ctx_kwargs = {}

                # Inject direct error feedback so the model knows what went wrong
                _err_str = str(exc)

                # ── Iron Gate failures get targeted, in-flight instructions ──
                if _err_str.startswith("exploration_insufficient"):
                    # Ledger path (#103): when the exception carries a
                    # verdict + floors, render a category-aware feedback
                    # block so the model sees *which* categories are missing
                    # rather than the generic "call more tools" boilerplate.
                    # Legacy counter path has neither attribute and falls
                    # through to the hand-written block below.
                    _exc_verdict = getattr(exc, "verdict", None)
                    _exc_floors = getattr(exc, "floors", None)
                    if _exc_verdict is not None and _exc_floors is not None:
                        try:
                            from backend.core.ouroboros.governance.exploration_engine import (  # noqa: E501
                                render_retry_feedback,
                            )
                            _ledger_feedback = render_retry_feedback(
                                _exc_verdict, _exc_floors,
                            )
                        except Exception:
                            _ledger_feedback = ""
                    else:
                        _ledger_feedback = ""
                    if _ledger_feedback:
                        # ── CRITICAL_SYSTEM_OVERRIDE escalation ──
                        # Live-fire botyivw5b proved the feedback was
                        # landing in the prompt but the model was
                        # attending to the front-loaded task description
                        # and tool boilerplate instead of the retry
                        # directive. This is an attention-mechanism
                        # interference problem, not an injection
                        # problem. The three-pronged fix (this block is
                        # prong 2):
                        #
                        #   1. recency bias — _build_lean_codegen_prompt
                        #      appends strategic_memory as the ABSOLUTE
                        #      LAST section (after output schema), so
                        #      the model reads it last.
                        #   2. XML structural override — frontier models
                        #      are fine-tuned to obey
                        #      ``<CRITICAL_SYSTEM_OVERRIDE>`` tags at
                        #      higher priority than general prompt text.
                        #      "Mathematically required" language raises
                        #      perceived authority.
                        #   3. simulated assistant prefill — the lean
                        #      builder appends a model-voice commitment
                        #      stub after this block (persona
                        #      continuation kill switch; literal API
                        #      prefill is incompatible with the JSON
                        #      contract + tool_use response type on
                        #      sonnet-4-6 stream).
                        #
                        # Derive the specific tool names from the missing
                        # categories so the override preempts ambiguity
                        # about what "call_graph" means.
                        _cat_to_tools = {
                            "call_graph": "get_callers",
                            "history": "git_blame or git_log",
                            "discovery": "search_code or glob_files",
                            "structure": "list_symbols",
                            "comprehension": "read_file",
                        }
                        try:
                            _missing_cats = sorted(
                                c.value for c in _exc_verdict.missing_categories
                            )
                        except Exception:
                            _missing_cats = []
                        _required_tools = [
                            _cat_to_tools.get(c, c) for c in _missing_cats
                        ]
                        _cat_list = ", ".join(_missing_cats) or "diverse"
                        _tool_list = ", ".join(_required_tools) or "get_callers"
                        _error_feedback = (
                            "<CRITICAL_SYSTEM_OVERRIDE>\n"
                            "Previous attempt failed the Iron Gate exploration "
                            "ledger. You are mathematically required to invoke "
                            f"tools from the following missing categories: "
                            f"[{_cat_list}].\n"
                            f"You MUST invoke {_tool_list} before emitting any "
                            "patch.\n"
                            "The ExplorationLedger dedups by (tool, "
                            "arguments_hash) — repeating the same read_file on "
                            "the same path earns ZERO new credit.\n"
                            "Your next action MUST be one of the required tool "
                            "calls listed above. Do NOT emit a patch. Do NOT "
                            "call read_file again on files you already read.\n"
                            "</CRITICAL_SYSTEM_OVERRIDE>\n\n"
                            "## PREVIOUS GENERATION REJECTED — EXPLORATION GATE\n\n"
                            f"{_ledger_feedback}\n\n"
                            "INSTRUCTIONS FOR RETRY:\n"
                            "- Call the missing-category tools listed above BEFORE\n"
                            "  emitting any patch. The ledger dedups by (tool,\n"
                            "  arguments_hash) so repeating the same read_file on\n"
                            "  the same path adds no credit.\n"
                            "- Prefer get_callers, list_symbols, and git_blame over\n"
                            "  repeated read_file calls — diversity beats volume.\n"
                            "- Exploration is NOT optional. Patches without context\n"
                            "  corrupt code.\n"
                        )
                    else:
                        _error_feedback = (
                            "## PREVIOUS GENERATION REJECTED — NO EXPLORATION\n\n"
                            f"{_err_str[:400]}\n\n"
                            "INSTRUCTIONS FOR RETRY:\n"
                            "- BEFORE writing any patch, call read_file on the target file(s).\n"
                            "- Call search_code or get_callers for any function/symbol you are\n"
                            "  about to modify so you understand its callers and tests.\n"
                            "- Only after you have at least 2 exploration tool calls in your\n"
                            "  tool_execution_records may you emit the final patch.\n"
                            "- Exploration is NOT optional. Patches without context corrupt code.\n"
                        )
                elif _err_str.startswith("ascii_corruption"):
                    # Extract the specific offending lines from the rejected
                    # candidate so the model sees its own bad code in context
                    # (not just "U+0641 at L106:C6"). The orchestrator stashed
                    # the full_content + BadCodepoint list on the exception
                    # just before raising, so we can reconstruct the exact
                    # lines that tripped the gate and show ASCII-only
                    # corrections alongside them.
                    _rejected = getattr(exc, "_ascii_rejected_content", "") or ""
                    _bad_cps = getattr(exc, "_ascii_bad_codepoints", None) or []
                    _offending_block = ""
                    if _rejected and _bad_cps:
                        _lines = _rejected.split("\n")
                        _seen_lines: set = set()
                        _line_samples = []
                        for _bc in _bad_cps[:5]:
                            _ln = getattr(_bc, "line", 0)
                            if _ln <= 0 or _ln in _seen_lines or _ln > len(_lines):
                                continue
                            _seen_lines.add(_ln)
                            _raw_line = _lines[_ln - 1]
                            # Build an ASCII-only "what-to-write-instead" hint
                            # by stripping every non-ASCII codepoint. For
                            # letters this produces a visible "hole" that
                            # shows where the model must make a deliberate
                            # spelling decision (e.g. rapidفuzz → rapiduzz,
                            # which makes the corruption obvious).
                            _stripped = "".join(
                                ch if ord(ch) < 128 else "·" for ch in _raw_line
                            )
                            _cp_hex = f"U+{getattr(_bc, 'codepoint', 0):04X}"
                            _char = getattr(_bc, "char", "?")
                            _line_samples.append(
                                f"  line {_ln} contains {_cp_hex} '{_char}':\n"
                                f"      WRONG: {_raw_line}\n"
                                f"      (·=non-ASCII): {_stripped}"
                            )
                        if _line_samples:
                            _offending_block = (
                                "\nSPECIFIC OFFENDING LINES FROM YOUR LAST OUTPUT:\n"
                                + "\n".join(_line_samples) + "\n"
                            )

                    _error_feedback = (
                        "## PREVIOUS GENERATION REJECTED — UNICODE CORRUPTION\n\n"
                        f"{_err_str[:400]}\n"
                        f"{_offending_block}\n"
                        "INSTRUCTIONS FOR RETRY:\n"
                        "- The lines above contain Unicode LETTERS that look like\n"
                        "  ASCII letters but aren't. These are HARD FAILURES — the\n"
                        "  Iron Gate auto-heals punctuation (em-dash, curly quotes,\n"
                        "  ellipsis, nbsp) but it will NEVER auto-heal letters\n"
                        "  because changing a letter changes the identity of a\n"
                        "  package, function, or variable.\n"
                        "- Re-emit the ENTIRE file using only 7-bit ASCII (0x20–0x7E)\n"
                        "  for every character. Every byte must satisfy ord(c) < 128.\n"
                        "- Common culprits in package manifests (requirements.txt,\n"
                        "  package.json, pyproject.toml, Pipfile):\n"
                        "    * U+0641 Arabic FEH 'ف' looks like ASCII 'f'\n"
                        "    * U+0430 Cyrillic 'а' looks like ASCII 'a'\n"
                        "    * U+0435 Cyrillic 'е' looks like ASCII 'e'\n"
                        "    * U+03BF Greek omicron 'ο' looks like ASCII 'o'\n"
                        "  If you're about to write 'rapidfuzz', type r-a-p-i-d-f-u-z-z\n"
                        "  using ONLY characters from the ASCII table. Do not rely on\n"
                        "  memory of what the package name 'looks like'.\n"
                        "- Sanity check: every single character in your output must\n"
                        "  be in the range 0x20–0x7E or \\n (0x0A). No exceptions.\n"
                    )
                elif _err_str.startswith("multi_file_coverage_insufficient"):
                    # Gate 5 rejection — name the missing target paths and
                    # reiterate the files: [...] shape. The model saw the
                    # single-file schema example in its prompt; here we
                    # hand it the multi-file example plus the exact list
                    # of paths it failed to cover.
                    _mf_missing = getattr(exc, "_mf_missing_paths", None) or []
                    _mf_targets = getattr(exc, "_mf_target_files", None) or tuple(
                        ctx.target_files
                    )
                    try:
                        from backend.core.ouroboros.governance.multi_file_coverage_gate import (
                            render_missing_block as _mf_render,
                        )
                        _missing_block = _mf_render(_mf_missing, _mf_targets)
                    except Exception:  # noqa: BLE001
                        _missing_block = (
                            "\nMISSING TARGET FILES:\n"
                            + "\n".join(f"  - {p}" for p in list(_mf_missing)[:16])
                            + "\n"
                        )
                    _target_count = len(_mf_targets)
                    _error_feedback = (
                        "## PREVIOUS GENERATION REJECTED — "
                        "MULTI-FILE COVERAGE INSUFFICIENT\n\n"
                        f"{_err_str[:400]}\n"
                        f"{_missing_block}\n"
                        "INSTRUCTIONS FOR RETRY:\n"
                        f"- This operation targets {_target_count} files. "
                        "You MUST return the multi-file shape: a `files` "
                        "list with one entry per target file.\n"
                        "- Do NOT use the legacy single-file schema "
                        "(`file_path` + `full_content` at the top level of "
                        "the candidate). That shape can only express ONE "
                        "file and will be rejected again.\n"
                        "- Use this structure for each candidate:\n\n"
                        "    {\n"
                        "      \"candidate_id\": \"c1\",\n"
                        "      \"files\": [\n"
                        "        {\n"
                        "          \"file_path\": \"<target path 1>\",\n"
                        "          \"full_content\": \"<complete file 1 content>\",\n"
                        "          \"rationale\": \"<why file 1 changes>\"\n"
                        "        },\n"
                        "        {\n"
                        "          \"file_path\": \"<target path 2>\",\n"
                        "          \"full_content\": \"<complete file 2 content>\",\n"
                        "          \"rationale\": \"<why file 2 changes>\"\n"
                        "        }\n"
                        "      ],\n"
                        "      \"rationale\": \"<one-sentence summary of the change set>\"\n"
                        "    }\n\n"
                        f"- Every one of the {_target_count} target paths above "
                        "must appear as a `file_path` entry in the `files` "
                        "list. Do not omit any.\n"
                        "- `full_content` in each entry must be the COMPLETE "
                        "file (not a diff, not a patch, not just the changed "
                        "lines).\n"
                        "- Python files must be syntactically valid "
                        "(`ast.parse()`-clean) per file.\n"
                    )
                elif _err_str.startswith("Dependency file rename/truncation suspected"):
                    # Gate 3 rejection — show the offender pairs and a clear
                    # rule: you are NOT allowed to rename/shorten an existing
                    # package name, only add new ones or bump versions.
                    _dep_offenders = getattr(exc, "_dep_file_offenders", None) or []
                    _dep_rejected = getattr(exc, "_dep_file_rejected_content", "") or ""
                    _offender_block = ""
                    if _dep_offenders:
                        _offender_lines = "\n".join(
                            f"  {i + 1}. {pair}" for i, pair in enumerate(_dep_offenders[:10])
                        )
                        _offender_block = (
                            "\nSUSPICIOUS RENAMES DETECTED:\n"
                            f"{_offender_lines}\n"
                        )
                    _error_feedback = (
                        "## PREVIOUS GENERATION REJECTED — DEPENDENCY FILE CORRUPTION\n\n"
                        f"{_err_str[:400]}\n"
                        f"{_offender_block}\n"
                        "INSTRUCTIONS FOR RETRY:\n"
                        "- You deleted existing package(s) and added a near-identical\n"
                        "  new name. This is almost always a typo or hallucination —\n"
                        "  real upgrades change only the VERSION, not the package name.\n"
                        "- If the goal is to UPGRADE a package: keep the name identical\n"
                        "  (e.g. `anthropic==0.75.0` → `anthropic==0.80.0`). NEVER change\n"
                        "  the letters of the package name.\n"
                        "- If you truly need to REPLACE a package with a different one,\n"
                        "  the new name must be clearly distinct (not a substring or\n"
                        "  truncation of the old name) AND the reason must be in the\n"
                        "  `rationale` field of your candidate.\n"
                        "- Common hallucination patterns to avoid:\n"
                        "    * truncation: `rapidfuzz` → `rapidfu` (WRONG)\n"
                        "    * suffix append: `anthropic` → `anthropichttp` (WRONG)\n"
                        "    * single-char typo: `requests` → `reqest` (WRONG)\n"
                        "- Before emitting, compare each package name against the\n"
                        "  source file character-by-character. Every name that was\n"
                        "  there must still be there with the exact same spelling.\n"
                    )
                else:
                    _error_feedback = (
                        "## PREVIOUS GENERATION FAILED\n\n"
                        f"Error: {_err_str[:300]}\n\n"
                        "INSTRUCTIONS FOR RETRY:\n"
                        "- Return schema_version '2b.1' with 'full_content' containing the COMPLETE file\n"
                        "- Do NOT return unified diffs or patches\n"
                        "- Ensure the JSON is valid (no trailing commas, no unquoted keys)\n"
                        "- full_content must be the entire file, not a summary or placeholder\n"
                    )
                _retry_ctx_kwargs["strategic_memory_prompt"] = _error_feedback

                # Record generation failure in episodic memory for downstream use
                if _episodic_memory is not None:
                    _gen_failure_class = "content"
                    if "exploration_insufficient" in _err_str:
                        _gen_failure_class = "exploration"
                    elif "ascii_corruption" in _err_str:
                        _gen_failure_class = "ascii"
                    elif _err_str.startswith("multi_file_coverage_insufficient"):
                        _gen_failure_class = "multi_file_coverage"
                    elif _err_str.startswith("Dependency file rename/truncation"):
                        _gen_failure_class = "dep_file_rename"
                    elif "json_parse_error" in _err_str:
                        _gen_failure_class = "json_parse"
                    elif "diff_apply_failed" in _err_str:
                        _gen_failure_class = "diff_apply"
                    elif "schema_invalid" in _err_str:
                        _gen_failure_class = "schema"
                    try:
                        _episodic_memory.record(
                            file_path=list(ctx.target_files)[0] if ctx.target_files else "unknown",
                            attempt=attempt + 1,
                            failure_class=_gen_failure_class,
                            error_summary=_err_str[:500],
                            specific_errors=[_err_str[:200]],
                            line_numbers=[],
                        )
                    except Exception:
                        pass

                # Inject re-plan if available (appends to error feedback)
                if _replan_text:
                    _existing = _retry_ctx_kwargs.get("strategic_memory_prompt", "")
                    _retry_ctx_kwargs["strategic_memory_prompt"] = (
                        f"{_existing}\n\n{_replan_text}" if _existing else _replan_text
                    )

                if _episodic_memory is not None and _episodic_memory.has_failures():
                    _failure_context = _episodic_memory.format_for_prompt()
                    if _failure_context:
                        # Preserve iron-gate feedback already staged for retry
                        # (ExplorationInsufficientError etc). Reading from ctx
                        # here would silently drop _error_feedback — the
                        # severed nervous system bug that hid category-aware
                        # retry instructions from the model on every
                        # post-Iron-Gate retry.
                        _existing = _retry_ctx_kwargs.get("strategic_memory_prompt", "") or ""
                        _retry_ctx_kwargs["strategic_memory_prompt"] = (
                            f"{_existing}\n\n{_failure_context}" if _existing else _failure_context
                        )
                        logger.info(
                            "[Orchestrator] Injecting %d episodic failure(s) into retry context [%s]",
                            _episodic_memory.total_episodes, ctx.op_id,
                        )
                # Inject consciousness fragile-file memory into retry context
                if _consciousness_bridge is not None:
                    try:
                        _fragile_ctx = _consciousness_bridge.get_fragile_file_context(
                            ctx.target_files
                        )
                        if _fragile_ctx:
                            _existing_mem = _retry_ctx_kwargs.get("strategic_memory_prompt", "")
                            _retry_ctx_kwargs["strategic_memory_prompt"] = (
                                f"{_existing_mem}\n\n{_fragile_ctx}" if _existing_mem else _fragile_ctx
                            )
                    except Exception:
                        pass
                ctx = ctx.advance(OperationPhase.GENERATE_RETRY, **_retry_ctx_kwargs)

        assert generation is not None  # guaranteed by loop logic

        # L1: emit tool execution audit records to ledger stream.
        # This runs BEFORE the noop guard so that tool records are always
        # persisted regardless of whether the response was a noop.
        for _rec in generation.tool_execution_records:
            try:
                _entry = LedgerEntry(
                    op_id=ctx.op_id,
                    state=OperationState.SANDBOXING,
                    data={"kind": "tool_exec.v1", **_dc_asdict(_rec)},
                    entry_id=_rec.call_id,
                )
                await orch._stack.ledger.append(_entry)
            except asyncio.CancelledError:
                raise
            except Exception as _exc:  # noqa: BLE001
                logger.warning(
                    "tool_exec ledger emit failed op=%s record=%s: %s",
                    ctx.op_id, getattr(_rec, "call_id", "?"), _exc,
                )  # ledger failure must never abort governance pipeline

        # Short-circuit: model signalled the change is already present.
        #
        # Read-only discipline (Session 10, Derek 2026-04-17 Manifesto §8):
        # when ctx.is_read_only=True the noop short-circuit represents the
        # structurally expected terminal state (findings delivered via
        # subagent rollup, no code change by contract). Emit a POSTMORTEM
        # event with root_cause="read_only_complete" so the Synthetic Soul
        # has a clean audit trail and post-hoc analysis can distinguish
        # cartography completions from "model said no-op" completions.
        # Terminal reason code + ledger reason are aligned to the same
        # value so log, ledger, and comm-protocol all agree.
        if generation.is_noop:
            _is_read_only_terminal = bool(
                getattr(ctx, "is_read_only", False)
            )
            _terminal_reason = (
                "read_only_complete"
                if _is_read_only_terminal
                else "noop"
            )
            logger.info(
                "[Orchestrator] op=%s is_noop=True (provider=%s) "
                "terminal_reason_code=%s — skipping APPLY",
                ctx.op_id,
                generation.provider_name,
                _terminal_reason,
            )
            # POSTMORTEM emission for read-only ops (Manifesto §8).
            # Emitted BEFORE ctx.advance so the audit trail matches
            # the lifecycle: GENERATE → (synthesis produced findings)
            # → POSTMORTEM → COMPLETE. Non-read-only noop ops retain
            # the legacy silent-complete semantics (no POSTMORTEM) to
            # preserve backward compatibility with existing analytics
            # that treat noop as a null event.
            if _is_read_only_terminal:
                try:
                    await orch._stack.comm.emit_postmortem(
                        op_id=ctx.op_id,
                        root_cause="read_only_complete",
                        failed_phase=None,
                        next_safe_action="none",
                    )
                except Exception:
                    logger.debug(
                        "[Orchestrator] read-only POSTMORTEM emit failed",
                        exc_info=True,
                    )
            ctx = ctx.advance(
                OperationPhase.COMPLETE,
                generation=generation,
                terminal_reason_code=_terminal_reason,
            )
            await orch._record_ledger(
                ctx,
                OperationState.APPLIED,
                {
                    "reason": _terminal_reason,
                    "provider": generation.provider_name,
                },
            )
            return PhaseResult(

                next_ctx=ctx, next_phase=None, status="fail",

                reason=ctx.terminal_reason_code or "generate_terminal",

                artifacts={"generation": generation, "episodic_memory": _episodic_memory},

            )
        # ---- end verbatim transcription ----

        # Success path: generation produced candidates; advance to VALIDATE.
        # generation local is guaranteed non-None here (is_noop break or
        # candidates present). Thread it + episodic memory + retries count
        # via artifacts. VALIDATERunner reads generate_retries_remaining
        # for entropy computation (inline GENERATE mutated it across attempts).
        return PhaseResult(
            next_ctx=ctx,
            next_phase=OperationPhase.VALIDATE,
            status="ok",
            reason="generated",
            artifacts={
                "generation": generation,
                "episodic_memory": _episodic_memory,
                "generate_retries_remaining": generate_retries_remaining,
            },
        )


__all__ = ["GENERATERunner"]
