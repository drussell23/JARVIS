# Sovereign Egress Interceptor Mesh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** A local pre-flight interceptor that GUARANTEES DoubleWord never receives a malformed or oversized request — schema sanitizer (extensible registry) + payload-weight governor (blocks oversized, raises a math-enriched error) + context-aware re-chunking + default-ON with a loud boot-guard.

**Architecture:** A pure leaf `dw_egress_interceptor.py` invoked at both DW egress chokepoints. Oversized payloads raise `LocalEgressOverweightError` (carrying the compression math) → classified our-side (weight 0.0, no breaker trip) → orchestrator routes to `decompose_for_block(compression_target=…)` which AST-slices sub-goals that fit. Reuse-first: existing reasoning floor, `ModelCard`, `FailureSource`, the chunking matrix.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`, `asyncio.wait_for`), pytest, stdlib only. ASCII only.

**Spec (binding):** `docs/superpowers/specs/2026-06-22-sovereign-egress-interceptor-mesh.md`.

## Global Constraints
- **Default-ON:** `JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED` default `true`. OFF → boot-guard `[SOVEREIGN WARNING]`, never silent.
- **Fail-soft ASYMMETRY (I2, critical):** a sanitize/estimate ERROR must NEVER wrongly block a valid request (→ pass-through); a CONFIRMED overweight MUST block. Never raise an unexpected exception into the dispatch path.
- **Zero oversized/malformed egress (I1):** when enabled + confirmed overweight, the HTTP fire MUST NOT happen.
- **`LOCAL_EGRESS_OVERWEIGHT` is weight 0.0** (our-side, like `FSM_EXHAUSTED`) — NEVER trips the vendor breaker/sentinel/surface-health.
- **No hardcoding:** ceilings + sanitize rules env-driven (curve), refined by the live `ModelCard`. No literal model `if/else`.
- **Reuse-first:** delegate reasoning to `_dw_model_min_effort`/`_reasoning_request_params`; reuse `ModelCard`, `FailureSource`, `decompose_for_block`, `ast_symbol_scoper`. NO parallel reasoning/capability/hash logic. A reviewer MUST reject a parallel capability store or a reimplemented reasoning floor.
- **Worktree:** verify writes via `git show`/`grep`; commits need `ledger_sovereignty.mark_owned` (stamped).

---

## Task T1: `dw_egress_interceptor.py` core (sanitizer + governor + error)
**Files:** Create `backend/core/ouroboros/governance/dw_egress_interceptor.py`; Test `tests/governance/test_dw_egress_interceptor.py`.

**Interfaces — Produces:**
- `class LocalEgressOverweightError(Exception)`: `__init__(self, *, attempted_size: int, max_allowed_size: int, model: str)`; computes `self.required_compression_ratio = attempted_size / max_allowed_size` (guard /0). Attrs: `attempted_size`, `max_allowed_size`, `required_compression_ratio`, `model`.
- `def egress_interceptor_enabled() -> bool` (`JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED`, default **true**).
- `def estimate_body_chars(body: dict) -> int` — sum of `len(str(content))` over `body["messages"][*]["content"]` (+ any top-level prompt). Fail-soft → 0.
- `def egress_char_ceiling(model: str) -> int` — `min(env JARVIS_DW_EGRESS_MAX_CHARS default 600000, _model_card_char_budget(model))` where the model budget derives from `ModelCard.context_window` (chars≈4×tokens, env `JARVIS_DW_EGRESS_CHARS_PER_TOKEN` default 4) when available, else the env cap. Fail-soft → env cap.
- `def sanitize_egress_body(body: dict, model: str) -> dict` — apply the extensible registry (4.1): returns a NEW dict with unsupported params stripped + floors applied. Reasoning rule delegates to the existing floor. Unknown model → unchanged. Fail-soft → return `body` unchanged.
- `def assert_egress_weight(body: dict, model: str) -> None` — `w=estimate_body_chars(body); c=egress_char_ceiling(model); if w>c: raise LocalEgressOverweightError(attempted_size=w, max_allowed_size=c, model=model)`. Fail-soft on estimation error → return (no block).
- registry: `def _sanitize_rules() -> dict` parsing `JARVIS_DW_EGRESS_SANITIZE_RULES` (format `substr={strip:p1|p2;floor:k:v}` — keep simple + documented) merged with built-ins (`{"gpt-oss": {"floor_reasoning": True}}`). NO literal model if/else in the apply loop.

- [ ] **Step 1: Read first** — `doubleword_provider.py:192` `_dw_model_min_effort`, `:296` `_reasoning_request_params` (the reasoning floor to DELEGATE to); `dw_catalog_client.py:338-358` `ModelCard` (`context_window`) + how to fetch a card for a model id.
- [ ] **Step 2: Failing tests** (`tests/governance/test_dw_egress_interceptor.py`):
```python
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import dw_egress_interceptor as egi

def _body(chars: int, model="m", reasoning="none"):
    return {"model": model, "messages": [{"role": "user", "content": "x" * chars}], "reasoning_effort": reasoning}

def test_enabled_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", raising=False)
    assert egi.egress_interceptor_enabled() is True

def test_estimate_chars():
    assert egi.estimate_body_chars(_body(100)) >= 100

def test_under_ceiling_passes(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_EGRESS_MAX_CHARS", "1000")
    egi.assert_egress_weight(_body(500), "anymodel")  # no raise

def test_over_ceiling_raises_with_math(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_EGRESS_MAX_CHARS", "1000")
    with pytest.raises(egi.LocalEgressOverweightError) as ei:
        egi.assert_egress_weight(_body(5000), "anymodel")
    e = ei.value
    assert e.max_allowed_size == 1000 and e.attempted_size >= 5000
    assert e.required_compression_ratio >= 5.0 and e.model == "anymodel"

def test_sanitize_unknown_model_passthrough():
    b = _body(10, model="totally-unknown", reasoning="none")
    assert egi.sanitize_egress_body(b, "totally-unknown") == b

def test_sanitize_gpt_oss_floors_reasoning():
    b = _body(10, model="openai/gpt-oss-120b", reasoning="none")
    out = egi.sanitize_egress_body(b, "openai/gpt-oss-120b")
    assert out["reasoning_effort"] != "none"  # floored via reused logic

def test_failsoft_bad_body_never_raises_unexpected():
    egi.assert_egress_weight({"messages": None}, "m")   # estimate→0→no block
    assert egi.sanitize_egress_body({"messages": None}, "m") == {"messages": None}

def test_overweight_error_ratio_zero_guard():
    e = egi.LocalEgressOverweightError(attempted_size=100, max_allowed_size=0, model="m")
    assert e.required_compression_ratio >= 1.0  # no ZeroDivisionError
```
- [ ] **Step 3: Run → fail** (`pytest tests/governance/test_dw_egress_interceptor.py -q`; ModuleNotFound).
- [ ] **Step 4: Implement** `dw_egress_interceptor.py` per the interfaces. Reasoning floor delegates: `from backend.core.ouroboros.governance.doubleword_provider import _dw_model_min_effort, _clamp_up_to_min` (or `_reasoning_request_params`) — DO NOT reimplement. ModelCard fetch via `dw_catalog_client` (lazy import, fail-soft). Every public fn wrapped fail-soft per I2 asymmetry.
- [ ] **Step 5: Run → pass.** Commit: `feat(egress): dw_egress_interceptor core — sanitizer (extensible registry) + weight governor + LocalEgressOverweightError(math)`.

## Task T2: Wire the interceptor at both DW egress chokepoints
**Files:** Modify `backend/core/ouroboros/governance/doubleword_provider.py` (realtime body site + batch JSONL site); Test `tests/governance/test_egress_chokepoint_wiring.py`.
**Consumes:** T1 `sanitize_egress_body`, `assert_egress_weight`, `egress_interceptor_enabled`, `LocalEgressOverweightError`.

- [ ] **Step 1: Read first** — `doubleword_provider.py:3132-3143` (realtime `body` assembly) + the `session.post` fire @~3272/3740; `:1715-1747` (batch JSONL `body`). Confirm exact var names.
- [ ] **Step 2: Failing test** (`tests/governance/test_egress_chokepoint_wiring.py`): with a fake session that records calls, an oversized realtime body must NOT reach `session.post` (zero egress) and must raise `LocalEgressOverweightError`; a normal body passes through + IS sanitized. Use monkeypatch of the interceptor + a fake `_effective_model`. (Structural assertion that both chokepoints call `sanitize_egress_body` + `assert_egress_weight` under `egress_interceptor_enabled()` is acceptable for the deep batch site.)
- [ ] **Step 3: Implement** at each chokepoint, immediately after the `body` dict is assembled (realtime ~3143; batch ~1747) and BEFORE the fire/JSONL-serialize:
```python
if egress_interceptor_enabled():
    try:
        body = sanitize_egress_body(body, <model_var>)
        assert_egress_weight(body, <model_var>)
    except LocalEgressOverweightError:
        raise            # block egress; bubble to candidate_generator (T3)
    except Exception:
        pass             # I2: sanitize/estimate bug never blocks a valid request
```
(import the interceptor at module top, lazy if cycle risk.) **Step 4: Run** + regression `-k "doubleword or candidate_generator"`. **Step 5: Commit:** `feat(egress): wire interceptor at realtime + batch egress chokepoints — block oversized before session.post`.

## Task T3: Route-back — FailureSource + orchestrator catch + compression target into decompose
**Files:** Modify `topology_sentinel.py` (new FailureSource), `candidate_generator.py` (classify), `orchestrator.py` (catch→decompose), `goal_decomposition_planner.py` + `ast_symbol_scoper.py` (compression_target); Test `tests/governance/test_egress_rechunk.py`.

- [ ] **Step 1: Read first** — `topology_sentinel.py:445-451` (`FailureSource.FSM_EXHAUSTED` weight 0.0 pattern); `candidate_generator.py` ~2256/5013 (DoublewordInfraError classify + re-raise); `orchestrator._decompose_block_or_legacy` + the generate-failure terminal path; `goal_decomposition_planner.decompose_for_block` signature; `ast_symbol_scoper.isolate_symbols`.
- [ ] **Step 2: Failing tests:** (a) classifying a `LocalEgressOverweightError` → `FailureSource.LOCAL_EGRESS_OVERWEIGHT` with weight 0.0 (no breaker trip). (b) `decompose_for_block(goal, zero_coverage=False, compression_target=1000)` → every returned sub-goal's `estimate_body_chars`-equivalent payload ≤ 1000 (or a single irreducible symbol logged). (c) orchestrator catch: a generate that raises `LocalEgressOverweightError` for a chunking-eligible op routes to `decompose_for_block` with `compression_target=error.max_allowed_size`.
- [ ] **Step 3: Implement** — add `LOCAL_EGRESS_OVERWEIGHT = "local_egress_overweight"` to `FailureSource` (weight 0.0, mirror FSM_EXHAUSTED's degrade/sever exclusion). `candidate_generator`: catch `LocalEgressOverweightError` in the dispatch try → classify `LOCAL_EGRESS_OVERWEIGHT`, attach `max_allowed_size` to the structured failure, re-raise as the wrapped RuntimeError carrying it. `orchestrator`: at the generate-failure site, if the failure is `LOCAL_EGRESS_OVERWEIGHT` + chunking-eligible → `await self._decompose_block_or_legacy(ctx, advisory, compression_target=<max_allowed>)` (extend the method with an optional `compression_target`). `decompose_for_block(..., compression_target: int | None = None)`: when set, the scoper slices so each sub-goal's estimated payload ≤ compression_target (split further; irreducible→emit+log). Reuse T1's `estimate_body_chars`/`egress_char_ceiling`. All fail-soft. **Step 4: Run** + regression `-k "topology or advisor or decomposition or candidate_generator or orchestrator"`. **Step 5: Commit:** `feat(egress): context-aware re-chunk — LOCAL_EGRESS_OVERWEIGHT (weight 0.0) -> decompose_for_block(compression_target)`.

## Task T4: Sovereign Telemetry Boot-Guard
**Files:** Modify `backend/core/ouroboros/governance/governed_loop_service.py` (boot sequence); Test `tests/governance/test_egress_boot_guard.py`.
- [ ] **Step 1: Read first** — `GovernedLoopService.start()` boot sequence (where other boot warnings/telemetry fire).
- [ ] **Step 2: Failing test:** when `JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED=false`, boot emits the exact `[SOVEREIGN WARNING] API Citizenship Guard Disabled: Egress Interceptor is OFF. Node is vulnerable to overweight payload dispatch.` (caplog/telemetry); when default/true, no warning. Boot never crashes either way.
- [ ] **Step 3: Implement** — in `start()`, `if not egress_interceptor_enabled(): logger.warning("[SOVEREIGN WARNING] API Citizenship Guard Disabled: ...")` (+ best-effort telemetry ledger note; async/non-blocking; fail-soft). **Step 4: Run.** **Step 5: Commit:** `feat(egress): Sovereign Telemetry Boot-Guard — loud [SOVEREIGN WARNING] when egress interceptor disabled`.

## Task T5: Integration + static validation (the user's gate)
**Files:** Test `tests/governance/test_egress_interceptor_integration.py`.
- [ ] End-to-end with fakes: (1) an oversized request is provably BLOCKED locally — fake session asserts `session.post` was NEVER called; the op surfaces `LOCAL_EGRESS_OVERWEIGHT` (weight 0.0, no breaker trip) → `decompose_for_block(compression_target)` → all sub-goals fit. (2) a valid request passes through sanitized. (3) sanitize/estimate error → pass-through (never wrongly blocks). (4) boot-guard fires only when disabled. (5) OFF (interceptor disabled) → byte-identical egress path (minus the boot-guard warning). Reused-subsystem regression sweep (doubleword_provider, candidate_generator, topology_sentinel, orchestrator, decomposition). Commit.

## Done criteria
- DW provably never receives a malformed/oversized request when enabled (fake-session zero-egress proof). Oversized → math-enriched `LocalEgressOverweightError` → weight-0.0 classification (no breaker trip) → context-aware re-chunk to fitting sub-goals. Default-ON; OFF emits the boot-guard warning. Fail-soft asymmetry holds (errors never wrongly block). Reuse-first (no parallel reasoning/capability/hash). Zero real regressions. Final cross-cutting coherence review (mandatory — wire the interceptor at the LIVE egress, not a dead path; confirm both chokepoints + the orchestrator catch are reachable).
- **Static validation (operator gate):** a >ceiling request blocked locally with zero egress, demonstrated in a test, BEFORE any C2 soak.

## Self-review
- Spec coverage: §4.1→T1, §4.2→T2, §4.3→T3, §4.4→T4, §6→T5. Default-ON + boot-guard + compression-target + extensible registry + weight-0.0 all covered.
- Interfaces consistent: `LocalEgressOverweightError(attempted_size/max_allowed_size/required_compression_ratio/model)`, `sanitize_egress_body`, `assert_egress_weight`, `egress_interceptor_enabled`, `estimate_body_chars`, `egress_char_ceiling`, `FailureSource.LOCAL_EGRESS_OVERWEIGHT`, `decompose_for_block(..., compression_target)`.
- Big-file tasks (T2/T3/T4) ship read-first instructions — confirm exact sites against live code; CRITICAL: T2 must wire the LIVE egress (both realtime + batch), and the final review must confirm reachability (the classify_runner-style dead-path miss must not recur).
