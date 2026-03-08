# Phase 2B: Code Generation with File Context — Design

**Date:** 2026-03-08
**Status:** Approved
**Scope:** Single-file, multi-candidate (max 3) sequential generation with enriched prompts,
strict schema validation, connectivity preflight, source-drift detection, and per-candidate
ledger provenance. No MAS, no multi-repo.

---

## Goal

Wire real LLM code generation (GCP J-Prime primary, Claude fallback) into the Ouroboros
pipeline so generated candidates carry full file context, pass language-appropriate tests
before APPLY, and leave a complete, deterministic audit trail.

---

## Architecture Overview

**Four files touched — zero new modules.**

| File | Change |
|------|--------|
| `providers.py` | Prompt enrichment (file contents + context + source hash); new candidate schema (`schema_version`, `candidate_id`, `source_hash`, `source_path`); strict parser with reject-all-extras policy |
| `orchestrator.py` | Per-candidate ledger entries; source-drift check before APPLY; winner traceability in GATE metadata; budget guard pre-generation |
| `governed_loop_service.py` | Connectivity preflight spending from deadline; QUEUE_ONLY → CANCELLED; primary-fail+fallback-active → ledger + continue |
| `op_context.py` | Add `model_id: str = ""` to `GenerationResult` (frozen dataclass, backward-compatible default) |

---

## Component: Single Deadline Owner + Connectivity Preflight

`pipeline_deadline` is stamped **first** in `submit()` — before the preflight probe —
so there is exactly one budget owner for the entire operation.

```
submit()
  1. stamp pipeline_deadline = now() + pipeline_timeout_s     ← single budget owner
  2. ctx = ctx.with_pipeline_deadline(pipeline_deadline)
  3. remaining_s = (pipeline_deadline - now()).total_seconds()

  4. Budget pre-check:
       if remaining_s < MIN_GENERATION_BUDGET_S (30 s):
           ctx.advance(CANCELLED)
           ledger(FAILED, {reason_code: "budget_exhausted_pre_generation"})
           return early

  5. Connectivity preflight (spends from deadline):
       probe_timeout = min(5.0, remaining_s * 0.05)   # never > 5 % of budget
       primary_ok = await generator.primary.health_probe(timeout=probe_timeout)

       primary_ok  → continue silently
       not primary_ok, FSM != QUEUE_ONLY (fallback healthy):
           → continue (FSM routes to fallback)
           → ledger(non-terminal informational entry,
                    {reason_code: "primary_unavailable_fallback_active"})
       not primary_ok, FSM == QUEUE_ONLY:
           → ctx.advance(CANCELLED)
           → ledger(FAILED, {reason_code: "provider_unavailable"})
           → return early

  6. Proceed to orchestrator.run(ctx)
```

`MIN_GENERATION_BUDGET_S = 30.0` — env-configurable via `JARVIS_MIN_GENERATION_BUDGET_S`.

---

## Component: Prompt Enrichment (`providers.py`)

### File reading — size budget and hash

For each file in `ctx.target_files`:

```
1. Resolve path via _safe_context_path(repo_root, raw_path)
     → raises BlockedPathError if outside repo_root or follows symlink
2. Read content (UTF-8, errors=replace)
3. source_hash = SHA-256(content.encode())
4. Truncation:
     len(content) ≤ 6000 chars → include full content
     len(content)  > 6000 chars → first 4000 chars
                                   + "\n[TRUNCATED: {N} bytes, {M} lines omitted]\n"
                                   + last 1000 chars
5. Header per file:
     ## File: {path} [SHA-256: {hash[:12]}] [{size} bytes, {line_count} lines]
```

### Surrounding context — discovery with hard caps

```
Import context:
  Scan target file's top-level import lines (re match, first 60 lines only)
  Resolve imported module → source file in repo
  Hard cap: max 5 import source files
  Per-file: first 30 lines, total max 1500 chars across all
  Path security: _safe_context_path() on every discovered file

Test context:
  Look for test_*.py / *_test.py in tests/ that contains target module name
  Hard cap: max 2 test files
  Per-file: first 50 lines, total max 1500 chars across all
  Path security: same

Both sections: labelled "## Surrounding Context (read-only — do not modify)"
```

### Output schema injected into prompt

```
schema_version: "2b.1"

{
  "schema_version": "2b.1",
  "candidates": [
    {
      "candidate_id": "c1",
      "file_path":    "<repo-relative path>",
      "full_content": "<complete file content — not a diff>",
      "rationale":    "<one sentence, max 200 chars>"
    }
    // up to 3 candidates
  ],
  "provider_metadata": {
    "model_id":           "<model identifier>",
    "reasoning_summary":  "<max 200 chars>"
  }
}

Rules injected:
- 1–3 candidates. c1 = primary approach, c2 = alternative, c3 = minimal-change fallback.
- full_content must be the complete file (not a diff or patch).
- Python files must be ast.parse()-valid.
- Extra keys at any level are rejected. Return ONLY the JSON object.
```

### Path security helper

```python
def _safe_context_path(repo_root: Path, target: Path) -> Path:
    resolved = target.resolve()
    if resolved.is_symlink():
        raise BlockedPathError(f"Symlink not allowed in context: {target}")
    if not str(resolved).startswith(str(repo_root.resolve())):
        raise BlockedPathError(f"Context file outside repo root: {target}")
    return resolved
```

---

## Component: Strict Parser (`providers.py: _parse_generation_response`)

Validation sequence — fail-fast, deterministic reason codes:

```
1. JSON parse
     fail → RuntimeError("{provider}_schema_invalid:json_parse_error")

2. Top-level type check: must be dict
     fail → RuntimeError("{provider}_schema_invalid:expected_object")

3. schema_version == "2b.1"
     wrong / missing → RuntimeError("{provider}_schema_invalid:wrong_schema_version:{actual}")

4. Extra top-level keys (beyond schema_version, candidates, provider_metadata)
     any extra → RuntimeError("{provider}_schema_invalid:unexpected_keys:{key_list}")

5. candidates: non-empty list, length 1–3
     length 0 → RuntimeError("{provider}_schema_invalid:candidates_empty")
     missing   → RuntimeError("{provider}_schema_invalid:missing_candidates")

6. If model returned > 3 candidates:
     Log warning + ledger informational entry:
       {event: "candidates_normalized", original_count: N,
        dropped_ids: ["c4", "c5", ...]}
     Silently truncate to first 3 and continue (not a hard reject)

7. Per-candidate validation (for each of the ≤ 3):
     required fields: candidate_id (str), file_path (str), full_content (str), rationale (str)
       any missing → RuntimeError("{provider}_schema_invalid:candidate_{i}_missing_{field}")
     extra fields in candidate dict
       any extra → RuntimeError("{provider}_schema_invalid:candidate_{i}_unexpected_keys:{key_list}")
     Python file AST check: ast.parse(full_content)
       SyntaxError → skip candidate (failure_class="build"), continue to next
       If ALL candidates fail AST → RuntimeError("{provider}_schema_invalid:all_candidates_syntax_error")

8. Compute per-candidate:
     candidate_hash = SHA-256(full_content.encode())
     Add to candidate dict: {candidate_hash, source_hash (from ctx), source_path}

9. Return GenerationResult(
       candidates = tuple(validated_candidates),
       provider_name = provider_name,
       generation_duration_s = duration_s,
       model_id = provider_metadata.get("model_id", ""),
   )
```

**`failure_class` taxonomy (exhaustive):**

| Class | Meaning | VALIDATE outcome |
|-------|---------|-----------------|
| `"test"` | Test suite failures | CANCELLED |
| `"build"` | SyntaxError / AST parse failure / compiler error | CANCELLED |
| `"infra"` | Runner crashed, timeout, IO error | POSTMORTEM |
| `"budget"` | Remaining budget ≤ 0 before subprocess | CANCELLED |
| `"security"` | BlockedPathError | CANCELLED |

`"parse"` is **not** a valid `failure_class` — SyntaxError maps to `"build"` at all layers.

---

## Component: Sequential Validation Loop (`orchestrator.py: VALIDATE phase`)

```
for candidate in generation.candidates:          # max 3, enforced by parser
    t_start = monotonic()
    remaining_s = (ctx.pipeline_deadline - now()).total_seconds()

    if remaining_s <= 0:
        # Per-candidate ledger (budget_exhausted)
        await _record_ledger(ctx, ..., {
            "event":              "candidate_validated",
            "candidate_id":       candidate["candidate_id"],
            "candidate_hash":     candidate["candidate_hash"],
            "validation_outcome": "skip",
            "failure_class":      "budget",
            "duration_s":         0.0,
            "provider":           generation.provider_name,
            "model":              generation.model_id,
        })
        break  # → CANCELLED(budget_exhausted)

    validation = await _run_validation(ctx, candidate, remaining_s)
    duration_s = monotonic() - t_start

    # Per-candidate ledger entry — always, pass or fail
    await _record_ledger(ctx, ..., {
        "event":              "candidate_validated",
        "candidate_id":       candidate["candidate_id"],
        "candidate_hash":     candidate["candidate_hash"],
        "validation_outcome": "pass" if validation.passed else "fail",
        "failure_class":      validation.failure_class,
        "duration_s":         duration_s,
        "provider":           generation.provider_name,
        "model":              generation.model_id,
    })

    if validation.passed:
        winner = candidate
        break

    if validation.failure_class == "infra":     → POSTMORTEM (return)
    if validation.failure_class == "budget":    → CANCELLED  (return)
    # test/build/security: try next candidate

# No winner
if winner is None:
    → CANCELLED(reason="no_candidate_valid")
    → ledger(FAILED, {reason_code: "no_candidate_valid",
                      candidates_tried: [c["candidate_id"] for c in generation.candidates]})
```

`_run_validation()` uses `candidate["file_path"]` and `candidate["full_content"]` (updated from Phase 2A's `file`/`content` keys).

---

## Component: Source-Drift Check (orchestrator.py, pre-APPLY)

Before advancing from GATE to APPLY, re-read the target file and verify it hasn't changed since generation:

```python
for candidate in [winner]:
    current_content = _safe_read_file(repo_root / candidate["file_path"])
    current_hash = SHA-256(current_content.encode())
    if current_hash != candidate["source_hash"]:
        ctx = ctx.advance(OperationPhase.CANCELLED)
        await _record_ledger(ctx, FAILED, {
            "reason_code":         "source_drift_detected",
            "file_path":           candidate["file_path"],
            "expected_source_hash": candidate["source_hash"],
            "actual_source_hash":   current_hash,
        })
        return ctx
```

---

## Component: Winner Traceability

On advancing to GATE with a passing candidate:

```python
await _record_ledger(ctx, ..., {
    "event":                  "validation_complete",
    "winning_candidate_id":   winner["candidate_id"],
    "winning_candidate_hash": winner["candidate_hash"],
    "winning_file_path":      winner["file_path"],
    "source_hash":            winner["source_hash"],
    "source_path":            winner["source_path"],
    "provider":               generation.provider_name,
    "model":                  generation.model_id,
    "total_candidates_tried": len(generation.candidates),
})
```

`_build_change_request()` passes `winning_candidate_id` and `winning_candidate_hash` in `ChangeRequest` metadata so ChangeEngine preserves them in the apply ledger entry.

---

## `GenerationResult` Update (`op_context.py`)

```python
@dataclass(frozen=True)
class GenerationResult:
    candidates:           Tuple[Dict[str, Any], ...]
    provider_name:        str
    generation_duration_s: float
    model_id:             str = ""    # ← NEW: backward-compatible default
```

---

## Failure Mapping (Complete)

| Condition | OperationPhase | OperationState | Ledger reason_code |
|-----------|---------------|----------------|-------------------|
| QUEUE_ONLY at preflight | CANCELLED | FAILED | `provider_unavailable` |
| Budget < MIN at preflight | CANCELLED | FAILED | `budget_exhausted_pre_generation` |
| Primary unavailable, fallback healthy | (continues) | (informational) | `primary_unavailable_fallback_active` |
| Schema invalid (parse fail) | CANCELLED | FAILED | `{provider}_schema_invalid:{detail}` |
| All candidates fail tests/build | CANCELLED | FAILED | `no_candidate_valid` |
| infra failure during candidate validation | POSTMORTEM | FAILED | `validation_infra_failure` |
| Budget exhausted during candidate loop | CANCELLED | FAILED | `validation_budget_exhausted` |
| Source drift detected pre-APPLY | CANCELLED | FAILED | `source_drift_detected` |
| All candidates pass → APPLY succeeds | COMPLETE | COMPLETE | — |
| APPLY → tests fail → rollback | POSTMORTEM | ROLLED_BACK | `verify_test_failure` |

---

## Hard Constraints Checklist

- [x] Zero new modules — all changes in 4 existing files
- [x] Single `pipeline_deadline` owner — stamped in `submit()`, never re-stamped
- [x] Preflight spends from deadline (not independent timer)
- [x] `failure_class="parse"` does not exist — SyntaxError → `"build"` everywhere
- [x] Extra keys → strict reject + reason code (no silent drop)
- [x] >3 candidates → ledger normalization event + truncate to 3 (not reject)
- [x] Context file limits: max 5 import files + max 2 test files
- [x] `source_hash` + `source_path` in every candidate dict post-parse
- [x] Source-drift check before APPLY — mismatch → CANCELLED
- [x] `winning_candidate_id` + `winning_candidate_hash` in GATE + ChangeRequest metadata
- [x] `op_id` continuous across generate/validate/apply/verify/ledger/comm
- [x] No AST-only success path (TestRunner required in VALIDATE)
- [x] Full stdout/stderr in ledger `data`, never in ValidationResult or OperationContext
- [x] Path security: `_safe_context_path()` on all discovered files; BlockedPathError → `failure_class="security"`
