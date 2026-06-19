# Sovereign Fleet Evaluator — Design Spec (2026-06-19)

**Author:** O+V (Claude Opus 4.8) under operator authorization (Derek J. Russell)
**Status:** Approved design — Option 1 (Advisory → auto-graduate)
**Branch:** `fleet/sovereign-evaluator`

---

## 1. Problem (the genuine gap)

The DoubleWord (DW) fleet selection layer is **already dynamic**: `dw_catalog_client.py`
discovers `/v1/models` every 30 min (403/502-safe), `dw_promotion_ledger.py` runs a
promote→demote "elites" FSM, and `dw_ttft_observer.py` computes per-model latency stats
with dynamic-N math gates. The static `dw_models:` YAML arrays were already purged
(Slice E, 2026-04-27); the catalog owns model *facts*, YAML owns *policy* only.

**Every existing telemetry axis is latency-only and passive.** The ledger promotes a
model when it is *fast and does not error*. **Nothing measures whether the output is
valid** — AST-parseable code, or a clean discrete classification label.

This is the exact, measured failure mode: `Qwen/Qwen3.5-397B-A17B-FP8` (the line-67
cold-start default in `doubleword_provider.py`) is fast, never errors, and emits **zero
valid code** — it reasons in prose. Under the current math it would *never be demoted*.
The same blind spot would crown `Qwen3-14B` (returns empty strings on triage) as the
triage model.

**The missing axis is correctness.** We add an active quality-calibration layer on top
of the existing discovery + latency skeleton — we do **not** rebuild discovery, the
ledger, or the observer, and we do **not** re-introduce static model names into YAML.

## 2. Goal

A `FleetEvaluator` subsystem that, on idle cycles, proactively probes each
**accessible** discovered DW model with a tiny, cost-bounded, **in-memory-only**
quality battery, fuses the resulting syntax/label pass-rate with existing latency into a
`valid_tok_per_s` composite (EWMA-smoothed), and feeds that score into
`provider_topology.dw_models_for_route()` as a guarded re-rank — **advisory first**,
then **auto-graduating** to authoritative once a soak proves the calibrated picks beat
the 397B default. Demote the 397B cold-start default to the calibrated top coder.

## 3. Non-goals (explicit, to prevent scope creep & duplication)

- **NOT** a new discovery path — reuse `dw_catalog_client.load_cached_snapshot()`.
- **NOT** a new latency tracker — read `dw_ttft_observer` / `dw_promotion_ledger` stats.
- **NOT** a YAML rewrite — `dw_allowed`, `block_mode`, `fallback_tolerance`, cost
  contracts stay 100% YAML-authored. We only re-order the already-ranked `dw_models`
  tuple a route returns.
- **NOT** code execution. **No `exec()`, no `eval()`, no file writes** of generated
  payloads. Validation is `ast.parse()` on an in-memory string + AST-node inspection.
- **NOT** an always-on benchmark — idle-gated + cost-capped + master-OFF by default.

## 4. Architecture & units

Four new modules under `backend/core/ouroboros/governance/`, plus one guarded call site
in `provider_topology.py`. Each unit has one responsibility and a clean interface.

```
fleet_quality_battery.py   (pure)   prompts + in-memory validators (the AST armor)
fleet_calibration_store.py (pure-ish) QualityScore + persistence + EWMA + rerank + graduation math
fleet_evaluator.py         (async)  idle-driven driver: discover→probe→score→persist→graduate
provider_topology.py       (1 line) guarded fleet rerank of dw_models_for_route() result
```

### 4.1 `fleet_quality_battery.py` — the AST-validation armor (pure, no I/O, no network)

The "Ephemeral In-Memory Validation" the operator mandated. Zero execution.

**Prompts (module constants):**
- `CODEGEN_PROMPT` — a concrete multi-function refactor task whose answer is a Python
  code block. (e.g. "implement `merge_intervals` + `interval_union` with docstrings,
  handle empty list; return ONLY a python code block.")
- `CLASSIFY_PROMPT` — a discrete-label task: classify a dev task as exactly one of
  `NO_OP | REDIRECT | ENRICH | GENERATE`, "reply with ONLY the label."
- `EXPECTED_LABEL = "ENRICH"` (the correct answer for the classify prompt).

**Pure validators (each takes a string, returns a bool/float, never raises):**
- `extract_code_block(text) -> str` — pull the fenced ```python block (or whole text).
- `is_ast_valid(text) -> bool` — `extract_code_block` → `ast.parse(...)` in a
  `try/except (SyntaxError, ValueError, RecursionError)`. **No exec.**
- `has_semantic_placeholder(text) -> bool` — walk the parsed AST; True if it contains a
  bare `Ellipsis` (`...`) constant as a statement body, a function whose body is only
  `pass`/`...`/`raise NotImplementedError`, or a `# TODO`/`# implement` marker. A model
  that emits `def f(): ...` "passes" `ast.parse` but is not real code — this catches it.
- `code_quality_pass(text) -> bool` — `is_ast_valid(text) and not has_semantic_placeholder(text)`.
- `label_adherence(text, expected) -> float` — 1.0 if the stripped, upper-cased output
  is exactly the expected label (allowing surrounding whitespace/punctuation strip);
  0.5 if expected token appears but with extra prose; 0.0 otherwise / empty.

**Why a separate pure module:** the validators are the security boundary (the operator's
"armor") and the part most worth exhaustively unit-testing in isolation. They must hold
even if a probed model returns adversarial/hallucinatory text — `ast.parse` only ever
builds a tree, never runs it.

### 4.2 `fleet_calibration_store.py` — scores, persistence, EWMA, rerank & graduation math

**`QualityScore` (frozen dataclass):**
```
model_id: str
ast_pass_rate: float          # EWMA of code_quality_pass over codegen probes [0,1]
label_adherence: float        # EWMA of label_adherence over classify probes  [0,1]
ttft_ms: float                # EWMA first-token latency
tok_per_s: float              # EWMA raw generation velocity
sample_count: int             # total probes folded in
updated_at: float             # unix ts (passed in by caller; module never calls time itself in pure paths)
```
Derived (pure properties / functions):
- `valid_tok_per_s(score) -> float` = `tok_per_s * ast_pass_rate` (codegen routes).
- `triage_fitness(score) -> float` = `label_adherence / max(ttft_ms, 1) * 1000`
  (classification — adherence per second; rewards Gemma-class fast+correct).

**EWMA merge (pure):**
`ewma_update(prev, new, alpha) -> float` = `alpha*new + (1-alpha)*prev` (alpha from
`JARVIS_FLEET_EWMA_ALPHA`, default 0.4). First sample (prev is None) → `new`. A single
transient 502 (treated as a failed probe → 0.0 contribution) cannot zero a strong model.

**Store (`FleetCalibrationStore`):**
- Persist `.jarvis/fleet_calibration.json` (env `JARVIS_FLEET_CALIBRATION_PATH`), atomic
  write (mirror `dw_promotion_ledger._atomic_write`).
- `load()` / `save()` / `_ensure_loaded()` (lazy, never raises).
- `record_probe(model_id, *, kind, code_pass=None, label_score=None, ttft_ms, tok_per_s, now)`
  → folds one probe result into that model's `QualityScore` via EWMA.
- `score(model_id) -> Optional[QualityScore]`.
- `all_scores() -> Dict[str, QualityScore]`.

**Rerank (pure, the binding math):**
`fleet_rerank(route, ranked_models, scores, *, route_kind) -> Tuple[str,...]`
- `route_kind` ∈ {`code`, `triage`} derived from route (IMMEDIATE/STANDARD/COMPLEX/
  BACKGROUND/SPECULATIVE → `code`; the SemanticTriage caller path → `triage`).
- Stable-sort the *input* `ranked_models` by the relevant composite descending
  (`valid_tok_per_s` for code, `triage_fitness` for triage). Models with no score (never
  benchmarked) keep their original relative order and sort *after* scored models only if
  they have ≥1 sample; **unbenchmarked models retain catalog rank** (never demote a model
  we haven't measured below one we have, on zero evidence — only reorder among measured).
- Returns the input tuple unchanged if scores is empty / route_kind unknown / fewer than
  2 scored models. Pure, deterministic, never raises.

**Graduation math (pure):**
`graduation_ready(scores, *, default_model, min_samples, min_margin) -> Optional[str]`
- Returns the model id that should *replace* `default_model` as the cold-start coder iff:
  a measured model has `sample_count >= min_samples`
  (`JARVIS_FLEET_GRAD_MIN_SAMPLES`, default 5) **and** `code_quality` pass-rate ≥
  `JARVIS_FLEET_GRAD_MIN_AST` (default 0.8) **and** its `valid_tok_per_s` exceeds the
  default model's by `min_margin` factor (`JARVIS_FLEET_GRAD_MARGIN`, default 1.5×) —
  *or* the default model itself scored `ast_pass_rate < 0.2` (the 397B case: it's
  measured-bad, so any valid coder wins). Else `None`.

### 4.3 `fleet_evaluator.py` — the async idle-driven driver

**Gates:**
- `fleet_evaluator_enabled()` — `JARVIS_FLEET_EVALUATOR_ENABLED` (default **false**).
- `fleet_authoritative_enabled()` — `JARVIS_FLEET_EVALUATOR_AUTHORITATIVE` (default
  **false**; flipped by auto-graduation).

**Injectable model caller (testability + reuse):**
```
async def model_caller(model_id, messages, *, max_tokens) -> ProbeResult
```
`ProbeResult{text, ttft_ms, total_ms, completion_tokens, ok, error}`. The default
implementation reuses the same DW HTTP idiom as `dw_catalog_client` (aiohttp POST to
`/v1/chat/completions`, `JARVIS_FLEET_PROBE_TIMEOUT_S` default 60, key from the same env
the catalog client uses). A `403/502/timeout` → `ProbeResult(ok=False)` → folds as a
0.0 quality contribution (the access wall self-skips, never crashes the loop). Tests
inject a fake caller — **no live network in unit tests.**

**Cost bounding (reuse, don't reinvent):**
- `max_tokens` per probe is small (`JARVIS_FLEET_PROBE_MAX_TOKENS`, default 512).
- Per-cycle model cap (`JARVIS_FLEET_MAX_MODELS_PER_CYCLE`, default 4) — round-robins
  across discovered models so each gets calibrated over several cycles.
- Daily probe-spend cap tracked in the store (`JARVIS_FLEET_DAILY_USD_CAP`, default
  0.50); estimated via DW per-token pricing already in the catalog `ModelCard`. Over cap
  → cycle no-ops.
- Only runs when idle (hook below).

**Idle hook (reuse DreamEngine / IdleDetector):**
`maybe_calibrate(now)` is invoked from the existing idle/background path. It:
1. returns immediately unless `fleet_evaluator_enabled()`,
2. checks idle + daily cap,
3. loads the catalog snapshot (`load_cached_snapshot()`); if None → no-op,
4. picks the next ≤N models (round-robin, prefer never-/least-recently-benchmarked),
5. for each: run codegen probe + classify probe via `model_caller`, validate in-memory
   via `fleet_quality_battery`, `store.record_probe(...)`,
6. log `[FleetEvaluator] model=X ast=.. label=.. vtps=.. samples=N`,
7. call `_maybe_graduate()`.

**Auto-graduation (`_maybe_graduate`):**
- Compute `graduation_ready(store.all_scores(), default_model=<line-67 default>, ...)`.
- If a winner is found **and** not already authoritative: log the proposed flip
  (advisory), and if the winner has held across `JARVIS_FLEET_GRAD_STABLE_CYCLES`
  (default 2) consecutive evaluations, persist `JARVIS_FLEET_EVALUATOR_AUTHORITATIVE=true`
  via the existing bounded `.env` writer (the same credential-safe persist used by the
  graduation orchestrator — **reuse, do not re-author**), and record the chosen coder.
- **Advisory mode = compute + log only; routing unchanged.** Authoritative flips the
  rerank live.

**Observability (Manifesto §7):** structured log lines + best-effort SSE
`fleet_calibrated` / `fleet_graduated` via the existing event broker (positional
`publish(event_type, op_id, payload)` signature — the real one, learned the hard way).

### 4.4 Binding in `provider_topology.dw_models_for_route()` (one guarded block)

At the **end** of `dw_models_for_route`, before returning the resolved `ranked`/`effective`
tuple, add a single guarded re-rank:
```python
if fleet_authoritative_enabled():
    ranked = fleet_apply_rerank(route, ranked)   # thin adapter: load store + route_kind + fleet_rerank
return ranked
```
`fleet_apply_rerank` lives in `fleet_calibration_store.py` (loads the store, derives
route_kind, calls pure `fleet_rerank`, returns input unchanged on any error). When
`JARVIS_FLEET_EVALUATOR_AUTHORITATIVE` is false (default) the call is never made →
**byte-identical legacy behavior.** The 397B-default demotion is automatic: once
authoritative, the calibrated top coder sorts above 397B in every code route, and the
cold-start default lookup consults the store's graduated coder.

## 5. Safety, fail-soft & invariants

- **No execution of model output, ever** — `ast.parse` only. (Operator armor mandate.)
- **OFF is byte-identical** — every new path is behind `JARVIS_FLEET_EVALUATOR_ENABLED`
  (compute) and `JARVIS_FLEET_EVALUATOR_AUTHORITATIVE` (routing). Both default false.
- **Fail-soft everywhere** — every public method swallows exceptions and returns a safe
  default (unchanged ctx / empty / None). A broken probe never wedges the loop.
- **No new discovery / ledger / observer** — reuse the graduated subsystems.
- **YAML policy untouched** — only the ranked tuple order changes.
- **Idle + cost capped** — cannot starve the host or burn quota; respects daily USD cap.
- **EWMA decay** — transient API flakes can't permanently mis-rank a model.
- **Unbenchmarked models keep catalog rank** — zero-evidence never demotes.

## 6. Testing strategy (TDD, all offline)

- `fleet_quality_battery`: AST-valid pass, syntax-fail reject, placeholder (`def f(): ...`)
  reject, `raise NotImplementedError` reject, label exact-match / prose / empty.
  Adversarial input (`os.system('rm -rf /')` as a *string*) → parses to a tree, is NOT
  executed (assert no side effect), classified by validity only.
- `fleet_calibration_store`: EWMA merge math, first-sample, persistence round-trip,
  rerank reorders measured-good above measured-bad, leaves unbenchmarked alone, returns
  input on <2 scored, graduation_ready fires on 397B-bad case + margin case + returns
  None below thresholds.
- `fleet_evaluator`: fake `model_caller` returning (valid coder / 397B-style prose /
  502) → store reflects correct scores; daily-cap no-op; disabled → no probes; idle-gate;
  `_maybe_graduate` flips only after stable cycles; SSE publish uses positional signature.
- `provider_topology`: authoritative-off → `dw_models_for_route` byte-identical;
  authoritative-on with a store → tuple reordered by quality; error in rerank → original
  tuple returned.

Verification in this sandbox is via `ast.parse` / grep / `pytest --collect-only`
(the full organism cannot be imported here — split-brain guard). Live unit tests run
green; the live calibration soak runs on a real host.

## 7. Deployment / soak

1. Land all units (gated OFF — byte-identical, safe to merge).
2. Arm `JARVIS_FLEET_EVALUATOR_ENABLED=true` (advisory) on a real host soak.
3. Watch logs: evaluator must (a) identify 397B `ast_pass_rate≈0` and (b) rank
   DeepSeek-V4-Flash's `valid_tok_per_s` highest among coders.
4. Once `graduation_ready` returns the Flash coder for ≥2 stable cycles, auto-graduation
   persists `JARVIS_FLEET_EVALUATOR_AUTHORITATIVE=true` → routing live, 397B demoted.

## 8. Flag summary (all `JARVIS_FLEET_*`, registered in FlagRegistry)

| Flag | Default | Meaning |
|---|---|---|
| `JARVIS_FLEET_EVALUATOR_ENABLED` | false | master: run calibration at all |
| `JARVIS_FLEET_EVALUATOR_AUTHORITATIVE` | false | quality scores re-rank live routing (auto-flipped) |
| `JARVIS_FLEET_EWMA_ALPHA` | 0.4 | EWMA smoothing factor |
| `JARVIS_FLEET_PROBE_MAX_TOKENS` | 512 | per-probe token cap |
| `JARVIS_FLEET_PROBE_TIMEOUT_S` | 60 | per-probe HTTP timeout |
| `JARVIS_FLEET_MAX_MODELS_PER_CYCLE` | 4 | models calibrated per idle cycle |
| `JARVIS_FLEET_DAILY_USD_CAP` | 0.50 | daily probe spend ceiling |
| `JARVIS_FLEET_GRAD_MIN_SAMPLES` | 5 | min probes before a model can graduate |
| `JARVIS_FLEET_GRAD_MIN_AST` | 0.8 | min code pass-rate to graduate |
| `JARVIS_FLEET_GRAD_MARGIN` | 1.5 | × valid_tok_per_s the winner must beat the default |
| `JARVIS_FLEET_GRAD_STABLE_CYCLES` | 2 | consecutive wins before auto-flip |
| `JARVIS_FLEET_CALIBRATION_PATH` | `.jarvis/fleet_calibration.json` | store path |
```
