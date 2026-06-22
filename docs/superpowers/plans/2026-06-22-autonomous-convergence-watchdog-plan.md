# Autonomous Convergence Watchdog — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** A trajectory-based, self-healing watchdog that detects a stalled recursive-decompose (a block that can't be sliced smaller but is still too heavy) and sheds weight structurally (tiered Epistemic Weight-Shedder) to fit — no infinite recursion, no hardcoded retry caps, no model call, no DW egress.

**Architecture:** Two pure leaf modules (`convergence_watchdog.py` reduction-trajectory tracker + telemetry; `epistemic_shedder.py` tiered pure-AST weight shedder) wired into the single decompose funnel `orchestrator._decompose_block_or_legacy`. Reuse the existing weight ruler, the bounded-FIFO ledger pattern, and the SSE telemetry surface.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`, stdlib `ast`), pytest. ASCII only.

**Spec (binding):** `docs/superpowers/specs/2026-06-22-autonomous-convergence-watchdog.md`.

## Global Constraints
- **No hardcoding:** `JARVIS_WATCHDOG_STALL_RATIO` (0.95), `JARVIS_WATCHDOG_STALL_PASSES` (2), `JARVIS_WATCHDOG_TRACKER_SIZE` (256) from env. No magic-number retry cap anywhere.
- **Pure AST shedder:** `ast.parse`/`ast.unparse`/`ast.get_source_segment` only — NEVER exec/eval/compile-exec.
- **Zero watchdog egress:** the shedder is deterministic (no model call) — it can never produce a DW request.
- **Fail-soft:** any watchdog/shedder error → the legacy slice path; never crashes a dispatch. Default-ON master `JARVIS_CONVERGENCE_WATCHDOG_ENABLED` (pure-safety, fires only on stall); OFF byte-identical.
- **Reuse-first:** the SAME ruler (`estimate_subgoal_payload_chars`/`dw_egress_interceptor.estimate_body_chars`), the FIFO/singleton pattern from `recursion_dedup`, `publish_task_event` telemetry, the decompose funnel. NO parallel state store.
- **Worktree:** verify via `git show`/`grep`; commits need `ledger_sovereignty.mark_owned` (stamped).

---

## Task T1: `convergence_watchdog.py` — reduction tracker + verdict + thresholds + telemetry
**Files:** Create `backend/core/ouroboros/governance/convergence_watchdog.py`; Test `tests/governance/test_convergence_watchdog.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) class WatchdogVerdict: stalled: bool; ratio: float; consecutive_stalls: int; passes: int`
- `stall_ratio_threshold() -> float` (`JARVIS_WATCHDOG_STALL_RATIO` default 0.95); `stall_passes_threshold() -> int` (`JARVIS_WATCHDOG_STALL_PASSES` default 2); `watchdog_enabled() -> bool` (`JARVIS_CONVERGENCE_WATCHDOG_ENABLED` default true).
- `class ReductionTracker`: bounded lineage map. `record_pass(self, lineage_id: str, parent_chars: int, max_child_chars: int) -> WatchdogVerdict` — `ratio = max_child_chars/max(1, parent_chars)`; append to the lineage's bounded deque; `consecutive_stalls` = trailing run of ratios ≥ `stall_ratio_threshold()`; `stalled = consecutive_stalls >= stall_passes_threshold()`. Fail-soft → `WatchdogVerdict(False, 0.0, 0, 0)`. `reset(lineage_id)` clears a lineage. `get_reduction_tracker() -> ReductionTracker` singleton.
- `emit_sovereign_yield(op_id, *, lineage_id, ratio, consecutive_stalls, parent_chars, child_chars, tier) -> None` — stdout WARNING `[SOVEREIGN YIELD] op=… lineage=… stalled reduction ratio=… passes=… -> structural weight-shed (tier=…) parent=… child=…` + best-effort `publish_task_event("sovereign_yield", op_id, {…})`. Fail-soft.

- [ ] **Step 1: Read first** — `recursion_dedup.py:57-130` (AttemptLedger bounded-deque + singleton pattern to MIRROR); `ide_observability_stream.py:2247` `publish_task_event` signature.
- [ ] **Step 2: Failing tests** (`tests/governance/test_convergence_watchdog.py`):
```python
from __future__ import annotations
import importlib, pytest
from backend.core.ouroboros.governance import convergence_watchdog as cw

def _fresh():
    importlib.reload(cw); return cw.ReductionTracker()

def test_thresholds_defaults(monkeypatch):
    monkeypatch.delenv("JARVIS_WATCHDOG_STALL_RATIO", raising=False)
    monkeypatch.delenv("JARVIS_WATCHDOG_STALL_PASSES", raising=False)
    assert cw.stall_ratio_threshold() == 0.95 and cw.stall_passes_threshold() == 2
    assert cw.watchdog_enabled() is True

def test_good_reduction_no_stall():
    t = _fresh()
    v = t.record_pass("g1", parent_chars=1000, max_child_chars=400)  # ratio 0.4
    assert v.stalled is False and v.ratio < 0.5 and v.consecutive_stalls == 0

def test_two_consecutive_stalls_trips():
    t = _fresh()
    t.record_pass("g1", 1000, 980)            # 0.98 stall #1
    v = t.record_pass("g1", 1000, 990)        # 0.99 stall #2 -> stalled
    assert v.consecutive_stalls >= 2 and v.stalled is True

def test_good_pass_resets_run():
    t = _fresh()
    t.record_pass("g1", 1000, 980)            # stall
    v = t.record_pass("g1", 1000, 300)        # good -> resets
    assert v.consecutive_stalls == 0 and v.stalled is False

def test_lineages_independent():
    t = _fresh()
    t.record_pass("a", 1000, 990); t.record_pass("a", 1000, 990)
    v = t.record_pass("b", 1000, 200)
    assert v.stalled is False

def test_failsoft_bad_input():
    t = _fresh()
    v = t.record_pass("g", parent_chars=0, max_child_chars=0)
    assert isinstance(v.stalled, bool)

def test_emit_sovereign_yield_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        cw.emit_sovereign_yield("op1", lineage_id="g1", ratio=0.97, consecutive_stalls=2,
                                parent_chars=5000, child_chars=4850, tier="tier2")
    assert any("[SOVEREIGN YIELD]" in r.getMessage() for r in caplog.records)
```
- [ ] **Step 3: Run → fail. Step 4: Implement** (bounded `dict[str, deque]` maxlen=tracker size lineages, each deque maxlen small e.g. passes_threshold+3; mirror recursion_dedup's singleton). `publish_task_event` lazy import + fail-soft. **Step 5: Run → pass.** Commit: `feat(watchdog): convergence_watchdog — reduction-trajectory stall detection + [SOVEREIGN YIELD] telemetry`.

## Task T2: `epistemic_shedder.py` — tiered pure-AST weight shedder
**Files:** Create `backend/core/ouroboros/governance/epistemic_shedder.py`; Test `tests/governance/test_epistemic_shedder.py`.

**Interfaces — Produces:** `def shed_to_fit(source: str, target_chars: int) -> tuple[str, str]` → `(shed_source, tier_reached)` where tier ∈ {"none","tier1","tier2","tier3"}. Re-measures with `dw_egress_interceptor.estimate_body_chars({"messages":[{"role":"user","content":shed_source}]})` (or a thin char-len) after each tier; stops at first fit.

- [ ] **Step 1: Read first** — `dw_egress_interceptor.estimate_body_chars` (the ruler); `ast` docs for unparse (3.9+: `ast.unparse` is 3.9+). For a char ruler over raw source, `len(source)` is acceptable + consistent.
- [ ] **Step 2: Failing tests:**
```python
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import epistemic_shedder as es

SRC = '''
"""Module docstring that is fairly long and counts as fluff padding padding."""
import os
class Big:
    """Class doc."""
    def build(self, x):
        """Build doc."""
        # a comment
        total = 0
        for i in range(1000):
            total += i * i * i  # heavy body line padding padding padding
        return total
    def small(self):
        return 1
'''

def test_tier1_strips_docstrings_still_parseable():
    out, tier = es.shed_to_fit(SRC, target_chars=len(SRC) - 40)
    import ast; ast.parse(out)                 # still valid
    assert "Module docstring" not in out or tier in ("tier2", "tier3")

def test_tier2_omits_bodies_keeps_signatures():
    out, tier = es.shed_to_fit(SRC, target_chars=120)   # force deep shed
    assert "[SOVEREIGN YIELD: Implementation Omitted]" in out or tier == "tier3"
    assert "def build" in out                  # signature kept

def test_tier3_truncates_to_fit():
    out, tier = es.shed_to_fit(SRC, target_chars=40)
    assert len(out) <= 60                       # truncated near target

def test_parse_error_falls_to_truncation():
    bad = "def (:\n  oops" * 50
    out, tier = es.shed_to_fit(bad, target_chars=30)
    assert len(out) <= 50 and tier == "tier3"

def test_never_execs(monkeypatch):
    import builtins
    monkeypatch.setattr(builtins, "exec", lambda *a, **k: (_ for _ in ()).throw(AssertionError("exec")))
    es.shed_to_fit(SRC, target_chars=50)

def test_already_fits_returns_none_tier():
    out, tier = es.shed_to_fit("x = 1\n", target_chars=10_000)
    assert tier == "none"
```
- [ ] **Step 3: Run → fail. Step 4: Implement** the 3 tiers: Tier1 `ast.parse`→strip docstrings (first `Expr(Constant str)` in each `Module/ClassDef/FunctionDef/AsyncFunctionDef` body)→`ast.unparse` (drops comments); Tier2 for heaviest defs (by `len(ast.get_source_segment)`) replace `.body` with `[ast.Expr(ast.Constant("[SOVEREIGN YIELD: Implementation Omitted]"))]` keeping the signature, heaviest-first until fit; Tier3 `source[:target_chars]` (chronological truncation). Measure after each. Parse error → Tier3 of raw source. PURE AST, fail-soft. **Step 5: Run → pass.** Commit: `feat(watchdog): epistemic_shedder — tiered pure-AST weight shed (fluff -> signature-only -> truncation)`.

## Task T3: Wire the watchdog into the decompose funnel
**Files:** Modify `backend/core/ouroboros/governance/orchestrator.py` (`_decompose_block_or_legacy`); Test `tests/governance/test_watchdog_wiring.py`.
**Consumes:** T1 `get_reduction_tracker`/`watchdog_enabled`/`emit_sovereign_yield`, T2 `shed_to_fit`, `estimate_subgoal_payload_chars`.
- [ ] **Step 1: Read first** — `orchestrator.py:2073` `_decompose_block_or_legacy`, the `_all_subs = decompose_for_block(... compression_target=…)` site (~2209), how `compression_target` arrives, the `goal`/lineage id (root goal_id) + `ctx.op_id`.
- [ ] **Step 2: Failing test** (fakes): with `compression_target` set and `watchdog_enabled()`, after a decompose pass whose largest child ≥95% of parent for 2 consecutive passes → the watchdog yields ONE fitting sub-goal (payload ≤ compression_target via `shed_to_fit`) AND `emit_sovereign_yield` fires; a reducible pass → normal `_all_subs`; disabled → byte-identical. Structural assertion that the funnel calls `record_pass` + (on stall) `shed_to_fit`+`emit_sovereign_yield` is acceptable for the deep site.
- [ ] **Step 3: Implement** after `_all_subs = decompose_for_block(...)` (only when `compression_target is not None and watchdog_enabled()`):
```python
try:
    _parent = estimate_subgoal_payload_chars(goal)
    _maxchild = max((estimate_subgoal_payload_chars(s) for s in _all_subs), default=0)
    _v = get_reduction_tracker().record_pass(<root_goal_id>, _parent, _maxchild)
    if _v.stalled:
        _src = <heaviest sub-goal's symbol source / goal source>
        _shed, _tier = shed_to_fit(_src, compression_target)
        emit_sovereign_yield(ctx.op_id, lineage_id=<root_goal_id>, ratio=_v.ratio,
                             consecutive_stalls=_v.consecutive_stalls, parent_chars=_parent,
                             child_chars=_maxchild, tier=_tier)
        _all_subs = [<one SubGoal carrying _shed as its scoped context/description>]
except Exception:
    pass   # fail-soft: legacy slice path; de-dup ledger is the backstop
```
**Step 4: Run** + regression `-k "orchestrator or decomposition or watchdog or egress"`. **Step 5: Commit:** `feat(watchdog): wire convergence watchdog into _decompose_block_or_legacy — stall -> structural yield, fail-soft`.

## Task T4: Integration + static validation (operator gate)
**Files:** Test `tests/governance/test_watchdog_integration.py`.
- [ ] End-to-end with fakes: (1) a synthetic irreducible-but-overweight lineage → 2 stalled passes → watchdog yields a ≤target sub-goal + `[SOVEREIGN YIELD]` (no infinite re-slice, no DW egress — fake session asserts zero post). (2) reducible lineage → no stall, normal slicing. (3) shedder Tier1→2→3 escalation fits. (4) parse-error → Tier3. (5) OFF byte-identical. (6) NEVER exec. Reused-subsystem regression sweep (orchestrator, decomposition, egress, recursion_dedup). Commit.

## Done criteria
- A stalled recursive-decompose YIELDS (sheds structurally to a fitting sub-goal) instead of looping — trajectory-triggered, no hardcoded cap, no DW egress, `[SOVEREIGN YIELD]` observable. Tiered pure-AST shedder (fluff → signature-only → truncation). Default-ON; OFF byte-identical. Fail-soft. Reuse-first. Zero real regressions. Final cross-cutting review (confirm the hook is on the LIVE funnel + the de-dup backstop intact).
- **Static validation (operator gate):** a stalled lineage provably converges to ≤target locally (no loop, no egress) in a test, BEFORE the C2 soak.

## Self-review
- Spec coverage: §4.1→T1, §4.2→T2, §4.3→T1 (emit), §4.4→T3, §6→T4. Thresholds/env, tiered shedder, telemetry, funnel hook, fail-soft, OFF byte-identical all covered.
- Interfaces consistent: `WatchdogVerdict`, `record_pass`, `get_reduction_tracker`, `watchdog_enabled`, `emit_sovereign_yield`, `shed_to_fit(source, target)->(src,tier)`.
- Big-file task T3 ships read-first instructions — confirm the funnel site + lineage id against live code; the hook MUST be on the live `_decompose_block_or_legacy` path (the same funnel the egress re-chunk already uses).
