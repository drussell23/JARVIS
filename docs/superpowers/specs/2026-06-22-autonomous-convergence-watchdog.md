# Autonomous Convergence Watchdog — Design Spec

> **Arc:** with AST-slicing + the egress interceptor live, the primary risk shifted from *network spam* to *local infinite recursion* — an AST block that can't be sliced smaller but is still too heavy re-decomposes forever. A trajectory-based, self-healing watchdog catches the stall and sheds weight structurally. No manual babysitting, no hardcoded retry caps.
> **Date:** 2026-06-22. **Branch:** `worktree-convergence-watchdog`. Feeds the recursive-chunking matrices ([[project_sovereign_resilience_chunking]]) + the egress interceptor ([[project_sovereign_egress_interceptor]]).

---

## 1. Diagnosis (reuse-first)
- **Weight ruler exists:** `goal_decomposition_planner.estimate_subgoal_payload_chars` / `dw_egress_interceptor.estimate_body_chars` — the watchdog measures with the SAME ruler the interceptor uses, so "fits the target" means the same thing everywhere.
- **Single funnel:** `orchestrator._decompose_block_or_legacy` (~2209) is the one place both the Advisor-BLOCK and the egress-overweight re-chunk call `decompose_for_block(compression_target=…)`. The watchdog hooks there.
- **No reduction tracking today.** Recursion is bounded by `recursion_dedup.AttemptLedger` (exact-repeat de-dup) + `adaptive_recursion_governor` (depth/fan-out) + the "irreducible symbol emitted anyway" log — but a near-irreducible-but-overweight block can re-decompose without reducing.
- **Telemetry:** `ide_observability_stream.publish_task_event(event_type, op_id, payload)` (SSE, best-effort) is the existing `[SOVEREIGN …]` event surface.

## 2. Goals / Non-Goals
**Goals.** (G1) Track per-pass **reduction trajectory** (largest child chars / parent chars) — NOT a retry count. (G2) Detect a **stall** = `ratio ≥ JARVIS_WATCHDOG_STALL_RATIO (0.95)` for `JARVIS_WATCHDOG_STALL_PASSES (2)` consecutive passes (env tunes the curve; no magic numbers). (G3) On stall, **shed weight structurally** (the Epistemic Weight-Shedder, tiered) to fit `compression_target` — never brute-force-chop, never re-spin, never crash. (G4) Emit a `[SOVEREIGN YIELD]` event (SSE + stdout WARNING) with the trajectory math + tier reached. (G5) Reuse the weight ruler, the bounded-FIFO ledger pattern, the telemetry surface, the decompose funnel — no parallel state.

**Non-Goals.** No model call from the watchdog (it must not itself produce DW egress). No new dispatch queue/FSM. No change to the Advisor/cage. The de-dup ledger + governor remain the backstop.

## 3. Reuse Inventory
| Need | Existing asset | Anchor |
|---|---|---|
| Weight ruler | `estimate_subgoal_payload_chars` / `dw_egress_interceptor.estimate_body_chars` | `goal_decomposition_planner.py:733`, `dw_egress_interceptor.py` |
| Decompose funnel (hook) | `orchestrator._decompose_block_or_legacy` after `decompose_for_block` | `orchestrator.py:~2209` |
| Bounded-FIFO ledger pattern | `recursion_dedup.AttemptLedger` (deque maxlen + singleton) | `recursion_dedup.py:57,119` |
| AST symbol extraction | `ast_symbol_scoper` (`ScopedTarget`, `isolate_symbols`) + stdlib `ast` | (merged) |
| Telemetry event surface | `ide_observability_stream.publish_task_event` | `ide_observability_stream.py:2247` |
| Sub-goal type | `goal_decomposition_planner.SubGoal` | `:331` |

## 4. Component Specs

### 4.1 `convergence_watchdog.py` (new pure leaf)
- `@dataclass(frozen=True) class WatchdogVerdict: stalled: bool; ratio: float; consecutive_stalls: int; passes: int`.
- `def stall_ratio_threshold() -> float` (`JARVIS_WATCHDOG_STALL_RATIO`, default 0.95) + `def stall_passes_threshold() -> int` (`JARVIS_WATCHDOG_STALL_PASSES`, default 2) + `def watchdog_enabled() -> bool` (`JARVIS_CONVERGENCE_WATCHDOG_ENABLED`, **default true** — pure-safety self-heal, only fires on a stall, fail-soft).
- `class ReductionTracker`: bounded per-lineage map (lineage_id → bounded deque of recent ratios; reuses the FIFO/singleton pattern, `JARVIS_WATCHDOG_TRACKER_SIZE` default 256 lineages). `record_pass(lineage_id, parent_chars, max_child_chars) -> WatchdogVerdict`: `ratio = max_child_chars / max(1, parent_chars)`; append; `consecutive_stalls` = trailing run of ratios ≥ threshold; `stalled = consecutive_stalls >= passes_threshold`. Pure, fail-soft (bad input → `WatchdogVerdict(stalled=False, …)`, never raises). `get_reduction_tracker()` singleton. `lineage_id` = the root goal_id of the recursive chain (so the trajectory follows the lineage, not a single op).

### 4.2 Epistemic Weight-Shedder (`epistemic_shedder.py`, pure AST + deterministic — NO model)
`def shed_to_fit(source: str, target_chars: int) -> tuple[str, str]` returns `(shed_source, tier_reached)`. Applies tiers in order, re-measuring with `estimate_body_chars` after each; STOPS at the first tier that fits:
- **Tier 1 — Fluff:** `ast.parse` → remove all docstrings (first `Expr(Constant(str))` in every module/class/func body) → `ast.unparse` (comments are not in the AST, so they drop automatically). Measure.
- **Tier 2 — Deep Implementations:** if still over, for the HEAVIEST symbols (by `estimate_body_chars` of their source segment), replace each `FunctionDef`/`AsyncFunctionDef`/`ClassDef` body with a single placeholder `Constant("[SOVEREIGN YIELD: Implementation Omitted]")` (or `Pass`), KEEPING the signature (name, args, decorators, returns). Heaviest-first until it fits. Measure.
- **Tier 3 — Nuclear truncation:** if STILL over, strict chronological truncation of the heaviest remaining source items until ≤ `target_chars`. Measure.
- Pure AST/string only (NEVER exec/eval/compile-exec). Fail-soft: any parse error → fall straight to Tier-3 truncation of the raw source (always returns something ≤ target or the best effort). Returns the tier label for telemetry.

### 4.3 `[SOVEREIGN YIELD]` telemetry
`def emit_sovereign_yield(op_id, *, lineage_id, ratio, consecutive_stalls, parent_chars, child_chars, tier) -> None`: stdout `logger.warning("[SOVEREIGN YIELD] op=%s lineage=%s stalled reduction ratio=%.3f passes=%d -> structural weight-shed (tier=%s) parent=%d child=%d", …)` + best-effort `publish_task_event("sovereign_yield", op_id, {…})` (SSE). Fail-soft, never raises.

### 4.4 Wiring into the decompose funnel
In `orchestrator._decompose_block_or_legacy`, after `decompose_for_block(... compression_target=…)` produces `_all_subs` (and only when `watchdog_enabled()` AND a `compression_target` is set — i.e. the egress-overweight re-chunk path):
1. `parent_chars = estimate_subgoal_payload_chars(goal)`; `max_child = max((estimate_subgoal_payload_chars(s) for s in _all_subs), default=0)`.
2. `verdict = get_reduction_tracker().record_pass(lineage_id=<root goal id>, parent_chars, max_child)`.
3. If `verdict.stalled`: for the over-target sub-goal(s), `shed_to_fit(<symbol source>, compression_target)` → emit ONE fitting sub-goal carrying the shed payload (in its description/evidence); `emit_sovereign_yield(...)`; use that in place of the non-shrinking slice. Else proceed with `_all_subs`.
4. Fail-soft: any watchdog error → the legacy slice path (the de-dup ledger remains the backstop). OFF (`watchdog_enabled()` false) → byte-identical (legacy slice).

## 5. Cross-cutting
- **Invariants.** (I1) No infinite recursion: a stalled lineage YIELDS (sheds + emits a fitting sub-goal) rather than re-slicing — bounded by the trajectory, not a hardcoded count. (I2) Zero watchdog egress: the shedder is pure-deterministic (no model call) — it can never spam DW. (I3) Self-healing: stall → yield → fitting sub-goal, zero human action, `[SOVEREIGN YIELD]` observable. (I4) Fail-soft: any watchdog/shedder error → legacy path; never crashes a dispatch. (I5) Reuse-first — ruler, FIFO pattern, telemetry, decompose funnel; no parallel state. (I6) Pure AST in the shedder (no exec/eval).
- **No hardcoding:** ratio (0.95) + passes (2) + tracker size from env. No magic-number retry cap anywhere.

## 6. Test strategy
- Unit: `ReductionTracker` (ratio math; stall after N consecutive ≥threshold; resets on a good pass; bounded; fail-soft). Shedder Tier 1 (docstrings/comments gone, still parseable), Tier 2 (heaviest bodies → placeholder, signatures kept, fits), Tier 3 (truncates to ≤ target), parse-error→Tier-3 fallback, NEVER exec, each tier re-measured. `emit_sovereign_yield` (stdout + SSE best-effort, fail-soft). Env thresholds.
- Interaction: a synthetic irreducible-but-overweight lineage → 2 stalled passes → watchdog yields a fitting sub-goal + emits `[SOVEREIGN YIELD]` (no further re-slice). A genuinely-reducible lineage → no stall, normal slicing. OFF byte-identical.
- **Static validation (operator gate):** a stalled lineage provably converges to a ≤target sub-goal locally (no infinite loop, no DW egress) BEFORE the C2 soak.

## 7. Phasing
1. `convergence_watchdog.py` (tracker + verdict + thresholds + telemetry) + tests. 2. `epistemic_shedder.py` (tiered, pure AST) + tests. 3. Wire into `_decompose_block_or_legacy` + interaction test. 4. Integration + static validation + final cross-cutting review (confirm the hook is on the LIVE funnel). Then the operator-authorized C2 soak.
