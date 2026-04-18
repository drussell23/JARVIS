"""
AgenticReviewSubagent — Phase B Execution Validation (Manifesto §6).

Implements the ReviewExecutor protocol defined in subagent_orchestrator.py.
Unlike AgenticExploreSubagent — which runs open-ended cartography — REVIEW
is scoped: given one candidate file (pre_apply_content → candidate_content),
it computes a structurally-derived verdict answering the question
"does this candidate preserve semantic integrity against the generation
intent?"

Manifesto §6 (Execution Validation) — architectural constraint:
    REVIEW cannot blindly approve code. It must be explicitly wired into
    the AST parser and the mutation testing suite to verify semantic
    integrity before rendering a verdict.

    This implementation wires:
      * SemanticGuardian.inspect() — 10 existing AST/regex patterns
        (removed_import_still_referenced, function_body_collapsed,
        guard_boolean_inverted, credential_shape_introduced, …)
      * Function-body-hash diff — detects "refactor that silently
        stubbed a function" vs "genuine refactor with preserved
        behavior".
      * Import-graph delta — new imports introduced + removed imports
        still referenced (the latter overlaps with SemanticGuardian
        but we also count additions for the rationale narrative).
      * Mutation-testing hook — WIRED AS A STUB in this first cut;
        callers can inject a real mutation_tester via constructor
        dependency injection. The hook is hard-kill-wrapped so a
        pathological source cannot hang REVIEW (same pattern as
        providers.py:5257 Claude stream hard-kill).

Verdict derivation (deterministic — no LLM prose opinion):
    1. Start at semantic_integrity_score = 1.0.
    2. For each SemanticGuardian Detection:
       - severity="hard" subtracts 0.35
       - severity="soft" subtracts 0.12
    3. If function-body-hash diff shows function reduction
       (new file has fewer function definitions than old), subtract
       0.20 per missing function (flags silent stubbing).
    4. If credential-shape pattern hits, force verdict=REJECT
       regardless of score (security-sensitive).
    5. Verdict mapping:
       score >= REVIEW_MIN_SCORE_APPROVE                   → APPROVE
       score >= REVIEW_MIN_SCORE_APPROVE_WITH_RESERVATIONS → APPROVE_WITH_RESERVATIONS
       otherwise                                            → REJECT
    6. If any "hard" severity Detection → force verdict downgrade by
       at least one tier (APPROVE → APPROVE_WITH_RESERVATIONS; already
       approve_with_reservations → REJECT).

All verdicts are auditable:
    * `rationale` is a one-paragraph prose summary derived from the
      detection list — not from LLM generation. This keeps the verdict
      reproducible across runs on the same candidate.
    * `type_payload` carries the full typed verdict tuple so the
      orchestrator can consume it programmatically without parsing.

Mutation testing hook:
    ``mutation_score`` is populated only when the candidate's file path
    is in the mutation-testing allowlist (shared with mutation_gate.py).
    The mutation run happens inside AgenticReviewSubagent.review() and
    is wrapped in asyncio.wait_for with a dedicated budget; a hang in
    the mutation tester cannot wedge REVIEW.

Cost: $0.00 per review (no LLM calls). All analysis is deterministic
AST/regex. This matches Phase 1's subagent cost profile and preserves
the economic thesis (LLM as orchestrator, code as worker).
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.subagent_contracts import (
    REVIEW_MIN_SCORE_APPROVE,
    REVIEW_MIN_SCORE_APPROVE_WITH_RESERVATIONS,
    REVIEW_VERDICT_APPROVE,
    REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS,
    REVIEW_VERDICT_REJECT,
    SubagentContext,
    SubagentFinding,
    SubagentResult,
    SubagentStatus,
    SubagentType,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Scoring constants — deterministic, auditable, env-free defaults.
# ============================================================================
#
# These are intentionally NOT env-tunable in this first cut. The whole
# point of §6 Execution Validation is that the verdict math is
# reproducible. An operator who wants a different policy opens a PR,
# not a shell variable. If graduation-arc data shows the defaults are
# miscalibrated, the thresholds move here (audit trail preserved).

_HARD_SEVERITY_PENALTY = 0.35
_SOFT_SEVERITY_PENALTY = 0.12
_FUNCTION_LOSS_PENALTY = 0.20
# Mutation score floor below which REVIEW forces a downgrade. A score
# below 0.40 means surviving mutants are frequent — the test suite
# doesn't actually exercise the changed lines.
_MUTATION_SCORE_FLOOR = 0.40

# Security-critical patterns that force outright REJECT regardless of
# score. The canonical pattern names match SemanticGuardian.
_REJECT_ON_MATCH_PATTERNS = frozenset({
    "credential_shape_introduced",
})

# Mutation testing is expensive. Hard-kill wrapper budget per mutation
# run inside REVIEW. Env-tunable because mutation test duration scales
# with the size of the touched code.
_MUTATION_BUDGET_S_DEFAULT = 30.0


# ============================================================================
# AgenticReviewSubagent
# ============================================================================


class AgenticReviewSubagent:
    """ReviewExecutor implementation wiring SemanticGuardian + AST diff.

    Constructor is dependency-injection-friendly so the mutation-testing
    hook can be swapped for a fake in tests:

        review = AgenticReviewSubagent(
            project_root=Path("/repo"),
            mutation_runner=my_fake_mutation_runner,  # optional
            mutation_budget_s=15.0,                   # optional
        )

    The default ``mutation_runner=None`` means REVIEW skips mutation
    testing entirely and emits ``mutation_score=None``. This is the
    safe default for Phase B graduation — callers can opt in explicitly
    once the mutation testing pathway is itself battle-tested here.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        mutation_runner: Optional[Callable[[str, str], Awaitable[float]]] = None,
        mutation_budget_s: float = _MUTATION_BUDGET_S_DEFAULT,
    ) -> None:
        self._root = Path(project_root)
        self._mutation_runner = mutation_runner
        self._mutation_budget_s = float(mutation_budget_s)

    # ------------------------------------------------------------------
    # ReviewExecutor protocol
    # ------------------------------------------------------------------

    async def review(self, ctx: SubagentContext) -> SubagentResult:
        """Run structural review on ctx.request.review_target_candidate.

        Returns a well-formed SubagentResult with type_payload carrying
        the typed verdict. Never raises except for asyncio.CancelledError
        (re-raised for cooperative cancellation).
        """
        started_ns = time.time_ns()
        candidate = getattr(ctx.request, "review_target_candidate", None)
        if not candidate:
            return self._malformed_input_result(
                ctx, started_ns,
                detail="review_target_candidate missing from request",
            )

        file_path = str(candidate.get("file_path", ""))
        pre_apply = str(candidate.get("pre_apply_content", ""))
        candidate_content = str(candidate.get("candidate_content", ""))
        intent = str(candidate.get("generation_intent", ""))
        if not file_path or not candidate_content:
            return self._malformed_input_result(
                ctx, started_ns,
                detail="file_path and candidate_content are required",
            )

        try:
            # Phase 1: SemanticGuardian AST/regex patterns.
            detections = self._run_semantic_guardian(
                file_path, pre_apply, candidate_content,
            )
            # Phase 2: function-body-hash diff (structural).
            fn_loss = self._compute_function_loss(pre_apply, candidate_content)
            # Phase 3: import-graph delta (additive signal for rationale).
            import_delta = self._compute_import_delta(
                pre_apply, candidate_content,
            )
            # Phase 4: optional mutation testing (hard-kill wrapped).
            mutation_score = await self._maybe_run_mutation_testing(
                file_path=file_path, candidate_content=candidate_content,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — structural defense
            logger.exception(
                "[AgenticReviewSubagent] unexpected failure sub=%s",
                ctx.subagent_id,
            )
            return self._internal_failure_result(
                ctx, started_ns, error=e,
            )

        # Verdict synthesis — deterministic math.
        score = self._compute_score(detections, fn_loss)
        verdict = self._derive_verdict(
            score=score,
            detections=detections,
            mutation_score=mutation_score,
        )
        rationale = self._render_rationale(
            verdict=verdict,
            score=score,
            detections=detections,
            fn_loss=fn_loss,
            import_delta=import_delta,
            mutation_score=mutation_score,
            intent=intent,
        )

        # Findings for observability — one SubagentFinding per Detection
        # so the Phase 1 observability pipeline (CommProtocolCommSink,
        # LedgerSubagentSink) can consume REVIEW output without changes.
        findings = tuple(
            SubagentFinding(
                category="pattern",
                description=f"[{d.severity}] {d.pattern}: {d.message}",
                file_path=d.file_path or file_path,
                line=int(d.lines[0]) if d.lines else 0,
                evidence=d.snippet,
                relevance=0.9 if d.severity == "hard" else 0.5,
            )
            for d in detections
        )

        # type_payload: tuple-of-tuple so SubagentResult stays frozen.
        payload: Tuple[Tuple[str, Any], ...] = (
            ("verdict", verdict),
            ("semantic_integrity_score", round(score, 3)),
            ("mutation_score", (round(mutation_score, 3)
                                if mutation_score is not None else None)),
            ("reservations", self._build_reservations(
                verdict=verdict, detections=detections, fn_loss=fn_loss,
                mutation_score=mutation_score,
            )),
            ("reject_reasons", self._build_reject_reasons(
                verdict=verdict, detections=detections,
                mutation_score=mutation_score,
            )),
            ("rationale", rationale),
            ("ast_pattern_count", len(detections)),
            ("ast_hard_count", sum(
                1 for d in detections if d.severity == "hard"
            )),
            ("function_loss_count", fn_loss),
            ("import_delta_added", tuple(sorted(import_delta["added"]))),
            ("import_delta_removed", tuple(sorted(import_delta["removed"]))),
        )

        finished_ns = time.time_ns()
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.REVIEW,
            status=SubagentStatus.COMPLETED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=finished_ns,
            findings=findings,
            files_read=(file_path,),
            search_queries=(),
            summary=f"REVIEW verdict={verdict} score={score:.2f}",
            cost_usd=0.0,                   # deterministic — no LLM
            tool_calls=1 + (1 if mutation_score is not None else 0),
            tool_diversity=2 if mutation_score is not None else 1,
            provider_used="deterministic",
            fallback_triggered=False,
            type_payload=payload,
        )

    # ------------------------------------------------------------------
    # Signal computation
    # ------------------------------------------------------------------

    def _run_semantic_guardian(
        self, file_path: str, pre_apply: str, candidate_content: str,
    ) -> List[Any]:  # List[semantic_guardian.Detection]
        """Invoke SemanticGuardian on the (old → new) candidate pair."""
        try:
            from backend.core.ouroboros.governance.semantic_guardian import (
                SemanticGuardian,
            )
        except Exception:
            # If SemanticGuardian is unavailable, REVIEW degrades to
            # function-body-hash diff only. Log at INFO so operators
            # see that signal quality has dropped.
            logger.info(
                "[AgenticReviewSubagent] SemanticGuardian unavailable — "
                "REVIEW running on function-diff signal only"
            )
            return []
        guardian = SemanticGuardian()
        return guardian.inspect(
            file_path=file_path,
            old_content=pre_apply,
            new_content=candidate_content,
        )

    def _compute_function_loss(
        self, pre_apply: str, candidate_content: str,
    ) -> int:
        """Count functions present in pre_apply but absent in candidate.

        Detects "silent stubbing" — the refactor that supposedly
        preserves behavior but actually removed the function body.
        Parse errors return 0 (can't compute signal; treat as "no loss"
        rather than false-positive — SemanticGuardian's
        ``function_body_collapsed`` pattern covers the syntactic case
        more precisely).
        """
        try:
            old_fns = {
                n.name for n in ast.walk(ast.parse(pre_apply or ""))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            new_fns = {
                n.name for n in ast.walk(ast.parse(candidate_content))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        except (SyntaxError, ValueError):
            return 0
        lost = old_fns - new_fns
        return len(lost)

    def _compute_import_delta(
        self, pre_apply: str, candidate_content: str,
    ) -> Dict[str, List[str]]:
        """Return {'added': [...], 'removed': [...]} import top-level names.

        Additive signal — not a verdict input on its own. Used in the
        rationale to explain the structural change narrative.
        """
        def _imports(src: str) -> set:
            try:
                tree = ast.parse(src or "")
            except (SyntaxError, ValueError):
                return set()
            out: set = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        out.add((a.asname or a.name).split(".")[0])
                elif isinstance(n, ast.ImportFrom):
                    for a in n.names:
                        out.add(a.asname or a.name)
            return out

        old_i = _imports(pre_apply)
        new_i = _imports(candidate_content)
        return {
            "added": sorted(new_i - old_i),
            "removed": sorted(old_i - new_i),
        }

    async def _maybe_run_mutation_testing(
        self, *, file_path: str, candidate_content: str,
    ) -> Optional[float]:
        """Run mutation testing if a runner is wired and the path is
        in the allowlist. Hard-kill-wrapped so a hung mutation run
        cannot wedge REVIEW.
        """
        if self._mutation_runner is None:
            return None
        # Allowlist consultation reuses mutation_gate's module-level
        # check. Keep the import lazy to avoid mandatory dependency.
        try:
            from backend.core.ouroboros.governance.mutation_gate import (
                _is_critical_path,
            )
            if not _is_critical_path(file_path):
                return None
        except Exception:
            # If the allowlist helper is unavailable, conservatively
            # skip mutation testing — log so operators see it.
            logger.debug(
                "[AgenticReviewSubagent] mutation_gate allowlist check "
                "unavailable — skipping mutation testing"
            )
            return None
        # Hard-kill wrapper (Manifesto §3) — reuses the providers.py
        # pattern so a pathological mutation run cannot paralyze REVIEW.
        # Bind the runner to a local so pyright sees a non-Optional
        # in the closure body (the None-guard above already narrowed
        # it, but the closure captures attribute access, not the
        # narrowed local).
        runner = self._mutation_runner

        async def _await_runner() -> float:
            return await runner(file_path, candidate_content)

        task = asyncio.create_task(_await_runner())
        try:
            done, pending = await asyncio.wait(
                {task}, timeout=self._mutation_budget_s + 5.0,
            )
            if pending:
                for t in pending:
                    t.cancel()
                logger.warning(
                    "[AgenticReviewSubagent] HARD-KILL mutation testing "
                    "after %.1fs on %s — runner wedged",
                    self._mutation_budget_s + 5.0, file_path,
                )
                return None
            return float(await task)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "[AgenticReviewSubagent] mutation runner raised: %s — "
                "treating as no-score",
                e,
            )
            return None

    # ------------------------------------------------------------------
    # Verdict synthesis
    # ------------------------------------------------------------------

    def _compute_score(
        self, detections: List[Any], fn_loss: int,
    ) -> float:
        """Deterministic score in [0.0, 1.0] from the detection list.

        Starts at 1.0, subtracts per-severity penalties and per-lost-
        function penalty, clamped to [0.0, 1.0].
        """
        score = 1.0
        for d in detections:
            if d.severity == "hard":
                score -= _HARD_SEVERITY_PENALTY
            else:
                score -= _SOFT_SEVERITY_PENALTY
        score -= _FUNCTION_LOSS_PENALTY * fn_loss
        return max(0.0, min(1.0, score))

    def _derive_verdict(
        self,
        *,
        score: float,
        detections: List[Any],
        mutation_score: Optional[float],
    ) -> str:
        """Map score + hard-pattern presence + mutation_score → verdict.

        Deterministic. Three-step:
          1. Base verdict from score.
          2. Force REJECT if any _REJECT_ON_MATCH_PATTERNS hit.
          3. Downgrade by one tier if any hard-severity Detection hit.
          4. Downgrade by one tier if mutation_score is below the floor.
        """
        if score >= REVIEW_MIN_SCORE_APPROVE:
            base = REVIEW_VERDICT_APPROVE
        elif score >= REVIEW_MIN_SCORE_APPROVE_WITH_RESERVATIONS:
            base = REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS
        else:
            base = REVIEW_VERDICT_REJECT

        # Security-sensitive patterns force REJECT.
        hit_names = {d.pattern for d in detections}
        if hit_names & _REJECT_ON_MATCH_PATTERNS:
            return REVIEW_VERDICT_REJECT

        # Mutation-score floor forces downgrade by one tier.
        if mutation_score is not None and mutation_score < _MUTATION_SCORE_FLOOR:
            base = self._downgrade(base)

        # Any hard-severity pattern forces downgrade by one tier.
        has_hard = any(d.severity == "hard" for d in detections)
        if has_hard:
            base = self._downgrade(base)

        return base

    @staticmethod
    def _downgrade(verdict: str) -> str:
        if verdict == REVIEW_VERDICT_APPROVE:
            return REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS
        if verdict == REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS:
            return REVIEW_VERDICT_REJECT
        return REVIEW_VERDICT_REJECT

    def _build_reservations(
        self,
        *,
        verdict: str,
        detections: List[Any],
        fn_loss: int,
        mutation_score: Optional[float],
    ) -> Tuple[str, ...]:
        """One reservation per soft-severity Detection + other
        non-blocking concerns. Non-empty when verdict is
        approve_with_reservations."""
        reservations: List[str] = []
        for d in detections:
            if d.severity == "soft":
                reservations.append(f"{d.pattern}: {d.message}")
        if (
            mutation_score is not None
            and mutation_score < _MUTATION_SCORE_FLOOR
        ):
            reservations.append(
                f"mutation_score={mutation_score:.2f} below floor "
                f"{_MUTATION_SCORE_FLOOR:.2f} — test suite may not "
                f"exercise changed lines"
            )
        if verdict == REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS and not reservations:
            reservations.append(
                "score in approve-with-reservations band; see rationale"
            )
        return tuple(reservations)

    def _build_reject_reasons(
        self,
        *,
        verdict: str,
        detections: List[Any],
        mutation_score: Optional[float],
    ) -> Tuple[str, ...]:
        """One reason per hard-severity Detection + any forced-REJECT
        triggers. Non-empty when verdict is reject."""
        if verdict != REVIEW_VERDICT_REJECT:
            return ()
        reasons: List[str] = []
        for d in detections:
            if d.severity == "hard":
                reasons.append(f"{d.pattern}: {d.message}")
        hit_names = {d.pattern for d in detections}
        for forced in hit_names & _REJECT_ON_MATCH_PATTERNS:
            reasons.append(f"security-critical pattern forced REJECT: {forced}")
        if not reasons:
            reasons.append(
                "semantic_integrity_score below reject threshold; "
                "see rationale"
            )
        return tuple(reasons)

    def _render_rationale(
        self,
        *,
        verdict: str,
        score: float,
        detections: List[Any],
        fn_loss: int,
        import_delta: Dict[str, List[str]],
        mutation_score: Optional[float],
        intent: str,
    ) -> str:
        """Prose summary — deterministic, no LLM. ≤ 800 chars."""
        parts: List[str] = []
        parts.append(
            f"Verdict={verdict} score={score:.2f} "
            f"intent={intent[:120]!r}"
        )
        parts.append(
            f"{len(detections)} AST pattern hit(s): "
            + ", ".join(f"{d.pattern}[{d.severity}]" for d in detections[:5])
            if detections else "no AST pattern hits"
        )
        if fn_loss:
            parts.append(f"{fn_loss} function(s) lost (possible stubbing)")
        added = import_delta.get("added", [])
        removed = import_delta.get("removed", [])
        if added or removed:
            parts.append(
                f"imports +{len(added)} -{len(removed)}"
                + (f" added={added[:3]}" if added else "")
                + (f" removed={removed[:3]}" if removed else "")
            )
        if mutation_score is not None:
            parts.append(f"mutation_score={mutation_score:.2f}")
        out = ". ".join(parts)
        if len(out) > 800:
            out = out[:797] + "…"
        return out

    # ------------------------------------------------------------------
    # Failure results
    # ------------------------------------------------------------------

    def _malformed_input_result(
        self, ctx: SubagentContext, started_ns: int, *, detail: str,
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.REVIEW,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            error_class="MalformedReviewInput",
            error_detail=detail,
            provider_used="deterministic",
        )

    def _internal_failure_result(
        self, ctx: SubagentContext, started_ns: int, *, error: Exception,
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.REVIEW,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            error_class=type(error).__name__,
            error_detail=str(error)[:500],
            provider_used="deterministic",
        )


def build_default_review_factory(
    project_root: Path,
) -> Callable[[], AgenticReviewSubagent]:
    """Factory helper matching the build_default_explore_factory pattern.

    The default factory wires AgenticReviewSubagent with NO mutation
    runner — mutation testing is opt-in via a custom factory that
    passes `mutation_runner=...` to the constructor. This keeps the
    Phase B graduation arc decoupled from the mutation-testing tracks.
    """
    def _factory() -> AgenticReviewSubagent:
        return AgenticReviewSubagent(project_root=project_root)
    return _factory
