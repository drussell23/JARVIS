# Sovereign Egress Interceptor Mesh — Design Spec

> **Arc:** make it *structurally impossible* to send DoubleWord a malformed or oversized request — validated locally before any egress. Triggered by Meryem @ DW (co-founder) flagging `reasoning_effort=none` errors on gpt-oss-120b (2026-06-21, pre-fix). "Impeccable API citizen."
> **Date:** 2026-06-22. **Branch:** `worktree-sovereign-egress-interceptor`. **Default-ON** (it's the citizenship guarantee).

---

## 1. Diagnosis (reuse-first — what already exists)
- **reasoning_effort is ALREADY leak-proof** (verified): every egress site resolves it via `_reasoning_request_params(complexity, model=…)` → `_dw_model_min_effort(model)` 3-tier floor (catalog → learned `dw_reasoning_profile` → static `gpt-oss:low` seed, `doubleword_provider.py:169`). No path can send `none` to gpt-oss. Meryem's errors were pre-fix. So Component A is NOT a bug fix — it formalizes a single chokepoint + an extensible registry + belt-and-suspenders.
- **Payload size:** batch upload has a 64 MiB byte gate (`doubleword_provider.py:4297-4306`); **realtime/streaming has NO size check** — this is the genuine gap.
- **Egress chokepoint:** realtime body assembled at `doubleword_provider.py:3132-3143` (shared by SSE + non-stream, before `session.post` @3272/3740); batch body baked into JSONL at `1715-1747`. Body construction is DUPLICATED across the two paths.
- **Failure→chunking:** `DoublewordInfraError` re-raised to candidate_generator → `RuntimeError` to orchestrator. No "payload too heavy → chunk" loop today.

## 2. Goals / Non-Goals
**Goals.** (G1) A single local pre-flight interceptor at both egress chokepoints that GUARANTEES DW never receives a malformed or oversized request. (G2) Schema sanitization via an EXTENSIBLE capability registry (no hardcoded if/else) — strips/maps unsupported params per the target model. (G3) A payload-weight governor that BLOCKS oversized egress locally, raising a math-enriched `LocalEgressOverweightError`. (G4) Context-aware re-chunking: the error carries the exact compression target, threaded into `decompose_for_block` so the AST decomposer slices sub-goals that fit. (G5) Default-ON + a loud boot-guard if disabled. (G6) Reuse `ModelCard`, the reasoning floor, `FailureSource`, `decompose_for_block`, `ast_symbol_scoper` — no parallel systems.

**Non-Goals.** No new HTTP client. No change to the reasoning floor logic (reuse it). No weakening of the Advisor/cage. The interceptor is a SAFETY NET, not the primary chunker (the Advisor-BLOCK→chunking path remains upstream).

## 3. Reuse Inventory
| Need | Existing asset | Anchor |
|---|---|---|
| Egress chokepoint (realtime) | `_generate_raw` body @ 3132-3143, fires 3272/3740 | `doubleword_provider.py` |
| Egress chokepoint (batch) | JSONL compose @ 1715-1747 | `doubleword_provider.py` |
| Reasoning floor (reused, not duplicated) | `_reasoning_request_params` / `_dw_model_min_effort` | `doubleword_provider.py:296,192` |
| Per-model capability metadata | `dw_catalog_client.ModelCard` (`context_window`, `param_count`, `supports_streaming`) | `dw_catalog_client.py:338-358` |
| Failure taxonomy (weight 0.0 our-side) | `topology_sentinel.FailureSource` (FSM_EXHAUSTED pattern) | `topology_sentinel.py:445-451` |
| Chunking matrix | `goal_decomposition_planner.decompose_for_block` + `ast_symbol_scoper` | (merged) |
| Boot daemon (boot-guard) | `GovernedLoopService.start()` | `governed_loop_service.py` |

## 4. Component Specs

### 4.1 `dw_egress_interceptor.py` (new pure leaf module)
- `class LocalEgressOverweightError(Exception)`: carries `attempted_size: int`, `max_allowed_size: int`, `required_compression_ratio: float` (`= attempted/max_allowed`), `model: str`. Math-enriched for the feedback loop.
- **A. `sanitize_egress_body(body: dict, model: str) -> dict`**: consults an EXTENSIBLE registry. Rule shape (env `JARVIS_DW_EGRESS_SANITIZE_RULES`, parsed like the existing min-effort map): `model-substr → {"strip": [param,…], "floor": {param: value}}`. Built-in rules compose the EXISTING reasoning floor (delegates to `_dw_model_min_effort`/`_reasoning_request_params` — does NOT reimplement). Returns a NEW dict with unsupported params stripped/mapped; unknown models pass through unchanged. Pure, fail-soft (error → return body unchanged).
- **B. `assert_egress_weight(body: dict, model: str) -> None`**: estimate payload weight = char-length of all `messages[].content` (+ system). Ceiling = `min(env JARVIS_DW_EGRESS_MAX_CHARS default e.g. 600_000, ModelCard.context_window-derived char budget when known)` — DYNAMIC, no literal cap (env tunes; registry refines). If `weight > ceiling` → raise `LocalEgressOverweightError(attempted=weight, max_allowed=ceiling, ratio=weight/ceiling, model=model)`. Else return. Pure, fail-soft (estimation error → do NOT block; never wrongly blocks a valid request).
- `def egress_interceptor_enabled() -> bool`: `JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED` **default TRUE**.

### 4.2 Wiring (both chokepoints)
At realtime `_generate_raw` (after body @3143, before `session.post`) and batch JSONL compose (1715-1747), when `egress_interceptor_enabled()`: `body = sanitize_egress_body(body, model)` then `assert_egress_weight(body, model)`. The `assert` raising blocks the HTTP fire (DW never sees it). Fail-soft wrapper so an interceptor bug never blocks a valid request.

### 4.3 Route-back to chunking (Context-Aware Compression Target)
- `candidate_generator` catches `LocalEgressOverweightError` → classifies a NEW `FailureSource.LOCAL_EGRESS_OVERWEIGHT` (weight 0.0 — like FSM_EXHAUSTED: our-side, NEVER trips the vendor breaker/sentinel/surface-health). Attaches the compression math to the op context/failure.
- The orchestrator's generate-failure path detects `LOCAL_EGRESS_OVERWEIGHT` + chunking-eligible → routes to `decompose_for_block(goal, …, compression_target=max_allowed_size)`.
- `decompose_for_block` gains an optional `compression_target: int | None`; when set, the AST decomposer (`ast_symbol_scoper`) slices so **no resulting sub-goal's estimated payload exceeds `compression_target`** (split a too-large symbol set further; if a single symbol still exceeds, emit it with a clear "irreducible" log — never silently exceed). Reuses the existing decomposition + the weight estimator from 4.1.

### 4.4 Sovereign Telemetry Boot-Guard
In `GovernedLoopService.start()`: if `not egress_interceptor_enabled()`, emit (async, non-crashing) a high-visibility WARNING to stdout + the telemetry ledger: `[SOVEREIGN WARNING] API Citizenship Guard Disabled: Egress Interceptor is OFF. Node is vulnerable to overweight payload dispatch.` Default-ON means this only fires on an explicit operator override.

## 5. Cross-cutting
- **Invariants.** (I1) DW NEVER receives a request the interceptor would reject (the citizenship guarantee). (I2) Fail-soft asymmetry: an interceptor ERROR must NEVER wrongly block a valid request (estimation/sanitize error → pass-through), but a CONFIRMED overweight MUST block. (I3) `LOCAL_EGRESS_OVERWEIGHT` is weight-0.0 — never trips the vendor breaker (it's our mistake, not DW's). (I4) Default-ON; OFF emits the boot-guard warning, never silent. (I5) Reuse-first — no duplicated reasoning/jitter/hash logic, no parallel capability store.
- **No hardcoding:** ceilings + sanitize rules are env-driven (curve), refined by the live `ModelCard` registry.

## 6. Test strategy
- Unit: `sanitize_egress_body` (strips registered param; floors reasoning via reuse; unknown model passthrough; fail-soft). `assert_egress_weight` (under→pass, over→raises with correct math; ModelCard ceiling; fail-soft estimation error→no block). `LocalEgressOverweightError` math. Registry parsing (env rules).
- Interaction: oversized body at the chokepoint → blocked, no `session.post` (fake session asserts zero egress). `LOCAL_EGRESS_OVERWEIGHT` classified weight-0.0 (no breaker trip). `decompose_for_block(compression_target=N)` → every sub-goal ≤ N (or irreducible logged). Boot-guard fires only when disabled.
- **Static validation (the user's gate):** a >ceiling request is provably blocked locally (no egress) BEFORE any soak. OFF byte-identical (minus the boot-guard warning).

## 7. Phasing
1. `dw_egress_interceptor.py` (sanitizer + governor + error) + tests. 2. Chokepoint wiring (both paths) + fake-session zero-egress test. 3. Route-back: FailureSource + orchestrator catch + `decompose_for_block(compression_target)` + AST slice bound. 4. Boot-guard. 5. Integration + final cross-cutting review. Then (operator-gated) the cost-capped C2 soak.
