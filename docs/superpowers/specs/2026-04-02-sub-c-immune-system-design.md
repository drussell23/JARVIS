# Sub-project C: The Immune System

**Date:** 2026-04-02
**Parent:** [Ouroboros HUD Pipeline Program](2026-04-02-ouroboros-hud-pipeline-program.md)
**Status:** Approved
**Depends on:** Sub-project A (sensor wiring), Sub-project B (context injection)

## Problem

The Ouroboros governance pipeline has strong infrastructure (multi-adapter test execution, security review, benchmarking) but lacks three deterministic guards:

1. **No duplication detection** — Generated code can silently duplicate existing functions/logic. Sub-project B's prompt engineering reduces this but provides no hard gate.
2. **No diff-aware similarity gate** — A patch that copies a large block and tweaks one line passes all current gates.
3. **No regression gating** — PatchBenchmarker computes pass_rate, coverage_pct, complexity_delta, and lint_violations but never enforces thresholds. VERIFY always advances to COMPLETE.

All three are **deterministic policy gates** (Manifesto §6 boundary mandate) — no LLM calls, pure measured checks.

## Changes

### 1. Duplication Guard in VALIDATE

**File:** `backend/core/ouroboros/governance/duplication_checker.py` (new)
**Called from:** `orchestrator.py` `_run_validation()`, between AST preflight and sandbox write

#### Algorithm: Per-Unit Canonical Fingerprint + Set Similarity

1. Parse both candidate and source with `ast.parse(content, mode='exec')`. On `SyntaxError` in either, skip guard (source may be broken; candidate syntax already checked by AST preflight).

2. Extract function/class units from both trees. For each unit (`FunctionDef`, `AsyncFunctionDef`, `ClassDef` at top-level and one level deep):
   - **Normalize identifiers:** Replace all local variable names with positional placeholders (`_v0`, `_v1`, ...) per scope. Keep parameter names as-is (they're part of the API contract).
   - **Keep literal kinds** (distinguish `ast.Constant` type: str vs int vs float) but replace values with a canonical placeholder per kind (`"_S"`, `0`, `0.0`).
   - **Keep call shapes:** `foo(x, y)` becomes `call(_v0, _v1)` — structure preserved, names normalized.
   - Compute fingerprint: `hashlib.sha256(ast.dump(normalized_node, include_attributes=False).encode()).hexdigest()`

3. **Scope:** Only compare units that appear in the candidate but NOT in the source (by function name). Modified versions of existing functions are expected to be similar — skip by matching on original name.

4. **Strict match:** If any new candidate unit's fingerprint exactly matches any source unit's fingerprint → fail with `failure_class="duplication"`.

5. **Fuzzy match:** For each new candidate unit, extract a multiset of normalized statement type sequences (e.g., `["Assign", "Call", "If", "Return"]`). Compute Jaccard similarity against each source unit: `|intersection| / |union|` where intersection/union use min/max counts per element. If Jaccard > 0.8 → fail.

6. **Known gap:** In-place body clone (model keeps function name but replaces body with copied logic from elsewhere). Mitigation: Section 2's diff-aware gate catches this if the added lines overlap significantly with existing source. For Sub-project C scope, this is accepted.

**Threshold:** `JARVIS_VALIDATE_DUPLICATION_JACCARD` env var, default `0.8`.

**Failure output:** `"Duplication detected: new function 'detect_search_antipattern' is structurally similar to existing '_filter_messaging_antipatterns' (Jaccard: 0.94)"`

**Performance:** Pure AST operations, no subprocess, no LLM. <100ms for typical files.

### 2. Diff-Aware Similarity Gate in GATE

**File:** `backend/core/ouroboros/governance/similarity_gate.py` (new)
**Called from:** `orchestrator.py` GATE phase, between SecurityReviewer and autonomy tier gate

#### Algorithm: N-gram Overlap on Added Hunks

1. Compute added lines between source and candidate using `difflib.SequenceMatcher` on normalized line lists (not `unified_diff` output — avoids parsing diff markup).

2. Normalize both source and candidate lines: use Python `tokenize` module to strip comments reliably, then strip whitespace and blank lines.

3. Extract only **added/changed lines** from the candidate (lines present in candidate but not in source after normalization).

4. Build 3-gram set from the normalized added lines. Build 3-gram set from the full normalized source.

5. Compute overlap ratio: `|added_ngrams & source_ngrams| / |added_ngrams|` (what fraction of added content already exists in the source).

6. If overlap > threshold → set `risk_tier = RiskTier.APPROVAL_REQUIRED` with reason `"High similarity between added code and existing source (overlap: 0.XX)"`. Does NOT hard-block — escalates for human review.

**Threshold:** `JARVIS_GATE_SIMILARITY_THRESHOLD` env var, default `0.7`.

**Scoping:** Only added lines are checked, so "small edit in huge file" does not trigger. Pure deletions have no added lines → overlap 0 → no escalation.

**Normalization:** Use `tokenize.generate_tokens()` on `io.StringIO(line)` to strip comments. Fall back to simple `line.split('#')[0].strip()` if tokenize fails on a line.

### 3. Regression Gating in VERIFY

**File:** `backend/core/ouroboros/governance/verify_gate.py` (new)
**Called from:** `orchestrator.py` VERIFY phase, after `_run_benchmark()` and before advancing to COMPLETE

#### New State Machine Path

Today, VERIFY always advances to COMPLETE:
```
VERIFY → _run_benchmark → COMPLETE
```

After this change:
```
VERIFY → _run_benchmark → _enforce_verify_thresholds
  → pass: COMPLETE
  → fail: rollback via pre_apply_snapshots → CANCELLED (reason="verify_regression")
```

#### Threshold Table

| Metric | Threshold | Env Var | Default | Behavior |
|--------|-----------|---------|---------|----------|
| `pass_rate` | `>= min` | `JARVIS_VERIFY_MIN_PASS_RATE` | `1.0` | CANCELLED |
| `coverage_pct` | `>= baseline - max_drop` | `JARVIS_VERIFY_COVERAGE_DROP_MAX` | `5.0` | CANCELLED |
| `complexity_delta` | `<= max` | `JARVIS_VERIFY_MAX_COMPLEXITY_DELTA` | `2.0` | CANCELLED |
| `lint_violations` | `<= max` | `JARVIS_VERIFY_MAX_LINT_VIOLATIONS` | `5` | CANCELLED |
| `timed_out` | `False` | — | — | CANCELLED |
| `error` | `None` | — | — | CANCELLED |

**Edge case: `pass_rate` when 0 tests collected:** If pytest reports 0 tests collected, `pass_rate` is undefined. Treat as `1.0` (no tests to regress). If pytest fails to run at all, `error` is set → hard fail.

#### Coverage Baseline

PatchBenchmarker snapshots complexity before APPLY (via `pre_apply_snapshots`) but not coverage. Two options:

**Option chosen: Reuse VALIDATE coverage when available.** If PythonAdapter's pytest run during VALIDATE produced a coverage report (it already runs `pytest`), capture that pass_rate and coverage_pct as the baseline on `ctx`. This avoids duplicating a full pytest+cov run.

If no VALIDATE coverage is available (e.g., VALIDATE used a different test subset, or coverage tool missing), skip coverage regression check — the gate is best-effort, not mandatory.

**Storage:** Add `ctx.with_pre_apply_metrics(pass_rate=..., coverage_pct=...)` (or extend `pre_apply_snapshots` dict with `"_metrics"` key) to carry baseline through APPLY.

#### Rollback Mechanism

On threshold failure:
1. Read `ctx.pre_apply_snapshots` (dict of `{rel_path: original_content_str}`)
2. For each file, write `original_content` back to disk
3. Verify restoration via SHA256 hash comparison
4. Advance to `OperationPhase.CANCELLED` with `terminal_reason_code="verify_regression"`
5. Record in ledger as `OperationState.FAILED` with `rollback_occurred=True`

This reuses the same `pre_apply_snapshots` that complexity baselines use. No new rollback infrastructure needed.

#### Telemetry

Emit a structured event when any threshold fires:
```python
{
    "event": "verify_gate_fired",
    "op_id": ctx.op_id,
    "metric": "pass_rate",
    "threshold": 1.0,
    "actual": 0.85,
    "action": "cancelled",
    "files": list(ctx.target_files),
}
```

Logged at WARNING level. Consumed by existing telemetry bus for §7 observability.

## Testing Strategy

### Unit Tests

| Test | File | What it verifies |
|------|------|-----------------|
| `test_exact_fingerprint_duplicate` | `test_duplication_checker.py` | Strict match catches copy-paste |
| `test_fuzzy_jaccard_above_threshold` | `test_duplication_checker.py` | Near-duplicate caught at 0.8 |
| `test_fuzzy_jaccard_below_threshold` | `test_duplication_checker.py` | Legitimately different code passes |
| `test_modified_function_not_flagged` | `test_duplication_checker.py` | Existing function modification skipped |
| `test_syntax_error_skips_guard` | `test_duplication_checker.py` | Graceful skip on unparseable source |
| `test_similarity_gate_high_overlap` | `test_similarity_gate.py` | >70% n-gram overlap escalates |
| `test_similarity_gate_small_edit` | `test_similarity_gate.py` | Small edit doesn't trigger |
| `test_similarity_gate_deletion_only` | `test_similarity_gate.py` | Pure deletion: overlap 0 |
| `test_verify_pass_rate_failure` | `test_verify_gate.py` | pass_rate < 1.0 → CANCELLED |
| `test_verify_coverage_regression` | `test_verify_gate.py` | coverage drop > 5% → CANCELLED |
| `test_verify_complexity_spike` | `test_verify_gate.py` | complexity_delta > 2 → CANCELLED |
| `test_verify_lint_cap` | `test_verify_gate.py` | lint_violations > 5 → CANCELLED |
| `test_verify_timed_out` | `test_verify_gate.py` | timed_out=True → CANCELLED |
| `test_verify_all_pass` | `test_verify_gate.py` | All metrics good → None (continue) |
| `test_verify_zero_tests_passes` | `test_verify_gate.py` | 0 tests collected → pass |
| `test_rollback_restores_files` | `test_verify_gate.py` | Files restored from pre_apply_snapshots |

## Files Created/Modified

| File | Action |
|------|--------|
| `backend/core/ouroboros/governance/duplication_checker.py` | Create |
| `backend/core/ouroboros/governance/similarity_gate.py` | Create |
| `backend/core/ouroboros/governance/verify_gate.py` | Create |
| `backend/core/ouroboros/governance/orchestrator.py` | Modify (VALIDATE, GATE, VERIFY wiring) |
| `tests/governance/test_duplication_checker.py` | Create |
| `tests/governance/test_similarity_gate.py` | Create |
| `tests/governance/test_verify_gate.py` | Create |

## Out of Scope

- LLM-based semantic duplication detection (Manifesto: deterministic gates only)
- Cross-repo consistency checks (future)
- Contract/spec verification (future)
- Import/dependency analysis (future)
- DaemonNarrator wiring (Sub-project D)
