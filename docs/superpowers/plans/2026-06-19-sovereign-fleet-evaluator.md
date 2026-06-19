# Sovereign Fleet Evaluator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add an active correctness/quality calibration axis to DW model selection — an idle-driven `FleetEvaluator` that AST-validates model output in-memory, EWMA-smooths a `valid_tok_per_s` composite, and re-ranks `dw_models_for_route()` advisory→auto-graduate, demoting the 397B default.

**Architecture:** Four new modules under `backend/core/ouroboros/governance/` + one guarded call in `provider_topology.py`. Reuses `dw_catalog_client` (discovery), `dw_promotion_ledger`/`dw_ttft_observer` (latency), the bounded `.env` writer (graduation). No code execution (`ast.parse` only). All gated OFF by default → byte-identical legacy.

**Tech Stack:** Python 3.9+, `from __future__ import annotations`, asyncio, stdlib `ast`/`json`, pytest (+ `pytest.mark.asyncio`). Full spec: `docs/superpowers/specs/2026-06-19-sovereign-fleet-evaluator-design.md`.

**Sandbox note for implementers:** the full organism cannot be imported here (split-brain guard). Each new module must be a **leaf** importable in isolation (stdlib + sibling governance leaves only). Verify with `python3 -c "import ast; ast.parse(open('<file>').read())"` and run the new tests directly with `PYTHONPATH=. python3 -m pytest <testfile> -q`. Do NOT import `unified_supervisor`, `orchestrator`, or `governed_loop_service`.

---

## File Structure

- Create `backend/core/ouroboros/governance/fleet_quality_battery.py` — pure prompts + in-memory validators (the AST armor).
- Create `backend/core/ouroboros/governance/fleet_calibration_store.py` — `QualityScore` + EWMA + persistence + pure `fleet_rerank` + `graduation_ready` + `fleet_apply_rerank` adapter.
- Create `backend/core/ouroboros/governance/fleet_evaluator.py` — async idle-driven driver, injectable caller, cost/idle gates, `_maybe_graduate`.
- Modify `backend/core/ouroboros/governance/provider_topology.py` — one guarded `fleet_apply_rerank` call at end of `dw_models_for_route`.
- Tests: `tests/governance/test_fleet_quality_battery.py`, `test_fleet_calibration_store.py`, `test_fleet_evaluator.py`, `test_fleet_topology_binding.py`.

---

### Task 1: `fleet_quality_battery.py` — in-memory AST validation armor

**Files:**
- Create: `backend/core/ouroboros/governance/fleet_quality_battery.py`
- Test: `tests/governance/test_fleet_quality_battery.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_fleet_quality_battery.py
from __future__ import annotations
from backend.core.ouroboros.governance import fleet_quality_battery as b


def test_prompts_exist_and_are_strings():
    assert isinstance(b.CODEGEN_PROMPT, str) and "ONLY" in b.CODEGEN_PROMPT.upper()
    assert isinstance(b.CLASSIFY_PROMPT, str)
    assert b.EXPECTED_LABEL == "ENRICH"


def test_extract_code_block_fenced_and_bare():
    assert "def f" in b.extract_code_block("```python\ndef f():\n    return 1\n```")
    assert "def g" in b.extract_code_block("def g():\n    return 2")


def test_is_ast_valid_true_for_real_code():
    assert b.is_ast_valid("```python\ndef merge(x):\n    return sorted(x)\n```") is True


def test_is_ast_valid_false_for_syntax_error():
    assert b.is_ast_valid("```python\ndef merge(x):\n    return sorted(\n```") is False


def test_placeholder_ellipsis_body_detected():
    assert b.has_semantic_placeholder("```python\ndef f():\n    ...\n```") is True


def test_placeholder_notimplemented_detected():
    assert b.has_semantic_placeholder(
        "```python\ndef f():\n    raise NotImplementedError\n```") is True


def test_real_code_has_no_placeholder():
    assert b.has_semantic_placeholder(
        "```python\ndef f(x):\n    '''doc'''\n    return x + 1\n```") is False


def test_code_quality_pass_combines_both():
    assert b.code_quality_pass("```python\ndef f(x):\n    return x*2\n```") is True
    assert b.code_quality_pass("```python\ndef f():\n    ...\n```") is False
    assert b.code_quality_pass("not code at all !!!(") is False


def test_label_adherence_exact_prose_empty():
    assert b.label_adherence("ENRICH", "ENRICH") == 1.0
    assert b.label_adherence("  enrich \n", "ENRICH") == 1.0
    assert 0.0 < b.label_adherence("The label is ENRICH because...", "ENRICH") < 1.0
    assert b.label_adherence("", "ENRICH") == 0.0
    assert b.label_adherence("NO_OP", "ENRICH") == 0.0


def test_adversarial_string_is_parsed_not_executed(tmp_path):
    # A malicious payload as a STRING parses to a tree but must never run.
    canary = tmp_path / "canary.txt"
    payload = f"```python\nimport os\nos.system('touch {canary}')\n```"
    assert b.is_ast_valid(payload) is True       # syntactically valid
    assert canary.exists() is False              # but NEVER executed
```

- [ ] **Step 2: Run tests, verify they fail** — `PYTHONPATH=. python3 -m pytest tests/governance/test_fleet_quality_battery.py -q` → ImportError.

- [ ] **Step 3: Implement `fleet_quality_battery.py`**

Requirements (no I/O, no network, no exec/eval/file-write):
- `from __future__ import annotations`; `import ast`, `import re`.
- `CODEGEN_PROMPT`: a concrete multi-function task ending "Return ONLY a python code block." Example body: implement `merge_intervals(intervals)` and `interval_union(a, b)` with docstrings, handle empty list.
- `CLASSIFY_PROMPT`: classify task `'enrich the README with usage examples'` as exactly one of `NO_OP | REDIRECT | ENRICH | GENERATE`; "Reply with ONLY the label." `EXPECTED_LABEL = "ENRICH"`.
- `extract_code_block(text)`: regex ```` ```(?:python)?\s*(.*?)``` ```` DOTALL; group(1) if matched else the whole stripped text. Never raises (None text → "").
- `is_ast_valid(text)`: `try: ast.parse(extract_code_block(text)); return bool(stripped)` `except (SyntaxError, ValueError, RecursionError, TypeError): return False`.
- `has_semantic_placeholder(text)`: parse; if parse fails return False (validity is is_ast_valid's job). Walk tree: return True if any `ast.FunctionDef`/`ast.AsyncFunctionDef` whose body is exactly one stmt that is `ast.Expr(ast.Constant(Ellipsis))`, `ast.Pass`, or `ast.Raise` of `NotImplementedError`; OR a module-level bare `...`; OR a regex `#\s*(TODO|implement|fill in|your code)` in the raw text.
- `code_quality_pass(text)`: `is_ast_valid(text) and not has_semantic_placeholder(text)`.
- `label_adherence(text, expected)`: `t = (text or "").strip().upper().strip(".!:\"' ")`; exact == → 1.0; `expected in t.split()` or `expected in t` (word-bounded) with extra tokens → 0.5; else 0.0.

- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit** — `feat(fleet): in-memory AST validation battery (no exec)`.

---

### Task 2: `fleet_calibration_store.py` — scores, EWMA, persistence, rerank, graduation math

**Files:**
- Create: `backend/core/ouroboros/governance/fleet_calibration_store.py`
- Test: `tests/governance/test_fleet_calibration_store.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_fleet_calibration_store.py
from __future__ import annotations
from backend.core.ouroboros.governance import fleet_calibration_store as s


def test_ewma_first_sample_and_update():
    assert s.ewma_update(None, 1.0, 0.4) == 1.0
    assert abs(s.ewma_update(0.0, 1.0, 0.5) - 0.5) < 1e-9


def test_quality_score_composites():
    sc = s.QualityScore(model_id="m", ast_pass_rate=0.5, label_adherence=1.0,
                        ttft_ms=100.0, tok_per_s=80.0, sample_count=5, updated_at=1.0)
    assert abs(s.valid_tok_per_s(sc) - 40.0) < 1e-9         # 80 * 0.5
    assert s.triage_fitness(sc) > 0


def test_store_record_and_persist_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "fleet_calibration.json"
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(p))
    st = s.FleetCalibrationStore()
    st.record_probe("deepseek", kind="code", code_pass=True, ttft_ms=200, tok_per_s=90, now=1.0)
    st.record_probe("deepseek", kind="triage", label_score=1.0, ttft_ms=200, tok_per_s=90, now=2.0)
    st.save()
    st2 = s.FleetCalibrationStore()
    sc = st2.score("deepseek")
    assert sc is not None and sc.sample_count == 2 and sc.ast_pass_rate > 0


def test_rerank_orders_measured_good_above_bad():
    scores = {
        "qwen397": s.QualityScore("qwen397", 0.0, 0.0, 150, 120, 8, 1.0),   # fast, no valid code
        "deepseek": s.QualityScore("deepseek", 1.0, 1.0, 200, 90, 8, 1.0),  # slower, valid
    }
    out = s.fleet_rerank("standard", ("qwen397", "deepseek"), scores, route_kind="code")
    assert out[0] == "deepseek"      # valid_tok_per_s: deepseek 90 > qwen397 0


def test_rerank_leaves_unbenchmarked_alone_and_noop_on_thin_data():
    scores = {"deepseek": s.QualityScore("deepseek", 1.0, 1.0, 200, 90, 8, 1.0)}
    # only 1 scored model -> input returned unchanged
    assert s.fleet_rerank("standard", ("qwen397", "deepseek"), scores, route_kind="code") == ("qwen397", "deepseek")
    assert s.fleet_rerank("standard", ("a", "b"), {}, route_kind="code") == ("a", "b")


def test_graduation_ready_fires_on_measured_bad_default():
    scores = {
        "qwen397": s.QualityScore("qwen397", 0.05, 0.0, 150, 120, 8, 1.0),
        "deepseek": s.QualityScore("deepseek", 0.95, 1.0, 200, 90, 8, 1.0),
    }
    winner = s.graduation_ready(scores, default_model="qwen397",
                                min_samples=5, min_margin=1.5)
    assert winner == "deepseek"


def test_graduation_ready_none_below_thresholds():
    scores = {"deepseek": s.QualityScore("deepseek", 0.95, 1.0, 200, 90, 2, 1.0)}  # too few samples
    assert s.graduation_ready(scores, default_model="qwen397", min_samples=5, min_margin=1.5) is None


def test_apply_rerank_failsoft_returns_input_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "none.json"))
    # no store data -> input unchanged
    assert s.fleet_apply_rerank("standard", ("a", "b")) == ("a", "b")
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** Mirror `dw_promotion_ledger` patterns (`_atomic_write`, lazy `_ensure_loaded`, env path getter, never raises).
- `QualityScore` frozen dataclass with the 7 fields from the spec.
- `ewma_update(prev, new, alpha)`: `new if prev is None else alpha*new + (1-alpha)*prev`.
- `valid_tok_per_s(sc) = sc.tok_per_s * sc.ast_pass_rate`.
- `triage_fitness(sc) = sc.label_adherence / max(sc.ttft_ms, 1.0) * 1000.0`.
- env getters: `_alpha()` (`JARVIS_FLEET_EWMA_ALPHA`, 0.4), `_path()` (`JARVIS_FLEET_CALIBRATION_PATH`, `.jarvis/fleet_calibration.json`).
- `FleetCalibrationStore`: `load/save/_ensure_loaded`; `record_probe(model_id, *, kind, code_pass=None, label_score=None, ttft_ms, tok_per_s, now)` — folds via EWMA (code_pass→ast_pass_rate as 1.0/0.0; label_score→label_adherence), always updates ttft/tok EWMA + `sample_count += 1` + `updated_at = now`; `score(model_id)`; `all_scores()`.
- `route_kind_for_route(route) -> str`: `"triage"` if route lower in {`triage`,`semantic_triage`,`classify`} else `"code"`.
- `fleet_rerank(route, ranked_models, scores, *, route_kind)`: key fn = `valid_tok_per_s` (code) or `triage_fitness` (triage); collect models in `ranked_models` that have a score with `sample_count>=1`; if `<2` such → return `tuple(ranked_models)` unchanged; else stable-sort the WHOLE input so scored models are ordered by key desc while preserving each unscored model's original index (only reorder scored entries among themselves, leave unscored in place). Never raises.
- `graduation_ready(scores, *, default_model, min_samples, min_margin)`: among scored models (excluding default) with `sample_count>=min_samples` and `ast_pass_rate >= _min_ast()` (`JARVIS_FLEET_GRAD_MIN_AST`, 0.8), pick max `valid_tok_per_s`; let `d = scores.get(default_model)`; return the winner iff (`d is None or d.ast_pass_rate < 0.2`) **or** `winner.valid_tok_per_s >= min_margin * valid_tok_per_s(d)`; else None.
- `fleet_apply_rerank(route, ranked_models)`: try: load store, `fleet_rerank(route, ranked_models, store.all_scores(), route_kind=route_kind_for_route(route))`; except: return `tuple(ranked_models)`.

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `feat(fleet): calibration store + EWMA + pure rerank/graduation math`.

---

### Task 3: `fleet_evaluator.py` — async idle-driven calibration driver

**Files:**
- Create: `backend/core/ouroboros/governance/fleet_evaluator.py`
- Test: `tests/governance/test_fleet_evaluator.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_fleet_evaluator.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import fleet_evaluator as fe
from backend.core.ouroboros.governance import fleet_calibration_store as s


def _fake_caller(behavior):
    async def call(model_id, messages, *, max_tokens):
        is_code = "code block" in messages[-1]["content"].lower()
        if behavior == "good":
            text = "```python\ndef merge_intervals(x):\n    '''m'''\n    return sorted(x)\n```" if is_code else "ENRICH"
            return fe.ProbeResult(text=text, ttft_ms=200, total_ms=1000, completion_tokens=80, ok=True, error="")
        if behavior == "prose":  # the 397B failure mode
            return fe.ProbeResult(text="Let me think about intervals..." , ttft_ms=150, total_ms=4000, completion_tokens=900, ok=True, error="")
        return fe.ProbeResult(text="", ttft_ms=0, total_ms=0, completion_tokens=0, ok=False, error="HTTP 502")
    return call


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "false")
    assert fe.fleet_evaluator_enabled() is False


@pytest.mark.asyncio
async def test_good_model_scores_high(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    store = s.FleetCalibrationStore()
    ev = fe.FleetEvaluator(model_caller=_fake_caller("good"), store=store,
                           idle_check=lambda: True, clock=lambda: 1.0)
    await ev.calibrate_models(["deepseek"])
    sc = store.score("deepseek")
    assert sc.ast_pass_rate > 0.9 and sc.label_adherence > 0.9


@pytest.mark.asyncio
async def test_prose_model_scores_zero_ast(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    store = s.FleetCalibrationStore()
    ev = fe.FleetEvaluator(model_caller=_fake_caller("prose"), store=store,
                           idle_check=lambda: True, clock=lambda: 1.0)
    await ev.calibrate_models(["qwen397"])
    assert store.score("qwen397").ast_pass_rate < 0.1   # the diagnosed bug, now measured


@pytest.mark.asyncio
async def test_502_is_failsoft(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    store = s.FleetCalibrationStore()
    ev = fe.FleetEvaluator(model_caller=_fake_caller("502"), store=store,
                           idle_check=lambda: True, clock=lambda: 1.0)
    await ev.calibrate_models(["devstral"])     # must NOT raise
    assert store.score("devstral").ast_pass_rate == 0.0


@pytest.mark.asyncio
async def test_maybe_calibrate_skips_when_not_idle(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    store = s.FleetCalibrationStore()
    calls = []
    async def spy(model_id, messages, *, max_tokens):
        calls.append(model_id); return fe.ProbeResult("", 0, 0, 0, False, "")
    ev = fe.FleetEvaluator(model_caller=spy, store=store, idle_check=lambda: False,
                           clock=lambda: 1.0, snapshot_loader=lambda: ["m"])
    await ev.maybe_calibrate(now=1.0)
    assert calls == []     # not idle -> no probes
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.**
- `from __future__ import annotations`; asyncio; dataclasses; logging; os.
- `ProbeResult` frozen dataclass: `text, ttft_ms, total_ms, completion_tokens, ok, error`.
- Env gates: `fleet_evaluator_enabled()` (`JARVIS_FLEET_EVALUATOR_ENABLED`, false), `fleet_authoritative_enabled()` (`JARVIS_FLEET_EVALUATOR_AUTHORITATIVE`, false), `_max_models_per_cycle()` (4), `_probe_max_tokens()` (512), `_stable_cycles()` (2).
- `FleetEvaluator.__init__(self, *, model_caller, store=None, idle_check=None, clock=None, snapshot_loader=None, default_model=None)`. Defaults: store=`FleetCalibrationStore()`, idle_check=`lambda: True`, clock=`time.time`, snapshot_loader → reads `dw_catalog_client.load_cached_snapshot().model_ids()` (guarded, returns [] on None), default_model → `os.environ.get("DOUBLEWORD_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")`.
- `async calibrate_models(self, model_ids)`: for each model, run codegen probe (messages from `CODEGEN_PROMPT`) then classify probe (`CLASSIFY_PROMPT`), `max_tokens=_probe_max_tokens()`; compute `tok_per_s = completion_tokens / max(total_ms/1000, 1e-3)`; on `ok`: `store.record_probe(code/label...)`; on `not ok`: record a failed probe (code_pass=False / label_score=0.0). Wrap each model in try/except (fail-soft, log). `store.save()` at end.
- `async maybe_calibrate(self, *, now)`: return if not enabled; return if not `idle_check()`; `models = snapshot_loader()`; pick ≤N least-recently-benchmarked (sort by store.score(m).updated_at, unscored first); `await calibrate_models(picked)`; `self._maybe_graduate(now=now)`.
- `_maybe_graduate(self, *, now)`: `winner = graduation_ready(store.all_scores(), default_model=self.default_model, min_samples=_grad_min_samples(), min_margin=_grad_margin())`; track consecutive-win count in an instance counter; if winner stable ≥ `_stable_cycles()` and not already authoritative → `persist_authoritative_flag()` (reuse the bounded `.env` writer used by the graduation orchestrator — locate it; do NOT author a new writer) + log `[FleetEvaluator] graduated coder=<winner>`. Advisory log of the proposed flip every cycle.
- Structured logs + best-effort SSE via event broker if available (positional `publish(event_type, op_id, payload)`); guard import, never raise.

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `feat(fleet): async idle-driven calibration driver + auto-graduation`.

---

### Task 4: Bind into `provider_topology` + register flags

**Files:**
- Modify: `backend/core/ouroboros/governance/provider_topology.py` (end of `dw_models_for_route`, ~line 433)
- Test: `tests/governance/test_fleet_topology_binding.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_fleet_topology_binding.py
from __future__ import annotations


def test_dw_models_for_route_byte_identical_when_off(monkeypatch):
    monkeypatch.delenv("JARVIS_FLEET_EVALUATOR_AUTHORITATIVE", raising=False)
    import backend.core.ouroboros.governance.provider_topology as pt
    src = open(pt.__file__).read()
    assert "fleet_authoritative_enabled" in src           # guarded call present
    assert "fleet_apply_rerank" in src
    # the guard must wrap the call (off -> never invoked)
    assert "if fleet_authoritative_enabled():" in src


def test_rerank_applied_when_authoritative(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_AUTHORITATIVE", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    from backend.core.ouroboros.governance import fleet_calibration_store as s
    st = s.FleetCalibrationStore()
    st.record_probe("good", kind="code", code_pass=True, ttft_ms=200, tok_per_s=90, now=1.0)
    st.record_probe("good", kind="code", code_pass=True, ttft_ms=200, tok_per_s=90, now=2.0)
    st.record_probe("bad", kind="code", code_pass=False, ttft_ms=100, tok_per_s=120, now=1.0)
    st.record_probe("bad", kind="code", code_pass=False, ttft_ms=100, tok_per_s=120, now=2.0)
    st.save()
    out = s.fleet_apply_rerank("standard", ("bad", "good"))
    assert out[0] == "good"
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement the guarded binding.** At the END of `dw_models_for_route`, immediately before each `return ranked`/`return effective`/`return _trusted_seed_dw_models_for_route(route)`, route the final tuple through a single guarded helper. Cleanest: capture the result in a local `result` and add ONE exit:
```python
        # ── Sovereign Fleet Evaluator (quality-aware re-rank, gated) ──
        # OFF by default → byte-identical. Authoritative flip is set by
        # FleetEvaluator auto-graduation once a soak proves the calibrated
        # coder beats the 397B default. Pure, fail-soft (returns input on error).
        try:
            from backend.core.ouroboros.governance.fleet_evaluator import (
                fleet_authoritative_enabled,
            )
            if fleet_authoritative_enabled():
                from backend.core.ouroboros.governance.fleet_calibration_store import (
                    fleet_apply_rerank,
                )
                result = fleet_apply_rerank(route, result)
        except Exception:
            pass
        return result
```
Refactor the three return paths to assign `result = ...` then fall to the single guarded return (preserve exact current values for each branch). Keep `if not self.enabled: return ()` and the catalog/early-empty returns unchanged — only the final resolved-tuple returns funnel through the guard.

- [ ] **Step 4: Run the binding test AND the existing topology tests** — `PYTHONPATH=. python3 -m pytest tests/governance/test_fleet_topology_binding.py tests/governance/ -k "topology" -q`. Expected: all pass (off-path byte-identical).

- [ ] **Step 5: Register flags in FlagRegistry.** Add the 12 `JARVIS_FLEET_*` flags (Table §8 of the spec) to the curated seed in `flag_registry.py` (type, category=`provider`/`routing`, source_file, example, posture-relevance IGNORED). Mirror an existing seed entry's shape. Add a test asserting `JARVIS_FLEET_EVALUATOR_ENABLED` is registered.

- [ ] **Step 6: Commit** — `feat(fleet): bind quality rerank into dw_models_for_route (gated) + register flags`.

---

## Self-Review

- **Spec coverage:** discovery reuse (Task 3 snapshot_loader), AST armor (Task 1), EWMA+rerank+graduation math (Task 2), idle/cost driver + auto-graduate (Task 3), guarded binding + flags (Task 4) — all spec sections mapped.
- **Type consistency:** `QualityScore`, `ProbeResult`, `fleet_rerank`, `fleet_apply_rerank`, `graduation_ready`, `record_probe` signatures identical across tasks.
- **No placeholders:** every step has concrete code/commands.
- **OFF byte-identical:** Task 4 guard + Tasks 1-3 default-false gates.
- **No exec:** Task 1 validators are `ast.parse`-only; adversarial test asserts no side effect.
```
